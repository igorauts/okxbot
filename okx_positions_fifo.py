"""Shared positions/fills FIFO for a single OKX account.

All bot instances share the same lock inode and conservative quota window.

No local fallback. Recover only corrupt/schema-1 JSON and wall-clock drift
under the original flock inode, conservatively refilling the quota window.
Real I/O failures and lock failure remain GateUnavailable; engine must provide
a separately bounded/verified emergency HARD/PANIC GET path before LIVE rollout.
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
    def _metric(self, phase, elapsed_ms, **fields):
        # Telemetry must never alter quota/FIFO decisions or propagate errors.
        try:
            if self.reporter:
                self.reporter(phase, elapsed_ms, **fields)
        except Exception:
            pass

    def _recovery_notice(self, component, reason):
        # Diagnostics only. A reporter failure must never affect rate safety.
        try:
            if self.reporter:
                self.reporter('positions_gate_recovery', 0.0,
                              reason='quota' if component=='quota' else 'fifo')
        except Exception:
            pass
    def _recover_quota(self, fd, now, reason):
        # Keep the SAME inode + flock. Never unlink/rename a live lock file.
        # Unknown recent reservations -> assume ALL quota consumed at recovery.
        # Thus nobody gets a new reservation until a FULL window has elapsed.
        self._write(fd, {'schema': 1, 'calls': [now] * self.MAX_SHARED})
        self._recovery_notice('quota', reason)
        return False, self.WINDOW

    @staticmethod
    def _open(path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)

    def _lock(self, fd, until):
        started=time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._metric('positions_gate_lock_wait',
                             (time.monotonic()-started)*1000, outcome='ok')
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
            if not math.isfinite(row['seen']) or not math.isfinite(row['created']):
                raise GateUnavailable('non-finite FIFO ticket time')
            if row['seen'] > now + 1 or row['created'] > now + 1:
                # Wall clock jumped BACKWARDS. Keep queue order and IDs.
                # Normalizing must be persisted by _visit (dirty comparison).
                row = dict(row)
                row['seen'] = min(row['seen'], now)
                row['created'] = min(row['created'], now)
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
            try:
                obj = self._read(fd)
            except GateUnavailable as exc:
                return self._recover_quota(fd, now, str(exc))
            if obj is None:
                # Could be a NEW quota file or a crash between truncate/write.
                # Treat both as uncertain rather than accidentally granting 8.
                return self._recover_quota(fd, now, 'empty quota data')
            if obj.get('schema') == 1 and isinstance(obj.get('calls'), list):
                calls = obj['calls']
                if not all(type(x) in (int, float) and math.isfinite(x)
                           and 0 <= x <= now + 0.5 for x in calls):
                    return self._recover_quota(fd, now, 'invalid/future call timestamps')
                if len(calls) > self.MAX_SHARED:
                    return self._recover_quota(fd, now, 'quota has too many reservations')
            else:
                # Schema other than 1 is NOT treated as crash corruption: it
                # might belong to a newer engine, and must fail closed.
                if obj.get('schema') != 1:
                    raise GateUnavailable('unknown quota schema; refusing reset')
                return self._recover_quota(fd, now, 'malformed schema-1 quota')
            calls = [x for x in calls if now - self.WINDOW < x <= now + 0.5]
            if len(calls) < self.MAX_SHARED:
                calls.append(now)
                self._write(fd, {'schema': 1, 'calls': calls})
                return True, 0.0
            delay=max(0.02, min(calls) + self.WINDOW - now)
            # Scheduled quota delay, not an actual measurement of sleep.
            self._metric('positions_gate_quota_wait', delay*1000,
                         outcome='advised_delay')
            return False, delay
        finally:
            os.close(fd)

    def _entry_extension(self):
        """Default legacy FIFO ticket has no priority fields."""
        return {}

    def _sort_queue(self, queue, now):
        """Original client preserves strict FIFO and wire-format semantics."""
        return queue

    def _visit(self, ticket, born, *, cancel=False):
        fd = self._open(self.queue_path)
        try:
            self._lock(fd, time.monotonic() + self.LOCK_TIMEOUT)
            now = time.time()
            loaded = None
            try:
                loaded = self._read(fd)
                queue = self._queue(loaded, now)
            except GateUnavailable as exc:
                # Unknown future schema is NOT crash damage and must never be
                # overwritten by an older engine. Only malformed schema-1/
                # invalid JSON may be conservatively reinitialized.
                if isinstance(loaded, dict) and loaded.get('schema') != 1:
                    raise
                # Only the FIFO tickets are lost, NEVER the rate quota.
                # Existing ticket holders will fail closed and retry through
                # their caller, rather than being silently granted out of turn.
                loaded = None
                queue = []
                self._write(fd, {'schema': 1, 'waiters': []})
                self._recovery_notice('fifo', str(exc))
            dirty = loaded is None or queue != loaded.get('waiters', [])
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
                    entry.update(self._entry_extension())
                    queue.append(entry)
                    dirty = True
                    born = now
                else:
                    raise GateUnavailable('FIFO lease expired while waiting')
            elif now - found['seen'] >= 2.0:
                found['seen'] = now
                dirty = True
            # Extension hook: default is a strict no-op for existing engines.
            reordered = self._sort_queue(queue, now)
            if reordered != queue:
                dirty = True
                queue = reordered
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
        except GateUnavailable as exc:
            msg=str(exc)
            category=('lock_timeout' if 'lock' in msg else
                      'quota_schema' if 'quota schema' in msg else
                      'fifo_lease' if 'lease' in msg else
                      'fifo_deadline' if 'deadline' in msg else 'gate_unavailable')
            self._metric('positions_gate_error', 0.0, reason=category,
                         outcome='unavailable')
            raise
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
