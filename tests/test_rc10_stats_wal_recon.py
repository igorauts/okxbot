"""Offline WAL concurrency, reconciliation trace and timestamp overlap tests.

No trading process, live account or network requests.
"""
import ast
import contextlib
import json
import os
import sqlite3
import threading
import time
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]


def extract_node(name):
    tree=ast.parse((ROOT/'okx_bot.py').read_text())
    return next(n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name==name)


def wal_store(path):
    cls=extract_node('FillStore')
    ns={'threading':threading,'contextlib':contextlib,'sqlite3':sqlite3,
        'os':os,'json':json,'FILL_DB':path,'INST':'ZEC-USDT-SWAP',
        'D':lambda n:Decimal(str(n or '0')),'Decimal':Decimal,'time':time,
        'ExchangeUnresolved':RuntimeError}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(ROOT/'okx_bot.py'),'exec'),ns)
    store=ns['FillStore']()
    with store._managed_conn():pass  # database/DDL bootstrap happens before live stats
    return store,ns


def fill(oid):
    return {'ordId':oid,'billId':'bill-'+oid,'fillSz':'1','fillPx':'100',
            'fee':'-0.01','feeCcy':'USDT','side':'buy','execType':'M',
            'instId':'ZEC-USDT-SWAP','tradeId':'trade-'+oid}


class WALReadIsolation(unittest.TestCase):
    def test_read_only_sqlite_does_not_serialize_fill_writer(self):
        with tempfile.TemporaryDirectory() as td:
            store,ns=wal_store(Path(td)/'fills.sqlite')
            store.insert([fill('a')])
            blocked=threading.Event();release=threading.Event();errors=[];out=[]
            original_json_loads=json.loads
            def slow_decode(data):
                if threading.current_thread().name=='stats-reader':
                    blocked.set()
                    if not release.wait(4):raise RuntimeError('test read timed out')
                return original_json_loads(data)
            # Only the isolated AST class sees the slow decoder.
            ns['json']=SimpleNamespace(loads=slow_decode,dumps=json.dumps)
            def read():
                try:out.extend(store.get_rows(['a','b']))
                except Exception as exc:errors.append(exc)
            worker=threading.Thread(target=read,name='stats-reader')
            worker.start()
            try:
                self.assertTrue(blocked.wait(3))
                start=time.monotonic()
                self.assertEqual(store.insert([fill('b')]),1)
                self.assertLess(time.monotonic()-start,1.0,'stats reader held writer lock')
            finally:
                release.set();worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual([r['ordId'] for r in out],['a'])
            self.assertEqual(set(r['ordId'] for r in store.get_rows(['a','b'])),{'a','b'})

    def test_multi_batch_is_a_single_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            store,ns=wal_store(Path(td)/'fills.sqlite')
            store.insert([fill('a')])
            blocked=threading.Event();release=threading.Event();errors=[];out=[]
            original_json_loads=json.loads
            def slow_decode(data):
                if threading.current_thread().name=='stats-reader':
                    blocked.set()
                    if not release.wait(4):raise RuntimeError('test read timed out')
                return original_json_loads(data)
            ns['json']=SimpleNamespace(loads=slow_decode,dumps=json.dumps)
            keys=['a']+['missing-'+str(i) for i in range(249)]+['b']
            def read():
                try:out.extend(store.get_rows(keys))
                except Exception as exc:errors.append(exc)
            worker=threading.Thread(target=read,name='stats-reader')
            worker.start()
            try:
                self.assertTrue(blocked.wait(3))
                self.assertEqual(store.insert([fill('b')]),1)
            finally:
                release.set();worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual([r['ordId'] for r in out],['a'])

    def test_missing_read_database_fails_not_invents_empty_history(self):
        with tempfile.TemporaryDirectory() as td:
            store,ns=wal_store(Path(td)/'fills.sqlite')
            (Path(td)/'fills.sqlite').unlink()
            with self.assertRaises(sqlite3.OperationalError):store.get_rows(['ordid'])


class ReconTrace(unittest.TestCase):
    def test_only_trace_present_before_phase_is_attached(self):
        ns={'time':time,'threading':threading}
        exec(compile(ast.Module(body=[extract_node('_ReconProfiler')],type_ignores=[]),
                     str(ROOT/'okx_bot.py'),'exec'),ns)
        traces=[];now={'id':''}
        p=ns['_ReconProfiler'](trace_supplier=lambda:now['id'],
            trace_sink=lambda *a,**k:traces.append((a,k)))
        def first():
            now['id']='a'*24
            return 'ok'
        self.assertEqual(p.run('pending',first),'ok')
        self.assertEqual(traces,[],'must not backdate an in-flight HTTP call')
        self.assertEqual(p.run('positions',lambda:42),42)
        self.assertEqual(len(traces),1)
        self.assertEqual(traces[0][0][0],'tp_recon_phase')
        self.assertEqual(traces[0][1]['step'],'positions')
        self.assertEqual(traces[0][1]['trace_id'],'a'*24)
        self.assertEqual(p.counts['pending'],1)
        self.assertEqual(p.counts['positions'],1)

    def test_telemetry_failure_cannot_change_business_result_or_exception(self):
        ns={'time':time,'threading':threading}
        exec(compile(ast.Module(body=[extract_node('_ReconProfiler')],type_ignores=[]),
                     str(ROOT/'okx_bot.py'),'exec'),ns)
        bad=lambda *a,**k: (_ for _ in ()).throw(OSError('journal down'))
        p=ns['_ReconProfiler'](trace_supplier=lambda:'b'*24,trace_sink=bad)
        self.assertEqual(p.run('positions',lambda:[]),[])
        with self.assertRaisesRegex(ValueError,'exchange error'):
            p.run('pending',lambda: (_ for _ in ()).throw(ValueError('exchange error')))
        self.assertEqual(p.counts['pending'],1)


class OverlapReport(unittest.TestCase):
    def test_stats_overlap_and_disjoint_are_categorized_not_explained(self):
        import sys
        sys.path.insert(0,str(ROOT))
        from okx_tp_trace_report import summarize
        tr='a'*24
        xs=[{'phase':'stats_stage','step':'fills','ms':100,'start_unix_ms':1000,'end_unix_ms':1100},
            {'phase':'tp_to_intent','trace_id':tr,'ms':90,'start_unix_ms':1010,'end_unix_ms':1090},
            {'phase':'tp_to_intent','trace_id':tr,'ms':20,'start_unix_ms':1300,'end_unix_ms':1320},
            {'phase':'tp_recon_phase','trace_id':tr,'step':'positions','ms':10}]
        report=summarize(xs)
        overlap=report['tp_stats_overlap']
        self.assertEqual(overlap['with_stats']['n'],1)
        self.assertEqual(overlap['without_stats']['n'],1)
        self.assertEqual(overlap['with_stats']['p50_ms'],90)
        self.assertEqual(overlap['without_stats']['p50_ms'],20)
        self.assertEqual(report['reconciliation_steps']['positions']['n'],1)
        self.assertEqual(report['stats_steps']['fills']['n'],1)
        self.assertEqual(overlap['interpretation'],'overlap_only_not_causality')

    def test_invalid_timestamps_do_not_enter_overlap_denominator(self):
        import sys
        sys.path.insert(0,str(ROOT))
        from okx_tp_trace_report import summarize
        rows=[{'phase':'tp_to_intent','trace_id':'a'*24,'ms':40},
              {'phase':'stats_stage','step':'fills','ms':100,'start_unix_ms':200,'end_unix_ms':100}]
        result=summarize(rows)
        self.assertEqual(result['tp_stats_overlap']['eligible_tp_intervals'],0)
        self.assertEqual(result['tp_stats_overlap']['stats_intervals'],0)

if __name__=='__main__':unittest.main()
