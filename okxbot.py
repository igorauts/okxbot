#!/usr/bin/env python3
"""Named OKX grid-bot instances: lifecycle via systemd, read-only statistics.

This controller does not place/cancel exchange orders. LIVE execution stays
behind bot config + two explicit environment guardrails and user consent.
"""
import argparse
import curses
import codecs
import contextlib
import fcntl
import functools
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
from okx_archive_reader import state_file
from okx_version import RELEASE_VERSION, ENGINE_VERSION, STATE_SCHEMA, validate_version_ids

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


def display_registry():
    """Tolerate single broken records for READ-ONLY pages only.

    NEVER use for a control command, SOFT account-reset, bot creation, or
    registry writes. Corrupt records stay on disk and are not silently erased.
    """
    try:
        raw=load_registry()
    except (OSError,ValueError,RuntimeError) as exc:
        return {},['Registry unavailable: '+type(exc).__name__]
    good={};warnings=[]
    for name,entry in raw.items():
        if not isinstance(name,str) or not isinstance(entry,dict):
            warnings.append(str(name)[:40]+': malformed registry record');continue
        try:
            unit=unit_name(entry)
            root=bot_path(entry)
            if (entry.get('service')!='okx-grid-'+root.name or
                    not (SYSTEMD_DIR/unit).is_file()):
                raise ValueError('registry unit/path mismatch')
            good[name]=entry
        except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
            warnings.append(name[:40]+': '+type(exc).__name__)
    return good,warnings


def display_instances():
    """Read-only best effort with explicit warning; not authoritative for PANIC."""
    from types import SimpleNamespace
    entries,warnings=display_registry()
    bots=[]
    for name,entry in entries.items():
        try:
            root=bot_path(entry)
            cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
            if not isinstance(cfg,dict) or not re.fullmatch(
                    r'[A-Z0-9]+-USDT-SWAP',str(cfg.get('instrument',''))):
                raise ValueError('invalid instrument')
            bots.append(SimpleNamespace(display_name=name,root=root,
                instrument=cfg['instrument'],
                systemd_state='active' if service_running(unit_name(entry)) else 'inactive'))
        except (OSError,ValueError,RuntimeError) as exc:
            warnings.append(name[:40]+': '+type(exc).__name__)
    return bots,warnings

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


def latest_engine(source=SOURCE):
    """Engine manifest tracks ENGINE_VERSION, never maintenance package version."""
    source=Path(source)
    path=source/'okx_bot.py'
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('Нет установленного торгового движка okx_bot.py')
    compile(path.read_bytes(),str(path),'exec')
    validate_version_ids(RELEASE_VERSION,ENGINE_VERSION)
    ver=ENGINE_VERSION
    manifest=json.loads((source/'engine_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version')!=STATE_SCHEMA or manifest.get('current_version')!=ver:
        raise RuntimeError('Несоответствие VER и текущей версии манифеста')
    entry=manifest.get('engines',{}).get(ver,{})
    if entry.get('file')!='okx_bot.py':
        raise RuntimeError('Манифест не указывает на okx_bot.py')
    import hashlib
    if hashlib.sha256(path.read_bytes()).hexdigest()!=entry.get('sha256'):
        raise RuntimeError('SHA256 движка не соответствует манифесту')
    return 'okx_bot.py',ver



def engine_file(path):
    """Only current engine can run. Archive is read-only."""
    p=Path(path)/'okx_bot.py'
    if not p.is_file() or p.is_symlink():
        raise RuntimeError('Current engine not installed')
    return 'okx_bot.py'


def latest_dependencies(engine_name,version,source=SOURCE):
    """Manifest-driven future dependencies; no manager changes per release."""
    path=Path(source)/'engine_manifest.json'
    if not path.is_file():
        raise RuntimeError('Engine manifest missing')
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
    if (created/'state').is_dir() and any((created/'state').iterdir()):
        raise RuntimeError('Never upgrade an existing trading state in wizard')
    shutil.copy2(Path(source)/name,created/name)
    for dependency in latest_dependencies(name,version,source):
        shutil.copy2(Path(source)/dependency,created/dependency)
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
            'Environment=OKX_GRID_LIVE=CONFIRM\n'
            'Environment=OKX_GRID_PROBE=FULL_25\n'
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
           if k not in ('OKX_GRID_LIVE','OKX_GRID_PROBE','OKX_GRID_STATS')}
    env['OKX_GRID_AUDIT'] = 'READ_ONLY'
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


def registration_guard(fn):
    """Serialize account-level new/attach operations, never replace live inode."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(BASE/'.okxbot_registration.lock',
                   os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            return fn(*args,**kwargs)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)
    return wrapped


def require_account_capacity(entries, *, proposed_instrument=None, proposed_name=None):
    """Cap nonterminal (including stopped/restartable) bots at ten per account."""
    if not isinstance(entries,dict):
        raise RuntimeError('Malformed account registry')
    active=[];instruments=set()
    for name,entry in entries.items():
        if not isinstance(name,str) or not isinstance(entry,dict):
            raise RuntimeError('Malformed account registry entry')
        try:
            root=bot_path(entry)  # malformed/missing instance blocks a registry write
            cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise RuntimeError('Unverifiable account registry entry') from exc
        instrument=cfg.get('instrument')
        if not isinstance(instrument,str) or not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP',instrument):
            raise RuntimeError('Bad instrument in account registry')
        # ONLY explicitly terminal, inactive instances can be excluded.
        # A stopped RUN/SOFT bot still occupies a slot and cannot be replaced.
        if instance_terminal(entry) and not service_running(unit_name(entry)):
            continue
        if instrument in instruments:
            raise RuntimeError('Duplicate nonterminal instrument in NET account')
        instruments.add(instrument)
        active.append(name)
    if proposed_instrument is not None:
        if proposed_instrument in instruments:
            raise RuntimeError('Only one nonterminal bot per NET instrument')
        active.append(proposed_name or proposed_instrument)
    if len(active)>10:
        raise RuntimeError('Account limit: maximum ten nonterminal bots')
    return len(active)



# Single-account Reader settings for newly CREATED (not already running) bots.
# Avoid copying stale clone .env or silently defaulting a fresh bot to direct GET.
READER_SOCKET = '/run/okx-position-reader/positions.sock'
READER_ENV_KEYS = ('OKX_POSITION_READER_SOCKET',
                   'OKX_POSITION_READER_QUOTA_SCHEDULING',
                   'OKX_POSITION_READER_PRIORITY_ACK')


def validated_newbot_env(env_file):
    chosen, raw = wizard.read_env(env_file)
    master, _ = wizard.read_env(BASE / '.okxbot_account.env')
    for secret in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE'):
        if chosen[secret] != master[secret]:
            raise RuntimeError('New bot credentials differ from single OKX account')
    if (master.get('OKX_POSITION_READER_SOCKET') != READER_SOCKET or
            master.get('OKX_POSITION_READER_QUOTA_SCHEDULING') != 'mixed-fifo' or
            master.get('OKX_POSITION_READER_PRIORITY_ACK')):
        raise RuntimeError('Shared Reader account template invalid; no new bot')
    # Retain chosen non-Reader options and credentials, never duplicate keys.
    lines = [line for line in raw.splitlines()
             if not ('=' in line and line.split('=', 1)[0].strip() in READER_ENV_KEYS)]
    lines.extend(('OKX_POSITION_READER_SOCKET=' + READER_SOCKET,
                  'OKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo'))
    merged = '\n'.join(lines) + '\n'
    chosen = dict(chosen)
    chosen['OKX_POSITION_READER_SOCKET'] = READER_SOCKET
    chosen['OKX_POSITION_READER_QUOTA_SCHEDULING'] = 'mixed-fifo'
    chosen.pop('OKX_POSITION_READER_PRIORITY_ACK', None)
    return chosen, merged


def reader_registration_ready(name, entry, cfg, *, timeout_sec=12):
    """Root-only, GET-free Reader registry refresh BEFORE first LIVE START.

    Does not restart Reader or any trading process. The Reader revalidates its
    authoritative registry; never accept a client-supplied registration list.
    """
    import socket
    instrument = cfg.get('instrument')
    if not isinstance(instrument, str) or not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', instrument):
        raise RuntimeError('Invalid instrument for Reader registration')
    # An existing running engine is not touched. For stopped old instances,
    # preserve the durable direct-GET fallback if Reader is genuinely down.
    bot_env, _ = wizard.read_env(bot_path(entry) / '.env')
    if bot_env.get('OKX_POSITION_READER_SOCKET') != READER_SOCKET:
        if cfg.get('live_trading') is not True:
            raise RuntimeError('New bot lacks Shared Reader configuration')
        return False
    deadline = time.monotonic() + timeout_sec
    last = 'Reader not reachable'
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(min(3.0, max(.2, deadline-time.monotonic())))
                sock.connect(READER_SOCKET)
                payload = json.dumps({'schema': 1, 'op': 'refresh_registry',
                                      'instrument': instrument, 'bot': name}, separators=(',', ':'))
                sock.sendall(payload.encode() + b'\n')
                data = b''
                while b'\n' not in data and len(data) < 4096:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            reply = json.loads(data.split(b'\n', 1)[0])
            if (isinstance(reply, dict) and reply.get('ok') is True and
                    isinstance(reply.get('data'), dict) and
                    reply['data'].get('registered') is True and
                    reply['data'].get('instrument') == instrument and
                    reply['data'].get('bot') == name):
                print(f'Shared Reader READY: {name} | {instrument}')
                return True
            last = 'Reader did not confirm instrument'
        except (OSError, ValueError, TypeError, KeyError) as exc:
            last = type(exc).__name__
        time.sleep(min(.15, max(0, deadline-time.monotonic())))
    if cfg.get('live_trading') is not True:
        raise RuntimeError('New bot NOT started: Shared Reader not ready (' + last + ')')
    print('WARNING: Reader unavailable; existing bot may use gated direct GET')
    return False

@registration_guard
def cmd_new(env_file):
    latest_engine()  # Fail closed BEFORE a wizard can write a new bot.
    entries = load_registry()
    require_account_capacity(entries)  # includes malformed registry fail-closed
    if sum(not instance_terminal(e) or service_running(unit_name(e))
           for e in entries.values())>=10:
        raise RuntimeError('Account limit: maximum ten nonterminal bots')
    env, raw = validated_newbot_env(env_file)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError('Для опросника нужен интерактивный SSH-терминал')
    ctx = curses.wrapper(lambda scr: wizard.wizard(scr, env, entries.keys()))
    if ctx is None:
        print('Отменено. Нового бота нет.')
        return
    name = wizard.validate_name(ctx['name'], entries.keys())
    require_account_capacity(entries,proposed_instrument=ctx['instrument'],proposed_name=name)
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


@registration_guard
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
    cfg=json.loads((source/'config.json').read_text(encoding='utf-8'))
    require_account_capacity(entries,proposed_instrument=cfg.get('instrument'),proposed_name=name)
    entries[name]={'slug':slug,'path':str(source),'service':service}
    save_registry(entries)
    print(f'Бот «{name}» привязан: {source} ({service})')
    print('Рабочий процесс, config.json и systemd НЕ изменены.')


def cmd_start(name):
    # Account capacity is a CREATION/ATTACH invariant only. Existing bots
    # must remain startable even if an unrelated archived registry path moved.
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
    reader_registration_ready(name,entry,cfg)
    print(f'\nВНИМАНИЕ: запуск «{name}» означает РЕАЛЬНЫЕ ордера на OKX.')
    print(f'Пара: {cfg.get("instrument")}  |  GRID: {cfg.get("grid_step")}  |  TP: {cfg.get("take_profit")}')
    if input(f'Для запуска введи точно START {name}: ').strip()!=f'START {name}':
        print('Запуск отменён. Ничего не менялось.')
        return
    reader_registration_ready(name,entry,cfg)
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
               and Path(token).name=='okx_bot.py'
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



# Account-wide margin latch recovery
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
            state = json.loads(state_file(bot.root).read_text(encoding='utf-8'))
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
    print(f'\nОбщая блокировка СНЯТА. {len(summary)} ботов по-прежнему SOFT.')
    print('Возобновляй нужные боты по одному через меню «Возобновить работу».')
    print('Если проблема с маржой повторится, блокировка автоматически восстановится.')


def _current_account_bots():
    """Authoritative discovery: unknown running services are fatal."""
    return discover_single_account(BASE, SYSTEMD_DIR)


def _account_env_file():
    account=BASE/'.okxbot_account.env'
    if account.is_file() and not account.is_symlink():
        return account
    # The VPS is deliberately bound to ONE OKX account; fallback credentials
    # are from a currently managed instance only, never arbitrary user input.
    bots=[b for b in display_instances()[0] if b.systemd_state=='active']
    if not bots:
        raise RuntimeError('Нет ключа для безопасного GET аккаунта')
    env=bots[0].root/'.env'
    if env.is_symlink() or not env.is_file():
        raise RuntimeError('Нет корректного файла API-ключей')
    return env


def _account_overview_data():
    from okx_account_ui import get_account_snapshot
    bots,warnings=display_instances()
    env=_account_env_file()  # GET-only; never derives trading authority.
    snapshot=get_account_snapshot(env)
    if warnings:
        snapshot['registry_warnings']=warnings
        snapshot['registry_partial']=True
    return snapshot,bots,_account_soft_latch_data()


def _account_overview_lines():
    from okx_account_ui import format_account
    return format_account(*_account_overview_data())


def cmd_account_status():
    for line in _account_overview_lines(): print(line)


def _panic_all_preflight():
    """Read-only individual verification. Incomplete independent bots are skipped.

    Unknown active instruments, duplicate NET pairs or broken service identities
    still fail globally; they cannot be proven independent of a target bot.
    """
    from okx_panic_all import survey
    plan,skipped,identity=survey(BASE,REGISTRY,SYSTEMD_DIR,control_supported)
    _panic_all_preflight.last_skipped=skipped
    _panic_all_preflight.last_identity=identity
    return plan


def cmd_panic_all():
    """One confirmation. Each eligible bot gets its OWN durable PANIC request.

    Partial completion is explicit. A rejected/unknown bot is NEVER assumed
    flat, nor does it cancel a safe command for an independent instrument.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError('PANIC ALL разрешён только в интерактивном терминале')
    plan=_panic_all_preflight()
    skipped=getattr(_panic_all_preflight,'last_skipped',())
    identity=getattr(_panic_all_preflight,'last_identity',())
    print('\n🚨 PANIC ALL — НЕОБРАТИМОЕ ЗАКРЫТИЕ УПРАВЛЯЕМЫХ БОТОВ')
    for name,instrument,service,pid,mode in plan:
        print(f'  {name}: {instrument} (PID {pid}, {mode})')
    for unit,reason in skipped:
        print(f'  ⚠️ SKIP {unit}: {reason}')
    print('Каждый подтверждённый бот получает независимый PANIC. SKIP не закрыт.')
    print('Это запросы к двигателям, НЕ подтверждение фактической ликвидации.')
    if input('Для подтверждения введи PANIC ALL: ').strip()!='PANIC ALL':
        print('Отменено. Команды не отправлены.')
        return
    if (_panic_all_preflight()!=plan or
            getattr(_panic_all_preflight,'last_skipped',())!=skipped or
            getattr(_panic_all_preflight,'last_identity',())!=identity):
        raise RuntimeError('Состав ботов/PID/режим изменился; ни одной команды не отправлено')
    sent=[];failed=[]
    from okx_panic_all import survey
    expected_names={row[0] for row in plan}
    for name,instrument,service,pid,mode in plan:
        try:
            # Verify the live universe again, but account for previously
            # submitted PANIC intents now being in a different control mode.
            _,_,current_identity=survey(BASE,REGISTRY,SYSTEMD_DIR,
                                          control_supported)
            if current_identity!=identity:
                raise RuntimeError('Состав/PID служб изменился во время отправки')
            _,entry=find_entry(name)
            if not service_running(service):
                raise RuntimeError('Служба уже остановлена')
            current_pid=subprocess.run(['systemctl','show',service,
                '-p','MainPID','--value'],capture_output=True,text=True,
                timeout=10,check=True).stdout.strip()
            if current_pid!=str(pid) or read_control(entry).get('mode')!=mode:
                raise RuntimeError('Состояние бота изменилось после preflight')
            write_control(entry,'PANIC')
            sent.append(name)
            print(f'✓ {name}: durable PANIC запрос сохранён')
        except Exception as exc:
            failed.append((name,str(exc)))
            print(f'⛔ {name}: PANIC не отправлен: {exc}',file=sys.stderr)
            # If universe changed, no OTHER target can be trusted; fail closed.
            if 'Состав/PID служб изменился' in str(exc):break
    print(f'ИТОГ PANIC ALL: отправлено {len(sent)}/{len(plan)}, '
          f'пропущено {len(skipped)}, ошибок {len(failed)}.')
    if skipped or failed:
        print('⛔ PANIC ALL ЧАСТИЧНЫЙ: пропущенные/ошибочные боты НЕ подтверждены закрытыми.',
              file=sys.stderr)
    print('Ожидай независимо подтверждённого PANIC_DONE каждого адресата.')
    if skipped or failed:
        raise RuntimeError('PANIC ALL частично выполнен, не все боты охвачены: '+
                           str((skipped,failed))[:500])


def _account_detail_menu(scr, initial):
    """Live age counter, fixed refresh/back actions, no background OKX polling."""
    from okx_account_ui import format_account
    data=initial
    error=''
    idx=0
    scroll=0
    scr.timeout(1000)
    try:
        while True:
            lines=format_account(*data)
            if error:
                lines+=['', 'Ошибка обновления: '+error]
            h,w=scr.getmaxyx()
            scr.erase()
            if h<12 or w<38:
                _put(scr,0,0,'Увеличьте окно терминала',max(0,w-1))
                scr.refresh()
            else:
                _put(scr,0,1,'OKX GRID BOT — Управление аккаунтом',w-2)
                _put(scr,1,1,'═'*(w-2),w-2)
                room=max(1,h-8)
                maximum=max(0,len(lines)-room)
                scroll=min(max(scroll,0),maximum)
                for row,line in enumerate(lines[scroll:scroll+room]):
                    _put(scr,2+row,1,line,w-2)
                if maximum:
                    _put(scr,h-6,1,f'Данные: {scroll+1}–{min(len(lines),scroll+room)} '
                                     f'из {len(lines)} (PgUp/PgDn)',w-2)
                for j,label in enumerate(('Обновить данные','Вернуться')):
                    _put(scr,h-4+j,1,('❯ ' if idx==j else '  ')+label,w-2)
                _put(scr,h-1,1,'↑ ↓ — выбор    Enter — действие    PgUp/PgDn — прокрутка    Esc — назад',w-2)
                scr.refresh()
            try:
                key=scr.get_wch()
            except curses.error:
                continue  # 1-second redraw refreshes age, never calls OKX.
            if key in ('\x1b','q'):
                return
            if key in (curses.KEY_DOWN,'j'):
                idx=(idx+1)%2
            elif key in (curses.KEY_UP,'k'):
                idx=(idx-1)%2
            elif key==curses.KEY_NPAGE:
                scroll=min(maximum,scroll+max(1,h-9))
            elif key==curses.KEY_PPAGE:
                scroll=max(0,scroll-max(1,h-9))
            elif key in ('\r','\n',curses.KEY_ENTER,'r','R'):
                if idx==1 and key not in ('r','R'):
                    return
                try:
                    data=_account_overview_data()
                    error=''
                    scroll=0
                except Exception as exc:
                    error=str(exc)[:140]
    finally:
        scr.timeout(-1)


def interactive_account_menu():
    while True:
        initial=None
        try:
            from okx_account_ui import format_account
            initial=_account_overview_data()
            preview=format_account(*initial)[:7]+['','Подробные позиции — в первом пункте.']
        except Exception as exc:
            preview=['Не удалось получить полный снимок OKX:',str(exc)[:160],
                     '','PANIC ALL не зависит от доступности снимка баланса.']
        choices=['Баланс, позиции и плечи','Обновить данные',
                 'Сбросить SOFT-блокировку',
                 '🚨 PANIC ALL — аварийное закрытие','Назад']
        idx=curses.wrapper(lambda s:wizard.menu(s,'OKX GRID BOT — Аккаунт',choices,preview))
        if idx is None or idx==4:return
        if idx==0:
            if initial is not None:
                curses.wrapper(lambda s:_account_detail_menu(s,initial))
            continue
        if idx==1:continue
        try:
            if idx==2:cmd_account_reset()
            elif idx==3:cmd_panic_all()
        except Exception as exc:
            print('ОШИБКА:',exc,file=sys.stderr)
        _pause_return()


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
            raise RuntimeError(f'PANIC запрещён: {conflict} одновременно торгует '
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
            'HARD':'HARD','PANIC':'PANIC'}[mode]
    confirm=f'{marker} {name}'
    if input(f'Введите точно {confirm}: ').strip()!=confirm:
        print('Отменено. Бот продолжает работать в прежнем режиме.')
        return
    if mode=='RUN' and account_soft_active():
        raise RuntimeError('Общий SOFT STOP снова активен. RESUME не отправлен.')
    write_control(entry,mode)
    print('Запрос принят. Движок подтвердит состояние после сверки OKX.')
    if mode in ('HARD','PANIC'):
        print(f'Статус: okxbot status "{name}" | Логи: okxbot logs "{name}" -f')


def cmd_stop(name):
    # Uniform SOFT semantics; legacy engines are not supported.
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
    _, clone_env_raw = validated_newbot_env(BASE / '.okxbot_account.env')
    dest.mkdir(mode=0o700)
    (dest/'state').mkdir(mode=0o700)
    (dest/'logs').mkdir(mode=0o700)
    for f in (selected_engine,'requirements.txt'):
        shutil.copy2(SOURCE/f,dest/f)
    for dependency in latest_dependencies(selected_engine,selected_version):
        shutil.copy2(SOURCE/dependency,dest/dependency)
    (dest/'.env').write_text(clone_env_raw, encoding='utf-8')
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
_STATS_HEADER = re.compile(r'^\[.+?\] 📊 OKX ([A-Z0-9]{2,32}) GRID STATISTICS\s*$')
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
    file=path/'state'/'bot_drawdown.json'
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
            return (header+final['report']+'\n\n'+'━'*32+'\n'
                    +'ИСТОРИЧЕСКАЯ СТАТИСТИКА (до завершения)\n\n'+previous)
        return header+'Исторический снимок недоступен.\n\n'+final['report']
    if instance_terminal(entry):
        return header+('⏳ Финальный расчёт не подтверждён. '
            'Торговлю не возобновлять. Выполните GET-only --terminal-finalize '
            'на установленном торговом движке.\n')
    try:
        snapshot = read_stats_snapshot(path, instrument)
    except (ValueError, OSError, RuntimeError) as e:
        raise RuntimeError(f'Ошибка снимка статистики: {e}') from e
    if snapshot is not None:
        report, ts = snapshot
        # Structured report, otherwise preserve the read-only historical screen.
        data=json.loads((path/'state'/'stats.json').read_text(encoding='utf-8'))
        if isinstance((data.get('performance') or data.get('performance_v0724')),dict):
            from okx_stats_ui import format_stats
            heartbeat=False
            try:
                runfile=path/'state'/'runtime_stats.json'
                if runfile.is_file() and not runfile.is_symlink():
                    rt=json.loads(runfile.read_text(encoding='utf-8'))
                    heartbeat=0<=time.time()-float(rt['last'])<=90
            except (OSError,ValueError,TypeError,KeyError):pass
            content=format_stats(path,instrument,(data.get('performance') or data.get('performance_v0724')),
                         bot_active=service_running(unit),heartbeat_fresh=heartbeat,
                         snapshot_at=ts)
            return content
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
        if status=='работает':
            ack=read_ack(entry)
            if ack.get('mode')=='RUN' and ack.get('phase') in (
                    'BUY_HOLD_TP_FAILURE','BUY_HOLD_RECONCILIATION'):
                status='BUY заблокированы ('+str(ack['phase'])+')'
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
    entries,warnings=display_registry()
    for issue in warnings:print('⚠️ Реестр: '+issue)
    if not entries:
        print('Ботов пока нет. Создать: okxbot new')
        return
    for name,entry in entries.items():
        try:
            running=service_running(unit_name(entry))
            status=('ЗАВЕРШЁН' if instance_terminal(entry) else (
                (control_desired(entry) if running and control_supported(entry) else 'РАБОТАЕТ')
                if running else 'ОСТАНОВЛЕН'))
            print(f'{name:<26} {describe(entry):<17} {status} '
                  f'[{entry["service"]}]')
        except (OSError,RuntimeError,ValueError,KeyError,TypeError) as exc:
            print(f'⚠️ {name}: запись недоступна ({type(exc).__name__})')


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


def _viewer_frame(scr, title, lines, scroll, footer, live=False, paint_stats=False):
    h, w = scr.getmaxyx()
    scr.erase()
    if h < 5 or w < 24:
        _put(scr, 0, 0, 'Увеличьте окно терминала', max(0, w - 1))
        scr.refresh()
        return 0, 0
    if not paint_stats:
        _put(scr, 0, 1, title, w - 2)
        _put(scr, 1, 1, '═' * max(0, w - 2), w - 2)
    room = max(1, h - (2 if paint_stats else 4))
    maximum = max(0, len(lines) - room)
    scroll = min(max(scroll, 0), maximum)
    first = max(0, len(lines) - room - scroll) if live else min(scroll, maximum)
    for j, line in enumerate(lines[first:first + room]):
        if not paint_stats:
            _put(scr, j + 2, 1, line, w - 2)
            continue
        attribute=curses.A_NORMAL
        if line.startswith(('GRID PROFIT:', 'ИТОГОВЫЙ PnL:')):
            attribute |= curses.A_BOLD
        if curses.has_colors() and line.startswith((
                'GRID PROFIT:', 'ИТОГОВЫЙ PnL:',
                'Floating PnL:', 'Макс. просадка:')):
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1,curses.COLOR_GREEN,-1)
                curses.init_pair(2,curses.COLOR_RED,-1)
                content=line.split(':',1)[1] if ':' in line else line
                attribute |= curses.color_pair(2 if '−$' in content else 1)
            except (curses.error,ValueError):pass
        try:
            scr.addnstr(j+(0 if paint_stats else 2),1,line,max(0,w-2),attribute)
        except (curses.error,TypeError):
            _put(scr,j+(0 if paint_stats else 2),1,line,w-2)
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
            '↑ ↓ — прокрутка    Esc / Enter — назад', paint_stats=True)
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
        choices=['Статистика','Статус','Настройки','Логи (LIVE)','Latency Diagnostics',
                 'FIFO Diagnostics','Execution Diagnostics',
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
                                'PANIC — завершить и закрыть позицию'])
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
        elif chosen=='Latency Diagnostics':
            from okx_latency_ui import report as latency_report
            curses.wrapper(lambda s: _text_screen(s,'Latency — '+name,
                             latency_report(bot_path(entry))))
        elif chosen in ('FIFO Diagnostics','Execution Diagnostics'):
            from okx_diagnostics_ui import fifo_report,execution_report
            reporter=fifo_report if chosen.startswith('FIFO') else execution_report
            curses.wrapper(lambda s: _text_screen(s,chosen+' — '+name,
                             reporter(bot_path(entry))))
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
                elif chosen.startswith('PANIC'):cmd_control(name,'PANIC')
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

def _menu_timestamp(name,entry,archive=False):
    root=bot_path(entry)
    cfg=json.loads((root/'config.json').read_text())
    if not archive:
        return float(cfg.get('created_at') or 0)
    ack=read_ack(entry)
    for k in ('completed_at','finished_at','stopped_at','updated_at','at'):
        v=ack.get(k)
        if isinstance(v,(float,int)) and v>0: return float(v)
        if isinstance(v,str):
            try: return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
            except ValueError: pass
    final=root/'state/final_stats.json'
    return final.stat().st_mtime if final.exists() else 0


def _display_timestamp(name,entry,archive=False):
    try:return _menu_timestamp(name,entry,archive)
    except (OSError,ValueError,RuntimeError,TypeError,KeyError):return 0


def _display_description(entry):
    try:return describe(entry)
    except (OSError,ValueError,RuntimeError,TypeError,KeyError):return 'Н/Д'


def interactive_archive_menu(entries):
    while True:
        names=sorted(entries,key=lambda n:_display_timestamp(n,entries[n],True),reverse=True)
        titles=[f'{name} — {_display_description(entries[name])}' for name in names]+['Назад']
        selected=curses.wrapper(lambda s:wizard.menu(s,'OKX GRID BOT — Архив',titles))
        if selected is None or selected==len(names): return
        name=names[selected]
        while True:
            choices=['Статистика','Настройки','Назад']
            k=curses.wrapper(lambda s:wizard.menu(s,'АРХИВ — '+name,choices))
            if k is None or k==2:break
            try:
                if k==0: curses.wrapper(lambda s:stats_screen(s,name))
                elif k==1: curses.wrapper(lambda s:_text_screen(s,'Настройки — '+name,_capture_settings(name)))
            except (OSError,RuntimeError,ValueError) as exc:
                curses.wrapper(lambda s:_text_screen(s,'Архив — ошибка',str(exc)))

def reader_diagnostics_snapshot():
    """One READ-ONLY account screen, not individual-bot control submenus."""
    from okx_reader_diagnostics import collect
    entries, warnings = display_registry()
    roots, units = {}, {}
    for name, entry in entries.items():
        try:
            if instance_terminal(entry):
                continue
            roots[name] = bot_path(entry)
            units[name] = unit_name(entry)
        except (OSError, KeyError, ValueError, RuntimeError, TypeError):
            continue
    return collect(roots, units), warnings


def reader_diagnostics_report():
    from okx_reader_diagnostics import report
    snapshot, warnings = reader_diagnostics_snapshot()
    result = report(snapshot)
    if warnings:
        result += '\n\n⚠️ Реестр: ' + ', '.join(warnings[:5])
    return result


def interactive_reader_diagnostics():
    """Single account-wide screen; no network or service actions."""
    try:
        text = reader_diagnostics_report()
    except (OSError, RuntimeError, ValueError) as exc:
        text = 'Reader Diagnostics: ошибка локального чтения (' + type(exc).__name__ + ')'
    curses.wrapper(lambda scr: _text_screen(
        scr, 'OKX GRID BOT — Shared Reader Diagnostics', text))


def interactive_main(env_file):
    while True:
        entries,warnings=display_registry()
        live={};archive={}
        for name,entry in entries.items():
            try:
                (archive if instance_terminal(entry) else live)[name]=entry
            except (OSError,ValueError,RuntimeError) as exc:
                warnings.append(name+': '+type(exc).__name__)
        if warnings:
            print('⚠️ Реестр (только чтение): '+', '.join(warnings[:5]),file=sys.stderr)
        names=sorted(live,key=lambda n:_display_timestamp(n,live[n]),reverse=True)
        def _live_label(name):
            entry=live[name]
            if not service_running(unit_name(entry)):
                return '🔴 ERROR'
            mode=control_desired(entry)
            if mode in ('PAUSE','SOFT') or account_soft_active():
                return '🟡 '+mode
            return '🟢 RUN'
        titles=[f'{_live_label(n)} {n} — {_display_description(live[n])}' for n in names]
        titles.extend(['➕ Создать бота','📦 Архив',
                       '📱 Telegram-монитор: '+telegram_monitor_label(),
                       '🔎 Reader Diagnostics (весь аккаунт)',
                       '⚙️   Управление аккаунтом','Выход'])
        i=curses.wrapper(lambda s:wizard.menu(s,'OKX Grid Bot v'+RELEASE_VERSION,titles))
        if i is None or i==len(titles)-1:return
        if i<len(names):
            interactive_bot_menu(names[i]);continue
        action=i-len(names)
        if action==0:
            try: cmd_new(env_file)
            except Exception as exc:print('ОШИБКА:',exc,file=sys.stderr)
            _pause_return()
        elif action==1: interactive_archive_menu(archive)
        elif action==2: interactive_telegram_menu()
        elif action==3: interactive_reader_diagnostics()
        elif action==4: interactive_account_menu()

def main(argv=None):
    p=argparse.ArgumentParser(description='Управление именованными OKX Grid ботами')
    sub=p.add_subparsers(dest='action')
    n=sub.add_parser('new',help='создать нового бота через опросник')
    n.add_argument('--env-file',default=str(BASE/'.okxbot_account.env'))
    for cmd in ('stats','status','show','start','stop','pause','resume','soft','hard','panic','logs','audit'):
        sp=sub.add_parser(cmd);sp.add_argument('name')
        if cmd=='logs':sp.add_argument('-f','--follow',action='store_true')
    sub.add_parser('list')
    sub.add_parser('account-reset', help='проверить и снять общую блокировку после ручной проверки маржи')
    sub.add_parser('account-status', help='GET-only баланс, маржа и позиции OKX')
    sub.add_parser('reader-diagnostics', help='READ-only Shared Reader по всему аккаунту, без OKX GET')
    sub.add_parser('panic-all', help='одно подтверждение, аварийное завершение всех LIVE-ботов')
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
        interactive_main(getattr(opts, 'env_file', str(BASE/'.okxbot_account.env')))
        return
    if cmd=='new':cmd_new(getattr(opts,'env_file',str(BASE/'.okxbot_account.env')))
    elif cmd=='list':cmd_list()
    elif cmd=='account-reset':cmd_account_reset()
    elif cmd=='account-status':cmd_account_status()
    elif cmd=='reader-diagnostics':print(reader_diagnostics_report())
    elif cmd=='panic-all':cmd_panic_all()
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
