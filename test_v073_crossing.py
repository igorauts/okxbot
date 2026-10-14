#!/usr/bin/env python3
"""Offline regression for fast WS peak -> retrace -> virtual MARKET escape."""
import ast
import copy
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

SOURCE=Path(__file__).with_name('okx_bot073.py')
TREE=ast.parse(SOURCE.read_text())

def isolated(*names):
    wanted=set(names)
    nodes=[copy.deepcopy(n) for n in TREE.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in wanted]
    assert {n.name for n in nodes}==wanted
    ns={'threading':threading, 'time':time, 'D':Decimal,
        'Decimal':Decimal, 'LIVE':True, 'ESCAPE_COALESCE_SEC':0.15}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(SOURCE),'exec'),ns)
    return ns

class CrossingTests(unittest.TestCase):
    def setUp(self):
        self.ns=isolated('PublicBidWatcher','persist_crossed_virtual_tps','virtual_escape')
        self.w=self.ns['PublicBidWatcher']()
        self.w.connected=True
        self.w._bid=Decimal('100')
        self.w._updated=time.monotonic()
        self.ns.update({
            'virtual_cycles': lambda st: [(key,c) for key,c in st['cycles'].items() if c['state']=='VIRTUAL_TP'],
            'save':lambda st: self.writes.append(1),
            'log':lambda *args,**kwargs: None,
            'remaining_tp':lambda c: Decimal(c['sz']),
            'tp_quantity_placeable':lambda q: q>0,
            'unresolved_aggregate':lambda st: False,
        })
        self.writes=[]
        self.c={'cycleId':'eth-parent','tpPx':'101','sz':'0.03','state':'VIRTUAL_TP'}
        self.st={'cycles':{'a':self.c}}

    def test_retrace_does_not_lose_crossing(self):
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        # One 100ms tick passed TP; the following one retraced below TP.
        with self.w._lock:
            self.w._capture_crossings(Decimal('102'))
            self.w._bid=Decimal('99')
            self.w._updated=time.monotonic()
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertIs(self.c.get('virtualTpCrossed'),True)
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal('99'),Decimal('0.03'),
                         [{'side':'sell'}],'RUN'),'NONE')
        self.assertIs(self.c.get('virtualTpCrossed'),True)
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal('99'),Decimal('0.03'),
                         [],'RUN'),'WAIT')
        self.assertEqual(self.st['escape_collection']['cycles'],['eth-parent'])

    def test_retraced_two_cycles_one_reduce_only_market(self):
        other={'cycleId':'eth-parent-2','tpPx':'103','sz':'0.02','state':'VIRTUAL_TP'}
        self.st['cycles']['b']=other
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        with self.w._lock:
            self.w._capture_crossings(Decimal('110'))
            self.w._bid=Decimal('90')
            self.w._updated=time.monotonic()
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertTrue(self.c['virtualTpCrossed'])
        self.assertTrue(other['virtualTpCrossed'])
        posts=[]
        class Exchange:
            def req(self,method,path,body=None,exp_time_ms=None):
                posts.append((method,path,body))
                return [{'sCode':'0','ordId':'MARKET1','clOrdId':body['clOrdId']}]
        self.ns.update({
            'A':Exchange(),
            'newid':lambda kind:'OX4E_TEST',
            'new_request_deadline_ms':lambda:12345678999999,
            'tp_records':lambda c:c.setdefault('tpOrders',[]),
            'INST':'ETH-USDT-SWAP',
        })
        # The exchange's 25 real SELLs have now exhausted after the peak.
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal('90'),Decimal('0.05'),[], 'RUN'),'WAIT')
        time.sleep(0.16)
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal('90'),Decimal('0.05'),[], 'RUN'),'ACTION')
        self.assertEqual(len(posts),1)
        method,path,body=posts[0]
        self.assertEqual((method,path),('POST','/api/v5/trade/order'))
        self.assertEqual(body['side'],'sell')
        self.assertEqual(body['ordType'],'market')
        self.assertIs(body['reduceOnly'],True)
        self.assertEqual(Decimal(body['sz']),Decimal('0.05'))  # contracts, not ETH
        self.assertEqual({r['cycleId'] for r in self.st['aggregate_exits'][0]['plan']},
                         {'eth-parent','eth-parent-2'})
        self.assertEqual(len({r['tpOrders'][-1]['ordId'] for r in (self.c,other)}),1)
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal('90'),Decimal('0.05'),[], 'RUN'),'NONE')
        self.assertEqual(len(posts),1)

    def test_no_old_signal_reused_after_real_tp(self):
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        with self.w._lock:self.w._capture_crossings(Decimal('105'))
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertTrue(self.c['virtualTpCrossed'])
        self.c['state']='TP_WAIT'
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertNotIn('virtualTpCrossed',self.c)
        self.c['state']='VIRTUAL_TP'
        self.w._bid=Decimal('95')
        self.w._updated=time.monotonic()
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertNotIn('virtualTpCrossed',self.c)

    def test_persisted_crossing_survives_reconnect(self):
        self.c['virtualTpCrossed']=True
        self.w.connected=False
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal('90'),Decimal('0.03'),
                         [],'RUN'),'WAIT')

    def test_current_bid_crosses_at_target_registration(self):
        self.w._bid=Decimal('106')
        self.w._updated=time.monotonic()
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.ns['persist_crossed_virtual_tps'](self.st,self.w)
        self.assertTrue(self.c.get('virtualTpCrossed'))

    def test_order_wake_not_rate_limited_by_price(self):
        cls=[n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='PublicBidWatcher'][0]
        code=ast.get_source_segment(SOURCE.read_text(),cls)
        self.assertIn('PRICE_WAKE_COOLDOWN',code)
        self.assertIn('_shared_wake.set()',code)

if __name__=='__main__':unittest.main()
