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
from decimal import Decimal
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
FUNDING_ROOT = Path('/opt/okx-funding')
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
    if len(parts) == 2 and parts[0] in ('tests', 'historical'):
        return True
    if len(parts)==3 and parts[:2]==('tests','legacy') and parts[2] in ('__init__.py','single_transition_upgrader.py','okx_shared_runtime_deploy.py'):
        return True
    if len(parts)==2 and parts[0]=='tools' and parts[1] in (
            'README.md','build_release_zip.py',
            'okx_market_fill_audit.py','okx_reconcile_trigger_report.py',
            'export_component.py'):
        return True
    if len(parts)==3 and parts[:2]==('docs','history') and parts[2].endswith('.md'):
        return True
    if len(parts)==3 and parts[:2]==('tests','funding') and parts[2] in (
            'benchmark_funding.py','f1_bills_fixture.json','test_funding_collector.py',
            'test_funding_f6.py','test_funding_f61.py','test_funding_ledger.py',
            'test_funding_view_backup.py','test_terminal_funding_lifetime.py'):
        return True
    if len(parts) == 3 and parts[:2] == ('deploy', 'reader'):
        return True
    # Explicit historical oracle fixture and original offline tests. This
    # nested layout is test-only, never copied into a trading instance.
    if name in (
        'deploy/resources/README.md',
        'deploy/resources/collector.py',
        'deploy/resources/http429_audit.py',
        'funding/README.md',
        'funding/okx_funding_backup.py',
        'funding/okx_funding_collector.py',
        'funding/systemd/okx-funding-collector.service',
        'funding/systemd/okx-funding-collector.timer',
        'funding/systemd/paths.conf.example',
    ):return True
    if parts in (('tests','fixtures','okx_bot_original_full_v0760.py.gz'),
                 ('tests','fixtures','okx_bot_v0780_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0781_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0782_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0783_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0784_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0785_baseline.py.gz'),
                 ('tests','fixtures','okx_positions_fifo_v0788.py.gz')):
        return True
    if (len(parts) == 5 and parts[:2] == ('tests','stats_v2_original')
        and parts[2] in ('stats_v2_release_work','stats_v2_prod_candidate')
        and parts[3] == 'tests' and parts[4].startswith('test_')
        and parts[4].endswith('.py')):
        return True
    return False


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
    target=json_read(STAGE/'engine_manifest.json')['engines'][NEW_VERSION]
    if target.get('partial_detach_resume_schema')!=getattr(mod,'PARTIAL_DETACH_RESUME_SCHEMA',None):
        fail('Partial detachment resume capability mismatch')
    from okx_source_attestation import verify_bundle
    verify_bundle(STAGE)
    from okx_dependency_attestation import verify
    manifest=json_read(STAGE/'engine_manifest.json')
    verify(STAGE,manifest)
    from okx_install_manifest import load
    from okx_manager_version import RELEASE_VERSION, validate_manager_version
    install=load(STAGE,manifest)
    if install['release_version']!=RELEASE_VERSION or not validate_manager_version():
        fail('Install manifest release mismatch')
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



def detach_resume_required(state):
    # Completed historical detach records do not constrain future upgrades.
    return any(c.get('partialDetach') and c.get('state') in ('BUY_LIVE','BUY_PARTIAL','CANCELING')
        or any(r.get('detachTerminalProof') and r.get('phase')=='PENDING'
               for r in c.get('tpOrders',[]))
        for c in state.get('cycles',{}).values())


def stop_safety_readonly(root, state, *, allow_detach_resume=False):
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
        detach=cycle.get('partialDetach')
        if detach and cycle.get('state') in ('BUY_LIVE','BUY_PARTIAL'):
            if not allow_detach_resume:
                fail('Unresolved partial TP amendment; refusing LIVE stop')
            try:
                valid=(isinstance(detach,dict) and detach['phase'] in ('REQUESTED','CONFIRMED','REJECTED')
                    and detach['reqId'] and detach['attachAlgoId']
                    and all(detach[k] and detach[k]==cycle[k] for k in ('ordId','clOrdId','tpClOrdId'))
                    and int(detach['beforeUTime'])>0 and 0<float(detach['requestedAt'])<=time.time()
                    and 0<Decimal(str(detach['beforeFill']))<=Decimal(str(cycle['filledBuy']))<Decimal(str(cycle['sz']))
                    and (detach['phase']!='REQUESTED' or time.time()-float(detach['requestedAt'])>=10))
            except (KeyError,TypeError,ValueError,ArithmeticError):valid=False
            if not valid:fail('Partial TP amendment not durable or diagnostic window still active')
        if any(r.get('detachTerminalProof') and r.get('phase')=='PENDING' for r in cycle.get('tpOrders',[])):
            if not allow_detach_resume:fail('Unresolved terminal AMEND; compatible target required')
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


def systemd_properties(unit, *keys):
    args = [item for key in keys for item in ('-p',key)]
    result = run('systemctl','show',unit,'--no-pager',*args,timeout=30).stdout
    values = dict(line.split('=',1) for line in result.splitlines() if '=' in line)
    if not set(keys)<=values.keys():fail('Incomplete systemd properties: '+unit)
    return values


def systemd_bus(method, signature, *args):
    # busctl call wraps the method output arguments in an outer array.
    raw = run('busctl','--system','--json=short','call',
              'org.freedesktop.systemd1','/org/freedesktop/systemd1',
              'org.freedesktop.systemd1.Manager',method,signature,*args,timeout=30).stdout
    result=json.loads(raw)
    data=result.get('data')
    if not isinstance(data,list) or len(data)!=1:fail('Invalid systemd D-Bus reply: '+method)
    return data[0]


def quota_unit_inventory():
    # Structured inventory: no glyphs, headings, column or quoting assumptions.
    patterns=('okx-grid-*.service','okx-bot*.service')
    files=systemd_bus('ListUnitFilesByPatterns','asas','0',str(len(patterns)),*patterns)
    loaded=systemd_bus('ListUnitsByPatterns','asas','0',str(len(patterns)),*patterns)
    import re
    def valid(name):
        if not isinstance(name,str) or not re.fullmatch(r'okx[-a-z0-9]+\.service',name):
            fail('Unrecognized trading service in quota inventory')
        return name
    units={valid(Path(row[0]).name) for row in files}
    # Unregistered, unloaded not-found placeholders are not actual consumers.
    units.update(valid(row[0]) for row in loaded
                 if row[2]!='not-found' or row[3] not in ('inactive','failed'))
    registry=json_read(BASE/'.okxbot_registry.json')
    entries={}
    for entry in registry.values():
        if not isinstance(entry,dict):fail('Malformed quota registry entry')
        unit=valid(str(entry.get('service',''))+'.service')
        if unit in entries:fail('Duplicate quota registry service: '+unit)
        entries[unit]=entry
    units.update(entries)
    if not units:fail('No trading services in quota inventory')
    return units,entries


def unit_root(unit,entry,props):
    root=Path(props['WorkingDirectory'])
    if not root.is_absolute() or root.resolve()!=root or BASE not in root.parents:
        fail('Unsafe quota consumer path: '+unit)
    if entry is not None and Path(entry.get('path',''))!=root:
        fail('Registry quota consumer path mismatch: '+unit)
    return root


def terminal_disabled(root,entry,props):
    if props['ActiveState']!='inactive' or props['MainPID']!='0' or props['UnitFileState'] not in ('disabled','masked','masked-runtime'):
        return False
    # Durable terminal evidence, not merely a disabled/stopped RUN unit.
    if entry and entry.get('retired') is True and entry.get('terminal_phase') in ('PANIC_DONE','RETIRED'):
        return True
    for name,key in (('state/bot_state.json','terminal_mode'),('state/control_ack.json','phase')):
        path=root/name
        if path.exists() or path.is_symlink():
            if json_read(path).get(key) in ('PANIC_DONE','RETIRED'):return True
    return False


def boot_id():
    import re
    value=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch(r'[0-9a-f-]{36}',value):fail('Invalid kernel boot identity')
    return value


def shared_activation(manifest_path):
    # A successful, source-pinned shared transaction records a monotonic
    # lower bound for consumers that have subsequently loaded its modules.
    # No separate daemon or mutable runtime state is introduced.
    expected=sha256(check_file(manifest_path))
    current_boot=boot_id()
    matches=[];attempts=[]
    if BACKUPS.is_dir() and not BACKUPS.is_symlink():
        for d in BACKUPS.glob('*-shared'):
            p=d/'activation.json'
            if not d.is_symlink() and (d/'attempt.json').exists():
                attempt=json_read(d/'attempt.json')
                if attempt.get('boot_id')==current_boot:
                    stamp=attempt.get('monotonic_us')
                    if type(stamp)!=int or stamp<=0:fail('Invalid shared attempt proof')
                    attempts.append((stamp,d))
            if d.is_symlink() or not (d/'complete').is_file() or (d/'complete').is_symlink():continue
            if not p.exists() and not p.is_symlink():continue
            receipt=json_read(p)
            if receipt.get('manifest_sha256')!=expected:continue
            stamp=receipt.get('monotonic_us')
            if type(stamp)!=int or stamp<=0 or not isinstance(receipt.get('boot_id'),str) or len(receipt['boot_id'])!=36:
                fail('Invalid shared activation proof')
            matches.append(receipt)
    if attempts:
        _,latest=max(attempts,key=lambda row:row[0])
        if (not (latest/'complete').is_file() or (latest/'complete').is_symlink() or
                not (latest/'activation.json').is_file()):return None
        r=json_read(latest/'activation.json')
        if r.get('manifest_sha256')!=expected or r.get('boot_id')!=current_boot:return None
    same_boot=[r for r in matches if r['boot_id']==current_boot]
    if same_boot:return max(same_boot,key=lambda r:r['monotonic_us'])
    return matches[0] if matches else None


def started_after_shared(props, receipt):
    if not receipt or props.get('ActiveState')!='active' or int(props.get('MainPID','0'))<=0:
        return False
    stamp=int(props.get('ExecMainStartTimestampMonotonic','0'))
    return stamp>0 and (receipt['boot_id']!=boot_id() or stamp>receipt['monotonic_us'])


def guarded_reader_running():
    # If proof is absent (old Reader, interrupted install, old backup), keep
    # the external barrier required rather than assume that disk bytes are live.
    try:
        path=SHARED/'engine_manifest.json'
        manifest=json_read(path)
        current=manifest['engines'][manifest['current_version']]
        if current.get('quota_boot_guard_schema')!=1:return False
        from okx_install_manifest import load
        inventory=load(SHARED,manifest,installed=True)
        for name in ('okx_positions_fifo.py','okx_positions_priority.py','okx_position_reader_service.py'):
            if sha256(check_file(SHARED/name))!=manifest['source_modules_sha256'][name]:return False
        for dest,src in inventory['reader'].items():
            if sha256(check_file(SHARED/dest))!=inventory['component_sha256'][src]:return False
        props=systemd_properties('okx-position-reader.service','ActiveState','MainPID','ExecMainStartTimestampMonotonic','WorkingDirectory','NeedDaemonReload')
        if (props['WorkingDirectory']!=str(SHARED) or props['NeedDaemonReload']!='no'
                or not started_after_shared(props,shared_activation(path))):return False
        proc=Path('/proc')/props['MainPID']
        argv=[os.fsdecode(a) for a in (proc/'cmdline').read_bytes().split(b'\0') if a]
        return (Path(os.readlink(proc/'cwd')).resolve()==SHARED
                and str(SHARED/'okx_position_reader_service.py') in argv[1:])
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError):
        return False


def effective_boot_barrier(unit,dropin,raw,argv):
    if (dropin.parent.is_symlink() or dropin.resolve()!=dropin
            or check_file(dropin).read_bytes()!=raw):
        fail('Quota boot barrier missing or drifted: '+unit)
    props=systemd_properties(unit,'LoadState','NeedDaemonReload','DropInPaths')
    if (props['LoadState']!='loaded' or props['NeedDaemonReload']!='no'
            or str(dropin) not in props['DropInPaths'].split()):
        fail('Quota boot barrier not effective: '+unit)
    obj=systemd_bus('LoadUnit','s',unit)
    # get-property returns the variant value directly, unlike call. ExecStartPre
    # contains an argv array: a space inside the Python script stays in ONE arg.
    reply=json.loads(run('busctl','--system','--json=short','get-property',
                        'org.freedesktop.systemd1',obj,'org.freedesktop.systemd1.Service',
                        'ExecStartPre',timeout=30).stdout)
    rows=reply.get('data')
    if reply.get('type')!='a(sasbttttuii)' or not isinstance(rows,list):
        fail('Unrecognized effective ExecStartPre property: '+unit)
    if sum(isinstance(row,list) and len(row)==10 and row[0]==argv[0]
           and row[1]==argv and row[2] is False for row in rows)!=1:
        fail('Quota boot barrier not effective: '+unit)


def verify_quota_boot_barriers(unit_dir=Path('/etc/systemd/system')):
    """Read-only mixed-version gate, with a single prefix drop-in for bots.

    Disabled terminal units are excluded. Current/future guarded engines are
    attested by their actual complete dependency bytes, not their labels.
    Any unproven Reader still requires its one shared external barrier.
    """
    template=STAGE/'deploy/reader/05-quota-boot-safety.conf'
    if not template.exists():return
    raw=check_file(template).read_bytes()
    import shlex
    commands=[line.partition('=')[2] for line in raw.decode().splitlines() if line.startswith('ExecStartPre=')]
    if len(commands)!=1:fail('Invalid quota boot barrier template')
    argv=shlex.split(commands[0])
    if argv[:4]!=['/usr/bin/python3','-I','-B','-c'] or len(argv)!=5:
        fail('Invalid quota boot barrier command')
    manifest=json_read(STAGE/'engine_manifest.json')
    units,entries=quota_unit_inventory()
    legacy=[];guarded=0;terminal=0
    from okx_dependency_attestation import verify
    for unit in sorted(units):
        props=systemd_properties(unit,'LoadState','ActiveState','MainPID','UnitFileState','WorkingDirectory')
        entry=entries.get(unit)
        if props['LoadState']!='loaded':fail('Unverifiable registered/installed quota consumer: '+unit)
        root=unit_root(unit,entry,props)
        if terminal_disabled(root,entry,props):terminal+=1;continue
        cfg=json_read(root/'config.json');state=json_read(root/'state/bot_state.json')
        version=cfg.get('version')
        if version!=state.get('version'):fail('Quota consumer metadata mismatch: '+unit)
        info=manifest['engines'].get(version,{})
        if info.get('quota_boot_guard_schema')==1:
            verify(root,dict(manifest,current_version=version),bot_only=True)
            from okx_ui import installed_engine_version
            if (props['ActiveState']=='inactive' and props['MainPID']=='0' or
                    props['ActiveState']=='active' and installed_engine_version(
                        root,props,STAGE/'engine_manifest.json')=='v'+version):
                guarded+=1;continue
        if not unit.startswith('okx-grid-'):
            fail('Legacy registry service outside quota prefix requires explicit migration: '+unit)
        legacy.append(unit)
    unit_dir=Path(unit_dir)
    for unit in legacy:
        effective_boot_barrier(unit,unit_dir/'okx-grid-.service.d'/template.name,raw,argv)
    reader_guarded=guarded_reader_running()
    if not reader_guarded:
        unit='okx-position-reader.service'
        effective_boot_barrier(unit,unit_dir/(unit+'.d')/template.name,raw,argv)
    print('QUOTA_BOOT_BARRIERS=PASS', 'legacy='+str(len(legacy)), 'guarded='+str(guarded),
          'terminal_skipped='+str(terminal),'reader_guarded='+str(reader_guarded),flush=True)


def verify_telegram_after_shared():
    """Gate G5 before a bot stop; no service restart here."""
    manifest=json_read(STAGE/'engine_manifest.json')
    if not manifest['engines'][manifest['current_version']].get('telegram_shared_reload_required'):return
    from okx_install_manifest import load
    inventory=load(STAGE,manifest)
    for name in inventory['shared']:
        if sha256(check_file(SHARED/name))!=sha256(check_file(STAGE/name)):
            fail('Telegram gate: shared source mismatch: '+name)
    for name in inventory['telegram']:
        if sha256(check_file(TELEGRAM_ROOT/name))!=sha256(check_file(STAGE/name)):
            fail('Telegram gate: local source mismatch: '+name)
    if inventory.get('funding_view_previous_sha256') and sha256(check_file(FUNDING_ROOT/'okx_funding_view.py'))!=sha256(STAGE/'okx_funding_view.py'):
        fail('Funding view must be synchronized before bot upgrade')
    if telegram_shadow_plan(inventory):fail('Telegram gate: unretired shadow')
    keys=('ActiveState','SubState','MainPID','InvocationID','ExecMainStartTimestampMonotonic','NeedDaemonReload')
    props=systemd_properties(TELEGRAM_UNIT,*keys)
    receipt=shared_activation(SHARED/'engine_manifest.json')
    if (props['SubState']!='running' or props['NeedDaemonReload']!='no'
            or not started_after_shared(props,receipt)):
        fail('Telegram must load the installed shared release before bot upgrade')
    import re
    invocation=props['InvocationID']
    if not re.fullmatch(r'[a-f0-9]{32}',invocation):fail('Invalid Telegram invocation')
    logs=run('journalctl','_SYSTEMD_INVOCATION_ID='+invocation,'--no-pager','-o','cat',timeout=30).stdout
    if 'OKX Telegram monitor running T1-T4' not in logs.splitlines() or any(x in logs for x in ('Traceback (most recent call last)','ImportError','ModuleNotFoundError')):
        fail('Telegram startup proof absent or failed')
    telegram_import_preflight(inventory)
    if systemd_properties(TELEGRAM_UNIT,*keys)!=props:fail('Telegram changed during preflight')
    print('TELEGRAM_SHARED_RELOAD=PASS',props['MainPID'],flush=True)


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
    (dest/'attempt.json').write_text(json.dumps({'boot_id':boot_id(),
        'monotonic_us':time.monotonic_ns()//1000})+'\n')
    from okx_install_manifest import load
    inventory=load(STAGE,json_read(STAGE/'engine_manifest.json'))
    to_copy=inventory['shared']
    retired=[]
    for name,expected in inventory['retire_shared'].items():
        p=SHARED/name
        if p.exists() or p.is_symlink():
            if p.is_symlink() or not p.is_file() or sha256(p)!=expected:
                print('RETIRED FILE PRESERVED (unrecognized bytes):',name,flush=True)
            else:retired.append(name)
    # Copy only byte/mode changes; unchanged pinned engine/Reader files retain their inode.
    import stat
    changed=[]
    for name in to_copy:
        dst=SHARED/name
        if dst.is_symlink():fail('Unsafe shared destination: '+name)
        if not dst.exists():changed.append(name);continue
        check_file(dst)
        if sha256(dst)!=sha256(STAGE/name) or stat.S_IMODE(dst.stat().st_mode)!=stat.S_IMODE((STAGE/name).stat().st_mode):
            changed.append(name)
    (dest/'old').mkdir(mode=0o700)
    old_exists=[]
    for name in changed+retired:
        src=SHARED/name
        if src.exists() or src.is_symlink():
            shutil.copy2(check_file(src),dest/'old'/name)
            old_exists.append(name)
    (dest/'old_existing.json').write_text(json.dumps(old_exists))
    try:
        for name in changed:atomic_replace_file(STAGE/name,SHARED/name)
        for name in retired:
            if sha256(check_file(SHARED/name))!=inventory['retire_shared'][name]:
                fail('Retired file changed during install: '+name)
            (SHARED/name).unlink()
        for name in to_copy:
            if sha256(STAGE/name)!=sha256(SHARED/name):fail('Shared content mismatch: '+name)
        from okx_source_attestation import verify_bundle
        verify_bundle(SHARED)
        from okx_dependency_attestation import verify
        verify(SHARED,json_read(SHARED/'engine_manifest.json'),installed_shared=True)
        if not os.access(SHARED/'okxbot.py',os.X_OK):
            fail('Manager executable bit lost; shared install requires rollback')
        receipt={'manifest_sha256':sha256(SHARED/'engine_manifest.json'),
                 'boot_id':boot_id(),'monotonic_us':time.monotonic_ns()//1000}
        (dest/'activation.json').write_text(json.dumps(receipt)+'\n')
    except BaseException:
        for name in old_exists:atomic_replace_file(dest/'old'/name,SHARED/name)
        for name in changed:
            if name not in old_exists and (SHARED/name).is_file(): (SHARED/name).unlink()
        raise
    (dest/'complete').write_text('Shared runtime updated, no trading services changed\n')
    print('SHARED PASS:',len(to_copy),'files; changed:',len(changed),'retired:',len(retired),'backup:',dest)


def install_funding_view():
    """Synchronize the duplicate read-only Funding view; no Collector restart."""
    stage_check()
    if FUNDING_ROOT.is_symlink() or not FUNDING_ROOT.is_dir():fail('Unsafe Funding root')
    from okx_install_manifest import load
    inventory=load(STAGE,json_read(STAGE/'engine_manifest.json'))
    old_pin=inventory.get('funding_view_previous_sha256')
    import re
    if not isinstance(old_pin,str) or not re.fullmatch(r'[0-9a-f]{64}',old_pin):
        fail('No reviewed Funding view migration pin')
    name='okx_funding_view.py'
    if inventory['funding'].get(name)!=name:fail('Unexpected Funding view mapping')
    for dest,src in inventory['funding'].items():
        if dest!=name and sha256(check_file(FUNDING_ROOT/dest))!=sha256(check_file(STAGE/src)):
            fail('Funding collector component drift: '+dest)
    source=check_file(STAGE/name)
    target=check_file(FUNDING_ROOT/name)
    new_pin=sha256(source)
    if sha256(check_file(SHARED/name))!=new_pin:fail('Shared Funding view not yet installed')
    if sha256(target)==new_pin:
        print('FUNDING_VIEW=PASS already current; no restart',flush=True);return
    if sha256(target)!=old_pin:fail('Unrecognized installed Funding view')
    import stat
    mode=stat.S_IMODE(target.stat().st_mode)
    if mode & 0o022:fail('Writable Funding view by non-owner')
    backup=backup_dir('funding-view')
    old=backup/'old.py';new=backup/'candidate.py'
    shutil.copy2(target,old);shutil.copy2(source,new);new.chmod(mode)
    if sha256(target)!=old_pin:fail('Funding view changed during installation')
    try:
        atomic_replace_file(new,target)
        if sha256(target)!=new_pin or stat.S_IMODE(target.stat().st_mode)!=mode:
            fail('Funding view postflight mismatch')
    except BaseException:
        atomic_replace_file(old,target)
        raise
    (backup/'complete').write_text('Funding view synchronized; no service restart\n')
    print('FUNDING_VIEW=PASS backup:',backup,'no restart',flush=True)


def telegram_shadow_plan(inventory):
    """Only explicitly pinned duplicate files may be removed; refuse other shadows."""
    names=set(inventory['telegram'])
    planned=inventory.get('retire_telegram_shadows',{})
    retired=[]
    for name in sorted(set(inventory['shared'])-names):
        if not name.endswith('.py'):continue
        p=TELEGRAM_ROOT/name
        if p.exists() or p.is_symlink():
            if name not in planned or sha256(check_file(p)) not in planned[name]:
                fail('Unexpected Telegram root shadow: '+name)
            retired.append(name)
        # A package or sourceless module can also override the shared module.
        for alias in (TELEGRAM_ROOT/name[:-3], TELEGRAM_ROOT/(name+'c')):
            if alias.exists() or alias.is_symlink():
                fail('Unexpected Telegram import alias: '+str(alias))
    return retired


def telegram_import_preflight(inventory):
    """Fresh isolated interpreter, actual installed roots; never execute main()."""
    expected={name[:-3]:str((TELEGRAM_ROOT if name in inventory['telegram'] else SHARED)/name)
              for name in inventory['shared'] if name.endswith('.py')}
    code = r"""
import importlib, importlib.machinery, json, pathlib, sys
root,shared=sys.argv[1:3]
expected=json.loads(sys.argv[3])
sys.path[:0]=[root,shared]
for name,want in expected.items():
    spec=importlib.machinery.PathFinder.find_spec(name,[root,shared])
    if spec is None or spec.origin!=want:
        raise RuntimeError('Telegram import resolution mismatch: '+name)
# Import monitor as a module, not as __main__: no Telegram or OKX calls.
monitor=importlib.import_module('okx_telegram_monitor')
for name,module in tuple(sys.modules.items()):
    if name in expected and getattr(module,'__file__',None)!=expected[name]:
        raise RuntimeError('Telegram loaded wrong module: '+name)
if monitor._LATENCY_CLI_PATH!=str(pathlib.Path(shared)/'okx_per_bot_speed_readonly.py'):
    raise RuntimeError('Telegram latency helper is not canonical')
from okx_stats_ui import validate_financial_snapshot, require_fresh_snapshot
print('TELEGRAM_IMPORT_PREFLIGHT_PASS')
"""
    result=run('/usr/bin/python3','-I','-B','-c',code,str(TELEGRAM_ROOT),str(SHARED),json.dumps(expected),timeout=30)
    if 'TELEGRAM_IMPORT_PREFLIGHT_PASS' not in result.stdout:fail('Telegram import probe incomplete')
    print('TELEGRAM_IMPORT_PREFLIGHT_PASS',flush=True)


def install_telegram():
    """Separate shared Telegram-only patch. NO trading services are touched."""
    stage_check()
    if TELEGRAM_ROOT.is_symlink() or not TELEGRAM_ROOT.is_dir():
        fail('Unsafe Telegram root')
    from okx_install_manifest import load
    inventory=load(STAGE,json_read(STAGE/'engine_manifest.json'))
    names=inventory['telegram']
    if len(names)<5:fail('Incomplete Telegram module set')
    # The canonical shared release must already be installed, including tools.
    for name in inventory['shared']:
        if sha256(check_file(SHARED/name))!=sha256(check_file(STAGE/name)):
            fail('Shared runtime not installed: '+name)
    retired=telegram_shadow_plan(inventory)
    old=backup_dir('telegram')
    (old/'old').mkdir(mode=0o700)
    existed=[]
    for name in names+retired:
        source=TELEGRAM_ROOT/name
        if source.exists() or source.is_symlink():
            shutil.copy2(check_file(source),old/'old'/name)
            existed.append(name)
    (old/'old_existing.json').write_text(json.dumps(existed))
    restart_attempted=False
    try:
        for name in names:atomic_replace_file(STAGE/name,TELEGRAM_ROOT/name)
        for name in names:
            if sha256(STAGE/name)!=sha256(TELEGRAM_ROOT/name):
                fail('Telegram content mismatch: '+name)
        for name in retired:
            p=check_file(TELEGRAM_ROOT/name)
            if sha256(p) not in inventory['retire_telegram_shadows'][name]:
                fail('Telegram shadow changed during install: '+name)
            p.unlink()
        telegram_import_preflight(inventory)
        # A failed pre-restart probe restores files without touching the process.
        restart_attempted=True
        run('systemctl','restart',TELEGRAM_UNIT,timeout=60)
        status=run('systemctl','show',TELEGRAM_UNIT,
                   '-p','ActiveState','--value',timeout=15).stdout.strip()
        if status!='active':fail('Telegram monitor not ACTIVE after restart')
    except BaseException:
        for name in existed:atomic_replace_file(old/'old'/name,TELEGRAM_ROOT/name)
        for name in names:
            if name not in existed and (TELEGRAM_ROOT/name).is_file():
                (TELEGRAM_ROOT/name).unlink()
        if restart_attempted:
            try:
                run('systemctl','restart',TELEGRAM_UNIT,timeout=60)
            except Exception as rollback_exc:
                print('TELEGRAM RESTORE RESTART FAILED:',str(rollback_exc)[:300],
                      file=sys.stderr,flush=True)
        raise
    (old/'complete').write_text('Telegram-only updated; bot engines untouched\n')
    print('TELEGRAM PASS:',len(names),'files; retired:',len(retired),'monitor ACTIVE; backup:',old)


def audit_output_complete(returncode, output, *, single_instance=False):
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
    if any(marker in output for marker in failure):return False
    if not single_instance:
        return all(marker in output for marker in required)
    lines=output.splitlines()
    if not lines or required[1] not in output:return False
    # Explicit canary requires a final post-finance marker; never accept an
    # isolated proof in the standard strict installer path.
    return ((lines[-1]=='UPGRADE AUDIT COMPLETE STRICT' and required[0] in output)
        or (lines[-1]=='UPGRADE AUDIT COMPLETE ISOLATED'
            and 'AUDIT ISOLATED COVERAGE PASS' in output
            and 'TP links resolved: False' in output))


def verify_single_instance_sources(root):
    from okx_dependency_attestation import verify
    manifest=json_read(STAGE/'engine_manifest.json')
    verify(STAGE,manifest)
    # The canary may change only engine and its version module. All other
    # dependencies must match the current published instance and target.
    old=manifest['engines'][OLD_VERSION];new=manifest['engines'][NEW_VERSION]
    old_deps=set(old['dependencies']);new_deps=set(new['dependencies'])
    if new_deps-old_deps:
        fail('Single-instance dependency additions outside approved scope')
    removed=old_deps-new_deps
    if removed:
        from okx_install_manifest import load
        retirement=load(STAGE,manifest)['retire_shared']
        for name in removed:
            if retirement.get(name)!=old['dependency_sha256'].get(name) or name=='okx_version.py':
                fail('Single-instance dependency removal not pinned: '+name)
    for name in old['dependencies']:
        expected=old['dependency_sha256'][name]
        if sha256(check_file(root/name))!=expected:
            fail('Installed dependency drift: '+name)
        if name in new_deps and name!='okx_version.py' and new['dependency_sha256'][name]!=expected:
            fail('Single-instance dependency change outside approved scope: '+name)
    return True


def migrate_partial_policy(config):
    """One-time approved strategy policy transition; not tied to release numbers."""
    revision=config.get('partial_buy_policy_revision',1)
    if revision==1:
        print('PARTIAL_POLICY_MIGRATION:',
              f"{config.get('partial_buy_timeout_sec',60)}s/{config.get('partial_buy_distance_steps',5)}steps -> 5s/3steps",
              flush=True)
        config.update(partial_buy_timeout_sec=5,partial_buy_distance_steps=3,
                      partial_buy_policy_revision=2)
    elif revision!=2:
        fail('Unknown partial BUY policy revision')


def upgrade_bot(bot, *, single_instance=False):
    if OLD_VERSION==NEW_VERSION:fail('Same-engine bot restart not permitted')
    stage_check()
    engine_files=files_required()
    if not single_instance:
        shared_ver=json_read(SHARED/'engine_manifest.json').get('current_version')
        if shared_ver!=NEW_VERSION:fail('Shared runtime is not staged target engine')
        for name in engine_files:
            if sha256(STAGE/name)!=sha256(check_file(SHARED/name)):
                fail('Shared runtime dependency mismatch: '+name)
    root,unit,kv=instance_check(bot,OLD_VERSION,need_active=True)
    if single_instance:
        verify_single_instance_sources(root)
        engine_files=['okx_bot.py','okx_version.py']
    def guarded_stop_snapshot():
        snapshot=json_read(root/'state/bot_state.json')
        resume=detach_resume_required(snapshot)
        if resume:
            target=json_read(STAGE/'engine_manifest.json')['engines'][NEW_VERSION]
            if target.get('partial_detach_resume_schema')!=1:
                fail('Target does not declare compatible partial detachment resume')
            stop_safety_readonly(root,snapshot,allow_detach_resume=True)
            print('DURABLE_PARTIAL_DETACH_RESUME=YES',flush=True)
        else:stop_safety_readonly(root,snapshot)
        return resume
    resume_detach=guarded_stop_snapshot()
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
    resume_detach=guarded_stop_snapshot() or resume_detach
    if single_instance:verify_single_instance_sources(root)
    dest=backup_dir(bot)
    backup_instance(root,dest,engine_files)
    print('BACKUP PASS:',dest,flush=True)
    audit_started=False
    try:
        for name in engine_files:atomic_replace_file(STAGE/name,root/name)
        config=json_read(root/'config.json');state=json_read(root/'state/bot_state.json')
        if config.get('version')!=OLD_VERSION or state.get('version')!=OLD_VERSION:
            fail('State changed during upgrade')
        config['version']=NEW_VERSION
        migrate_partial_policy(config)
        state['version']=NEW_VERSION
        atomic_json(root/'config.json',config)
        atomic_json(root/'state/bot_state.json',state)
        instance_check(bot,NEW_VERSION,need_active=False)
        print('AUDIT START',bot,'GET only, no trading',flush=True)
        env={k:v for k,v in os.environ.items() if k not in ('OKX_GRID_LIVE','OKX_GRID_PROBE','OKX_GRID_STATS','OKX_GRID_ISOLATION_AUDIT','OKX_GRID_DETACH_RESUME_AUDIT','OKX_GRID_TERMINAL_ONLY')}
        env['OKX_GRID_AUDIT']='READ_ONLY'
        if single_instance or resume_detach:env['OKX_GRID_ISOLATION_AUDIT']='1'
        if resume_detach:env['OKX_GRID_DETACH_RESUME_AUDIT']='1'
        audit_started=True
        try:
            proc=subprocess.run([str(root/'.venv/bin/python'),'-u',str(root/'okx_bot.py')],
                cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=180)
        except subprocess.TimeoutExpired as exc:
            output=exc.stdout or ''
            if isinstance(output,bytes):output=output.decode('utf-8',errors='replace')
            if output:print(output,flush=True)
            raise
        if not audit_output_complete(proc.returncode, proc.stdout,single_instance=single_instance or resume_detach):
            if proc.stdout:print(proc.stdout,flush=True)
            fail('READ-ONLY AUDIT did not fully finish; BOT REMAINS STOPPED')
        coverage='ISOLATED' if 'UPGRADE AUDIT COMPLETE ISOLATED' in proc.stdout.splitlines() else 'STRICT'
        print('POSITION COVERAGE PASS',coverage,flush=True)
        print('FINANCIAL AUDIT PASS',flush=True)
        print('AUDIT PASS',bot,flush=True)
    except BaseException:
        if audit_started:
            # GET-only audit can persist new exchange facts and fill evidence.
            # Never restore an old state over those facts after an audit starts.
            print('AUDIT FAILED; CANDIDATE AND CURRENT STATE RETAINED; SERVICE STOPPED; backup',dest,flush=True)
            raise
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
    ap.add_argument('action',choices=('preflight','shared','funding-view','telegram','bot','status'))
    ap.add_argument('--bot',required=True,help='Explicit individual instance name (one only)')
    ap.add_argument('--package',type=Path,required=True,help='Pre-reviewed candidate ZIP')
    ap.add_argument('--stage',type=Path,required=True,help='Already unpacked identical ZIP contents')
    ap.add_argument('--sha256',required=True,help='Exact 64-hex reviewed ZIP SHA256')
    ap.add_argument('--from-engine',required=True,
                    help='Explicit currently installed engine identity; must have trusted manifest SHA256')
    ap.add_argument('--confirm-live-stop',action='store_true',
                    help='Acknowledge stop leaves exchange orders LIVE; required for bot')
    ap.add_argument('--single-instance',action='store_true',
                    help='Canary: no shared install; allow bounded attached TP reservation audit')
    args=ap.parse_args()
    if args.single_instance and args.action not in ('preflight','bot'):
        fail('Single-instance mode is only for preflight or bot')
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
    if not isinstance(NEW_VERSION,str):fail('Invalid target engine identity')
    from okx_manager_version import RELEASE_KIND, validate_manager_version
    if RELEASE_KIND=='maintenance':
        if args.action=='bot' or args.single_instance:
            fail('Maintenance release forbids trading bot upgrade/restart')
        if OLD_VERSION!=NEW_VERSION or not validate_manager_version():
            fail('Maintenance requires identical source and target engine identities')
    elif OLD_VERSION==NEW_VERSION:
        fail('Same-engine installation requires a declared maintenance release')
    old_entry=expected.get('engines',{}).get(OLD_VERSION)
    if not isinstance(old_entry,dict) or old_entry.get('file')!='okx_bot.py':
        fail('Source engine identity not attested in target package manifest')
    OLD_ENGINE_SHA256=old_entry.get('sha256')
    if not isinstance(OLD_ENGINE_SHA256,str) or not re.fullmatch(r'[0-9a-f]{64}',OLD_ENGINE_SHA256):
        fail('Source engine lacks exact published SHA256 identity')
    fd=os.open(LOCK,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='bot' and not args.confirm_live_stop:
            fail('Refuse to stop trading bot without --confirm-live-stop')
        if RELEASE_KIND=='engine' and args.action in ('preflight','shared','bot'):
            # Attest ZIP/stage before interpreting any new migration requirement.
            stage_check()
            verify_quota_boot_barriers()
            if args.action=='bot':verify_telegram_after_shared()
        if args.action=='preflight':
            verify_staging()
            if args.single_instance:
                root,_,_=instance_check(args.bot,OLD_VERSION,need_active=True)
                verify_single_instance_sources(root)
                stop_safety_readonly(root,json_read(root/'state/bot_state.json'))
                print('SINGLE INSTANCE PREFLIGHT PASS; NO SHARED WRITES')
        elif args.action=='shared':install_shared()
        elif args.action=='funding-view':install_funding_view()
        elif args.action=='telegram':install_telegram()
        elif args.action=='bot':
            upgrade_bot(args.bot,single_instance=args.single_instance)
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
