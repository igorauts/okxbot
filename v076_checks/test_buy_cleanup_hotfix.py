#!/usr/bin/env python3
"""Deterministic offline checks of exact production cleanup function by AST.
No engine imports, env, network calls, exchange orders, or LIVE writes.
"""
import ast
from decimal import Decimal
from pathlib import Path
import unittest

CODE=Path(__file__).with_name('okx_bot076.py').read_text()
TREE=ast.parse(CODE)
FN=next(n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='plan_buy_buffer_cleanup')
class ExchangeUnresolved(RuntimeError):pass
D=lambda v:Decimal(str(v))
NAMESPACE={'D':D,'STEP':Decimal('0.03'),'BUY_CANCEL_PROTECT_STEPS':5,
           'ExchangeUnresolved':ExchangeUnresolved}
exec(compile(ast.Module(body=[FN],type_ignores=[]),'<production clean-up function>','exec'),NAMESPACE)
PLAN=NAMESPACE['plan_buy_buffer_cleanup']

def buy(px, state='BUY_LIVE', order_id=None):
    return {'buyPx':str(px),'state':state,'ordId':str(order_id or px)}

class CleanupHotfixTests(unittest.TestCase):
    def setUp(self):
        self.active=[buy(Decimal('104.67')+Decimal('0.03')*i) for i in range(26)]
        self.desired={Decimal('105.39')-Decimal('0.03')*i for i in range(25)}

    def test_exact_real_sol_regression(self):
        # BID 105.42: top 105.42 is outside desired; lowest 104.67 is inside.
        self.assertEqual(Decimal('105.42') not in self.desired,True)
        self.assertIn(Decimal('104.67'),self.desired)
        # Correct behaviour independent of desired: cancel bottom-most 104.67.
        chosen, protected=PLAN(list(reversed(self.active)),D('105.42'),1)
        self.assertEqual([x['buyPx'] for x in chosen],['104.67'])
        self.assertIn('105.42',[x['buyPx'] for x in protected])

    def test_small_price_move_does_not_flip_to_top(self):
        for bid in ['105.42','105.43','105.44','105.47','105.40','105.38']:
            with self.subTest(bid=bid):
                chosen,_=PLAN(self.active,D(bid),1)
                self.assertEqual(chosen[0]['buyPx'],'104.67')

    def test_bottom_up_sorted_not_abs_distance(self):
        chosen,_=PLAN(list(reversed(self.active)),D('105.42'),5)
        self.assertEqual([c['buyPx'] for c in chosen],
                         ['104.67','104.70','104.73','104.76','104.79'])

    def test_guard_five_steps_never_cancel_near_bid(self):
        # cutoff 105.42 - 5*0.03 = 105.27, strictly below cutoff required.
        chosen,pro=PLAN(self.active,D('105.42'),30)
        self.assertTrue(all(D(c['buyPx'])<D('105.27') for c in chosen))
        self.assertIn('105.27',[c['buyPx'] for c in pro])
        self.assertIn('105.42',[c['buyPx'] for c in pro])

    def test_drop_below_orders_blocks_cancellations(self):
        chosen,protected=PLAN(self.active,D('104.69'),10)
        self.assertEqual(chosen,[])
        self.assertEqual(len(protected),26)

    def test_partial_or_unconfirmed_buy_cannot_be_canceled(self):
        a=[buy('104.67','BUY_PARTIAL'),buy('104.70','PLACING'),
           buy('104.73','CANCELING'),buy('104.76'),buy('104.79')]
        chosen,_=PLAN(a,D('105.42'),5)
        self.assertEqual([c['buyPx'] for c in chosen],['104.76','104.79'])
        a[3]['ordId']=''
        chosen,_=PLAN(a,D('105.42'),5)
        self.assertEqual([c['buyPx'] for c in chosen],['104.79'])

    def test_no_quote_or_no_quota_does_nothing(self):
        with self.assertRaises(ExchangeUnresolved):PLAN(self.active,None,1)
        self.assertEqual(PLAN(self.active,D('105.42'),0),([],[]))

    def test_unchanged_strategy_parameters(self):
        for needle in ["VER = '0.7.6'",'BUY_BUFFER = 5',
                       'BUY_CANCEL_PROTECT_STEPS = 5',
                       'REAL_TP_RESERVE = TARGET + BUY_BUFFER',
                       'BUY_BUFFER_COOLDOWN = 1.5',
                       'time.sleep(0.05)  # v0.7.5: BUY pacing 50ms']:
            self.assertIn(needle,CODE)

    def test_all_buy_revalidation_before_cancel(self):
        # Second gate must review the WHOLE grid with fresh BID, not just [c].
        start=CODE.index('            for c in selected:')
        end=CODE.index('                if moved:',start)
        block=CODE[start:end]
        self.assertIn('active_buy(st),cancel_bid,1',block)
        self.assertIn("fresh_lowest[0].get('ordId')!=c.get('ordId')",block)
        self.assertLess(block.index('fresh_lowest[0]'),block.index('cancel_lowest_specific(st,c)'))

    def test_production_cleanup_ignores_desired_grid(self):
        self.assertNotIn('desired',set(FN.args.args[i].arg for i in range(len(FN.args.args))))
        self.assertNotIn('desired',ast.unparse(FN.body))
        self.assertIn('desired_buy_levels(top,occupied_tp)',CODE) # BUY placement unchanged

if __name__=='__main__':unittest.main(verbosity=2)
