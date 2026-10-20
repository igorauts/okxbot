#!/usr/bin/env python3
from okx_runtime_paths import state_file
"""Manage optional per-instance read-only burst/execution observers.

Never imports the trading engine, touches credentials or restarts trading services.
No monitor service is created until explicitly enabled by the operator.
"""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

KIND=('burst','execution')
SAFE_UNIT=re.compile(r'^okx-grid-[a-z0-9-]+$')

def monitor_unit(trading_service,kind):
    if not SAFE_UNIT.fullmatch(trading_service) or kind not in KIND:
        raise ValueError('Invalid service or monitor kind')
    # Unique name for the selected bot and selected monitor.
    return trading_service+'-'+kind+'-monitor074.service'

def unit_text(root,trading_service,kind,observer):
    if not root.is_absolute() or not root.is_dir() or not (root/'config.json').is_file():
        raise ValueError('Missing absolute bot instance directory/config')
    if any(ch.isspace() for ch in str(root)) or not str(root).isascii():
        raise ValueError('Unsafe bot path')
    if not observer.is_absolute() or not observer.is_file():
        raise ValueError('Observer source missing')
    mode=kind
    return (f'[Unit]\nDescription=OKX {kind} observer for {trading_service} (READ ONLY)\n'
            'After=network-online.target\n\n'
            '[Service]\nType=simple\nUser=root\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\n'
            f'WorkingDirectory={root}\n'
            f'ExecStart=/usr/bin/python3 -B {observer} --root {root} --service {trading_service} --mode {mode}\n'
            'Restart=on-failure\nRestartSec=5\n'
            'UMask=0077\nNoNewPrivileges=true\nProtectSystem=strict\n'
            f'ReadWritePaths={root}/diagnostics\n'
            f'ReadOnlyPaths={root}\n'
            'ProtectHome=read-only\n'
            '[Install]\nWantedBy=multi-user.target\n')

def is_active(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit],check=False).returncode==0

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--service',required=True,help='Existing trading service without .service suffix')
    p.add_argument('--kind',choices=KIND,required=True)
    p.add_argument('action',choices=('on','off','status','report'))
    p.add_argument('--units-dir',type=Path,default=Path('/etc/systemd/system'))
    a=p.parse_args()
    root=a.root.resolve(strict=True)
    unit=monitor_unit(a.service,a.kind)
    target=a.units_dir/unit
    out=root/'diagnostics'/(a.kind+'_v074')
    observer=Path(__file__).resolve().with_name('okx_execution_observer074.py')
    if a.action=='status':
        print(unit,'ON' if is_active(unit) else 'OFF');return
    if a.action=='report':
        subprocess.run(['/usr/bin/python3','-B',str(observer),'--root',str(root),'--mode',a.kind,'--report'],check=True);return
    if os.geteuid()!=0:raise PermissionError('Root privileges required for systemd')
    # Observe only explicitly chosen existing trading service, never start it.
    if not (root/'config.json').is_file(): raise ValueError('Missing instance config')
    if a.action=='off':
        subprocess.run(['systemctl','disable','--now',unit],check=False)
        print('Observer disabled. Trading service untouched:',a.service)
        return
    # Require v0.7.4, but never alter the engine or its state.
    from json import JSONDecodeError
    try:
        config=json.loads((root/'config.json').read_text())
        st=json.loads((state_file(root)).read_text())
        if st.get('version')!='0.7.4' or st.get('instrument')!=config.get('instrument'):
            raise RuntimeError('Observer needs running v0.7.4 state matching config')
    except (OSError,JSONDecodeError) as exc:
        raise RuntimeError('Cannot read valid trading state') from exc
    if not is_active(a.service+'.service'):
        raise RuntimeError('Selected trading service must already be running. No action.')
    if target.exists():
        if target.read_text()!=unit_text(root,a.service,a.kind,observer):
            raise RuntimeError('Existing observer unit differs; refusing to overwrite')
    else:
        # Only add a new service: no overwrite, no trading reload.
        out.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(out,0o700)
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
        with os.fdopen(fd,'w') as f:f.write(unit_text(root,a.service,a.kind,observer))
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',unit],check=True)
    print('Read-only observer ON:',unit,'trading untouched:',a.service)

if __name__=='__main__': main()
