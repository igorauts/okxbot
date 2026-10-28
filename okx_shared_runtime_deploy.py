#!/usr/bin/env python3
"""Two-stage shared-only LIVE installer; NO trading-engine changes.

Preflight is read-only (systemctl metadata + local files + health IPC).
Apply requires explicit confirmation, exact ZIP digest and byte-identical stage.
Reader / Telegram ONLY restart; trading services never stopped or restarted.
Manual rollback from private backup; no blind automatic rollback on failure.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SHARED=Path('/opt/okx-grid-live')
TELEGRAM=Path('/root/okx')
REGISTRY=Path('/root/okx/.okxbot_registry.json')
BACKUPS=Path('/srv/okxbot-live-backups')
READER_UNIT='okx-position-reader.service'
TELEGRAM_UNIT='okx-telegram-monitor.service'
WATCHDOG_UNIT='okx-position-reader-watchdog.timer'
# Exactly attested current LIVE baselines: installed new-bot autoreg patch +
# verified published v0.7.35 Telegram modules. No version-string asserts.
BASELINE={
    'okxbot.py':'02d7796a6a77fce18896e2502e0d77173121ca083870565f9c0869f99cc7177a',
    'okx_position_reader_ipc.py':'0bc13b98edea48d3b10bd1d3320850c17907aafc263e08d84075b757dc25d9ac',
    'okx_telegram_monitor.py':'f77e383273defbadd6d3b78638007c247518a7449e7cb202b2d59d0ac0024060',
    'okx_telegram_health.py':'44b5b488d7ea527ba0e5c62865e225934e77c7533ea3a90173d87dce17a09893',
    'okx_version.py':'ba45a79f3c037a8136a6d549fa99cae8bc01d297ef6c72c6607db3cec21b2ddd',
}
SHARED_FILES=('okxbot.py','okx_position_reader_ipc.py','okx_reader_diagnostics.py',
              'okx_version.py','okx_live_upgrade.py','rollout_preflight.py')
TELEGRAM_FILES=('okx_telegram_health.py','okx_telegram_monitor.py')


def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):
            h.update(b)
    return h.hexdigest()


def require_file(p, *, optional=False):
    p=Path(p)
    if optional and not p.exists() and not p.is_symlink():return False
    if not p.is_file() or p.is_symlink():raise RuntimeError('Unsafe or missing file: '+str(p))
    return True


def proc(cmd,timeout=8):
    return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=timeout)


def systemd(unit,key):
    result=proc(['systemctl','show',unit,'-p',key,'--value','--no-pager'])
    return result.stdout.strip()


def old_ipc_alive():
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect('/run/okx-position-reader/positions.sock')
        s.sendall(b'{"schema":1,"op":"health"}\n')
        buf=b''
        while b'\n' not in buf and len(buf)<=8192:
            b=s.recv(4096)
            if not b:break
            buf+=b
    val=json.loads(buf.split(b'\n')[0])
    if not isinstance(val,dict) or val.get('ok') is not True or val.get('data',{}).get('reader_running') is not True:
        raise RuntimeError('Reader IPC not healthy')
    return val['data']


def bots():
    require_file(REGISTRY)
    reg=json.loads(REGISTRY.read_text())
    if not isinstance(reg,dict):raise RuntimeError('Malformed registry')
    result={}
    for name,entry in reg.items():
        if not isinstance(name,str) or not isinstance(entry,dict):
            raise RuntimeError('Malformed registry record')
        service=entry.get('service','')
        path=Path(str(entry.get('path','')))
        if (not re.fullmatch(r'okx-grid-[a-z0-9-]+',service) or
                path.parent != TELEGRAM or path.name != service.removeprefix('okx-grid-')):
            raise RuntimeError('Untrusted registry path/service')
        unit=service+'.service'
        if systemd(unit,'ActiveState')!='active':continue
        pid=systemd(unit,'MainPID')
        if not pid.isdecimal() or int(pid)<=0:raise RuntimeError('Bad live PID')
        if (path/'state/final_stats.json').exists():
            raise RuntimeError('Active terminal bot: fail closed')
        cfg=json.loads((path/'config.json').read_text())
        inst=cfg.get('instrument')
        if not isinstance(inst,str) or not re.fullmatch('[A-Z0-9]+-USDT-SWAP',inst):
            raise RuntimeError('Unsafe live instrument')
        result[name]={'pid':pid,'instrument':inst,'path':path}
    if not result:raise RuntimeError('No active trading bots attested')
    return result


def package_check(args):
    require_file(args.package)
    if not re.fullmatch('[a-f0-9]{64}',args.sha256) or digest(args.package)!=args.sha256:
        raise RuntimeError('ZIP digest mismatch')
    from tests.verify_release_asset import verify
    release,engine,engine_sha=verify(args.package)
    if engine_sha != digest(args.stage/'okx_bot.py'):
        raise RuntimeError('Stage engine mismatch')
    with zipfile.ZipFile(args.package) as z:
        if z.namelist() != sorted(z.namelist()):
            raise RuntimeError('ZIP entries not sorted')
        for name in z.namelist():
            p=args.stage/name
            require_file(p)
            if digest(p) != hashlib.sha256(z.read(name)).hexdigest():
                raise RuntimeError('Stage bytes differ from ZIP: '+name)
        for name in ('okxbot.py','okx_live_upgrade.py','rollout_preflight.py'):
            zi=z.getinfo(name)
            if (zi.external_attr >> 16) & 0o777 != 0o755:
                raise RuntimeError('Executable bit lost: '+name)
    # Trading engine remains byte-identical to LIVE (no strategy changes).
    if digest(SHARED/'okx_bot.py')!=engine_sha:
        raise RuntimeError('Trading engine digest differs from package')
    print('PACKAGE PASS:',release,'engine',engine,'SHA256',args.sha256)


def preflight(args):
    if os.geteuid()!=0:raise RuntimeError('Run as root on owner VPS')
    if not args.stage.is_dir() or args.stage.is_symlink():raise RuntimeError('Unsafe staging dir')
    sys.path.insert(0,str(args.stage))
    package_check(args)
    # Strictly require exact reviewed prior LIVE source to avoid overwrites.
    for name,expected in BASELINE.items():
        root=TELEGRAM if name.startswith('okx_telegram_') else SHARED
        require_file(root/name)
        if digest(root/name)!=expected:
            raise RuntimeError('Unexpected LIVE baseline: '+name)
    if (SHARED/'okx_reader_diagnostics.py').exists():
        raise RuntimeError('Reader Diagnostics already exists; inspect first')
    for u in (READER_UNIT, TELEGRAM_UNIT, WATCHDOG_UNIT):
        if systemd(u,'ActiveState')!='active':raise RuntimeError('Unit not ACTIVE: '+u)
    if systemd(READER_UNIT,'NeedDaemonReload')!='no':
        raise RuntimeError('Reader systemd unit needs reload')
    before=bots()
    old_ipc_alive()
    if not (Path('/root/okx/.okxbot_account.env').is_file()):
        raise RuntimeError('Account .env missing')
    print('BASELINE PASS: Reader, Telegram, watchdog, active bots:',
          {n:d['pid'] for n,d in before.items()})
    print('PREFLIGHT PASS — NO WRITES / RESTARTS / OKX GET')
    return before


def atomic_install(src,dst):
    src=Path(src);dst=Path(dst)
    require_file(src)
    if dst.is_symlink() or not dst.parent.is_dir():
        raise RuntimeError('Unsafe destination: '+str(dst))
    fd,tmp=tempfile.mkstemp(prefix='.reader-release-',dir=dst.parent)
    try:
        with os.fdopen(fd,'wb') as out,src.open('rb') as inp:
            shutil.copyfileobj(inp,out)
            out.flush();os.fsync(out.fileno())
        os.chmod(tmp,src.stat().st_mode & 0o777)
        os.replace(tmp,dst)
        directory=os.open(dst.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    if digest(dst)!=digest(src):raise RuntimeError('Post-install digest mismatch: '+str(dst))


def apply(args):
    before=preflight(args)
    if not args.confirm_live_reader_telegram_restart:
        raise RuntimeError('Explicit LIVE confirmation required')
    base=BACKUPS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+'-shared-runtime')
    base.mkdir(parents=True,mode=0o700)
    os.chmod(base,0o700)
    meta={'before_pids':{n:v['pid'] for n,v in before.items()},
          'package_sha256':args.sha256, 'files':{}}
    for name in (*SHARED_FILES,*TELEGRAM_FILES):
        root=TELEGRAM if name.startswith('okx_telegram_') else SHARED
        dest=root/name
        if require_file(dest,optional=True):
            backup=base/name
            shutil.copy2(dest,backup)
            os.chmod(backup,0o600)
            meta['files'][name]={'before':digest(dest),'location':str(dest)}
        else:
            meta['files'][name]={'before':None,'location':str(dest)}
    (base/'MANIFEST.json').write_text(json.dumps(meta,indent=2))
    os.chmod(base/'MANIFEST.json',0o600)
    print('BACKUP PASS:',base,flush=True)
    # Installing shared sources does not replace running trading engines.
    for name in SHARED_FILES:
        atomic_install(args.stage/name,SHARED/name)
    print('SHARED SOURCES PASS; RESTARTING READER ONLY',flush=True)
    proc(['systemctl','restart',READER_UNIT],timeout=25)
    import importlib.util
    spec=importlib.util.spec_from_file_location('verified_reader_diag',args.stage/'okx_reader_diagnostics.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ready=None
    for _ in range(20):
        ready=module.reader_health()
        if ready is not None:break
        time.sleep(.4)
    if ready is None:raise RuntimeError('Reader health failed; STOP, do not retry blindly')
    instruments=set(ready['instruments'])
    if any(v['instrument'] not in instruments for v in before.values()):
        raise RuntimeError('Active instrument absent in Reader; STOP and investigate')
    print('READER HEALTH+REGISTRATIONS PASS',flush=True)
    if {n:d['pid'] for n,d in bots().items()} != {n:d['pid'] for n,d in before.items()}:
        raise RuntimeError('Trading PID changed; stop and investigate')
    print('ALL TRADING PIDS UNCHANGED — PASS',flush=True)
    for name in TELEGRAM_FILES:
        atomic_install(args.stage/name,TELEGRAM/name)
    proc(['systemctl','restart',TELEGRAM_UNIT],timeout=25)
    time.sleep(2)
    if systemd(TELEGRAM_UNIT,'ActiveState')!='active':
        raise RuntimeError('Telegram not ACTIVE after restart; STOP')
    if {n:d['pid'] for n,d in bots().items()} != {n:d['pid'] for n,d in before.items()}:
        raise RuntimeError('Trading PID changed after Telegram; STOP')
    if systemd(WATCHDOG_UNIT,'ActiveState')!='active':
        raise RuntimeError('Watchdog no longer ACTIVE')
    print('READER + TELEGRAM LIVE PASS; ALL TRADING PIDS UNCHANGED',flush=True)
    print('BACKUP:',base,flush=True)
    print('LIVE READER DIAGNOSTICS PATCH PASS — NO TRADING ENGINE RESTART',flush=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('preflight','apply'))
    p.add_argument('--stage',type=Path,required=True)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--sha256',required=True)
    p.add_argument('--confirm-live-reader-telegram-restart',action='store_true')
    a=p.parse_args()
    if a.action=='preflight':preflight(a)
    else:apply(a)

if __name__=='__main__':
    try:main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        print('INSTALLER STOP:',type(exc).__name__,str(exc),file=sys.stderr)
        sys.exit(1)
