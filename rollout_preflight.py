#!/usr/bin/env python3
"""Read-only local VPS acceptance gate for the OKX 0.7.30 candidate.

Does not invoke exchange APIs, touch systemd state, open .env for output,
read/modify order databases, or update trading/Telegram files. Returns nonzero
on uncertainty. Package staging and actual exchange GET audit are separate.
"""
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile
from okx_gate_snapshot import read_gate_snapshot, GateSnapshotUnavailable

BOTS_ROOT=Path('/root/okx')
SHARED=Path('/opt/okx-grid-live')
QUOTAS={
 'OKX_ACCOUNT_POSITIONS_GATE_PATH':BOTS_ROOT/'.account_positions_gate_v075.json',
 'OKX_FILL_HISTORY_GATE_PATH':BOTS_ROOT/'.account_fills_history_gate_v0712.json'
}
BOT_RE=re.compile(r'[a-z0-9-]{3,32}\Z')

class Unsafe(RuntimeError):pass

def need(ok,msg):
    if not ok:raise Unsafe(msg)

def bytes_digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()

def jfile(path):
    need(path.is_file() and not path.is_symlink(),'Unsafe/missing '+str(path))
    value=json.loads(path.read_text(encoding='utf-8'))
    need(isinstance(value,dict),'Expected object '+str(path))
    return value

def unit_props(name):
    p=subprocess.run(['systemctl','show',name,'--no-pager',
       '-p','ActiveState','-p','MainPID','-p','WorkingDirectory','-p','FragmentPath',
       '-p','ExecStart','-p','LoadState'],capture_output=True,text=True,timeout=12)
    need(p.returncode==0,'systemctl show failed: '+name)
    return dict(x.split('=',1) for x in p.stdout.splitlines() if '=' in x)

def env_gate_check(path):
    need(path.is_file() and not path.is_symlink(),'Missing/unsafe env file')
    need(path.stat().st_mode & 0o077 == 0,'Env permissions too broad')
    found={}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:continue
        k,v=line.split('=',1);k=k.strip()
        if k in QUOTAS:found[k]=v.strip().strip('"').strip("'")
    for key,expected in QUOTAS.items():
        candidate=Path(found[key]) if key in found else expected
        need(candidate.is_absolute() and candidate==expected,
             'Unexpected account gate override for '+key)
    return 'shared gate defaults/overrides canonical'

def quota_schema_check(path):
    # Shared JSON is rewritten in-place under an exclusive flock. Reading it
    # without a matching shared lock can see truncate-before-write emptiness.
    try:
        obj=read_gate_snapshot(path,'calls',allow_missing=True)
    except GateSnapshotUnavailable as exc:
        raise Unsafe(str(exc)) from exc
    if obj is None:
        return 'not-created (first-use must conservatively fill window)'
    return 'schema=1'

def state_risk(st):
    # Only unambiguous critical intent states. A deliberately conservative
    # predicate; a clean local snapshot cannot prove exchange coverage.
    if st.get('terminal_mode') or st.get('panic_orders'):
        return 'terminal/panic state present'
    for c in st.get('cycles',{}).values():
        if isinstance(c,dict):
            if c.get('state') in ('PLACING','CANCELING'):
                return 'BUY order still transitioning'
            for t in c.get('tpOrders',[]):
                if isinstance(t,dict) and t.get('phase') in ('PLACING','CANCELING'):
                    return 'TP order still transitioning'
    for g in st.get('aggregate_exits',[]):
        if isinstance(g,dict) and g.get('phase') not in ('FILLED','REJECTED','CANCELED'):
            return 'aggregate MARKET exit still unresolved'
    return None

def preflight(args):
    stage=args.stage.resolve(strict=True)
    need(stage.is_dir() and not args.stage.is_symlink(),'Unsafe staging directory')
    pkg=Path(args.package)
    need(pkg.is_file() and not pkg.is_symlink(),'Missing/unsafe package')
    # Verify review-pinned bytes BEFORE interpreting any staged source.
    need(bytes_digest(pkg)==args.sha256,'Untrusted ZIP hash')
    manifest=jfile(stage/'engine_manifest.json')
    # Extract literal metadata only; never exec() a staged Python file.
    declarations=ast.parse((stage/'okx_version.py').read_text(encoding='utf-8'))
    ns={}
    for statement in declarations.body:
        if isinstance(statement,ast.Assign) and len(statement.targets)==1 and isinstance(statement.targets[0],ast.Name):
            if statement.targets[0].id in ('ENGINE_VERSION','RELEASE_VERSION','STATE_SCHEMA'):
                ns[statement.targets[0].id]=ast.literal_eval(statement.value)
    engine=ns.get('ENGINE_VERSION')
    need(isinstance(engine,str) and manifest.get('current_version')==engine,
         'Stage engine/manifest mismatch')
    need(ns.get('STATE_SCHEMA')==1,'Unknown stage state schema')
    published=manifest.get('engines',{}).get(engine)
    need(isinstance(published,dict) and published.get('sha256')==bytes_digest(stage/'okx_bot.py'),
         'Stage engine digest mismatch')
    with ZipFile(pkg) as archive:
        names=archive.namelist()
        need(len(set(names))==len(names),'Duplicate entries in ZIP')
        for item in names:
            need('..' not in Path(item).parts and not item.startswith('/'),'Unsafe ZIP member')
            target=stage/item
            need(target.is_file() and not target.is_symlink(),'Missing stage member '+item)
            need(bytes_digest(target)==hashlib.sha256(archive.read(item)).hexdigest(),
                 'Stage differs from candidate ZIP: '+item)
    print('PACKAGE PASS:',ns.get('RELEASE_VERSION'),'engine',engine,'ZIP',args.sha256)
    names=list(dict.fromkeys(args.bot))
    need(bool(names),'Choose at least one bot for preflight')
    for bot in names:
        need(BOT_RE.fullmatch(bot),'Invalid bot name')
        root=BOTS_ROOT/bot
        need(root.is_dir() and not root.is_symlink() and root.resolve()==root,'Unsafe bot root')
        cfg=jfile(root/'config.json');state=jfile(root/'state/bot_state.json')
        version=cfg.get('version')
        need(isinstance(version,str) and version==state.get('version'),bot+' version mismatch')
        need(state.get('schema_version')==ns['STATE_SCHEMA'],bot+' state schema differs')
        need(state.get('instrument')==cfg.get('instrument'),bot+' instrument mismatch')
        need(isinstance(state.get('cycles'),dict),bot+' cycles invalid')
        need(cfg.get('live_trading') is True,bot+' not configured LIVE; review separately')
        rec=manifest.get('engines',{}).get(version)
        need(isinstance(rec,dict),bot+' engine not attested in candidate')
        need(bytes_digest(root/'okx_bot.py')==rec['sha256'],bot+' disk engine not attested')
        env_gate_check(root/'.env')
        risk=state_risk(state)
        need(risk is None,bot+' not safe to stop at this snapshot: '+str(risk))
        u=unit_props('okx-grid-'+bot+'.service')
        need(u.get('LoadState')=='loaded' and u.get('ActiveState')=='active'
             and int(u.get('MainPID','0'))>0,bot+' trading process not active')
        need(u.get('WorkingDirectory')==str(root),bot+' unit root mismatch')
        need(str(root/'okx_bot.py') in u.get('ExecStart',''),bot+' unit exec mismatch')
        need(u.get('FragmentPath')==str(Path('/etc/systemd/system')/('okx-grid-'+bot+'.service')),
             bot+' unit file mismatch')
        heartbeat=jfile(root/'state/runtime_stats.json')
        try: age=time.time()-float(heartbeat.get('last',0))
        except (ValueError,TypeError) as exc:raise Unsafe(bot+' heartbeat unavailable') from exc
        need(-5<=age<=75,bot+' heartbeat stale or clock inconsistent')
        for kind in ('execution','burst'):
            observer=unit_props('observer-okx-grid-'+bot+'-'+kind+'.service')
            need(observer.get('ActiveState')=='active',bot+' '+kind+' observer not active')
        print('BOT PREFLIGHT PASS:',bot,'engine',version,'PID',u['MainPID'],
              'heartbeat_seconds',round(age,1),'intent_check','clean')
    for key,path in QUOTAS.items():
        print('SHARED QUOTA:',key,path.name,quota_schema_check(path))
        queue=path.with_name(path.name+'.fifo-v0712.json')
        if read_gate_snapshot(queue,'waiters',allow_missing=True) is not None:
            print('SHARED FIFO:',queue.name,'schema=1')
    monitor=unit_props('okx-telegram-monitor.service')
    need(monitor.get('ActiveState')=='active','Telegram Monitor not active')
    print('LOCAL PREFLIGHT PASS; NO EXCHANGE GET, NO POSITION/ORDER AUDIT, NO WRITES')


def main():
    parser=argparse.ArgumentParser(description='Read-only preflight; never stops or restarts LIVE')
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--bot',action='append',required=True)
    args=parser.parse_args()
    need(re.fullmatch('[0-9a-f]{64}',args.sha256) is not None,'Bad SHA256 argument')
    preflight(args)

if __name__=='__main__':
    try:main()
    except (Unsafe,OSError,ValueError,KeyError,subprocess.TimeoutExpired,RuntimeError) as err:
        print('PREFLIGHT BLOCKED:',type(err).__name__,str(err),file=sys.stderr)
        sys.exit(2)
