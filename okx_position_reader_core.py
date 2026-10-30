"""OFFLINE ONLY -- shared position GET scheduling/validation contracts.

No OKX transport, no secrets, no trading, no persistent state, no systemd.
The provider and quota are explicit FAKE/injected protocols. Do not install to VPS.
"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from typing import Protocol

INSTRUMENT_RE = re.compile(r"^[A-Z0-9]+-USDT-SWAP$")
MAX_ACCOUNT_BOTS = 10
MAX_PENDING_CLIENTS = 256  # IPC flood protection; does not change number of bots


class ReaderError(RuntimeError):
    pass


class RegistryError(ReaderError):
    pass


class QuotaRequestAbandoned(ReaderError):
    """All clients left before a shared /positions quota slot was granted."""
    pass


class SnapshotError(ReaderError):
    pass


class Priority(IntEnum):
    EMERGENCY = 0
    EXIT = 1
    NORMAL = 2
    BACKGROUND = 3


@dataclass(frozen=True)
class Registration:
    name: str
    instrument: str
    # The caller must prove verified terminal + exchange-flat externally.
    verified_archived: bool = False


def validate_registrations(entries: list[Registration]) -> tuple[Registration, ...]:
    if not isinstance(entries, list):
        raise RegistryError('Registry must be a complete list')
    names, pairs, active = set(), set(), []
    for item in entries:
        if not isinstance(item, Registration):
            raise RegistryError('Malformed registration (fail-closed)')
        if (not isinstance(item.name, str) or not item.name or
                not isinstance(item.verified_archived, bool)):
            raise RegistryError('Malformed registration identity')
        if not isinstance(item.instrument, str) or not INSTRUMENT_RE.fullmatch(item.instrument):
            raise RegistryError('Unexpected instrument')
        if item.name in names:
            raise RegistryError('Duplicate bot name')
        names.add(item.name)
        if not item.verified_archived:
            if item.instrument in pairs:
                raise RegistryError('Only one non-archived bot per instrument')
            pairs.add(item.instrument)
            active.append(item)
    if len(active) > MAX_ACCOUNT_BOTS:
        raise RegistryError('Account limit: at most ten non-archived bots')
    return tuple(active)


def assert_can_create(entries: list[Registration], proposed: Registration) -> None:
    if not isinstance(proposed, Registration) or proposed.verified_archived:
        raise RegistryError('Proposed bot is invalid/archived')
    validate_registrations(entries + [proposed])


class Quota(Protocol):
    async def take(self, priority_supplier) -> None: ...


class Provider(Protocol):
    async def fetch(self, instruments: tuple[str, ...]) -> dict: ...


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    started_ns: int
    completed_ns: int
    instruments: tuple[str, ...]
    by_instrument: dict[str, tuple[dict, ...]]
    instrument_errors: dict[str, str]
    quota_wait_ms: float | None = None
    fetch_ms: float | None = None
    batch_size: int = 0

    # True only after an exact-scope, complete, validated signed GET.
    absence_proven: bool = False
    def slice(self, instrument: str, *, not_before_ns: int, trace_id: str = '',
              request_received_ns: int | None = None) -> dict:
        if instrument not in self.instruments or self.started_ns < not_before_ns:
            raise SnapshotError('GET began before critical freshness barrier')
        if instrument in self.instrument_errors:
            raise SnapshotError('Instrument isolated as invalid: ' + self.instrument_errors[instrument])
        return {
            'snapshot_id': self.snapshot_id,
            'get_started_mono_ns': self.started_ns,
            'get_completed_mono_ns': self.completed_ns,
            'covered_instruments': list(self.instruments),
            'instrument': instrument,
            'rows': copy.deepcopy(list(self.by_instrument[instrument])),
            # Empty array is NOT an authenticated 'zero position' proof.
            'absence_is_proven_zero': bool(self.absence_proven and not self.by_instrument[instrument]),
            'trace_id': trace_id,
            'request_received_mono_ns': request_received_ns,
            'quota_wait_ms': self.quota_wait_ms,
            'fetch_ms': self.fetch_ms,
            'batch_size': self.batch_size,
        }


def build_snapshot(*, response: dict, instruments: tuple[str, ...],
                   started_ns: int, completed_ns: int) -> Snapshot:
    if type(started_ns) is not int or type(completed_ns) is not int or started_ns <= 0 or completed_ns < started_ns:
        raise SnapshotError('Invalid GET time boundary')
    if (not isinstance(response, dict) or response.get('code') != '0'
            or not isinstance(response.get('data'), list)):
        raise SnapshotError('Unverified OKX-style response')
    if (not isinstance(instruments, tuple) or len(instruments) != len(set(instruments))
            or not 1 <= len(instruments) <= MAX_ACCOUNT_BOTS
            or any(not isinstance(i, str) or not INSTRUMENT_RE.fullmatch(i) for i in instruments)):
        raise SnapshotError('Invalid instrument batch')
    grouped: dict[str, list[dict]] = {instrument: [] for instrument in instruments}
    errors: dict[str, str] = {}
    for row in response['data']:
        # A non-object or a row without an identifiable instId cannot safely
        # be attributed to one instrument; reject the complete payload.
        if not isinstance(row, dict) or not isinstance(row.get('instId'), str):
            raise SnapshotError('Unattributable malformed position row')
        instrument = row['instId']
        if instrument not in grouped:
            # Broader instType=SWAP responses legitimately contain OTHER pairs.
            continue
        if instrument in errors:
            continue
        if row.get('posSide') != 'net':
            errors[instrument] = 'Non-NET/unknown position side'
            grouped[instrument].clear()
            continue
        try:
            pos = Decimal(str(row['pos']))
            if not pos.is_finite():
                raise InvalidOperation
        except (KeyError, ValueError, TypeError, InvalidOperation):
            errors[instrument] = 'Malformed position quantity'
            grouped[instrument].clear()
            continue
        if grouped[instrument]:
            errors[instrument] = 'Duplicate NET position rows'
            grouped[instrument].clear()
            continue
        grouped[instrument].append(copy.deepcopy(row))
    # Do not treat an absent row as zero without a complete, exact-scope transport.
    scope = response.get('_reader_transport_scope')
    transport_complete = (isinstance(scope, dict)
        and scope.get('endpoint') == '/api/v5/account/positions'
        and scope.get('instType') == 'SWAP'
        and scope.get('complete') is True
        and type(scope.get('instIds')) is list
        and scope['instIds'] == list(instruments)
        and all(row['instId'] in grouped for row in response['data']))
    return Snapshot(secrets.token_hex(12), started_ns, completed_ns, instruments,
                    {k: tuple(v) for k, v in grouped.items()}, errors, absence_proven=transport_complete)


@dataclass
class _Waiter:
    instrument: str
    priority: Priority
    not_before_ns: int
    created_ns: int
    future: asyncio.Future = field(repr=False)
    trace_id: str = ''


class SharedReaderCore:
    """One in-flight batched GET; all eligible waiting requests may share it.

    Enforces no cache reuse and per-client freshness barriers. A background-
    only grace period lets new higher-priority demand join *before quota entry*.
    Quota arbitration against legacy processes is NOT implemented here.
    """
    def __init__(self, registrations: list[Registration], provider: Provider, quota: Quota,
                 *, background_grace_sec: float = 0.020,
                 quota_timeout_sec: float = 15.0, get_timeout_sec: float = 4.5,
                 account_uid: str | None = None):
        self.registrations = validate_registrations(registrations)
        self.account_uid = account_uid
        if not self.registrations or provider is None or quota is None:
            raise RegistryError('Must inject provider, quota and active registrations')
        if not 0 <= background_grace_sec <= 0.100:
            raise ValueError('Invalid background scheduling grace')
        if not 0 < quota_timeout_sec <= 30 or not 0 < get_timeout_sec <= 30:
            raise ValueError('Invalid bounded operation timeout')
        self.instruments = tuple(sorted(r.instrument for r in self.registrations))
        self.provider, self.quota = provider, quota
        self.background_grace_sec = background_grace_sec
        self.quota_timeout_sec, self.get_timeout_sec = quota_timeout_sec, get_timeout_sec
        self._waiters: list[_Waiter] = []
        self._wake = asyncio.Event()
        self._closed = False
        self._inflight = False
        self._inflight_since_ns = 0  # diagnostic only, monotonic
        self._worker_task: asyncio.Task | None = None
        self.metrics = deque(maxlen=1024)
        self.counters = {'get_batches': 0, 'coalesced_clients': 0, 'quota_failed': 0,
                         'fetch_failed': 0, 'canceled_clients': 0,
                         'quota_abandoned': 0, 'unused_quota_grants': 0,
                         'quota_granted': 0, 'provider_fetch_calls': 0}
        # Bounded diagnostic history; never enters scheduling/approval decisions.
        self._metric_seq = 0

    def interval_diagnostics(self, after_seq: int = 0) -> dict:
        """GET-free, side-effect-free metrics since previous journal sample.

        This is NOT a physical HTTP counter. Only the provider can report
        actual wire attempts. The 1024-entry ring preserves bounded memory.
        """
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError('Invalid diagnostic cursor')
        samples = [m for m in self.metrics if m.get('seq', 0) > after_seq]
        missing = max(0, self._metric_seq - after_seq - len(samples))
        def lat(field):
            xs = sorted(float(m[field]) for m in samples if type(m.get(field)) in (int,float))
            return {'n':len(xs), 'p50_ms':round(xs[(len(xs)-1)//2],2),
                    'p95_ms':round(xs[__import__('math').ceil(.95*len(xs))-1],2),
                    'max_ms':round(xs[-1],2)} if xs else {'n':0}
        return {'seq':self._metric_seq, 'batches':len(samples),
                'ring_samples_missing':missing,
                'served_clients':sum(m['batch_size'] for m in samples),
                'batch_size_max':max((m['batch_size'] for m in samples),default=0),
                'priority_batches':{str(i):sum(m['highest_priority']==i for m in samples)
                                    for i in range(4)},
                'quota_wait':lat('quota_wait_ms'), 'fetch':lat('fetch_ms')}

    def replace_registrations(self, entries: list[Registration]) -> None:
        '''Atomic offline model of registry reload, ONLY while idle.'''
        if self._closed or self._inflight or self._pending():
            raise RegistryError('Registry reload requires drained Reader')
        fresh = validate_registrations(entries)
        if not fresh:
            raise RegistryError('Cannot reload empty instrument set')
        # One event-loop turn: no await between validating and swapping.
        self.registrations = fresh
        self.instruments = tuple(sorted(r.instrument for r in fresh))

    async def start(self) -> None:
        if self._closed or self._worker_task is not None:
            raise ReaderError('Already started/closed')
        self._worker_task = asyncio.create_task(self._worker(), name='offline-position-reader')

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_exception(ReaderError('Reader closed'))
        self._waiters.clear()

    async def request(self, instrument: str, priority: Priority, *,
                      not_before_ns: int | None = None,
                      timeout_sec: float = 5.0, trace_id: str = '',
                      account_uid: str | None = None) -> dict:
        if self._closed or self._worker_task is None:
            raise ReaderError('Reader not running')
        if (not isinstance(self.account_uid, str) or not self.account_uid
                or account_uid != self.account_uid):
            raise ReaderError('Account UID mismatch; refuse snapshot')
        if instrument not in self.instruments:
            raise RegistryError('Unknown instrument')
        if not isinstance(priority, Priority):
            raise ReaderError('Unknown priority')
        now = time.monotonic_ns()
        barrier = now if not_before_ns is None else not_before_ns
        if type(barrier) is not int or barrier <= 0 or barrier > now + 1_000_000:
            raise ReaderError('Invalid/future freshness barrier')
        if type(timeout_sec) not in (float, int) or not math.isfinite(timeout_sec) or not 0 < timeout_sec <= 30:
            raise ReaderError('Invalid timeout')
        if not isinstance(trace_id, str) or len(trace_id) > 128:
            raise ReaderError('Invalid trace ID')
        if len(self._pending()) >= MAX_PENDING_CLIENTS:
            raise ReaderError('Bounded IPC waiter limit reached')
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = _Waiter(instrument, priority, barrier, now, future, trace_id)
        self._waiters.append(waiter)
        self._wake.set()
        try:
            return await asyncio.wait_for(future, timeout_sec)
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            if future.cancelled():
                self.counters['canceled_clients'] += 1

    def highest_waiting_priority(self) -> int:
        """Live priority supplier for quota queue; no stale cached category."""
        # Consulted repeatedly by the cross-process FIFO worker thread.
        # A timed-out client must not leave a phantom queue ticket behind and
        # later consume a quota permit for a GET nobody needs. Raising here
        # makes PriorityPositionsFIFO remove its own ticket safely.
        pending = self._pending()
        if not pending or self._closed:
            raise QuotaRequestAbandoned('No live Reader clients need /positions')
        return int(min(w.priority for w in pending))

    def _pending(self) -> list[_Waiter]:
        return [w for w in self._waiters if not w.future.done()]

    async def _worker(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            pending = self._pending()
            if not pending or self._closed:
                continue
            # Delay *only background requests*. Urgent requests interrupt grace.
            if all(w.priority == Priority.BACKGROUND for w in pending) and self.background_grace_sec:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.background_grace_sec)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                pending = self._pending()
                if not pending:
                    continue
            claimed: list[_Waiter] = []
            self._inflight = True
            self._inflight_since_ns = time.monotonic_ns()
            try:
                # Quota acquisition is account-wide responsibility. During
                # mixed fleet this MUST be original shared FIFO, not new quota.
                quota_begin = time.monotonic_ns()
                await asyncio.wait_for(self.quota.take(self.highest_waiting_priority), self.quota_timeout_sec)
                self.counters['quota_granted'] += 1
                started = time.monotonic_ns()
                quota_wait_ms = round((started - quota_begin) / 1_000_000, 3)
                claimed = [w for w in self._pending()
                           if w.created_ns <= started and w.not_before_ns <= started]
                if not claimed:
                    # A race after the last pre-grant liveness check can
                    # consume one permit. Record it; never send an unused GET.
                    self.counters['unused_quota_grants'] += 1
                    continue
                highest_priority = min(w.priority for w in claimed)
                self.counters['provider_fetch_calls'] += 1
                response = await asyncio.wait_for(
                    self.provider.fetch(self.instruments), self.get_timeout_sec)
                completed = time.monotonic_ns()
                snapshot = build_snapshot(response=response, instruments=self.instruments,
                                          started_ns=started, completed_ns=completed)
                # Diagnostics only, never influence safety or request coalescing.
                snapshot = Snapshot(snapshot.snapshot_id, snapshot.started_ns,
                                    snapshot.completed_ns, snapshot.instruments,
                                    snapshot.by_instrument, snapshot.instrument_errors,
                                    quota_wait_ms=quota_wait_ms,
                                    fetch_ms=round((completed-started)/1_000_000, 3),
                                    batch_size=len(claimed), absence_proven=snapshot.absence_proven)
                for waiter in claimed:
                    if waiter.future.done():
                        continue
                    try:
                        value = snapshot.slice(waiter.instrument,
                            not_before_ns=waiter.not_before_ns,
                            trace_id=waiter.trace_id,
                            request_received_ns=waiter.created_ns)
                    except SnapshotError as exc:
                        # A bad ZEC row must not blind unrelated BTC/NEAR/WLD.
                        waiter.future.set_exception(exc)
                    else:
                        waiter.future.set_result(value)
                self.counters['get_batches'] += 1
                self.counters['coalesced_clients'] += len(claimed)
                self._metric_seq += 1
                self.metrics.append({
                    'seq':self._metric_seq,
                    'batch_size': len(claimed),
                    'get_started_mono_ns': started,
                    'get_completed_mono_ns': completed,
                    'snapshot_id': snapshot.snapshot_id,
                    'highest_priority': int(highest_priority),
                    'quota_wait_ms': snapshot.quota_wait_ms,
                    'fetch_ms': snapshot.fetch_ms,
                    'background_only': highest_priority == Priority.BACKGROUND,
                })
            except Exception as exc:
                if isinstance(exc, QuotaRequestAbandoned):
                    self.counters['quota_abandoned'] += 1
                    continue
                if isinstance(exc, asyncio.TimeoutError):
                    self.counters['quota_failed' if not claimed else 'fetch_failed'] += 1
                else:
                    self.counters['fetch_failed'] += 1
                # Fail only waiters already present at failure boundary.
                failures = claimed if claimed else list(self._pending())
                for w in failures:
                    if not w.future.done():
                        w.future.set_exception(ReaderError(f'Reader operation failed: {type(exc).__name__}'))
            finally:
                self._inflight = False
                self._inflight_since_ns = 0
                self._waiters = [w for w in self._waiters if not w.future.done()]
                if self._waiters and not self._closed:
                    self._wake.set()
