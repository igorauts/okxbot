"""BUY 0/N journal follower; monitor-only, bounded and self-recovering.

The durable stream cursor is an optimization, not an authority on current BUY
coverage. Any continuity gap is explicitly reported and monitoring resumes at
new journal events; stale incidents are not invented or silently repaired.
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
_STALE = 45.0
_STATE = {'running': False, 'last_error': '', 'restarts': 0, 'overflow': 0,
          'line_count': 0, 'parse_errors': 0, 'last_line_at': 0.0,
          'child_pid': 0, 'started_at': 0.0, 'gap_id': '', 'gap_reason': '',
          'gap_count': 0, 'gap_open': False, 'recovering': False, 'backoff_seconds': 0.0}
_WANT = ()
_THREAD = None
_SEED_CURSOR = None
_ACKED_CURSOR = None
_ACKED_AT = 0.0
_FORCE_TAIL = False
_INITIALIZED = False
_RESET_EPOCH = 0
_STOP = threading.Event()


def _gap(reason):
    """With lock held: bounded, explicit loss of continuity, never a permanent latch."""
    global _SEED_CURSOR, _ACKED_CURSOR, _ACKED_AT, _FORCE_TAIL, _RESET_EPOCH
    _ROWS.clear()
    _SEED_CURSOR = None
    _ACKED_CURSOR = None
    _ACKED_AT = 0.0
    _FORCE_TAIL = True
    _RESET_EPOCH += 1
    if not _STATE['gap_open']:
        _STATE['gap_id'] = str(time.time_ns())
        _STATE['gap_reason'] = reason
    _STATE['gap_count'] += 1
    _STATE['gap_open'] = True
    _STATE['recovering'] = True
    _STATE['last_error'] = reason


def _loop():
    global _SEED_CURSOR, _FORCE_TAIL
    failures = 0
    while not _STOP.is_set():
        with _LOCK:
            names = _WANT
            seed = None if _FORCE_TAIL else (_ACKED_CURSOR or _SEED_CURSOR)
            _SEED_CURSOR = None
            _FORCE_TAIL = False
            if seed and _ACKED_AT and time.time() - _ACKED_AT > _STALE:
                _gap('stale cursor on reconnect')
                seed = None
            reset_epoch = _RESET_EPOCH
        if not names:
            _STOP.wait(.25)
            continue
        cmd = ['journalctl', '--follow', '--no-pager', '-o', 'json']
        if isinstance(seed, str) and 0 < len(seed) <= 1024 and '\n' not in seed:
            cmd += ['--after-cursor', seed]
        else:
            cmd += ['-n', '0']
        for name in names:
            cmd += ['-u', 'okx-grid-' + name + '.service']
        p = None
        began = time.monotonic()
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
            with _LOCK:
                _STATE.update(running=True, child_pid=p.pid, started_at=time.time(),
                              last_error='', recovering=False)
            fd = p.stdout.fileno()
            os.set_blocking(fd, False)
            pending = bytearray()
            while not _STOP.is_set():
                with _LOCK:
                    if _WANT != names or _RESET_EPOCH != reset_epoch:
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
                        _gap('oversize journal stream chunk')
                    break
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending = bytearray(rest)
                    if not line or len(line) > 131072:
                        with _LOCK:
                            _STATE['parse_errors'] += 1
                            _gap('invalid or oversized journal row')
                        raise ValueError('invalid journal line')
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('nonobject row')
                        cursor = row.get('__CURSOR')
                        if (not isinstance(cursor, str) or not 0 < len(cursor) <= 1024
                                or any(c in cursor for c in ('\r','\n','\x00'))):
                            raise ValueError('missing cursor')
                    except (ValueError, UnicodeError, TypeError):
                        with _LOCK:
                            _STATE['parse_errors'] += 1
                            _gap('invalid JSON or cursor')
                        raise ValueError('unparseable journal row')
                    with _LOCK:
                        if _RESET_EPOCH != reset_epoch:
                            raise OverflowError('resync requested')
                        if len(_ROWS) >= _MAX:
                            _STATE['overflow'] += 1
                            _gap('queue overflow')
                            raise OverflowError('queue full')
                        _ROWS.append(row)
                        _STATE['line_count'] += 1
                        _STATE['last_line_at'] = time.time()
        except (OSError, ValueError, OverflowError, select.error) as exc:
            with _LOCK:
                if not _STATE['last_error']:
                    _STATE['last_error'] = type(exc).__name__
        finally:
            if p is not None:
                try:
                    if p.poll() is None:
                        p.terminate()
                    p.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try: p.kill()
                    except OSError: pass
            with _LOCK:
                _STATE['running'] = False
                _STATE['child_pid'] = 0
                _STATE['restarts'] += 1
                if not _STATE['last_error']:
                    _STATE['last_error'] = 'journalctl exited'
        # Process absent while we wait: no false healthy state; bounded restart
        # rate, including failure to execute journalctl on a fresh installation.
        failures = 0 if time.monotonic() - began >= 60 else min(failures+1, 6)
        pause = min(60., 3. * (2 ** max(0,failures-1)))
        with _LOCK: _STATE['backoff_seconds'] = pause
        _STOP.wait(pause)


def drain(st, units):
    """Nonblocking. Returns rows, or None when the stream isn't currently trustworthy."""
    global _THREAD, _WANT, _SEED_CURSOR, _ACKED_AT, _INITIALIZED
    names = tuple(sorted(str(x) for x in units))
    now = time.time()
    with _LOCK:
        if not _INITIALIZED:
            _INITIALIZED = True
            cursor = st.get('buy_zero_stream_cursor_v4')
            at = st.get('buy_zero_stream_cursor_at_v4')
            if isinstance(cursor, str) and cursor:
                if not isinstance(at,(int,float)) or not 0 <= now-at <= _STALE:
                    _gap('stale saved cursor; prior observation gap')
                else:
                    _SEED_CURSOR = cursor
                    _ACKED_AT = at
        if names != _WANT:
            _WANT = names
            if _ACKED_CURSOR:
                _SEED_CURSOR = _ACKED_CURSOR
            # Follower will restart when the unit set changes, retaining any
            # already-delivered rows and their ACK boundary.
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_loop, name='okx-telegram-journal-stream', daemon=True)
            _THREAD.start()
        items = list(_ROWS)
        silence = (_STATE['running'] and now-max(_STATE['last_line_at'],_STATE['started_at']) > _STALE)
        unhealthy = (_STATE['recovering'] or silence or not _STATE['running'])
        metrics = dict(_STATE)
        metrics['queue'] = len(_ROWS)
    st['buy_zero_stream_health_v4'] = {
        'running': metrics['running'], 'last_error':metrics['last_error'],
        'restarts':metrics['restarts'], 'overflow':metrics['overflow'],
        'gap_count':metrics['gap_count'], 'observed_at':now}
    gap = metrics['gap_id']
    if gap and st.get('buy_zero_stream_gap_notified_v4') != gap:
        # Persisted by existing one()/outbox before cursor is advanced.
        from okx_telegram_events import one
        one(None, st, 'buy_zero_stream_gap/' + gap,
            '⚠️ BUY 0/N: пробел в наблюдении журнала. Старые события не '
            'воспроизводятся; контроль свежих событий возобновляется.')
        st['buy_zero_stream_gap_notified_v4'] = gap
        st.pop('buy_zero_stream_cursor_v4', None)
        st.pop('buy_zero_stream_cursor_at_v4', None)
    if unhealthy:
        return None
    return items


def ack(cursor):
    """Return True only if cursor was present, and remove rows through it."""
    global _ACKED_CURSOR, _ACKED_AT
    with _LOCK:
        if not any(row.get('__CURSOR') == cursor for row in _ROWS):
            _gap('ack cursor absent from queue')
            return False
        while _ROWS:
            row = _ROWS.popleft()
            if row.get('__CURSOR') == cursor:
                break
        _ACKED_CURSOR = cursor
        _ACKED_AT = time.time()
        _STATE['gap_open'] = False
        return True


def snapshot():
    with _LOCK:
        data = dict(_STATE)
        data['queue'] = len(_ROWS)
        return data
