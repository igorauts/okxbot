"""Offline phase-alignment experiment: no orders, no exchange, no LIVE."""
import ast
import math
import threading
import time
import unittest
from pathlib import Path
from okx_idle_reconcile import aligned_idle_audit_wait, wait_for_idle_reconcile, idle_deadline

ROOT=Path(__file__).resolve().parents[1]

class QuietAlignedAuditTests(unittest.TestCase):
    def test_stagger_removed_when_explicitly_enabled(self):
        for wall in [100.0,100.1,102.1,104.2,105.01,105.91]:
            d=aligned_idle_audit_wait(wall,5.0)
            self.assertGreaterEqual(d,.1)
            self.assertLessEqual(d,5.0)
        self.assertAlmostEqual(aligned_idle_audit_wait(103.0),2.0)
        self.assertAlmostEqual(aligned_idle_audit_wait(101.0),4.0)

    def test_bad_wall_falls_back_to_bounded_cadence(self):
        for w in (float('nan'),float('inf'),float('-inf'),-1):
            self.assertEqual(aligned_idle_audit_wait(w),5.0)
        for p in (0.0,-1.,float('inf')):
            with self.assertRaises(ValueError):aligned_idle_audit_wait(100.,p)

    def test_wake_and_panic_still_preempt(self):
        e=threading.Event()
        t=threading.Timer(.025,e.set);t.start()
        try:
            out=wait_for_idle_reconcile(wake=e,control_read=lambda:'RUN',
                original_control='RUN',ws_health=lambda:(True,),
                initial_ws_health=(True,),extra_due=None,
                max_wait_seconds=aligned_idle_audit_wait(104.),local_poll_seconds=.05)
            self.assertEqual(out,'wake')
        finally:t.join()
        mode=['RUN'];t=threading.Timer(.025,lambda:mode.__setitem__(0,'PANIC'));t.start()
        try:
            out=wait_for_idle_reconcile(wake=threading.Event(),control_read=lambda:mode[0],
                original_control='RUN',ws_health=lambda:(True,),
                initial_ws_health=(True,),extra_due=None,
                max_wait_seconds=aligned_idle_audit_wait(103.),local_poll_seconds=.05)
            self.assertEqual(out,'control')
        finally:t.join()

    def test_extra_due_remains_earlier_than_aligned_clock(self):
        now=time.monotonic()
        out=wait_for_idle_reconcile(wake=threading.Event(),control_read=lambda:'RUN',
            original_control='RUN',ws_health=lambda:(True,),initial_ws_health=(True,),
            extra_due=now+.03,max_wait_seconds=4.,local_poll_seconds=.05)
        self.assertEqual(out,'timer')

    def test_source_contract_safe_and_opt_in_only(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='live_loop')
        src=ast.unparse(fn)
        self.assertIn("globals().get('SYNC_IDLE_AUDITS_EXPERIMENT', False)",src)
        self.assertIn('b2_aligned_deduplicated_wait(',src)
        self.assertEqual(src.count('reconcile_position('),1)
        self.assertLess(src.index('reconcile_position('),src.index('coverage_audit('))
        self.assertLess(src.index('coverage_audit('),src.index('virtual_escape('))
        self.assertLess(src.index('virtual_escape('),src.index('if place(st, price)'))
        self.assertIn('extra_due=earliest_due',src)
        for x in ast.walk(fn):
            if isinstance(x,(ast.If,ast.While,ast.IfExp)) and x.test:
                # Never make financial decisions depend on our telemetry.
                self.assertNotIn('reconcile_trigger',ast.unparse(x.test))

if __name__=='__main__':unittest.main()
