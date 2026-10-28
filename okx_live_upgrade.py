#!/usr/bin/env python3
"""Controlled engine-identity upgrade, one bot at a time.

NO API orders here. systemctl stop leaves exchange orders live. Upgraded engine
must pass its GET-only audit before systemctl start. Never auto-restart on failure.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from okx_gate_snapshot import read_gate_snapshot

PACKAGE_SHA256 = None  # REQUIRED --sha256 of operator-reviewed ZIP
OLD_ENGINE_SHA256 = None  # loaded from attested staged engine manifest
OLD_VERSION = None  # explicitly chosen old engine identity, attested by digest
NEW_VERSION = None  # obtained from the staged manifest
PACKAGE = None  # set via --package
STAGE = None  # set via --stage
SHARED = Path('/opt/okx-grid-live')
BASE = Path('/root/okx')
BACKUPS = Path('/srv/okxbot-live-backups')
TELEGRAM_ROOT = Path('/root/okx')
TELEGRAM_UNIT = 'okx-telegram-monitor.service'
BOTS = ()  # assigned from --bot, never a hardcoded list
LOCK = Path('/run/okxbot-live-upgrade.lock')


def fail(message):
    raise RuntimeError(message)


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as src:
        for chunk in iter(lambda: src.read(1048576), b''):h.update(chunk)
    return h.hexdigest()


def run(*args, timeout=90, env=None):
    return subprocess.run(args, check=True, timeout=timeout, text=True,
                          capture_output=True, env=env)


def check_file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():fail('Unsafe or missing file: '+str(path))
    return path


def json_read(path):
    o=json.loads(check_file(path).read_text(encoding='utf-8'))
    if not isinstance(o,dict):fail('Not JSON object: '+str(path))
    return o


def atomic_replace_file(src, dst):
    src=check_file(src);dst=Path(dst)
    if dst.is_symlink() or not dst.parent.is_dir():fail('Unsafe destination '+str(dst))
    fd,tmp=tempfile.mkstemp(dir=dst.parent,prefix='.okx-upgrade-')
    try:
        with os.fdopen(fd,'wb') as out,src.open('rb') as inp:
            shutil.copyfileobj(inp,out)
            out.flush();os.fsync(out.fileno())
        os.chmod(tmp, src.stat().st_mode & 0o777)
        os.replace(tmp,dst)
        dfd=os.open(dst.parent,os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def atomic_json(path,obj):
    path=check_file(path)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.okx-json-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(obj,out,ensure_ascii=False,indent=2)
            out.write('\n');out.flush();os.fsync(out.fileno())
        os.chmod(tmp,path.stat().st_mode & 0o777)
        os.replace(tmp,path)
        dfd=os.open(path.parent,os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def files_required():
    manifest=json_read(STAGE/'engine_manifest.json')
    if manifest.get('schema_version')!=1 or manifest.get('current_version')!=NEW_VERSION:
        fail('Manifest version mismatch')
    engine=manifest.get('engines',{}).get(NEW_VERSION)
    if not isinstance(engine,dict) or engine.get('file')!='okx_bot.py':
        fail('Engine manifest entry invalid')
    deps=engine.get('dependencies')
    if not isinstance(deps,list) or len(deps)!=len(set(deps)):
        fail('Invalid dependencies')
    import re
    if any(not isinstance(x,str) or not re.fullmatch('[A-Za-z0-9_]+\\.py',x) for x in deps):
        fail('Unsafe dependencies')
    for n in ['okx_bot.py', *deps]:check_file(STAGE/n)
    if sha256(STAGE/'okx_bot.py') != engine.get('sha256'):
        fail('Current engine bytes not matching manifest')
    return ['okx_bot.py',*deps]


def allowed_package_member(name):
    """Permit only documented source/test/historical and Reader recovery paths."""
    if (not isinstance(name, str) or not name or name.endswith('/') or
            '\\' in name or '\x00' in name or name.startswith('/')):
        return False
    parts = Path(name).parts
    if '..' in parts or any(x in ('', '.') or x.startswith('.') for x in parts[1:]):
        return False
    if name.startswith('.') and name not in ('.gitignore', '.env.example'):
        return False
    if len(parts) == 1:
        return True
    if len(parts) == 2:
        return parts[0] in ('tests', 'historical')
    return len(parts) == 3 and parts[:2] == ('deploy', 'reader')


def stage_check():
    check_file(PACKAGE)
    if sha256(PACKAGE)!=PACKAGE_SHA256:fail('ZIP SHA256 mismatch')
    if not STAGE.is_dir() or STAGE.is_symlink():fail('Missing or unsafe staging directory')
    # Stage was already clean-tested by operator; verify each packaged member byte-for-byte.
    from zipfile import ZipFile
    with ZipFile(PACKAGE) as z:
        names=z.namelist()
        if len(names)<53 or len(set(names))!=len(names) or any(
            not allowed_package_member(n) for n in names):
            fail('Unexpected ZIP members')
        # Fail before writing ANY shared file if packaging stripped the manager's
        # executable bit. The source 0.7.30 manager was executable (0755).
        manager = z.getinfo('okxbot.py')
        manager_mode = manager.external_attr >> 16
        if manager.create_system != 3 or (manager_mode & 0o777) != 0o755:
            fail('Manager ZIP permission must be 0755; refuse shared install')
        if not os.access(STAGE/'okxbot.py', os.X_OK):
            fail('Manager staged file is not executable; refuse shared install')
        for name in names:
            if (z.getinfo(name).create_system==3 and
                    (z.getinfo(name).external_attr >> 16) & 0o170000 != 0o100000):
                fail('Non-regular ZIP entry: '+name)
            if (STAGE/name).is_symlink():fail('Stage file symlink: '+name)
            if sha256(STAGE/name)!=hashlib.sha256(z.read(name)).hexdigest():
                fail('Staging/ZIP mismatch: '+name)
    files_required()
    from importlib.util import spec_from_file_location, module_from_spec
    spec=spec_from_file_location('okx_version_installer',STAGE/'okx_version.py')
    mod=module_from_spec(spec);spec.loader.exec_module(mod)
    if (mod.ENGINE_VERSION != NEW_VERSION or mod.STATE_SCHEMA != 1
            or mod.validate_version_ids() is not True):
        fail('Version constants mismatch')
    return True


def unit_check(bot, need_active=True):
    if bot not in BOTS:fail('Bot not explicitly selected')
    root=BASE/bot
    if root.is_symlink() or not root.is_dir() or root.resolve()!=BASE/bot:fail('Unsafe bot root')
    unit='okx-grid-'+bot+'.service'
    props=run('systemctl','show',unit,'--no-pager','-p','ActiveState','-p','MainPID',
              '-p','WorkingDirectory','-p','FragmentPath','-p','ExecStart').stdout
    kv=dict(x.split('=',1) for x in props.splitlines() if '=' in x)
    if kv.get('WorkingDirectory')!=str(root):fail('systemd WorkingDirectory mismatch')
    if f'{root}/okx_bot.py' not in kv.get('ExecStart','') or f'{root}/.venv/bin/python' not in kv.get('ExecStart',''):
        fail('Unexpected ExecStart')
    if kv.get('FragmentPath')!=f'/etc/systemd/system/{unit}':fail('Unexpected systemd unit file')
    active=kv.get('ActiveState')=='active'
    if need_active and (not active or int(kv.get('MainPID','0'))<=0):fail('Trading service not ACTIVE')
    if not check_file(root/'state/bot_state.json') or not check_file(root/'config.json'):fail('Missing state')
    return root,unit,kv


def instance_check(bot, desired, need_active=True):
    root,unit,kv=unit_check(bot,need_active=need_active)
    config=json_read(root/'config.json');state=json_read(root/'state/bot_state.json')
    if (config.get('version')!=desired or state.get('version')!=desired
        or state.get('schema_version')!=1 or not isinstance(state.get('cycles'),dict)
        or state.get('instrument')!=config.get('instrument')):
        fail(bot+' config/state mismatched or unrecognized')
    if state.get('terminal_mode') in ('RETIRED','PANIC_DONE'):
        fail(bot+' terminal; never restart')
    control=root/'state/control.json'
    if control.exists() or control.is_symlink():
        value=json_read(control)
        if value.get('mode') != 'RUN':
            fail(bot+' not in RUN control mode')
    engine=check_file(root/'okx_bot.py')
    if desired==OLD_VERSION and sha256(engine)!=OLD_ENGINE_SHA256:
        fail(bot+' OLD installed engine bytes not matching pinned published baseline')
    if desired==NEW_VERSION and sha256(engine)!=sha256(STAGE/'okx_bot.py'):
        fail(bot+' engine hash not matching candidate')
    return root,unit,kv



def stop_safety_readonly(root, state):
    """Recheck invariants immediately before stopping a real-money instance.

    Local snapshot is only an eligibility gate; actual position/ownership requires
    the separate exchange audit and can change while the engine is stopped.
    """
    if not isinstance(state.get('cycles'),dict):
        fail('Unrecognized bot cycle state; refusing LIVE stop')
    if state.get('terminal_mode') or state.get('panic_orders'):
        fail('Terminal/PANIC intent present; refusing LIVE stop')
    for cycle in state['cycles'].values():
        if not isinstance(cycle,dict):fail('Malformed cycle; refusing LIVE stop')
        if cycle.get('state') in ('PLACING','CANCELING'):
            fail('Unresolved BUY intent; refusing LIVE stop')
        for rec in cycle.get('tpOrders',[]):
            if isinstance(rec,dict) and rec.get('phase') in ('PLACING','CANCELING'):
                fail('Unresolved TP intent; refusing LIVE stop')
    for group in state.get('aggregate_exits',[]):
        if (not isinstance(group,dict) or
                group.get('phase') not in ('FILLED','CANCELED','REJECTED')):
            fail('Unresolved aggregate MARKET intent; refusing LIVE stop')
    env_path=root/'.env'
    check_file(env_path)
    if env_path.stat().st_mode & 0o077:
        fail('Bot .env permissions unsafe')
    expected={
        'OKX_ACCOUNT_POSITIONS_GATE_PATH':'/root/okx/.account_positions_gate_v075.json',
        'OKX_FILL_HISTORY_GATE_PATH':'/root/okx/.account_fills_history_gate_v0712.json',
    }
    # Never display .env contents or print credentials.
    for line in env_path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        key,value=line.split('=',1);key=key.strip()
        if key in expected and value.strip().strip('\"').strip("'")!=expected[key]:
            fail('Unapproved shared account FIFO override: '+key)
    for path in map(Path,expected.values()):
        for target,field in ((path,'calls'),
                             (path.with_name(path.name+'.fifo-v0712.json'),'waiters')):
            # Read under same inode flock as the trading processes. Never
            # replace/unlink or modify the account-wide quota/FIFO files.
            read_gate_snapshot(target,field,allow_missing=True)
    return True


def backup_dir(label):
    BACKUPS.mkdir(mode=0o700,parents=True,exist_ok=True)
    if BACKUPS.is_symlink():fail('Unsafe backup root')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    result=BACKUPS/(stamp+'-'+label)
    result.mkdir(mode=0o700)
    return result


def check_tree_no_symlinks(path):
    for root,dirs,files in os.walk(path,followlinks=False):
        for name in dirs+files:
            if (Path(root)/name).is_symlink():fail('Symlink in backed-up state: '+str(Path(root)/name))


def backup_instance(root,dest,engine_files):
    (dest/'code').mkdir(mode=0o700)
    for name in engine_files:
        src=root/name
        if src.exists():shutil.copy2(check_file(src),dest/'code'/name)
    shutil.copy2(root/'config.json',dest/'config.json')
    check_tree_no_symlinks(root/'state')
    shutil.copytree(root/'state',dest/'state')
    # No keys/.env/logs are included or copied to another server.
    (dest/'backup_complete').write_text('config, state, code; no .env\n')


def verify_staging():
    stage_check()
    if not SHARED.is_dir() or SHARED.is_symlink():fail('Bad shared runtime')
    for bot in BOTS:
        root,unit,kv=unit_check(bot,need_active=False)
        cfg=json_read(root/'config.json');st=json_read(root/'state/bot_state.json')
        if cfg.get('version')!=st.get('version') or st.get('schema_version')!=1:
            fail(bot+' metadata invalid')
        if cfg.get('version') not in (OLD_VERSION, NEW_VERSION):
            fail(bot+' version not in supported upgrade path')
        # Verify the actual on-disk engine BEFORE any stop or destructive write.
        # Verify old bytes against an immutable digest, not a version label.
        instance_check(bot,cfg['version'],need_active=True)
        print('PREFLIGHT',bot,kv.get('ActiveState'), 'engine',cfg['version'],
              'pid',kv.get('MainPID'),'installed-engine SHA256 PASS')
    print('PREFLIGHT PASS: pinned ZIP, bytes-identical stage, chosen instance metadata coherent')


def install_shared():
    stage_check()
    if not SHARED.is_dir() or SHARED.is_symlink():fail('Unsafe shared runtime')
    # Do not touch the trading services. Shared Python modules are imported on next process start.
    dest=backup_dir('shared')
    to_copy=sorted(p.name for p in STAGE.iterdir() if p.suffix=='.py')+['engine_manifest.json']
    (dest/'old').mkdir(mode=0o700)
    old_exists=[]
    for name in to_copy:
        src=SHARED/name
        if src.exists():
            shutil.copy2(check_file(src),dest/'old'/name)
            old_exists.append(name)
    (dest/'old_existing.json').write_text(json.dumps(old_exists))
    try:
        for name in to_copy:atomic_replace_file(STAGE/name,SHARED/name)
        for name in to_copy:
            if sha256(STAGE/name)!=sha256(SHARED/name):fail('Shared content mismatch: '+name)
        if not os.access(SHARED/'okxbot.py',os.X_OK):
            fail('Manager executable bit lost; shared install requires rollback')
    except BaseException:
        for name in old_exists:atomic_replace_file(dest/'old'/name,SHARED/name)
        for name in to_copy:
            if name not in old_exists and (SHARED/name).is_file(): (SHARED/name).unlink()
        raise
    (dest/'complete').write_text('Shared runtime updated, no trading services changed\n')
    print('SHARED PASS:',len(to_copy),'files; backup:',dest)


def install_telegram():
    """Separate shared Telegram-only patch. NO trading services are touched."""
    stage_check()
    if TELEGRAM_ROOT.is_symlink() or not TELEGRAM_ROOT.is_dir():
        fail('Unsafe Telegram root')
    names=sorted(p.name for p in STAGE.glob('okx_telegram_*.py'))
    if len(names)<5:fail('Incomplete Telegram module set')
    old=backup_dir('telegram')
    (old/'old').mkdir(mode=0o700)
    existed=[]
    for name in names:
        source=TELEGRAM_ROOT/name
        if source.exists():
            shutil.copy2(check_file(source),old/'old'/name)
            existed.append(name)
    (old/'old_existing.json').write_text(json.dumps(existed))
    try:
        for name in names:atomic_replace_file(STAGE/name,TELEGRAM_ROOT/name)
        for name in names:
            if sha256(STAGE/name)!=sha256(TELEGRAM_ROOT/name):
                fail('Telegram content mismatch: '+name)
        # This is NOT a trading bot restart. Telegram monitor must reload its
        # Python modules to activate new alarms and the durable delivery fix.
        run('systemctl','restart',TELEGRAM_UNIT,timeout=60)
        status=run('systemctl','show',TELEGRAM_UNIT,
                   '-p','ActiveState','--value',timeout=15).stdout.strip()
        if status!='active':fail('Telegram monitor not ACTIVE after restart')
    except BaseException:
        for name in existed:atomic_replace_file(old/'old'/name,TELEGRAM_ROOT/name)
        for name in names:
            if name not in existed and (TELEGRAM_ROOT/name).is_file():
                (TELEGRAM_ROOT/name).unlink()
        # Telegram has no trading authority. Restore old monitor once; never
        # touch trading services. If recovery fails, report both failures.
        try:
            run('systemctl','restart',TELEGRAM_UNIT,timeout=60)
        except Exception as rollback_exc:
            print('TELEGRAM RESTORE RESTART FAILED:',str(rollback_exc)[:300],
                  file=sys.stderr,flush=True)
        raise
    (old/'complete').write_text('Telegram-only updated; bot engines untouched\n')
    print('TELEGRAM PASS:',len(names),'files; monitor ACTIVE; backup:',old)


def audit_output_complete(returncode, output):
    """Require the entire mandatory pre-LIVE financial audit, not an early marker.

    The current engine prints AUDIT CONSISTENT before mandatory PnL/history
    verification. Its outer exception handler can print STOP while exiting 0.
    The complete financial report prints the final closed-cycle confirmation.
    """
    if returncode != 0 or not isinstance(output, str):
        return False
    required = ('AUDIT CONSISTENT', '✅ Все закрытые циклы сопоставлены.')
    failure = ('⛔ STOP:', '⚠️ Статистика неполная:',
               '⚠️ Статистика неполная', 'Финансовые суммы не выводятся.')
    return all(marker in output for marker in required) and not any(
        marker in output for marker in failure)


def upgrade_bot(bot):
    stage_check()
    shared_ver=json_read(SHARED/'engine_manifest.json').get('current_version')
    if shared_ver!=NEW_VERSION:fail('Shared runtime is not staged target engine')
    engine_files=files_required()
    # Check staged dependencies are byte-identical to installed shared runtime.
    for name in engine_files:
        if sha256(STAGE/name)!=sha256(check_file(SHARED/name)):
            fail('Shared runtime dependency mismatch: '+name)
    root,unit,kv=instance_check(bot,OLD_VERSION,need_active=True)
    stop_safety_readonly(root,json_read(root/'state/bot_state.json'))
    before_pid=int(kv['MainPID'])
    print('STOPPING',bot,'PID',before_pid,'(exchange orders remain active)',flush=True)
    run('systemctl','stop',unit,timeout=75)
    # 'systemctl is-active' exits 3 on a safely stopped service; do not use check=True.
    # Re-read authoritative service properties instead.
    stopped=unit_check(bot,need_active=False)[2]
    if stopped.get('ActiveState')!='inactive':
        fail('Service not INACTIVE after stop; NOTHING overwritten')
    stopped_pid=stopped.get('MainPID')
    if stopped_pid not in ('0',None):fail('Nonzero PID after stop; NOTHING overwritten')
    # Re-evaluate state AFTER service actually stopped and flushed any completed writes.
    root,unit,_=instance_check(bot,OLD_VERSION,need_active=False)
    stop_safety_readonly(root,json_read(root/'state/bot_state.json'))
    dest=backup_dir(bot)
    backup_instance(root,dest,engine_files)
    print('BACKUP PASS:',dest,flush=True)
    try:
        for name in engine_files:atomic_replace_file(STAGE/name,root/name)
        config=json_read(root/'config.json');state=json_read(root/'state/bot_state.json')
        if config.get('version')!=OLD_VERSION or state.get('version')!=OLD_VERSION:
            fail('State changed during upgrade')
        config['version']=NEW_VERSION
        state['version']=NEW_VERSION
        atomic_json(root/'config.json',config)
        atomic_json(root/'state/bot_state.json',state)
        instance_check(bot,NEW_VERSION,need_active=False)
        print('AUDIT START',bot,'GET only, no trading',flush=True)
        env={k:v for k,v in os.environ.items() if k not in ('OKX_GRID_LIVE','OKX_GRID_PROBE','OKX_GRID_STATS')}
        env['OKX_GRID_AUDIT']='READ_ONLY'
        proc=subprocess.run([str(root/'.venv/bin/python'),'-u',str(root/'okx_bot.py')],
            cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=180)
        print('\n'.join(proc.stdout.splitlines()[-28:]),flush=True)
        if not audit_output_complete(proc.returncode, proc.stdout):
            fail('READ-ONLY AUDIT did not fully finish; BOT REMAINS STOPPED')
        print('AUDIT PASS',bot,flush=True)
    except BaseException:
        # Fail before any LIVE start: restore config, state and engine files.
        # No automatic restart, because exchange state must be independently checked.
        try:
            for name in engine_files:
                old=dest/'code'/name
                if old.is_file():atomic_replace_file(old,root/name)
                elif (root/name).is_file():(root/name).unlink()
            atomic_replace_file(dest/'config.json',root/'config.json')
            atomic_replace_file(dest/'state/bot_state.json',root/'state/bot_state.json')
        finally:
            print('ROLLBACK FILES ATTEMPTED; SERVICE LEFT STOPPED; check OKX orders immediately!',flush=True)
        raise
    # Record pre-start heartbeat; a recently written OLD heartbeat is NOT proof
    # that the new process reached its own live loop.
    heartbeat=root/'state/runtime_stats.json'
    try: previous_heartbeat=float(json_read(heartbeat).get('last',0))
    except (ValueError,TypeError,OSError,RuntimeError):previous_heartbeat=0.0
    # Single controlled start AFTER audit, never panic/stop commands in manager.
    run('systemctl','start',unit,timeout=75)
    deadline=time.monotonic()+70
    ready=False
    while time.monotonic()<deadline:
        time.sleep(2)
        props=unit_check(bot,need_active=False)[2]
        if props.get('ActiveState')!='active' or int(props.get('MainPID','0'))<=0:
            fail('START FAILED/EXITED: '+bot+'; DO NOT AUTOMATICALLY ROLLBACK OR RESTART')
        try:
            h=json_read(heartbeat)
            last=float(h.get('last',0))
            if (last>previous_heartbeat and last>=time.time()-12 and
                int(props['MainPID'])!=before_pid):
                ready=True;break
        except (ValueError,TypeError,RuntimeError,OSError):pass
    if not ready:
        fail('START not verified within 70s: '+bot+'; NO AUTOMATIC NEXT BOT')
    instance_check(bot,NEW_VERSION,need_active=True)
    print('LIVE VERIFIED:',bot, 'engine',NEW_VERSION,'PID',props['MainPID'],
          'heartbeat fresh; backup',dest,flush=True)


def main():
    if os.geteuid()!=0:fail('Run as root on VPS')
    ap=argparse.ArgumentParser(description='Pinned ZIP, single-bot LIVE upgrade; no implicit restarts')
    ap.add_argument('action',choices=('preflight','shared','telegram','bot','status'))
    ap.add_argument('--bot',required=True,help='Explicit individual instance name (one only)')
    ap.add_argument('--package',type=Path,required=True,help='Pre-reviewed candidate ZIP')
    ap.add_argument('--stage',type=Path,required=True,help='Already unpacked identical ZIP contents')
    ap.add_argument('--sha256',required=True,help='Exact 64-hex reviewed ZIP SHA256')
    ap.add_argument('--from-engine',required=True,
                    help='Explicit currently installed engine identity; must have trusted manifest SHA256')
    ap.add_argument('--confirm-live-stop',action='store_true',
                    help='Acknowledge stop leaves exchange orders LIVE; required for bot')
    args=ap.parse_args()
    import re
    if not re.fullmatch(r'[a-z0-9-]{3,32}',args.bot):fail('Invalid bot name')
    if not re.fullmatch(r'[0-9a-f]{64}',args.sha256):fail('Invalid expected SHA256')
    global PACKAGE_SHA256, PACKAGE, STAGE, BOTS, OLD_VERSION, NEW_VERSION, OLD_ENGINE_SHA256
    PACKAGE_SHA256=args.sha256
    PACKAGE=args.package
    STAGE=args.stage
    BOTS=(args.bot,)
    # Materialize staged source-of-truth; no guessed version ranges or manual maps.
    expected=json_read(STAGE/'engine_manifest.json')
    NEW_VERSION=expected.get('current_version')
    OLD_VERSION=args.from_engine
    if not isinstance(NEW_VERSION,str) or OLD_VERSION==NEW_VERSION:
        fail('Upgrade requires different explicit source and target engine identities')
    old_entry=expected.get('engines',{}).get(OLD_VERSION)
    if not isinstance(old_entry,dict) or old_entry.get('file')!='okx_bot.py':
        fail('Source engine identity not attested in target package manifest')
    OLD_ENGINE_SHA256=old_entry.get('sha256')
    if not isinstance(OLD_ENGINE_SHA256,str) or not re.fullmatch(r'[0-9a-f]{64}',OLD_ENGINE_SHA256):
        fail('Source engine lacks exact published SHA256 identity')
    fd=os.open(LOCK,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='preflight':verify_staging()
        elif args.action=='shared':install_shared()
        elif args.action=='telegram':install_telegram()
        elif args.action=='bot':
            if not args.confirm_live_stop:
                fail('Refuse to stop trading bot without --confirm-live-stop')
            upgrade_bot(args.bot)
        else:
            _,_,kv=unit_check(args.bot,need_active=False)
            print(args.bot,kv.get('ActiveState'),'PID',kv.get('MainPID'),
                  'version',json_read(BASE/args.bot/'config.json').get('version'))
    finally:
        os.close(fd)

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('UPGRADE STOPPED:',type(e).__name__,str(e),file=sys.stderr,flush=True)
        sys.exit(1)
