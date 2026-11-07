"""Production candidate tests: ALL sources are synthetic temporary SQLite files."""
import ast
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

HERE=Path(__file__).resolve()
SRC=HERE.parents[2]/'stats_v2_release_work'
sys.path[:0]=[str(SRC),str(SRC/'tests'),str(HERE.parents[1])]
from test_stats_v2_phase3 import Phase3TransactionIntegration, INST
from test_stats_v2_money import O, NOW, closed, fill, run_full
from okx_stats_v2_contract import Drift, NotReady
from okx_stats_v2_production import SingleAuthorityStats

# Isolated AST financial extraction of the same pinned production engine
raw=(SRC/'reference/okx_bot.py').read_bytes()
nodes=[n for n in ast.parse(raw).body if isinstance(n,ast.FunctionDef) and n.name=='expected_filled_orders']
assert len(nodes)==1
scope=dict(O)
exec(compile(ast.Module(body=nodes,type_ignores=[]),'PINNED_EXPECTED_AST','exec'),scope)
expected=scope['expected_filled_orders']

class DummyFills:
    def __init__(self):self.calls=[]
    def ensure_expected(self, exp):self.calls.append(dict(exp))

class ProductionAdapterTests(Phase3TransactionIntegration):
    def prod(self, **kwargs):
        fs=kwargs.pop('fill_store', DummyFills())
        c=SingleAuthorityStats(self.root,INST,self.s.path,oracle=O,
                 fill_store=fs,expected_filled_orders=expected,
                 contract_value='1',freeze_budget=64,**kwargs)
        self.addCleanup(c.close)
        # Stager setUp opened a separate connection; close before starting
        # production writer to avoid simultaneous writers in the same fixture.
        self.s.close()
        return c

    def test_initial_closed_money_and_report_fields(self):
        c=self.add_simple()
        prod=self.prod()
        r=prod.compute('110',now=NOW/1000)
        self.assertEqual(r.frozen_new,1)
        self.assertEqual(r.pending,0)
        self.assertEqual(r.hot_cycles,0)
        self.assertEqual(r.last_24h,Decimal('0.98'))
        self.assertEqual(r.stats,run_full([c],[fill('ba','babill',1,'buy',100),
                              fill('sa','sabill',1,'sell',101)]))
        again=prod.compute('110',now=NOW/1000)
        self.assertEqual(again.stats,r.stats)
        self.assertEqual(again.source_rows,{'cycles':0,'accounted':0,'fills':0})

    def test_hot_closed_state_waits_until_archived(self):
        c=self.add_simple()
        self.write_state({'0.001':c})
        prod=self.prod()
        hot=prod.compute('110',now=NOW/1000)
        self.assertEqual(hot.pending,1)
        self.assertEqual(hot.frozen_new,0)
        self.assertEqual(hot.stats['grid_gain'],Decimal('0.98'))
        self.write_state({})
        frozen=prod.compute('110',now=NOW/1000)
        self.assertEqual(frozen.pending,0)
        self.assertEqual(frozen.frozen_new,1)
        self.assertEqual(frozen.stats['grid_gain'],hot.stats['grid_gain'])

    def test_frozen_then_hot_open_mark_to_market(self):
        c=self.add_simple()
        prod=self.prod()
        prod.compute('110',now=NOW/1000)
        open_cycle={'cycleId':'hot','state':'TP_WAIT','ordId':'hb',
               'filledBuy':'1','filledTP':'0','tpOrders':[]}
        self.write_state({'0.009':open_cycle})
        self.add_fill(fill('hb','hbb',1,'buy',98))
        r=prod.compute('110',now=NOW/1000)
        self.assertEqual(r.hot_cycles,1)
        self.assertEqual(r.stats['open_gain'],Decimal('11.99'))
        self.assertEqual(r.stats['closed_count'],1)
        self.assertEqual(r.stats['open_contracts'],Decimal('1'))

    def test_missing_hot_fill_recovery_never_publishes_fake_money(self):
        fs=DummyFills()
        open_cycle={'cycleId':'open','state':'TP_WAIT','ordId':'hb',
               'filledBuy':'1','filledTP':'0','tpOrders':[]}
        self.write_state({'0.01':open_cycle})
        prod=self.prod(fill_store=fs)
        with self.assertRaisesRegex(NotReady,'HOT fills not complete'):
            prod.compute('110',now=NOW/1000)
        self.assertEqual(fs.calls, [{'hb':Decimal('1')}])
        self.add_fill(fill('hb','hb1',1,'buy',100))
        r=prod.compute('110',now=NOW/1000)
        self.assertTrue(r.stats['complete'])
        self.assertEqual(r.stats['open_contracts'],Decimal('1'))

    def test_hot_terminal_missing_ledger_refused(self):
        c=closed('x','bx','sx')
        self.write_state({'0.01':c})
        self.add_fill(fill('bx','xb',1,'buy',100))
        self.add_fill(fill('sx','xs',1,'sell',101))
        prod=self.prod()
        with self.assertRaisesRegex(NotReady,'PnL ledger'):
            prod.compute('110',now=NOW/1000)
        self.add_ledger('bx:sx','CLOSED','0.98')
        r=prod.compute('110',now=NOW/1000)
        self.assertEqual(r.stats['grid_gain'],Decimal('0.98'))

    def test_hot_ledger_mismatch_fails_closed(self):
        c=closed('x','bx','sx')
        self.write_state({'0.01':c})
        self.add_fill(fill('bx','xb',1,'buy',100))
        self.add_fill(fill('sx','xs',1,'sell',101))
        self.add_ledger('bx:sx','CLOSED','12.00')
        prod=self.prod()
        with self.assertRaisesRegex(Drift,'ledger'):
            prod.compute('110',now=NOW/1000)

    def test_frozen_late_fill_trips_drift(self):
        self.add_simple()
        prod=self.prod()
        prod.compute('110',now=NOW/1000)
        self.add_fill(fill('sa','additional', '0.01','sell',101))
        with self.assertRaisesRegex(Drift,'LATE_FILL_FROZEN_ORDER'):
            prod.compute('110',now=NOW/1000)

    def test_bound_is_not_silent_success(self):
        for i in range(3):
            self.write_state({str(i):{'cycleId':f'c{i}','state':'BUY_LIVE',
                 'ordId':f'b{i}','filledBuy':'0','filledTP':'0','tpOrders':[]}
                 for i in range(3)})
        prod=self.prod(hot_cycle_cap=2)
        with self.assertRaisesRegex(NotReady,'bound'):
            prod.compute('110',now=NOW/1000)

if __name__=='__main__':
    import unittest
    unittest.main()
