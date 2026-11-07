"""Phase-3 integration checks against ORIGINAL pinned financial oracle.

The fixture uses isolated, temporary state and SQLite databases. It NEVER
imports the executable engine or uses OKX/network/trading services.
"""
import json
import sqlite3
from contextlib import closing, contextmanager

@contextmanager
def write_db(path):
    # sqlite3.Connection.__exit__ commits; it does NOT close a connection.
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_stats_v2_contract import Drift, NotReady
from okx_stats_v2_phase3 import DurableStager
from test_stats_v2_money import O, closed, fill, run_full, NOW
from okx_stats_v2_money import FrozenTotals, compose_with_hot

INST = 'BLUR-USDT-SWAP'


class Phase3TransactionIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'bot'
        d = self.root / 'state'
        d.mkdir(parents=True)
        self.write_state({})
        for filename, ddl in [
            ('cycles.sqlite', 'CREATE TABLE cycles(id TEXT PRIMARY KEY, data TEXT NOT NULL)'),
            ('pnl.sqlite', 'CREATE TABLE accounted(id TEXT PRIMARY KEY, kind TEXT NOT NULL, net TEXT NOT NULL, data TEXT NOT NULL)'),
            ('fills.sqlite', 'CREATE TABLE fills(order_id TEXT NOT NULL, bill_id TEXT NOT NULL, qty TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(order_id,bill_id))'),
        ]:
            with write_db(d / filename) as con:
                con.execute(ddl)
        self.s = DurableStager(Path(self.temp.name) / 'sidecar' / 'stats-v2.sqlite', INST)
        self.addCleanup(self.s.close)

    def write_state(self, cycles, archived=None):
        (self.root/'state'/'bot_state.json').write_text(json.dumps({
            'instrument': INST, 'cycles': cycles, 'archived_cycles': archived or []
        }))

    def add_cycle(self, c):
        with write_db(self.root/'state'/'cycles.sqlite') as db:
            db.execute('INSERT INTO cycles VALUES (?,?)',
                       (c['cycleId'], json.dumps(c)))

    def add_fill(self, f):
        with write_db(self.root/'state'/'fills.sqlite') as db:
            db.execute('INSERT INTO fills VALUES (?,?,?,?)',
                       (f['ordId'], f['billId'], f['fillSz'], json.dumps(f)))

    def add_ledger(self, key, kind, net):
        entry = {'kind':kind,'net':str(net)}
        with write_db(self.root/'state'/'pnl.sqlite') as db:
            db.execute('INSERT INTO accounted VALUES (?,?,?,?)',
                       (key, kind, str(net), json.dumps(entry)))

    def add_simple(self, cid='a', *, buy='ba', sell='sa', buy_px='100', sell_px='101'):
        c = closed(cid, buy, sell)
        self.add_cycle(c)
        self.add_fill(fill(buy, buy+'bill', 1, 'buy', buy_px))
        self.add_fill(fill(sell, sell+'bill', 1, 'sell', sell_px))
        net = Decimal(sell_px) - Decimal(buy_px) - Decimal('0.02')
        self.add_ledger(buy+':'+sell, 'CLOSED', net)
        return c

    def freeze(self, cid='a', **more):
        return self.s.freeze_one(self.root, cid,
                                 full_oracle=O['full_stats_snapshot'],
                                 allocate=O['allocated_market_fills'],
                                 contract_value=Decimal('1'),bid=Decimal('110'),
                                 now_ms=NOW, **more)

    def test_exact_money_and_restart(self):
        c = self.add_simple()
        self.assertEqual(self.s.ingest(self.root)['new'],
                         {'cycles':1, 'accounted':1, 'fills':2})
        got = self.freeze()
        self.assertEqual(got['cycles'], 1)
        self.assertEqual(got['grid_gain'], '0.98')
        self.assertEqual(self.s.pending_count(), 0)
        self.assertEqual(self.s.frozen_count(), 1)
        self.assertEqual(self.s.last_24h(NOW), Decimal('0.98'))
        self.assertEqual(self.s.totals_snapshot()['last_25h_events'], [])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM recent_events').fetchone()[0], 1)
        snap = self.s.totals_snapshot()
        restored = self.s._restore_totals(snap)
        hot = run_full([], [])
        composed = compose_with_hot(restored, hot, '0', '0',
                                    {'cycles':{}, 'completed':0}, Decimal('110'),
                                    Decimal('1'),stats_pct=O['stats_pct'])
        self.assertEqual(composed, run_full([c], [fill('ba','babill',1,'buy',100),
                                                  fill('sa','sabill',1,'sell',101)]))
        self.assertEqual(self.s.ingest(self.root)['new'],
                         {'cycles':0, 'accounted':0, 'fills':0})
        other = DurableStager(self.s.path, INST)
        self.addCleanup(other.close)
        self.assertEqual(other.export_summary()['totals'], snap)
        self.assertEqual(other.pending_count(), 0)
        self.assertEqual(other.frozen_count(), 1)
        with self.assertRaises((Drift, NotReady)):
            self.freeze()

    def test_atomic_ingest_crash_cursors_and_pending_rollback(self):
        self.add_simple()
        with self.assertRaisesRegex(RuntimeError,'Simulated crash'):
            self.s.ingest(self.root, inject_crash_after_cycles=True)
        self.assertEqual(self.s.pending_count(), 0)
        self.assertIsNone(self.s._cursor('cycles'))
        self.assertEqual(self.s.ingest(self.root)['new']['cycles'],1)
        self.freeze()

    def test_freeze_crash_rolls_back_money_and_membership(self):
        self.add_simple()
        self.s.ingest(self.root)
        before = self.s.totals_snapshot()
        with self.assertRaisesRegex(RuntimeError,'Simulated crash'):
            self.freeze(inject_crash_before_commit=True)
        self.assertEqual(self.s.pending_count(),1)
        self.assertEqual(self.s.frozen_count(),0)
        self.assertEqual(self.s.totals_snapshot(), before)
        self.freeze()
        self.assertEqual(self.s.frozen_count(),1)

    def test_price_keyed_state_overrides_archive_until_removed(self):
        c = self.add_simple()
        self.write_state({'0.009125':c})  # LIVE-like keys are grid PRICES, not cycle IDs
        self.s.ingest(self.root)
        with self.assertRaisesRegex(NotReady,'still present in state'):
            self.freeze()
        self.write_state({})
        self.s.ingest(self.root)
        self.freeze()
        self.assertEqual(self.s.pending_count(),0)

    def test_missing_archive_after_state_disappears_rejected(self):
        c = closed('x','b','s')
        self.write_state({'0.01':c})
        self.s.ingest(self.root)
        self.write_state({})
        with self.assertRaisesRegex(Drift,'disappeared'):
            self.s.ingest(self.root)
        self.assertEqual(self.s.pending_count(),0)

    def test_missing_late_fill_deferred_and_then_frozen(self):
        c = closed('a','ba','sa')
        self.add_cycle(c)
        self.add_fill(fill('ba','b1',1,'buy',100))
        self.add_ledger('ba:sa','CLOSED','0.98')
        self.s.ingest(self.root)
        with self.assertRaisesRegex(NotReady,'fills_incomplete'):
            self.freeze()
        self.assertEqual(self.s.pending_count(),1)
        self.add_fill(fill('sa','s1',1,'sell',101))
        self.s.ingest(self.root)
        self.freeze()
        self.assertEqual(self.s.pending_count(),0)
        self.add_fill(fill('sa','s2','0.01','sell',101))
        with self.assertRaisesRegex(Drift,'LATE_FILL_FROZEN_ORDER'):
            self.s.ingest(self.root)

    def test_shared_market_component_waits_for_full_archive(self):
        plan = [{'cycleId':'a','sz':'1'},{'cycleId':'b','sz':'1'}]
        a,b = closed('a','ba','market'),closed('b','bb','market')
        for c in (a,b):
            c['tpOrders']=[{'ordId':'market','filled':'1','source':'aggregate_exit',
                            'aggregatePlan':plan}]
        self.add_cycle(a)
        self.add_fill(fill('ba','ba1',1,'buy',100))
        self.add_fill(fill('bb','bb1',1,'buy',100))
        self.add_fill(fill('market','m1',1,'sell',101,t=NOW-2500))
        self.add_fill(fill('market','m2',1,'sell',102,t=NOW-1000))
        self.add_ledger('ba:market','CLOSED','0.98')
        self.add_ledger('bb:market','CLOSED','1.98')
        self.s.ingest(self.root)
        with self.assertRaisesRegex(NotReady,'not fully archived'):
            self.freeze('a')
        self.add_cycle(b)
        self.s.ingest(self.root)
        out = self.freeze('a')
        self.assertEqual(out['cycles'],2)
        self.assertEqual(out['orders'],3)
        self.assertEqual(out['grid_gain'],'2.96')
        self.assertEqual(self.s.last_24h(NOW),Decimal('2.96'))
        self.assertEqual(self.s.pending_count(),0)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM frozen_orders').fetchone()[0],3)

    def test_unchanged_window_only_scans_new_rows(self):
        self.add_simple()
        self.s.ingest(self.root)
        self.freeze()
        for _ in range(100):
            report=self.s.ingest(self.root)
            self.assertEqual(report['new'], {'cycles':0,'accounted':0,'fills':0})
        self.assertEqual(self.s.frozen_count(),1)

    def test_nonterminal_component_cannot_freeze(self):
        c = {'cycleId':'hot','state':'TP_WAIT','ordId':'buy','filledBuy':'1',
             'filledTP':'0', 'tpOrders': []}
        self.add_cycle(c)
        self.s.ingest(self.root)
        with self.assertRaises(NotReady):
            self.freeze('hot')

    def test_no_engine_files_modified(self):
        ref = Path(__file__).resolve().parents[1]/'reference/okx_bot.py'
        import hashlib
        before=hashlib.sha256(ref.read_bytes()).hexdigest()
        self.add_simple()
        self.s.ingest(self.root)
        self.freeze()
        self.assertEqual(hashlib.sha256(ref.read_bytes()).hexdigest(),before)


if __name__ == '__main__':
    unittest.main()
