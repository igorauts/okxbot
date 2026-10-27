"""RC9: public WS event wake and best-effort trace, no import of LIVE bot."""
import ast
import hashlib
import json
import sys
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_tp_telemetry import crossing_trace_id,intent_timing
from okx_api import position_trace_id
from okx_tp_trace_report import summarize


def watcher_class():
    src=(ROOT/'okx_bot.py').read_text()
    tree=ast.parse(src)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)
             and n.name=='PublicBidWatcher')
    ns={'threading':threading,'time':time,'D':Decimal,
        'INST':'ZEC-USDT-SWAP','PRICE_WAKE_COOLDOWN':0.5,
        'LATENCY':SimpleNamespace(record=lambda *a,**kw:None),
        'websocket':None,'json':json,'grid_top':lambda bid:10}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(ROOT/'okx_bot.py'),'exec'),ns)
    return ns


class CrossWake(unittest.TestCase):
    def test_capture_only_first_cross_and_no_reset_on_retrace(self):
        w=watcher_class()['PublicBidWatcher']()
        w.set_virtual_targets({'a':Decimal('100'),'b':Decimal('101')})
        self.assertTrue(w._capture_crossings(Decimal('100.2')))
        first=w._virtual_crossed['a']
        self.assertFalse(w._capture_crossings(Decimal('100.4')))
        self.assertFalse(w._capture_crossings(Decimal('99')))
        self.assertTrue(w._capture_crossings(Decimal('101.2')))
        self.assertEqual(w._virtual_crossed['a'],first)
        self.assertFalse(w._capture_crossings(Decimal('101.3')))

    def test_fresh_ws_bid_when_targets_published_wakes_main_thread(self):
        wake=threading.Event()
        w=watcher_class()['PublicBidWatcher'](shared_wake=wake)
        w.connected=True
        w._bid=Decimal('103')
        w._updated=time.monotonic()
        w.set_virtual_targets({'a':Decimal('100')})
        self.assertTrue(wake.is_set())
        wake.clear()
        w.set_virtual_targets({'a':Decimal('100')})
        self.assertFalse(wake.is_set())  # no spurious repeated wake

    def test_ws_ticker_wakes_even_without_grid_step_or_cooldown(self):
        ns=watcher_class()
        calls=[]
        class FakeSocket:
            def __init__(self):self.count=0
            def send(self,data):pass
            def settimeout(self,n):pass
            def recv(self):
                self.count+=1
                if self.count==1:return json.dumps({'event':'subscribe'})
                if self.count==2:return json.dumps({'arg':{'channel':'tickers'},'data':[
                    {'instId':'ZEC-USDT-SWAP','bidPx':'100'}]})
                raise KeyboardInterrupt  # end infinite listener, no retry/sleep
            def close(self):pass
        ns['websocket']=SimpleNamespace(create_connection=lambda *a,**kw:FakeSocket(),
                                        WebSocketTimeoutException=TimeoutError)
        ns['LATENCY']=SimpleNamespace(record=lambda *a,**kw:calls.append((a,kw)))
        wake=threading.Event()
        watcher=ns['PublicBidWatcher'](shared_wake=wake)
        watcher.set_virtual_targets({'one':Decimal('100')})
        watcher._grid_ref=10
        watcher._last_price_wake=time.monotonic()  # normal grid wake forbidden
        with self.assertRaises(KeyboardInterrupt):watcher.run()
        self.assertTrue(wake.is_set())
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0][0],'tp_ws_cross')
        self.assertEqual(len(calls[0][1]['trace_id']),24)

    def test_only_wake_main_loop_never_order_from_ws(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PublicBidWatcher')
        ws_src=ast.unparse(cls)
        self.assertIn('self._shared_wake.set()',ws_src)
        for forbidden in ('/api/v5/trade/order','A.req(', 'virtual_escape(', 'save(st)'):
            self.assertNotIn(forbidden,ws_src)


class TraceTests(unittest.TestCase):
    def test_trace_matches_ws_to_intent_and_is_not_raw_cycle_id(self):
        stamps={'cycle-a':1234.123456,'cycle-b':1234.2}
        result=crossing_trace_id(stamps)
        self.assertEqual(len(result),24)
        self.assertNotIn('cycle',result)
        expected=intent_timing([
            {'cycleId':'cycle-a','virtualTpCrossedAt':1234.123456},
            {'cycleId':'cycle-b','virtualTpCrossedAt':1234.2}],
            {'startedAt':1234.25,'initialCycleIds':['cycle-a','cycle-b']},1234.43)
        self.assertEqual(result,expected['trace_id'])
        self.assertEqual(expected['cross_source'],'ws_bid')

    def test_unknown_timestamp_no_fabricated_trace(self):
        self.assertEqual(crossing_trace_id({'a':None,'b':float('nan')}),'')
        result=intent_timing([{'cycleId':'a'}],{'startedAt':1000},1001)
        self.assertNotIn('trace_id',result)
        self.assertNotIn('cross_to_collect_ms',result)

    def test_position_trace_supplier_is_best_effort(self):
        self.assertEqual(position_trace_id(SimpleNamespace(_reader_trace_id=lambda:'a'*24)),'a'*24)
        self.assertEqual(position_trace_id(SimpleNamespace(_reader_trace_id=lambda:'secret value')),'')
        self.assertEqual(position_trace_id(SimpleNamespace(_reader_trace_id=lambda:1/0)),'')
        self.assertEqual(position_trace_id(SimpleNamespace()),'')

    def test_offline_trace_report_never_fabricates_missing_phases(self):
        trace='a'*24
        result=summarize([
            {'trace_id':trace,'phase':'tp_to_intent','ms':240.0},
            {'trace_id':trace,'phase':'http','route':'/api/v5/account/positions','ms':34},
            {'trace_id':trace,'phase':'tp_to_intent','ms':float('nan')},
            {'trace_id':trace,'phase':'tp_cross_to_collect','ms':100.0},
            {'trace_id':'BAD ID','phase':'http','ms':10},
        ],[
            {'event':'AGG_MARKET_INTENT','clOrdId':'order1','trace_id':trace},
            {'event':'AGG_MARKET_ACK','clOrdId':'order1','trace_id':trace},
            {'event':'AGG_MARKET_INTENT','clOrdId':'unknown2','trace_id':trace},
        ])
        self.assertEqual(result['traces'],1)
        self.assertEqual(result['traces_with_position_http'],1)
        self.assertEqual(result['linked_intent_ack_count'],1)
        self.assertEqual(result['phase_stats']['tp_to_intent']['n'],1)
        self.assertEqual(result['phase_stats']['tp_collect_to_intent']['n'],0)

    def test_join_marker_in_reader_and_direct_api_http(self):
        source=(ROOT/'okx_api.py').read_text()
        self.assertIn('trace_id=position_trace_id(self) or secrets.token_hex(12)',source)
        self.assertIn("'/api/v5/trade/orders-pending'",source)
        self.assertIn('trace_id=tp_trace',source)
        engine=(ROOT/'okx_bot.py').read_text()
        self.assertIn('A._reader_trace_id=lambda: active_tp_trace_id(st,market)',engine)
        self.assertIn("log('AGG_MARKET_INTENT'",engine)
        self.assertIn('trace_id=tp_timing.get(\'trace_id\')',engine)

if __name__=='__main__':unittest.main()
