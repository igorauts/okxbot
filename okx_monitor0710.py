#!/usr/bin/env python3
"""Enable per-instance LOCAL READ-ONLY v0.7.10 monitors without restarting trading bots."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

SERVICE_RE = re.compile(r'^okx-grid-[a-z0-9-]+$')
KIND = ('burst','execution')


def monitor_unit(service, kind):
    if not SERVICE_RE.fullmatch(service) or kind not in KIND:
        raise ValueError('Invalid bot service or monitor kind')
    # IMPORTANT: unit does NOT begin with okx! Manager old collision guard scans okx*.service.
    return f'observer-{service}-{kind}-v0710.service'


def running(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit],capture_output=True).returncode == 0


def unit_text(manager, root, service, kind):
    source = manager/'okx_execution_observer0710.py'
    if not source.is_file():
        raise RuntimeError('Observer script not installed')
    return (f'[Unit]\nDescription=Read-only OKX v0.7.10 {kind} monitor for {service}\n'
            'After=network-online.target\n\n[Service]\nType=simple\nUser=root\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\n'
            f'WorkingDirectory={manager}\n'
            f'ExecStart=/usr/bin/python3 -B {source} --root {root} --service {service} --mode {kind}\n'
            'Restart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=true\n'
            'Nice=10\nIOSchedulingClass=idle\nCPUQuota=5%\nMemoryMax=96M\n'
            'ProtectSystem=strict\n'
            f'ReadWritePaths={root}/diagnostics\n'
            f'ReadOnlyPaths={root}\n'
            'ProtectHome=read-only\n'
            'IPAddressDeny=any\n'
            '[Install]\nWantedBy=multi-user.target\n')


def main():
    a=argparse.ArgumentParser()
    a.add_argument('--root',required=True,type=Path)
    a.add_argument('--service',required=True)
    a.add_argument('--kind',required=True,choices=KIND)
    a.add_argument('action',choices=('on','off','status','report'))
    args=a.parse_args()
    root=args.root.resolve(strict=True)
    service=args.service
    kind=args.kind
    unit=monitor_unit(service,kind)
    manager=Path(__file__).resolve().parent
    unit_path=Path('/etc/systemd/system')/unit
    if args.action=='status':
        print(unit,'ON' if running(unit) else 'OFF')
        return
    if args.action=='report':
        subprocess.run(['/usr/bin/python3','-B',str(manager/'okx_execution_observer0710.py'),
                        '--root',str(root),'--mode',kind,'--report'],check=True)
        return
    if os.geteuid()!=0:
        raise RuntimeError('Root is required')
    if args.action=='off':
        subprocess.run(['systemctl','disable','--now',unit],check=True)
        print('Observer OFF; trading bot untouched:',service)
        return
    if not root.is_relative_to(Path('/root/okx')) or not (root/'config.json').is_file():
        raise RuntimeError('Unexpected bot directory')
    if not running(service+'.service'):
        raise RuntimeError('Trading bot service not active')
    cfg=json.loads((root/'config.json').read_text())
    st=json.loads((root/'state/bot04_state.json').read_text())
    if st.get('version') != '0.7.10' or st.get('instrument')!=cfg.get('instrument'):
        raise RuntimeError('Invalid bot version/state/instrument for observer v0.7.10')
    info=subprocess.run(['systemctl','show',service+'.service','-p','ExecStart','--value'],
                        capture_output=True,text=True,check=True)
    if not re.search(r'\bokx_bot0710\.py\b',info.stdout):
        raise RuntimeError('Trading service does not run v0.7.7/v0.7.10 engine')
    # Make sure service belongs to this instance root.
    unit_show=subprocess.run(['systemctl','show',service+'.service','-p','WorkingDirectory','--value'],
                             capture_output=True,text=True,check=True)
    if Path(unit_show.stdout.strip()).resolve()!=root:
        raise RuntimeError('Trading service WorkingDirectory mismatch')
    desired=unit_text(manager,root,service,kind)
    if unit_path.exists() and unit_path.read_text()!=desired:
        raise RuntimeError('Existing observer unit differs; refusing overwrite')
    if not unit_path.exists():
        out=root/'diagnostics'
        out.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(unit_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644)
        with os.fdopen(fd,'w') as f:
            f.write(desired)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',unit],check=True)
    if not running(unit):
        raise RuntimeError('Observer failed to start; inspect journalctl -u '+unit)
    print('PASS observer ON:',unit,'no trading restart/no OKX API')


if __name__=='__main__':
    main()
