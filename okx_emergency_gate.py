"""Isolated, cross-process, emergency-only GET /account/positions quota.

This is NOT a replacement for the normal FIFO. It may only be invoked from
HARD/PANIC after a genuine shared-gate STORAGE failure. A full initial rolling
window is drained before first reservation. At most one emergency position
request may be issued per WINDOW for the ENTIRE VPS. If this independent lock
is inaccessible, fail closed; NEVER fall back to unlimited REST requests.

The emergency quota is on /run (tmpfs) so a broken /root/okx filesystem does
not automatically make it unavailable. The trading engine must still persist
all PANIC intents before ANY POST: an inaccessible state file blocks trading.
"""
import fcntl
import json
import os
import stat
import time
from pathlib import Path


class EmergencyGateUnavailable(RuntimeError):
    pass


def eligible_emergency_failure(exc):
    """NOT for ordinary congestion, stale FIFO tickets, or unknown schemas."""
    if isinstance(exc, OSError):
        return True
    text = str(exc).lower()
    # Never reinterpret wait/fairness issues or newer data formats as a
    # storage emergency. Keep the original failure instead.
    disallowed = ('queue wait deadline', 'lock unavailable', 'lease expired',
                  'schema mismatch', 'unknown quota schema',
                  'invalid fifo queue schema')
    if any(key in text for key in disallowed):
        return False
    eligible = ('invalid persisted gate json', 'gate data must be an object',
                'short shared-gate write', 'positions fifo storage failure:',
                'invalid/future call timestamps',
                'corrupt/future quota timestamps', 'non-finite fifo ticket time')
    return any(key in text for key in eligible)


class EmergencyPositionsGate:
    WINDOW = 2.10
    MAX_WAIT = 14.0
    LOCK_TIMEOUT = 2.0
    DEFAULT_PATH = Path('/run/lock/okxbot/account_positions_emergency_gate.json')

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else self.DEFAULT_PATH
        if not self.path.is_absolute():
            raise ValueError('Emergency shared gate path must be absolute')
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not self.boot_id:
            raise EmergencyGateUnavailable('Cannot establish Linux boot_id')

    def _open(self):
        parent = self.path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        d = parent.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(d.st_mode) or d.st_uid != os.geteuid()
                or (stat.S_IMODE(d.st_mode) & 0o077)):
            raise EmergencyGateUnavailable('Emergency gate directory unsafe')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            s = os.fstat(fd)
            current = self.path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                    or s.st_uid != os.geteuid() or (stat.S_IMODE(s.st_mode) & 0o077)
                    or (s.st_dev, s.st_ino) != (current.st_dev, current.st_ino)):
                raise EmergencyGateUnavailable('Emergency gate inode/permissions unsafe')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read(self, fd):
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        if not raw:
            return None
        try:
            obj = json.loads(raw.decode('ascii'))
        except (UnicodeError, ValueError):
            return None  # truncated state: conservatively reinitialize cooldown
        if not isinstance(obj, dict):
            return None
        if obj.get('schema') != 1:
            raise EmergencyGateUnavailable('Unknown emergency quota schema')
        if (not isinstance(obj.get('boot_id'), str)
                or type(obj.get('next_ns')) is not int):
            return None
        return obj

    @staticmethod
    def _write(fd, obj):
        raw = json.dumps(obj, separators=(',', ':'), allow_nan=False).encode('ascii')
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        if os.write(fd, raw) != len(raw):
            raise EmergencyGateUnavailable('Short emergency quota write')
        os.fsync(fd)

    def _locked_visit(self, deadline):
        fd = self._open()
        try:
            lock_deadline = min(deadline, time.monotonic() + self.LOCK_TIMEOUT)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise EmergencyGateUnavailable('Emergency quota lock timed out')
                    # Deadline enforced by take(); no busy-spin holding lock.
                    time.sleep(0.015)
            now = time.monotonic_ns()
            interval = int(self.WINDOW * 1_000_000_000)
            obj = self._read(fd)
            if (obj is None or obj['boot_id'] != self.boot_id
                    or obj['next_ns'] < 0 or obj['next_ns'] > now + interval):
                # Unknown recent reservations: FIRST WAIT A FULL WINDOW.
                self._write(fd, {'schema': 1, 'boot_id': self.boot_id,
                                 'next_ns': now + interval})
                return False, self.WINDOW
            delay = (obj['next_ns'] - now) / 1_000_000_000
            if delay > 0:
                return False, delay
            # One permit, persisted before HTTP, even if process dies.
            self._write(fd, {'schema': 1, 'boot_id': self.boot_id,
                             'next_ns': now + interval})
            return True, 0.0
        finally:
            os.close(fd)

    def take(self):
        deadline = time.monotonic() + self.MAX_WAIT
        while time.monotonic() < deadline:
            try:
                granted, delay = self._locked_visit(deadline)
            except (OSError, EmergencyGateUnavailable) as exc:
                raise EmergencyGateUnavailable('Emergency quota unavailable: ' + type(exc).__name__) from exc
            if granted:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, max(0.005, delay), remaining))
        raise EmergencyGateUnavailable('Emergency quota wait deadline exceeded')
