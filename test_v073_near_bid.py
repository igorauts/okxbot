#!/usr/bin/env python3
"""ETH-only v0.7.3 regression: LIVE 19:01:50 UTC, BUY 2430.00 protected.

All prices are ETH-USDT-SWAP; no API calls, simulated orders only.
"""
import ast
import copy
import unittest
from decimal import Decimal as D
from pathlib import Path

SRC=Path(__file__).with_name('okx_bot073.py')
TREE=ast.parse(SRC.read_text())

def ns():
    funcs=[copy.deepcopy(n) for n in TREE.body if isinstance(n,ast.FunctionDef) and
           n.name in ('grid_top','desired_buy_levels','plan_buy_buffer_cleanup')]
    assert len(funcs)==3
    space={'D':D,'STEP':D('.50'),'TARGET':25,'ROUND_FLOOR':__import__('decimal').ROUND_FLOOR,
           'BUY_CANCEL_PROTECT_STEPS':3, 'ExchangeUnresolved':RuntimeError,
           'die':lambda msg: (_ for _ in ()).throw(ValueError(msg))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=funcs,type_ignores=[])),str(SRC),'exec'),space)
    return space

def buy(v,state='BUY_LIVE'):
    return {'buyPx':f'{D(str(v)):.2f}','state':state,'ordId':f'MOCK{v}'}

class TestNearBid(unittest.TestCase):
    def setUp(self):
        self.m=ns()
        self.desired=lambda bid,occupied=set():self.m['desired_buy_levels'](self.m['grid_top'](D(str(bid))),occupied)
        self.plan=lambda a,d,b,n:self.m['plan_buy_buffer_cleanup'](a,d,D(str(b)),n)

    def test_exact_live_19_01_50_reproduced_and_PROTECTED(self):
        # 25 old BUY 2418.00...2430.00 + newly posted 2417.50 = 26.
        active=[buy(D('2417.50')+D('.50')*i) for i in range(26)]
        desired=self.desired('2430.00')
        self.assertEqual(min(desired),D('2417.50'))
        self.assertEqual(max(desired),D('2429.50'))
        selected,protected=self.plan(active,desired,'2430.00',1)
        self.assertEqual(selected,[], 'NEVER cancel 2430 at BID 2430')
        self.assertEqual([p['buyPx'] for p in protected],['2430.00'])

    def test_after_drop_of_79_cents_also_retained(self):
        active=[buy(D('2417.50')+D('.50')*i) for i in range(26)]
        selected,protected=self.plan(active,self.desired('2429.21'),'2429.21',3)
        self.assertNotIn('2430.00',[c['buyPx'] for c in selected])
        self.assertIn('2430.00',[c['buyPx'] for c in protected])

    def test_boundary_three_steps_is_still_protected(self):
        active=[buy('2430.00')]
        selected,blocked=self.plan(active,set(),'2428.50',1)
        self.assertEqual(selected,[])
        self.assertEqual(len(blocked),1)

    def test_unneeded_distant_stale_buy_may_be_canceled(self):
        active=[buy('2430.00'),buy('2438.00'),buy('2417.50')]
        selected,blocked=self.plan(active,self.desired('2430.00'),'2430.00',5)
        self.assertEqual([c['buyPx'] for c in selected],['2438.00'])
        self.assertEqual([c['buyPx'] for c in blocked],['2430.00'])

    def test_upward_shift_removes_farthest_bottom_first(self):
        active=[buy('2417.50'),buy('2418.00'),buy('2427.00'),buy('2428.00')]
        selected,blocked=self.plan(active,self.desired('2450.00'),'2450.00',3)
        self.assertEqual([c['buyPx'] for c in selected],['2417.50','2418.00','2427.00'])
        self.assertEqual(blocked,[])

    def test_all_protected_full_buffer_means_NO_CANCEL(self):
        active=[buy('2429.50'),buy('2430.00'),buy('2430.50')]
        selected,blocked=self.plan(active,set(),'2430.00',5)
        self.assertEqual(selected,[])
        self.assertEqual(len(blocked),3)

    def test_never_cancel_buy_partial_even_far_away(self):
        active=[buy('2410.00','BUY_PARTIAL'),buy('2411.00')]
        selected,_=self.plan(active,set(),'2430.00',3)
        self.assertEqual([c['buyPx'] for c in selected],['2411.00'])

    def test_never_cancel_member_of_current_desired_grid(self):
        active=[buy('2418.00'),buy('2417.50')]
        selected,blocked=self.plan(active,self.desired('2430.00'),'2430.00',5)
        self.assertEqual(selected,[])
        self.assertEqual(blocked,[])

    def test_if_bid_reverses_before_cancel_recheck_protects(self):
        active=[buy('2430.00')]
        selected,_=self.plan(active,set(),'2420.00',1)
        self.assertEqual(len(selected),1)
        selected_again,blocked=self.plan(selected,set(),'2429.50',1)
        self.assertEqual(selected_again,[])
        self.assertEqual(len(blocked),1)

    def test_invalid_bid_fails_closed(self):
        with self.assertRaises(RuntimeError):
            self.plan([buy('2430.00')],set(),'0',1)

    def test_live_loop_has_fresh_quote_for_each_cancel_and_bidirectional_cooldown(self):
        loops=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop']
        self.assertEqual(len(loops),1)
        src=ast.get_source_segment(SRC.read_text(),loops[0])
        self.assertIn('top!=live_loop._shift_top',src)
        self.assertIn('fresh_bid=market.get_bid()',src)
        self.assertIn('cancel_bid=market.get_bid()',src)
        self.assertIn('plan_buy_buffer_cleanup(',src)
        self.assertNotIn('sorted(obsolete,key=lambda x:D(x',src)

if __name__=='__main__':unittest.main()
