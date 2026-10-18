"""v0.7.11 non-blocking, local-only latency telemetry.

No network, no order management and no secrets. Drop metrics on overload;
trading must never wait for filesystem I/O from this component.
"""
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class LatencyJournal:
    MAX_BYTES = 256 * 1024 * 1024

    def __init__(self, path, enabled=True, max_queue=4096):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.queue = queue.Queue(maxsize=max_queue)
        self._lock = threading.Lock()
        self._thread = None
        self.dropped = 0
        self.write_errors = 0

    def record(self, phase, elapsed_ms, **fields):
        if not self.enabled:
            return
        try:
            # Never collect HTTP bodies, URL query strings, IDs, credentials or headers.
            if not isinstance(phase, str) or phase not in (
                'http', 'rest_gate', 'positions_gate', 'position_reconcile',
                'positions_gate_lock_wait', 'positions_gate_quota_wait',
                'positions_gate_fifo_wait',
                'buy_post', 'buy_batch', 'ws_drain', 'loop_pass',
                'fills_history_shared_wait', 'pnl_submit',
                'drawdown_submit'):
                return
            permitted = {'route', 'method', 'passes', 'mismatches', 'outcome',
                         'count', 'placed', 'attempted', 'status', 'partial',
                         'resolved', 'active_buys', 'target_buys', 'dropped'}
            safe = {k: v for k,v in fields.items()
                    if k in permitted and (v is None or type(v) in (str, int, float, bool))}
            if 'route' in safe:
                route = safe['route']
                if not route.startswith('/api/v5/') or '?' in route or len(route) > 100:
                    safe.pop('route')
            rec = {'ts': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                   'phase': phase, 'ms': round(max(0.0, float(elapsed_ms)), 3), **safe}
            self.queue.put_nowait(rec)
            if self._thread is None:
                with self._lock:
                    if self._thread is None:
                        self._thread = threading.Thread(target=self._write_loop,
                                                        name='okx-latency-v0711', daemon=True)
                        self._thread.start()
        except queue.Full:
            self.dropped += 1
        except (ValueError, TypeError, OSError, RuntimeError):
            # Read-only telemetry must never propagate an error into trading.
            self.dropped += 1

    def _write_loop(self):
        while True:
            rec = self.queue.get()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.exists() and self.path.stat().st_size >= self.MAX_BYTES:
                    old = self.path.with_name(self.path.name + '.1')
                    os.replace(self.path, old)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (json.dumps(rec, separators=(',', ':')) + '\n').encode('utf-8'))
                finally:
                    os.close(fd)
            except (OSError, ValueError, TypeError):
                self.write_errors += 1
            finally:
                self.queue.task_done()
