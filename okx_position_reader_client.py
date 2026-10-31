"""Opt-in read-only shared position snapshots for a single OKX NET account.

No external API, file I/O or exchange writes occur at import time. The calling
engine retains ALL existing order/position/coverage/intent reconciliation.
No TTL-based trading cache is supported: every call requires GET started at or
later than its individual freshness barrier.
"""
import json
import os
import socket
import stat
import time
from pathlib import Path
from okx_position_reader_core import Priority, SnapshotError, build_snapshot

MAX_IPC_BYTES = 65536

class PositionReaderUnavailable(RuntimeError):
    pass

class PositionReaderEmpty(PositionReaderUnavailable):
    pass

class PositionReaderPreempted(RuntimeError):
    """Local B3 abort: do not use stale data and do not direct-GET fallback."""
    pass


def read_positions(socket_path, instrument, *, account_uid, emergency=False, exit_priority=False, background=False, cancel_if=None, trace_id='', on_trace=None):
    """Returns only the target instrument rows or fails closed.

    Same-host CLOCK_MONOTONIC timestamps are meaningful across processes.
    The local gate is NOT taken here: Reader owns the shared original gate.
    """
    p=Path(socket_path)
    if not p.is_absolute() or not p.parent.is_dir() or p.is_symlink():
        raise PositionReaderUnavailable('Invalid IPC path')
    mode=p.parent.stat().st_mode
    if (not stat.S_ISDIR(mode) or stat.S_IMODE(mode)&0o077):
        raise PositionReaderUnavailable('IPC directory is not private')
    if not isinstance(account_uid,str) or not account_uid.strip() or len(account_uid)>64:
        raise PositionReaderUnavailable('Unverified account UID')
    if type(background) is not bool or (background and not callable(cancel_if)):
        raise PositionReaderUnavailable('Unverified background interruption guard')
    def check_preempt():
        if background:
            try:
                if cancel_if(): raise PositionReaderPreempted('Urgent event during B3 background wait')
            except PositionReaderPreempted:
                raise
            except Exception:
                raise PositionReaderPreempted('B3 interruption guard failed')
    check_preempt()
    min_start=time.monotonic_ns()
    request={'schema':1,'op':'positions','instrument':instrument,
             'priority':int(Priority.EMERGENCY if emergency else (
                 Priority.EXIT if exit_priority else (Priority.BACKGROUND if background else Priority.NORMAL))),
             'not_before_mono_ns':min_start,'trace_id':trace_id[:128],
             'account_uid':account_uid}
    # One absolute 5-second budget covering connect, send and the ENTIRE
    # response, even when a broken peer drip-feeds small chunks. Socket
    # per-operation timeout alone is not an end-to-end deadline.
    deadline=time.monotonic()+5.0
    def remaining():
        left=deadline-time.monotonic()
        if left<=0:
            raise PositionReaderUnavailable('Reader total IPC deadline expired')
        return left
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(remaining())
            s.connect(str(p))
            check_preempt()
            s.settimeout(remaining())
            s.sendall((json.dumps(request,separators=(',',':'))+'\n').encode())
            check_preempt()
            buf=bytearray()
            while b'\n' not in buf:
                check_preempt()
                s.settimeout(min(remaining(),0.05) if background else remaining())
                try:
                    chunk=s.recv(min(4096,MAX_IPC_BYTES+1-len(buf)))
                except socket.timeout:
                    if background:
                        check_preempt()
                        continue
                    raise
                if not chunk: raise PositionReaderUnavailable('Incomplete IPC response')
                buf.extend(chunk)
                if len(buf)>MAX_IPC_BYTES:raise PositionReaderUnavailable('Oversized IPC response')
        remaining()  # reject response completed only after the budget
        check_preempt()
        obj=json.loads(bytes(buf).split(b'\n',1)[0])
        if not isinstance(obj,dict) or obj.get('ok') is not True:
            raise PositionReaderUnavailable('Reader rejected request')
        response=obj.get('data')
        if (not isinstance(response,dict) or response.get('instrument')!=instrument
                or response.get('trace_id')!=trace_id[:128]
                or type(response.get('get_started_mono_ns')) is not int
                or response['get_started_mono_ns']<min_start
                or type(response.get('get_completed_mono_ns')) is not int
                or response['get_completed_mono_ns']<response['get_started_mono_ns']
                or response['get_completed_mono_ns']>time.monotonic_ns()
                or instrument not in response.get('covered_instruments',[])
                or not isinstance(response.get('rows'),list)):
            raise PositionReaderUnavailable('Unverified IPC snapshot freshness/identity')
        # CORE already validated grouping and NET safety before IPC; defense
        # in depth: zero/negative and duplicate handling is validated below.
        # Batched OKX GET may omit instruments without positions. An empty
        # slice is not an authenticated zero; require narrow GET independently.
        # Empty rows require a positive proof; old Reader stays fail-closed.
        if not response['rows'] and response.get('absence_is_proven_zero') is not True:
            raise PositionReaderEmpty('Missing authenticated zero proof')
        if response['rows'] and response.get('absence_is_proven_zero') is True:
            raise PositionReaderUnavailable('Contradictory zero proof')
        if len(response['rows'])>1:
            raise PositionReaderUnavailable('Ambiguous NET position rows')
        for r in response['rows']:
            if not isinstance(r,dict) or r.get('instId')!=instrument or r.get('posSide')!='net':
                raise PositionReaderUnavailable('Unexpected instrument or position side')
        # These are untrusted diagnostic fields: validate before reporting;
        # they must NEVER affect trading result/verification.
        if on_trace is not None:
            try:
                values={}
                for key in ('quota_wait_ms','fetch_ms'):
                    x=response.get(key)
                    if type(x) in (int,float) and __import__('math').isfinite(x) and 0<=x<=120000:
                        values[key]=float(x)
                batch=response.get('batch_size')
                if type(batch) is int and 1<=batch<=256:
                    values['batch_size']=batch
                on_trace(values)
            except Exception:
                pass  # telemetry MUST be best-effort and non-blocking
        check_preempt()
        return response['rows']
    except (OSError,TimeoutError,ValueError,TypeError,UnicodeError,KeyError,
            SnapshotError) as exc:
        raise PositionReaderUnavailable('Reader IPC/response unavailable: '+type(exc).__name__) from exc
