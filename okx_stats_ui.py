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
    if x is None or isinstance(x, bool):
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


def tp_reserve_text(root, instrument, *, state=None, now=None, include_age=False):
    """Presentation only: unknown canceled-partial attached SELL reservations.

    Display retained volume even during a temporary admission HOLD. Never
    infer trading permission from this field or the age of the reservation.
    """
    if state is None:
        try:
            from okx_paths import state_file
            state=json.loads(state_file(Path(root)).read_text(encoding='utf-8'))
        except (OSError,ValueError,TypeError):return None
    if not isinstance(state,dict) or state.get('instrument') not in (None,instrument):return None
    cycles=state.get('cycles')
    if not isinstance(cycles,dict):return None
    count=0;qty=Decimal(0);seen=set();stamps=[]
    now=time.time() if now is None else float(now)
    for c in cycles.values():
        if not isinstance(c,dict) or c.get('state')!='TP_WAIT' or not c.get('partial_cleanup'):continue
        records=c.get('tpOrders')
        if not isinstance(records,list):continue
        for r in records:
            if (not isinstance(r,dict) or r.get('source')!='attached'
                    or r.get('phase')!='PENDING' or r.get('ordId')):continue
            cid=r.get('clOrdId');size=dec(r.get('sz'));filled=dec(r.get('filled'))
            if (not cid or cid in seen or cid!=c.get('tpClOrdId') or size is None or filled is None
                    or not 0<=filled<size):return 'н/д (состояние резерва не подтверждено)'
            seen.add(cid);count+=1;qty+=size-filled
            try:
                stamp=float(r.get('placedAt'))
                if math.isfinite(stamp) and 0<stamp<=now+1:stamps.append(stamp)
            except (ValueError,TypeError,OverflowError):pass
    if not count:return None
    q=format(qty,'f')
    if '.' in q:q=q.rstrip('0').rstrip('.')
    result=f'{count} / {q} контр.'
    if include_age:
        result+=' · '+(run_time(now-min(stamps)) if len(stamps)==count else 'возраст н/д')
    return result


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


def read_drawdown(root, instrument, now):
    """Read the independent local DD sample; no fallback to stale report text."""
    path=Path(root)/'state'/'bot_drawdown.json'
    if not path.is_file() or path.is_symlink():return None
    try:
        d=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(d,dict) or type(d.get('schema')) is not int or d['schema']!=1 or d.get('instrument')!=instrument:
            return None
        value=dec(d['max_drawdown_usdt']);stamp=dec(d['max_drawdown_at'])
        if value is None or value<0 or stamp is None or not 0<stamp<=Decimal(str(now))+60:
            return None
        return value,float(stamp)
    except (OSError,ValueError,TypeError,KeyError,OverflowError,ArithmeticError):
        return None


def drawdown_lines(root, instrument, capital, now):
    record=read_drawdown(root,instrument,now)
    if record is None:return ['Макс. просадка: н/д']
    value,timestamp=record
    return [f'Макс. просадка: {signed(-value)} ({pct(-value,capital)})',
            f'Зафиксирована: {at_utc(timestamp)}']


def validate_performance(payload):
    """One monetary identity validator, shared by all current consumers."""
    if not isinstance(payload, dict) or type(payload.get('schema')) is not int or payload['schema'] != 1:
        raise ValueError('Unsupported performance snapshot schema')
    fields = ('bid', 'open_base', 'total_pnl', 'grid_profit', 'floating_pnl',
              'limit_tp_net', 'market_tp_net', 'partial_exit_net',
              'realized_open_components', 'open_nominal')
    v = {k: dec(payload.get(k)) for k in fields}
    if any(x is None for x in v.values()) or v['bid'] <= 0 or v['open_base'] < 0:
        raise ValueError('Incomplete or invalid financial snapshot')
    if type(payload.get('closed_count')) is not int or payload['closed_count'] < 0:
        raise ValueError('Invalid closed cycle count')
    tolerance = Decimal('0.00000001')
    if (abs(v['limit_tp_net'] + v['market_tp_net'] - v['grid_profit']) > tolerance
            or abs(v['grid_profit'] + v['partial_exit_net'] + v['floating_pnl'] +
                   v['realized_open_components'] - v['total_pnl']) > tolerance):
        raise ValueError('PnL attribution not reconciling; report withheld')
    if v['open_nominal'] != v['open_base'] * v['bid']:
        raise ValueError('Open nominal does not match exact BID')
    # Optional metrics may be genuinely unavailable before the first fill.
    # If supplied, they must still be finite numbers, never booleans or text.
    for k in ('top_open_buy','capital','last_24h','volume','paid_fees','launch_at',
              'per_day','per_30d','per_year','max_base','grid_reference_top',
              'grid_bottom','grid_average','maker_volume','taker_volume'):
        if payload.get(k) is not None and dec(payload[k]) is None:
            raise ValueError('Invalid performance field: '+k)
    if payload.get('top_open_buy') is not None and dec(payload['top_open_buy'])<=0:
        raise ValueError('Invalid top filled BUY')
    return v


def read_stats_record(root):
    """Read exactly one stats.json generation. Missing is not malformed."""
    path=Path(root)/'state'/'stats.json'
    if path.is_symlink():
        raise ValueError('Unsafe statistics snapshot path')
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('Statistics snapshot is not a regular file')
    record=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(record,dict):
        raise ValueError('Statistics snapshot must be an object')
    return record


def structured_payload(stats):
    """No fallback from a malformed canonical payload to a legacy alias."""
    if 'performance' in stats:
        return stats['performance']
    return stats.get('performance_v0724')


def requires_structured_snapshot(cfg, stats):
    """Do not downgrade an attested structured producer to legacy text.

    A consumer's imported ENGINE_VERSION can differ from the producer during
    a staged rollout. Only this fallback decision reads the local manifest;
    normal structured validation never needs that extra read. Historical
    engines with the production StatsWorker dependency are also structured.
    """
    if 'stats_v2' in stats:
        return True
    try:
        path = Path(__file__).resolve().with_name('engine_manifest.json')
        if path.is_symlink() or not path.is_file():
            return True  # cannot establish legacy compatibility
        manifest = json.loads(path.read_text(encoding='utf-8'))
        engines = manifest['engines']
        current = manifest['current_version']
        if not isinstance(engines, dict) or current not in engines:
            return True
        for version in (cfg.get('version'), stats.get('bot_version')):
            if version == current:
                return True
            if version is None:
                continue
            if not isinstance(version, str):
                return True
            entry = engines.get(version)
            if entry is not None:
                deps = entry.get('dependencies')
                if not isinstance(deps, list):
                    return True
                if 'okx_stats_v2_production.py' in deps:
                    return True
        return False
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return True


def validate_financial_snapshot(root, instrument, stats, *, now=None):
    """Validate a SINGLE already-read stats.json generation, including anchor.

    The embedded anchor is authoritative. Legacy FULL publications may have a
    separate anchor; they must match the same sample. No state or API writes.
    """
    now = time.time() if now is None else now
    if (not isinstance(stats, dict) or type(stats.get('schema_version')) is not int
            or stats['schema_version'] != 1 or stats.get('complete') is not True
            or stats.get('instrument') != instrument or not instrument):
        raise ValueError('Statistics identity/completeness invalid')
    raw_stamp = stats.get('generated_at_unix')
    generated = dec(raw_stamp)
    if (type(raw_stamp) not in (int, float) or generated is None or generated <= 0
            or generated > Decimal(str(now))):
        raise ValueError('Financial statistics timestamp invalid')
    payload = structured_payload(stats)
    values = validate_performance(payload)
    if 'financial_anchor' in stats:
        anchor = stats['financial_anchor']
        tolerance = Decimal(0)
    else:
        if 'stats_v2' in stats:
            raise ValueError('Atomic StatsWorker financial anchor missing')
        path = Path(root) / 'state' / 'stats_financial_anchor.json'
        if path.is_symlink():
            raise ValueError('Unsafe legacy financial anchor')
        anchor = json.loads(path.read_text(encoding='utf-8'))
        tolerance = Decimal('0.5')
    if (not isinstance(anchor, dict) or type(anchor.get('schema')) is not int
            or anchor['schema'] != 1 or anchor.get('instrument') != instrument):
        raise ValueError('Invalid financial anchor')
    stamp = dec(anchor.get('at'))
    if (stamp is None or stamp <= 0 or abs(stamp - generated) > tolerance
            or dec(anchor.get('bid')) != values['bid']
            or dec(anchor.get('total_pnl_usdt')) != values['total_pnl']):
        raise ValueError('Financial anchor mismatch')
    return payload, values, generated


def require_fresh_snapshot(generated, now, *, max_age=900):
    """Current Telegram views only; verified historical money is still valid."""
    if not 0 <= Decimal(str(now)) - generated <= max_age:
        raise ValueError('Financial statistics stale')


def format_stats(root, instrument, payload, *, bot_active=False,
                 heartbeat_fresh=False, snapshot_at=None, now=None,
                 include_funding=False, funding_db=None):
    """Render a verified schema-1 performance payload. Never mutates files."""
    root=Path(root)
    now=time.time() if now is None else float(now)
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    if config.get('instrument')!=instrument:raise ValueError('Instrument mismatch')
    v=validate_performance(payload)
    bid,base,total,grid,floating,lim,market,partial,nominal=(v[k] for k in (
        'bid','open_base','total_pnl','grid_profit','floating_pnl','limit_tp_net',
        'market_tp_net','partial_exit_net','open_nominal'))
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
        if ack.get('mode') in ('HARD','PANIC') and ack.get('phase')=='WAIT_RESERVED_TP':
            trade_hold=ack['mode']+' WAIT_RESERVED_TP'
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
    # Manager opt-in only; original fill-audited PnL checks above stay unchanged.
    funding_line=None
    if include_funding:
        try:
            from okx_funding_view import display_funding
            db_path=Path(funding_db) if funding_db is not None else Path('/srv/okx-funding/funding.sqlite')
            funding=display_funding(db_path,instrument,config.get('created_at'),now_ms=int(now*1000))
            funding_line=funding.manager_line(int(now*1000))
            if funding.snapshot is not None:
                total=funding.snapshot.add_to(total)
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ArithmeticError,ImportError):
            funding_line='FUNDING: +0.000000, нет данных'
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
        'Realized Open: '+signed(v['realized_open_components']),
        '',
        f'Открытая позиция: {base_quantity(base,config)} {coin} ({usd(nominal)})',
        'Floating PnL: '+signed(floating),
        *([funding_line] if funding_line is not None else []),
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
    reserve=tp_reserve_text(root,instrument,now=now,include_age=True)
    if reserve is not None:lines.append('⚠ TP reserve: '+reserve)
    lines.extend(drawdown_lines(root,instrument,capital,now))
    lines.append(peak_line(root,instrument))
    lines.append('✅ Все закрытые циклы сопоставлены.')
    if snapshot_at is not None and now-snapshot_at>900:
        lines.append('⚠️ Финансовая статистика устарела; суммы показаны на момент последней сверки.')
    return '\n'.join(lines)
