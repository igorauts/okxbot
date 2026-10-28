"""Offline fault injection against the *actual* live_loop AST.

No import of the live bot (which needs real credentials/config), no OKX networking,
no real state files or trading writes. Tests assert integration, not only helpers.
"""
import ast
import queue
import threading
import time
import types
import unittest
from unittest.mock import patch
from pathlib import Path
from decimal import Decimal

from okx_idle_reconcile import wait_for_idle_reconcile, idle_deadline, wait_error_backoff

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT/'okx_bot.py').read_text())
LOOP = next(n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop')

class StopSimulation(Exception):
    pass

class FakeWS:
    def __init__(self):
        self.events=queue.Queue()
        self.wake=threading.Event()
        self.private_wake=threading.Event()
        self.connection_epoch=1
        self.connected=True
        self._healthy=True
    def healthy(self):
        return self._healthy

class FakeMarket:
    def __init__(self):
        self.connection_epoch=1
        self._healthy=True
        self.wake=threading.Event()
    def healthy(self):
        return self._healthy
    def take_wake_causes(self):
        return (0,0)
    def get_bid(self):
        return Decimal('100')
    def cached_bid(self):
        return Decimal('100')
    def set_grid_reference(self,top):
        pass

class FakeLatency:
    dropped=0
    def __init__(self):
        self.events=[]
    def record(self,*args,**kwargs):
        self.events.append((args,kwargs))

class FakeTPBuffer:
    quiet_since=None
    last_cancel_at=None
    def observe(self,*args):
        pass

class FakePnl:
    def status(self,st):
        return 'stable'
    def submit(self,st):
        pass

class Harness:
    def __init__(self, *, allow_buy=False):
        self.mode='RUN'
        self.watcher=FakeWS()
        self.market=FakeMarket()
        self.pnl=FakePnl()
        self.lat=FakeLatency()
        self.reconciles=0
        self.buy_calls=0
        self.audit_waits=0
        self.backoffs=0
        self.max_reconciles=3
        self.allow_buy=allow_buy
        self.on_reconcile=None
        self.on_place=None
        self.local_wait=None
        self.logs=[]
        self.state={'cycles':{},'completed':0}
        n={
            'time':time,'ROOT':types.SimpleNamespace(name='offline-test'),
            'INST':'TEST-USDT-SWAP',
            'INDEPENDENT_AUDIT_SEC':5.0,'LOCAL_CONTROL_POLL_SEC':0.2,
            'TPBufferScheduler':FakeTPBuffer,
            'runtime_heartbeat':lambda:None,
            'control_mode':lambda:self.mode,
            'control_ack':lambda *a,**k:None,
            'emergency_precancel_own_buys':lambda *a,**k:False,
            'LATENCY':self.lat,'latency_ms':lambda *a,**k:None,
            'classify_reconcile_trigger':lambda prev,n,g,v:(prev,prev),
            'reconcile_position':self.reconcile,
            'persist_crossed_virtual_tps':lambda *a,**k:None,
            'log':lambda *a,**k:self.logs.append((a,k)),
            'pnl_submit_start':None,
            'compact_archived':lambda *a:None,
            'controlled_shutdown':lambda *a,**k:True,
            'tp_failure_buy_hold':lambda *a:[],
            'active_buy':lambda *a:[],
            'real_tp_active':lambda *a:[],
            'virtual_cycles':lambda *a:[],
            'tp_records':lambda *a:[],
            'coverage_audit':lambda *a:None,
            'partial_cleanup_action':lambda *a:'NONE',
            'virtual_escape':lambda *a:'NONE',
            'maintain_virtual_tp':lambda *a:'NONE',
            'unplaceable_virtual_tp':lambda *a:[],
            'grid_top':lambda bid:Decimal('100'),
            'validate_buy_buffer':lambda *a:None,
            'desired_buy_levels':lambda *a:([Decimal('90')] if self.allow_buy else []),
            'missing_buy_levels_for_addition':lambda desired,active:desired,
            'count':lambda *a:0,
            'level_free':lambda *a:True,
            'required_safe_tp':lambda *a:Decimal('1'),
            'place':self.place,
            'save':lambda *a:None,
            'plan_buy_buffer_cleanup':lambda *a:([],[]),
            'cancel_lowest_specific':lambda *a:False,
            'latency_ms':lambda *a,**k:None,
            'D':Decimal,'fmt':str,
            'A':types.SimpleNamespace(maker_fee=lambda:Decimal('0')),
            'TP':Decimal('2'),'UPPER':Decimal('0'),
            'REAL_TP_TARGET':0,'TARGET':1,
            'BUY_MAX':6,'CAP':100,'EX_CAP':500,'BUY_BUFFER_COOLDOWN':1.5,
            'REAL_TP_MAX':150,'REDUCE_ONLY_HARD_CAP':150,
            'PARTIAL_AGE':60,'TP_BUFFER_COOLDOWN':1.5,
            'TP_BUFFER_CANCEL_INTERVAL':.1,
            'LIVE':True,'LATENCY':self.lat,
            'ExchangeTemporary':type('ExchangeTemporary',(Exception,),{}),
            'ExchangeUnresolved':type('ExchangeUnresolved',(Exception,),{}),
            'CoverageMismatch':type('CoverageMismatch',(Exception,),{}),
            'safe_tp_during_buy_wait':lambda *a:'NONE',
            'wait_coverage_hold':lambda *a:None,
            'coverage_hold_delay':lambda *a:1,
            'BUY_BUFFER':5,
            'audit_jitter_seconds':lambda *a:0.0,
            'wait_for_idle_reconcile':self.wait,
            'idle_deadline':idle_deadline,
            'wait_error_backoff':self.backoff,
        }
        self.env=n
        n['print']=lambda *a,**k:None
        isolated=ast.Module(body=[LOOP],type_ignores=[])
        exec(compile(isolated,str(ROOT/'okx_bot.py'),'exec'),n)

    def reconcile(self, *args, **kwargs):
        self.reconciles+=1
        if self.reconciles>self.max_reconciles:
            raise StopSimulation()
        if self.on_reconcile:
            return self.on_reconcile(self.reconciles)
        return [], True, Decimal('0')

    def place(self, *args):
        self.buy_calls+=1
        if self.on_place:
            return self.on_place(self.buy_calls)
        return True

    def wait(self, **kwargs):
        self.audit_waits+=1
        if self.local_wait:
            return self.local_wait(kwargs)
        # Do not actually idle for five seconds in an offline simulation.
        return 'timer'

    def backoff(self, **kwargs):
        self.backoffs+=1
        return 'backoff'

    def run(self):
        import okx_idle_reconcile
        with patch.object(okx_idle_reconcile,'wait_for_idle_reconcile',self.wait), \
             patch.object(okx_idle_reconcile,'wait_error_backoff',self.backoff), \
             self.assert_stopped():
            self.env['live_loop'](self.state,self.watcher,self.market,self.pnl,self.pnl)

    @staticmethod
    def assert_stopped():
        return unittest.TestCase().assertRaises(StopSimulation)

class LiveLoopChaosTests(unittest.TestCase):
    def test_quiet_market_has_no_buy_and_waits_on_audit(self):
        h=Harness()
        h.run()
        self.assertEqual(h.buy_calls,0)
        self.assertEqual(h.audit_waits,3)
        self.assertEqual(h.reconciles,4)

    def test_private_ws_fill_preempts_idle_five_second_wait(self):
        h=Harness()
        def wake(kwargs):
            self.assertGreaterEqual(kwargs['max_wait_seconds'],5)
            e=kwargs['wake']
            timer=threading.Timer(.025,e.set)
            timer.start()
            try:
                started=time.monotonic()
                result=wait_for_idle_reconcile(**kwargs)
                self.assertLess(time.monotonic()-started,.4)
                return result
            finally: timer.join()
        h.local_wait=wake
        h.run()
        self.assertEqual(h.audit_waits,3)

    def test_ws_epoch_change_during_reconcile_discards_old_snapshot(self):
        h=Harness(allow_buy=True)
        def reconcile(n):
            if n==1: h.watcher.connection_epoch+=1
            return [],True,Decimal('0')
        h.on_reconcile=reconcile
        h.max_reconciles=1
        h.run()
        self.assertEqual(h.buy_calls,0)
        self.assertEqual(h.audit_waits,0)

    def test_unhealthy_private_ws_blocks_buy_with_rest_still_reconciling(self):
        h=Harness(allow_buy=True)
        h.watcher._healthy=False
        h.run()
        self.assertEqual(h.buy_calls,0)
        self.assertEqual(h.reconciles,4)
        self.assertEqual(h.audit_waits,3)
        self.assertTrue(any(e[0][0]=='BUY_HOLD_WS_HEALTH' for e in h.logs))

    def test_unhealthy_public_ws_blocks_buy_with_rest_still_reconciling(self):
        h=Harness(allow_buy=True)
        h.market._healthy=False
        h.run()
        self.assertEqual(h.buy_calls,0)
        self.assertEqual(h.audit_waits,3)

    def test_unknown_post_blocks_replacement_until_reconciliation_resolves(self):
        h=Harness(allow_buy=True)
        def unknown(n):
            raise h.env['ExchangeUnresolved']('unknown ACK')
        def reconcile(n):
            if n==2: return [],False,Decimal('0')
            return [],True,Decimal('0')
        h.on_reconcile=reconcile
        h.on_place=unknown
        h.max_reconciles=2
        h.run()
        self.assertEqual(h.backoffs,1)
        self.assertEqual(h.buy_calls,1,'No second POST while prior outcome remains unresolved')
        self.assertEqual(h.reconciles,3,'Unresolved POST must return to REST reconcile')

    def test_private_fill_arrives_during_grid_calculation_blocks_buy(self):
        h=Harness(allow_buy=True)
        def desired(*args):
            h.watcher.events.put({'ordId':'late-fill','side':'sell','state':'filled'})
            h.watcher.wake.set()
            return [Decimal('90')]
        h.env['desired_buy_levels']=desired
        h.max_reconciles=1
        h.run()
        self.assertEqual(h.buy_calls,0,'Late fill during computation must block POST')

    def test_public_ws_reconnect_during_rest_blocks_buy(self):
        h=Harness(allow_buy=True)
        def reconcile(n):
            if n==1: h.market.connection_epoch+=1
            return [],True,Decimal('0')
        h.on_reconcile=reconcile
        h.max_reconciles=1
        h.run()
        self.assertEqual(h.buy_calls,0)


    def test_private_fill_arrives_during_rest_must_not_trade_from_stale_reconcile(self):
        h=Harness(allow_buy=True)
        def reconcile(n):
            if n==1:
                h.watcher.events.put({'ordId':'synthetic','side':'buy','state':'filled'})
                h.watcher.wake.set()
            return [],True,Decimal('0')
        h.on_reconcile=reconcile
        h.max_reconciles=1
        h.run()
        self.assertEqual(h.buy_calls,0,'A new private fill after REST snapshot requires another REST reconcile before BUY')

    def test_panic_during_rest_outage_interrupts_backoff_and_never_places_buy(self):
        h=Harness(allow_buy=True)
        def reconcile(n):
            if n==1:
                raise h.env['ExchangeTemporary']('offline TEST only')
            return [],True,Decimal('0')
        def backoff(**kw):
            h.backoffs+=1
            h.mode='PANIC'
            return 'control'
        h.on_reconcile=reconcile
        import okx_idle_reconcile
        with patch.object(okx_idle_reconcile,'wait_error_backoff',backoff):
            h.env['live_loop'](h.state,h.watcher,h.market,h.pnl,h.pnl)
        self.assertEqual(h.reconciles,2)
        self.assertEqual(h.buy_calls,0)
        self.assertEqual(h.backoffs,1)


    def test_panic_changed_while_rest_raises_bypasses_three_second_backoff(self):
        h=Harness(allow_buy=True)
        def reconcile(n):
            if n==1:
                h.mode='PANIC'
                raise h.env['ExchangeTemporary']('outage immediately after control change')
            return [],True,Decimal('0')
        h.on_reconcile=reconcile
        import okx_idle_reconcile
        observations=[]
        def backoff(**kwargs):
            observations.append((kwargs['original_control'],kwargs['control_read']()))
            self.assertNotEqual(*observations[-1])
            return 'control'
        with patch.object(okx_idle_reconcile,'wait_error_backoff',backoff):
            h.env['live_loop'](h.state,h.watcher,h.market,h.pnl,h.pnl)
        self.assertEqual(observations,[('RUN','PANIC')])
        self.assertEqual(h.reconciles,2)
        self.assertEqual(h.buy_calls,0)

    def test_burst_private_fills_drained_before_one_rest_reconcile(self):
        h=Harness()
        for i in range(150):
            h.watcher.events.put({'ordId':str(i),'side':'buy','state':'filled'})
        h.max_reconciles=1
        h.run()
        ws_events=[e for e in h.logs if e[0][0]=='WS_ORDER']
        self.assertEqual(len(ws_events),150)
        self.assertEqual(h.reconciles,2)

    def test_control_change_mid_reconcile_disallows_buy(self):
        h=Harness(allow_buy=True)
        def reconcile(n):
            if n==1:h.mode='PANIC'
            return [],True,Decimal('0')
        h.on_reconcile=reconcile
        h.max_reconciles=2
        # Second pass should terminate through controlled_shutdown; no BUY.
        h.env['live_loop'](h.state,h.watcher,h.market,h.pnl,h.pnl)
        self.assertEqual(h.buy_calls,0)

if __name__=='__main__':unittest.main()
