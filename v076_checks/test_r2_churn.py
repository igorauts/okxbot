#!/usr/bin/env python3
"""Offline AST tests. Never import trading engine or contact exchange."""
import ast
import random
from decimal import Decimal
from pathlib import Path
import unittest

FILE=Path(__file__).with_name('okx_bot076.py')
SOURCE=FILE.read_text()
TREE=ast.parse(SOURCE)
NAMES=('grid_top','desired_buy_levels','missing_buy_levels_for_addition','plan_buy_buffer_cleanup')
FUNCS=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES]
assert len(FUNCS)==len(NAMES)

class ExchangeUnresolved(Exception):pass
D=lambda v: Decimal(str(v))
N={'D':D,'STEP':D('0.03'),'TARGET':25,'BUY_CANCEL_PROTECT_STEPS':5,'ExchangeUnresolved':ExchangeUnresolved,
   'ROUND_FLOOR':__import__('decimal').ROUND_FLOOR}
exec(compile(ast.Module(body=FUNCS,type_ignores=[]),'<exact production functions>','exec'),N)

def b(p,i):return {'buyPx':str(p),'state':'BUY_LIVE','ordId':str(i)}

def fixture(bid,step):
    N['STEP']=D(step)
    top=N['grid_top'](D(bid))
    desired=N['desired_buy_levels'](top,set())
    return desired

class BotChurnR2(unittest.TestCase):
    def test_SOL_identical_bid_old_rebuy_loop_is_suppressed(self):
        desired=fixture('105.33','0.03')
        active=[b(D('105.33')-D('0.03')*i,i) for i in range(25)]
        self.assertEqual(sorted(desired-{D(c['buyPx']) for c in active}),[D('104.58')])
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[])
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[])

    def test_ETH_identical_bid_old_rebuy_loop_is_suppressed(self):
        desired=fixture('2440.00','0.50')
        active=[b(D('2440')-D('0.5')*i,i) for i in range(25)]
        self.assertEqual(sorted(desired-{D(c['buyPx']) for c in active}),[D('2427.5')])
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[])

    def test_below_25_BUYs_fill_bottom_to_target(self):
        desired=fixture('105.33','0.03')
        active=[b(D('105.30')-D('0.03')*i,i) for i in range(24)]
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[D('104.58')])

    def test_higher_near_bid_level_is_added_on_upward_move(self):
        desired=fixture('105.36','0.03')
        active=[b(D('105.30')-D('0.03')*i,i) for i in range(25)]
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[D('105.33')])

    def test_gap_within_top_25_is_filled(self):
        desired=fixture('105.36','0.03')
        # 105.24 absent, highest 25th existing is 104.58.
        active=[b(D('105.33')-D('0.03')*i,i) for i in range(26) if i!=3]
        missing=N['missing_buy_levels_for_addition'](desired,active)
        self.assertEqual(missing,[D('105.24')])

    def test_26_with_desired_below_top_25_remains_stable(self):
        desired=fixture('105.33','0.03')
        active=[b(D('105.33')-D('0.03')*i,i) for i in range(26)]
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[])
        selected,protected=N['plan_buy_buffer_cleanup'](active,D('105.33'),1)
        self.assertEqual([c['buyPx'] for c in selected],['104.58'])
        self.assertTrue(protected)

    def test_desired_skips_busy_tp_and_extends_down_but_not_posted_below_floor(self):
        N['STEP']=D('0.03')
        desired=N['desired_buy_levels'](N['grid_top'](D('105.33')),{D('105.12')})
        active=[b(D('105.33')-D('0.03')*i,i) for i in range(25)]
        self.assertEqual(N['missing_buy_levels_for_addition'](desired,active),[])

    def test_random_floor_invariant(self):
        rng=random.Random(7502)
        step=D('0.03'); N['STEP']=step
        for _ in range(800):
            bid=D('100')+step*rng.randrange(100,300)
            desired=N['desired_buy_levels'](N['grid_top'](bid),set())
            n=rng.randrange(22,31)
            levels=rng.sample(range(1,60),n)
            active=[b(bid-step*k,i) for i,k in enumerate(levels)]
            missing=N['missing_buy_levels_for_addition'](desired,active)
            if n>=25:
                floor=sorted([D(c['buyPx']) for c in active],reverse=True)[24]
                self.assertTrue(all(p>floor for p in missing))
            else:
                self.assertEqual(missing,sorted(desired-{D(c['buyPx']) for c in active},reverse=True))

    def test_live_loop_uses_filter_instead_of_unfiltered_missing(self):
        self.assertIn('missing=missing_buy_levels_for_addition(desired,active)',SOURCE)
        loop=next(n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop')
        self.assertNotIn('missing=sorted((px for px in desired if px not in active_prices),reverse=True)',ast.unparse(loop))

    def test_deployment_does_not_touch_cancel_selection_or_post_intents(self):
        old=ast.parse(Path(__file__).with_name('r1_original.py').read_text())
        for fname in ('place','cancel_lowest_specific','plan_buy_buffer_cleanup'):
            a=next(x for x in old.body if isinstance(x,ast.FunctionDef) and x.name==fname)
            n=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name==fname)
            self.assertEqual(ast.dump(a),ast.dump(n),fname)

if __name__=='__main__':unittest.main()
