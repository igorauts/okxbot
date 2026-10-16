#!/usr/bin/env python3
"""v0.7.6 fail-degraded positions gate and bounded POST absence proof.

AST extraction: no credentials, no trade requests, no bot state touched.
"""
import ast
import fcntl
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

SRC=Path(__file__).with_name('okx_bot076.py').read_text()
TREE=ast.parse(SRC)

def load_symbol(name, env):
    node=next(n for n in TREE.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<exact v076 source>','exec'),env)
    return env[name]

class FakeWallClock:
    def __init__(self,real):
        self.wall=real
        self.sleep_calls=[]
    def time(self):return self.wall
    def monotonic(self):return time.monotonic()
    def sleep(self,n):self.sleep_calls.append(n);time.sleep(min(n,.001))

class GateTests(unittest.TestCase):
    def make_gate(self,tmp,clock=None):
        events=[]
        env={'Path':Path,'ENV':{'OKX_ACCOUNT_POSITIONS_GATE_PATH':str(Path(tmp)/'gate.json')},
             'os':os,'fcntl':fcntl,'json':json,'threading':threading,
             'time':clock or time, 'log':lambda event,**kw:events.append((event,kw)),
             'die':lambda s: (_ for _ in ()).throw(ValueError(s))}
        Gate=load_symbol('_AccountPositionsGate',env)
        return Gate(),events

    def test_corrupt_file_recovers_under_flock(self):
        with tempfile.TemporaryDirectory() as d:
            gate,events=self.make_gate(d)
            gate.path.write_bytes(b'{"schema":1,"calls":[1]}garbage')
            gate.take()
            data=json.loads(gate.path.read_text())
            self.assertEqual(len(data['calls']),1)
            self.assertIn('ACCOUNT_POSITIONS_GATE_RECOVERED',[x[0] for x in events])
            gate.take()
            self.assertEqual(len(json.loads(gate.path.read_text())['calls']),2)

    def test_clock_rollback_discards_future_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            wall=FakeWallClock(1000000.)
            gate,events=self.make_gate(d,wall)
            gate.path.write_text(json.dumps({'schema':1,'calls':[1000500.] * 8}))
            start=time.monotonic()
            gate.take()
            self.assertLess(time.monotonic()-start,.3)
            self.assertEqual(json.loads(gate.path.read_text())['calls'],[1000000.])
            self.assertIn('ACCOUNT_POSITIONS_GATE_RECOVERED',[x[0] for x in events])

    def test_gate_storage_unavailable_uses_bounded_local_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            gate,events=self.make_gate(d)
            # A directory as the gate 'file' produces an OSError.
            gate.path=Path(d)
            for _ in range(3):gate.take()
            self.assertEqual(len(gate._local_times),3)
            self.assertEqual(len([a for a,k in events if a=='ACCOUNT_POSITIONS_GATE_DEGRADED']),1)

    def test_permission_failure_does_not_trigger_unbounded_retries(self):
        with tempfile.TemporaryDirectory() as d:
            gate,_=self.make_gate(d)
            gate.path=Path(d)
            for _ in range(4):gate.take()
            start=time.monotonic();gate.take()
            self.assertGreaterEqual(time.monotonic()-start,1.6)

class AbsenceTests(unittest.TestCase):
    def test_40_minute_post_still_reconciles_without_repeat(self):
        now=[1740000000.]
        actions=[]
        class MockAPI:
            def server_time_ms(self):return int(now[0]*1000)
            def req(self,method,path,params):
                self.assert_dummy = True
                actions.append((method,path))
                return [{'instId':'ETH-USDT-SWAP','ordId':'7',
                         'clOrdId':'unrelated','cTime':str(int((now[0]-4000)*1000))}]
        logs=[]
        class Clock:
            @staticmethod
            def time():return now[0]
        env={'time':Clock,'A':MockAPI(),'INST':'ETH-USDT-SWAP',
             'ABSENCE_GRACE_MS':120000, 'ABSENCE_MAX_AGE_SEC':6*86400,
             'ABSENCE_CHECK_GAP_SEC':20,'ABSENCE_REQUIRED_CHECKS':3,
             'log':lambda *a,**k:logs.append((a,k)), 'save':lambda st:None,
             'ExchangeTemporary':RuntimeError,'ExchangeUnresolved':RuntimeError}
        fn=load_symbol('post_absence_confirmed',env)
        record={'placedAt':now[0]-2400,'expTimeMs':int((now[0]-2392)*1000),
                'clOrdId':'not-on-exchange'}
        self.assertFalse(fn(record,'buy',{'test':1}))
        now[0]+=21
        self.assertFalse(fn(record,'buy',{'test':1}))
        now[0]+=21
        self.assertTrue(fn(record,'buy',{'test':1}))
        self.assertEqual(len(actions),3)
        self.assertEqual(record['absenceChecks'],3)

    def test_old_or_future_post_never_auto_cleared(self):
        now=[1740000000.]
        env={'time':type('Clock',(),{'time':lambda:now[0]}),
             'ABSENCE_GRACE_MS':120000,'ABSENCE_MAX_AGE_SEC':6*86400,
             'ABSENCE_CHECK_GAP_SEC':20, 'ABSENCE_REQUIRED_CHECKS':3}
        fn=load_symbol('post_absence_confirmed',env)
        rec={'placedAt':now[0]-7*86400,'expTimeMs':int((now[0]-7*86400)*1000),
             'clOrdId':'x'}
        self.assertFalse(fn(rec,'buy'))
        rec['placedAt']=now[0]+90
        self.assertFalse(fn(rec,'buy'))
        self.assertFalse(fn(rec,'unknown'))

class UpgradeTests(unittest.TestCase):
    def test_audit_v075_to_v076_leaves_live_state_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'state.json'
            previous={'version':'0.7.5','instrument':'ETH-USDT-SWAP',
                      'cycles':{'2':{'cycleId':'2','state':'BUY_LIVE'}}}
            before=json.dumps(previous).encode();f.write_bytes(before)
            writes=[]
            env={'DATA':f,'INST':'ETH-USDT-SWAP','VER':'0.7.6',
                 'AUDIT':True,'LIVE':False,'shutil':__import__('shutil'),'os':os,
                 'json':json,'save':lambda st:writes.append(st.copy()),
                 'log':lambda *a,**k:None, 'migrate_cycle_records':lambda s:None,
                 'die':lambda s: (_ for _ in ()).throw(RuntimeError(s))}
            fn=load_symbol('ensure_state',env)
            result=fn()
            self.assertEqual(result['version'],'0.7.6')
            self.assertEqual(f.read_bytes(),before)
            self.assertEqual(writes,[])
            self.assertFalse((Path(d)/'bot04_state_before_v076.json').exists())

    def test_live_v075_to_v076_durable_backup(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'state.json'
            previous={'version':'0.7.5','instrument':'ETH-USDT-SWAP',
                      'cycles':{'1':{'cycleId':'one','state':'TP_WAIT'}},
                      'aggregate_exits':[{'clOrdId':'X','phase':'ACTIVE'}]}
            before=json.dumps(previous).encode();f.write_bytes(before)
            def saved(st):f.write_text(json.dumps(st))
            env={'DATA':f,'INST':'ETH-USDT-SWAP','VER':'0.7.6',
                 'AUDIT':False,'LIVE':True,'shutil':__import__('shutil'),'os':os,
                 'json':json,'save':saved,'log':lambda *a,**k:None,
                 'migrate_cycle_records':lambda s:None,
                 'die':lambda s: (_ for _ in ()).throw(RuntimeError(s))}
            st=load_symbol('ensure_state',env)()
            self.assertEqual(st['version'],'0.7.6')
            self.assertEqual(st['cycles'],previous['cycles'])
            self.assertEqual(st['aggregate_exits'],previous['aggregate_exits'])
            self.assertEqual((Path(d)/'bot04_state_before_v076.json').read_bytes(),before)

class NonRegressionTests(unittest.TestCase):
    def test_r2_core_trading_unchanged_except_market_completion_metadata(self):
        original=ast.parse(Path(__file__).with_name('r2_original.py').read_text())
        newer=ast.parse(SRC)
        left={n.name:n for n in original.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
        right={n.name:n for n in newer.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
        unchanged=('virtual_escape','place','cancel_lowest_specific','plan_buy_buffer_cleanup',
                   'missing_buy_levels_for_addition','track','reconcile_position',
                   'atomic_json','_durable_save','_flush_confirmed_facts')
        for name in unchanged:
            self.assertEqual(ast.dump(left[name],include_attributes=False),
                             ast.dump(right[name],include_attributes=False),name)
        # No updates whatsoever to actual market-exit decision logic.
        self.assertIn("c.get('virtualTpCrossed')",ast.unparse(right['virtual_escape']))
        self.assertIn("state=g['phase']",SRC)

if __name__=='__main__':unittest.main()
