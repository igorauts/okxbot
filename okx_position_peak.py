"""Exact peak of bot net LONG position from append-only actual OKX fills.

For operator display only: never access OKX or alter trading engine/state.
One record per instrument+bot, preserved across restarts. Requires complete
local fills archive; if unavailable, return a visible unavailable status.
"""
from __future__ import annotations
import json
import math
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

RECORD = 'bot_max_position_v1.json'


def dec(value, *, positive=False):
    number = Decimal(str(value))
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError('Неправильное значение сделки/конфигурации')
    return number


def _ordered_rows(db_file: Path, instrument: str):
    """Read a consistent SQLite snapshot; never modify journal or connections."""
    uri = 'file:' + quote(str(db_file), safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        bootstrap = conn.execute("SELECT value FROM meta WHERE name='complete_bootstrap'").fetchone()
        if not bootstrap or bootstrap[0] != '1':
            raise ValueError('Архив исполнений не полностью инициализирован')
        rows = conn.execute('SELECT order_id,bill_id,data FROM fills').fetchall()
    if not rows:
        raise ValueError('Нет записей исполнений')
    output = []
    seen = set()
    for order_id, bill_id, raw in rows:
        row = json.loads(raw)
        if str(row.get('ordId')) != str(order_id) or str(row.get('billId')) != str(bill_id):
            raise ValueError('Несовпадение идентификаторов записей SQLite')
        identity = (str(order_id),str(bill_id))
        if identity in seen:
            raise ValueError('Повторение исполнения')
        seen.add(identity)
        if row.get('instId',instrument) != instrument or row.get('side') not in ('buy','sell'):
            raise ValueError('Чужая пара или неизвестное направление исполнения')
        if row.get('feeCcy') != 'USDT' or row.get('fee') in (None,''):
            raise ValueError('Неполное исполнение (fee)')
        price,qty = dec(row['fillPx'],positive=True),dec(row['fillSz'],positive=True)
        ts = str(row.get('ts') or row.get('fillTime') or '')
        if not ts.isdigit():
            raise ValueError('Отсутствует время исполнения')
        # tradeId increases by instrument; handle same-ms fills deterministically.
        tradeid = str(row.get('tradeId') or '')
        if not tradeid.isdigit():
            raise ValueError('Нет tradeId для однозначной хронологии')
        output.append((int(ts),int(tradeid),str(bill_id),row['side'],qty,price))
    output.sort(key=lambda r:r[:3])
    return output


def reconstruct_peak(rows, contract_value):
    contract_value=dec(contract_value,positive=True)
    qty=Decimal('0')
    max_qty=Decimal('0')
    best=None
    for ms,tradeid,bill_id,side,size,price in rows:
        qty+=(size if side == 'buy' else -size)
        if qty < 0:
            raise ValueError('Неполная история: обнаружена отрицательная LONG-позиция')
        if qty > max_qty:
            max_qty=qty
            best={'contracts':str(qty), 'base_qty':str(qty*contract_value),
                  'nominal_usdt':str(qty*contract_value*price),
                  'price':str(price), 'at_unix_ms':ms, 'trade_id':str(tradeid)}
    if best is None:
        raise ValueError('Ни одной исполненной BUY в истории')
    return best, qty


def _safe_atomic(path, payload):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.position-peak-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as w:
            json.dump(payload,w,ensure_ascii=False,indent=2)
            w.flush();os.fsync(w.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load_and_update(path: Path, instrument: str):
    """Read local archive, safely persist maximum ever observed; never call API."""
    path=Path(path)
    cfg=json.loads((path/'config.json').read_text(encoding='utf-8'))
    if cfg.get('instrument') != instrument:
        raise ValueError('Пара в конфигурации и реестре различается')
    cv=dec(cfg['contract_value'],positive=True)
    rowdata=_ordered_rows(path/'state'/'fills_v068.sqlite',instrument)
    best,net=reconstruct_peak(rowdata,cv)
    file=path/'state'/RECORD
    previous=None
    if file.exists():
        previous=json.loads(file.read_text(encoding='utf-8'))
        if previous.get('schema') != 1 or previous.get('instrument') != instrument:
            raise ValueError('Неверный формат ранее сохранённого максимума')
        old_cv=dec(previous['contract_value'],positive=True)
        if old_cv != cv: raise ValueError('Размер контракта изменился')
        old=dec(previous['best']['contracts'],positive=True)
        if old>=dec(best['contracts']):
            best=previous['best']
    snapshot={'schema':1,'instrument':instrument,'contract_value':str(cv),
              'best':best, 'fill_rows':len(rowdata),
              'archive_net_contracts':str(net), 'computed_at_unix':time.time(),
              'method':'chronological_okx_fills'}
    if previous is None or previous.get('best') != best:
        _safe_atomic(file,snapshot)
    return best


def format_peak(path: Path, instrument: str):
    try:
        best=load_and_update(path,instrument)
        coin=instrument.split('-',1)[0]
        at=datetime.fromtimestamp(best['at_unix_ms']/1000,timezone.utc)
        base=dec(best['base_qty']); usdt=dec(best['nominal_usdt']);price=dec(best['price'])
        return [f'МАКС. ПОЗИЦИЯ В ИСТОРИИ:  {base:.8f} {coin}',
                f'  Номинал в момент пика: ${usdt:,.2f} (цена ${price:,.2f})',
                '  Достигнута:             '+at.strftime('%d.%m.%Y %H:%M:%S UTC')]
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError,
            InvalidOperation, OverflowError, json.JSONDecodeError) as e:
        return ['МАКС. ПОЗИЦИЯ В ИСТОРИИ:  нет достоверных данных',
                '  Причина: '+str(e)[:100]]
