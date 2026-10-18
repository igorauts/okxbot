#!/usr/bin/env python3
"""v0.7.8 read-only monitor switch; preserve historical pre-078 monitors."""
import json
import subprocess
import sys
from pathlib import Path
import okx_monitor_compat074 as legacy
from okx_monitor078 import monitor_unit


def is_078(root):
    try:
        st=json.loads((Path(root)/'state/bot04_state.json').read_text(encoding='utf-8'))
        return st.get('version') in ('0.7.7','0.7.8')
    except (ValueError,OSError):
        return False


def monitor_state(service, root, kind):
    old=legacy.monitor_state(service,root,kind)
    old_077=f'observer-{service}-{kind}-v077.service'
    if legacy.service_active(old_077):
        old={'managed':(old_077,)+tuple(old['managed']),'external':old['external']}
    new=monitor_unit(service,kind)
    if legacy.service_active(new):
        return {'managed':(new,)+tuple(old['managed']), 'external':old['external']}
    return old


def label(service,root,kind):
    st=monitor_state(service,root,kind)
    if st['managed'] and st['external']:
        return 'ON (встроенный + внешний!)'
    if st['external']:
        return 'ON (внешний)'
    if st['managed']:
        return 'ON'
    return 'OFF'


def status_label(service,root,kind):
    v=label(service,root,kind)
    return {'ON':'ВКЛЮЧЁН','OFF':'ВЫКЛЮЧЕН',
            'ON (внешний)':'ВКЛЮЧЁН (внешний)',
            'ON (встроенный + внешний!)':'ВКЛЮЧЕНЫ ДВА МОНИТОРА!'}.get(v,v)


def monitor_action(service,root,kind,action,source):
    new=monitor_unit(service,kind)
    new_active=legacy.service_active(new)
    if action=='status':
        st=monitor_state(service,root,kind)
        print(kind, label(service,root,kind),'managed',st['managed'],'external',st['external'])
        return
    if action=='report' and new_active:
        subprocess.run([sys.executable,str(Path(source)/'okx_monitor078.py'),
                        '--root',str(root),'--service',service,'--kind',kind,'report'],check=True)
        return
    if action=='off' and new_active:
        subprocess.run([sys.executable,str(Path(source)/'okx_monitor078.py'),
                        '--root',str(root),'--service',service,'--kind',kind,'off'],check=True)
        return
    if action=='on' and is_078(root):
        st=monitor_state(service,root,kind)
        if st['external'] or any(x!=new for x in st['managed']):
            raise RuntimeError('Другой монитор уже работает: '+str(st))
        if new_active:
            print('Observer already ON:',new)
            return
        subprocess.run([sys.executable,str(Path(source)/'okx_monitor078.py'),
                        '--root',str(root),'--service',service,'--kind',kind,'on'],check=True)
        return
    return legacy.monitor_action(service,root,kind,action,source)


# V079_MONITOR_COMPAT_PATCH
# Extend v078 compatibility without changing historical observers.

from okx_monitor079 import monitor_unit as _unit079

_previous_monitor_state = monitor_state
_previous_monitor_action = monitor_action


def _is079(root):
    try:
        st = json.loads(
            (Path(root) / 'state/bot04_state.json').read_text()
        )
        return st.get('version') == '0.7.9'
    except (OSError, ValueError):
        return False


def monitor_state(service, root, kind):
    old = _previous_monitor_state(service, root, kind)
    unit = _unit079(service, kind)

    if legacy.service_active(unit):
        return {
            'managed': (unit,) + tuple(old['managed']),
            'external': old['external'],
        }

    return old


def monitor_action(service, root, kind, action, source):
    unit = _unit079(service, kind)
    active = legacy.service_active(unit)

    if action == 'status' and (_is079(root) or active):
        st = monitor_state(service, root, kind)
        print(kind, label(service, root, kind),
              'managed', st['managed'],
              'external', st['external'])
        return

    if action in ('off', 'report') and active:
        subprocess.run([
            sys.executable,
            str(Path(source) / 'okx_monitor079.py'),
            '--root', str(root),
            '--service', service,
            '--kind', kind,
            action
        ], check=True)
        return

    if action == 'on' and _is079(root):
        st = monitor_state(service, root, kind)

        if st['external'] or any(
            name != unit for name in st['managed']
        ):
            raise RuntimeError(
                'Another monitor is already active: ' + str(st)
            )

        if active:
            print('Observer already ON:', unit)
            return

        subprocess.run([
            sys.executable,
            str(Path(source) / 'okx_monitor079.py'),
            '--root', str(root),
            '--service', service,
            '--kind', kind,
            'on'
        ], check=True)
        return

    return _previous_monitor_action(
        service, root, kind, action, source
    )


# V0711_MONITOR_COMPAT: opt-in observer for NEW v0.7.11 bots only.
# Existing legacy observers and their controls stay unchanged.
from okx_monitor0711 import monitor_unit as _unit0711

_previous_monitor_state_0711 = monitor_state
_previous_monitor_action_0711 = monitor_action


def _is0711(root):
    try:
        st = json.loads((Path(root) / 'state/bot04_state.json').read_text())
        return st.get('version') == '0.7.11'
    except (OSError, ValueError, TypeError):
        return False


def monitor_state(service, root, kind):
    old = _previous_monitor_state_0711(service, root, kind)
    unit = _unit0711(service, kind)
    if legacy.service_active(unit):
        return {'managed': (unit,) + tuple(old['managed']), 'external': old['external']}
    return old


def monitor_action(service, root, kind, action, source):
    unit = _unit0711(service, kind)
    active = legacy.service_active(unit)
    if action == 'status' and (_is0711(root) or active):
        st = monitor_state(service, root, kind)
        print(kind, label(service, root, kind),
              'managed', st['managed'], 'external', st['external'])
        return
    if action in ('off', 'report') and active:
        subprocess.run([
            sys.executable, str(Path(source) / 'okx_monitor0711.py'),
            '--root', str(root), '--service', service,
            '--kind', kind, action
        ], check=True)
        return
    if action == 'on' and _is0711(root):
        st = monitor_state(service, root, kind)
        if st['external'] or any(x != unit for x in st['managed']):
            raise RuntimeError('Another monitor is already active: ' + str(st))
        if active:
            print('Observer already ON:', unit)
            return
        subprocess.run([
            sys.executable, str(Path(source) / 'okx_monitor0711.py'),
            '--root', str(root), '--service', service,
            '--kind', kind, 'on'
        ], check=True)
        return
    return _previous_monitor_action_0711(service, root, kind, action, source)

# OKX_MONITOR_STABLE_NAME_V0712_FIX_1
# v0.7.12 observers are managed by stable versionless systemd unit names.
# Preserve every old adapter for legacy bots, but do not let the old adapter
# generate a duplicate -v0711 observer on a v0.7.12 bot.
import re as _stable_re

_original_stable_monitor_state = monitor_state
_original_stable_monitor_action = monitor_action


def _stable0712(service, root, kind):
    root=Path(root).resolve()
    if kind not in ('burst','execution') or not _stable_re.fullmatch(r'okx-grid-[a-z0-9-]+',str(service)):
        raise ValueError('Invalid observer kind or trading service')
    try:
        st=json.loads((root/'state/bot04_state.json').read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):
        return None
    if st.get('version') != '0.7.12':
        return None
    # Never control any observer that might belong to a different trading bot.
    if root != Path('/root/okx')/service.removeprefix('okx-grid-'):
        raise RuntimeError('Stable observer root/service identity mismatch')
    return f'observer-{service}-{kind}.service'


def _stable0712_props(unit):
    r=subprocess.run(['systemctl','show',unit,'--no-pager',
            '-p','LoadState','-p','ActiveState','-p','ExecStart',
            '-p','WorkingDirectory'],capture_output=True,text=True,timeout=8,check=False)
    if r.returncode:
        raise RuntimeError('Cannot read observer service '+unit+': '+r.stderr.strip()[:150])
    return dict(x.split('=',1) for x in r.stdout.splitlines() if '=' in x)


def _stable0712_validate(service,root,kind,unit):
    p=Path('/etc/systemd/system')/unit
    if not p.is_file() or p.is_symlink():
        raise RuntimeError('Stable observer unit missing or unsafe: '+unit)
    props=_stable0712_props(unit)
    expected=('/opt/okx-grid-v0712-live/okx_execution_observer0711.py '
              f'--root {Path(root).resolve()} --service {service} --mode {kind}')
    if (props.get('LoadState')!='loaded' or
        props.get('WorkingDirectory')!='/opt/okx-grid-v0712-live' or
        expected not in props.get('ExecStart','')):
        raise RuntimeError('Stable observer unit is NOT the expected read-only v0.7.12 observer: '+unit)
    return props


def monitor_state(service,root,kind):
    previous=_original_stable_monitor_state(service,root,kind)
    unit=_stable0712(service,root,kind)
    if unit is not None and legacy.service_active(unit):
        return {'managed':(unit,)+tuple(x for x in previous['managed'] if x!=unit),
                'external':previous['external']}
    return previous


def monitor_action(service,root,kind,action,source):
    unit=_stable0712(service,root,kind)
    if unit is None:
        return _original_stable_monitor_action(service,root,kind,action,source)
    if action=='status':
        st=monitor_state(service,root,kind)
        print(kind,label(service,root,kind),'managed',st['managed'],'external',st['external'])
        return
    if action=='report':
        subprocess.run([sys.executable,'-B',str(Path(source)/'okx_execution_observer0711.py'),
                        '--root',str(Path(root).resolve()),'--mode',kind,'--report'],check=True)
        return
    if action not in ('on','off'):
        raise ValueError('Unknown observer action')
    props=_stable0712_validate(service,root,kind,unit)
    active=props.get('ActiveState')=='active'
    if action=='off':
        if not active:
            print('Observer already OFF:',unit)
            return
        subprocess.run(['systemctl','disable','--now',unit],check=True,timeout=35)
        if legacy.service_active(unit):
            raise RuntimeError('Observer did not stop: '+unit)
        print('Observer OFF (trading bot untouched):',unit)
        return
    if active:
        print('Observer already ON:',unit)
        return
    if not legacy.service_active(service+'.service'):
        raise RuntimeError('Trading service is not active; observer not started')
    old=_original_stable_monitor_state(service,root,kind)
    if old['managed'] or old['external']:
        raise RuntimeError('Another observer is already active: '+str(old))
    # Enable ONLY the pre-existing, verified versionless unit. No new unit creation.
    subprocess.run(['systemctl','enable','--now',unit],check=True,timeout=35)
    if not legacy.service_active(unit):
        raise RuntimeError('Observer failed to start: '+unit)
    print('Observer ON (trading bot untouched):',unit)
