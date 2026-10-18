#!/usr/bin/env python3
"""Named OKX grid-bot instances: lifecycle via systemd, read-only statistics.

This controller does not place/cancel exchange orders. LIVE execution stays
behind bot config + two explicit environment guardrails and user consent.
"""
import argparse
import curses
import codecs
import contextlib
import io
import selectors
import json
import math
import time
from datetime import datetime, timezone
import os
import re
from collections import deque
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import okx_setup as wizard
from okx_observers import provision_observers
from okx_instance_discovery import discover_single_account

BASE = Path(os.environ.get('OKXBOT_BASE_DIR', '/root/okx')).resolve()
REGISTRY = Path(os.environ.get('OKXBOT_REGISTRY', str(BASE / '.okxbot_registry.json')))
SYSTEMD_DIR = Path(os.environ.get('OKXBOT_SYSTEMD_DIR', '/etc/systemd/system'))
SOURCE = Path(__file__).resolve().parent
SERVICE_RE = re.compile(r'^okx[-a-z0-9]+$')


def run(args, **kw):
    return subprocess.run(args, check=kw.pop("check", True), **kw)


def load_registry():
    if not REGISTRY.exists():
        return {}
    raw = json.loads(REGISTRY.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise RuntimeError('Некорректный реестр ботов')
    return raw


def save_registry(items):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.okxbot-', dir=REGISTRY.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, REGISTRY)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def find_entry(name):
    items = load_registry()
    for display, entry in items.items():
        if display.casefold() == name.casefold():
            return display, entry
    raise ValueError(f'Бот «{name}» не найден. Посмотреть список: okxbot list')


def safe_slug(name, items):
    slug = re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')[:32].strip('-')
    used = {e.get('slug') for e in items.values()}
    if not slug or slug in used:
        n = 1
        while f'bot-{n:03d}' in used or (BASE / f'bot-{n:03d}').exists():
            n += 1
        slug = f'bot-{n:03d}'
    if not SERVICE_RE.fullmatch(f'okx-grid-{slug}'):
        raise ValueError('Недопустимое имя службы')
    return slug


def unit_name(entry):
    val = entry['service']
    if not SERVICE_RE.fullmatch(val):
        raise ValueError('Недопустимое имя службы')
    return val + '.service'


def bot_path(entry):
    p = Path(entry['path']).resolve()
    if p == BASE or BASE not in p.parents or not (p / 'config.json').is_file():
        raise RuntimeError('Некорректный каталог бота')
    return p


def _engine_key(filename):
    """0.6.10 < 0.7.0 < 0.7.7 < 0.7.10 (not lexical ordering)."""
    m=re.fullmatch(r'okx_bot0(\d)(\d{1,2})\.py',filename)
    return (0,int(m.group(1)),int(m.group(2))) if m else None


def latest_engine(source=SOURCE):
    """The release manifest and internal VER are authoritative; never filename digits."""
    source=Path(source)
    path=source/'okx_bot.py'
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('Нет установленного торгового движка okx_bot.py')
    raw=path.read_text(encoding='utf-8')
    m=re.search(r"(?m)^VER\s*=\s*['\"]([0-9]+\.[0-9]+\.[0-9]+)['\"]",raw)
    if not m:
        raise RuntimeError('Внутренняя версия VER в okx_bot.py отсутствует')
    ver=m.group(1)
    compile(raw,str(path),'exec')
    manifest=json.loads((source/'engine_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version')!=1 or manifest.get('current_version')!=ver:
        raise RuntimeError('Несоответствие VER и текущей версии манифеста')
    if manifest.get('engines',{}).get(ver,{}).get('file')!='okx_bot.py':
        raise RuntimeError('Манифест не указывает на okx_bot.py')
    return 'okx_bot.py',ver



def engine_file(path):
    """Prefer versionless engine; keep legacy read-only audit support for migration."""
    path=Path(path)
    stable=path/'okx_bot.py'
    if stable.is_file() and not stable.is_symlink():
        return 'okx_bot.py'
    matches=[(_engine_key(p.name),p.name) for p in path.glob('okx_bot0*.py')
             if _engine_key(p.name) is not None and p.is_file() and not p.is_symlink()]
    if not matches:
        raise RuntimeError('Не найден исполняемый Python-файл бота')
    return max(matches)[1]



def latest_dependencies(engine_name,version,source=SOURCE):
    """Manifest-driven future dependencies; no manager changes per release."""
    path=Path(source)/'engine_manifest.json'
    if not path.is_file():
        # Backwards compatibility for already installed old manager directories.
        return ['okx_terminal_accounting_v077.py'] if version=='0.7.7' else []
    manifest=json.loads(path.read_text())
    if manifest.get('schema_version')!=1:
        raise RuntimeError('Unsupported engine manifest')
    entry=manifest.get('engines',{}).get(version)
    if not isinstance(entry,dict) or entry.get('file')!=engine_name:
        raise RuntimeError('Latest engine missing/mismatched manifest')
    deps=entry.get('dependencies')
    if not isinstance(deps,list):raise RuntimeError('Invalid engine dependencies')
    for dep in deps:
        if (not isinstance(dep,str) or not re.fullmatch(r'[a-zA-Z0-9_]+\.py',dep)
            or not (Path(source)/dep).is_file()):
            raise RuntimeError('Missing/unsafe engine dependency')
    return deps


def provision_latest_engine(created,source=SOURCE):
    """One-time wizard may ship an old engine: replace ONLY in NEW directory."""
    name,version=latest_engine(source)
    created=Path(created)
    if not (created/'config.json').is_file():
        raise RuntimeError('Wizard did not create config.json')
    cfg=json.loads((created/'config.json').read_text())
    if (created/'state/bot04_state.json').exists():
        raise RuntimeError('Never upgrade an existing trading state in wizard')
    shutil.copy2(Path(source)/name,created/name)
    for dependency in latest_dependencies(name,version,source):
        shutil.copy2(Path(source)/dependency,created/dependency)
    for old in created.glob('okx_bot0*.py'):
        if _engine_key(old.name) is not None and old.name != name:old.unlink()
    cfg['version']=version
    fd,tmp=tempfile.mkstemp(prefix='.config-engine-',dir=created)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(cfg,f,indent=2,ensure_ascii=False)
            f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,created/'config.json')
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return name,version


def unit_text(path, engine, name):
    # Only paths created by our wizard are allowed: ASCII and no whitespace.
    if any(ch.isspace() for ch in str(path)) or not str(path).isascii():
        raise RuntimeError('Путь с пробелами не поддерживается для службы')
    description = name.replace('\n', ' ').replace('\r', ' ')[:48]
    cleanup_file = 'okx_panic_cleanup.py'
    return (f'[Unit]\nDescription=OKX Grid Bot ({description})\n'
            'Wants=network-online.target\nAfter=network-online.target\n'
            'StartLimitIntervalSec=300\nStartLimitBurst=10\n\n'
            '[Service]\nType=simple\nUser=root\n'
            f'WorkingDirectory={path}\n'
            'Environment=PYTHONUNBUFFERED=1\n'
            'Environment=OKX_BOT04_LIVE=CONFIRM\n'
            'Environment=OKX_BOT04_PROBE=FULL_25\n'
            f'ExecStart={path}/.venv/bin/python -u {path}/{engine}\n'
            f'ExecStopPost=/usr/bin/python3 -B {SOURCE}/{cleanup_file} --root {path} --service okx-grid-{path.name}\n'
            'Restart=on-failure\nRestartSec=15\n\n'
            '[Install]\nWantedBy=multi-user.target\n')


def service_running(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                          check=False).returncode == 0


def describe(entry):
    cfg = json.loads((bot_path(entry) / 'config.json').read_text())
    return cfg.get('instrument','?')


def audit_entry(entry):
    path = bot_path(entry)
    env = {k:v for k,v in os.environ.items()
           if k not in ('OKX_BOT04_LIVE','OKX_BOT04_PROBE','OKX_BOT05_STATS')}
    env['OKX_BOT04_AUDIT'] = 'READ_ONLY'
    print('Проверка OKX без выставления ордеров…', flush=True)
    run([str(path / '.venv/bin/python'), '-u', str(path / engine_file(path))],
        cwd=path, env=env, timeout=180)


def scan_other_live_same_instrument(current_path, instrument):
    """Catch running non-registered okx-bot01/02 too, not only our registry."""
    for unit in SYSTEMD_DIR.glob('okx*.service'):
        try:
            match = re.search(r'^WorkingDirectory=(.+)$',unit.read_text(),re.M)
            if not match: continue
            other = Path(match.group(1).strip()).resolve()
            if other == current_path or not (other / 'config.json').exists(): continue
            cfg = json.loads((other / 'config.json').read_text())
            if cfg.get('instrument') == instrument and service_running(unit.name):
                return unit.name
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def cmd_new(env_file):
    entries = load_registry()
    env, raw = wizard.read_env(env_file)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError('Для опросника нужен интерактивный SSH-терминал')
    ctx = curses.wrapper(lambda scr: wizard.wizard(scr, env, entries.keys()))
    if ctx is None:
        print('Отменено. Нового бота нет.')
        return
    name = wizard.validate_name(ctx['name'], entries.keys())
    slug = safe_slug(name, entries)
    path = BASE / slug
    service = f'okx-grid-{slug}'
    if (SYSTEMD_DIR / f'{service}.service').exists():
        raise RuntimeError('Уже есть systemd-служба с этим именем')
    # No trading: create isolated config/state, install packages, install disabled unit.
    created = wizard.persist(ctx, path, raw)
    selected_engine, selected_version = provision_latest_engine(created)
    unit = SYSTEMD_DIR / f'{service}.service'
    try:
        run([sys.executable, '-m', 'venv', str(created / '.venv')])
        run([str(created / '.venv/bin/pip'), 'install', '-r', str(created / 'requirements.txt')])
        unit.write_text(unit_text(created, selected_engine, name))
        os.chmod(unit, 0o644)
        run(['systemctl', 'daemon-reload'])
        # Auto-arm BOTH read-only observers; never start the trading bot.
        provision_observers(created, service, SOURCE, SYSTEMD_DIR)
        entries[name] = {'slug':slug, 'path':str(created),'service':service}
        save_registry(entries)
    except Exception:
        # Do not delete the new directory: contains a secret .env, no accidental loss.
        print(f'Создана папка {created}, но установка не завершилась. Торговля выключена.',file=sys.stderr)
        raise
    print(f'\nБот «{name}» создан. Торговля пока выключена.')
    print(f'Управление: okxbot start "{name}" | okxbot stats "{name}" | okxbot stop "{name}"')
    if sys.stdin.isatty():
        if input('\nЗапустить реальные торги сейчас? (yes/no): ').strip().casefold() == 'yes':
            cmd_start(name)


def cmd_attach(name, path, service):
    """Read-only registration of an EXISTING systemd bot, never restart it."""
    entries = load_registry()
    wizard.validate_name(name, entries.keys())
    source = Path(path).resolve()
    if source == BASE or BASE not in source.parents or not (source / 'config.json').is_file():
        raise RuntimeError('Можно привязать только существующий бот внутри /root/okx')
    if not SERVICE_RE.fullmatch(service):
        raise ValueError('Имя службы должно начинаться с okx и содержать только a-z, 0-9, -')
    f = SYSTEMD_DIR / f'{service}.service'
    if not f.is_file():raise RuntimeError(f'Служба {service}.service не найдена')
    m = re.search(r'^WorkingDirectory=(.+)$',f.read_text(),re.M)
    if not m or Path(m.group(1).strip()).resolve() != source:
        raise RuntimeError('WorkingDirectory службы отличается от каталога бота')
    if any(e['path'] == str(source) or e['service'] == service for e in entries.values()):
        raise RuntimeError('Этот бот или служба уже привязаны к другому имени')
    slug=safe_slug(name,entries)
    entries[name]={'slug':slug,'path':str(source),'service':service}
    save_registry(entries)
    print(f'Бот «{name}» привязан: {source} ({service})')
    print('Рабочий процесс, config.json и systemd НЕ изменены.')


def cmd_start(name):
    name,entry=find_entry(name)
    path=bot_path(entry)
    unit=unit_name(entry)
    cfg_path=path / 'config.json'
    cfg=json.loads(cfg_path.read_text())
    if instance_terminal(entry) or control_desired(entry) in ('HARD','PANIC'):
        raise RuntimeError('Этот бот завершён окончательно или завершает работу. '
                           'Создайте новый экземпляр-клон.')
    if service_running(unit):
        print(f'Бот «{name}» уже работает.')
        return
    conflict = scan_other_live_same_instrument(path,cfg.get('instrument'))
    if conflict:
        raise RuntimeError(f'{conflict} уже торгует {cfg.get("instrument")}. '
                           'В NET-режиме два независимых бота на одной паре опасны.')
    if not (path / '.venv/bin/python').exists():
        raise RuntimeError('Python venv не найден. Не запускаем.')
    # Every attempted start must pass a fresh read-only reconciliation.
    audit_entry(entry)
    print(f'\nВНИМАНИЕ: запуск «{name}» означает РЕАЛЬНЫЕ ордера на OKX.')
    print(f'Пара: {cfg.get("instrument")}  |  GRID: {cfg.get("grid_step")}  |  TP: {cfg.get("take_profit")}')
    if input(f'Для запуска введи точно START {name}: ').strip()!=f'START {name}':
        print('Запуск отменён. Ничего не менялось.')
        return
    if cfg.get('live_trading') is not True:
        cfg['live_trading'] = True
        fd,tmp=tempfile.mkstemp(dir=path,prefix='.config-')
        try:
            with os.fdopen(fd,'w') as f:
                json.dump(cfg,f,indent=2,ensure_ascii=False)
                f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600)
            os.replace(tmp,cfg_path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    run(['systemctl','enable','--now',unit])
    run(['systemctl','status',unit,'--no-pager','--lines=10'],check=False)
    print(f'Логи: okxbot logs "{name}"')


# The lifecycle is a durable request to the engine, NOT a systemctl stop.
# The bot reconciles and cancels only its own orders. A manager must never
# silently cancel unknown OKX orders or kill a bot during cancellation.
CONTROL_MODES = {'RUN', 'PAUSE', 'SOFT', 'HARD', 'PANIC'}
TERMINAL_PHASES = {'RETIRED', 'PANIC_DONE'}


def control_supported(entry):
    """Inspect effective ExecStart, respecting ordered systemd drop-ins.

    Older running engines remain recognizable until explicitly migrated.
    No systemd mutations, no trust in registry alone.
    """
    path=bot_path(entry)
    unit=SYSTEMD_DIR/unit_name(entry)
    if not unit.is_file() or unit.is_symlink():return False
    files=[unit]
    override=SYSTEMD_DIR/(unit.name+'.d')
    if override.is_dir():files+=sorted(override.glob('*.conf'))
    command=''
    for f in files:
        if f.is_symlink():return False
        for line in f.read_text(encoding='utf-8').splitlines():
            if line.startswith('ExecStart='):
                command=line[len('ExecStart='):].strip()
    if not command:return False
    parts=command.split()
    return any(Path(token).parent==path
               and (Path(token).name=='okx_bot.py' or
                    _engine_key(Path(token).name) is not None)
               and Path(token).is_file() and not Path(token).is_symlink()
               for token in parts)




def read_control(entry):
    p=bot_path(entry) / 'state/control.json'
    if not p.exists():return {'mode':'RUN'}
    raw=json.loads(p.read_text(encoding='utf-8'))
    if raw.get('mode') not in CONTROL_MODES:
        raise RuntimeError('Некорректное сохранённое состояние управления')
    return raw


def read_ack(entry):
    p=bot_path(entry) / 'state/control_ack.json'
    if not p.exists():return {}
    raw=json.loads(p.read_text(encoding='utf-8'))
    return raw if isinstance(raw,dict) else {}


def instance_terminal(entry):
    if entry.get('retired'):return True
    return read_ack(entry).get('phase') in TERMINAL_PHASES


def control_desired(entry):
    if not control_supported(entry):return 'LEGACY'
    return read_control(entry)['mode']


def write_control(entry, mode):
    if mode not in CONTROL_MODES:raise ValueError('Unknown control mode')
    path=bot_path(entry)/'state/control.json'
    raw={'schema_version':1,'mode':mode,
         'requested_at':datetime.now(timezone.utc).isoformat()}
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.control-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(raw,out)
            out.flush();os.fsync(out.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def finalize_registry_if_needed(name, entry):
    if not instance_terminal(entry) or entry.get('retired'):
        return
    # A completed engine exits normally: do not kill it, and do not restart it.
    run(['systemctl','disable',unit_name(entry)])
    entries=load_registry()
    if name in entries:
        entries[name]['retired']=True
        entries[name]['terminal_phase']=read_ack(entry).get('phase','RETIRED')
        save_registry(entries)



# OKX_ACCOUNT_RESET_V0712_MANAGER_PATCH_1
# Manager-only account-wide margin latch recovery. No exchange orders here.
ACCOUNT_SOFT_LATCH = Path('/var/lib/okxbot/account_soft_stop.json')

def _account_soft_latch_data():
    """Read account latch; corrupt/unknown latch is always fail closed."""
    if ACCOUNT_SOFT_LATCH.is_symlink():
        raise RuntimeError('Общая блокировка — символическая ссылка; требуется ручная проверка')
    if not ACCOUNT_SOFT_LATCH.exists():
        return None
    try:
        value=json.loads(ACCOUNT_SOFT_LATCH.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Невозможно прочитать общую блокировку; восстановление запрещено') from exc
    if not isinstance(value,dict) or value.get('schema')!=1 or value.get('mode')!='SOFT':
        raise RuntimeError('Некорректная общая блокировка; восстановление запрещено')
    return value

def account_soft_active():
    return _account_soft_latch_data() is not None

def account_soft_menu_label():
    try:
        return ('Общая остановка аккаунта — восстановление (SOFT)' if account_soft_active()
                else 'Общая остановка аккаунта — статус (нет блокировки)')
    except (OSError,RuntimeError,ValueError):
        return 'Общая остановка аккаунта — ОШИБКА, нужна проверка'

def _account_reset_preflight(latch):
    """Verify EVERY active registered instance; never hardcode bot names.

    The VPS is designated for exactly one main OKX account. This checks local
    state and systemd ONLY; operator must separately confirm exchange margin.
    A disappeared/unknown running service or any racing startup fails closed.
    """
    if (latch.get('reason') != 'MARGIN_REJECTION' or
            latch.get('manual_reset_required') is not True):
        raise RuntimeError('Разрешён только ручной сброс MARGIN_REJECTION')
    try:
        latch_at = datetime.fromisoformat(str(latch['at']).replace('Z', '+00:00'))
        if latch_at.tzinfo is None:
            raise ValueError('naive timestamp')
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('Некорректное время общей блокировки') from exc

    # Each invocation enumerates registry and real systemd units afresh.
    # The discovery module rejects unknown active bot services, duplicates,
    # broken registry entries and missing configuration/credentials.
    instances = discover_single_account(BASE, SYSTEMD_DIR)
    registered = load_registry()
    unstable = [bot.service for bot in instances
                if bot.systemd_state in ('activating', 'reloading', 'deactivating')]
    if unstable:
        raise RuntimeError('Службы меняют состояние; повтори позже: '+str(unstable))
    active = [bot for bot in instances if bot.systemd_state == 'active']
    if not active:
        raise RuntimeError('Нет активных экземпляров для безопасной проверки SOFT')

    summary = []
    for bot in active:
        name, slug, instrument = bot.display_name, bot.root.name, bot.instrument
        entry = registered.get(name)
        if not isinstance(entry, dict):
            raise RuntimeError('Экземпляр исчез из реестра: '+name)
        # Catch registry/service changes between discovery and verification.
        if (unit_name(entry) != bot.service or bot_path(entry) != bot.root or
                not service_running(bot.service) or not control_supported(entry)):
            raise RuntimeError('Служба/движок недоступны или изменились: '+slug)
        if instance_terminal(entry):
            raise RuntimeError('Активный экземпляр уже терминален: '+slug)
        try:
            config = json.loads((bot.root/'config.json').read_text(encoding='utf-8'))
            state = json.loads((bot.root/'state/bot04_state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError('Не удаётся проверить сохранённое состояние: '+slug) from exc
        if (not isinstance(config, dict) or not isinstance(state, dict) or
                config.get('instrument') != instrument or
                not isinstance(config.get('version'), str) or
                not config['version'] or
                state.get('version') != config['version']):
            raise RuntimeError('Версия/инструмент/state не согласованы: '+slug)
        # Current local requests and engine acknowledgments BOTH must be SOFT.
        if read_control(entry).get('mode') != 'SOFT':
            raise RuntimeError('Требуется локальный SOFT: '+slug)
        ack = read_ack(entry)
        if ack.get('mode') != 'SOFT' or ack.get('phase') != 'SOFT_STOPPED':
            raise RuntimeError('Отсутствует подтверждение SOFT_STOPPED: '+slug)
        try:
            ack_at = datetime.fromisoformat(str(ack['updated_at']).replace('Z', '+00:00'))
            if ack_at.tzinfo is None or ack_at < latch_at:
                raise ValueError('stale SOFT acknowledgment')
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError('Устаревшее/некорректное SOFT подтверждение: '+slug) from exc
        cycles = state.get('cycles')
        if not isinstance(cycles, dict):
            raise RuntimeError('Некорректный список циклов: '+slug)
        unsettled = sum(1 for cyc in cycles.values()
                        if not isinstance(cyc, dict) or
                        cyc.get('state') in ('BUY_LIVE', 'BUY_PARTIAL', 'PLACING', 'CANCELING'))
        if unsettled:
            raise RuntimeError(f'{slug}: {unsettled} незавершённых BUY/ордерных переходов')
        summary.append((slug, instrument, name, len(cycles)))

    # Discovery is repeated after user confirmation under the latch lock
    # by cmd_account_reset(); thus a fourth bot cannot silently bypass preflight.
    return summary

def cmd_account_reset():
    """Unlock account-wide SOFT ONLY. Each bot must be resumed separately."""
    import fcntl
    data=_account_soft_latch_data()
    if data is None:
        print('Общая блокировка аккаунта отсутствует. Снимать нечего.')
        return
    print('\nОБЩАЯ ОСТАНОВКА АККАУНТА OKX')
    print('Причина:',data.get('reason'),'| код:',data.get('code'))
    print('Инструмент:',data.get('trigger_instrument'),'| время UTC:',data.get('at'))
    summary=_account_reset_preflight(data)
    for slug,inst,name,count in summary:
        print(f'  {slug}: {inst}, SOFT_STOPPED, {count} циклов')
    print('\nВАЖНО: менеджер НЕ МОЖЕТ подтвердить наличие свободной маржи на OKX.')
    print('Перед сбросом проверь плечо, свободную маржу и риск новых BUY вручную.')
    print('Сброс НЕ возобновляет BUY. Каждого бота после него запускаешь отдельно через Resume.')
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError('Сброс возможен только из интерактивного терминала')
    phrase='RESET ACCOUNT SOFT STOP'
    if input('Для снятия общей блокировки введи точно '+phrase+': ').strip()!=phrase:
        print('Отменено: общая блокировка сохранена.')
        return
    lock=ACCOUNT_SOFT_LATCH.with_name(ACCOUNT_SOFT_LATCH.name+'.lock')
    fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        fresh=_account_soft_latch_data()
        if fresh!=data:
            raise RuntimeError('Общая блокировка изменилась за время подтверждения; сброс запрещён')
        # Re-check AFTER operator confirmation while holding exchange-compatible flock.
        _account_reset_preflight(fresh)
        if ACCOUNT_SOFT_LATCH.is_symlink():
            raise RuntimeError('Недопустимая ссылка вместо общей блокировки')
        # Preserve the evidence BEFORE unlocking. No config, state or SQLite writes.
        audit=ACCOUNT_SOFT_LATCH.with_name('account_soft_reset_audit.jsonl')
        record={'event':'MANUAL_ACCOUNT_SOFT_RESET',
                'at':datetime.now(timezone.utc).isoformat(),
                'latch':fresh,'bots':[x[0] for x in summary]}
        with os.fdopen(os.open(audit,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600),'a',encoding='utf-8') as f:
            f.write(json.dumps(record,ensure_ascii=False,sort_keys=True)+'\n')
            f.flush();os.fsync(f.fileno())
        ACCOUNT_SOFT_LATCH.unlink()
        dfd=os.open(ACCOUNT_SOFT_LATCH.parent,os.O_RDONLY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)
    print('\nОбщая блокировка СНЯТА. Все три бота по-прежнему SOFT.')
    print('Возобновляй нужные боты по одному через меню «Возобновить работу».')
    print('Если проблема с маржой повторится, блокировка автоматически восстановится.')

def cmd_control(name, mode):
    if mode=='RUN' and account_soft_active():
        raise RuntimeError('Общий SOFT STOP аккаунта активен. Сначала открой меню '
                           '«Общая остановка аккаунта — восстановление».')
    name,entry=find_entry(name)
    if not control_supported(entry):
        raise RuntimeError(f'Бот «{name}» работает на старом движке. '
            'Команды Pause/Soft/Hard/Panic без поддержки движка запрещены. '
            'Сейчас бот НЕ изменён; требуется отдельное безопасное обновление.')
    if instance_terminal(entry):
        finalize_registry_if_needed(name,entry)
        raise RuntimeError(f'Бот «{name}» уже завершён. Новый запуск только через клонирование.')
    old=read_control(entry)['mode']
    if old in ('HARD','PANIC'):
        raise RuntimeError(f'Уже выполняется окончательное завершение {old}. '
                           'Нельзя отменить или заменить команду.')
    if not service_running(unit_name(entry)):
        raise RuntimeError('Бот не работает. Не отправляем команду в неработающий движок. '
                           'Проверьте его состояние и ордера.')
    if mode=='PANIC':
        conflict=scan_other_live_same_instrument(bot_path(entry),describe(entry))
        if conflict:
            raise RuntimeError(f'Panic Sell запрещён: {conflict} одновременно торгует '
                               'этот NET-инструмент. Чужую позицию закрывать нельзя.')
    if mode=='RUN' and old not in ('PAUSE','SOFT'):
        raise RuntimeError('Возобновление возможно только после паузы или Soft Stop')
    if mode=='PAUSE' and old=='PAUSE':
        print(f'Бот «{name}» уже на паузе.')
        return
    if mode=='SOFT' and old=='SOFT':
        print(f'Soft Stop «{name}» уже выполняется/выполнен.')
        return
    if mode in ('HARD','PANIC'):
        print(f'\nВНИМАНИЕ: окончательное завершение «{name}».')
        print('Все BUY и реальные SELL TP будут отменены с ожиданием подтверждения OKX.')
        print('Виртуальные TP прекращают обслуживаться.')
        if mode=='HARD':
            print('ОТКРЫТАЯ ПОЗИЦИЯ НЕ ЗАКРЫВАЕТСЯ и остаётся БЕЗ ЗАЩИТЫ.')
            print('Её придётся закрыть/защитить вручную, прежде чем запускать клон на той же паре.')
        else:
            print('После подтверждённой отмены ордеров позиция бота будет закрыта MARKET reduce-only.')
            print('В NET-режиме закрывать чужую позицию нельзя. Любое расхождение блокирует исполнение.')
    elif mode=='SOFT':
        print(f'Soft Stop «{name}»: отменяются только BUY LIMIT. Все TP продолжают обслуживаться.')
    elif mode=='PAUSE':
        print(f'Пауза «{name}»: текущие BUY остаются, новых BUY и переносов не будет, TP работают.')
    else:
        print(f'Возобновить LIVE BUY-сетку бота «{name}»?')
    marker={'PAUSE':'PAUSE','RUN':'RESUME','SOFT':'SOFT',
            'HARD':'HARD','PANIC':'PANIC SELL'}[mode]
    confirm=f'{marker} {name}'
    if input(f'Введите точно {confirm}: ').strip()!=confirm:
        print('Отменено. Бот продолжает работать в прежнем режиме.')
        return
    if mode in ('HARD','PANIC'):
        confirm2=f'FINAL {name}'
        if input(f'Повторное подтверждение {confirm2}: ').strip()!=confirm2:
            print('Отменено. Бот продолжает работать в прежнем режиме.')
            return
    if mode=='RUN' and account_soft_active():
        raise RuntimeError('Общий SOFT STOP снова активен. RESUME не отправлен.')
    write_control(entry,mode)
    print('Запрос принят. Движок подтвердит состояние после сверки OKX.')
    if mode in ('HARD','PANIC'):
        print(f'Статус: okxbot status "{name}" | Логи: okxbot logs "{name}" -f')


def cmd_stop(name):
    # Uniform SOFT semantics for v0.6.4. Legacy engines are not supported.
    return cmd_control(name,'SOFT')


def cmd_clone(source_name, new_name):
    """Make a NEW isolated bot from a completed instance's parameters.

    Do NOT copy state, statistics, logs, private order history or LIVE flag.
    Market price must be obtained fresh if capital reference is unbounded.
    """
    old,entry=find_entry(source_name)
    if not instance_terminal(entry):
        raise RuntimeError('Клонируются только окончательно завершённые боты.')
    entries=load_registry()
    new_name=wizard.validate_name(new_name,entries.keys())
    slug=safe_slug(new_name,entries)
    dest=BASE/slug
    if dest.exists() or (SYSTEMD_DIR/f'okx-grid-{slug}.service').exists():
        raise RuntimeError('Каталог или служба клона уже существует')
    source=bot_path(entry)
    cfg=json.loads((source/'config.json').read_text())
    # Fresh PUBLIC price; never reuse old setup price for 1x capital.
    import urllib.request,urllib.parse
    url='https://www.okx.com/api/v5/market/ticker?'+urllib.parse.urlencode({'instId':cfg['instrument']})
    req=urllib.request.Request(url,headers={'User-Agent':'curl/8.5.0'})
    with urllib.request.urlopen(req,timeout=12) as response:
        data=json.load(response)
    if data.get('code')!='0' or len(data.get('data',[]))!=1:
        raise RuntimeError('Не удалось получить актуальную цену клона')
    from decimal import Decimal,ROUND_FLOOR
    tick=Decimal(str(cfg['tick_size']))
    bid=Decimal(str(data['data'][0]['bidPx']))
    px=(bid/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    upper=Decimal(str(cfg['upper_price']))
    ref=upper if upper else px
    if ref-Decimal(str(cfg['grid_step']))*(int(cfg['max_grids'])-1)<=0:
        raise RuntimeError('Новая цена несовместима с выбранным диапазоном гридов')
    cfg['real_tp_target']=int(cfg.get('real_tp_target',min(25,int(cfg.get('active_tp_orders',25)))))
    cfg['active_tp_orders']=cfg['real_tp_target']
    cfg['buy_buffer_orders']=5
    cfg['buy_cancel_protect_steps']=5
    cfg['tp_buffer_orders']=int(cfg['active_buy_orders'])+5
    selected_engine, selected_version = latest_engine()
    cfg.update(version=selected_version,name=new_name,created_at=time.time(),
               live_trading=False,price_at_setup=str(px),
               capital_reference_price=str(ref),cloned_from=old)
    print(f'Клон «{new_name}» из «{old}» | {cfg["instrument"]} | текущий BID {px}')
    if input(f'Введите точно CLONE {new_name}: ').strip()!=f'CLONE {new_name}':
        print('Отменено.')
        return
    dest.mkdir(mode=0o700)
    (dest/'state').mkdir(mode=0o700)
    (dest/'logs').mkdir(mode=0o700)
    for f in (selected_engine,'requirements.txt'):
        shutil.copy2(SOURCE/f,dest/f)
    for dependency in latest_dependencies(selected_engine,selected_version):
        shutil.copy2(SOURCE/dependency,dest/dependency)
    (dest/'.env').write_bytes((source/'.env').read_bytes())
    os.chmod(dest/'.env',0o600)
    fd=os.open(dest/'config.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(cfg,f,indent=2,ensure_ascii=False)
    run([sys.executable,'-m','venv',str(dest/'.venv')])
    run([str(dest/'.venv/bin/pip'),'install','-r',str(dest/'requirements.txt')])
    service=f'okx-grid-{slug}'
    (SYSTEMD_DIR/f'{service}.service').write_text(unit_text(dest,selected_engine,new_name))
    run(['systemctl','daemon-reload'])
    # Auto-arm BOTH read-only observers before registering the clone.
    provision_observers(dest, service, SOURCE, SYSTEMD_DIR)
    entries[new_name]={'slug':slug,'path':str(dest),'service':service}
    save_registry(entries)
    print(f'Клон создан, торговля выключена. Запуск: okxbot start "{new_name}"')



# A universal, read-only statistics interface. No bot-specific dispatch.
# New engines publish state/stats.json atomically. Previous engines already
# publish the same complete human-readable report in their systemd journal.
# The journal adapter exists for backwards compatibility and is never allowed
# to start a trading engine, use API credentials, or alter trading state.
_STATS_HEADER = re.compile(r'^\[.+?\] 📊 OKX (BTC|ETH|SOL) GRID STATISTICS\s*$')
_STATS_DONE = 'Все закрытые циклы сопоставлены'
_STATS_MAX_AGE = 900  # background reports every 600s + margin


def read_stats_snapshot(path, expected_instrument):
    file = path / 'state' / 'stats.json'
    if not file.exists():
        return None
    data = json.loads(file.read_text(encoding='utf-8'))
    if (data.get('schema_version') != 1 or data.get('complete') is not True
            or data.get('instrument') != expected_instrument
            or not isinstance(data.get('report'), str)
            or not data['report'].strip()
            or _STATS_DONE not in data['report']):
        raise RuntimeError('Некорректный снимок статистики (версия, полнота или пара)')
    ts = data.get('generated_at_unix')
    if isinstance(ts, bool) or not isinstance(ts, (float, int)) or not math.isfinite(ts) or ts<=0:
        raise RuntimeError('Некорректное время снимка статистики')
    if ts > time.time()+60:
        raise RuntimeError('Время снимка статистики находится в будущем')
    return data['report'], ts


def journal_last_complete_report(text, expected_instrument):
    """Extract last FINISHED report for any supported asset, without API calls."""
    coin=expected_instrument.split('-',1)[0]
    current=None
    complete=None
    for line in text.splitlines():
        marker=_STATS_HEADER.match(line)
        if marker:
            current=[line] if marker.group(1)==coin else None
            continue
        if current is None:
            continue
        current.append(line)
        if _STATS_DONE in line:
            complete='\n'.join(current)
            current=None
    return complete


def read_legacy_journal(unit, instrument):
    # Only reads service journal. No engine process, no exchange requests.
    proc=subprocess.run(
        ['journalctl','-u',unit,'--since','90 minutes ago',
         '--no-pager','-o','cat'],
        check=True,capture_output=True,text=True,timeout=15)
    return journal_last_complete_report(proc.stdout,instrument)


def with_latest_bot_dd(report, path, expected_instrument):
    """Overlay latest local 5-second drawdown sample on 10-minute cached report.

    This is read-only and does not load the engine, keys, or contact OKX.
    """
    file=path/'state'/'bot_drawdown_v067.json'
    if not file.is_file():
        return report
    try:
        from decimal import Decimal
        dd=json.loads(file.read_text(encoding='utf-8'))
        if dd.get('schema')!=1 or dd.get('instrument')!=expected_instrument:
            return report
        max_dd=Decimal(str(dd['max_drawdown_usdt']))
        if not max_dd.is_finite() or max_dd<0:
            return report
        at=datetime.fromtimestamp(float(dd['max_drawdown_at']),timezone.utc)
        sampled=datetime.fromtimestamp(float(dd['last_at']),timezone.utc)
        if sampled.timestamp()>time.time()+60:
            return report
        lines=[line for line in report.splitlines()
               if not line.startswith(('МАКС. ПРОСАДКА БОТА:', '  Зафиксирована:', '  Последний замер DD:'))]
        pos=next((i for i,line in enumerate(lines) if _STATS_DONE in line),len(lines))
        # Percentage uses the same 1x capital as the bot's main report.
        capital_line=next((x for x in lines if x.startswith('Расчётный капитал для 1x:')),None)
        pct=''
        if capital_line:
            m=re.search(r'\$([0-9,]+\.[0-9]+)',capital_line)
            if m:
                capital=Decimal(m.group(1).replace(',',''))
                if capital>0:
                    pct=f' (−{max_dd/capital*100:.4f}% от 1x)'
        latest=[f'МАКС. ПРОСАДКА БОТА:      −${max_dd:,.8f}'+pct,
                '  Зафиксирована:          '+at.strftime('%d.%m.%Y %H:%M:%S UTC'),
                '  Последний замер DD:     '+sampled.strftime('%d.%m.%Y %H:%M:%S UTC')]
        return '\n'.join(lines[:pos]+latest+lines[pos:])
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        return report


def stats_text(name):
    """Render the same universal report for CLI and the interactive viewer."""
    name, entry = find_entry(name)
    path = bot_path(entry)
    instrument = describe(entry)
    unit = unit_name(entry)
    header = f'Статистика: {name} | {instrument}\n\n'
    final_file=path/'state'/'final_stats.json'
    if final_file.is_file():
        final=json.loads(final_file.read_text(encoding='utf-8'))
        if (final.get('schema_version')!=1 or final.get('complete') is not True
                or final.get('instrument')!=instrument
                or final.get('mode') not in ('RETIRED','PANIC_DONE')
                or not isinstance(final.get('report'),str)):
            raise RuntimeError('Некорректный окончательный отчёт')
        # Historical trading report is immutable: final accounting ADDS to it.
        # Its market/open-position values are PRE-TERMINATION snapshots.
        try:
            historical=read_stats_snapshot(path,instrument)
        except (ValueError,OSError,RuntimeError):
            historical=None
        if historical is not None:
            previous=with_latest_bot_dd(historical[0],path,instrument)
            return (header+'ИСТОРИЧЕСКАЯ СТАТИСТИКА (снимок ДО завершения)\n\n'
                    +previous+'\n\n'+'━'*32+'\n'
                    +final['report'])
        return header+'Исторический снимок недоступен.\n\n'+final['report']
    if instance_terminal(entry):
        return header+('⏳ Финальный расчёт не подтверждён. '
            'Торговлю не возобновлять. Выполните GET-only --terminal-finalize '
            'на установленном движке v0.7.8.\n')
    try:
        snapshot = read_stats_snapshot(path, instrument)
    except (ValueError, OSError, RuntimeError) as e:
        raise RuntimeError(f'Ошибка снимка статистики: {e}') from e
    if snapshot is not None:
        report, ts = snapshot
        report = with_latest_bot_dd(report, path, instrument)
        try:
            from okx_position_peak import format_peak
            peak=format_peak(path,instrument)
            if peak:
                report += "\n"+"\n".join(peak)
        except (OSError,ValueError,KeyError,ImportError):
            pass
        age = max(0, int(time.time() - ts))
        freshness = 'АКТУАЛЬНА' if age <= _STATS_MAX_AGE else 'УСТАРЕЛА'
        status = 'работает' if service_running(unit) else 'остановлен'
        return header + f'Снимок: {freshness} | {age}с назад | бот {status}\n\n' + report
    report = read_legacy_journal(unit, instrument)
    if report is None:
        return header + ('Готовой статистики нет. Новый бот создаст снимок автоматически; '
                         'для старого нет завершённого отчёта в журнале за 90 минут.')
    return (header + 'Источник: журнал работающего бота (совместимость со старыми версиями).'
            ' Время отчёта указано в заголовке.\n\n' + report)


def cmd_stats(name):
    print(stats_text(name))


def cmd_list():
    entries=load_registry()
    if not entries:
        print('Ботов пока нет. Создать: okxbot new')
        return
    for name,entry in entries.items():
        if instance_terminal(entry):
            finalize_registry_if_needed(name,entry)
            entry=find_entry(name)[1]
        running=service_running(unit_name(entry))
        status=('ЗАВЕРШЁН' if instance_terminal(entry) else (
            (control_desired(entry) if running and control_supported(entry) else 'РАБОТАЕТ')
            if running else 'ОСТАНОВЛЕН'))
        print(f'{name:<26} {describe(entry):<17} {status} '
              f'[{entry["service"]}]')


def cmd_status(name):
    name, entry = find_entry(name)
    if instance_terminal(entry):
        print(stats_text(name))
        return
    from okx_ui import status
    print(status(name,bot_path(entry),unit_name(entry)))


def cmd_show(name):
    from okx_ui import settings
    name, entry = find_entry(name)
    print(settings(name,bot_path(entry),unit_name(entry)))


def cmd_logs(name,follow=False):
    name,entry=find_entry(name)
    args=['journalctl','-u',unit_name(entry),'--no-pager','-o','cat']
    args += ['-f','-n','30'] if follow else ['-n','60']
    print('Логи:', name,flush=True)
    run(args,check=False)


def _put(scr, y, x, text, width):
    """Curses-safe output even after terminal resize or on narrow SSH windows."""
    if width <= 0:
        return
    try:
        scr.addnstr(y, x, text, width)
    except curses.error:
        pass


def _viewer_frame(scr, title, lines, scroll, footer, live=False):
    h, w = scr.getmaxyx()
    scr.erase()
    if h < 5 or w < 24:
        _put(scr, 0, 0, 'Увеличьте окно терминала', max(0, w - 1))
        scr.refresh()
        return 0, 0
    _put(scr, 0, 1, title, w - 2)
    _put(scr, 1, 1, '═' * max(0, w - 2), w - 2)
    room = max(1, h - 4)
    maximum = max(0, len(lines) - room)
    scroll = min(max(scroll, 0), maximum)
    first = max(0, len(lines) - room - scroll) if live else min(scroll, maximum)
    for j, line in enumerate(lines[first:first + room]):
        _put(scr, j + 2, 1, line, w - 2)
    _put(scr, h - 1, 1, footer, w - 2)
    scr.refresh()
    return scroll, maximum


def stats_screen(scr, name):
    try:
        text = stats_text(name)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as e:
        text = f'ОШИБКА: {e}'
    lines = text.splitlines()
    scroll = 0
    while True:
        scroll, maximum = _viewer_frame(
            scr, f'OKX GRID BOT — {name} — статистика', lines, scroll,
            '↑ ↓ — прокрутка    Esc / Enter — назад')
        key = scr.get_wch()
        if key in ('\x1b', '\n', '\r', curses.KEY_ENTER, 'q'):
            return
        if key in (curses.KEY_DOWN, 'j'):
            scroll = min(maximum, scroll + 1)
        elif key in (curses.KEY_UP, 'k'):
            scroll = max(0, scroll - 1)


def live_logs_screen(scr, name):
    """Read-only journalctl -f, bounded local buffer; Esc returns to bot menu.

    journalctl runs in its own process group. Thus Ctrl+C during live viewing
    never sends SIGINT to the systemd trading service or exits the manager.
    """
    _, entry = find_entry(name)
    args = ['journalctl', '-u', unit_name(entry), '--no-pager', '-o', 'cat', '-f', '-n', '60']
    lines = deque(['Подключение к журналу…'], maxlen=3000)
    child = None
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    partial = ''
    scroll = 0
    selector = selectors.DefaultSelector()
    try:
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        scr.timeout(100)
        while True:
            footer = ('LIVE | ↑ ↓ — прокрутка    Esc / q / Ctrl+C — назад'
                      if child.poll() is None else
                      'Поток завершился | Esc / q — назад')
            scroll, maximum = _viewer_frame(
                scr, f'OKX GRID BOT — {name} — логи', list(lines), scroll, footer,
                live=True)
            key = None
            # get_wch() raises curses.error on timeout. This is not a crash.
            try:
                key = scr.get_wch()
            except curses.error:
                pass
            if key in ('\x1b', 'q', '\x03'):
                return
            if key in (curses.KEY_UP, 'k'):
                scroll = min(maximum, scroll + 1)
            elif key in (curses.KEY_DOWN, 'j'):
                scroll = max(0, scroll - 1)
            for _key, _ in selector.select(0):
                while True:
                    try:
                        raw = os.read(child.stdout.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not raw:
                        try:
                            selector.unregister(child.stdout)
                        except KeyError:
                            pass
                        break
                    partial += decoder.decode(raw)
                    parts = partial.split('\n')
                    partial = parts.pop()
                    for line in parts:
                        lines.append(line.rstrip('\r'))
                    if scroll:
                        scroll = min(3000, scroll + len(parts))
            if child.poll() is not None and partial:
                lines.append(partial)
                partial = ''
    except KeyboardInterrupt:
        return
    finally:
        scr.timeout(-1)
        selector.close()
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if child.stdout:
                child.stdout.close()


def _text_screen(scr, title, text):
    lines = text.splitlines()
    scroll = 0
    while True:
        scroll, max_scroll = _viewer_frame(scr, title, lines, scroll,
                           '↑ ↓ — прокрутка    Esc / Enter — назад')
        key = scr.get_wch()
        if key in ('\x1b', '\n', '\r', curses.KEY_ENTER, 'q'):
            return
        if key in (curses.KEY_DOWN, 'j'): scroll = min(max_scroll, scroll + 1)
        elif key in (curses.KEY_UP, 'k'): scroll = max(0, scroll - 1)


def _capture_status(name):
    from okx_ui import status
    name, entry = find_entry(name)
    return status(name,bot_path(entry),unit_name(entry))


def _capture_settings(name):
    from okx_ui import settings
    name, entry = find_entry(name)
    return settings(name,bot_path(entry),unit_name(entry))


def _pause_return():
    if sys.stdin.isatty():
        try:
            input('\nEnter — вернуться в меню…')
        except EOFError:
            pass


def cmd_monitor(name, kind, action):
    """Monitor toggle: preflight and managed-only changes, never touch LIVE bot."""
    from okx_monitor import monitor_action
    _, entry = find_entry(name)
    return monitor_action(entry['service'], bot_path(entry), kind, action, SOURCE)

def monitor_enabled(entry, kind):
    from okx_monitor import monitor_state
    st = monitor_state(entry['service'], bot_path(entry), kind)
    return bool(st['managed'] or st['external'])


def monitor_label(entry, kind):
    from okx_monitor import label
    return label(entry['service'], bot_path(entry), kind)

def interactive_bot_menu(name):
    """Single menu; only controls supported by the selected trading engine."""
    while True:
        _,entry=find_entry(name)
        is_new=control_supported(entry)
        terminal=instance_terminal(entry)
        mode=control_desired(entry)
        choices=['Статистика','Статус','Настройки','Логи (LIVE)',
                 'Burst Monitor: '+monitor_label(entry,'burst'),
                 'Execution Monitor: '+monitor_label(entry,'execution')]
        active=service_running(unit_name(entry))
        if is_new and not terminal and active:
            if mode in ('PAUSE','SOFT'):
                choices.append('Возобновить работу')
            if mode=='RUN':
                choices.append('Поставить на паузу')
            if mode in ('RUN','PAUSE'):
                choices.append('Soft Stop — отменить BUY LIMIT')
            if mode in ('RUN','PAUSE','SOFT'):
                choices.extend(['Hard Stop — завершить бота, отменить все ордера',
                                'Panic Sell — завершить и закрыть позицию'])
        if terminal:
            choices.append('Создать нового бота-клона')
        if not active and not terminal and mode=='RUN':
            choices.append('Запустить')
        choices.append('Назад')
        j=curses.wrapper(lambda s: wizard.menu(s,'OKX GRID BOT — '+name,choices))
        if j is None or choices[j]=='Назад':return
        chosen=choices[j]
        if chosen.startswith(('Burst Monitor:','Execution Monitor:')):
            kind='burst' if chosen.startswith('Burst') else 'execution'
            try:
                cmd_monitor(name,kind,'off' if monitor_enabled(entry,kind) else 'on')
            except Exception as exc:
                curses.wrapper(lambda s: _text_screen(
                    s,'OKX GRID BOT — '+name+' — мониторинг',
                    'Действие не выполнено.\n\n'+str(exc)))
        elif chosen=='Статистика':
            curses.wrapper(lambda s: stats_screen(s,name))
        elif chosen=='Логи (LIVE)':
            curses.wrapper(lambda s: live_logs_screen(s,name))
        elif chosen in ('Статус','Настройки'):
            try:
                content=_capture_status(name) if chosen=='Статус' else _capture_settings(name)
            except Exception as exc:content='ОШИБКА: '+str(exc)
            curses.wrapper(lambda s: _text_screen(s,'OKX GRID BOT — '+name+' — '+chosen,content))
        else:
            try:
                if chosen=='Поставить на паузу':cmd_control(name,'PAUSE')
                elif chosen=='Возобновить работу':cmd_control(name,'RUN')
                elif chosen.startswith('Soft Stop'):cmd_control(name,'SOFT')
                elif chosen.startswith('Hard Stop'):cmd_control(name,'HARD')
                elif chosen.startswith('Panic Sell'):cmd_control(name,'PANIC')
                elif chosen=='Запустить':cmd_start(name)
                elif chosen=='Создать нового бота-клона':
                    candidate=input('Имя нового бота-клона: ').strip()
                    cmd_clone(name,candidate)
            except Exception as exc:
                print('ОШИБКА:',exc,file=sys.stderr)
            _pause_return()



TELEGRAM_MONITOR_UNIT = 'okx-telegram-monitor.service'


def telegram_monitor_state():
    # Only systemd metadata; does not access the exchange or trading services.
    result = subprocess.run(
        ['systemctl', 'show', TELEGRAM_MONITOR_UNIT, '--no-pager',
         '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
         '-p', 'UnitFileState', '-p', 'MainPID'],
        capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise RuntimeError('Ошибка systemctl show: ' + result.stderr.strip()[:300])
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def telegram_monitor_label():
    try:
        state = telegram_monitor_state()
        if state.get('LoadState') != 'loaded':
            return 'НЕ УСТАНОВЛЕН'
        return 'РАБОТАЕТ' if state.get('ActiveState') == 'active' else 'ОСТАНОВЛЕН'
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return 'ОШИБКА СТАТУСА'


def telegram_monitor_report():
    state = telegram_monitor_state()
    return '\n'.join([
        'Служба: ' + TELEGRAM_MONITOR_UNIT,
        'Установлена: ' + ('да' if state.get('LoadState') == 'loaded' else 'нет'),
        'Состояние: ' + state.get('ActiveState', '?') + ' / ' + state.get('SubState', '?'),
        'Автозапуск: ' + state.get('UnitFileState', '?'),
        'PID: ' + state.get('MainPID', '?'),
        '',
        'Это общий Telegram-монитор, а не торговый процесс.',
    ])


def interactive_telegram_menu():
    while True:
        choices = ['Показать статус', 'Запустить и включить автозапуск', 'Назад']
        selected = curses.wrapper(
            lambda scr: wizard.menu(scr, 'OKX GRID BOTS — Telegram Monitor', choices))
        if selected is None or selected == 2:
            return
        try:
            if selected == 1:
                state = telegram_monitor_state()
                if state.get('LoadState') != 'loaded':
                    raise RuntimeError('Служба Telegram-монитора не установлена.')
                # Only the fixed Telegram unit; does not touch trading bots.
                run(['systemctl', 'enable', '--now', TELEGRAM_MONITOR_UNIT], timeout=45)
                if telegram_monitor_state().get('ActiveState') != 'active':
                    raise RuntimeError('systemctl завершился, но монитор не активен')
            text = telegram_monitor_report()
        except (OSError, RuntimeError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as exc:
            text = 'Действие не выполнено: ' + str(exc)
        curses.wrapper(lambda scr: _text_screen(
            scr, 'OKX GRID BOTS — Telegram Monitor', text))

def interactive_main(env_file):
    while True:
        entries = load_registry()
        titles = ['Создать нового бота', 'Список ботов',
                  'Telegram Monitor: ' + telegram_monitor_label(),
                  account_soft_menu_label()]
        names = sorted(entries,key=lambda name: (not service_running(unit_name(entries[name])),name.casefold()))
        titles.extend(f'{name} — {describe(entries[name])}' for name in names)
        titles.append('Выход')
        i = curses.wrapper(lambda s: wizard.menu(s, 'OKX GRID BOTS', titles))
        if i is None or i == len(titles) - 1:
            return
        if i == 0:
            try:
                cmd_new(env_file)
            except Exception as exc:
                print('ОШИБКА:', exc, file=sys.stderr)
            _pause_return()
        elif i == 1:
            with io.StringIO() as buf, contextlib.redirect_stdout(buf):
                cmd_list()
                text = buf.getvalue()
            curses.wrapper(lambda s: _text_screen(s, 'OKX GRID BOTS — список', text))
        elif i == 2:
            interactive_telegram_menu()
        elif i == 3:
            try:
                cmd_account_reset()
            except Exception as exc:
                print('ОШИБКА:',exc,file=sys.stderr)
            _pause_return()
        else:
            interactive_bot_menu(names[i - 4])


def main(argv=None):
    p=argparse.ArgumentParser(description='Управление именованными OKX Grid ботами')
    sub=p.add_subparsers(dest='action')
    n=sub.add_parser('new',help='создать нового бота через опросник')
    n.add_argument('--env-file',default=str(BASE/'bot01/.env'))
    for cmd in ('stats','status','show','start','stop','pause','resume','soft','hard','panic','logs','audit'):
        sp=sub.add_parser(cmd);sp.add_argument('name')
        if cmd=='logs':sp.add_argument('-f','--follow',action='store_true')
    sub.add_parser('list')
    sub.add_parser('account-reset', help='проверить и снять общую блокировку после ручной проверки маржи')
    clone=sub.add_parser('clone',help='новый экземпляр из завершённого бота')
    clone.add_argument('source_name');clone.add_argument('new_name')
    a=sub.add_parser('attach',help='дать имя существующему боту без его перезапуска')
    a.add_argument('name');a.add_argument('--path',required=True);a.add_argument('--service',required=True)
    mon=sub.add_parser('monitor',help='read-only monitor for selected instance')
    mon.add_argument('name');mon.add_argument('kind',choices=('burst','execution'))
    mon.add_argument('setting',choices=('on','off','status','report'))
    opts=p.parse_args(argv)
    cmd=opts.action
    name=getattr(opts,'name',None)
    if cmd is None:
        if not sys.stdin.isatty():
            p.print_help()
            return
        interactive_main(getattr(opts, 'env_file', str(BASE/'bot01/.env')))
        return
    if cmd=='new':cmd_new(getattr(opts,'env_file',str(BASE/'bot01/.env')))
    elif cmd=='list':cmd_list()
    elif cmd=='account-reset':cmd_account_reset()
    elif cmd=='attach':cmd_attach(opts.name,opts.path,opts.service)
    elif cmd=='stats':cmd_stats(name)
    elif cmd=='status':cmd_status(name)
    elif cmd=='show':cmd_show(name)
    elif cmd=='start':cmd_start(name)
    elif cmd in ('stop','soft'):cmd_stop(name)
    elif cmd=='pause':cmd_control(name,'PAUSE')
    elif cmd=='resume':cmd_control(name,'RUN')
    elif cmd=='hard':cmd_control(name,'HARD')
    elif cmd=='panic':cmd_control(name,'PANIC')
    elif cmd=='clone':cmd_clone(opts.source_name,opts.new_name)
    elif cmd=='logs':cmd_logs(name,getattr(opts,'follow',False))
    elif cmd=='audit':audit_entry(find_entry(name)[1])
    elif cmd=='monitor':cmd_monitor(name,opts.kind,opts.setting)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('\nОтменено.')
        sys.exit(130)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print('ОШИБКА:',e,file=sys.stderr)
        sys.exit(1)
