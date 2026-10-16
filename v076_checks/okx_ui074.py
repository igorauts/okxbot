#!/usr/bin/env python3
"""OKX Grid Bot v0.7.6 — read-only presentation only.

Never imports trading engine, accesses .env, submits OKX HTTP requests, or
modifies live bot state. Every unavailable metric remains unknown rather than 0.
"""
from __future__ import annotations
import json
import re
import sqlite3
from contextlib import closing
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

MANAGER_VERSION='v0.7.6'
ENGINE_PATTERN=re.compile(r'\bokx_bot(\d{3,4})\.py\b')
GRID_LINE=re.compile(r'BID=([0-9.]+)\b')
TERMINAL_AGG={'FILLED','CANCELED','REJECTED','DONE','CLOSED'}


def dec(x):
    try:
        result=Decimal(str(x));return result if result.is_finite() else None
    except (TypeError,InvalidOperation,ValueError):return None


def fmt_num(x,places=2):
    n=dec(x)
    return f'${n:,.{places}f}' if n is not None else '—'


def duration(seconds):
    if seconds is None or seconds < 0:return 'нет данных'
    s=int(seconds);d,s=divmod(s,86400);h,s=divmod(s,3600);m,s=divmod(s,60)
    parts=[]
    if d:parts.append(f'{d} д')
    if h or d:parts.append(f'{h} ч')
    parts.append(f'{m} мин')
    if not d and not h:parts.append(f'{s} сек')
    return ' '.join(parts)


def systemd_info(unit):
    try:
        p=subprocess.run(['systemctl','show',unit,
            '-p','ActiveState','-p','ActiveEnterTimestampMonotonic',
            '-p','ActiveEnterTimestamp','-p','ExecStart'],
            check=False,capture_output=True,text=True,timeout=5)
        if p.returncode not in (0,):return {}
        rows={k:v for line in p.stdout.splitlines() if '=' in line
              for k,v in [line.split('=',1)]}
        return rows
    except (OSError,subprocess.TimeoutExpired):return {}


def engine_version(info):
    match=ENGINE_PATTERN.search(info.get('ExecStart',''))
    if not match:return None
    value=match.group(1)
    if len(value)<3:return None
    return f'v{int(value[0])}.{int(value[1])}.{int(value[2:])}'


def uptimes(info,proc_uptime='/proc/uptime'):
    try:host=float(Path(proc_uptime).read_text().split()[0])
    except (OSError,ValueError,IndexError):host=None
    process=None
    try:
        boot_us=int(info.get('ActiveEnterTimestampMonotonic') or 0)
        if info.get('ActiveState')=='active' and boot_us>0 and host is not None:
            process=max(0,host-boot_us/1e6)
    except (ValueError,TypeError):pass
    return process,host


def read_json(path):
    try:
        x=json.loads(Path(path).read_text(encoding='utf-8'))
        return x if isinstance(x,dict) else None
    except (OSError,ValueError,TypeError):return None


def read_local_orders(state):
    if not isinstance(state,dict) or not isinstance(state.get('cycles'),dict):return None
    cycles=state['cycles'].values()
    r=dict(buy=0,buy_placing=0,real_tp=0,tp_canceling=0,tp_pending=0,
           virtual_tp=0,partial_buy=0,partial_tp=0,unresolved_market=0)
    for c in cycles:
        if not isinstance(c,dict):continue
        typ=c.get('state')
        if typ in ('BUY_LIVE','BUY_PARTIAL'):r['buy']+=1
        if typ=='PLACING':r['buy_placing']+=1
        partial_size=dec(c.get('filledBuy')) or Decimal(0)
        order_size=dec(c.get('sz')) or Decimal(0)
        if typ=='BUY_PARTIAL' or (typ=='CANCELING' and order_size>partial_size>0):
            r['partial_buy']+=1
        if typ=='VIRTUAL_TP':r['virtual_tp']+=1
        for rec in c.get('tpOrders',[]) or []:
            if not isinstance(rec,dict):continue
            if rec.get('phase')=='ACTIVE':r['real_tp']+=1
            elif rec.get('phase')=='CANCELING':r['tp_canceling']+=1
            elif rec.get('phase') in ('PENDING','PLACING'):r['tp_pending']+=1
            filled=dec(rec.get('filled')) or Decimal(0)
            qty=dec(rec.get('sz')) or Decimal(0)
            if qty>filled>0 and rec.get('phase') in ('ACTIVE','CANCELING','PENDING'):
                r['partial_tp']+=1
        if typ=='TP_PARTIAL' and not c.get('tpOrders'):
            # Legacy TP without individual leg data: count is unknown, never guess.
            r['partial_tp']=None
    agg=state.get('aggregate_exits')
    if isinstance(agg,list):
        r['unresolved_market']=sum(1 for a in agg if isinstance(a,dict)
                                   and str(a.get('phase')).upper() not in TERMINAL_AGG)
    else:r['unresolved_market']=None
    return r


def read_monitor(root,now=None):
    now=time.time() if now is None else now
    st=read_json(Path(root)/'state/bot04_state.json')
    version={'0.7.6':'075','0.7.5':'075','0.7.4':'074','0.7.3':'073'}.get(st.get('version') if st else None)
    if version is None:return None
    for version in (version,):
        p=Path(root)/'diagnostics'/('execution_v'+version)/('summary_v'+version+'.json')
        item=read_json(p)
        if not item:continue
        raw=item.get('ts')
        try:t=datetime.fromisoformat(raw.replace('Z','+00:00')).timestamp()
        except (ValueError,AttributeError):continue
        if 0<=now-t<=20:
            return item
    return None


def last_local_bid(unit):
    """Fresh market BID from this bot's recent local journal, not an OKX API.

    Only display a value if the trading bot printed it within the last 20s.
    When the optional observer is OFF, this preserves a useful status screen.
    """
    try:
        proc=subprocess.run(['journalctl','-u',unit,'--since','20 seconds ago',
            '-n','80','--no-pager','-o','cat'],capture_output=True,text=True,
            check=False,timeout=4)
        if proc.returncode!=0:return None
        bids=GRID_LINE.findall(proc.stdout)
        x=dec(bids[-1]) if bids else None
        return x if x is not None and x>0 else None
    except (OSError,subprocess.TimeoutExpired):return None


def monitor_service_enabled(unit,kind):
    """systemd per-instance toggle; older custom monitors are not misidentified."""
    base=unit[:-8] if unit.endswith('.service') else unit
    for version in ('074','073'):
        svc=f'{base}-{kind}-monitor{version}.service'
        try:
            p=subprocess.run(['systemctl','is-active','--quiet',svc],
                             capture_output=True,timeout=3,check=False)
            if p.returncode==0:return True
        except (OSError,subprocess.TimeoutExpired):pass
    return False


def history_multifill(path):
    """Count unique order IDs with >1 distinct exchange execution.

    Multiple trades != necessarily an unfilled remainder. Only show when the
    full historical bootstrap flag is set and the read-only DB is available.
    """
    dbpath=Path(path)/'state/fills_v068.sqlite'
    if not dbpath.is_file():return None
    try:
        with closing(sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.25)) as db:
            row=db.execute("SELECT value FROM meta WHERE name='complete_bootstrap'").fetchone()
            if not row or str(row[0]) not in ('1','true','True'):return None
            val=db.execute('SELECT COUNT(*) FROM (SELECT order_id FROM fills '
                           'GROUP BY order_id HAVING COUNT(DISTINCT bill_id)>1)').fetchone()
            return int(val[0])
    except (sqlite3.DatabaseError,OSError,ValueError):return None


def table_row(label,value):return f'  {label:<26} {value}'


def settings(name,root,unit):
    cfg=read_json(Path(root)/'config.json')
    if cfg is None:return f'Бот {name}: настройки недоступны'
    info=systemd_info(unit);ver=engine_version(info) or 'не определена'
    buy=int(cfg.get('active_buy_orders',25));real_tp=int(cfg.get('real_tp_target',min(25,int(cfg.get('active_tp_orders',25)))))
    buf=buy+5
    coin=str(cfg.get('instrument','')).split('-')[0]
    qty=dec(cfg.get('order_size'));contract=dec(cfg.get('contract_value'))
    amount=(qty*contract) if qty is not None and contract is not None else None
    step=dec(cfg.get('grid_step'));tp=dec(cfg.get('take_profit'))
    ratio=f' ({tp/step:.2f} шага)' if tp is not None and step and step>0 else ''
    up=dec(cfg.get('upper_price'))
    ref=up if up else dec(cfg.get('capital_reference_price'))
    lower=(ref-step*(int(cfg.get('max_grids',0))-1)) if ref is not None and step is not None else None
    cap=(amount*int(cfg.get('max_grids',0))*(ref+lower)/2) if (amount is not None and ref is not None and lower is not None) else None
    created='—'
    try:created=datetime.fromtimestamp(float(cfg['created_at']),timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except (KeyError,ValueError,TypeError,OverflowError):pass
    rows=[f'Бот: {name}  |  {cfg.get("instrument","—")}',
          '\nОБЩЕЕ','─'*54,table_row('Версия',ver),
          table_row('Направление',str(cfg.get('direction','—')).upper()),
          '\nСЕТКА И ОРДЕРА','─'*54,
          table_row('Шаг сетки',fmt_num(step)),table_row('Take Profit',fmt_num(tp)+ratio),
          table_row('Размер BUY',f'{amount} {coin}' if amount is not None else '—'),
          table_row('Активные BUY',f'{buy} (макс. {buy+5})'),
          table_row('Реальные SELL TP',f'{real_tp} (макс. {real_tp+buf})'),
          table_row('Защита BUY возле BID',f'5 шагов ({fmt_num(step*5)})' if step is not None else '5 шагов'),
          table_row('Максимум гридов',cfg.get('max_grids','—')),
          '\nЦЕНОВОЙ ДИАПАЗОН','─'*54,
          table_row('Верхняя граница',fmt_num(up) if up else 'без ограничения'),
          table_row('Цена при создании',fmt_num(cfg.get('price_at_setup'))),
          table_row('Расчётная нижняя цена',fmt_num(lower)),
          table_row('Расчётный капитал 1x',fmt_num(cap)),
          '\nПРОЧЕЕ','─'*54,
          table_row('Создан',created)]
    if ver!='не определена' and ver!=MANAGER_VERSION:
        rows.append(f'⚠ Менеджер {MANAGER_VERSION}, этот бот использует {ver}')
    return '\n'.join(map(str,rows))


def status(name,root,unit):
    cfg=read_json(Path(root)/'config.json')
    if cfg is None:return f'Бот {name}: конфигурация недоступна'
    info=systemd_info(unit);ver=engine_version(info) or 'не определена'
    running=info.get('ActiveState')=='active'
    control=read_json(Path(root)/'state/control.json') or {}
    ack=read_json(Path(root)/'state/control_ack.json') or {}
    mode=control.get('mode','RUN')
    label=('РАБОТАЕТ' if running else 'ОСТАНОВЛЕН')
    if running and mode=='PAUSE':label='ПАУЗА'
    if running and mode=='SOFT':label='SOFT STOP'
    if mode=='HARD' or mode=='PANIC':label='ЗАВЕРШАЕТ РАБОТУ' if running else 'ЗАВЕРШЁН'
    proc,host=uptimes(info)
    started=info.get('ActiveEnterTimestamp','—') if running else '—'
    state=read_json(Path(root)/'state/bot04_state.json')
    counters=read_local_orders(state)
    buy=int(cfg.get('active_buy_orders',25));real_tp=int(cfg.get('real_tp_target',min(25,int(cfg.get('active_tp_orders',25)))))
    max_tp=real_tp+buy+5
    mon=read_monitor(root)
    bid=mon.get('bid') if mon and mon.get('bid') else last_local_bid(unit)
    missing=(mon.get('state') or {}).get('missing_buy') if mon else None
    near=(mon.get('state') or {}).get('near_missing_buy') if mon else None
    def show(x):return 'нет данных' if x is None else str(x)
    s=[f'Бот: {name}  |  {cfg.get("instrument","—")}',
       '\nСОСТОЯНИЕ','─'*54,
       table_row('Версия',ver),table_row('Бот',label),
       table_row('Работает с',started),
       table_row('Аптайм бота',duration(proc)),
       table_row('Аптайм VPS',duration(host)),
       '\nРЫНОК И ОРДЕРА','─'*54,
       table_row('BID',fmt_num(bid) if bid else 'нет свежих данных'),
       table_row('BUY LIMIT (сейчас)',f'{show(counters["buy"] if counters else None)}/{buy} (макс. {buy+5})'),
       table_row('BUY выставляются',show(counters['buy_placing'] if counters else None)),
       table_row('SELL TP (сейчас)',f'{show(counters["real_tp"] if counters else None)}/{real_tp} (макс. {max_tp})'),
       table_row('SELL TP в отмене',show(counters['tp_canceling'] if counters else None)),
       table_row('SELL TP ожидают',show(counters['tp_pending'] if counters else None)),
       table_row('Виртуальные TP',show(counters['virtual_tp'] if counters else None)),
       '\nКОНТРОЛЬ ИСПОЛНЕНИЙ','─'*54,
       table_row('Пропущенные BUY',show(missing)),
       table_row('Пропуски возле BID',show(near)),
       table_row('Незавершённые MARKET',show(counters['unresolved_market'] if counters else None)),
       table_row('Частичные BUY сейчас',show(counters['partial_buy'] if counters else None)),
       table_row('Частичные SELL сейчас',show(counters['partial_tp'] if counters else None)),
       table_row('Ордеров с 2+ сделками',show(history_multifill(root))),
       '\nМОНИТОРИНГ','─'*54,
       table_row('Burst Monitor','ВКЛЮЧЁН' if monitor_service_enabled(unit,'burst') else 'ВЫКЛЮЧЕН'),
       table_row('Execution Monitor','ВКЛЮЧЁН' if monitor_service_enabled(unit,'execution') else ('внешние данные' if mon else 'ВЫКЛЮЧЕН'))]
    # These counters are from optional local observer only, never fabricate.
    if mon is not None:
        events=mon.get('event_counts',{})
        s += [table_row('Бурстов (с запуска)',show(events.get('completed_bursts'))),
              table_row('Предупреждений',show(events.get('alerts_WARNING'))),
              table_row('Критических ошибок',show(events.get('alerts_CRITICAL')))]
    else:
        s += [table_row('Бурсты / предупреждения','нет свежих данных')]
    s.append('\nСчётчики ордеров — локальное состояние; OKX независимо не опрашивается.')
    if not running:s.append('⚠ Бот остановлен: локальная сетка может быть устаревшей.')
    if ver!='не определена' and ver!=MANAGER_VERSION:
        s.append(f'⚠ Менеджер {MANAGER_VERSION}, торговый движок {ver}')
    return '\n'.join(s)
