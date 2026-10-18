"""EXPERIMENTAL OFFLINE v0.7.12 positions gate; NOT LIVE-VALIDATED.

Cross-process FIFO for cooperative 0.7.12 engines. Keeps legacy v0.7.11
schema=1 calls file unchanged so 0.7.11 processes keep respecting the same
rolling 8 / 2.10 s quota during a sequential rollout. 0.7.11 participants
cannot join FIFO: strict fairness is only guaranteed after all migrate.

No local fallback. Any storage problem / expired ticket -> GateUnavailable;
caller must turn it into ExchangeTemporary BEFORE any trade-related request.
"""
import errno
import fcntl
import json
import math
import os
import secrets
import time
from pathlib import Path


class GateUnavailable(RuntimeError):
    pass


class AccountPositionsFIFO:
    WINDOW = 2.10
    MAX_SHARED = 8
    MAX_QUEUE_WAIT = 15.0
    LOCK_TIMEOUT = 2.0
    LEASE_SEC = 20.0

    def __init__(self, shared_path, reporter=None):
        self.path = Path(shared_path)
        if not self.path.is_absolute():
            raise ValueError('Shared positions quota file must be absolute')
        self.queue_path = self.path.with_name(self.path.name + '.fifo-v0712.json')
        self.reporter = reporter

    @staticmethod
    def _open(path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)

    def _lock(self, fd, until):
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > until:
                    raise GateUnavailable('shared gate lock unavailable')
                time.sleep(0.015)

    @staticmethod
    def _read(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 1024 * 1024)
        if not raw:
            return None
        try:
            obj = json.loads(raw.decode('ascii'))
        except (UnicodeError, ValueError) as exc:
            raise GateUnavailable('invalid persisted gate JSON; no fallback') from exc
        if not isinstance(obj, dict):
            raise GateUnavailable('gate data must be an object')
        return obj

    @staticmethod
    def _write(fd, data):
        packed = json.dumps(data, separators=(',', ':'), allow_nan=False).encode('ascii')
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        if os.write(fd, packed) != len(packed):
            raise GateUnavailable('short shared-gate write')
        os.fsync(fd)

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _queue(self, obj, now):
        if obj is None:
            return []
        if obj.get('schema') != 1 or not isinstance(obj.get('waiters'), list):
            raise GateUnavailable('invalid FIFO queue schema')
        out = []
        ids = set()
        for row in obj['waiters']:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not isinstance(row.get('pid'), int)
                or not isinstance(row.get('seen'), (int, float))
                or not isinstance(row.get('created'), (int, float))):
                raise GateUnavailable('invalid FIFO queue entry')
            if row['id'] in ids:
                raise GateUnavailable('duplicate FIFO ticket')
            ids.add(row['id'])
            if row['seen'] > now + 1 or row['created'] > now + 1:
                raise GateUnavailable('FIFO clock moved backwards; cannot establish fairness')
            if now - row['seen'] > self.LEASE_SEC or not self._pid_alive(row['pid']):
                continue
            out.append(row)
        return out

    def _reserve_if_available(self, now):
        fd = self._open(self.path)
        try:
            self._lock(fd, time.monotonic() + self.LOCK_TIMEOUT)
            # Read wall-clock time AFTER acquiring the quota lock.
            # Never evaluate reservations using a stale pre-lock timestamp.
            now = time.time()
            obj = self._read(fd)
            if obj is None:
                calls = []
            elif (obj.get('schema') == 1 and isinstance(obj.get('calls'), list)):
                calls = obj['calls']
                if not all(isinstance(x, (int, float)) and math.isfinite(x) and x <= now + 0.5
                           and x >= 0 for x in calls):
                    raise GateUnavailable('corrupt/future quota timestamps; refusing reset')
            else:
                raise GateUnavailable('quota schema mismatch; refusing reset')
            calls = [x for x in calls if now - self.WINDOW < x <= now + 0.5]
            if len(calls) < self.MAX_SHARED:
                calls.append(now)
                self._write(fd, {'schema': 1, 'calls': calls})
                return True, 0.0
            return False, max(0.02, min(calls) + self.WINDOW - now)
        finally:
            os.close(fd)

    def _visit(self, ticket, born, *, cancel=False):
        fd = self._open(self.queue_path)
        try:
            self._lock(fd, time.monotonic() + self.LOCK_TIMEOUT)
            now = time.time()
            loaded=self._read(fd)
            queue = self._queue(loaded, now)
            dirty = loaded is None or len(queue) != len(loaded.get('waiters', []))
            found = next((w for w in queue if w['id'] == ticket), None)
            if cancel:
                queue = [w for w in queue if w['id'] != ticket]
                self._write(fd, {'schema': 1, 'waiters': queue})
                return False, 0.0
            if found is None:
                # Only register once. If already present but pruned (e.g. long
                # scheduler suspension), fail closed rather than cut the line.
                if born is None:
                    entry = {'id': ticket, 'pid': os.getpid(), 'created': now, 'seen': now}
                    queue.append(entry)
                    dirty = True
                    born = now
                else:
                    raise GateUnavailable('FIFO lease expired while waiting')
            elif now - found['seen'] >= 2.0:
                found['seen'] = now
                dirty = True
            delay = 0.05
            granted = False
            if queue and queue[0]['id'] == ticket:
                granted, delay = self._reserve_if_available(time.time())
                if granted:
                    queue.pop(0)
                    dirty = True
            if dirty:
                self._write(fd, {'schema': 1, 'waiters': queue})
            return granted, max(0.02, min(0.25, delay))
        finally:
            os.close(fd)

    def take(self):
        ticket = secrets.token_hex(16)
        start = time.monotonic()
        born = None
        granted = False
        try:
            while time.monotonic() - start <= self.MAX_QUEUE_WAIT:
                granted, delay = self._visit(ticket, born)
                if granted:
                    return
                born = True
                time.sleep(delay)
            raise GateUnavailable('FIFO queue wait deadline; no independent fallback')
        except (OSError, TimeoutError, ValueError) as exc:
            raise GateUnavailable(f'positions FIFO storage failure: {type(exc).__name__}') from exc
        finally:
            if not granted:
                try:
                    self._visit(ticket, born, cancel=True)
                except Exception:
                    # Failure to remove a ticket is safe: lease/PID pruning.
                    pass
            if self.reporter:
                try:
                    self.reporter('positions_gate_fifo_wait',
                                  (time.monotonic() - start) * 1000,
                                  outcome='ok' if granted else 'unavailable')
                except Exception:
                    pass


class FillsHistoryFIFO(AccountPositionsFIFO):
    """Separate account-wide budget: 9 requests per 2.1 seconds (headroom)."""
    MAX_SHARED = 9
    WINDOW = 2.10
