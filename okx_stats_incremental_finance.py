#!/usr/bin/env python3
"""Offline ONLY v0.7.51 candidate: cached connected-cycle finance calculation.

Not imported by LIVE engine. No I/O, REST, network or state writes. This is a
SHADOW candidate, never an alternative financial authority without FULL parity.
Uses the unchanged FULL calculator for each affected group of cycles, including
shared aggregate-exit orders. Group cache is private, volatile, fail-closed.
"""
import hashlib
import json
from decimal import Decimal

class IncompleteSnapshot(ValueError): pass
class CacheParityError(ValueError): pass


def D(x):
    return Decimal(str(x or '0'))


def digest(x):
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str,
                     separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


class IncrementalFinance:
    def __init__(self, oracle):
        self.oracle = oracle
        self.cache = {}
        self.last = {}

    @staticmethod
    def groups(cycles):
        """All cycles sharing an exchange order are one indivisible component."""
        parent = list(range(len(cycles)))
        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def union(a, b):
            ra, rb = root(a), root(b)
            if ra != rb: parent[rb] = ra
        order_owner = {}
        for i, c in enumerate(cycles):
            ids = [str(c.get('ordId') or '')]
            ids.extend(str(x.get('ordId') or '') for x in c.get('tpOrders', []))
            if c.get('tpOrdId'): ids.append(str(c['tpOrdId']))
            for oid in ids:
                if not oid: continue
                if oid in order_owner: union(i, order_owner[oid])
                else: order_owner[oid] = i
        found = {}
        for i in range(len(cycles)):
            found.setdefault(root(i), []).append(i)
        return list(found.values())

    def calculate(self, state, fills, bid, cv, *, snapshot_archive):
        """Returns candidate stats; caller MUST compare with FULL before publishing."""
        o = self.oracle
        cycles = o['all_stat_cycles'](state, snapshot_archive=snapshot_archive)
        expected = o['expected_filled_orders'](state, snapshot_archive=snapshot_archive)
        # Input must already have passed the existing pinned fill verifier.
        pinned = o['trim_verified_fills_for_snapshot'](fills, expected)
        if pinned is None or len(pinned) != len(fills):
            raise IncompleteSnapshot('Snapshot has missing or post-capture fills: FULL path required')
        # Keep fill identity stable and detect duplicates that could hide fees.
        by_order = {}
        identity = set()
        for index, f in enumerate(fills):
            oid, bill = str(f.get('ordId') or ''), str(f.get('billId') or '')
            if not oid or not bill or (oid, bill) in identity or oid not in expected:
                raise IncompleteSnapshot('Unexpected or duplicated fill identity')
            identity.add((oid, bill))
            by_order.setdefault(oid, []).append((index, f))
        components = self.groups(cycles)
        next_cache = {}
        parts = []
        hits = misses = 0
        for indexes in components:
            chunk = [cycles[i] for i in indexes]
            oid_set = set()
            for c in chunk:
                if c.get('ordId'): oid_set.add(str(c['ordId']))
                if c.get('tpOrdId'): oid_set.add(str(c['tpOrdId']))
                oid_set.update(str(leg.get('ordId')) for leg in c.get('tpOrders', []) if leg.get('ordId'))
            # Preserve original FILL ordering; no synthetic splitting/fees.
            chunk_fills = [f for _, f in sorted((pair for oid in oid_set
                            for pair in by_order.get(oid, [])), key=lambda pair: pair[0])]
            # OPEN cycles depend on fresh BID even without changed fills.
            dynamic = any(c.get('state') not in ('CLOSED', 'EXITED') for c in chunk)
            key = digest([chunk, chunk_fills, str(cv), str(bid) if dynamic else None])
            if key in self.cache:
                value = self.cache[key]
                hits += 1
            else:
                substate = {'cycles': {str(i): c for i, c in enumerate(chunk)},
                            'archived_cycles': [], 'completed': 0}
                stat = o['full_stats_snapshot'](substate, chunk_fills, bid, cv,
                                                include_durable_archive=False)
                if stat.get('complete') is not True:
                    raise IncompleteSnapshot('Component FULL calculation incomplete')
                closed_nominal = open_nominal = Decimal(0)
                for c in chunk:
                    buy_id = str(c.get('ordId') or '')
                    if D(c.get('filledBuy')) <= 0: continue
                    notional = sum((D(f['fillPx'])*D(f['fillSz'])*cv
                        for _, f in by_order.get(buy_id, [])), Decimal(0))
                    if c.get('state') == 'CLOSED': closed_nominal += notional
                    elif c.get('state') != 'EXITED': open_nominal += notional
                value = (stat, closed_nominal, open_nominal)
                misses += 1
            next_cache[key] = value
            parts.append(value)
        # Cache never accumulates stale checkpoints indefinitely.
        self.cache = next_cache
        dsum = lambda k: sum((x[0][k] for x in parts), Decimal(0))
        isum = lambda k: sum((int(x[0][k]) for x in parts), 0)
        buy_closed = sum((x[1] for x in parts), Decimal(0))
        buy_open = sum((x[2] for x in parts), Decimal(0))
        grid = dsum('grid_gain')
        open_gain = dsum('open_gain')
        exit_gain = dsum('partial_exit_net')
        floating = dsum('floating_pnl')
        limit_tp = dsum('limit_tp_net')
        market_tp = dsum('market_tp_net')
        maker_volume = dsum('maker_volume')
        taker_volume = dsum('taker_volume')
        maker_fee = dsum('maker_fee')
        taker_fee = dsum('taker_fee')
        volume = maker_volume + taker_volume
        pct = o['stats_pct']
        active = list(state.get('cycles', {}).values())
        open_contracts = sum((D(c.get('filledBuy'))-D(c.get('filledTP')) for c in active), Decimal(0))
        out = dict(complete=True, bid=bid, closed_count=isum('closed_count'),
                   partial_exit_count=isum('partial_exit_count'), partial_exit_net=exit_gain,
                   active_buys=sum(c.get('state') in ('BUY_LIVE','BUY_PARTIAL','PLACING') for c in active),
                   tps=sum(c.get('state') in ('TP_WAIT','TP_PARTIAL') for c in active),
                   grid_gain=grid, grid_pct=pct(grid, buy_closed),
                   open_gain=open_gain, open_pct=pct(open_gain, buy_open),
                   floating_pnl=floating, realized_open_components=open_gain-floating,
                   limit_tp_net=limit_tp, market_tp_net=market_tp,
                   total_gain=grid+open_gain+exit_gain,
                   total_pct=pct(grid+open_gain+exit_gain, buy_closed+buy_open),
                   total_fees=maker_fee+taker_fee, volume=volume,
                   maker_volume=maker_volume, taker_volume=taker_volume,
                   maker_fee=maker_fee, taker_fee=taker_fee,
                   maker_percent=pct(maker_volume, volume),
                   taker_percent=pct(taker_volume, volume),
                   maker_fee_pct=pct(-maker_fee, maker_volume),
                   taker_fee_pct=pct(-taker_fee, taker_volume),
                   open_contracts=open_contracts, open_base=open_contracts*cv,
                   account_closed=state.get('completed', 0),
                   expected_orders=len(expected))
        self.last = {'groups': len(components), 'cache_hits': hits,
                     'recalculated_groups': misses, 'cycles': len(cycles)}
        return out
