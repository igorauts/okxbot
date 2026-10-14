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

import okx_setup068 as wizard

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


def engine_file(path):
    for name in ('okx_bot0610.py', 'okx_bot069.py', 'okx_bot068.py', 'okx_bot067.py', 'okx_bot066.py', 'okx_bot065.py', 'okx_bot064.py', 'okx_bot062.py', 'okx_bot061.py', 'okx_bot060.py', 'okx_bot059.py'):
        if (path / name).is_file():
            return name
    raise RuntimeError('Не найден исполняемый Python-файл бота')


def unit_text(path, engine, name):
    # Only paths created by our wizard are allowed: ASCII and no whitespace.
    if any(ch.isspace() for ch in str(path)) or not str(path).isascii():
        raise RuntimeError('Путь с пробелами не поддерживается для службы')
    description = name.replace('\n', ' ').replace('\r', ' ')[:48]
    return (f'[Unit]\nDescription=OKX Grid Bot ({description})\n'
            'Wants=network-online.target\nAfter=network-online.target\n'
            'StartLimitIntervalSec=300\nStartLimitBurst=10\n\n'
            '[Service]\nType=simple\nUser=root\n'
            f'WorkingDirectory={path}\n'
            'Environment=PYTHONUNBUFFERED=1\n'
            'Environment=OKX_BOT04_LIVE=CONFIRM\n'
            'Environment=OKX_BOT04_PROBE=FULL_25\n'
            f'ExecStart={path}/.venv/bin/python -u {path}/{engine}\n'
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
    unit = SYSTEMD_DIR / f'{service}.service'
    try:
        run([sys.executable, '-m', 'venv', str(created / '.venv')])
        run([str(created / '.venv/bin/pip'), 'install', '-r', str(created / 'requirements.txt')])
        unit.write_text(unit_text(created, engine_file(created), name))
        os.chmod(unit, 0o644)
        run(['systemctl', 'daemon-reload'])
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
    """Check the *effective* service command, including ordered systemd drop-ins.

    Unit-file-only checks are wrong after an engine upgrade performed with an
    ExecStart drop-in. No shell or manager writes happen in this function.
    """
    path=bot_path(entry)
    unit=SYSTEMD_DIR/unit_name(entry)
    if not unit.is_file():return False
    files=[unit]
    override=SYSTEMD_DIR/(unit.name+'.d')
    if override.is_dir():files+=sorted(override.glob('*.conf'))
    command=''
    for f in files:
        for line in f.read_text(encoding='utf-8').splitlines():
            if line.startswith('ExecStart='):
                command=line[len('ExecStart='):].strip()
    if not command:return False
    engines=('okx_bot0610.py','okx_bot069.py','okx_bot068.py',
             'okx_bot067.py','okx_bot066.py','okx_bot065.py','okx_bot064.py')
    return any((path/name).is_file() and str(path/name) in command.split()
               for name in engines)


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


def cmd_control(name, mode):
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
    cfg.update(version='0.6.8',name=new_name,created_at=time.time(),
               live_trading=False,price_at_setup=str(px),
               capital_reference_price=str(ref),cloned_from=old)
    print(f'Клон «{new_name}» из «{old}» | {cfg["instrument"]} | текущий BID {px}')
    if input(f'Введите точно CLONE {new_name}: ').strip()!=f'CLONE {new_name}':
        print('Отменено.')
        return
    dest.mkdir(mode=0o700)
    (dest/'state').mkdir(mode=0o700)
    (dest/'logs').mkdir(mode=0o700)
    for f in ('okx_bot068.py','requirements.txt'):
        shutil.copy2(SOURCE/f,dest/f)
    (dest/'.env').write_bytes((source/'.env').read_bytes())
    os.chmod(dest/'.env',0o600)
    fd=os.open(dest/'config.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(cfg,f,indent=2,ensure_ascii=False)
    run([sys.executable,'-m','venv',str(dest/'.venv')])
    run([str(dest/'.venv/bin/pip'),'install','-r',str(dest/'requirements.txt')])
    service=f'okx-grid-{slug}'
    (SYSTEMD_DIR/f'{service}.service').write_text(unit_text(dest,'okx_bot068.py',new_name))
    run(['systemctl','daemon-reload'])
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
    if instance_terminal(entry):
        header += ('ЭКЗЕМПЛЯР ЗАВЕРШЁН. Отчёт исторический; операции Hard/Panic '
                   'и их комиссии могут не учитываться в GRID PROFIT.\n\n')
    try:
        snapshot = read_stats_snapshot(path, instrument)
    except (ValueError, OSError, RuntimeError) as e:
        raise RuntimeError(f'Ошибка снимка статистики: {e}') from e
    if snapshot is not None:
        report, ts = snapshot
        report = with_latest_bot_dd(report, path, instrument)
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
    name,entry=find_entry(name)
    print(f'Имя: {name}; Пара: {describe(entry)}; Каталог: {entry["path"]}')
    if control_supported(entry):
        print('Режим:',control_desired(entry),'| Подтверждение:',read_ack(entry).get('phase','ожидание'))
    run(['systemctl','status',unit_name(entry),'--no-pager','--lines=15'],check=False)


def cmd_show(name):
    name,entry=find_entry(name)
    cfg=json.loads((bot_path(entry)/'config.json').read_text())
    print('Бот:',name,'| Служба:',entry['service'])
    print(json.dumps(cfg,ensure_ascii=False,indent=2))


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
    _, entry = find_entry(name)
    result = subprocess.run(['systemctl', 'status', unit_name(entry),
                             '--no-pager', '--lines=20'],
                            capture_output=True, text=True, timeout=10, check=False)
    return result.stdout + result.stderr


def _capture_settings(name):
    _, entry = find_entry(name)
    cfg = json.loads((bot_path(entry) / 'config.json').read_text())
    return f'Бот: {name} | Служба: {entry["service"]}\n\n' + json.dumps(cfg, ensure_ascii=False, indent=2)


def _pause_return():
    if sys.stdin.isatty():
        try:
            input('\nEnter — вернуться в меню…')
        except EOFError:
            pass


def interactive_bot_menu(name):
    """Single menu; only controls supported by the selected trading engine."""
    while True:
        _,entry=find_entry(name)
        is_new=control_supported(entry)
        terminal=instance_terminal(entry)
        mode=control_desired(entry)
        choices=['Статистика','Статус','Настройки','Логи (LIVE)']
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
        if chosen=='Статистика':
            curses.wrapper(lambda s: stats_screen(s,name))
        elif chosen=='Логи (LIVE)':
            curses.wrapper(lambda s: live_logs_screen(s,name))
        elif chosen in ('Статус','Настройки'):
            try:
                content=_capture_status(name) if chosen=='Статус' else _capture_settings(name)
                if chosen=='Статус' and is_new:
                    content=f'Режим: {mode}\nПоследняя операция: {read_ack(entry).get("phase","—")}\n\n'+content
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


def interactive_main(env_file):
    while True:
        entries = load_registry()
        titles = ['Создать нового бота', 'Список ботов']
        names = list(entries)
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
        else:
            interactive_bot_menu(names[i - 2])


def main(argv=None):
    p=argparse.ArgumentParser(description='Управление именованными OKX Grid ботами')
    sub=p.add_subparsers(dest='action')
    n=sub.add_parser('new',help='создать нового бота через опросник')
    n.add_argument('--env-file',default=str(BASE/'bot01/.env'))
    for cmd in ('stats','status','show','start','stop','pause','resume','soft','hard','panic','logs','audit'):
        sp=sub.add_parser(cmd);sp.add_argument('name')
        if cmd=='logs':sp.add_argument('-f','--follow',action='store_true')
    sub.add_parser('list')
    clone=sub.add_parser('clone',help='новый экземпляр из завершённого бота')
    clone.add_argument('source_name');clone.add_argument('new_name')
    a=sub.add_parser('attach',help='дать имя существующему боту без его перезапуска')
    a.add_argument('name');a.add_argument('--path',required=True);a.add_argument('--service',required=True)
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


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('\nОтменено.')
        sys.exit(130)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print('ОШИБКА:',e,file=sys.stderr)
        sys.exit(1)
