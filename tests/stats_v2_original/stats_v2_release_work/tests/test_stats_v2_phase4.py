"""Offline shadow FULL parity against byte-pinned production oracle."""
import hashlib
import sys
import unittest
from pathlib import Path
from decimal import Decimal
from test_stats_v2_phase3 import Phase3TransactionIntegration, INST
from test_stats_v2_money import NOW, O, fill, closed
from okx_stats_v2_contract import Drift, NotReady
from okx_stats_v2_phase4 import OfflineShadowRunner, audit_due, load_pinned_oracle


class Phase4Tests(Phase3TransactionIntegration):
    def runner(self, **kwargs):
        return OfflineShadowRunner(self.s,O,**kwargs)

    def step(self, **kwargs):
        return self.runner().step(self.root,bid='110',contract_value='1',
                                  now_ms=NOW,compare_full=True,**kwargs)

    def test_empty_shadow_full_matches(self):
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.pending,0)
        self.assertEqual(r.hot_cycles,0)

    def test_one_closed_then_hot_position_parity(self):
        self.add_simple()
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.frozen,1)
        self.assertEqual(r.incremental['grid_gain'],Decimal('0.98'))
        opened={'cycleId':'hot','state':'TP_WAIT','ordId':'hb',
                'filledBuy':'1', 'filledTP':'0','tpOrders':[]}
        self.write_state({'0.009':opened})
        self.add_fill(fill('hb','hb1',1,'buy','100'))
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.incremental['open_gain'],Decimal('9.99'))
        self.assertEqual(r.incremental['closed_count'],1)
        self.assertEqual(r.incremental['open_contracts'],Decimal('1'))
        self.assertEqual(r.hot_cycles,1)
        self.assertEqual(self.s.pending_count(),0)

    def test_pending_late_fill_then_frozen(self):
        c=closed('late','blate','slate')
        self.add_cycle(c)
        self.add_fill(fill('blate','b1',1,'buy',100))
        self.add_ledger('blate:slate','CLOSED','0.98')
        # Cannot construct complete financial report without the missing fill.
        with self.assertRaises(NotReady):
            self.step()
        self.assertEqual(self.s.pending_count(),1)
        self.add_fill(fill('slate','s1',1,'sell',101))
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.pending,0)

    def test_state_archive_duplicate_then_transition(self):
        c=self.add_simple()
        self.write_state({'0.009':c})
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.pending,1)
        self.assertEqual(r.frozen,0)
        self.write_state({})
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.pending,0)
        self.assertEqual(r.incremental['grid_gain'],Decimal('0.98'))

    def test_two_closed_plus_shared_market(self):
        plan=[{'cycleId':'a','sz':'1'},{'cycleId':'b','sz':'1'}]
        a,b=closed('a','ba','market'), closed('b','bb','market')
        for c in (a,b):
            c['tpOrders']=[{'ordId':'market','filled':'1','source':'aggregate_exit','aggregatePlan':plan}]
        for c in (a,b): self.add_cycle(c)
        self.add_fill(fill('ba','b1',1,'buy',100))
        self.add_fill(fill('bb','b2',1,'buy',100))
        self.add_fill(fill('market','m1',1,'sell',101,t=NOW-2500))
        self.add_fill(fill('market','m2',1,'sell',102,t=NOW-1000))
        self.add_ledger('ba:market','CLOSED','0.98')
        self.add_ledger('bb:market','CLOSED','1.98')
        r=self.step()
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.frozen,1)
        self.assertEqual(r.incremental['market_tp_net'],Decimal('2.96'))
        self.assertEqual(r.last_24h_incremental, '2.96')

    def test_late_fill_on_frozen_order_rejected(self):
        self.add_simple()
        self.step()
        self.add_fill(fill('sa','after', '0.01','sell',101))
        with self.assertRaisesRegex(Drift,'LATE_FILL_FROZEN_ORDER'):
            self.step()

    def test_hot_cap_fail_closed(self):
        c=self.add_simple()
        r=self.runner(hot_cycle_cap=1).step(self.root,bid='110', contract_value='1',now_ms=NOW)
        self.assertEqual(r.frozen,1)
        self.write_state({'0.01':{'cycleId':'x','state':'BUY_LIVE','ordId':'x','filledBuy':'0','filledTP':'0'},
                          '0.02':{'cycleId':'y','state':'BUY_LIVE','ordId':'y','filledBuy':'0','filledTP':'0'}})
        with self.assertRaisesRegex(NotReady,'HOT cycle cap'):
            self.runner(hot_cycle_cap=1).step(self.root,bid='110',contract_value='1',now_ms=NOW)

    def test_stale_source_reference_rejected(self):
        oracle=load_pinned_oracle(Path(__file__).parents[1]/'reference/okx_bot.py')
        self.assertEqual(oracle['stats_pct'](Decimal(1),Decimal(100)),'1.0000%')
        other=Path(self.temp.name)/'tampered.py'
        other.write_text('def full_stats_snapshot(*x): return {}')
        with self.assertRaisesRegex(Drift,'hash mismatch'):
            load_pinned_oracle(other)

    def test_audit_stagger_deterministic(self):
        t=6*3600*1000
        self.assertTrue(audit_due(INST,NOW,None))
        self.assertFalse(audit_due(INST,NOW,NOW-1000))
        self.assertTrue(audit_due(INST,NOW+t,NOW-1000))
        self.assertNotEqual(audit_due(INST,NOW,NOW-1000), audit_due(INST,NOW+t,NOW-1000))

    def test_repeat_no_new_history_parity(self):
        self.add_simple()
        self.step()
        for i in range(30):
            r=self.step(freeze_budget=0)
            self.assertEqual(r.comparison,'PASS')
            self.assertEqual(r.ingested_rows,{'cycles':0,'accounted':0,'fills':0})


if __name__=='__main__': unittest.main()

# Regression: unarchived sibling (not in pending_cycles) may share a MARKET
# order. That component cannot be frozen as an archive-only subset.
class UnarchivedSiblingTest(Phase4Tests):
    def test_market_sibling_still_only_in_state_deferred(self):
        a=closed('a','ba','market')
        b=closed('b','bb','market')
        plan=[{'cycleId':'a','sz':'1'},{'cycleId':'b','sz':'1'}]
        for c in (a,b):
            c['tpOrders']=[{'ordId':'market','filled':'1','source':'aggregate_exit',
                            'aggregatePlan':plan}]
        self.add_cycle(a)
        self.write_state({'0.009': b})
        self.s.ingest(self.root)
        with self.assertRaises(NotReady):
            self.freeze('a')
        self.assertEqual(self.s.pending_count(),1)
        self.assertEqual(self.s.frozen_count(),0)

class FullAuditSafetyTest(Phase4Tests):
    def test_due_audit_and_durable_checkpoint(self):
        self.add_simple()
        r=self.runner().step(self.root,bid='110',contract_value='1',now_ms=NOW,audit_policy='due')
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.authoritative_source,'FULL')
        r=self.runner().step(self.root,bid='110',contract_value='1',now_ms=NOW+1000,audit_policy='due')
        self.assertEqual(r.comparison,'NOT_REQUESTED')
        self.assertEqual(r.authoritative_source,'NONE')
        r=self.runner().step(self.root,bid='110',contract_value='1',now_ms=NOW+6*3600*1000,audit_policy='due')
        self.assertEqual(r.comparison,'PASS')

    def test_mismatch_durable_event_and_full_authority(self):
        self.add_simple()
        self.step()
        # Corrupt only disposable OFFLINE sidecar aggregates. Source money
        # remains pristine. A full audit must disagree and signal mismatch.
        import json
        row=self.s.db.execute('SELECT payload FROM totals WHERE id=1').fetchone()[0]
        bad=json.loads(row)
        bad['money']['grid_gain']='999'
        self.s.db.execute('UPDATE totals SET payload=? WHERE id=1',(json.dumps(bad),))
        r=self.step(freeze_budget=0)
        self.assertEqual(r.comparison,'MISMATCH')
        self.assertIn('grid_gain',r.mismatch_fields)
        self.assertEqual(r.authoritative_source,'FULL')
        self.assertEqual(r.authoritative_full['grid_gain'],Decimal('0.98'))
        count=self.s.db.execute('SELECT COUNT(*) FROM shadow_audit_events WHERE event=?',
                                ('STATS_INCREMENTAL_MISMATCH',)).fetchone()[0]
        self.assertEqual(count,1)

class BootstrapTests(Phase4Tests):
    def test_backlog_larger_than_hot_cap_bootstraps_without_report(self):
        for i in range(12):
            self.add_simple(cid=f'c{i}',buy=f'b{i}',sell=f's{i}')
        runner=self.runner(hot_cycle_cap=2)
        b=runner.bootstrap_batch(self.root,bid='110',contract_value='1',
                                  now_ms=NOW,freeze_budget=5)
        self.assertEqual(b['frozen_new_components'],5)
        self.assertEqual(b['pending_remaining'],7)
        self.assertFalse(b['report_published'])
        b=runner.bootstrap_batch(self.root,bid='110',contract_value='1',
                                  now_ms=NOW,freeze_budget=5)
        self.assertEqual(b['pending_remaining'],2)
        b=runner.bootstrap_batch(self.root,bid='110',contract_value='1',
                                  now_ms=NOW,freeze_budget=5)
        self.assertEqual(b['pending_remaining'],0)
        r=runner.step(self.root,bid='110',contract_value='1',now_ms=NOW,
                      freeze_budget=0,compare_full=True)
        self.assertEqual(r.comparison,'PASS')
        self.assertEqual(r.incremental['closed_count'],12)

class HashChainAuditTests(Phase4Tests):
    def test_hidden_middle_row_mutation_caught_only_at_full(self):
        import sqlite3
        import json
        from contextlib import closing
        from pathlib import Path
        from okx_stats_v2_phase4 import verify_full_prefix_chains
        self.add_simple(cid='a',buy='ba',sell='sa')
        self.add_simple(cid='b',buy='bb',sell='sb')
        self.step()
        p=self.root/'state'/'cycles.sqlite'
        with closing(sqlite3.connect(p)) as db:
            c=json.loads(db.execute('SELECT data FROM cycles WHERE id=?',('a',)).fetchone()[0])
            c['nonfinancial_original_metadata']='tampered'
            db.execute('UPDATE cycles SET data=? WHERE id=?',(json.dumps(c),'a'))
            db.commit()
        # It is allowed that the cheap O(1) head/tail check does not detect
        # this tampering. The periodical FULL chain MUST detect it.
        with self.assertRaisesRegex(Drift,'SOURCE_FULL_CHAIN_MISMATCH'):
            self.step()
