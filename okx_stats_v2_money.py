"""Offline-only phase 2: bounded-component frozen financial contributions.

DO NOT import into a trading process.  The reference FULL function is injected
by the offline tests.  This module is not a complete incremental StatsWorker:
no LIVE wiring, no recovery scheduler and no publication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from okx_stats_v2_contract import Drift, freeze_readiness, money, pnl_identity, component_orders

DAY_MS = 86400000
ROLLING_RETAIN_MS = 25 * 3600 * 1000
MONEY_FIELDS = (
    'grid_gain', 'partial_exit_net', 'open_gain', 'floating_pnl',
    'limit_tp_net', 'market_tp_net', 'maker_volume', 'taker_volume',
    'maker_fee', 'taker_fee',
)
COUNT_FIELDS = ('closed_count', 'partial_exit_count', 'expected_orders')


def _cycle_id(c):
    cid = str(c.get('cycleId') or '')
    if not cid:
        raise Drift('Missing component cycleId')
    return cid


def _rows_by_order(cycles, fills):
    """Only a component's fills. Never use this for whole historical database."""
    wanted = set().union(*(component_orders(c) for c in cycles))
    byorder = {}
    seen = set()
    for f in fills:
        oid, bill = str(f.get('ordId') or ''), str(f.get('billId') or '')
        if oid not in wanted or not bill or (oid, bill) in seen:
            raise Drift('Unexpected/duplicate/missing scoped fill identity')
        seen.add((oid, bill))
        byorder.setdefault(oid, []).append(f)
    return byorder


def _closed_events(cycles, byorder, contract_value, allocate):
    """Exact source _last_24h_profit closed event rule (last SELL fill ts)."""
    events = []
    for c in cycles:
        if c.get('state') != 'CLOSED':
            continue
        buy_id = str(c.get('ordId') or '')
        buys = byorder.get(buy_id, [])
        legs = [r for r in c.get('tpOrders', []) if money(r.get('filled', 0)) > 0]
        if not legs and c.get('tpOrdId'):
            legs = [{'ordId': c['tpOrdId'], 'filled': c.get('filledTP', '0')}]
        sells = []
        for leg in legs:
            oid = str(leg.get('ordId') or '')
            rows = byorder.get(oid, [])
            if leg.get('source') == 'aggregate_exit':
                rows = allocate(rows, leg['aggregatePlan'], _cycle_id(c))
            sells.extend(rows)
        if not buys or not sells:
            raise Drift('Missing closed-cycle fill or allocation')
        try:
            ts_values = [int(r.get('ts') or r.get('fillTime')) for r in sells]
        except (TypeError, ValueError) as exc:
            raise Drift('Invalid SELL timestamp') from exc
        if any(ts <= 0 for ts in ts_values):
            raise Drift('Invalid SELL timestamp')
        ts = max(ts_values)
        sell_net = sum((money(f['fillPx']) * money(f['fillSz']) * contract_value + money(f['fee'])
                        for f in sells), Decimal(0))
        buy_cost = sum((money(f['fillPx']) * money(f['fillSz']) * contract_value - money(f['fee'])
                        for f in buys), Decimal(0))
        events.append((ts, sell_net - buy_cost))
    return tuple(events)


@dataclass(frozen=True)
class FrozenChunk:
    """Verified immutable component's additive contribution; O(component), not O(history)."""
    monetary: dict[str, str]
    counts: dict[str, int]
    closed_buy_nominal: str
    close_events: tuple[tuple[int, str], ...]
    orders: tuple[str, ...]
    ledger_total: str


def verified_frozen_chunk(cycles: list[dict], fills: list[dict], accounted: dict,
                          contract_value, *, full_oracle: Callable,
                          allocate: Callable, bid=Decimal('1')) -> FrozenChunk:
    """Fail-closed bounded monetary calculation delegated to the original FULL oracle.

    Caller must independently prove every cycle was captured from an archived
    row, is absent from state, and is a whole closed connected component.
    The caller must also pin the SQLite source snapshots and source checkpoint.
    """
    if not cycles or not isinstance(accounted, dict):
        raise Drift('Invalid frozen component')
    ids = [_cycle_id(c) for c in cycles]
    if len(ids) != len(set(ids)):
        raise Drift('Duplicate cycle in component')
    cv, bid = money(contract_value), money(bid)
    if cv <= 0 or bid <= 0:
        raise Drift('Invalid contract value or BID')
    # Restrict the phase-1 permissive eligibility check: a CANCELED cycle
    # must have neither BUY nor TP fills, and terminal finance must have
    # genuine matched BUY/SELL quantities.
    for cycle in cycles:
        status = cycle.get('state')
        buy_qty = money(cycle.get('filledBuy', 0))
        sell_qty = money(cycle.get('filledTP', 0))
        if status == 'CANCELED' and (buy_qty != 0 or sell_qty != 0):
            raise Drift('Nonempty CANCELED component cannot be frozen')
        if status in ('CLOSED','EXITED') and (buy_qty <= 0 or sell_qty <= 0):
            raise Drift('Terminal component has zero BUY/SELL quantity')
    byorder = _rows_by_order(cycles, fills)
    ready, reason = freeze_readiness(cycles, set(ids), set(), byorder, accounted)
    if not ready:
        raise Drift('Freeze denied: ' + reason)
    try:
        # The canonical full_stats_snapshot is used for monetary attribution.
        # A component contains all common MARKET orders: no shared order may
        # be split across component boundaries.
        state = {'cycles': {}, 'archived_cycles': cycles, 'completed': 0}
        result = full_oracle(state, fills, bid, cv,
                             include_durable_archive=False, snapshot_archive=[])
        if not result.get('complete'):
            raise Drift('Original FULL oracle incomplete')
        if result['open_gain'] != 0 or result['floating_pnl'] != 0:
            raise Drift('Non-terminal financial contribution would be frozen')
        events = _closed_events(cycles, byorder, cv, allocate)
        if sum((net for _, net in events), Decimal(0)) != result['grid_gain']:
            raise Drift('24h events do not reconcile with closed grid profit')
        expected_ledger = Decimal(0)
        for c in cycles:
            if c.get('state') == 'CANCELED':
                continue
            key = pnl_identity(c)
            entry = accounted.get(key)
            if not entry:
                raise Drift('Missing PnL ledger row')
            expected_ledger += money(entry['net'])
        if abs(expected_ledger - (result['grid_gain'] + result['partial_exit_net'])) > Decimal('0.00000001'):
            raise Drift('PnL journal vs FULL oracle mismatch')
        closed_nominal = Decimal(0)
        for c in cycles:
            if c.get('state') != 'CLOSED':
                continue
            for row in byorder.get(str(c.get('ordId') or ''), ()):
                closed_nominal += money(row['fillPx']) * money(row['fillSz']) * cv
        # Every quantity in the frozen ledger is stored as an exact decimal string.
        monetary = {field: str(money(result[field])) for field in MONEY_FIELDS}
        counts = {field: int(result[field]) for field in COUNT_FIELDS}
        orders = tuple(sorted(set().union(*(component_orders(c) for c in cycles))))
        if counts['expected_orders'] > len(orders):
            raise Drift('Unexpected frozen order count')
        return FrozenChunk(
            monetary=monetary, counts=counts,
            closed_buy_nominal=str(closed_nominal),
            close_events=tuple((ts, str(net)) for ts, net in events),
            orders=orders, ledger_total=str(expected_ledger),
        )
    except Drift:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise Drift('Original FULL chunk rejected: ' + type(exc).__name__) from exc


class FrozenTotals:
    """Small in-memory accumulator; no cycle IDs/order IDs or fill rows retained.

    ADD ONLY after the caller has durably reserved the component via FrozenIndex.
    This class deliberately has no financial publication or persistence API.
    """
    def __init__(self, *, now_ms: int):
        self.now_ms = int(now_ms)
        self.money = {k: Decimal(0) for k in MONEY_FIELDS}
        self.counts = {k: 0 for k in COUNT_FIELDS}
        self.closed_buy_nominal = Decimal(0)
        self.ledger_total = Decimal(0)
        self.days = {}
        self.recent = []

    def add(self, chunk: FrozenChunk):
        # Validate first: avoid mutating totals on bad input.
        vals = {k: money(chunk.monetary[k]) for k in MONEY_FIELDS}
        nums = {k: chunk.counts[k] for k in COUNT_FIELDS}
        if any(type(n) is not int or n < 0 for n in nums.values()):
            raise Drift('Invalid counts')
        nominal = money(chunk.closed_buy_nominal)
        ledger = money(chunk.ledger_total)
        if nominal < 0:
            raise Drift('Negative frozen BUY nominal')
        new_days = {}
        recent = []
        for ts, raw in chunk.close_events:
            if type(ts) is not int or ts <= 0 or ts > self.now_ms + 3600*1000:
                raise Drift('Invalid future/negative close timestamp')
            net = money(raw)
            day = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()
            new_days[day] = new_days.get(day, Decimal(0)) + net
            if ts >= self.now_ms - ROLLING_RETAIN_MS:
                recent.append((ts, str(net)))
        for key, val in vals.items():
            self.money[key] += val
        for key, val in nums.items():
            self.counts[key] += val
        self.closed_buy_nominal += nominal
        self.ledger_total += ledger
        for day, net in new_days.items():
            self.days[day] = self.days.get(day, Decimal(0)) + net
        self.recent.extend(recent)

    def roll_forward(self, now_ms: int):
        now_ms = int(now_ms)
        if now_ms < self.now_ms:
            raise Drift('Wall clock moved backwards')
        self.now_ms = now_ms
        cutoff = now_ms - ROLLING_RETAIN_MS
        self.recent = [(ts, net) for ts, net in self.recent if ts >= cutoff]

    def last_24h(self, now_ms: int):
        now_ms = int(now_ms)
        if now_ms < self.now_ms:
            raise Drift('Wall clock moved backwards')
        cutoff = now_ms - DAY_MS
        return sum((money(net) for ts, net in self.recent if ts >= cutoff), Decimal(0))

    def safe_snapshot(self):
        """No array of historical orders, fills or cycle IDs in persisted result."""
        return {
            'money': {k: str(v) for k, v in self.money.items()},
            'counts': dict(self.counts),
            'closed_buy_nominal': str(self.closed_buy_nominal),
            'ledger_total': str(self.ledger_total),
            'days_utc': {k: str(v) for k, v in self.days.items()},
            'last_25h_events': list(self.recent),
            'as_of_ms': self.now_ms,
        }


def compose_with_hot(frozen: FrozenTotals, hot: dict, hot_closed_nominal,
                     hot_open_nominal, state: dict, bid, contract_value,
                     *, stats_pct: Callable):
    """Offline financial partition composition (not a publishable result)."""
    cv, bid = money(contract_value), money(bid)
    summation = lambda k: frozen.money[k] + money(hot[k])
    count = lambda k: frozen.counts[k] + int(hot[k])
    closed = summation('grid_gain')
    open_net = summation('open_gain')
    exited = summation('partial_exit_net')
    maker, taker = summation('maker_volume'), summation('taker_volume')
    mfee, tfee = summation('maker_fee'), summation('taker_fee')
    buys = frozen.closed_buy_nominal + money(hot_closed_nominal)
    open_buys = money(hot_open_nominal)
    active = list(state.get('cycles', {}).values())
    open_qty = sum((money(c.get('filledBuy',0)) - money(c.get('filledTP',0)) for c in active), Decimal(0))
    total = closed + open_net + exited
    return {
        'complete': True, 'bid': bid, 'closed_count': count('closed_count'),
        'partial_exit_count': count('partial_exit_count'), 'partial_exit_net': exited,
        'active_buys': sum(c.get('state') in ('BUY_LIVE','BUY_PARTIAL','PLACING') for c in active),
        'tps': sum(c.get('state') in ('TP_WAIT','TP_PARTIAL') for c in active),
        'grid_gain': closed, 'grid_pct': stats_pct(closed, buys),
        'open_gain': open_net, 'open_pct': stats_pct(open_net, open_buys),
        'floating_pnl': summation('floating_pnl'),
        'realized_open_components': open_net - summation('floating_pnl'),
        'limit_tp_net': summation('limit_tp_net'),
        'market_tp_net': summation('market_tp_net'),
        'total_gain': total, 'total_pct': stats_pct(total, buys + open_buys),
        'total_fees': mfee + tfee, 'volume': maker + taker,
        'maker_volume': maker, 'taker_volume': taker,
        'maker_fee': mfee, 'taker_fee': tfee,
        'maker_percent': stats_pct(maker, maker + taker),
        'taker_percent': stats_pct(taker, maker + taker),
        'maker_fee_pct': stats_pct(-mfee, maker),
        'taker_fee_pct': stats_pct(-tfee, taker),
        'open_contracts': open_qty, 'open_base': open_qty * cv,
        'account_closed': state.get('completed', 0),
        'expected_orders': count('expected_orders'),
    }
