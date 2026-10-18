"""Offline behavioral stress tests of exact uploaded OKX v0.7.9 functions.

Compiles selected functions from AST, avoiding top-level imports, .env, OKX I/O
and any file writes to trading state. All trading calls are replaced by mocks.
"""
import ast
import json
import os
import queue
import sys
import unittest
from pathlib import Path
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'okx_bot0711.py'
D = lambda v: Decimal(str(v or '0'))
ENGINE_TREE = ast.parse(SOURCE.read_text())
FUNCTIONS = {n.name: n for n in ENGINE_TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}

# Actual engine definitions, not re-implemented trading decisions.
ACTUAL = ('live_loop', 'coverage_audit', 'TPBufferScheduler', 'tp_buffer_candidates',
          'maintain_virtual_tp', 'active_buy', 'real_tp_active', 'tp_records',
          'active_tp_records', 'remaining_tp', 'cycle_sell_fill', 'virtual_cycles',
          'unprotected_partial_buy', 'count', 'level_free', 'validate_buy_buffer',
          'desired_buy_levels', 'missing_buy_levels_for_addition', 'grid_top',
          'required_safe_tp', 'fmt')

CODE = compile(ast.Module(body=[FUNCTIONS[x] for x in ACTUAL], type_ignores=[]),
               str(SOURCE), 'exec')

class StepDone(Exception): pass

class FakeClock:
    def __init__(self): self.t = 200.
    def monotonic(self): return self.t
    def time(self): return 1000 + self.t
    def sleep(self, sec): self.t += sec
    def perf_counter_ns(self): return int(self.t * 1_000_000_000)

class FakeWake:
    def __init__(self, clock, limits):
        self.clock=clock; self.calls=0; self.limits=limits
    def wait(self, delay):
        self.clock.sleep(max(1.7, delay))
        self.calls += 1
        if self.calls >= self.limits: raise StepDone()
    def clear(self): pass

class FakeMarket:
    def __init__(self): self.bid = D('2400.01')
    def get_bid(self): return self.bid
    def set_grid_reference(self, top): pass

class DummyWorker:
    def submit(self, state): pass
    def submit_reconciled(self, state): pass

class TestLiveBehavior(unittest.TestCase):
    def make_case(self, real_tp, initial_buy, *, reconciled=True,
                  corrupt_coverage=False, passes=1, quiet=True, price='2400.01',
                  drop_bid_before_post=False, virtual_tp=0):
        clock=FakeClock(); operations=[]
        st={'cycles':{},'completed':0, 'version':'0.7.9'}
        # Each SELL represents a confirmed outstanding attached TP: complete BUY,
        # qty 1 (contracts), no prior sell.  Separating BUY and SELL levels
        # allows exact level-occupied checks in the real code.
        for i in range(real_tp):
            px=D('2500') + D(i)*D('.5')
            k=f'{px:.2f}'
            st['cycles'][k]={
                'cycleId':f'tp-{i}', 'state':'TP_WAIT', 'buyPx':k,
                'tpPx':f'{px+D("2.25"):.2f}', 'filledBuy':'1', 'filledTP':'0',
                'tpOrders':[{'phase':'ACTIVE','filled':'0','sz':'1',
                             'ordId':f'S{i}','clOrdId':f'SC{i}'}]}
        for i in range(initial_buy):
            px=D('2399.50')-D(i)*D('.5')
            st['cycles'][f'{px:.2f}']={'cycleId':f'open-{i}',
                'state':'BUY_LIVE','buyPx':f'{px:.2f}','ordId':f'B{i}',
                'filledBuy':'0','filledTP':'0','tpOrders':[]}
        for i in range(virtual_tp):
            px=D('2490')+D(i)*D('.5')
            k=f'{px:.2f}'
            st['cycles'][k]={'cycleId':f'virtual-{i}', 'state':'VIRTUAL_TP',
                'buyPx':k,'tpPx':f'{px+D("2.25"):.2f}',
                'filledBuy':'1', 'filledTP':'0','tpOrders':[]}
        bid_calls=[0]
        def quote():
            bid_calls[0]+=1
            return D('2390.01') if drop_bid_before_post and bid_calls[0]>1 else D(price)
        mk=SimpleNamespace(get_bid=quote,set_grid_reference=lambda x:None)
        wake=FakeWake(clock,passes)
        watcher=SimpleNamespace(events=queue.Queue(),wake=wake)
        namespace={
            'D':D,'Decimal':Decimal,'ROUND_FLOOR':ROUND_FLOOR,'ROUND_CEILING':ROUND_CEILING,
            'TARGET':5,'BUY_MAX':10,'BUY_BUFFER':5,'STEP':D('.5'),
            'TP':D('2.25'),'REAL_TP_TARGET':5,'REAL_TP_MAX':150,
            'TP_BUFFER_COOLDOWN':1.5,'TP_BUFFER_CANCEL_INTERVAL':.1,
            'BUY_BUFFER_COOLDOWN':1.5,'BUY_CANCEL_PROTECT_STEPS':5,
            'REDUCE_ONLY_HARD_CAP':150,'PRICE_STEP':D('.01'),
            'UPPER':D('0'),'CAP':500,'EX_CAP':500,'LIVE':True,
            'time':clock,'log':lambda *a,**k:operations.append(('log',a,k)),
            'die':lambda message: (_ for _ in ()).throw(RuntimeError(message)),
            'control_mode':lambda:'RUN', 'control_ack':lambda *a:None,
            'runtime_heartbeat':lambda:None,
            'persist_crossed_virtual_tps':lambda *a:None,
            'latency_ms':lambda *a,**k:None,
            'LATENCY':SimpleNamespace(dropped=0),
            'pnl_worker':DummyWorker(),
            'ExchangeTemporary': type('ExchangeTemporary',(Exception,),{}),
            'ExchangeUnresolved': type('ExchangeUnresolved',(Exception,),{}),
            'safe_tp_during_buy_wait':lambda *a:None,
            'reconcile_position':None,
            'partial_cleanup_action':lambda *a:'NONE',
            'virtual_escape':lambda *a:'NONE',
            'unplaceable_virtual_tp':lambda st:[],
            'plan_buy_buffer_cleanup':lambda *a:([],[]),
            'cancel_lowest_specific':lambda *a:False,
            'margin_rejection':lambda *a:False,
            'INTERVAL':1.0,
            'A':SimpleNamespace(maker_fee=lambda:D('0.0002')),
            'waiting':False,
            'TP_ACTIONS_PER_PASS':5,
            'tp_quantity_placeable':lambda x:True,
            'place_virtual_tp':lambda st,c:False,
        }
        exec(CODE,namespace)
        namespace['live_loop'].__dict__.clear()
        def reconcile(st, **kw):
            pending=[]
            for c in st['cycles'].values():
                if c['state'] in ('BUY_LIVE','BUY_PARTIAL'):
                    pending.append({'side':'buy','sz':'1','accFillSz':'0','ordId':c['ordId']})
                for r in c.get('tpOrders',[]):
                    if r['phase']=='ACTIVE':
                        pending.append({'side':'sell','sz':r['sz'],
                                        'accFillSz':r['filled'],'ordId':r['ordId']})
            actual = D(real_tp + virtual_tp) - (D('1') if corrupt_coverage else D('0'))
            operations.append(('reconcile', len(pending), None))
            return pending,reconciled,actual
        namespace['reconcile_position']=reconcile
        def place(st,px):
            assert namespace['level_free'](st,px),f'Duplicate BUY price {px}'
            assert namespace['len_check']() < namespace['BUY_MAX']
            k=f'{px:.2f}'
            operations.append(('BUY_POST',k,None))
            st['cycles'][k]={'cycleId':'post-'+k,'state':'BUY_LIVE','buyPx':k,
                             'filledBuy':'0','filledTP':'0','ordId':'new-'+k,
                             'tpOrders':[]}
            return True
        namespace['len_check']=lambda:len(namespace['active_buy'](st))
        namespace['place']=place
        def cancel(st,c,rec):
            operations.append(('TP_CANCEL_POST',rec['ordId'],None))
            rec['phase']='CANCELING'
            return 'CANCELING'
        namespace['cancel_tp']=cancel
        pnl=SimpleNamespace(status=lambda st:'test pnl')
        try:
            namespace['live_loop'](st,watcher,mk,pnl,DummyWorker(),None)
        except StepDone: pass
        return st,operations,namespace

    def test_rotation_at_baseline_defers_until_buy_refilled(self):
        st,operations,ns=self.make_case(5,0,passes=2,virtual_tp=1)
        kinds=[a[0] for a in operations]
        self.assertEqual(kinds.count('BUY_POST'),5)
        self.assertEqual(kinds.count('TP_CANCEL_POST'),1)
        self.assertLess(max(i for i,k in enumerate(kinds) if k=='BUY_POST'),
                        next(i for i,k in enumerate(kinds) if k=='TP_CANCEL_POST'))

    def test_rotation_does_not_cancel_on_first_buy_refill_pass(self):
        st,operations,ns=self.make_case(5,0,passes=1,virtual_tp=1)
        kinds=[a[0] for a in operations]
        self.assertEqual(kinds.count('BUY_POST'),5)
        self.assertEqual(kinds.count('TP_CANCEL_POST'),0)

    def test_mass20_into25_tp_prioritizes_buy_refill(self):
        state,op,ns=self.make_case(25,0)
        kinds=[a[0] for a in op]
        self.assertEqual(kinds.count('BUY_POST'),5)
        self.assertEqual(kinds.count('TP_CANCEL_POST'),0)
        self.assertEqual(len(ns['active_buy'](state)),5)
        self.assertEqual(len({x[1] for x in op if x[0]=='BUY_POST'}),5)

    def test_55_tp_two_existing_buys_refills_exactly_three(self):
        state,op,ns=self.make_case(55,2)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),3)
        self.assertEqual([a[0] for a in op].count('TP_CANCEL_POST'),0)
        self.assertEqual(len(ns['active_buy'](state)),5)

    def test_140_tp_refills_five_with_headroom(self):
        state,op,ns=self.make_case(140,0)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),5)

    def test_144_tp_restricts_capacity_to_one(self):
        state,op,ns=self.make_case(144,0)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),1)

    def test_145_tp_uses_reserve_no_buy(self):
        state,op,ns=self.make_case(145,0)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),0)

    def test_150_tp_cleanup_no_extra_buy(self):
        state,op,ns=self.make_case(150,0)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),0)
        self.assertEqual([a[0] for a in op].count('TP_CANCEL_POST'),1)
        self.assertIn('TP_HARD_CAP_REACHED', [a[1][0] for a in op if a[0]=='log'])

    def test_25_tp_cleanup_after_refill_and_quiet(self):
        state,op,ns=self.make_case(25,0,passes=2)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),5)
        self.assertEqual([a[0] for a in op].count('TP_CANCEL_POST'),1)
        buy_last=max(i for i,a in enumerate(op) if a[0]=='BUY_POST')
        cancel_first=next(i for i,a in enumerate(op) if a[0]=='TP_CANCEL_POST')
        self.assertLess(buy_last,cancel_first)

    def test_145_tp_eventually_trims_one_after_quiet(self):
        state,op,ns=self.make_case(145,0,passes=2)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),0)
        self.assertEqual([a[0] for a in op].count('TP_CANCEL_POST'),1)

    def test_151_tp_fails_closed_before_writes(self):
        with self.assertRaisesRegex(RuntimeError, 'Exchange TP count exceeds 150'):
            self.make_case(151,0)

    def test_existing_buy_5_respects_intentional_plus_five_buffer(self):
        state,op,ns=self.make_case(25,5)
        placed=[a[1] for a in op if a[0]=='BUY_POST']
        self.assertLessEqual(len(ns['active_buy'](state)),10)
        self.assertEqual(len(placed),len(set(placed)))
        # One extra may be deliberately added for Grid Following reserve.
        self.assertEqual(len(placed),1)

    def test_fresh_bid_drop_blocks_unsafe_buy_price(self):
        state,op,ns=self.make_case(25,0,drop_bid_before_post=True)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),0)

    def test_unresolved_state_blocks_all_buy_and_cleanup(self):
        state,op,ns=self.make_case(25,0,reconciled=False)
        self.assertEqual([a[0] for a in op].count('BUY_POST'),0)
        self.assertEqual([a[0] for a in op].count('TP_CANCEL_POST'),0)

    def test_coverage_mismatch_stops_before_all_writes(self):
        with self.assertRaisesRegex(RuntimeError,'(Active TP quantity|Coverage mismatch)'):
            self.make_case(25,0,corrupt_coverage=True)

if __name__=='__main__':
    unittest.main(verbosity=2)
