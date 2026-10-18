#!/usr/bin/env python3
"""OKX Grid Bot 0.7.14: rollout package, manifest and read-only migration plan.

Builds self-contained manager bundle from verified candidate. Does NOT install
it into /opt or alter /usr/local/bin/okxbot. Those actions require separate
LIVE authorization. The bot-side rollout also remains deliberately DISABLED:
actual engine stop, SQLite snapshot, unit swap, exchange reconciliation and
observer unit migration must be orchestrated as ONE reviewed operation.

Legacy compatibility modules may retain historic filenames INSIDE the bundle;
all 0.7.14 primary runtime components have versionless filenames.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path('/root/okx-v0714-work/candidate')
TARGET='/opt/okx-grid-live'
MANAGER='/usr/local/bin/okxbot'
EXPECTED_VERSION='0.7.14'
CORE=(
    'okxbot.py','okx_ui.py','okx_setup.py','okx_bot.py',
    'okx_latency.py','okx_positions_fifo.py','okx_emergency_gate.py',
    'okx_fill_singleflight.py','okx_terminal_accounting.py',
    'okx_execution_observer.py','okx_monitor.py','okx_observers.py',
    'okx_panic_cleanup.py','okx_instance_discovery.py',
    'okx_rollout_plan.py','okx_snapshot.py',
    'okx_rollout_transaction.py','okx_rollout_fencing.py',
)
MANIFEST='engine_manifest.json'
EXTRA=('requirements.txt',)  # Required by new-bot wizard and clone

class DeploymentBlocked(RuntimeError):
    pass


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as fp:
        for block in iter(lambda:fp.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def _read_object(path):
    if path.is_symlink() or not path.is_file():
        raise DeploymentBlocked('Missing or symlinked: '+str(path))
    try: result=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError) as exc:
        raise DeploymentBlocked('Invalid JSON: '+str(path)) from exc
    if not isinstance(result,dict):
        raise DeploymentBlocked('Invalid JSON object: '+str(path))
    return result


def _local_imports(source):
    mods=set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node,ast.Import):
            for alias in node.names:
                if alias.name.startswith('okx_') or alias.name=='okxbot':
                    mods.add(alias.name.split('.')[0])
        if isinstance(node,ast.ImportFrom) and node.module:
            if node.module.startswith('okx_') or node.module=='okxbot':
                mods.add(node.module.split('.')[0])
    return mods


def dependency_closure(source):
    """Recursively package candidate's imported local modules, incl legacy adapters.

    Unknown `okx_*` imports fail closed; no glob-copy of historical engines.
    """
    source=Path(source)
    if not source.is_dir() or source.is_symlink():
        raise DeploymentBlocked('Invalid candidate directory')
    manifest=_read_object(source/MANIFEST)
    if manifest.get('current_version')!=EXPECTED_VERSION:
        raise DeploymentBlocked('Candidate manifest version mismatch')
    engine=manifest.get('engines',{}).get(EXPECTED_VERSION,{})
    if engine.get('file')!='okx_bot.py':
        raise DeploymentBlocked('Not a versionless engine manifest')
    roots=set(CORE)
    roots.update(engine.get('dependencies',[]))
    if not isinstance(engine.get('dependencies'),list):
        raise DeploymentBlocked('Malformed dependency list')
    queue=list(sorted(roots))
    visited=set()
    while queue:
        name=queue.pop()
        if name in visited:continue
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*\.py',name):
            raise DeploymentBlocked('Unsafe local module name: '+repr(name))
        path=source/name
        if path.is_symlink() or not path.is_file():
            raise DeploymentBlocked('Missing local import: '+name)
        code=path.read_text(encoding='utf-8')
        compile(code,str(path),'exec')
        visited.add(name)
        for dep in _local_imports(code):
            depfile=dep+'.py'
            if depfile not in visited:queue.append(depfile)
    # Important: stable manager's legacy monitor adapter imports various
    # legacy Python modules; include ONLY its discovered transitive imports.
    return tuple(sorted(visited))


def audit_bundle(source):
    modules=dependency_closure(source)
    manifest=_read_object(Path(source)/MANIFEST)
    if len(modules)<len(CORE):
        raise DeploymentBlocked('Incomplete manager bundle')
    engine=(Path(source)/'okx_bot.py').read_text(encoding='utf-8')
    if not re.search(r'(?m)^VER\s*=\s*[\'\"]0\.7\.14[\'\"]',engine):
        raise DeploymentBlocked('Internal engine version mismatch')
    if 'MAX_BYTES = 256 * 1024 * 1024' not in (Path(source)/'okx_latency.py').read_text():
        raise DeploymentBlocked('Latency rotation 256 MiB is missing')
    if manifest['engines'][EXPECTED_VERSION]['file']!='okx_bot.py':
        raise DeploymentBlocked('Wrong engine file')
    for name in EXTRA:
        fp=Path(source)/name
        if fp.is_symlink() or not fp.is_file():
            raise DeploymentBlocked('Required non-Python runtime asset missing: '+name)
    files=(MANIFEST,)+EXTRA+modules
    return [(name,_sha(Path(source)/name)) for name in files]


def make_bundle(source, destination):
    """Create privately in isolated candidate path; never overwrite anything."""
    source=Path(source);destination=Path(destination)
    if destination.exists() or destination.is_symlink():
        raise DeploymentBlocked('Destination already exists')
    if not destination.is_absolute() or not destination.parent.is_dir():
        raise DeploymentBlocked('Destination parent missing')
    # Package rehearsal strictly within the candidate tree, NOT /opt, LIVE.
    if source.resolve()!=ROOT or not destination.is_relative_to(ROOT):
        raise DeploymentBlocked('Only isolated candidate rehearsal is permitted')
    audit=audit_bundle(source)
    temp=Path(tempfile.mkdtemp(prefix='.package-0714-',dir=destination.parent))
    os.chmod(temp,0o700)
    try:
        for name,sha in audit:
            f=temp/name
            mode=0o700 if name=='okxbot.py' else 0o600
            fd=os.open(f,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode)
            with os.fdopen(fd,'wb') as out, (source/name).open('rb') as inp:
                shutil.copyfileobj(inp,out)
                out.flush();os.fsync(out.fileno())
            if _sha(f)!=sha:raise DeploymentBlocked('Copy SHA changed: '+name)
        report={
            'release':EXPECTED_VERSION,
            'manager_entry':'okxbot.py',
            'primary_engine':'okx_bot.py',
            'target_directory':TARGET,
            'manager_symlink':MANAGER,
            'files':{name:sha for name,sha in audit},
            'installation_authorized':False,
            'trading_upgrade_authorized':False,
        }
        doc=temp/'BUNDLE_SHA256.json'
        fd=os.open(doc,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(report,out,sort_keys=True,indent=2)
            out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,destination)
    except BaseException:
        shutil.rmtree(temp,ignore_errors=True)
        raise
    return report


def verify_bundle(path):
    path=Path(path)
    doc=_read_object(path/'BUNDLE_SHA256.json')
    if doc.get('release')!=EXPECTED_VERSION or doc.get('installation_authorized') is not False:
        raise DeploymentBlocked('Not a staged candidate bundle')
    for name,digest in doc.get('files',{}).items():
        if Path(name).name!=name or _sha(path/name)!=digest:
            raise DeploymentBlocked('Package integrity mismatch: '+name)
    return doc


def manager_layout(install_dir=Path(TARGET),manager=Path(MANAGER)):
    """Read-only intended permanent manager layout; no symlink manipulation."""
    return {
        'install_dir':str(install_dir),
        'manager_entry':str(install_dir/'okxbot.py'),
        'command':str(manager),
        'existing_command':str(manager.resolve(strict=False)) if manager.exists() or manager.is_symlink() else '<absent>',
        'existing_install': 'exists' if install_dir.exists() else 'absent',
    }


def offline_unit_rehearsal(source):
    """Plan 0.7.12 -> 0.7.14 ExecStart and ExecStopPost, on fixture ONLY."""
    if str(source) not in sys.path:sys.path.insert(0,str(source))
    from okx_rollout_plan import plan_unit_rewrite
    bot=Path('/root/okx/test0714')
    unit=(
        '[Unit]\nDescription=OKX Grid Bot (test0714)\n'
        '[Service]\nType=simple\nUser=root\n'
        'WorkingDirectory=/root/okx/test0714\n'
        'Environment=OKX_BOT04_LIVE=CONFIRM\n'
        'ExecStart=/root/okx/test0714/.venv/bin/python -u /root/okx/test0714/okx_bot0712.py\n'
        'ExecStopPost=/usr/bin/python3 -B /opt/okx-grid-v0712-live/okx_panic_cleanup0712.py --root /root/okx/test0714 --service okx-grid-test0714\n'
        'Restart=on-failure\n'
    )
    rewrite=plan_unit_rewrite(unit,bot,'okx-grid-test0714')
    if ('/root/okx/test0714/okx_bot.py' not in rewrite or
        '/opt/okx-grid-live/okx_panic_cleanup.py' not in rewrite):
        raise DeploymentBlocked('Rehearsal of unit migration failed')
    for line in unit.splitlines():
        if not line.startswith(('ExecStart=','ExecStopPost=')) and line not in rewrite.splitlines():
            raise DeploymentBlocked('Unit migration changed unrelated directive')
    return rewrite


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',action='store_true',help='Build candidate-only package')
    parser.add_argument('--audit',action='store_true',help='Read-only candidate package audit')
    args=parser.parse_args()
    audit=audit_bundle(ROOT)
    offline_unit_rehearsal(ROOT)
    print('CANDIDATE: v0.7.14 manager+engine verified; modules:',len(audit))
    print('INSTALL PLAN:',json.dumps(manager_layout(),ensure_ascii=False))
    print('ROLLING UPGRADE: individual approval per bot; LIVE apply not implemented here')
    if args.package:
        package=ROOT/'.v0714-stage12-package'
        report=make_bundle(ROOT,package)
        verify_bundle(package)
        print('OFFLINE BUNDLE:',package,'modules:',len(report['files'])-1)
    else:
        print('Read-only audit OK. Use --package to build OFFLINE candidate package.')
    print('NO LIVE changes, systemd, SQLite, registry or exchange requests')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('BLOCKED:',type(exc).__name__,exc,file=sys.stderr)
        raise SystemExit(2)
