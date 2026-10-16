#!/usr/bin/env python3
"""Read-only monitor discovery + guarded actions for OKX manager v0.7.4.

Never changes/reads exchange credentials or trading state. The only systemd
mutations possible here are for optional *manager-created* monitor units after
explicit menu/CLI action. Legacy externally-managed observers are never stopped.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
from pathlib import Path

KNOWN_EXTERNAL = {
    ('okx-grid-eth064', 'burst'): ('okx-eth-burst-observer.service',),
    ('okx-grid-eth064', 'execution'): ('okx-eth-execution-observer073.service',),
}
VALID_KIND = frozenset(('burst', 'execution'))
ENGINE_RE = re.compile(r'\bokx_bot074\.py\b')


def service_active(unit: str) -> bool:
    try:
        res = subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                             capture_output=True, timeout=3, check=False)
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def managed_units(service: str, kind: str):
    from okx_monitor074 import monitor_unit
    if kind not in VALID_KIND:
        raise ValueError('Unknown monitor kind')
    return {v: (monitor_unit(service, kind) if v == '074' else
                service + '-' + kind + '-monitor073.service') for v in ('074', '073')}


def external_units(service: str, root: Path, kind: str):
    # Never attribute an unrelated machine's observer to an OKX bot.
    # The two known historical external observers were installed for eth064.
    root = Path(root)
    if service == 'okx-grid-eth064' and root.name == 'eth064':
        return KNOWN_EXTERNAL.get((service, kind), ())
    return ()


def monitor_state(service: str, root: Path, kind: str):
    try:
        candidates = managed_units(service, kind).values()
    except ValueError:
        candidates = ()
    managed = tuple(unit for unit in candidates if service_active(unit))
    external = tuple(unit for unit in external_units(service, root, kind)
                     if service_active(unit))
    return {'managed': managed, 'external': external}


def label(service: str, root: Path, kind: str) -> str:
    st = monitor_state(service, root, kind)
    if st['managed'] and st['external']:
        return 'ON (встроенный + внешний!)'
    if st['external']:
        return 'ON (внешний)'
    if st['managed']:
        return 'ON'
    return 'OFF'


def status_label(service: str, root: Path, kind: str) -> str:
    v = label(service, root, kind)
    return {'ON': 'ВКЛЮЧЁН', 'OFF': 'ВЫКЛЮЧЕН',
            'ON (внешний)': 'ВКЛЮЧЁН (внешний)',
            'ON (встроенный + внешний!)': 'ВКЛЮЧЕНЫ ДВА МОНИТОРА!'}.get(v, v)


def _engine_is_074(service: str) -> bool:
    try:
        res = subprocess.run(['systemctl', 'show', service + '.service',
                              '-p', 'ExecStart', '--value'],
                             check=False, capture_output=True, text=True, timeout=4)
        return res.returncode == 0 and bool(ENGINE_RE.search(res.stdout))
    except (OSError, subprocess.TimeoutExpired):
        return False


def assert_compatible(service: str, root: Path) -> None:
    if not service_active(service + '.service'):
        raise RuntimeError('Бот не работает: сначала запустите торговый процесс.')
    if not _engine_is_074(service):
        raise RuntimeError('Монитор v0.7.4 поддерживает только запущенный движок v0.7.4. '
                           'Торговый бот пока на старой версии; ничего не изменено.')
    try:
        cfg = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        state = json.loads((root / 'state' / 'bot04_state.json').read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise RuntimeError('Не удалось сверить конфигурацию и состояние бота.') from exc
    if state.get('version') != '0.7.4' or cfg.get('instrument') != state.get('instrument'):
        raise RuntimeError('Монитор v0.7.4: несовместимая версия состояния или инструмент.')


def monitor_action(service: str, root: Path, kind: str, action: str, source: Path):
    """Explicit action only, never touches external monitor or trading service."""
    root, source = Path(root), Path(source)
    if action not in ('on', 'off', 'status', 'report'):
        raise ValueError('Monitor action must be on/off/status/report')
    state = monitor_state(service, root, kind)
    if action == 'status':
        print(kind + ':', label(service, root, kind))
        for unit in (*state['managed'], *state['external']):
            print('  active:', unit)
        return
    if action == 'report':
        if state['external']:
            print(kind + ': внешний наблюдатель:', ', '.join(state['external']))
            if kind == 'execution':
                script = root / 'diagnostics' / 'eth_execution_observer073.py'
                if script.is_file():
                    subprocess.run([sys.executable, str(script), '--report'], check=True)
                else:
                    print('Отчёт внешнего Execution Monitor: ',
                          root / 'diagnostics' / 'execution_v073')
            else:
                print('История внешнего Burst Monitor:',
                      root / 'diagnostics' / 'eth070_bursts.jsonl')
            return
        if state['managed']:
            script = source / 'okx_monitor074.py'
            if 'monitor073' in ' '.join(state['managed']):
                script = source / 'okx_monitor073.py'
            if not script.is_file():
                raise RuntimeError('Не найден скрипт отчёта действующего монитора')
            subprocess.run([sys.executable, str(script), '--root', str(root),
                            '--service', service, '--kind', kind, 'report'], check=True)
            return
        print(kind + ': монитор выключен; отчёт не формировался.')
        return
    if state['external']:
        units = ', '.join(state['external'])
        raise RuntimeError('Уже работает внешний монитор: ' + units + '. '
                           'Он не управляется этим переключателем. '
                           'Сервис оставлен без изменений.')
    scripts = {'074': source / 'okx_monitor074.py',
               '073': source / 'okx_monitor073.py'}
    units = managed_units(service, kind)
    if action == 'on':
        if units['074'] in state['managed']:
            print('Монитор уже включён:', units['074'])
            return
        if units['073'] in state['managed']:
            raise RuntimeError('Уже работает встроенный монитор v0.7.3. '
                               'Сначала отключите его отдельной командой.')
        assert_compatible(service, root)
        v = '074'
        if not scripts[v].is_file():
            raise RuntimeError('Не найден скрипт наблюдателя v0.7.4')
        cmd = [sys.executable, str(scripts[v]), '--root', str(root),
               '--service', service, '--kind', kind, 'on']
        subprocess.run(cmd, check=True)
    else:
        if not state['managed']:
            print('Встроенный монитор уже выключен.')
            return
        # Explicitly disable only a manager-owned monitor, no external services.
        for v in ('074', '073'):
            if units[v] not in state['managed']:
                continue
            if not scripts[v].is_file():
                raise RuntimeError(f'Не найден скрипт для отключения встроенного {v}')
            cmd = [sys.executable, str(scripts[v]), '--root', str(root),
                   '--service', service, '--kind', kind, 'off']
            subprocess.run(cmd, check=True)
