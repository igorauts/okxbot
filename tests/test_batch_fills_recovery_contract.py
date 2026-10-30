"""Fail-closed quantity/identity and bulk recovery offline tests."""
import ast
import decimal
import json
from pathlib import Path
import types
import unittest

BASE=Path(__file__).resolve().parents[1]
D=decimal.Decimal

class Unresolved(RuntimeError):pass

class Fixture:
    def __init__(self):
        src=ast.parse((BASE/'okx_bot.py').read_text())
        cls=next(x for x in src.body if isinstance(x,ast.ClassDef) and x.name=='FillStore')
        method=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='ensure_expected')
        self.sqlite={}
        self.pages=[]
        self.batch_calls=[]
        self.target_calls=[]
        self.insert_calls=[]
        self.err=None
        fixture=self
        class A:
            def req(_,method,route,params,background=False):
                assert method=='GET' and route=='/api/v5/trade/fills-history'
                assert params['instId']=='TEST-USDT-SWAP' and params['instType']=='SWAP'
                assert background is True and params['limit']=='100'
                fixture.batch_calls.append(dict(params))
                if fixture.err:raise fixture.err
                return fixture.pages.pop(0) if fixture.pages else []
            def fills_by_order(_,oid,expected_qty=None):
                fixture.target_calls.append((oid,expected_qty))
                # A mock exchange exact-order response, inserted via durable path.
                if oid in fixture.remote_exact:
                    fixture.insert(fixture.remote_exact[oid])
        self.a=A()
        self.remote_exact={}
        env={'A':self.a,'Decimal':D,'D':D,'INST':'TEST-USDT-SWAP',
             'ExchangeUnresolved':Unresolved,'time':__import__('time')}
        sandbox_ast=ast.Module(body=[ast.ClassDef(name='F',bases=[],keywords=[],
              body=[method],decorator_list=[])],type_ignores=[])
        ast.fix_missing_locations(sandbox_ast)
        exec(compile(sandbox_ast,'test-extracted','exec'),env)
        self.obj=env['F']()
        self.obj.perf=types.SimpleNamespace(last=None)
        self.obj.get_rows=self.get_rows
        self.obj.insert=self.insert
    def get_rows(self,ids):
        return [dict(f) for oid in ids for f in self.sqlite.get(oid,[])]
    def insert(self,rows):
        if not rows:return 0
        self.insert_calls.append(len(rows))
        for f in rows:
            oid=f.get('ordId')
            bill=f.get('billId')
            if (f.get('instId')!='TEST-USDT-SWAP' or not oid or not bill
                or f.get('side') not in ('buy','sell') or f.get('execType') not in ('M','T')
                or f.get('feeCcy')!='USDT' or f.get('fee') in (None,'')
                or D(f.get('fillPx') or '0')<=0 or D(f.get('fillSz') or '0')<=0):
                raise ValueError('invalid exchange fill')
            dest=self.sqlite.setdefault(oid,[])
            prior=next((r for r in dest if r['billId']==bill),None)
            if prior and any(str(prior.get(k))!=str(f.get(k)) for k in
                       ('fillPx','fillSz','fee','feeCcy','side','execType','tradeId')):
                raise ValueError('Conflicting exchange fill')
            if not prior:dest.append(dict(f))
        return len(rows)

def fill(oid, suffix='1',qty='1',inst='TEST-USDT-SWAP'):
    return {'ordId':oid,'billId':oid+'_'+suffix,'instId':inst,'side':'buy',
            'execType':'T','feeCcy':'USDT','fee':'-0.001',
            'fillPx':'1','fillSz':qty,'tradeId':oid+'_'+suffix}

class Contract(unittest.TestCase):
    def test_zero_expected_no_rest(self):
        f=Fixture();self.assertEqual(f.obj.ensure_expected({}),[])
        self.assertEqual((f.batch_calls,f.target_calls),([],[]))
    def test_complete_sqlite_no_rest(self):
        f=Fixture();f.insert([fill('x')]);self.assertEqual(len(f.obj.ensure_expected({'x':D(1)})),1)
        self.assertEqual((f.batch_calls,f.target_calls),([],[]))
    def test_20_missing_one_bulk_page(self):
        f=Fixture();ids=['x'+str(i) for i in range(20)]
        f.pages=[[fill(i) for i in ids]]
        got=f.obj.ensure_expected({i:D('1') for i in ids})
        self.assertEqual((len(got),len(f.batch_calls),len(f.target_calls)),(20,1,0))
        self.assertEqual(f.obj.perf.last['bulk_resolved'],20)
    def test_small_missing_keeps_exact_order_behavior(self):
        f=Fixture();f.remote_exact={s:[fill(s)] for s in ('a','b','c')}
        self.assertEqual(len(f.obj.ensure_expected({s:D(1) for s in f.remote_exact})),3)
        self.assertEqual((len(f.batch_calls),len(f.target_calls)),(0,3))
    def test_small_missing_original_sqlite_read_sequence(self):
        f=Fixture()
        f.insert([fill('a')])
        f.remote_exact={'b':[fill('b')]}
        original_get=f.get_rows
        calls=[]
        def tracked_get(ids):
            calls.append(tuple(ids))
            return original_get(ids)
        f.obj.get_rows=tracked_get
        rows=f.obj.ensure_expected({'a':D(1),'b':D(1)})
        self.assertEqual(len(rows),2)
        self.assertEqual(calls,[('a','b'),('b',)])
        self.assertEqual(f.target_calls,[('b',None)])

    def test_original_exact_order_api_signature_without_expected_qty(self):
        f=Fixture()
        calls=[]
        def legacy_fetch(oid):
            calls.append(oid)
            f.insert([fill(oid)])
        f.a.fills_by_order=legacy_fetch
        ids=['a','b','c']
        result=f.obj.ensure_expected({oid:D(1) for oid in ids})
        self.assertEqual(len(result),3)
        self.assertEqual(calls,ids)
        self.assertEqual(f.batch_calls,[])

    def test_empty_bulk_falls_back_on_all_missing(self):
        f=Fixture();ids=['x'+str(i) for i in range(5)]
        f.remote_exact={i:[fill(i)] for i in ids}
        self.assertEqual(len(f.obj.ensure_expected({i:D(1) for i in ids})),5)
        self.assertEqual((len(f.batch_calls),len(f.target_calls)),(1,5))
    def test_partial_bulk_only_fallback_remaining(self):
        f=Fixture();ids=['x'+str(i) for i in range(5)]
        f.pages=[[fill('x0'),fill('x1')]]
        f.remote_exact={i:[fill(i)] for i in ids[2:]}
        self.assertEqual(len(f.obj.ensure_expected({i:D(1) for i in ids})),5)
        self.assertEqual((len(f.batch_calls),len(f.target_calls)),(1,3))
    def test_incomplete_after_exact_does_not_fabricate(self):
        f=Fixture();ids=['x'+str(i) for i in range(5)]
        f.pages=[[fill(i) for i in ids[:-1]]]
        got=f.obj.ensure_expected({i:D(1) for i in ids})
        # ensure_expected returns real incomplete rows only, never invented qty.
        # trim_verified_fills_for_snapshot still refuses an incomplete report.
        self.assertEqual(len(got),4)
        self.assertEqual(len(f.target_calls),1)
    def test_bulk_foreign_instrument_fails_closed(self):
        f=Fixture();f.pages=[[fill('x0',inst='OTHER-USDT-SWAP')]]
        with self.assertRaises(Unresolved):f.obj.ensure_expected({str(i):D(1) for i in range(5)})
    def test_malformed_matched_row_fails_closed(self):
        f=Fixture();r=fill('0');r['fee']='';f.pages=[[r]]
        with self.assertRaises(ValueError):f.obj.ensure_expected({str(i):D(1) for i in range(5)})
    def test_conflicting_duplicate_fill_fails_closed(self):
        f=Fixture();f.insert([fill('0',qty='0.5')])
        f.pages=[[fill('0',qty='1')]]
        with self.assertRaisesRegex(ValueError,'Conflicting'):f.obj.ensure_expected({str(i):D(1) for i in range(5)})
    def test_transport_error_propagates(self):
        f=Fixture();f.err=Unresolved('GET unavailable')
        with self.assertRaises(Unresolved):f.obj.ensure_expected({str(i):D(1) for i in range(5)})
    def test_100_page_pagination_by_bill_id(self):
        f=Fixture();ids=['x'+str(i) for i in range(5)]
        page=[fill('unrelated'+str(i)) for i in range(99)]+[fill(ids[0])]
        f.pages=[page,[fill(i) for i in ids[1:]]]
        out=f.obj.ensure_expected({i:D(1) for i in ids})
        self.assertEqual(len(out),5)
        self.assertEqual(len(f.batch_calls),2)
        self.assertEqual(f.batch_calls[1]['after'],page[-1]['billId'])
    def test_no_finite_missing_credit_without_fill(self):
        f=Fixture();ids=['x'+str(i) for i in range(6)]
        f.pages=[[fill(ids[0],qty='0.5')]]
        got=f.obj.ensure_expected({i:D(1) for i in ids})
        self.assertEqual(sum(D(x['fillSz']) for x in got),D('0.5'))
        self.assertEqual(len(f.target_calls),6)
    def test_retains_only_pinned_order_ids(self):
        f=Fixture();ids=['x'+str(i) for i in range(5)]
        f.pages=[[fill(ids[0]),fill('unrelated')]]
        f.obj.ensure_expected({i:D(1) for i in ids})
        self.assertNotIn('unrelated',f.sqlite)
    def test_snapshot_trim_rejects_missing_and_crossing_fills(self):
        src=ast.parse((BASE/'okx_bot.py').read_text())
        fn=next(n for n in src.body if isinstance(n,ast.FunctionDef)
                and n.name=='trim_verified_fills_for_snapshot')
        env={'D':D,'Decimal':D}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'verified-trim','exec'),env)
        trim=env['trim_verified_fills_for_snapshot']
        self.assertIsNone(trim([],{'a':D('1')}))
        self.assertIsNone(trim([fill('a',qty='1.5')],{'a':D('1')}))
        self.assertEqual(len(trim([fill('a',qty='0.5'),fill('a',suffix='2',qty='0.5')],{'a':D('1')})),2)
        with self.assertRaises(ValueError):
            trim([fill('a'),fill('a')],{'a':D(1)})
        with self.assertRaises(ValueError):
            trim([fill('foreign')],{'a':D(1)})

    def test_latest_fills_do_not_invent_aggregate_pnl(self):
        src=ast.parse((BASE/'okx_bot.py').read_text())
        fn=next(n for n in src.body if isinstance(n,ast.FunctionDef)
                and n.name=='trim_verified_fills_for_snapshot')
        env={'D':D,'Decimal':D}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'verified-trim','exec'),env)
        # A shared aggregated MARKET order may have multiple real fills.
        rows=[fill('agg',suffix='0',qty='2'),fill('agg',suffix='1',qty='3')]
        self.assertEqual(len(env['trim_verified_fills_for_snapshot'](rows,{'agg':D(5)})),2)
        self.assertIsNone(env['trim_verified_fills_for_snapshot'](rows,{'agg':D(4)}))

    def test_old_api_methods_and_trading_logic_untouched(self):
        code=(BASE/'okx_bot.py').read_text()
        tree=ast.parse(code)
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='FillStore')
        fns={x.name for x in cls.body if isinstance(x,ast.FunctionDef)}
        self.assertIn('ensure_expected',fns)
        api=(BASE/'okx_api.py').read_text()
        self.assertIn("attempts=4 if method=='GET' else 1",api)
        self.assertIn('UNKNOWN {method} OUTCOME',api)
        self.assertIn('A.fills_by_order(oid)',code)

if __name__=='__main__':unittest.main()
