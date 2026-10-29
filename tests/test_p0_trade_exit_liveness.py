"""P0 starvation regressions on the real, isolated live_loop AST.
No OKX, file IO, process/service control, or real trading writes.
"""
import unittest
from decimal import Decimal
from tests.test_live_loop_chaos_rc4 import Harness

class TradeExitLiveness(unittest.TestCase):
    def test_private_events_during_every_reconcile_do_not_starve_virtual_escape(self):
        h = Harness(allow_buy=True)
        reached = []
        def reconcile(n):
            h.watcher.events.put({'ordId':str(n),'side':'sell','state':'filled'})
            return [],True,Decimal('0')
        def escape(*args):
            reached.append(h.reconciles)
            return 'NONE'
        h.on_reconcile = reconcile
        h.env['virtual_escape'] = escape
        h.max_reconciles = 4
        h.run()
        self.assertEqual(reached,[1,2,3,4])
        self.assertEqual(h.buy_calls,0,'New private events forbid stale BUY')

    def test_epoch_churn_during_every_reconcile_does_not_starve_virtual_escape(self):
        h = Harness(allow_buy=True)
        reached = []
        def reconcile(n):
            h.watcher.connection_epoch += 1
            return [],True,Decimal('0')
        def escape(*args):
            reached.append(h.reconciles)
            return 'NONE'
        h.on_reconcile = reconcile
        h.env['virtual_escape'] = escape
        h.max_reconciles = 4
        h.run()
        self.assertEqual(reached,[1,2,3,4])
        self.assertEqual(h.buy_calls,0,'WS epoch change forbids stale BUY')

    def test_panic_still_reaches_shutdown_if_epoch_changes_during_rest(self):
        h = Harness(allow_buy=True)
        h.mode='PANIC'
        called=[]
        def reconcile(n):
            h.watcher.connection_epoch += 1
            return [],True,Decimal('0')
        def shutdown(*args):
            called.append(h.reconciles)
            return True
        h.on_reconcile = reconcile
        h.env['controlled_shutdown'] = shutdown
        h.max_reconciles=1
        h.env['live_loop'](h.state,h.watcher,h.market,h.pnl,h.pnl)
        self.assertEqual(called,[1])
        self.assertEqual(h.buy_calls,0)

    def test_hard_still_reaches_shutdown_if_epoch_changes_during_rest(self):
        h = Harness(allow_buy=True)
        h.mode='HARD'
        called=[]
        def reconcile(n):
            h.market.connection_epoch += 1
            return [],True,Decimal('0')
        def shutdown(*args):
            called.append(h.reconciles)
            return True
        h.on_reconcile = reconcile
        h.env['controlled_shutdown'] = shutdown
        h.max_reconciles=1
        h.env['live_loop'](h.state,h.watcher,h.market,h.pnl,h.pnl)
        self.assertEqual(called,[1])
        self.assertEqual(h.buy_calls,0)

    def test_private_event_burst_does_not_bypass_unresolved_state_hold(self):
        h=Harness(allow_buy=True)
        reached=[]
        def reconcile(n):
            h.watcher.events.put({'ordId':str(n),'side':'buy','state':'filled'})
            return [],False,Decimal('0')
        def escape(*args):
            reached.append(h.reconciles)
            return 'NONE'
        h.on_reconcile=reconcile
        h.env['virtual_escape']=escape
        h.max_reconciles=2
        h.run()
        self.assertEqual(reached,[])
        self.assertEqual(h.buy_calls,0)

if __name__=='__main__':
    unittest.main()
