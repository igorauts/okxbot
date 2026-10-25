"""Read-only shared quota/FIFO snapshots under the writers' flock inode.

No creation, truncation, replacement, or recovery. Unknown, missing after
initialization, invalid or locked data fail closed. A missing never-created
file may be allowed by the caller before any trading process uses it.
"""
import fcntl
import json
import os
import stat
import time
from pathlib import Path


class GateSnapshotUnavailable(RuntimeError):
    pass


def read_gate_snapshot(path, field, *, allow_missing=False, max_wait=3.5):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing and not path.is_symlink():
            return None
        raise GateSnapshotUnavailable('Missing shared gate file: ' + path.name) from None
    except OSError as exc:
        raise GateSnapshotUnavailable('Cannot open shared gate file: ' + path.name) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise GateSnapshotUnavailable('Non-regular shared gate file: ' + path.name)
        deadline = time.monotonic() + max_wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GateSnapshotUnavailable('Shared gate snapshot lock timeout: ' + path.name)
                time.sleep(0.01)
        # Both the reads and the validation happen under the SAME inode lock.
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024:
            raise GateSnapshotUnavailable('Empty/oversized shared gate snapshot: ' + path.name)
        try:
            obj = json.loads(raw.decode('ascii'))
        except (ValueError, UnicodeError) as exc:
            raise GateSnapshotUnavailable('Invalid shared gate JSON: ' + path.name) from exc
        if not isinstance(obj, dict) or obj.get('schema') != 1 or not isinstance(obj.get(field), list):
            raise GateSnapshotUnavailable('Unknown shared gate schema/fields: ' + path.name)
        return obj
    finally:
        os.close(fd)
