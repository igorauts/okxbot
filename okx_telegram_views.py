"""Read-only Telegram presentation and diagnostics for OKX Grid Bot v0.7.16.

Never imports a trading engine, reads API credentials, mutates bot state or calls OKX.
"""
import html
import json
import math
import re
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from okx_paths import state_file, pnl_db, fills_db, cycles_db, observer_dir

LINE = '────────────────────────'
COUNTS = re.compile(r'\bBUY\s+(\d+)/(\d+)\s*\|\s*TP\s+(\d+)\s*\(реальных\s+(\d+)\)')
MODE = re.compile(r'РЕЖИМ=([A-Z_]+)')


def read_json(path):
    try:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def date_epoch(ts):
    try:
        if isinstance(ts, (int, float)):
            return float(ts)
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def number(raw):
    try:
        n = Decimal(str(raw).replace(',', '').replace('−', '-').replace(' ', ''))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def human_amount(raw, places=2, sign=False):
    val = number(raw)
    if val is None:
        return 'н/д'
    val = val.quantize(Decimal('1').scaleb(-places), rounding=ROUND_HALF_UP)
    return ('+' if sign and val > 0 else '−' if val < 0 else '') + f'{abs(val):,.{places}f}'


def money(raw, places=2, sign=False):
    s = human_amount(raw, places, sign)
    return s if s == 'н/д' else s.replace('−', '−$').replace('+', '+$') if s[0] in '+−' else '$' + s


def fmt_source(raw, *, digits=2, money_value=False, signed=False, percent=False):
    """Round every numeric token while preserving the source's surrounding text."""
    if not raw or raw == 'н/д':
        return 'н/д'
    s = str(raw).replace('\u00a0', ' ').replace('−', '-')
    pattern = re.compile(r'(?<![A-Za-z0-9])([+\-]?\s*\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(%?)(?![A-Za-z0-9])')
    def sub(m):
        inp = m.group(1).replace('$', '').replace(' ', '')
        n = number(inp)
        if n is None:
            return m.group(0)
        dec = human_amount(n, digits, sign=signed or inp.startswith('+'))
        sym = '$' if ('$' in m.group(1) or (money_value and not m.group(2))) else ''
        if dec[0] in '+−':
            dec = dec[0] + sym + dec[1:]
        else:
            dec = sym + dec
        return dec + ('%' if m.group(2) else '')
    return re.sub(r' +\(', ' (', pattern.sub(sub, s))


def report_map(text):
    result = {}
    for row in str(text).splitlines():
        k, sep, v = row.partition(':')
        if sep:
            result[k.strip().casefold()] = v.strip()
    return result


def value(values, *keys):
    for k in keys:
        v = values.get(k.casefold())
        if v and v != '—':
            return v
    return 'н/д'


def price_digits(cfg):
    tick = number(cfg.get('tick_size'))
    return max(2, -tick.normalize().as_tuple().exponent) if tick is not None and tick > 0 else 2


def verified_snapshot_bid(root, cfg, stats):
    """Price from the same confirmed stats sample, never its rounded report.

    The v0.7.21+ running engine publishes stats_financial_anchor.json and
    stats.json with the *same* timestamp, in that order. The two files can
    briefly be from different samples, so fail closed rather than show a
    spurious $0.000000 / -100% price change. Read-only, no exchange calls.
    """
    anchor = read_json(Path(root) / 'state' / 'stats_financial_anchor.json')
    if anchor.get('schema') != 1 or anchor.get('instrument') != cfg.get('instrument'):
        return None
    if not stats.get('complete') or stats.get('instrument') != cfg.get('instrument'):
        return None
    at = number(anchor.get('at'))
    generated = number(stats.get('generated_at_unix'))
    bid = number(anchor.get('bid'))
    if (at is None or generated is None or at <= 0 or generated <= 0
            or abs(at - generated) > Decimal('0.5')
            or bid is None or bid <= 0):
        return None
    return bid


def highest_open_filled_buy_price(root, cfg):
    """Highest entry price of an executed BUY with a still-open TP cycle.

    Read only already-persisted bot cycle state; not active unfilled BUY limits,
    config upper_price, or SELL TP limit price. No exchange queries.
    """
    state = read_json(state_file(root))
    instrument = state.get('instrument')
    if instrument and instrument != cfg.get('instrument'):
        return 'н/д'
    cycles = state.get('cycles')
    if not isinstance(cycles, dict):
        return 'н/д'
    prices = []
    for cycle in cycles.values():
        if not isinstance(cycle, dict):
            continue
        if cycle.get('state') not in ('TP_WAIT', 'TP_PARTIAL', 'VIRTUAL_TP'):
            continue
        bought = number(cycle.get('filledBuy'))
        sold = number(cycle.get('filledTP') or '0')
        px = number(cycle.get('buyPx'))
        if bought is None or sold is None or px is None:
            continue
        if bought > sold >= 0 and px > 0:
            prices.append(px)
    return '$' + human_amount(max(prices), price_digits(cfg)) if prices else '—'


def status_one(name, root, age_text='н/д'):
    root = Path(root)
    stats = read_json(root/'state/stats.json')
    cfg = read_json(root/'config.json')
    if not stats.get('complete') or stats.get('instrument') != cfg.get('instrument'):
        return '<b>'+html.escape(name)+'</b>\nСтатистика недоступна/не подтверждена'
    values = report_map(stats.get('report', ''))
    ver = str(cfg.get('version') or stats.get('bot_version') or '?').lstrip('v')
    first = ', '.join([name, 'v'+ver, age_text])
    def row(title, val):
        return title+': '+val
    # Engine report may contain $0.00 for inexpensive assets (v0.7.21).
    # Anchor and report must share an identical sample timestamp.
    bid_exact = verified_snapshot_bid(root, cfg, stats)
    bid = ('$' + human_amount(bid_exact, price_digits(cfg))
           if bid_exact is not None else 'н/д')
    up = highest_open_filled_buy_price(root, cfg)
    pos = value(values, 'Открытая позиция')
    # Preserve valid instrument name; remove insignificant quantity trailing zeros.
    p = re.match(r'^\s*([+−-]?[\d,.]+)\s+(\S+)\s*$', pos)
    if p and number(p.group(1)) is not None:
        pos = format(number(p.group(1)), 'f').rstrip('0').rstrip('.') + ' ' + p.group(2) if '.' in format(number(p.group(1)), 'f') else format(number(p.group(1)), 'f') + ' ' + p.group(2)
    subtotal = fmt_source(value(values,'ИТОГОВЫЙ PnL'))
    sub_n = number(re.search(r'[+\-−]?\$?([\d,.]+)',subtotal.replace('−','-')).group(0).replace('$','')) if re.search(r'[+\-−]?\$?([\d,.]+)',subtotal.replace('−','-')) else None
    icon = '🟩' if sub_n is not None and sub_n >= 0 else '🟥'
    # Read the authoritative local 5s DD record, not a stale 10-minute report.
    # No exchange requests; schema/instrument checked before displaying.
    dd_fallback = fmt_source(value(values,'МАКС. ПРОСАДКА БОТА').replace(' от 1x',''))
    dd_record = read_json(root/'state/bot_drawdown.json')
    dd = dd_fallback
    if dd_record.get('schema') == 1 and dd_record.get('instrument') == cfg.get('instrument'):
        amount = number(dd_record.get('max_drawdown_usdt'))
        if amount is not None and amount >= 0:
            dd = '−$'+human_amount(amount,2)
            capital_text = value(values,'Расчётный капитал для 1x')
            match = re.search(r'\$?([\d,]+(?:\.\d+)?)', capital_text)
            capital = number(match.group(1)) if match else None
            if capital is not None and capital > 0:
                dd += ' (−'+human_amount(amount * 100 / capital,2)+'%)'
    # Price difference from the highest executed, still-open BUY (not active BUY limit).
    # Telegram view only: two already-persisted values, zero OKX requests.
    _bid_num=bid_exact
    _grid_num=number(up.replace('$',''))
    _price_change=None
    if _bid_num is not None and _grid_num is not None and _grid_num>0:
        _price_change=human_amount((_bid_num/_grid_num-1)*100,2,sign=True)+'%'
    main = [
        row('Верх сетки',up),
        row('Текущий bid',bid+(' ('+_price_change+')' if _price_change is not None else '')),
        row('Закрытых циклов',value(values,'Закрытых циклов')),
        row('Грид профит',fmt_source(value(values,'GRID PROFIT'))),
        row('Торговый объём',fmt_source(value(values,'Общий объём'))),
        row('Уплаченная комиссия',fmt_source(value(values,'Уплачено комиссий'))),
        row('Прибыль за последние 24ч',fmt_source(value(values,'Прибыль за последние 24ч'))),
        row('Открытая позиция',pos),
        row('Floating PnL',fmt_source(value(values,'Нереализованный PnL'))),
    ]
    parts = ['<b>'+html.escape(first)+'</b>', '\n'.join('<code>'+html.escape(line)+'</code>' for line in main),
             '<b>'+icon+' Итоговый PnL: '+html.escape(subtotal)+' '+icon+'</b>',
             '<code>'+html.escape('Максимальная просадка бота: '+dd)+'</code>']
    if time.time()-float(stats.get('generated_at_unix') or 0)>1200:
        parts.append('⚠️ Статистика устарела')
    return '\n\n'.join(parts)


def quantiles(values):
    """P50/P95/P99/MAX of existing local samples; no new measurements."""
    if not values:
        return 'н/д'
    ordered = sorted(values)
    def pct(p):
        at = (len(ordered) - 1) * p
        lower = int(at)
        val = ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (at - lower)
        return round(val)
    p50, p95, p99, maximum = (pct(.50), pct(.95), pct(.99), round(ordered[-1]))
    return f'{p50:>5} / {p95:>5} / {p99:>5} / {maximum:>5} мс'


def jsonl(path, start_epoch):
    try:
        with Path(path).open('rt', encoding='utf-8', errors='replace') as file:
            for line in file:
                try:
                    row=json.loads(line)
                except (ValueError,TypeError):
                    continue
                ts=date_epoch(row.get('ts'))
                if ts is not None and start_epoch <= ts <= time.time()+30:
                    yield row
    except OSError:
        return


def journal(name, hours):
    """Read only journal, no status reload or mutation; prevents huge outputs."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,60}',name):
        return []
    try:
        res=subprocess.run(['journalctl','-u','okx-grid-'+name+'.service',
             '--since',f'{int(hours*3600)} seconds ago', '-o','short-iso-precise','--no-pager'],
             capture_output=True,text=True,timeout=25,check=False)
        return res.stdout.splitlines() if res.returncode == 0 else []
    except (OSError,subprocess.TimeoutExpired):
        return []


def engine_count_history(name, hours, cutoff):
    points=[]
    for line in journal(name,hours):
        match=COUNTS.search(line)
        mode=MODE.search(line)
        if not match or not mode:
            continue
        stamp=date_epoch(line.split(' ',1)[0])
        if stamp is None or stamp < cutoff:
            continue
        b,target,_,_=map(int,match.groups())
        # 'WAITING' when outside upper price is an expected state.
        expected = (mode.group(1)=='RUN' and 'WAITING' not in line
                    and 'VIP fee unavailable' not in line)
        points.append((stamp,b,target,expected))
    return points


def deficit_summary(points):
    min_cov=None
    below=[]
    zero=[]
    active_start=None
    zero_start=None
    episodes=0
    for stamp,b,target,expected in points:
        if not expected:
            active_start=None;zero_start=None
            continue
        if min_cov is None or b/target < min_cov[0]/min_cov[1]:
            min_cov=(b,target)
        if b<target:
            if active_start is None:
                active_start=stamp;episodes+=1
            if b==0 and zero_start is None:
                zero_start=stamp
            if b>0 and zero_start is not None:
                zero.append(max(0,stamp-zero_start));zero_start=None
        else:
            if active_start is not None:
                below.append(max(0,stamp-active_start));active_start=None
            if zero_start is not None:
                zero.append(max(0,stamp-zero_start));zero_start=None
    # Open intervals are not claimed as completed durations.
    return min_cov,episodes,len(zero)+(1 if zero_start is not None else 0),zero,below


def latency_one(name, root, hours):
    root=Path(root)
    cutoff=time.time()-hours*3600
    sources=sorted(root.glob('logs/latency*.jsonl*'))
    if (root/'logs/latency.jsonl').is_file():
        sources=[p for p in sources if not p.name.startswith('latency_v0711') or p.name.startswith('latency.jsonl')]
    phases=defaultdict(list)
    sample_count=0
    for src in sources:
        for row in jsonl(src,cutoff):
            phase=row.get('phase')
            if phase in ('loop_pass','positions_gate_fifo_wait','buy_post','http','position_reconcile'):
                n=row.get('ms')
                if isinstance(n,(int,float)) and math.isfinite(n) and 0<=n<1_000_000:
                    phases[phase].append(float(n))
                    sample_count+=1
    paths = list([observer_dir(root,'burst')/'bursts.jsonl'] if (observer_dir(root,'burst')/'bursts.jsonl').is_file() else list(root.glob('diagnostics/burst_*/bursts.jsonl')))
    bursts=[x for p in paths for x in jsonl(p,cutoff)]
    paths = list([observer_dir(root,'execution')/'alerts.jsonl'] if (observer_dir(root,'execution')/'alerts.jsonl').is_file() else list(root.glob('diagnostics/execution_*/alerts.jsonl')))
    alerts=[x for p in paths for x in jsonl(p,cutoff)]
    points=engine_count_history(name,hours,cutoff)
    mincov,episodes,zeros,zero_durations,recoveries=deficit_summary(points)
    target = read_json(root/'config.json').get('active_buy_orders','?')
    def coverage(pair):
        return f'{pair[0]}/{pair[1]}' if pair else 'н/д'
    mx_fill = max((int(b.get('counts',{}).get('BUY_FILLED',0)) + int(b.get('counts',{}).get('BUY_FILLED_DURING_CANCEL',0)) for b in bursts),default=0)
    minburst=min((int(b['min_buy']) for b in bursts if b.get('min_buy') is not None),default=None)
    warnings=len([x for x in alerts if x.get('kind') not in ('ENGINE_BUY_BELOW_TARGET','ENGINE_BUY_RECOVERED')])
    if sample_count == 0 and not bursts and not points:
        return f'<b>⚡ {html.escape(name)}</b>\nНет измерений за период (или журнал не сохранён).'
    rows=[
        'Цикл:     '+quantiles(phases['loop_pass']),
        'FIFO:     '+quantiles(phases['positions_gate_fifo_wait']),
        'BUY POST: '+quantiles(phases['buy_post']),
        'REST:     '+quantiles(phases['http']),
        'Сверка:   '+quantiles(phases['position_reconcile']),
        '',
        'BUY DEFICIT',
        f'Эпизодов: {episodes}, минимум: {coverage(mincov)}',
        f'Полный ноль: {zeros} раз, макс. ≈{max(zero_durations,default=0):.2f} с' if zeros else 'Полный ноль: 0 наблюдалось',
        'Восстановление P50/P95: '+(quantiles([x*1000 for x in recoveries]) if recoveries else 'н/д'),
        '',
        'BURST',
        f'Всего: {len(bursts)}, макс. BUY fills: {mx_fill}',
        f'Минимум BUY: {str(minburst)+"/"+str(target) if minburst is not None else "н/д"}',
        f'Execution: {warnings} предупреждений',
        f'Записей latency: {sample_count:,}',
    ]
    lines=[f'<b>⚡ {html.escape(name)}</b>',
           '\n'.join('<code>'+html.escape(row)+'</code>' if row else '' for row in rows)]
    if zeros:
        lines.append('<i>0/N и длительности — по наблюдавшимся строкам journald, не непрерывный биржевой замер.</i>')
    return '\n'.join(lines)


def latency_report(bots, hours=1):
    if hours not in (1,8):
        return 'Допустимо: /latency или /latency8h'
    blocks=['<b>⚡ OKX LATENCY | '+('60 минут' if hours==1 else '8 часов')+'</b>',
            '<i>Время: P50 / P95 / P99 / MAX, локальные наблюдения</i>']
    for name,root in bots:
        blocks.append(latency_one(name,root,hours))
    if not bots:
        blocks.append('Нет активных ботов.')
    return ('\n'+LINE+'\n').join(blocks)


def _readonly_connect(path):
    import sqlite3
    # URI mode=ro: never creates DB, never modifies journal mode.
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True, timeout=2)


def market_ledger(root, event):
    """Return verified group net + estimated LIMIT net, or None if incomplete.

    Read-only SQLite. No partial fees or fabricated profits. Same-sell-order
    single-leg accounted cycles only; multi-leg cycles defer for audit.
    """
    root=Path(root)
    oid=str(event.get('ordId') or '')
    if not oid or event.get('state')!='FILLED':
        return None
    state=read_json(state_file(root))
    cfg=read_json(root/'config.json')
    groups=[g for g in state.get('aggregate_exits',[]) if str(g.get('ordId'))==oid]
    if len(groups)!=1 or groups[0].get('phase')!='FILLED':
        return None
    group=groups[0]
    plan=group.get('plan',[])
    if not isinstance(plan,list) or not plan:
        return None
    qty=number(group.get('sz'))
    cv=number(cfg.get('contract_value'))
    rate=number(cfg.get('maker_fee_at_setup'))
    if not qty or not cv or rate is None or rate<0 or rate>=1:
        return None
    rows=[]
    try:
        with _readonly_connect(pnl_db(root)) as db:
            for raw, in db.execute('SELECT data FROM accounted'):
                data=json.loads(raw)
                if oid in list(map(str,data.get('sell_ids',[]))):
                    rows.append(data)
    except (OSError,ValueError,Exception):
        return None
    if len(rows)!=len(plan):
        return None
    if any(list(map(str,r.get('sell_ids',[])))!=[oid] or r.get('kind')!='CLOSED' for r in rows):
        return None
    if sum((number(r.get('qty_contracts')) or Decimal(0) for r in rows),Decimal(0))!=qty:
        return None
    # Sum each BUY order's real fee ONCE, verified by stored exchange fills.
    buy_fees=Decimal(0)
    try:
        with _readonly_connect(fills_db(root)) as db:
            for result in rows:
                buyid=str(result.get('buy_id') or '')
                if not buyid:
                    return None
                matched=[json.loads(raw) for raw, in db.execute('SELECT data FROM fills WHERE order_id=?',(buyid,))]
                if sum((number(f.get('fillSz')) or Decimal(0) for f in matched),Decimal(0)) != number(result.get('qty_contracts')):
                    return None
                if any(f.get('side')!='buy' or f.get('feeCcy')!='USDT' or number(f.get('fee')) is None for f in matched):
                    return None
                buy_fees += sum((number(f['fee']) for f in matched),Decimal(0))
    except (OSError,ValueError,Exception):
        return None
    if any(number(r.get('net')) is None or number(r.get('planned_gross')) is None
           or number(r.get('qty_base')) is None or number(r.get('buy_px')) is None
           for r in rows):
        return None
    actual=sum((number(r['net']) for r in rows),Decimal(0))
    planned_gross=sum((number(r['planned_gross']) for r in rows),Decimal(0))
    # Original LIMIT TP price must come from each persisted cycle, NOT from
    # ledger.planned_gross, because the recorded limit BUY price may differ
    # from actual BUY VWAP. Calculate plan at *actual* BUY VWAP and original TP.
    all_cycles=list(state.get('cycles',{}).values())+list(state.get('archived_cycles',[]))
    by_buy={}
    for c in all_cycles:
        buy_id=str(c.get('ordId') or '')
        if buy_id:
            if buy_id in by_buy:
                # Duplicate identity -> ambiguous accounting, fail closed.
                return None
            by_buy[buy_id]=c
    # The engine compacts old completed cycles to cycles_v068.sqlite. A Market
    # group may be finalized before Telegram reads it, leaving some parent
    # cycles absent from bot04_state.json; look them up READ ONLY in the archive.
    missing={str(r.get('buy_id') or '') for r in rows} - set(by_buy)
    if missing:
        archive=cycles_db(root)
        if not archive.is_file():
            return None
        try:
            with _readonly_connect(archive) as db:
                for (raw,) in db.execute('SELECT data FROM cycles'):
                    c=json.loads(raw)
                    buyid=str(c.get('ordId') or '')
                    if buyid in missing:
                        if buyid in by_buy: return None
                        by_buy[buyid]=c
                        missing.remove(buyid)
                        if not missing: break
        except (OSError,ValueError,TypeError,Exception):
            return None
        if missing: return None
    expected_gross=Decimal(0)
    expected_sell_fee=Decimal(0)
    for result in rows:
        cycle=by_buy.get(str(result.get('buy_id')))
        if not cycle:
            return None
        tp=number(cycle.get('tpPx'))
        size=number(result.get('qty_base'))
        buy_vwap=number(result.get('buy_px'))
        if tp is None or tp<=0 or size is None or size<=0 or buy_vwap is None or buy_vwap<=0:
            return None
        expected_gross+=(tp-buy_vwap)*size
        expected_sell_fee+=tp*size*rate
    plan_net=expected_gross+buy_fees-expected_sell_fee
    return dict(actual=actual,plan=plan_net,execution=actual-plan_net,
                qty_base=qty*cv,cycles=len(rows),order_id=oid)


def signed_usd6(value):
    return money(value,6,True)


# v0.7.29 Market SELL UI (accounting unchanged)
def market_message(name, root, event, timing):
    v=market_ledger(root,event)
    if v is None:
        return None
    unit=read_json(Path(root)/'config.json').get('instrument','').split('-')[0]
    def ms(key, approx=False):
        n=timing.get(key)
        return ('≈' if approx else '')+str(int(round(n)))+' мс' if isinstance(n,(int,float)) and n>=0 else 'н/д'
    actual=v['actual']
    execution=v['execution']
    circle='🟢' if actual>0 else '🔴' if actual<0 else '⚪'
    square='🟩' if execution>0 else '🟥' if execution<0 else '⬜'
    header=(f'💰 Market SELL {format(v["qty_base"].normalize(),"f")} {unit} '
            f'({v["cycles"]}), {signed_usd6(actual)} в копилку ({circle})')
    return ('<b>'+html.escape(header)+'</b>\n'
            +html.escape(f'Исполнение {signed_usd6(execution)} {square}')+'\n'
            +'<code>'+html.escape('⚡ Fill '+ms('fill',True)+' | ACK '+ms('ack')+' | Full '+ms('full',True))+'</code>')
