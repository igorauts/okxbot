#!/usr/bin/env python3
"""One observer pair per active bot; never starts trading services."""
from __future__ import annotations
from okx_version import VERSION
import json
import os
from pathlib import Path
import re
import subprocess
import sys

KINDS=('burst','execution')
SERVICE_RE=re.compile(r'okx-grid-[a-z0-9-]+\Z')


def unit_name(service,kind):
    if not SERVICE_RE.fullmatch(service) or kind not in KINDS:
        raise ValueError('Invalid OKX bot service / observer kind')
    return f'observer-{service}-{kind}.service'


def validate_root(root,service,base=Path('/root/okx')):
    root=Path(root)
    base=Path(base).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('Observer root missing or unsafe')
    resolved=root.resolve(strict=True)
    if resolved != base/service.removeprefix('okx-grid-'):
        raise RuntimeError('Observer root/service identity mismatch')
    cfg_file=root/'config.json'
    if not cfg_file.is_file() or cfg_file.is_symlink():
        raise RuntimeError('Observer config missing or unsafe')
    cfg=json.loads(cfg_file.read_text(encoding='utf-8'))
    if not isinstance(cfg,dict) or not isinstance(cfg.get('version'),str):
        raise RuntimeError('Invalid observer config')
    return resolved,cfg


def supports_versionless(root, service, base=Path('/root/okx')):
    resolved,cfg=validate_root(root,service,base)
    ver=cfg['version']
    if ver != VERSION:
        return False
    engine=resolved/'okx_bot.py'
    return engine.is_file() and not engine.is_symlink()


def unit_text(root,service,kind,source):
    name=unit_name(service,kind)
    root=Path(root)
    source=Path(source)
    if any(ch.isspace() for ch in str(root)) or not str(root).isascii():
        raise RuntimeError('Unsafe root path in observer unit')
    if any(ch.isspace() for ch in str(source)) or not str(source).isascii():
        raise RuntimeError('Unsafe source path in observer unit')
    script=source/'okx_execution_observer.py'
    if not script.is_file() or script.is_symlink():
        raise RuntimeError('Observer module missing or unsafe')
    return (f'[Unit]\nDescription=Read-only OKX {kind} observer ({service})\n'
            'After=network-online.target '+service+'.service\n'
            'PartOf='+service+'.service\n'
            'ConditionPathExists='+str(root/'config.json')+'\n\n'
            '[Service]\nType=simple\nUser=root\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\n'
            f'WorkingDirectory={source}\n'
            f'ExecStart=/usr/bin/python3 -B {script} --root {root} --service {service} --mode {kind}\n'
            'Restart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=true\n'
            'Nice=10\nIOSchedulingClass=idle\nCPUQuota=5%\nMemoryMax=96M\n'
            'ProtectSystem=strict\n'
            f'ReadWritePaths={root}/diagnostics\n'
            f'ReadOnlyPaths={root}\n'
            'ProtectHome=read-only\nIPAddressDeny=any\n\n'
            '[Install]\nWantedBy='+service+'.service\n')


def _run(args):
    subprocess.run(args,check=True,timeout=45,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)


def props(unit):
    result=subprocess.run(['systemctl','show',unit,'--no-pager',
        '-p','LoadState','-p','ActiveState','-p','UnitFileState'],
        capture_output=True,text=True,timeout=10,check=True)
    return dict(x.split('=',1) for x in result.stdout.splitlines() if '=' in x)


def _collision_guard(service,kind,systemd_dir,active_fn):
    expected=unit_name(service,kind)
    # Includes unknown externally installed observer units for this bot/kind.
    for item in Path(systemd_dir).glob('observer-'+service+'-'+kind+'*.service'):
        if item.name!=expected and active_fn(item.name):
            raise RuntimeError('Duplicate active observer: '+item.name)


def provision_observers(root,service,source,systemd_dir=Path('/etc/systemd/system'),
                        *,base=Path('/root/okx'),run=_run,active_fn=None):
    """Create+ENABLE observers for new bot, NEVER start its trading service.

    On incomplete work, try to remove our newly created units and enable links.
    Does not overwrite or manipulate existing or unknown observer units.
    """
    if os.geteuid()!=0:
        raise RuntimeError('Root required to provision observer systemd units')
    root,cfg=validate_root(root,service,base)
    if not supports_versionless(root,service,base):
        raise RuntimeError('Observer provisioning requires the current engine')
    systemd_dir=Path(systemd_dir)
    if not systemd_dir.is_dir() or systemd_dir.is_symlink():
        raise RuntimeError('Unsafe systemd directory')
    if not (systemd_dir/(service+'.service')).is_file():
        raise RuntimeError('Trading service unit must exist before observer provisioning')
    if active_fn is None:
        active_fn=lambda unit: subprocess.run(
            ['systemctl','is-active','--quiet',unit],capture_output=True,timeout=8).returncode==0
    if active_fn(service+'.service'):
        raise RuntimeError('Provisioning only permitted before new trading service starts')
    # Validate entire plan BEFORE changes.
    planned=[]
    for kind in KINDS:
        _collision_guard(service,kind,systemd_dir,active_fn)
        unit=unit_name(service,kind)
        f=systemd_dir/unit
        text=unit_text(root,service,kind,source)
        if f.exists() or f.is_symlink() or (systemd_dir/(unit+'.d')).exists():
            raise RuntimeError('Existing observer unit requires explicit migration: '+unit)
        planned.append((unit,f,text))
    created=[]; enabled=[]
    try:
        (root/'diagnostics').mkdir(mode=0o700,parents=True,exist_ok=True)
        for unit,f,content in planned:
            fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
            created.append((unit,f))
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                out.write(content)
                out.flush();os.fsync(out.fileno())
        run(['systemctl','daemon-reload'])
        for unit,f in created:
            run(['systemctl','enable',unit])  # NOT '--now': NEVER start trading.
            enabled.append(unit)
        # Nothing started, these will start with trading unit because of WantedBy.
        return tuple(u for u,f in created)
    except BaseException:
        for unit,f in reversed(created):
            try:run(['systemctl','disable',unit])
            except Exception:pass
        for unit,f in created:
            try:f.unlink()
            except OSError:pass
        try:run(['systemctl','daemon-reload'])
        except Exception:pass
        raise


def monitor_action(root,service,kind,action,source,systemd_dir=Path('/etc/systemd/system'),
                   *,base=Path('/root/okx'),run=_run,active_fn=None,props_fn=props):
    root,cfg=validate_root(root,service,base)
    if not supports_versionless(root,service,base):
        raise RuntimeError('Not a versionless observer instance')
    unit=unit_name(service,kind)
    target=Path(systemd_dir)/unit
    if active_fn is None:
        active_fn=lambda n: subprocess.run(
            ['systemctl','is-active','--quiet',n],capture_output=True,timeout=8).returncode==0
    if action=='report':
        subprocess.run([sys.executable,'-B',str(Path(source)/'okx_execution_observer.py'),
                        '--root',str(root),'--mode',kind,'--report'],check=True,timeout=30)
        return
    if action not in ('on','off'):
        raise ValueError('Invalid observer command')
    _collision_guard(service,kind,systemd_dir,active_fn)
    if not target.is_file() or target.is_symlink():
        raise RuntimeError('Versionless observer unit not installed')
    expected=unit_text(root,service,kind,source)
    if target.read_text(encoding='utf-8')!=expected:
        raise RuntimeError('Observer unit differs from expected template; refusing modification')
    p=props_fn(unit)
    if p.get('LoadState')!='loaded':
        raise RuntimeError('Observer systemd unit is not loaded')
    if action=='off':
        run(['systemctl','disable','--now',unit])
        return
    # Only enable prepared monitor; never attempt to start trading service.
    trade_active=active_fn(service+'.service')
    if trade_active:
        run(['systemctl','enable','--now',unit])
    else:
        run(['systemctl','enable',unit])
