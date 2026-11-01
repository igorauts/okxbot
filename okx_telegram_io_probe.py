"""Best-effort, monitor-only subprocess I/O diagnostics; no command changes.

In single-threaded monitor, /proc/self/io includes *waited-for child* I/O.
Deltas are estimates attributed to completed subprocess.run calls; they are
not a guarantee of exclusive per-child accounting if processes overlap.
No subprocess arguments, output, tokens, command texts or bot data are stored.
"""
import collections
import datetime
import json
import os
from pathlib import Path
import subprocess
import time

_OUT = Path('/srv/okx-telegram-io-telemetry')
_PREV_RUN = subprocess.run
_INSTALLED = False
_MINUTE = None
_GROUPS = {}
_TOTAL_BASE = None
_TOTAL_READ = 0
_TOTAL_WRITE = 0
_ERROR_COUNT = 0
_BINS_MS = (25, 100, 500, 2000, 8000)

def _counter():
    values = {}
    try:
        with open('/proc/self/io', 'rt', encoding='ascii') as f:
            for line in f:
                k, sep, v = line.partition(':')
                if sep:
                    values[k.strip()] = int(v.strip())
    except (OSError, ValueError):
        return None
    return values

def _kind(command):
    if not isinstance(command, (list, tuple)) or not command:
        return None
    try:
        name = os.path.basename(os.fspath(command[0]))
        parts = [os.fspath(x) for x in command[1:]]
    except (OSError, TypeError, ValueError):
        return None
    if name == 'journalctl':
        if '-g' in parts or '--grep' in parts or any(x.startswith('--grep=') for x in parts):
            return 'journal_grep'
        if '--after-cursor' in parts or any(x.startswith('--after-cursor=') for x in parts):
            return 'journal_cursor'
        return 'journal_other'
    if name == 'systemctl':
        if 'show' in parts:
            return 'systemctl_show'
        if 'is-active' in parts:
            return 'systemctl_isactive'
        return 'systemctl_other'
    return None

def _bucket(label):
    return _GROUPS.setdefault(label, {
        'n': 0, 'wall_ms_sum': 0, 'wall_ms_max': 0,
        'read_mib_sum_bytes': 0, 'read_bytes_max': 0,
        'logical_read_bytes_sum': 0, 'write_bytes_sum': 0,
        'rc_nonzero': 0, 'timeouts': 0, 'exceptions': 0,
        'stdout_lines_sum': 0, 'stdout_length_sum': 0,
        'wall_bins_ms': [0] * (len(_BINS_MS)+1),
    })

def _utc_minute():
    return int(time.time() // 60)

def _persist(minute, groups, process_read, process_write, issues):
    # Best effort: diagnostics must NEVER interrupt Telegram incident delivery.
    try:
        if _OUT.is_symlink() or not _OUT.is_dir():
            return
        date = datetime.datetime.fromtimestamp(minute * 60, datetime.timezone.utc).strftime('%Y%m%d')
        target = _OUT / ('monitor_io_' + date + '.jsonl')
        if target.is_symlink():
            return
        item = {
            'schema': 1,
            'utc_minute': datetime.datetime.fromtimestamp(minute*60, datetime.timezone.utc).isoformat(),
            'minutes': 1, 'process_read_bytes_delta': process_read,
            'process_write_bytes_delta': process_write,
            'subprocess_io_accounting': 'proc_self_io_includes_waited_children',
            'probe_errors': issues, 'groups': groups,
        }
        line = (json.dumps(item, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(line) > 16384:
            return
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(target), flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            pos = 0
            while pos < len(line):
                n = os.write(fd, line[pos:])
                if n <= 0:
                    raise OSError('short telemetry write')
                pos += n
        finally:
            os.close(fd)
        # Only delete owned, named telemetry files older than fourteen days.
        cutoff = (datetime.datetime.fromtimestamp(minute*60, datetime.timezone.utc) -
                  datetime.timedelta(days=14)).strftime('%Y%m%d')
        for old in _OUT.glob('monitor_io_????????.jsonl'):
            part = old.name[len('monitor_io_'):-len('.jsonl')]
            if part.isdigit() and part < cutoff and old.is_file() and not old.is_symlink():
                try:
                    old.unlink()
                except OSError:
                    pass
    except Exception:
        return

def _advance_if_needed():
    global _MINUTE, _GROUPS, _TOTAL_BASE, _TOTAL_READ, _TOTAL_WRITE, _ERROR_COUNT
    now = _utc_minute()
    if _MINUTE is None:
        _MINUTE = now
        _TOTAL_BASE = _counter()
        return
    if now == _MINUTE:
        return
    c = _counter()
    if c is not None and _TOTAL_BASE is not None:
        _TOTAL_READ = max(0, c.get('read_bytes', 0)-_TOTAL_BASE.get('read_bytes', 0))
        _TOTAL_WRITE = max(0, c.get('write_bytes', 0)-_TOTAL_BASE.get('write_bytes', 0))
    # If the clock jumps, do not fabricate missing records.
    _persist(_MINUTE, _GROUPS, _TOTAL_READ, _TOTAL_WRITE, _ERROR_COUNT)
    _MINUTE = now
    _GROUPS = {}
    _TOTAL_READ = 0
    _TOTAL_WRITE = 0
    _ERROR_COUNT = 0
    _TOTAL_BASE = c

def _instrumented_run(*args, **kwargs):
    global _ERROR_COUNT
    if not args or kwargs.get('shell'):
        return _PREV_RUN(*args, **kwargs)
    kind = _kind(args[0])
    if kind is None:
        return _PREV_RUN(*args, **kwargs)
    try:
        _advance_if_needed()
    except Exception:
        pass
    before = _counter()
    start = time.monotonic()
    failure = None
    status = 0
    output = None
    try:
        completed = _PREV_RUN(*args, **kwargs)
        status = completed.returncode
        output = completed.stdout
        return completed
    except subprocess.TimeoutExpired:
        failure = 'timeout'
        raise
    except BaseException:
        failure = 'exception'
        raise
    finally:
        try:
            elapsed_ms = max(0, int((time.monotonic()-start)*1000))
            after = _counter()
            rd = wr = logical = 0
            if before is not None and after is not None:
                rd = max(0, after.get('read_bytes', 0) - before.get('read_bytes', 0))
                wr = max(0, after.get('write_bytes', 0) - before.get('write_bytes', 0))
                logical = max(0, after.get('rchar', 0) - before.get('rchar', 0))
            else:
                _ERROR_COUNT += 1
            b = _bucket(kind)
            b['n'] += 1
            b['wall_ms_sum'] += elapsed_ms
            b['wall_ms_max'] = max(b['wall_ms_max'], elapsed_ms)
            b['read_mib_sum_bytes'] += rd
            b['read_bytes_max'] = max(b['read_bytes_max'], rd)
            b['logical_read_bytes_sum'] += logical
            b['write_bytes_sum'] += wr
            b['rc_nonzero'] += int(status != 0)
            b['timeouts'] += int(failure == 'timeout')
            b['exceptions'] += int(failure == 'exception')
            if isinstance(output, (str, bytes)):
                b['stdout_length_sum'] += len(output)
                b['stdout_lines_sum'] += output.count('\n' if isinstance(output,str) else b'\n')
            ix = next((i for i, bound in enumerate(_BINS_MS) if elapsed_ms <= bound), len(_BINS_MS))
            b['wall_bins_ms'][ix] += 1
        except Exception:
            # Preserve original subprocess result/exception semantics.
            pass

def install():
    global _INSTALLED
    if _INSTALLED:
        return
    if _OUT.is_symlink() or not _OUT.is_dir():
        raise RuntimeError('Monitor I/O probe output directory unavailable')
    _advance_if_needed()
    subprocess.run = _instrumented_run
    _INSTALLED = True

def tick():
    try:
        _advance_if_needed()
    except Exception:
        pass
