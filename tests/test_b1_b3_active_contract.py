"""Offline tests for active B1 + interruptible B3 (no exchange)."""
import ast
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from okx_idle_reconcile import b1_active_after_orders,b3_background_interrupted
from okx_position_reader_client import read_positions,PositionReaderPreempted

ROOT=pathlib.Path(__file__).resolve().parents[1]
class Watcher:
    def __init__(self):
        self.wake=threading.Event();self.private_wake=threading.Event()
        self.events=__import__('queue').Queue();self.connection_epoch=1
    def healthy(self):return True
class Market:
    def __init__(self):
        self._lock=threading.Lock();self._grid_wake_count=0
        self._virtual_wake_count=0;self._virtual_crossed={}
        self.connection_epoch=1
    def healthy(self):return True
class ActiveSafety(unittest.TestCase):
    def setUp(self):
        self.w=Watcher();self.m=Market()
        self.st={'cycles':{},'aggregate_exits':[]}
    def policy(self,**extras):
        args=dict(source='timer',mode='RUN',resolved=True,changed=False,
            state=self.st,watcher=self.w,market=self.m,ws_epoch=(1,1),
            control_mode=lambda:'RUN',urgent_hint=lambda:False,protective=False)
        args.update(extras)
        return b1_active_after_orders(**args)
    def test_quiet_and_fail_closed(self):
        self.assertTrue(self.policy()[0])
        for flag in ('changed','protective'):
            self.assertFalse(self.policy(**{flag:True})[0])
        for kw in ({'resolved':False},{'source':'private'}, {'mode':'PAUSE'},
                   {'control_mode':lambda:'PANIC'}, {'urgent_hint':lambda:True},
                   {'ws_epoch':(0,0)}):
            self.assertFalse(self.policy(**kw)[0])
        self.st['panic_orders']=[{'x':1}]
        self.assertFalse(self.policy()[0])
    def test_private_grid_virtual_preempt(self):
        arg=dict(watcher=self.w,market=self.m,ws_epoch=(1,1),
            control_mode=lambda:'RUN',original_mode='RUN',urgent_hint=lambda:False)
        self.assertFalse(b3_background_interrupted(**arg))
        for setter in (lambda:self.w.private_wake.set(),
                       lambda:self.w.events.put({'x':1}),
                       lambda:setattr(self.m,'_grid_wake_count',1),
                       lambda:setattr(self.m,'_virtual_wake_count',1),
                       lambda:self.m._virtual_crossed.update({'a':1}),
                       lambda:setattr(self.m,'connection_epoch',2)):
            self.setUp();arg['watcher']=self.w;arg['market']=self.m
            setter();self.assertTrue(b3_background_interrupted(**arg))
    def test_client_cancel_does_not_direct_get(self):
        # Minimal fake Unix server which accepts but intentionally never replies.
        with tempfile.TemporaryDirectory() as td:
            directory=pathlib.Path(td);directory.chmod(0o700)
            p=directory/'reader.sock';ready=threading.Event();stop=threading.Event()
            def server():
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                    sock.bind(str(p));sock.listen(1);ready.set()
                    conn,_=sock.accept()
                    with conn:
                        conn.recv(4096);stop.wait(.5)
            t=threading.Thread(target=server,daemon=True);t.start()
            self.assertTrue(ready.wait(1))
            start=time.monotonic()
            with self.assertRaises(PositionReaderPreempted):
                read_positions(str(p),'BTC-USDT-SWAP',account_uid='test',
                    background=True,cancel_if=lambda:time.monotonic()-start>=.09)
            self.assertLess(time.monotonic()-start,.45)
            stop.set();t.join(1)
    def test_engine_wiring_after_orders_only(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        f=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='reconcile_position')
        src=ast.unparse(f)
        self.assertLess(src.index('A.pending'),src.index('b1_active_context'))
        self.assertLess(src.index('verify_existing'),src.index('b1_active_context'))
        self.assertLess(src.index('b1_active_context'),src.index('A.positions'))
        self.assertIn('PositionReaderPreempted', (ROOT/'okx_bot.py').read_text())
        self.assertIn('b3_preempt_to_reconcile',(ROOT/'okx_bot.py').read_text())
        api=(ROOT/'okx_api.py').read_text()
        # The preempt exception must never be converted to a direct GET fallback.
        self.assertNotIn('except PositionReaderPreempted:',api)
        self.assertIn('Priority.BACKGROUND',(ROOT/'okx_position_reader_client.py').read_text())
        self.assertIn('background=background',(ROOT/'okx_api.py').read_text())
if __name__=='__main__': unittest.main()
