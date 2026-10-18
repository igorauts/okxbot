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
