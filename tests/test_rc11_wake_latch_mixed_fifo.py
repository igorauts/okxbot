"""P0 regression suite: persistent TP wake latch and mixed-fleet direct FIFO.

Pure AST-isolated trading functions, mocked WS and quota, zero OKX I/O.
"""
import ast
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from okx_api import use_priority_positions_fifo
from tests.test_rc9_ws_wake_trace import watcher_class

ROOT=Path(__file__).resolve().parents[1]


def persist_function():
    tree=ast.parse((ROOT/'okx_bot.py').read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
            and n.name=='persist_crossed_virtual_tps')
    ns={'D':Decimal}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(ROOT/'okx_bot.py'),'exec'),ns)
    return ns


class SustainedBidNoStorm(unittest.TestCase):
    def test_paused_and_soft_bots_do_not_rewake_on_unchanged_tp(self):
        # Reproduce main loop: take/copy timestamps, republish virtual targets
        # after each reconciled pass. Keeping BID > TP with a blocked exit
        # MUST NOT schedule an unbounded next pass.
        for mode in ('PAUSE','SOFT','RUN_BLOCKED_BY_UNRESOLVED_EXIT'):
            with self.subTest(mode=mode):
                wake=threading.Event()
                w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
                w.connected=True
                w._bid=Decimal('105')
                w._updated=time.monotonic()
                state={'cycles':{str(i):{'cycleId':f'c{i}','state':'VIRTUAL_TP',
                                       'tpPx':str(100+i),'tpOrders':[]}
                                 for i in range(3)}}
                ns=persist_function()
                saves=[]
                ns['save']=lambda st:saves.append(1)
                ns['virtual_cycles']=lambda st:[(k,c) for k,c in st['cycles'].items()
                                                if c['state']=='VIRTUAL_TP']
                w.set_virtual_targets({c['cycleId']:Decimal(c['tpPx'])
                                       for c in state['cycles'].values()})
                self.assertTrue(wake.is_set())
                # Simulate 250 consecutive main-loop passes with BID stuck
                # above every TP. No new WS edge and no sale allowed.
                for n in range(250):
                    wake.clear()
                    ns['persist_crossed_virtual_tps'](state,w)
                    self.assertFalse(wake.is_set(), f'{mode}: wake storm at pass {n}')
                    self.assertFalse(w._capture_crossings(Decimal('105')))
                self.assertEqual(len(saves),1)
                self.assertTrue(all(c['virtualTpCrossed'] for c in state['cycles'].values()))

    def test_consuming_first_cross_does_not_rearm_high_bid(self):
        wake=threading.Event()
        w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
        w.connected=True;w._bid=Decimal('102');w._updated=time.monotonic()
        w.set_virtual_targets({'a':Decimal('100')})
        self.assertTrue(wake.is_set())
        saved=w.take_crossed_virtual_stamps()
        self.assertEqual(set(saved),{'a'})
        wake.clear()
        self.assertEqual(w.take_crossed_virtual_stamps(),{})
        for _ in range(100):
            w.set_virtual_targets({'a':Decimal('100')})
            self.assertFalse(w._capture_crossings(Decimal('102')))
            self.assertFalse(wake.is_set())
        self.assertIn('a',w._virtual_woken)

    def test_bid_retrace_rearms_then_next_upcross_wakes_once(self):
        wake=threading.Event()
        w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
        w.connected=True;w._bid=Decimal('101');w._updated=time.monotonic()
        w.set_virtual_targets({'a':Decimal('100')})
        wake.clear();w.take_crossed_virtual_stamps()
        self.assertFalse(w._capture_crossings(Decimal('101')))
        self.assertFalse(w._capture_crossings(Decimal('99.9')))
        self.assertNotIn('a',w._virtual_woken)
        # Quick retrace is suppressed; first WS stamp persists.
        self.assertFalse(w._capture_crossings(Decimal('100')))
        self.assertFalse(w._capture_crossings(Decimal('101')))
        self.assertIn('a',w.take_crossed_virtual_stamps())

    def test_removed_or_repriced_target_rearms_without_old_stamp(self):
        wake=threading.Event()
        w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
        w.connected=True;w._bid=Decimal('105');w._updated=time.monotonic()
        w.set_virtual_targets({'a':Decimal('100')})
        self.assertTrue(wake.is_set())
        wake.clear()
        # Removal must clear both latch and pending stamp for that epoch.
        w.set_virtual_targets({})
        self.assertNotIn('a',w._virtual_woken)
        self.assertNotIn('a',w._virtual_crossed)
        w.set_virtual_targets({'a':Decimal('100')})
        self.assertTrue(wake.is_set())
        wake.clear();w.take_crossed_virtual_stamps()
        w.set_virtual_targets({'a':Decimal('101')})
        self.assertTrue(wake.is_set())
        self.assertIn('a',w.take_crossed_virtual_stamps())

    def test_virtual_bid_only_wakes_no_trading_action(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)
                 and n.name=='PublicBidWatcher')
        text=ast.unparse(cls)
        self.assertIn('self._virtual_woken',text)
        self.assertIn('self._shared_wake.set()',text)
        self.assertNotIn("A.req('POST'",text)
        self.assertNotIn('save(st)',text)


class DirectFIFOInMixedFleet(unittest.TestCase):
    def test_mixed_and_incomplete_config_cannot_use_priority(self):
        configs=[{},
                 {'OKX_POSITION_READER_SOCKET':'/run/reader.sock'},
                 {'OKX_POSITION_READER_SOCKET':'/run/reader.sock',
                  'OKX_POSITION_READER_QUOTA_SCHEDULING':'mixed-fifo'},
                 {'OKX_POSITION_READER_SOCKET':'/run/reader.sock',
                  'OKX_POSITION_READER_QUOTA_SCHEDULING':'mixed-fifo',
                  'OKX_POSITION_READER_PRIORITY_ACK':'ALL_BOTS_PRIORITY_AWARE'},
                 {'OKX_POSITION_READER_SOCKET':'/run/reader.sock',
                  'OKX_POSITION_READER_QUOTA_SCHEDULING':'priority'},
                 {'OKX_POSITION_READER_QUOTA_SCHEDULING':'priority',
                  'OKX_POSITION_READER_PRIORITY_ACK':'ALL_BOTS_PRIORITY_AWARE'}]
        for config in configs:
            with self.subTest(config=config):
                self.assertFalse(use_priority_positions_fifo(config))

    def test_priority_only_when_entire_fleet_operator_ack(self):
        self.assertTrue(use_priority_positions_fifo({
            'OKX_POSITION_READER_SOCKET':'/run/reader.sock',
            'OKX_POSITION_READER_QUOTA_SCHEDULING':'priority',
            'OKX_POSITION_READER_PRIORITY_ACK':'ALL_BOTS_PRIORITY_AWARE'}))

    def test_direct_get_and_fallback_share_same_original_fifo_by_default(self):
        src=ast.unparse(ast.parse((ROOT/'okx_api.py').read_text()))
        self.assertIn('if use_priority_positions_fifo(ENV):',src)
        self.assertIn('ACCOUNT_POSITIONS_GATE.take()',src)
        self.assertIn('ACCOUNT_POSITIONS_GATE.take(lambda: direct_position_priority(self, control_mode))',src)
        # In req(), both fallback and narrow zero-position reads take same gate.
        self.assertIn("if method == 'GET' and route == '/api/v5/account/positions':",src)

if __name__=='__main__':unittest.main(verbosity=2)
