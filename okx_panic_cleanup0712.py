#!/usr/bin/env python3
"""v0.7.12 systemd ExecStopPost: local-only, fail-closed PANIC_DONE cleanup.

Runs after a newly created trading instance EXITS. Not a trading component;
never sends OKX requests. All decision gates are local, synchronous and
idempotent. Exit 0 on cleanup failure to avoid triggering systemd Restart=on-failure
for a retired trading service; failures are visible in journald.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BOT_ROOT = Path('/root/okx')
UNIT_DIR = Path('/etc/systemd/system')
SERVICE_RE = re.compile(r'^okx-grid-[a-z0-9-]+$')


def load_json(path):
    raw=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw,dict):
        raise ValueError(f'Expected JSON object: {path.name}')
    return raw


def confirmed_panic(root):
    """No API; require independently persisted terminal state, ack and config."""
    try:
        cfg=load_json(root/'config.json')
        st=load_json(root/'state'/'bot04_state.json')
        ack=load_json(root/'state'/'control_ack.json')
        control=load_json(root/'state'/'control.json')
    except (OSError,ValueError,TypeError) as exc:
        print(f'PANIC CLEANUP SKIP: state unavailable ({type(exc).__name__})',flush=True)
        return False
    if not (cfg.get('version')=='0.7.12' and
            st.get('version')=='0.7.12' and
            cfg.get('instrument')==st.get('instrument') and
            control.get('mode')=='PANIC' and
            st.get('terminal_mode')=='PANIC_DONE' and
            ack.get('mode')=='PANIC' and
            ack.get('phase')=='PANIC_DONE' and
            str(ack.get('remaining_position'))=='0' and
            str((st.get('terminal_snapshot') or {}).get('position_contracts'))=='0' and
            (st.get('terminal_snapshot') or {}).get('mode')=='PANIC_DONE'):
        print('PANIC CLEANUP SKIP: PANIC_DONE not consistently confirmed',flush=True)
        return False
    return True


def call_systemctl(*args):
    result=subprocess.run(['systemctl',*args],capture_output=True,text=True,timeout=20)
    if result.returncode:
        raise RuntimeError(f'systemctl {" ".join(args)}: {result.stderr[:350]}')


def service_root_matches(root, service):
    result=subprocess.run(['systemctl','show',service+'.service',
                           '--property=WorkingDirectory','--value'],
                          capture_output=True,text=True,timeout=10)
    return result.returncode==0 and Path(result.stdout.strip()).resolve()==root


def cleanup(root,service,unit_dir=UNIT_DIR):
    root=Path(root).resolve()
    if (not SERVICE_RE.fullmatch(service) or
        root.parent!=BOT_ROOT or
        service != 'okx-grid-'+root.name):
        raise ValueError('Refuse cleanup: service/root identity mismatch')
    if not service_root_matches(root,service):
        raise RuntimeError('Refuse cleanup: systemd WorkingDirectory mismatch')
    if not confirmed_panic(root):
        return False
    failures=[]
    found=[]
    # Strict list for our managed observers. Never touch a global Telegram unit
    # or a different trading instance. No wildcard systemctl commands.
    for version in ('v077','v078','v079','v0710','v0711','v0712'):
        for kind in ('burst','execution'):
            unit=f'observer-{service}-{kind}-{version}.service'
            unit_file=unit_dir/unit
            if not unit_file.is_file() or unit_file.is_symlink():
                continue
            content=unit_file.read_text(encoding='utf-8')
            if (f'--root {root}' not in content or
                f'--service {service}' not in content or
                'okx_execution_observer' not in content):
                failures.append(f'{unit}: identity/ownership check failed')
                continue
            found.append(unit)
            try:
                call_systemctl('disable','--now',unit)
                print('PANIC CLEANUP observer DISABLED:',unit,flush=True)
            except (RuntimeError,subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
    try:
        # Disable only; the engine is already exiting. Never --now self here.
        call_systemctl('disable','--no-reload',service+'.service')
        print('PANIC CLEANUP bot AUTOSTART DISABLED:',service,flush=True)
    except (RuntimeError,subprocess.TimeoutExpired) as exc:
        failures.append(str(exc))
    if failures:
        for err in failures:print('PANIC CLEANUP WARNING:',err,flush=True)
        return False
    print('PANIC CLEANUP PASS:',service,'observers=',len(found),flush=True)
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--service',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:
        print('PANIC CLEANUP WARNING: expected systemd root execution',flush=True)
        return
    try:
        cleanup(args.root,args.service)
    except Exception as exc:
        # Exit 0: a failure in post-exit housekeeping must never cause a
        # terminal bot to start again due to Restart=on-failure.
        print('PANIC CLEANUP WARNING:',type(exc).__name__,str(exc)[:300],flush=True)

if __name__=='__main__':
    main()
