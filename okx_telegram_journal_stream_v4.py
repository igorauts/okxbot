"""Telegram-only, nonblocking, single persistent journal follower for BUY 0/N.

No trading/order/API calls; the long-running journalctl process is a follower,
not a repeated full-journal grep. Missing stream is UNKNOWN, never healthy.
"""
import collections
import json
import os
import select
import subprocess
import threading
import time

_LOCK = threading.Lock()
_ROWS = collections.deque()
_MAX = 4096
_STATE = {'running': False, 'last_error': '', 'restarts': 0, 'overflow': 0,
          'line_count': 0, 'parse_errors': 0, 'last_line_at': 0.0, 'child_pid': 0, 'started_at': 0.0}
_WANT = ()
_THREAD = None
_SEED_CURSOR = None
_ACKED_CURSOR = None
_STOP = threading.Event()


def _loop():
    global _SEED_CURSOR
    while not _STOP.is_set():
        with _LOCK:
            names = _WANT
            # On an abnormal follower exit, replay from the last ACKed cursor,
            # never from the current tail (which would lose interim events).
            seed = _ACKED_CURSOR or _SEED_CURSOR
            _SEED_CURSOR = None
        if not names:
            time.sleep(.25)
            continue
        # Start at current tail on first boot; on known recent cursor reconnect
        # from that cursor, never replay old (>45s) BUY alerts.
        cmd = ['journalctl', '-f', '--no-pager', '-o', 'json']
        if isinstance(seed, str) and 0 < len(seed) <= 1024 and '\n' not in seed:
            cmd += ['--after-cursor', seed]
        else:
            cmd += ['-n', '0']
        for name in names:
            cmd += ['-u', 'okx-grid-' + name + '.service']
        p = None
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
            with _LOCK:
                _STATE['running'] = True
                _STATE['child_pid'] = p.pid
                _STATE['started_at'] = time.time()
                _STATE['last_error'] = ''
            fd = p.stdout.fileno()
            os.set_blocking(fd, False)
            pending = bytearray()
            while not _STOP.is_set():
                with _LOCK:
                    if _WANT != names:
                        break
                if p.poll() is not None:
                    break
                ready, _, _ = select.select([fd], [], [], .5)
                if not ready:
                    continue
                try:
                    block = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not block:
                    break
                pending.extend(block)
                if len(pending) > 524288:
                    with _LOCK:
                        _STATE['overflow'] += 1
                        _STATE['last_error'] = 'oversize journal stream chunk'
                    break
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending = bytearray(rest)
                    if not line or len(line) > 131072:
                        with _LOCK:
                            _STATE['parse_errors'] += 1
                            _STATE['overflow'] += 1
                            _STATE['last_error'] = 'invalid or oversized journal row'
                        raise ValueError('invalid journal line')
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('nonobject row')
                        cursor = row.get('__CURSOR')
                        if not isinstance(cursor, str) or not 0 < len(cursor) <= 1024:
                            raise ValueError('missing cursor')
                        with _LOCK:
                            if len(_ROWS) >= _MAX:
                                # Never silently skip events; mark degraded,
                                # stop stream, restart at last ACKED cursor.
                                _STATE['overflow'] += 1
                                _STATE['last_error'] = 'queue overflow'
                                raise OverflowError('queue full')
                            _ROWS.append(row)
                            _STATE['line_count'] += 1
                            _STATE['last_line_at'] = time.time()
                    except OverflowError:
                        raise
                    except (ValueError, UnicodeDecodeError):
                        with _LOCK:
                            _STATE['parse_errors'] += 1
                            _STATE['overflow'] += 1
                            _STATE['last_error'] = 'invalid JSON or cursor'
                        raise
        except (OSError, ValueError, OverflowError, select.error) as exc:
            with _LOCK:
                _STATE['last_error'] = type(exc).__name__
        finally:
            if p is not None:
                try:
                    if p.poll() is None:
                        p.terminate()
                    p.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        p.kill()
                    except OSError:
                        pass
            with _LOCK:
                _STATE['running'] = False
                _STATE['child_pid'] = 0
                _STATE['restarts'] += 1
                if not _STATE['last_error']:
                    _STATE['last_error'] = 'journalctl exited'
        _STOP.wait(3.0)


def drain(st, units):
    """Return list of immutable raw JSON objects or None when stream degraded.

    Cursor in *existing* monitor state is committed only by zero_buy_scan after
    processing rows. Never read a historical tail on every poll.
    """
    global _THREAD, _WANT, _SEED_CURSOR
    names = tuple(sorted(str(x) for x in units))
    with _LOCK:
        if names != _WANT:
            _WANT = names
            # Keep unacknowledged rows. Reconnect from last processed cursor;
            # deleting the queue here silently loses pending BUY incidents.
            _SEED_CURSOR = _ACKED_CURSOR or st.get('buy_zero_stream_cursor_v4')
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_loop, name='okx-telegram-journal-stream', daemon=True)
            _THREAD.start()
        items = list(_ROWS)
        silence = (_STATE['running'] and time.time() - max(_STATE['last_line_at'], _STATE['started_at']) > 45)
        bad = _STATE['overflow'] or silence or not _STATE['running']
        metrics = dict(_STATE)
        metrics['queue'] = len(_ROWS)
    st['buy_zero_stream_health_v4'] = {
        'running': metrics['running'], 'last_error':metrics['last_error'],
        'restarts':metrics['restarts'], 'overflow':metrics['overflow'],
        'observed_at':time.time()}
    if bad:
        # No fabricated BUY 0/N on missing or dropped journal facts.
        return None
    return items


def ack(cursor):
    # ACK only rows actually processed; never silently lose a queue on mismatch.
    global _ACKED_CURSOR
    with _LOCK:
        if not any(x.get('__CURSOR') == cursor for x in _ROWS):
            _STATE['overflow'] += 1
            _STATE['last_error'] = 'ack cursor absent from queue'
            return
        while _ROWS:
            item = _ROWS.popleft()
            if item.get('__CURSOR') == cursor:
                break
        _ACKED_CURSOR = cursor


def snapshot():
    with _LOCK:
        data = dict(_STATE)
        data['queue'] = len(_ROWS)
        return data
