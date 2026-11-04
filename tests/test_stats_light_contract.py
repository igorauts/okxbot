"""Executable isolated contracts for StatsWorker light optimization.

Tests have no hardcoded version, no live credentials, no network and no writes.
"""
import ast
import contextlib
import decimal
import http.client
import json
import pathlib
import ssl
import threading
import time
import types
import unittest
from unittest import mock

BASE=pathlib.Path(__file__).resolve().parents[1]


def find_function(module, name):
    source=(BASE / module).read_text(encoding='utf-8')
    tree=ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==name:
            return node
    raise AssertionError('missing '+name)


class StatsBidTest(unittest.TestCase):
    def setUp(self):
        node=find_function('okx_bot.py','stats_capture_bid')
        class ExchangeUnresolved(Exception):pass
        self.ExchangeUnresolved=ExchangeUnresolved
        self.calls=0
        class A:
            def ticker(_self):
                self.calls+=1
                return decimal.Decimal('2.50')
        self.env={'A':A(),'D':decimal.Decimal,'ExchangeUnresolved':ExchangeUnresolved}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'probe','exec'),self.env)
        self.capture=self.env['stats_capture_bid']

    def test_fresh_ws_avoids_rest(self):
        market=types.SimpleNamespace(cached_bid=lambda:decimal.Decimal('1.23'))
        self.assertEqual(self.capture(market),decimal.Decimal('1.23'))
        self.assertEqual(self.calls,0)

    def test_stale_or_no_watcher_falls_back_to_rest(self):
        self.assertEqual(self.capture(types.SimpleNamespace(cached_bid=lambda:None)),decimal.Decimal('2.50'))
        self.assertEqual(self.capture(),decimal.Decimal('2.50'))
        self.assertEqual(self.calls,2)

    def test_invalid_ws_bid_does_not_publish(self):
        for invalid in ('0','-1','NaN','Infinity'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(self.ExchangeUnresolved):
                    self.capture(types.SimpleNamespace(cached_bid=lambda v=invalid:v))
        self.assertEqual(self.calls,0)

    def test_stats_only_has_independent_rest(self):
        root=ast.parse((BASE/'okx_bot.py').read_text(encoding='utf-8'))
        fn=next(n for n in root.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        branch=next(n for n in ast.walk(fn) if isinstance(n,ast.If) and isinstance(n.test,ast.Name) and n.test.id=='STATS_ONLY')
        self.assertIn('print_full_stats()',ast.unparse(branch))


class ReuseExpectedTest(unittest.TestCase):
    def test_reuses_exact_pinned_expected_object(self):
        node=find_function('okx_bot.py','stats_exchange_fills')
        expected={'o1':decimal.Decimal('2')}
        calls=[]
        class Store:
            def ensure_expected(_,mapping):
                calls.append(mapping)
                return ['ok']
        env={'FILL_STORE':Store(),'expected_filled_orders':lambda *args,**kwargs:self.fail('unexpected rescan')}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'probe','exec'),env)
        result=env['stats_exchange_fills']({'cycles':{}},expected=expected)
        self.assertEqual(result,['ok'])
        self.assertIs(calls[0],expected)

    def test_legacy_call_still_calculates_full_expected(self):
        node=find_function('okx_bot.py','stats_exchange_fills')
        calls=[]
        class Store:
            def ensure_expected(_,x):return x
        env={'FILL_STORE':Store(),'expected_filled_orders':lambda st,**k:calls.append(1) or {'x':1}}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'probe','exec'),env)
        self.assertEqual(env['stats_exchange_fills']({}),{'x':1})
        self.assertEqual(calls,[1])


class IdleGetConnectionTest(unittest.TestCase):
    def setUp(self):
        # Exercise the REAL build_api / req / _http / _close path, not a
        # hand-written approximation. All network calls are fake connections.
        import sys
        sys.path.insert(0,str(BASE))
        from okx_api import build_api
        class DummyRest:
            def __init__(self):self.begins=0;self.ends=0;self.throttles=0
            def begin(self,*args,**kw):self.begins+=1;return False
            def end(self,*args):self.ends+=1
            def throttle(self,*args):self.throttles+=1
        class DummyGate:
            def take(self,*args,**kw):pass
        class Temporary(RuntimeError):pass
        class Unresolved(RuntimeError):pass
        class Expired(RuntimeError):pass
        class Singleflight:
            def run(self,*args):return args[-1]()
        self.gate=DummyRest()
        api_type=build_api(ENV={'OKX_API_KEY':'TEST','OKX_API_SECRET':'TEST',
                    'OKX_API_PASSPHRASE':'TEST'},CFG={},COIN='BTC',
                    INST='BTC-USDT-SWAP',LIVE=True,VER='test',
                    D=decimal.Decimal,die=lambda x:(_ for _ in ()).throw(RuntimeError(x)),
                    CONTRACT_LOT=decimal.Decimal(1),CONTRACT_MIN=decimal.Decimal(1),
                    CONTRACT_VALUE=decimal.Decimal(1),PRICE_STEP=decimal.Decimal(1),
                    QTY=decimal.Decimal(1),STEP=decimal.Decimal(1),TP=decimal.Decimal(1),
                    ExchangeTemporary=Temporary,ExchangeUnresolved=Unresolved,
                    OrderExpiredBeforeSend=Expired,RateLimitedError=Temporary,
                    GateUnavailable=RuntimeError,REST_GATE=self.gate,
                    FILL_HISTORY_GATE=DummyGate(),ACCOUNT_POSITIONS_GATE=DummyGate(),
                    FILL_FETCH_FLIGHT=Singleflight(),_PERF_LOCAL=threading.local(),
                    _PERF_RECON_ENABLED=False,_flush_confirmed_facts=lambda:None,
                    latency_ms=lambda *a,**kw:None,fill_store=lambda:None)
        self.obj=api_type()
        self.obj._local.conn=None
        self.obj._local.conn_last_used=None
        self.new_count=0
        self.close_count=0
        self.attempts=[]
        parent=self
        class Connection:
            def __init__(self):self.closed=False
            def request(self,method,*args,**kwargs):
                parent.attempts.append(method)
            def getresponse(self):
                return types.SimpleNamespace(read=lambda:b'{"code":"0","data":[]}',
                                             status=200,will_close=False)
            def close(self):self.closed=True;parent.close_count+=1
        self.Connection=Connection
        def fake_connect(obj):
            conn=getattr(obj._local,'conn',None)
            if conn is None:
                parent.new_count+=1
                conn=Connection()
                obj._local.conn=conn
            return conn
        self.obj._connect=types.MethodType(fake_connect,self.obj)

    def test_idle_get_rotates_before_io(self):
        self.obj.req('GET','/api/v5/market/ticker')
        self.assertEqual(self.new_count,1)
        self.obj._local.conn_last_used=time.monotonic()-25
        self.obj.req('GET','/api/v5/market/ticker')
        self.assertEqual((self.close_count,self.new_count),(1,2))
        self.assertEqual(self.gate.begins,self.gate.ends)

    def test_fresh_get_reuses_connection(self):
        self.obj.req('GET','/api/v5/market/ticker')
        self.obj.req('GET','/api/v5/market/ticker')
        self.assertEqual((self.new_count,self.close_count),(1,0))

    def test_idle_post_keeps_original_transport_semantics(self):
        self.obj.req('GET','/api/v5/market/ticker')
        self.obj._local.conn_last_used=time.monotonic()-25
        self.obj.req('POST','/api/v5/trade/order',body={'clOrdId':'TEST'})
        self.assertEqual((self.new_count,self.close_count),(1,0))
        self.assertEqual(self.attempts,['GET','POST'])

    def test_network_error_closes_and_get_retries_same_gate(self):
        parent=self
        class Broken(self.Connection):
            def request(self,*args,**kw):
                parent.attempts.append('GET-error')
                raise OSError('broken')
        self.obj._local.conn=Broken()
        self.obj.req('GET','/api/v5/market/ticker')
        self.assertEqual(self.close_count,1)
        self.assertEqual(self.new_count,1)
        self.assertEqual(self.gate.begins,self.gate.ends)
        self.assertEqual(self.gate.begins,2)

    def test_network_error_post_never_retries(self):
        parent=self
        class Broken(self.Connection):
            def request(self,*args,**kw):
                parent.attempts.append('POST-error')
                raise OSError('broken')
        self.obj._local.conn=Broken()
        with self.assertRaisesRegex(RuntimeError,'UNKNOWN POST OUTCOME'):
            self.obj.req('POST','/api/v5/trade/order',body={'clOrdId':'TEST'})
        self.assertEqual(parent.attempts,['POST-error'])
        self.assertEqual(self.gate.begins,self.gate.ends)
        self.assertEqual(self.gate.begins,1)

    def test_idempotent_offline_ast_core_transport(self):
        # Explicit pinning of the three untouched review-guarded methods.
        ref=json.loads((BASE/'tests/reference_method_ast.json').read_text())['API']
        tree=ast.parse((BASE/'okx_api.py').read_text())
        factory=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_api')
        cls=next(n for n in factory.body if isinstance(n,ast.ClassDef) and n.name=='API')
        import hashlib
        import copy
        for node in cls.body:
            if isinstance(node,ast.FunctionDef) and node.name in ('_connect','_close','_http'):
                n=copy.deepcopy(node)
                if (n.body and isinstance(n.body[0],ast.Expr)
                    and isinstance(n.body[0].value,ast.Constant)
                    and isinstance(n.body[0].value.value,str)):
                    n.body.pop(0)
                digest=hashlib.sha256(ast.dump(n,include_attributes=False).encode()).hexdigest()
                self.assertEqual(digest,ref[node.name],node.name)


class WiringAndSafetyTest(unittest.TestCase):
    def test_live_worker_has_market_and_stats_only_does_not(self):
        # The former string equality constrained the *old* call signature;
        # verify actual AST-level safety invariants after the incremental
        # source cache was added, without weakening STATS_ONLY separation.
        text=(BASE/'okx_bot.py').read_text(encoding='utf-8')
        tree=ast.parse(text)
        classes=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='StatsWorker']
        self.assertEqual(len(classes),1)
        worker=classes[0]
        init=[n for n in worker.body if isinstance(n,ast.FunctionDef) and n.name=='__init__']
        run=[n for n in worker.body if isinstance(n,ast.FunctionDef) and n.name=='run']
        self.assertEqual((len(init),len(run)),(1,1))
        def calls(node,name):
            return [c for c in ast.walk(node) if isinstance(c,ast.Call)
                    and isinstance(c.func,ast.Name) and c.func.id==name]
        def is_self_attr(value,attr):
            return (isinstance(value,ast.Attribute)
                    and isinstance(value.value,ast.Name) and value.value.id=='self'
                    and value.attr==attr)
        # The running worker must use the EXISTING market and its own cache.
        constructors=calls(init[0],'StatsSourceCache')
        self.assertEqual(len(constructors),1)
        self.assertEqual([ast.unparse(a) for a in constructors[0].args],
                         ['FILL_DB','ARCHIVE_DB','INST'])
        self.assertEqual(sum(isinstance(n,ast.Assign)
                             and len(n.targets)==1
                             and is_self_attr(n.targets[0],'stats_cache')
                             and n.value is constructors[0]
                             for n in ast.walk(init[0])),1)
        calls_run=calls(run[0],'print_full_stats')
        self.assertEqual(len(calls_run),1)
        finance_kw=[k.value for k in calls_run[0].keywords if k.arg=='finance_cache']
        self.assertEqual(len(finance_kw),1)
        self.assertIsInstance(finance_kw[0],ast.Attribute)
        self.assertEqual(finance_kw[0].attr,'finance_cache')
        args={k.arg:k.value for k in calls_run[0].keywords}
        self.assertTrue(is_self_attr(args.get('market'),'market'))
        self.assertTrue(is_self_attr(args.get('stats_cache'),'stats_cache'))
        self.assertEqual(len(calls_run[0].args),1)
        self.assertTrue(is_self_attr(calls_run[0].args[0],'pnl'))
        # Independent one-shot STATS_ONLY must NOT use a worker, cache, or
        # market watcher. It retains the original FULL financial path.
        mains=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main']
        self.assertEqual(len(mains),1)
        only=[n for n in ast.walk(mains[0]) if isinstance(n,ast.If)
              and isinstance(n.test,ast.Name) and n.test.id=='STATS_ONLY']
        self.assertEqual(len(only),1)
        solo=calls(only[0],'print_full_stats')
        self.assertEqual(len(solo),1)
        self.assertEqual(len(solo[0].args),0)
        self.assertEqual(len(solo[0].keywords),0)
        self.assertEqual(len(calls(only[0],'StatsWorker')),0)
        self.assertEqual(len(calls(only[0],'StatsSourceCache')),0)
        # Live worker construction still happens exactly once.
        self.assertEqual(text.count('stats_worker=StatsWorker(pnl,market);stats_worker.start()'),1)
        fn=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='print_full_stats']
        self.assertEqual(len(fn),1)
        kwonly=fn[0].args.kwonlyargs
        defaults=fn[0].args.kw_defaults
        named={k.arg:d for k,d in zip(kwonly,defaults)}
        self.assertTrue({'require_complete','market','stats_cache'} <= set(named))
        for key in ('market','stats_cache'):
            self.assertIsInstance(named[key],ast.Constant)
            self.assertIsNone(named[key].value)
        self.assertIn("journal.record('stats_detail'",text)

    def test_telemetry_allowed(self):
        text=(BASE/'okx_latency.py').read_text(encoding='utf-8')
        self.assertIn("'tp_recon_phase','stats_stage','stats_detail'",text)
        self.assertIn("'fills_missing_recovery','fills_sqlite_second'",text)
        self.assertIn("'expected','missing'",text)

    def test_trading_http_retry_law_unchanged(self):
        text=(BASE/'okx_api.py').read_text(encoding='utf-8')
        self.assertIn("attempts=4 if method=='GET' else 1",text)
        self.assertIn("UNKNOWN {method} OUTCOME",text)


if __name__=='__main__':unittest.main()
