"""Telegram-only regression: SQLite FD lifetime, MARKET retries, durable command pre-ACK.
Temporary SQLite and mock services only; no live IO and no exchange API.
"""
import ast
from contextlib import closing
import gc
import json
import os
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import okx_telegram_delivery as delivery

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import okx_telegram_events as events
import okx_telegram_views as views
import okx_telegram_followups as followups

TREE=ast.parse((ROOT/'okx_telegram_monitor.py').read_text())

def extract(*names):
    nodes=[n for n in TREE.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in names]
    assert len(nodes)==len(names)
    return compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'<telegram-test>','exec')


def fd_count():
    return len(os.listdir('/proc/self/fd'))

class SQLiteLifetime(unittest.TestCase):
    def test_followups_1000_gc_disabled_no_fd_growth(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.sqlite'
            with closing(sqlite3.connect(p)) as db:
                db.execute('CREATE TABLE test (data TEXT)')
                db.execute('INSERT INTO test VALUES (?)',('"ok"',))
                db.commit()
            before=fd_count();enabled=gc.isenabled();gc.disable()
            try:
                for _ in range(1000):
                    self.assertEqual(followups._read_db(p,'SELECT data FROM test',()),['ok'])
                self.assertLessEqual(fd_count(),before+1)
            finally:
                if enabled: gc.enable()

    def test_market_1000_null_accounting_no_fd_growth(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);state=root/'state';state.mkdir()
            (root/'config.json').write_text(json.dumps({
                'instrument':'SOPH-USDT-SWAP','contract_value':'1','maker_fee_at_setup':'0.001'}))
            (state/'bot_state.json').write_text(json.dumps({'aggregate_exits':[
                {'ordId':'sell','phase':'FILLED','plan':['buy'],'sz':'1'}],
                'cycles':{},'archived_cycles':[]}))
            with closing(sqlite3.connect(state/'pnl.sqlite')) as db:
                db.execute('CREATE TABLE accounted (id TEXT, data TEXT)')
                db.execute('INSERT INTO accounted VALUES (?,?)',('x',json.dumps({
                    'sell_ids':['sell'],'kind':'CLOSED','qty_contracts':'1',
                    'buy_id':'buy','net':'0.1','planned_gross':'0.1',
                    'qty_base':'1','buy_px':'0.5','sell_px':'0.6'})))
                db.commit()
            with closing(sqlite3.connect(state/'fills.sqlite')) as db:
                db.execute('CREATE TABLE fills (order_id TEXT, data TEXT)')
                db.commit()
            item={'terminal':{'state':'FILLED','ordId':'sell'},'terminal_at':0}
            st={'market_events':{'soph/x':item}}
            read_calls=[]
            real=views.market_ledger
            def counted(*args,**kwargs):
                read_calls.append(1)
                return real(*args,**kwargs)
            before=fd_count();enabled=gc.isenabled();gc.disable()
            try:
                with patch.object(views,'market_ledger',side_effect=counted), \
                     patch.object(events,'one',return_value=True), \
                     patch.object(events,'print',create=True):
                    for i in range(1000):
                        now=1000000000+901*i
                        item['terminal_at']=now-100
                        item['_tg_accounting_next_retry']=0
                        with patch.object(events.time,'time',return_value=now):
                            events.market_process(None,st,{'soph':root})
                self.assertEqual(len(read_calls),1000)
                self.assertLessEqual(fd_count(),before+2)
                self.assertEqual(item['_tg_accounting_last_reason'],'buy_fill_quantity_not_confirmed')
                self.assertIn('soph/x',st['market_events'])
            finally:
                if enabled:gc.enable()

    def test_market_retry_and_24h_alarm(self):
        item={'terminal':{'state':'FILLED','ordId':'sell'},'terminal_at':100.0}
        st={'market_events':{'soph/x':item}}
        alerts=[]
        with patch.object(events,'market_message',return_value=None) as message, \
             patch.object(events,'one',side_effect=lambda _api,_st,key,_msg:alerts.append(key)), \
             patch.object(events,'print',create=True), \
             patch.object(events.time,'time',return_value=200.0):
            for _ in range(100):events.market_process(None,st,{'soph':Path('/fake')})
            self.assertEqual(message.call_count,1)
            self.assertEqual(item['_tg_accounting_next_retry'],260.0)
        with patch.object(events,'one',side_effect=lambda _api,_st,key,_msg:alerts.append(key)), \
             patch.object(events.time,'time',return_value=86501.0):
            events.market_process(None,st,{'soph':Path('/fake')})
        self.assertNotIn('soph/x',st['market_events'])
        self.assertTrue(any('accounting_stale' in x for x in alerts))

class CommandSafety(unittest.TestCase):
    def bridge(self):
        class Telegram:
            def __init__(self,*args):pass
            def _close_http(self):pass
        ns={'_cmd_queue':queue,'_cmd_threading':threading,'Telegram':Telegram,
            '_dispatch_command':lambda *args:None,'save_state':lambda st:None,
            'time':time,'print':lambda *args,**kwargs:None}
        exec(extract('_CommandBridge'),ns)
        return ns['_CommandBridge']('x','y',500),ns

    def test_100_failed_preacks_never_send(self):
        # CRITICAL ISOLATION: real commit_completion imports delivery.enqueue.
        # Every persistent operation stays in a disposable private directory,
        # including the third-attempt dead-letter path. Never touch LIVE state.
        with tempfile.TemporaryDirectory(prefix='telegram-ack-offline-') as d:
            path=Path(d)/'command_state.json'
            fallback=Path(d)/'default_must_remain_sandboxed.json'
            b,ns=self.bridge();st={'updates_offset':500,'schema':1}
            ns['_dispatch_command']=lambda *args: self.fail('sent without durable ACK')
            ns['save_state']=lambda st: (_ for _ in ()).throw(OSError(24,'too many files'))
            recorded=[]
            real=delivery.enqueue
            def isolated_enqueue(state,key,message):
                self.assertEqual(key,'telegram/command_failed/500')
                recorded.append(key)
                return real(state,key,message,path=path)
            with patch.object(delivery,'STATE_PATH',fallback), \
                 patch.object(delivery,'enqueue',side_effect=isolated_enqueue):
                for _ in range(100):
                    with self.assertRaises(OSError):b.prepare_dispatch(st,500)
                    self.assertFalse(b.permit.get_nowait())
                    b.done.put((500,False))
                    with self.assertRaises(OSError):b.commit_completion(st)
                    self.assertIsNone(b.ack.get_nowait())
            self.assertEqual(st['updates_offset'],500)
            self.assertGreater(len(recorded),0)
            self.assertTrue(path.is_file())
            self.assertFalse(fallback.exists())
            self.assertEqual(json.loads(path.read_text())['schema'],1)

    def test_preack_success_then_failed_dispatch_not_replayed(self):
        b,ns=self.bridge();st={'updates_offset':500,'schema':1};saves=[]
        ns['save_state']=lambda st:saves.append(st['updates_offset'])
        self.assertTrue(b.prepare_dispatch(st,500))
        self.assertTrue(b.permit.get_nowait())
        b.done.put((500,False))
        self.assertTrue(b.commit_completion(st))
        self.assertEqual(b.ack.get_nowait(),501)
        self.assertEqual(saves,[501])
        self.assertFalse(b.prepare_dispatch(st,500))
        self.assertFalse(b.permit.get_nowait())

    def test_zero_discovery_does_not_erase_known_bots(self):
        ns={'BOT_DIRS':{'soph':'/tmp/soph'},'SERVICES':{'soph':'okx-grid-soph'},
            '_DISCOVERY_LAST':0.0,'time':time,'discover_bots':lambda:{},
            '_confirmed_archived':lambda *args:False,'print':lambda *args,**kwargs:None}
        exec(extract('refresh_bots'),ns)
        ns['refresh_bots'](force=True)
        self.assertEqual(ns['BOT_DIRS'],{'soph':'/tmp/soph'})
        self.assertEqual(ns['SERVICES'],{'soph':'okx-grid-soph'})

    def test_fd_alarm_once_per_episode(self):
        import resource
        class R:
            RLIMIT_NOFILE=resource.RLIMIT_NOFILE
            RLIM_INFINITY=resource.RLIM_INFINITY
            @staticmethod
            def getrlimit(_):return 100,100
        alerts=[];st={}
        ns={'os':os,'time':time,'one':lambda _api,_st,key,_msg:alerts.append(key),
            'print':lambda *args,**kwargs:None}
        exec(extract('_fd_health_check'),ns)
        with patch.dict(sys.modules,{'resource':R}),patch.object(os,'listdir',return_value=[0]*70):
            ns['_fd_health_check'](None,st,100)
            ns['_fd_health_check'](None,st,200)
        self.assertEqual(len(alerts),1)
        with patch.dict(sys.modules,{'resource':R}),patch.object(os,'listdir',return_value=[0]*20):
            ns['_fd_health_check'](None,st,300)
        with patch.dict(sys.modules,{'resource':R}),patch.object(os,'listdir',return_value=[0]*70):
            ns['_fd_health_check'](None,st,400)
        self.assertEqual(len(alerts),2)

if __name__=='__main__':unittest.main()
