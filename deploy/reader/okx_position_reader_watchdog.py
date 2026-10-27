#!/usr/bin/env python3
"""GET-free Reader IPC watchdog; runs only if installed+enabled by operator.

A crash is handled by systemd on-failure. This optional separate timer checks
active-but-unresponsive Reader and uses a strictly bounded, same-PID restart.
It NEVER starts an inactive/failed Reader or touches trading bots/OKX.
"""
import argparse
import fcntl
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

UNIT = 'okx-position-reader.service'
SOCKET = '/run/okx-position-reader/positions.sock'
STATE = Path('/var/lib/okxbot/reader_watchdog_state.json')
LOCK = Path('/run/okx-position-reader-watchdog.lock')
MIN_FAILURES = 3
MIN_RESTART_GAP = 600
MAX_RESTARTS_PER_HOUR = 2


def show():
    try:
        result = subprocess.run(['systemctl','show',UNIT,'--no-pager',
           '-p','ActiveState','-p','MainPID','-p','InvocationID'],
           text=True,capture_output=True,timeout=4,check=False)
        if result.returncode:return None
        rows=dict(x.split('=',1) for x in result.stdout.splitlines() if '=' in x)
        pid=rows.get('MainPID','')
        if not pid.isdecimal() or int(pid)<=0:
            return None
        return rows
    except (OSError,subprocess.TimeoutExpired,ValueError):
        return None


def ipc_ok(path=SOCKET):
    deadline=time.monotonic()+2.0
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(path)
            s.sendall(b'{"schema":1,"op":"health"}\n')
            buf=b''
            while b'\n' not in buf and len(buf)<=4096:
                left=deadline-time.monotonic()
                if left<=0:return False
                s.settimeout(left)
                chunk=s.recv(4096)
                if not chunk:return False
                buf+=chunk
            if len(buf)>4096:return False
            doc=json.loads(buf.split(b'\n',1)[0])
            data=doc.get('data') if isinstance(doc,dict) else None
            if not (doc.get('ok') is True and isinstance(data,dict)
                    and data.get('reader_running') is True):
                return False
            # Optional new IPC metric: a blocked core worker is NOT healthy
            # merely because the event loop answers health. Normal bounded
            # quota+GET complete well below this conservative threshold.
            age=data.get('inflight_age_ms',0)
            if isinstance(age,(int,float)) and age>45000:
                return False
            return True
    except (OSError,TimeoutError,ValueError,TypeError):
        return False


def _state_load():
    if not STATE.exists():return {}
    if STATE.is_symlink() or not STATE.is_file() or STATE.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe watchdog state')
    value=json.loads(STATE.read_text())
    if not isinstance(value,dict):raise RuntimeError('Invalid watchdog state')
    return value


def _state_save(data):
    STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.reader-watchdog-',dir=STATE.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f)
            f.flush();os.fsync(f.fileno())
        os.chmod(name,0o600)
        os.replace(name,STATE)
        dfd=os.open(STATE.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(name):os.unlink(name)


def policy(prev, *, now, pid, healthy):
    """Pure decision function. Always fail closed on discontinuities."""
    data=dict(prev)
    if not isinstance(pid,str) or not pid.isdecimal() or not int(pid):
        return data,False
    if not isinstance(data.get('last_check',0),(int,float)):
        return data,False
    if float(data.get('last_check',0))>now or now-float(data.get('last_check',0))>3600:
        data={'fails':0,'pid':pid,'restarts':[]}
    times=data.get('restarts',[])
    if not isinstance(times,list):return data,False
    try:
        times=[float(x) for x in times if isinstance(x,(int,float)) and 0<=now-float(x)<3600]
    except (ValueError,TypeError):return data,False
    prior_pid=data.get('pid')
    fails=(int(data.get('fails',0)) if prior_pid==pid else 0)
    data['pid']=pid
    data['last_check']=now
    if healthy:
        data['fails']=0
        data['restarts']=times
        return data,False
    data['fails']=min(fails+1,MIN_FAILURES)
    data['restarts']=times
    authorized=(data['fails']>=MIN_FAILURES and len(times)<MAX_RESTARTS_PER_HOUR
                and (not times or now-max(times)>=MIN_RESTART_GAP))
    return data,authorized


def main():
    args=argparse.ArgumentParser(description=__doc__)
    args.add_argument('--check-only',action='store_true',help='never restart service')
    opts=args.parse_args()
    fd=os.open(LOCK,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return
    try:
        first=show()
        if not first or first.get('ActiveState')!='active':
            print('Reader not active: no watchdog restart')
            return
        healthy=ipc_ok()
        try:previous=_state_load()
        except (OSError,RuntimeError,ValueError) as e:
            print('State invalid, fail-closed:',type(e).__name__)
            return
        now=time.time()
        state,may_restart=policy(previous,now=now,pid=first['MainPID'],healthy=healthy)
        if may_restart and not opts.check_only:
            second=show()
            if (second and second.get('ActiveState')=='active' and
                second.get('MainPID')==first['MainPID'] and
                second.get('InvocationID')==first.get('InvocationID') and
                not ipc_ok()):
                # Record the attempt BEFORE invoking systemctl. A failed command
                # still counts toward the restart budget; never restart-spam.
                state['restarts'].append(now)
                state['fails']=0
                _state_save(state)
                p=subprocess.run(['systemctl','restart',UNIT],timeout=12,check=False)
                print('Reader restart attempted; status:',p.returncode)
                return
        if not opts.check_only:
            _state_save(state)
        print('reader IPC:', 'OK' if healthy else 'UNRESPONSIVE',
              '| consecutive failures:',state.get('fails',0),
              '| restart eligible:',bool(may_restart and not opts.check_only))
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

if __name__=='__main__':
    main()
