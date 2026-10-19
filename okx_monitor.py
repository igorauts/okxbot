#!/usr/bin/env python3
"""OKX observers: versionless v0.7.14+ and unchanged legacy compatibility.
Only local state, systemd metadata and read-only observer program are used.
"""
import subprocess
from pathlib import Path
import okx_monitor_compat078 as legacy
from okx_monitor_compat074 import service_active as _service_active
from okx_observers import supports_versionless, unit_name, monitor_action as _stable_action


def _new(service,root):
    try:
        return supports_versionless(root,service)
    except (OSError,ValueError,RuntimeError):
        return False


def _enabled(unit):
    p=subprocess.run(['systemctl','is-enabled',unit],capture_output=True,
                     text=True,timeout=8,check=False)
    return p.returncode==0 and p.stdout.strip() in ('enabled','enabled-runtime')


def monitor_state(service,root,kind):
    old=legacy.monitor_state(service,root,kind)
    if not _new(service,root):
        return old
    unit=unit_name(service,kind)
    known=_enabled(unit) or _service_active(unit)
    managed=tuple(x for x in old['managed'] if x!=unit)
    return {'managed':((unit,)+managed if known else managed),
            'external':old['external']}


def label(service,root,kind):
    if not _new(service,root):
        return legacy.label(service,root,kind)
    st=monitor_state(service,root,kind)
    unit=unit_name(service,kind)
    nonstandard=tuple(x for x in st['managed'] if x!=unit)
    if st['external'] or nonstandard:
        return 'ON (встроенный + внешний!)' if unit in st['managed'] else 'ON (внешний)'
    if unit not in st['managed']:
        return 'OFF'
    if _service_active(unit):
        return 'ON'
    if _service_active(service+'.service'):
        return 'ERROR (монитор не работает)'
    return 'ARMED (автозапуск)' 


def status_label(service,root,kind):
    value=label(service,root,kind)
    return {'ON':'ВКЛЮЧЁН','OFF':'ВЫКЛЮЧЕН',
            'ARMED (автозапуск)':'НАСТРОЕН АВТОЗАПУСК',
            'ERROR (монитор не работает)':'ОШИБКА МОНИТОРА',
            'ON (внешний)':'ВКЛЮЧЁН (внешний)',
            'ON (встроенный + внешний!)':'ВКЛЮЧЕНЫ ДВА МОНИТОРА!'}.get(value,value)


def monitor_action(service,root,kind,action,source):
    if not _new(service,root):
        return legacy.monitor_action(service,root,kind,action,source)
    if action=='status':
        st=monitor_state(service,root,kind)
        print(kind,label(service,root,kind),'managed',st['managed'],'external',st['external'])
        return
    if action=='report':
        return _stable_action(root,service,kind,'report',source)
    st=monitor_state(service,root,kind)
    unit=unit_name(service,kind)
    if st['external'] or any(x!=unit for x in st['managed']):
        raise RuntimeError('Other observer active; refusing duplicate: '+str(st))
    return _stable_action(root,service,kind,action,source)
