"""Read-only independent PANIC ALL discovery, no account orders/control writes.

A faulty but IDENTIFIABLE, separately-instrumented service cannot veto the
PANIC intent of another verified service. Unknown NET ownership can veto all.
The discovery is repeated before confirmation and re-checked during dispatch.
"""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path

SERVICE = re.compile(r'okx-grid-[a-z0-9-]+\.service\Z')
PAIR = re.compile(r'[A-Z0-9]+-USDT-SWAP\Z')
TRANSITIONAL = {'activating', 'deactivating', 'reloading'}


def read_object(path):
    path=Path(path)
    if path.is_symlink():raise RuntimeError('Unsafe symlink: '+str(path))
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise RuntimeError('Not a JSON object: '+str(path))
    return value


def _properties(unit):
    proc=subprocess.run(['systemctl','show',unit,'--no-pager',
                         '-p','ActiveState','-p','MainPID','-p','WorkingDirectory',
                         '-p','ExecStart'],capture_output=True,text=True,timeout=10,check=True)
    return dict(s.split('=',1) for s in proc.stdout.splitlines() if '=' in s)


def survey(base,registry_path,systemd_dir,validate=None):
    """Return (ready, skipped, host-identity). Fail on unknown pair ownership.

    ready rows: (name,instrument,unit,pid,mode).
    Skip records are readable notes; never imply a PANIC was delivered.
    """
    base=Path(base).resolve(strict=True)
    records=read_object(registry_path)
    p=subprocess.run(['systemctl','list-units','--all','--type=service',
        '--no-legend','--plain','--no-pager','okx-grid-*.service','okx-bot*.service'],
        capture_output=True,text=True,timeout=15,check=True)
    units={}
    for line in p.stdout.splitlines():
        parts=line.lstrip('● ').split()
        if len(parts)>=3 and (SERVICE.fullmatch(parts[0]) or parts[0].startswith('okx-bot')):
            if parts[0] in units:raise RuntimeError('Duplicate systemd unit')
            units[parts[0]]=parts[2]
    live={name:state for name,state in units.items()
          if state=='active' or state in TRANSITIONAL}
    ready=[];skipped=[];seen_pairs={};identity=[]
    # Guard all ACTIVE services, including unregistered ones. Never silently
    # assume an unknown running service is on another NET instrument.
    for unit in sorted(live):
        if not SERVICE.fullmatch(unit):
            raise RuntimeError('Unknown active legacy trading unit: '+unit)
        props=_properties(unit)
        state=props.get('ActiveState')
        rawroot=Path(props.get('WorkingDirectory') or '/')
        if (rawroot.is_symlink() or not rawroot.is_dir()
                or rawroot.resolve()!=base/unit.removeprefix('okx-grid-').removesuffix('.service')):
            raise RuntimeError('Unknown active instrument/unsafe root: '+unit)
        root=rawroot.resolve()
        try:
            cfg=read_object(root/'config.json')
            inst=cfg.get('instrument')
            if not isinstance(inst,str) or not PAIR.fullmatch(inst):
                raise ValueError('Invalid instrument')
        except (OSError, ValueError, RuntimeError):
            raise RuntimeError('Cannot prove instrument of active service: '+unit)
        if inst in seen_pairs:
            raise RuntimeError('Duplicate NET instrument on running services: '+inst)
        seen_pairs[inst]=unit
        pid=str(props.get('MainPID',''))
        # Include all independent service identities, not just candidates.
        identity.append((unit,str(state),pid,inst))
        matches=[(name,entry) for name,entry in records.items()
            if isinstance(entry,dict) and entry.get('service')==unit.removesuffix('.service')]
        if len(matches)!=1:
            skipped.append((unit,'missing/duplicate valid registry entry'))
            continue
        name,entry=matches[0]
        if not isinstance(name,str) or entry.get('path')!=str(root):
            skipped.append((unit,'registry path/identity mismatch'));continue
        if state!='active' or not pid.isdecimal() or int(pid)<=0:
            skipped.append((unit,'not stably active'));continue
        try:
            st=read_object(root/'state/bot_state.json')
            if (st.get('schema_version')!=1 or st.get('version')!=cfg.get('version')
                    or st.get('instrument')!=inst):
                raise ValueError('config/state version/identity mismatch')
            control_file=root/'state/control.json'
            control=read_object(control_file) if control_file.exists() else {'mode':'RUN'}
            mode=control.get('mode')
            ack_path=root/'state/control_ack.json'
            ack=read_object(ack_path) if ack_path.exists() else {}
            if mode in ('PANIC','HARD') or ack.get('phase') in ('PANIC_DONE','RETIRED'):
                skipped.append((unit,'already in emergency/terminal'));continue
            if mode not in ('RUN','PAUSE','SOFT') or entry.get('retired'):
                raise ValueError('unsafe control mode/retired')
            env=root/'.env'
            if env.is_symlink() or not env.is_file():
                raise ValueError('missing/unsafe credentials file')
            keys=[line.split('=',1)[1].strip() for line in env.read_text().splitlines()
                  if line.strip().startswith('OKX_API_KEY=')]
            if len(keys)!=1 or not keys[0]:raise ValueError('missing/duplicate credentials field')
            if (str(root/'okx_bot.py') not in props.get('ExecStart','')
                    or str(root/'.venv/bin/python') not in props.get('ExecStart','')):
                raise ValueError('engine ExecStart mismatch')
            if validate is not None and not validate(entry):
                raise ValueError('local engine control not supported')
        except (OSError, ValueError, RuntimeError) as exc:
            skipped.append((unit, str(exc)[:140]));continue
        ready.append((name,inst,unit,int(pid),mode))
    # Explicitly report known registry entries whose unit is absent/inactive.
    for name,entry in records.items():
        if isinstance(entry,dict):
            unit=str(entry.get('service',''))+'.service'
            if unit not in live:
                skipped.append((unit,'inactive/absent'))
        else:
            skipped.append((str(name),'malformed registry entry'))
    if not ready:
        raise RuntimeError('No independently verified bots eligible for PANIC ALL; skipped: '+str(skipped)[:600])
    return tuple(sorted(ready)),tuple(sorted(set(skipped))),tuple(sorted(identity))
