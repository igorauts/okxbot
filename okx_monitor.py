"""One observer control interface for current bots; no legacy adapters."""
import subprocess
from pathlib import Path
from okx_observers import (unit_name, monitor_action as _action,
                           supports_versionless)


def _active(unit):
    try:
        return subprocess.run(['systemctl','is-active','--quiet',unit],
            capture_output=True,timeout=5,check=False).returncode == 0
    except (OSError,subprocess.TimeoutExpired):
        return False


def _enabled(unit):
    try:
        p=subprocess.run(['systemctl','is-enabled',unit],capture_output=True,
            text=True,timeout=5,check=False)
        return p.returncode == 0 and p.stdout.strip() in ('enabled','enabled-runtime')
    except (OSError,subprocess.TimeoutExpired):
        return False


def monitor_state(service,root,kind):
    if not supports_versionless(root,service):
        return {'managed':(), 'external':()}
    unit=unit_name(service,kind)
    return {'managed':(unit,) if _enabled(unit) or _active(unit) else (), 'external':()}


def label(service,root,kind):
    st=monitor_state(service,root,kind)
    unit=unit_name(service,kind)
    if unit not in st['managed']:return 'OFF'
    if _active(unit):return 'ON'
    if _active(service+'.service'):return 'ERROR (монитор не работает)'
    return 'ARMED (автозапуск)'


def status_label(service,root,kind):
    return {'ON':'ВКЛЮЧЁН','OFF':'ВЫКЛЮЧЕН',
        'ARMED (автозапуск)':'НАСТРОЕН АВТОЗАПУСК',
        'ERROR (монитор не работает)':'ОШИБКА МОНИТОРА'}.get(label(service,root,kind),label(service,root,kind))


def monitor_action(service,root,kind,action,source):
    if not supports_versionless(root,service):
        raise RuntimeError('Legacy bot monitor management disabled')
    if action=='status':
        st=monitor_state(service,root,kind)
        print(kind,label(service,root,kind),'managed',st['managed'])
        return
    return _action(root,service,kind,action,source)
