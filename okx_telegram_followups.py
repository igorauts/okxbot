from okx_telegram_wire import state_key
"""OKX Telegram follow-ups; local READ-ONLY observer.

Only reads existing bot JSON/SQLite. Never imports the engine, loads account
credentials, or sends/cancels an OKX order. All persistence is in the existing
Telegram monitor's OWN st dictionary (managed by okx_telegram_monitor.save_state).
"""
import hashlib
import html
import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from okx_paths import cycles_db, pnl_db, state_file
from okx_telegram_events import one
from okx_telegram_views import read_json, number, human_amount, money, date_epoch

TERMINAL = frozenset(('CLOSED', 'EXITED'))
MARKET_SOURCES = frozenset(('partial_exit', 'aggregate_exit'))


def _fmt_qty(raw):
    x = number(raw)
    if x is None:
        return 'н/д'
    return format(x.normalize(), 'f')


def _units(root):
    cfg = read_json(Path(root) / 'config.json')
    inst = str(cfg.get('instrument') or '')
    cv = number(cfg.get('contract_value'))
    if not inst.endswith('-USDT-SWAP') or not cv or cv <= 0:
        return None
    return cv, inst.split('-', 1)[0]


def _quantities(filled, total, cv, coin):
    if filled is None or total is None or total <= 0 or filled < 0 or filled > total:
        return None
    return (f'{_fmt_qty(filled)}/{_fmt_qty(total)} контрактов '
            f'({_fmt_qty(filled*cv)}/{_fmt_qty(total*cv)} {coin})')


def _read_db(path, sql, args):
    path = Path(path)
    if not path.is_file():
        return []
    # SQLite URI mode=ro: no database creation or journal-mode mutations.
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        return [json.loads(v) for (v,) in db.execute(sql, args)]


def _cycle(root, state, buy_id, cycle_id):
    """Exact immutable buy identity; never infer a cycle from grid price."""
    candidates = list(state.get('cycles', {}).values()) + list(state.get('archived_cycles', []))
    matches = [c for c in candidates if str(c.get('ordId') or '') == buy_id
               and str(c.get('cycleId') or '') == cycle_id]
    if len(matches) > 1:
        return None
    if matches:
        return matches[0]
    if not cycle_id:
        return None
    rows = _read_db(cycles_db(root), 'SELECT data FROM cycles WHERE id=?', (cycle_id,))
    if len(rows) != 1:
        return None
    c = rows[0]
    if str(c.get('ordId') or '') != buy_id or str(c.get('cycleId') or '') != cycle_id:
        return None
    return c


def _ledger(root, cycle):
    buy_id = str(cycle.get('ordId') or '')
    if not buy_id or cycle.get('state') not in TERMINAL:
        return None
    rows = _read_db(pnl_db(root),
        'SELECT data FROM accounted WHERE id=? OR id LIKE ?',
        ('chain:' + buy_id, buy_id + ':%'))
    rows = [r for r in rows if str(r.get('buy_id') or '') == buy_id]
    if len(rows) != 1 or rows[0].get('kind') != cycle['state']:
        return None
    r = rows[0]
    qty = number(cycle.get('filledBuy'))
    sold = number(cycle.get('filledTP'))
    booked = number(r.get('qty_contracts'))
    net = number(r.get('net'))
    if not qty or qty <= 0 or sold != qty or booked != qty or net is None:
        return None
    legs = [x for x in cycle.get('tpOrders', []) if (number(x.get('filled')) or 0) > 0]
    if not legs or sum((number(x.get('filled')) for x in legs), Decimal(0)) != qty:
        return None
    sell_ids = [str(x.get('ordId') or '') for x in legs]
    if any(not oid for oid in sell_ids) or sell_ids != list(map(str, r.get('sell_ids', []))):
        return None
    # An order's source is durable evidence of LIMIT vs MARKET; no guesses
    # from avgPx or taker fees (a crossed LIMIT can be TAKER).
    market = any(x.get('source') in MARKET_SOURCES for x in legs)
    limit = any(x.get('source') not in MARKET_SOURCES for x in legs)
    if market and limit:
        exit_kind = 'LIMIT + MARKET'
    elif market:
        exit_kind = 'MARKET SELL'
    else:
        exit_kind = 'LIMIT TP'
    return r, exit_kind


def _partial_message(name, kind, c, rec, cv, coin):
    if kind == 'BUY':
        filled = number(c.get('filledBuy'))
        total = number(c.get('sz'))
        px = c.get('buyPx')
        label = '⚠️ PARTIAL BUY'
    else:
        filled = number(rec.get('filled'))
        total = number(rec.get('sz'))
        px = rec.get('px') or c.get('tpPx')
        label = '⚠️ PARTIAL SELL TP'
    if filled is None or total is None or not (0 < filled < total):
        return None
    q = _quantities(filled, total, cv, coin)
    if q is None or number(px) is None:
        return None
    return f'{label} | {name}\n{q} | ${_fmt_qty(px)}'


def _followup_message(name, record, cycle, ledger, cv, coin):
    row, kind = ledger
    bought = number(cycle.get('filledBuy'))
    intended = number(cycle.get('sz'))
    orig_qty = _quantities(bought, intended, cv, coin)
    if not orig_qty:
        return None
    net = money(row['net'], 6, True)
    sell_px = number(row.get('sell_px'))
    if sell_px is None or sell_px <= 0:
        return None
    if record['side'] == 'BUY':
        title = '✅ PARTIAL BUY' if kind == 'LIMIT TP' else '🧹 PARTIAL BUY'
        line = 'BUY завершён: ' + orig_qty
    else:
        title = '✅ PARTIAL SELL TP' if kind == 'LIMIT TP' else '🧹 PARTIAL SELL TP'
        line = 'SELL завершён: ' + _quantities(bought, bought, cv, coin)
    return (f'{title} | {name}\n{line}\n'
            f'Закрытие: {kind} | ${_fmt_qty(sell_px)}\n'
            f'PnL цикла: {net} (net)')


def partial_scan(api, st, names, all_names=None):
    """Initial partial alerts and verified FINAL closed-cycle outcomes.

    Disappearance from current state does not resolve a watch.
    All existing follow-ups survive a restart via the monitor's persisted st.
    """
    now = time.time()
    since = st.setdefault('partial_tracking', {})
    watches = st.setdefault(state_key('partial_followups'), {})
    observed = set()
    states = {}
    cfgs = {}
    all_names = names if all_names is None else all_names
    for name, root in names.items():
        root = Path(root)
        spec = _units(root)
        if not spec:
            continue
        cv, coin = spec
        cfgs[name] = spec
        path = state_file(root)
        try:
            if now - path.stat().st_mtime > 30:
                continue
        except OSError:
            continue
        state = read_json(path)
        if not isinstance(state.get('cycles'), dict):
            continue
        states[name] = state
        for c in state['cycles'].values():
            buy_id = str(c.get('ordId') or '')
            cycle_id = str(c.get('cycleId') or '')
            if not buy_id or not cycle_id:
                continue
            if (c.get('state') == 'BUY_PARTIAL' and
                number(c.get('filledBuy')) is not None and
                number(c.get('sz')) is not None and
                0 < number(c['filledBuy']) < number(c['sz'])):
                key = f'{name}/BUY/{buy_id}'
                observed.add(key)
                original = number(c.get('partial_since'))
                fallback = now
                initial = float(original) if original is not None and 0 < original <= now else fallback
                since.setdefault(key, initial)
                if now - since[key] >= 2:
                    msg = _partial_message(name, 'BUY', c, None, cv, coin)
                    if msg:
                        one(api, st, 'partial/' + key, msg)
                        watches.setdefault(key, {'side': 'BUY', 'name': name,
                            'buy_id': buy_id, 'cycle_id': cycle_id, 'at': now})
            for rec in c.get('tpOrders', []) or []:
                if rec.get('source') in MARKET_SOURCES:
                    continue  # Covered by the existing aggregate/market notification.
                oid = str(rec.get('ordId') or rec.get('clOrdId') or '')
                filled = number(rec.get('filled'))
                qty = number(rec.get('sz'))
                if (oid and filled is not None and qty is not None and
                    0 < filled < qty and rec.get('phase') == 'ACTIVE'):
                    key = f'{name}/TP/{oid}'
                    observed.add(key)
                    since.setdefault(key, now)
                    if now - since[key] >= 2:
                        msg = _partial_message(name, 'TP', c, rec, cv, coin)
                        if msg:
                            one(api, st, 'partial/' + key, msg)
                            watches.setdefault(key, {'side': 'TP', 'name': name,
                                'buy_id': buy_id, 'cycle_id': cycle_id, 'at': now})
    for key in list(since):
        if key not in observed:
            del since[key]

    # Read durable ledger only for a watched, previously alerted partial;
    # ordinary BUY/TP events never trigger any Telegram message.
    for key, record in list(watches.items()):
        name = record.get('name')
        root = all_names.get(name)
        spec = cfgs.get(name) or (_units(root) if root is not None else None)
        if root is None or spec is None:
            continue
        cv, coin = spec
        try:
            state = states.get(name) or read_json(state_file(root))
            cycle = _cycle(root, state, record['buy_id'], record['cycle_id'])
            if cycle and cycle.get('state') in TERMINAL:
                ledger = _ledger(root, cycle)
                if ledger is not None:
                    msg = _followup_message(name, record, cycle, ledger, cv, coin)
                    if msg:
                        one(api, st, 'partial/final/' + key, msg)
                        del watches[key]
            # A PANIC may close all remaining exposure without normal CLOSED
            # cycle accounting. It has a separate independently verified report.
            elif (state.get('terminal_mode') == 'PANIC_DONE' and
                  _confirmed_panic(root, state) and
                  _final_stats(root, state) is not None):
                one(api, st, 'partial/final/' + key,
                    f'🏁 PARTIAL {record["side"]} | {name}\n'
                    'Позиция завершена через PANIC SELL. Результат — в финальном PANIC-отчёте.')
                del watches[key]
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, OverflowError):
            # Unknown/unfinished state is never announced as a filled trade.
            continue


def _confirmed_panic(root, state):
    if state.get('terminal_mode') != 'PANIC_DONE':
        return False
    snap = state.get('terminal_snapshot') or {}
    ack = read_json(Path(root) / 'state/control_ack.json')
    return (snap.get('mode') == 'PANIC_DONE' and number(snap.get('position_contracts')) == 0
            and ack.get('mode') == 'PANIC' and ack.get('phase') == 'PANIC_DONE'
            and number(ack.get('remaining_position')) == 0)


def _final_stats(root, state):
    """Accept ONLY terminal accounting proven by engine and same state hash."""
    root = Path(root)
    final = read_json(root / 'state/final_stats.json')
    cfg = read_json(root / 'config.json')
    if (final.get('schema_version') != 1 or final.get('complete') is not True or
        final.get('mode') != 'PANIC_DONE' or
        final.get('instrument') != cfg.get('instrument')):
        return None
    path = state_file(root)
    try:
        if final.get('terminal_state_sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
            return None
    except OSError:
        return None
    vals = final.get('values')
    if not isinstance(vals, dict):
        return None
    required = ('grid_profit', 'other_closed_profit', 'panic_pnl',
                'panic_sell_qty', 'panic_sell_fee', 'final_trade_pnl',
                'position_contracts', 'position_base', 'closed_cycles')
    nums = {k: number(vals.get(k)) for k in required}
    if any(v is None for v in nums.values()):
        return None
    if nums['position_contracts'] != 0 or nums['position_base'] != 0:
        return None
    if nums['closed_cycles'] < 0 or nums['panic_sell_qty'] < 0:
        return None
    if abs(nums['grid_profit'] + nums['other_closed_profit'] +
           nums['panic_pnl'] - nums['final_trade_pnl']) > Decimal('0.00000001'):
        return None
    spec = _units(root)
    if not spec:
        return None
    cv, coin = spec
    return final, nums, cv, coin


def _panic_message(name, verified):
    final, v, cv, coin = verified
    closed = date_epoch(final.get('closed_at'))
    if closed is None:
        return None
    stamp = datetime.fromtimestamp(closed, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    rows = [f'<b>🏁 PANIC SELL | {html.escape(name)} | завершён</b>',
            html.escape(stamp), '',
            f'Закрытых циклов: {int(v["closed_cycles"]):,}'.replace(',', ' '),
            f'GRID PROFIT: {money(v["grid_profit"], 2, True)}']
    if v['other_closed_profit']:
        rows.append(f'Другие выходы: {money(v["other_closed_profit"], 2, True)}')
    rows += [f'PANIC SELL PnL: {money(v["panic_pnl"], 2, True)}',
             f'PANIC SELL объём: {_fmt_qty(v["panic_sell_qty"]*cv)} {html.escape(coin)}',
             f'PANIC SELL комиссия: {money(v["panic_sell_fee"], 2, True)}',
             f'Открытая позиция: 0 {html.escape(coin)}', '',
             f'<b>ИТОГОВЫЙ PnL: {money(v["final_trade_pnl"], 8, True)}</b>']
    return '\n'.join(rows)


def panic_scan(api, st, names):
    """Two independent Telegram milestones; archived bots aren't replayed."""
    since = st.setdefault(state_key('observing_since'), time.time())
    baseline = st.setdefault(state_key('panic_seen'), {})
    for name, root in names.items():
        root = Path(root)
        state = read_json(state_file(root))
        if not _confirmed_panic(root, state):
            if state.get('terminal_mode') not in ('RETIRED', 'PANIC_DONE'):
                baseline.setdefault(name, 'observed_running')
            continue
        final = read_json(root / 'state/control_ack.json')
        closed_at = date_epoch(final.get('updated_at'))
        # Initial installation/restart must not flood chat with old archived PANICs.
        # First observed terminal after this monitor was already running is OK.
        if name not in baseline:
            baseline[name] = ('observed_running' if closed_at is not None and
                              closed_at >= since else 'old_terminal')
        if baseline[name] == 'old_terminal':
            continue
        snapshot = state.get('terminal_snapshot') or {}
        marker = str(snapshot.get('at_utc') or final.get('updated_at') or '')
        if not marker:
            continue
        verified = _final_stats(root, state)
        if verified is not None:
            msg = _panic_message(name, verified)
            if msg:
                one(api, st, f'panic/final/{name}/{marker}', msg, html_mode=True)
        else:
            age = time.time() - (closed_at if closed_at is not None else since)
            if age > 15:
                one(api, st, f'panic/confirmed/{name}/{marker}',
                    f'✅ PANIC SELL | {name}\n'
                    'Позиция 0, ордера закрыты — подтверждено торговым движком по OKX.\n'
                    'Финальный финансовый расчёт ещё ожидается.')
            if age > 60:
                one(api, st, f'panic/accounting_wait/{name}/{marker}',
                    f'⚠️ PANIC SELL | {name}\nФинальный PnL не подтверждён более 60 секунд.')
