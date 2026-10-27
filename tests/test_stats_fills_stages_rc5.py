"""Offline AST-isolated stats fills timing contracts, no SQLite or REST."""
import ast
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


def make_store():
    source=Path(__file__).resolve().parents[1]/'okx_bot.py'
    tree=ast.parse(source.read_text())
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='FillStore')
    methods=[node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name in ('__init__','ensure_expected')]
    assert len(methods)==2
    isolated=ast.ClassDef(name='FillStore',bases=[],keywords=[],body=methods,decorator_list=[])
    ns={'time':time,'threading':threading,'Decimal':Decimal,'D':Decimal}
    mod=ast.fix_missing_locations(ast.Module(body=[isolated],type_ignores=[]))
    exec(compile(mod,str(source),'exec'),ns)
    return ns['FillStore'],ns


class StatsStageInstrumentation(unittest.TestCase):
    def test_complete_rows_same_values_and_no_network(self):
        typ,ns=make_store();store=typ();calls=[]
        data=[{'ordId':'a','fillSz':'2'},{'ordId':'b','fillSz':'1'}]
        store.get_rows=lambda orders: (calls.append(('sqlite',tuple(orders))) or list(data))
        ns['A']=SimpleNamespace(fills_by_order=lambda oid:calls.append(('unexpected_net',oid)))
        returned=store.ensure_expected({'a':Decimal(2),'b':Decimal(1)})
        self.assertEqual(returned,data)
        self.assertEqual(calls,[('sqlite',('a','b'))])
        self.assertEqual(store.perf.last['missing'],0)
        self.assertEqual(store.perf.last['expected'],2)
        for field in ('sqlite_first','qty_index','missing_recovery','sqlite_second'):
            self.assertGreaterEqual(store.perf.last[field],0.0)

    def test_missing_only_one_verified_ordid_and_sqlite_refresh(self):
        typ,ns=make_store();store=typ();calls=[]
        db=[{'ordId':'a','fillSz':'1'}]
        def get_rows(orders):
            calls.append(('sqlite',tuple(orders)))
            return list(db)
        def fetch(oid):
            calls.append(('GET_missing_order',oid))
            db.append({'ordId':'b','fillSz':'3'})
        store.get_rows=get_rows
        ns['A']=SimpleNamespace(fills_by_order=fetch)
        result=store.ensure_expected({'a':Decimal(1),'b':Decimal(3)})
        self.assertEqual(result,db)
        self.assertEqual(calls,[('sqlite',('a','b')),('GET_missing_order','b'),('sqlite',('a','b'))])
        self.assertEqual(store.perf.last['missing'],1)
        self.assertEqual(store.perf.last['expected'],2)

    def test_empty_expected_no_fetch_and_no_stale_metric(self):
        typ,ns=make_store();store=typ();store.perf.last={'missing':42}
        store.get_rows=lambda ids:self.fail('get_rows must not be invoked')
        self.assertEqual(store.ensure_expected({}),[])
        self.assertEqual(store.perf.last['missing'],0)
        self.assertEqual(store.perf.last['expected'],0)

if __name__=='__main__':unittest.main()
