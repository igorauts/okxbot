#!/usr/bin/env python3
"""Guarded Reader-only install of 230ms pacing, bucket capacity two.

--preflight: read-only checks, compile + isolated pacer tests.
--install: backed-up source-only patch, Reader restart, health check, auto-rollback.
--rollback: restore exact preinstall bytes and restart Reader.
Never patches engine, shared quota gate, credentials, registry or other services.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SERVICE = 'okx-position-reader.service'
BACKUPS = Path('/srv/okx-reader-pacing-backups')
MANIFEST = BACKUPS / 'current.json'
MARKER = 'OKX_READER_PACING_230_PLUS_ONE_BEGIN'
PACER = '''\
# OKX_READER_PACING_230_PLUS_ONE_BEGIN
class _ReaderPacer230PlusOne:
    """Reader-local two-permit bucket, 1/230ms; existing account gate remains authoritative."""
    GAP = 0.230
    CAPACITY = 2.0
    def __init__(self):
        self._tokens = self.CAPACITY
        self._last = time.monotonic()
        self.delays = 0
        self.wait_ms = 0.0
    async def take(self, priority_supplier):
        start = time.monotonic()
        delayed = False
        while True:
            priority_supplier()  # fail closed if all client requests abandoned
            now = time.monotonic()
            self._tokens = min(self.CAPACITY, self._tokens + max(0, now-self._last)/self.GAP)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                if delayed:
                    self.delays += 1
                    self.wait_ms += (now-start)*1000
                return
            delayed = True
            await asyncio.sleep(min(.050, (1.0-self._tokens)*self.GAP))
# OKX_READER_PACING_230_PLUS_ONE_END
'''

class Refuse(RuntimeError):
    pass

def digest(data):
    return hashlib.sha256(data).hexdigest()

def cmd(args, timeout=35):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)

def live_pid():
    r = cmd(['systemctl','show',SERVICE,'--no-pager','-p','LoadState','-p','ActiveState','-p','MainPID'])
    if r.returncode:
        raise Refuse('Reader systemd show failed')
    fields = {}
    for line in r.stdout.splitlines():
        key,sep,val = line.partition('=')
        if not sep or key in fields:
            raise Refuse('Unexpected systemd metadata')
        fields[key]=val
    if fields.get('LoadState')!='loaded' or fields.get('ActiveState')!='active':
        raise Refuse('Reader must already be active; no blind restart')
    try:
        pid = int(fields['MainPID'])
    except (KeyError,ValueError):
        raise Refuse('Unknown Reader PID')
    if pid < 2:
        raise Refuse('Reader has invalid PID')
    return pid

def live_source(pid):
    proc = Path('/proc') / str(pid)
    args = [a.decode('utf8') for a in (proc/'cmdline').read_bytes().split(b'\0') if a]
    matches = [a for a in args if a.endswith('/okx_position_reader_service.py') or a=='okx_position_reader_service.py']
    if len(matches)!=1:
        raise Refuse('Cannot uniquely resolve running Reader source')
    path=Path(matches[0])
    if not path.is_absolute():
        path=(proc/'cwd').resolve() / path
    if path.is_symlink() or not path.is_file():
        raise Refuse('Reader source is absent or a symlink')
    if path.resolve()!=Path('/opt/okx-grid-live/okx_position_reader_service.py'):
        raise Refuse('Unrecognized Reader source path: '+str(path))
    s=path.stat()
    if s.st_uid!=0 or s.st_nlink!=1:
        raise Refuse('Reader source must be root-owned with one hard link')
    return path

def unique(nodes, name, cls):
    result = [x for x in nodes if isinstance(x,cls) and x.name==name]
    if len(result)!=1:
        raise Refuse(f'Expected one {name} definition; got {len(result)}')
    return result[0]

def patch(src):
    if MARKER in src or '_reader_pacer' in src or 'ReaderPacer' in src:
        raise Refuse('Existing/suspected pacing patch; no double application')
    if not src.endswith('\n'):
        raise Refuse('Source lacks final newline')
    old = ast.parse(src)
    imports={a.name for node in old.body if isinstance(node,ast.Import) for a in node.names}
    if not {'asyncio','time'} <= imports:
        raise Refuse('Expected Reader imports not present')
    klass=unique(old.body,'SharedOriginalQuota',ast.ClassDef)
    init=unique(klass.body,'__init__',ast.FunctionDef)
    take=unique(klass.body,'take',ast.AsyncFunctionDef)
    if [a.arg for a in take.args.args]!=['self','priority_supplier'] or take.args.kwonlyargs or take.args.vararg or take.args.kwarg:
        raise Refuse('Quota take() signature differs')
    gates=[x for x in ast.walk(init) if isinstance(x,ast.Attribute) and x.attr=='gate']
    if not gates:
        raise Refuse('Original quota gate not found')
    thread_calls=[x for x in ast.walk(take) if isinstance(x,ast.Call) and isinstance(x.func,ast.Attribute)
                  and x.func.attr=='to_thread' and isinstance(x.func.value,ast.Name)
                  and x.func.value.id=='asyncio']
    if not (1 <= len(thread_calls) <= 2):
        raise Refuse('Original quota-taking code unexpected')
    if not any(isinstance(x,ast.Attribute) and x.attr in ('take_cancellable','take_prioritized') for x in ast.walk(take)):
        raise Refuse('Original quota entrypoints absent')
    lines=src.splitlines(keepends=True)
    changes=[(klass.lineno-1,PACER),
             (init.body[0].lineno-1,'        self._reader_pacer = _ReaderPacer230PlusOne()\n'),
             (take.body[0].lineno-1,'        await self._reader_pacer.take(priority_supplier)\n')]
    if len(set(i for i,_ in changes))!=3:
        raise Refuse('Ambiguous patch locations')
    for pos, addition in sorted(changes,reverse=True):
        lines.insert(pos,addition)
    modified=''.join(lines)
    compile(modified,'<reader candidate>','exec')
    parsed=ast.parse(modified)
    newclass=unique(parsed.body,'SharedOriginalQuota',ast.ClassDef)
    newinit=unique(newclass.body,'__init__',ast.FunctionDef)
    newtake=unique(newclass.body,'take',ast.AsyncFunctionDef)
    if not isinstance(newinit.body.pop(0),ast.Assign) or not isinstance(newtake.body.pop(0),ast.Expr):
        raise Refuse('Pacer was not inserted first')
    parsed.body=[n for n in parsed.body if not (isinstance(n,ast.ClassDef) and n.name=='_ReaderPacer230PlusOne')]
    if ast.dump(parsed,include_attributes=False)!=ast.dump(old,include_attributes=False):
        raise Refuse('Original Reader AST changed unexpectedly')
    return modified

def selftest(modified):
    klass=unique(ast.parse(modified).body,'_ReaderPacer230PlusOne',ast.ClassDef)
    isolated=ast.Module(body=[klass],type_ignores=[])
    namespace={'time':time,'asyncio':asyncio}
    exec(compile(isolated,'<pacer isolated>','exec'),namespace,namespace)
    p=namespace['_ReaderPacer230PlusOne']()
    async def test():
        t0=time.monotonic()
        await p.take(lambda:2)
        a=time.monotonic()
        await p.take(lambda:2)
        b=time.monotonic()
        await p.take(lambda:2)
        c=time.monotonic()
        assert a-t0<.05 and b-a<.05, 'first two calls paced'
        assert .16<c-b<.45, 'third call pacing unexpected'
        assert p.delays==1 and p.wait_ms>160
        before=p._tokens
        def abandoned(): raise RuntimeError('abandoned')
        try: await p.take(abandoned)
        except RuntimeError as exc: assert str(exc)=='abandoned'
        else: raise AssertionError('abandoned request was not rejected')
        assert p._tokens==before
        await asyncio.sleep(.49)
        start=time.monotonic()
        await p.take(lambda:2)
        await p.take(lambda:2)
        assert time.monotonic()-start<.05, 'idle tokens did not replenish'
    asyncio.run(test())

def atomic_write(path, content, prev_stat):
    fd,name=tempfile.mkstemp(prefix='.reader_230_',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.chmod(name,stat.S_IMODE(prev_stat.st_mode))
        os.chown(name,prev_stat.st_uid,prev_stat.st_gid)
        os.replace(name,path)
        dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(name): os.unlink(name)

def restart_check(before,source):
    r=cmd(['systemctl','restart',SERVICE])
    if r.returncode:
        return False, 'systemctl restart failed '+r.stderr[:200]
    for _ in range(5):
        time.sleep(1)
        try:
            pid=live_pid()
            if pid==before or live_source(pid)!=source:
                return False,'Reader PID or source mismatch after restart'
        except (Refuse,OSError,UnicodeError) as exc:
            return False,'Reader not healthy: '+str(exc)
    return True,'MainPID='+str(pid)

def prepare():
    if os.geteuid()!=0:
        raise Refuse('Run on VPS as root')
    if MANIFEST.exists():
        raise Refuse('Existing installed pacing manifest; no stacking')
    pid=live_pid()
    source=live_source(pid)
    raw=source.read_bytes()
    if len(raw)>3_000_000 or not raw:
        raise Refuse('Reader source size unexpected')
    changed=patch(raw.decode('utf8')).encode('utf8')
    selftest(changed.decode('utf8'))
    print('PREFLIGHT_PASS source='+str(source))
    print('ORIGINAL_SHA256='+digest(raw))
    print('CANDIDATE_SHA256='+digest(changed))
    print('PATCH_SCOPE=Reader source only; original AST preserved')
    print('PACER_TEST=PASS; initial permits 2, refill 1/230ms, abandoned check')
    print('HARD_SHARED_GATE_UNCHANGED=YES')
    return pid,source,raw,changed

def install():
    pid,source,raw,changed=prepare()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder=BACKUPS/stamp
    folder.mkdir(parents=True,mode=0o700,exist_ok=False)
    backup=folder/source.name
    backup.write_bytes(raw)
    os.chmod(backup,0o600)
    manifest={'source':str(source),'backup':str(backup),
              'before':digest(raw),'after':digest(changed),'utc':stamp}
    if source.read_bytes()!=raw:
        raise Refuse('Source concurrently modified before install; original untouched')
    atomic_write(source,changed,source.stat())
    # Persist recovery information BEFORE restarting Reader.
    tmp=MANIFEST.with_suffix('.new')
    try:
        tmp.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        os.chmod(tmp,0o600)
        os.replace(tmp,MANIFEST)
    except Exception:
        atomic_write(source,raw,source.stat())
        raise
    ok,msg=restart_check(pid,source)
    if ok:
        print('===== READER_PACING_230_PLUS_ONE_LIVE_PASS =====')
        print(msg)
        print('TRADING_SERVICES_NOT_TOUCHED=YES')
        print('Monitor LIVE IPC/GET latency and quota before considering performance proven')
        return
    print('POSTFLIGHT_FAIL='+msg,file=sys.stderr)
    if digest(source.read_bytes())!=digest(changed):
        raise Refuse('Concurrent source change; backup at '+str(backup))
    atomic_write(source,raw,source.stat())
    restored=cmd(['systemctl','restart',SERVICE])
    if restored.returncode:
        raise Refuse('Original source restored, but Reader restart failed!')
    MANIFEST.unlink()
    raise Refuse('Automatic rollback completed after failed Reader postflight')

def rollback():
    if os.geteuid()!=0 or not MANIFEST.is_file() or MANIFEST.is_symlink():
        raise Refuse('No valid manifest / root required')
    m=json.loads(MANIFEST.read_text())
    source=Path(m['source']); backup=Path(m['backup'])
    if source!=Path('/opt/okx-grid-live/okx_position_reader_service.py'):
        raise Refuse('Unrecognized source in manifest')
    if source.is_symlink() or not source.is_file() or backup.is_symlink() or not backup.is_file():
        raise Refuse('Original or installed source missing')
    cur=source.read_bytes(); old=backup.read_bytes()
    if digest(cur)!=m['after'] or digest(old)!=m['before']:
        raise Refuse('Source changed since install; cannot safely auto-rollback')
    try:
        pid=live_pid()
    except Refuse:
        pid=0  # Restore remains possible after Reader service failure.
    atomic_write(source,old,source.stat())
    ok,msg=restart_check(pid,source)
    if not ok:
        raise Refuse('Old source restored but Reader health check failed: '+msg)
    MANIFEST.unlink()
    print('===== READER_PACING_ROLLBACK_PASS =====')
    print(msg)

def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--preflight',action='store_true')
    group.add_argument('--install',action='store_true')
    group.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    try:
        if args.preflight:
            prepare()
            print('READ_ONLY=YES NO_SERVICE_RESTART=YES')
        elif args.install: install()
        else: rollback()
    except (Refuse,OSError,UnicodeError,ValueError,SyntaxError,AssertionError,
            KeyError,subprocess.TimeoutExpired) as exc:
        print('READER_PACING_FAIL_CLOSED='+str(exc),file=sys.stderr)
        sys.exit(2)

if __name__=='__main__': main()
