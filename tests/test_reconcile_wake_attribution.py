"""Offline-only wake-reason instrumentation contract; no live bot imports/I/O."""
import ast
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from tests.test_rc9_ws_wake_trace import watcher_class

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'okx_bot.py'


def classifier():
    tree=ast.parse(SRC.read_text())
    fn=next(node for node in tree.body if isinstance(node,ast.FunctionDef)
            and node.name=='classify_reconcile_trigger')
    ns={}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(SRC),'exec'),ns)
    return ns['classify_reconcile_trigger']


class PureWakeReasonTests(unittest.TestCase):
    def test_default_timer_and_internal_and_overrun(self):
        c=classifier()
        for name in ('timer','internal','overrun','recovery','wake'):
            self.assertEqual(c(name,0,0,0),(name,name))
        self.assertEqual(c('unexpected',0,0,0),('internal','internal'))

    def test_private_and_public_wakes_preserved_when_simultaneous(self):
        c=classifier()
        self.assertEqual(c('timer',2,1,1),('private','private+virtual+grid'))
        self.assertEqual(c('timer',0,0,1),('virtual','virtual'))
        self.assertEqual(c('timer',0,1,0),('grid','grid'))
        self.assertEqual(c('overrun',0,1,1),('virtual','virtual+grid'))

    def test_virtual_crossing_counter_is_edge_triggered(self):
        wake=threading.Event()
        w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
        w.connected=True
        w._bid=Decimal('103')
        import time
        w._updated=time.monotonic()
        w.set_virtual_targets({'cycle':Decimal('100')})
        self.assertTrue(wake.is_set())
        self.assertEqual(w.take_wake_causes(),(0,1))
        wake.clear()
        for _ in range(30):
            w.set_virtual_targets({'cycle':Decimal('100')})
        self.assertFalse(wake.is_set())
        self.assertEqual(w.take_wake_causes(),(0,0))
        # Taking diagnostics must not consume the actual TP crossing latch.
        self.assertEqual(set(w.take_crossed_virtual_stamps()),{'cycle'})

    def test_grid_counter_consumption_cannot_schedule_trade(self):
        wake=threading.Event()
        w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
        with w._lock:
            w._note_public_wake(True,False)
            w._note_public_wake(True,True)
        self.assertEqual(w.take_wake_causes(),(2,1))
        self.assertEqual(w.take_wake_causes(),(0,0))
        self.assertFalse(wake.is_set())

    def test_trading_api_paths_and_safety_operations_unmodified(self):
        tree=ast.parse(SRC.read_text())
        loop=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop')
        src=ast.unparse(loop)
        # Reconcile calls unchanged, critical ordering remains.
        self.assertEqual(src.count('reconcile_position('),1)
        self.assertLess(src.index('reconcile_position('),src.index('coverage_audit('))
        self.assertLess(src.index('coverage_audit('),src.index('virtual_escape('))
        self.assertLess(src.index('virtual_escape('),src.index('if place(st, price)'))
        self.assertIn("LATENCY.record('reconcile_trigger'",src)
        self.assertIn("reason=reason",src)
        self.assertIn('watcher.private_wake.clear()',src)
        # No branch decisions depend on diagnostic wake attribution.
        for n in ast.walk(loop):
            if isinstance(n,(ast.If,ast.While,ast.IfExp)):
                test=ast.unparse(n.test)
                self.assertNotIn('grid_wakes',test)
                self.assertNotIn('virtual_wakes',test)
                self.assertNotIn('trigger_from_previous_pass',test)
                if 'source' in test:
                    self.assertIn("source == 'timer'",test)
                    self.assertIn('B1_ACTIVE_BACKGROUND_EXPERIMENT',test)
                self.assertNotIn('reason',test) if test == 'reason' else None

if __name__=='__main__':
    unittest.main()
