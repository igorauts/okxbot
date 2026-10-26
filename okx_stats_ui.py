"""Local, read-only, terminal statistics presentation.

All monetary amounts come from the fill-audited engine snapshot. Never infer
profits from order prices, archived event counters or independent REST calls.
"""
from __future__ import annotations
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


def dec(x):
    if x is None:
        return None
    try:
        value=Decimal(str(x))
        return value if value.is_finite() else None
    except (ValueError, InvalidOperation, TypeError):
        return None


def signed(value, digits=8):
    v=dec(value)
    if v is None:return 'н/д'
    v=v.quantize(Decimal(1).scaleb(-digits),rounding=ROUND_HALF_UP)
    return ('+' if v>0 else '−' if v<0 else '')+'$'+f'{abs(v):,.{digits}f}'


def usd(value, digits=2):
    v=dec(value)
    return f'${v:,.{digits}f}' if v is not None else 'н/д'


def pct(value, base):
    x=dec(value);b=dec(base)
    if x is None or b is None or b<=0:return 'н/д'
    return f'{x/b*100:+.4f}%'


def at_utc(value):
    try:
        v=float(value)
        if not math.isfinite(v) or v<=0:raise ValueError
        return datetime.fromtimestamp(v,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
    except (ValueError,TypeError,OverflowError):return 'н/д'


def run_time(seconds):
    if seconds is None:return 'н/д'
    seconds=max(0,int(seconds))
    d,seconds=divmod(seconds,86400);h,seconds=divmod(seconds,3600);m,seconds=divmod(seconds,60)
    return (f'{d}д ' if d else '')+f'{h}ч {m}м {seconds}с'


def base_quantity(value, config):
    """Readable exact base quantity at OKX contract precision; no fictitious rounding.

    contract_value * contract_lot is the minimum base-asset order increment.
    Preserve any non-aligned historical partial quantity instead of rounding it.
    """
    v=dec(value)
    if v is None:return 'н/д'
    contract=dec(config.get('contract_value'))
    lot=dec(config.get('contract_lot'))
    quantum=contract*lot if contract is not None and lot is not None else None
    if quantum is not None and quantum>0 and v % quantum == 0:
        digits=max(0,-quantum.normalize().as_tuple().exponent)
        out=f'{v:.{digits}f}'
    else:
        out=format(v,'f')  # exact fallback, never round off a partial fill
    return out.rstrip('0').rstrip('.') if '.' in out else out


def peak_line(root, instrument):
    """Read SQLite in query_only mode; never write a peak cache on UI repaint."""
    try:
        from okx_position_peak import reconstruct_peak, _ordered_rows
        from okx_paths import fills_db
        cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
        if cfg.get('instrument')!=instrument:raise ValueError('instrument mismatch')
        best,_=reconstruct_peak(_ordered_rows(fills_db(root),instrument),cfg['contract_value'])
        base=dec(best['base_qty']);nominal=dec(best['nominal_usdt'])
        if base is None or nominal is None:raise ValueError('peak invalid')
        return f'Макс. позиция в истории: {base_quantity(base,cfg)} {instrument.split("-",1)[0]} ({usd(nominal)})'
    except (OSError,ValueError,TypeError,KeyError,sqlite3.DatabaseError,InvalidOperation):
        return 'Макс. позиция в истории: нет достоверных данных'


def drawdown_lines(root, instrument, capital, now):
    path=root/'state'/'bot_drawdown.json'
    if not path.is_file() or path.is_symlink():return ['Макс. просадка: н/д']
    try:
        d=json.loads(path.read_text(encoding='utf-8'))
        if d.get('schema')!=1 or d.get('instrument')!=instrument:return ['Макс. просадка: н/д']
        value=dec(d['max_drawdown_usdt']);timestamp=float(d['max_drawdown_at'])
        if value is None or value<0 or not math.isfinite(timestamp) or timestamp>now+60:
            raise ValueError('invalid drawdown')
        return [f'Макс. просадка: {signed(-value)} ({pct(-value,capital)})',
                f'Зафиксирована: {at_utc(timestamp)}']
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        return ['Макс. просадка: н/д']


def format_stats(root, instrument, payload, *, bot_active=False,
                 heartbeat_fresh=False, snapshot_at=None, now=None):
    """Render a verified schema-1 performance payload. Never mutates files."""
    root=Path(root)
    now=time.time() if now is None else float(now)
    if not isinstance(payload,dict) or payload.get('schema')!=1:
        raise ValueError('Unsupported performance snapshot schema')
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    if config.get('instrument')!=instrument:raise ValueError('Instrument mismatch')
    bid=dec(payload.get('bid'));base=dec(payload.get('open_base'))
    total=dec(payload.get('total_pnl'));grid=dec(payload.get('grid_profit'))
    floating=dec(payload.get('floating_pnl'));lim=dec(payload.get('limit_tp_net'))
    market=dec(payload.get('market_tp_net'));partial=dec(payload.get('partial_exit_net'))
    hidden=dec(payload.get('realized_open_components'))
    if any(x is None for x in (bid,base,total,grid,floating,lim,market,partial,hidden)) or bid<=0 or base<0:
        raise ValueError('Incomplete or invalid financial snapshot')
    tolerance=Decimal('0.00000001')
    if abs(lim+market-grid)>tolerance or abs(grid+partial+floating+hidden-total)>tolerance:
        raise ValueError('PnL attribution not reconciling; report withheld')
    nominal=dec(payload.get('open_nominal'))
    if nominal!=base*bid:raise ValueError('Open nominal does not match exact BID')
    if snapshot_at is not None and (snapshot_at>now+60 or now-snapshot_at>900):
        active=False
    else:
        active=bool(bot_active and heartbeat_fresh)
    # A live process can be in RUN while new BUY are blocked by a
    # reconciliation/failure hold. RUNNING service != trading available.
    trade_hold=None
    try:
        ack=json.loads((root/'state'/'control_ack.json').read_text(encoding='utf-8'))
        if ack.get('mode')=='RUN' and ack.get('phase') in (
                'BUY_HOLD_TP_FAILURE','BUY_HOLD_RECONCILIATION'):
            trade_hold=ack['phase']
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if trade_hold:active=False
    coin=instrument.split('-',1)[0]
    tick=dec(config.get('tick_size'))
    digits=max(2,-tick.normalize().as_tuple().exponent) if tick and tick>0 else 8
    upper=dec(payload.get('top_open_buy'))
    if upper is not None and upper<=0:raise ValueError('Invalid top filled BUY')
    if upper is None:
        bidtext=f'{usd(bid,digits)} (верх исполненных BUY отсутствует)'
    else:
        change=(bid-upper)/upper*100
        bidtext=f'{usd(bid,digits)} ({change:+.2f}%)'
    started=payload.get('launch_at')
    elapsed=now-float(started) if started is not None else None
    uptime=payload.get('uptime') or 'н/д'
    capital=dec(payload.get('capital'))
    pct_total=pct(total,capital)
    waiting=payload.get('tp_waiting_all')
    if type(waiting) is not int or waiting<0:
        # Older engine snapshots contained only real TP. Never mislabel those as
        # ALL real+virtual while running mixed-package rollout.
        waiting='н/д'
    lines=[
        (('🔴 BUY заблокированы ('+trade_hold+'): ') if trade_hold else
         ('🟢 Работает: ' if active else '⚪ Работает (RUN не подтверждён): '))
            +f'{run_time(elapsed)}, uptime {uptime}',
        'Запущен: '+at_utc(started),
        '',
        'Верх сетки: '+(usd(upper,digits) if upper is not None else '—'),
        'Текущий BID: '+bidtext,
        f'Ждут TP: {waiting}',
        '',
        'GRID PROFIT: '+signed(grid),
        f'Закрытых циклов: {payload["closed_count"]}',
        'LIMIT TP: '+signed(lim),
        'MARKET TP: '+signed(market),
        'Partial MARKET Exit: '+signed(partial),
        '',
        f'Открытая позиция: {base_quantity(base,config)} {coin} ({usd(nominal)})',
        'Floating PnL: '+signed(floating),
        '',
        'ИТОГОВЫЙ PnL: '+signed(total)+f' ({pct_total} от 1x)',
        '',
        f'Прибыль за последние 24ч: {signed(payload.get("last_24h"),4)} ({pct(payload.get("last_24h"),capital)})',
        'Средний темп GRID:',
        f'  сутки: {signed(payload.get("per_day"),4)} ({pct(payload.get("per_day"),capital)})',
        f'  30 дней: {signed(payload.get("per_30d"),2)} ({pct(payload.get("per_30d"),capital)})',
        f'  год: {signed(payload.get("per_year"),2)} ({pct(payload.get("per_year"),capital)})',
        '',
        'Общий объём: '+usd(payload.get('volume'),4),
        '  Maker: '+usd(payload.get('maker_volume'),4),
        '  Taker: '+usd(payload.get('taker_volume'),4),
        'Уплачено комиссий: '+usd(payload.get('paid_fees'),8),
        '',
        'Расчётный капитал для 1x: '+usd(capital),
        '  max position: '+(f'{base_quantity(payload["max_base"],config)} {coin}' if dec(payload.get('max_base')) is not None else '—'),
        '  сетка: '+(f'{usd(payload["grid_reference_top"],digits)} → {usd(payload["grid_bottom"],digits)}'
                     if payload.get('grid_reference_top') is not None else '—'),
        '  средняя цена: '+usd(payload.get('grid_average'),digits),
        '',
    ]
    lines.extend(drawdown_lines(root,instrument,capital,now))
    lines.append(peak_line(root,instrument))
    lines.append('✅ Все закрытые циклы сопоставлены.')
    if snapshot_at is not None and now-snapshot_at>900:
        lines.append('⚠️ Финансовая статистика устарела; суммы показаны на момент последней сверки.')
    return '\n'.join(lines)
