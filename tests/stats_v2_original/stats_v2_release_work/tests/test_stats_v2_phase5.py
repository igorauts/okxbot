"""Phase 5 OFFLINE-only performance refactor safety regressions."""
import json
from pathlib import Path
import sqlite3
from contextlib import closing

import unittest
import test_stats_v2_phase3 as p3
INST = p3.INST
from test_stats_v2_money import O, closed, fill, NOW
from decimal import Decimal
from okx_stats_v2_contract import Drift, NotReady
from okx_stats_v2_phase3 import DurableStager


class Phase5AtomicBatchTests(unittest.TestCase):
    setUp = p3.Phase3TransactionIntegration.setUp
    write_state = p3.Phase3TransactionIntegration.write_state
    add_cycle = p3.Phase3TransactionIntegration.add_cycle
    add_fill = p3.Phase3TransactionIntegration.add_fill
    add_ledger = p3.Phase3TransactionIntegration.add_ledger
    add_simple = p3.Phase3TransactionIntegration.add_simple
    freeze = p3.Phase3TransactionIntegration.freeze
    def test_two_components_persist_in_single_atomic_batch(self):
        self.add_simple('a',buy='ba',sell='sa')
        self.add_simple('b',buy='bb',sell='sb')
        self.s.ingest(self.root)
        with self.s.finance_snapshots(self.root) as views, self.s.atomic_freeze_batch():
            self.freeze('a',pinned_views=views)
            self.freeze('b',pinned_views=views)
            self.assertEqual(self.s.frozen_count(),2)
            self.assertEqual(self.s.pending_count(),0)
        self.assertEqual(self.s.totals_snapshot()['money']['grid_gain'],'1.96')
        with DurableStager(self.s.path,INST) as reopened:
            self.assertEqual(reopened.frozen_count(),2)
            self.assertEqual(reopened.totals_snapshot(),self.s.totals_snapshot())

    def test_batch_crash_reverts_all_durable_money_and_index(self):
        self.add_simple('a',buy='ba',sell='sa')
        self.add_simple('b',buy='bb',sell='sb')
        self.s.ingest(self.root)
        before=self.s.totals_snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Injected batch crash'):
            with self.s.finance_snapshots(self.root) as views, self.s.atomic_freeze_batch():
                self.freeze('a',pinned_views=views)
                self.freeze('b',pinned_views=views)
                raise RuntimeError('Injected batch crash')
        self.assertEqual(self.s.pending_count(),2)
        self.assertEqual(self.s.frozen_count(),0)
        self.assertEqual(self.s.totals_snapshot(),before)
        with DurableStager(self.s.path,INST) as reopened:
            self.assertEqual(reopened.frozen_count(),0)
            self.assertEqual(reopened.db.execute('SELECT COUNT(*) FROM frozen_orders').fetchone()[0],0)

    def test_one_component_savepoint_failure_does_not_poison_rest(self):
        self.add_simple('a',buy='ba',sell='sa')
        self.add_simple('b',buy='bb',sell='sb')
        self.s.ingest(self.root)
        with self.s.finance_snapshots(self.root) as views, self.s.atomic_freeze_batch():
            with self.assertRaisesRegex(RuntimeError,'Simulated crash'):
                self.freeze('a',pinned_views=views,inject_crash_before_commit=True)
            self.assertEqual(self.s.pending_count(),2)
            self.assertEqual(self.s.frozen_count(),0)
            self.freeze('b',pinned_views=views)
        self.assertEqual(self.s.pending_count(),1)
        self.assertEqual(self.s.frozen_count(),1)
        self.assertEqual(self.s.totals_snapshot()['money']['grid_gain'],'0.98')

    def test_pinned_snapshot_rejects_modified_sidecar_watermark(self):
        self.add_simple()
        self.s.ingest(self.root)
        with self.s.finance_snapshots(self.root) as views:
            before=self.s._cursor('fills')
            corrupt=dict(before)
            corrupt['rowid']=0
            self.s.db.execute('UPDATE cursors SET payload=? WHERE kind=?',
                              (json.dumps(corrupt),'fills'))
            with self.assertRaisesRegex(Drift,'Stale pooled finance snapshot'):
                self.freeze(pinned_views=views)
        self.s.db.execute('UPDATE cursors SET payload=? WHERE kind=?',
                          (json.dumps(before),'fills'))
        self.assertEqual(self.s.pending_count(),1)
        self.assertEqual(self.s.frozen_count(),0)

    def test_source_bytes_unchanged_by_batch(self):
        import hashlib
        self.add_simple()
        self.s.ingest(self.root)
        files=[self.root/'state'/f for f in ('cycles.sqlite','pnl.sqlite','fills.sqlite')]
        before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
        with self.s.finance_snapshots(self.root) as views, self.s.atomic_freeze_batch():
            self.freeze('a',pinned_views=views)
        after=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
        self.assertEqual(before,after)

class Phase5RollingParityTests(unittest.TestCase):
    setUp = p3.Phase3TransactionIntegration.setUp
    write_state = p3.Phase3TransactionIntegration.write_state
    add_cycle = p3.Phase3TransactionIntegration.add_cycle
    add_fill = p3.Phase3TransactionIntegration.add_fill
    add_ledger = p3.Phase3TransactionIntegration.add_ledger
    add_simple = p3.Phase3TransactionIntegration.add_simple
    freeze = p3.Phase3TransactionIntegration.freeze

    def freeze_at(self, cid, now_ms):
        return self.s.freeze_one(self.root, cid,
                                 full_oracle=O['full_stats_snapshot'],
                                 allocate=O['allocated_market_fills'],
                                 contract_value=Decimal('1'),bid=Decimal('110'),
                                 now_ms=now_ms)

    def test_24h_exact_cutoff_and_crash_restart(self):
        self.add_simple()
        self.s.ingest(self.root)
        self.freeze()
        day=24*3600*1000
        self.assertEqual(self.s.last_24h(NOW), Decimal('0.98'))
        # SELL timestamp equals NOW-1000, and equality at cutoff is inclusive.
        self.assertEqual(self.s.last_24h(NOW+day-1000),Decimal('0.98'))
        self.assertEqual(self.s.last_24h(NOW+day-999),Decimal('0'))
        with DurableStager(self.s.path,INST) as reopened:
            self.assertEqual(reopened.last_24h(NOW+day-999),Decimal('0'))
            self.assertEqual(reopened.db.execute(
                'SELECT total FROM rolling_24h WHERE id=1').fetchone()[0],'0.00')

    def test_two_frozen_components_before_after_24h_boundary(self):
        self.add_simple('a',buy='ba',sell='sa')
        self.s.ingest(self.root)
        self.freeze('a')
        day=24*3600*1000
        self.assertEqual(self.s.last_24h(NOW+day-999),Decimal('0'))
        cycle=closed('b','bb','sb')
        self.add_cycle(cycle)
        self.add_fill(fill('bb','bb-bill',1,'buy',100,t=NOW+day))
        self.add_fill(fill('sb','sb-bill',1,'sell',101,t=NOW+day))
        self.add_ledger('bb:sb','CLOSED','0.98')
        self.s.ingest(self.root)
        self.freeze_at('b',now_ms=NOW+day+1000)
        self.assertEqual(self.s.last_24h(NOW+day+1000),Decimal('0.98'))

    def test_25h_prune_after_long_gap_preserves_rolling_total(self):
        self.add_simple('a',buy='ba',sell='sa')
        self.s.ingest(self.root)
        self.freeze('a')
        day=24*3600*1000
        c=closed('b','bb','sb')
        self.add_cycle(c)
        self.add_fill(fill('bb','bill-bb',1,'buy',100,t=NOW+3*day))
        self.add_fill(fill('sb','bill-sb',1,'sell',101,t=NOW+3*day))
        self.add_ledger('bb:sb','CLOSED','0.98')
        self.s.ingest(self.root)
        self.freeze_at('b',now_ms=NOW+3*day+1000)
        self.assertEqual(self.s.last_24h(NOW+3*day+1000),Decimal('0.98'))
        self.assertEqual(self.s.db.execute(
            'SELECT COUNT(*) FROM recent_events').fetchone()[0],1)

    def test_stable_rolling_24h_does_not_scan_all_events(self):
        self.add_simple()
        self.s.ingest(self.root)
        self.freeze()
        queries=[]
        self.s.db.set_trace_callback(queries.append)
        try:
            for _ in range(50):
                self.assertEqual(self.s.last_24h(NOW),Decimal('0.98'))
        finally:
            self.s.db.set_trace_callback(None)
        self.assertFalse(any('FROM recent_events' in q for q in queries))
        self.assertFalse(any('UPDATE rolling_24h' in q for q in queries))

    def test_rollback_includes_rolling_24h(self):
        self.add_simple()
        self.s.ingest(self.root)
        before=self.s.last_24h(NOW)
        with self.assertRaisesRegex(RuntimeError,'Simulated crash'):
            self.freeze(inject_crash_before_commit=True)
        self.assertEqual(self.s.last_24h(NOW),before)
        self.freeze()
        self.assertEqual(self.s.last_24h(NOW),Decimal('0.98'))
