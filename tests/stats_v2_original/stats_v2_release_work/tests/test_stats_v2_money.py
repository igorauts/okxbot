"""Synthetic differential checks against AST-extracted ORIGINAL_FULL_LOW_RAM oracle.

Exact pinned production code is compiled as functions ONLY; the trading module
is never imported or executed. OFFLINE only, no network, API, or LIVE state.
"""
import ast
from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from okx_stats_v2_contract import Drift
from okx_stats_v2_money import FrozenTotals, verified_frozen_chunk, compose_with_hot, DAY_MS, MONEY_FIELDS, COUNT_FIELDS

PINNED_SHA = '9c6cf1b46ba8f0b045a80ea97b3178657fd296c2e6d564fb8579839e66057cbd'
NOW = 1800000000000


def oracle():
    import hashlib
    path = ROOT / 'reference/okx_bot.py'
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINNED_SHA, 'Oracle source is NOT pinned v0.7.60'
    need = {'all_stat_cycles', 'accumulate_expected_sell', 'allocated_market_fills',
            'stats_pct', 'full_stats_snapshot', '_last_24h_profit'}
    nodes = [n for n in ast.parse(raw).body
             if isinstance(n, ast.FunctionDef) and n.name in need]
    assert len(nodes) == len(need)
    env = {'Decimal': Decimal, 'D': lambda v: Decimal(str(v or '0'))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'PINNED_ORACLE', 'exec'), env)
    return env

O = oracle()


def fill(oid, bill, qty, side, px, *, t=NOW-1000, fee='-0.01', maker=True):
    return {'ordId':oid, 'billId':bill, 'tradeId':bill, 'fillSz':str(qty),
            'fillPx':str(px), 'side':side, 'fee':str(fee),'feeCcy':'USDT',
            'execType':'M' if maker else 'T', 'ts':str(t)}


def closed(cid, buy, sell, *, state='CLOSED'):
    return {'cycleId':cid, 'ordId':buy, 'tpOrdId':sell, 'filledBuy':'1',
            'filledTP':'1', 'state':state,
            'tpOrders':[{'ordId':sell,'filled':'1'}]}


def run_full(cycles, fills, *, hot=None, bid=Decimal('110')):
    state = {'cycles':hot or {}, 'archived_cycles':cycles, 'completed':0}
    return O['full_stats_snapshot'](state, fills, bid, Decimal(1),
                                    include_durable_archive=False, snapshot_archive=[])


def freeze(cycles, fills, ledger):
    return verified_frozen_chunk(cycles, fills, ledger, Decimal('1'),
                                 full_oracle=O['full_stats_snapshot'],
                                 allocate=O['allocated_market_fills'])


class MonetaryDifferential(unittest.TestCase):
    def test_closed_chunk_equals_original_oracle(self):
        c = closed('a','ba','sa')
        rows = [fill('ba','b1',1,'buy',100), fill('sa','s1',1,'sell',101)]
        d = freeze([c], rows, {'ba:sa':{'kind':'CLOSED','net':'0.98'}})
        totals = FrozenTotals(now_ms=NOW)
        totals.add(d)
        hot = run_full([], [])
        composed = compose_with_hot(totals, hot, '0', '0',
                                    {'cycles':{}, 'completed':0}, Decimal('110'), Decimal('1'),
                                    stats_pct=O['stats_pct'])
        self.assertEqual(composed, run_full([c], rows))
        self.assertEqual(totals.last_24h(NOW), Decimal('0.98'))
        self.assertEqual(totals.closed_buy_nominal, Decimal('100'))
        self.assertEqual(totals.ledger_total, Decimal('0.98'))

    def test_exit_and_empty_canceled(self):
        e = closed('e','be','se',state='EXITED')
        canceled = {'cycleId':'z', 'state':'CANCELED', 'filledBuy':'0', 'filledTP':'0'}
        rows = [fill('be','eb',1,'buy',100), fill('se','es',1,'sell',99)]
        d = freeze([e,canceled], rows, {'be:se':{'kind':'EXITED','net':'-1.02'}})
        self.assertEqual(d.monetary['partial_exit_net'],'-1.02')
        self.assertEqual(d.monetary['grid_gain'],'0')
        self.assertEqual(len(d.close_events),0)
        self.assertEqual(d.counts['partial_exit_count'],1)

    def test_market_two_cycles_exact_attribution(self):
        plan = [{'cycleId':'a','sz':'1'},{'cycleId':'b','sz':'1'}]
        a,b = closed('a','ba','market'), closed('b','bb','market')
        for c in (a,b):
            c['tpOrders']=[{'ordId':'market','filled':'1','source':'aggregate_exit',
                            'aggregatePlan':plan}]
        rows = [fill('ba','b1',1,'buy',100),fill('bb','b2',1,'buy',100),
                fill('market','m1',1,'sell',101,t=NOW-2500),
                fill('market','m2',1,'sell',102,t=NOW-1000)]
        led = {'ba:market':{'kind':'CLOSED','net':'0.98'},
               'bb:market':{'kind':'CLOSED','net':'1.98'}}
        d = freeze([a,b], rows, led)
        self.assertEqual(d.monetary['grid_gain'],'2.96')
        self.assertEqual(d.monetary['market_tp_net'],'2.96')
        self.assertEqual(Decimal(d.monetary['maker_fee']) + Decimal(d.monetary['taker_fee']), Decimal('-0.04'))
        self.assertEqual(d.counts['expected_orders'],3)
        self.assertEqual(d.orders,('ba','bb','market'))
        t = FrozenTotals(now_ms=NOW)
        t.add(d)
        self.assertEqual(t.last_24h(NOW),Decimal('2.96'))
        oracle24 = O['_last_24h_profit']({'cycles':{},'archived_cycles':[a,b]},
                                         rows,Decimal('1'),NOW,snapshot_archive=[])
        self.assertEqual(t.last_24h(NOW),oracle24)
        self.assertEqual(t.days[__import__('datetime').datetime.fromtimestamp(
            (NOW-1000)/1000,__import__('datetime').timezone.utc).date().isoformat()],Decimal('2.96'))

    def test_two_independent_frozen_components_match_whole_full(self):
        a, b = closed('a', 'ba', 'sa'), closed('b', 'bb', 'sb')
        fa = [fill('ba','ba1',1,'buy',100, maker=False),
              fill('sa','sa1',1,'sell',101, maker=False)]
        fb = [fill('bb','bb1',1,'buy',95),
              fill('sb','sb1',1,'sell',97)]
        totals = FrozenTotals(now_ms=NOW)
        totals.add(freeze([a], fa, {'ba:sa':{'kind':'CLOSED','net':'0.98'}}))
        totals.add(freeze([b], fb, {'bb:sb':{'kind':'CLOSED','net':'1.98'}}))
        empty = run_full([], [])
        got = compose_with_hot(totals, empty, '0', '0', {'cycles':{}, 'completed':0},
                               Decimal('110'), Decimal(1), stats_pct=O['stats_pct'])
        want = run_full([a,b],fa+fb)
        self.assertEqual(got,want)
        self.assertEqual(totals.last_24h(NOW),Decimal('2.96'))

    def test_frozen_plus_hot_equals_full(self):
        c = closed('a','ba','sa')
        o = {'cycleId':'o', 'state':'TP_WAIT','ordId':'bo', 'filledBuy':'1',
             'filledTP':'0', 'tpOrders':[]}
        closed_rows = [fill('ba','b1',1,'buy',100), fill('sa','s1',1,'sell',101)]
        hot_rows = [fill('bo','ob',1,'buy',98)]
        t = FrozenTotals(now_ms=NOW)
        t.add(freeze([c],closed_rows,{'ba:sa':{'kind':'CLOSED','net':'0.98'}}))
        hot = run_full([], hot_rows, hot={'o':o})
        full = run_full([c],closed_rows+hot_rows,hot={'o':o})
        combined = compose_with_hot(t,hot,'0','98',{'cycles':{'o':o},'completed':0},
                                    Decimal('110'), Decimal('1'), stats_pct=O['stats_pct'])
        self.assertEqual(combined,full)

    def test_late_fill_incomplete_denied(self):
        c=closed('a','ba','sa')
        with self.assertRaisesRegex(Drift,'fills_incomplete'):
            freeze([c],[fill('ba','b1',1,'buy',100)],{'ba:sa':{'kind':'CLOSED','net':'0.98'}})

    def test_multi_leg_chain_identity_and_net(self):
        cycle = {'cycleId':'chain', 'ordId':'bc', 'state':'CLOSED',
                 'filledBuy':'2', 'filledTP':'2',
                 'tpOrders':[{'ordId':'sc1','filled':'1'},
                             {'ordId':'sc2','filled':'1'}]}
        rows = [fill('bc','b1',2,'buy',100,fee='-0.02'),
                fill('sc1','s1',1,'sell',101),
                fill('sc2','s2',1,'sell',102)]
        chunk = freeze([cycle], rows,
                       {'chain:bc':{'kind':'CLOSED','net':'2.96'}})
        self.assertEqual(chunk.monetary['grid_gain'],'2.96')
        self.assertEqual(Decimal(chunk.monetary['limit_tp_net']),Decimal('2.96'))
        self.assertEqual(chunk.counts['expected_orders'],3)

    def test_nonempty_canceled_denied(self):
        c = {'cycleId':'bad','state':'CANCELED','filledBuy':'0','filledTP':'1'}
        with self.assertRaisesRegex(Drift, 'Nonempty CANCELED'):
            freeze([c], [], {})

    def test_journal_mismatch_denied(self):
        c=closed('a','ba','sa')
        rows=[fill('ba','b1',1,'buy',100),fill('sa','s1',1,'sell',101)]
        with self.assertRaisesRegex(Drift, 'PnL journal'):
            freeze([c],rows,{'ba:sa':{'kind':'CLOSED','net':'0.01'}})

    def test_exact_window_expires_and_day_buckets_persist(self):
        t=FrozenTotals(now_ms=NOW)
        c=closed('a','ba','sa')
        rows=[fill('ba','b1',1,'buy',100,t=NOW-100000),
              fill('sa','s1',1,'sell',101,t=NOW-1000)]
        t.add(freeze([c],rows,{'ba:sa':{'kind':'CLOSED','net':'0.98'}}))
        self.assertEqual(t.last_24h(NOW),Decimal('0.98'))
        t.roll_forward(NOW+DAY_MS+1000)
        self.assertEqual(t.last_24h(NOW+DAY_MS+1000),Decimal(0))
        self.assertEqual(sum(t.days.values()),Decimal('0.98'))
        t.roll_forward(NOW+DAY_MS+2*3600*1000)
        self.assertFalse(t.recent)

    def test_no_historical_order_fill_sets_retained(self):
        c=closed('a','ba','sa')
        rows=[fill('ba','b1',1,'buy',100),fill('sa','s1',1,'sell',101)]
        t=FrozenTotals(now_ms=NOW)
        chunk=freeze([c],rows,{'ba:sa':{'kind':'CLOSED','net':'0.98'}})
        for _ in range(1000):
            t.add(chunk)  # ONLY tests aggregate memory, NOT idempotent input contract
        snapshot=t.safe_snapshot()
        self.assertNotIn('orders', snapshot)
        self.assertNotIn('cycleIds', snapshot)
        self.assertEqual(len(t.days),1)
        self.assertEqual(len(t.recent),1000)  # 25h bound, not all history bound

    def test_failure_does_not_partially_add(self):
        from okx_stats_v2_money import FrozenChunk
        t=FrozenTotals(now_ms=NOW)
        bad=FrozenChunk({k:'0' for k in MONEY_FIELDS},
                        {k:0 for k in COUNT_FIELDS},'0',
                        ((NOW+7200000,'1'),),(), '0')
        with self.assertRaisesRegex(Drift,'timestamp'):
            t.add(bad)
        self.assertEqual(t.safe_snapshot()['money']['grid_gain'],'0')


if __name__ == '__main__': unittest.main()
