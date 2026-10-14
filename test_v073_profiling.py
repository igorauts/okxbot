#!/usr/bin/env python3
import ast, copy, time, unittest, threading, json, tempfile
from pathlib import Path
from decimal import Decimal
SOURCE=Path(__file__).with_name('okx_bot073.py')
def pick(name):
    nodes=ast.parse(SOURCE.read_text()).body
    selected=[copy.deepcopy(n) for n in nodes if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name==name]
    assert len(selected)==1
    return compile(ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[])),str(SOURCE),'exec')
class Tests(unittest.TestCase):
    def test_profile_passes_mismatches_and_saves(self):
        class MockProfiler:
            def __init__(self):self.passes=0;self.mismatches=0;self.phases=[];self.error=None
            def run(self,stage,fn,*args,**kw):self.phases.append(stage);return fn(*args,**kw)
            def finish(self,error_type=None):self.error=error_type
        class API:
            n=0
            def pending(self):return []
            def positions(self):
                self.n+=1
                return [{'pos':'2' if self.n==1 else '1'}]
        api=API(); st={'cycles':{'a':{'filledBuy':'1','filledTP':'0'}}}
        profiles=[]
        def factory():p=MockProfiler();profiles.append(p);return p
        class T:pass
        ns=dict(time=time,sys=__import__('sys'),A=api,Decimal=Decimal,D=Decimal,
            _PERF_RECON_ENABLED=True,_PERF_LOCAL=T(),_ReconProfiler=factory,
            register_cancel_attached=lambda *_:None,track=lambda *_:True,
            verify_existing=lambda *_:None,panic_executions=lambda *_:Decimal(0),
            log=lambda *a,**kw:None,ExchangeUnresolved=RuntimeError)
        exec(pick('reconcile_position'),ns)
        pending,ok,pos=ns['reconcile_position'](st,timeout=5,pause=.001)
        self.assertEqual(pos,Decimal(1))
        self.assertTrue(ok)
        self.assertEqual(profiles[0].passes,2)
        self.assertEqual(profiles[0].mismatches,1)
        self.assertEqual(profiles[0].phases.count('positions'),2)
        self.assertEqual(profiles[0].phases.count('retry_sleep'),1)
        self.assertIsNone(profiles[0].error)
        self.assertIsNone(ns['_PERF_LOCAL'].current)
    def test_disabled_profiling_does_not_require_profiler_classes(self):
        class API:
            def pending(self):return []
            def positions(self):return [{'pos':'0'}]
        ns=dict(time=time,sys=__import__('sys'),A=API(),Decimal=Decimal,D=Decimal,
            register_cancel_attached=lambda *_:None,track=lambda *_:True,
            verify_existing=lambda *_:None,panic_executions=lambda *_:Decimal(0),
            log=lambda *a,**kw:None,ExchangeUnresolved=RuntimeError)
        exec(pick('reconcile_position'),ns)
        st={'cycles':{}}
        self.assertEqual(ns['reconcile_position'](st,timeout=0)[2],Decimal(0))
if __name__=='__main__':unittest.main()
