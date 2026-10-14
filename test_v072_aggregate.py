#!/usr/bin/env python3
"""Offline tests for shared exchange market order and per-cycle accounting."""
import ast, copy, json, unittest, contextlib
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
SOURCE=Path(__file__).parent/'okx_bot072.py'
TREE=ast.parse(SOURCE.read_text())
def load(*names):
    nodes=[copy.deepcopy(x) for x in TREE.body if isinstance(x,(ast.FunctionDef,ast.ClassDef)) and x.name in names]
    assert {n.name for n in nodes}==set(names)
    ns={'Decimal':Decimal,'D':Decimal,'REAL_TP_TARGET':25,'contextlib':contextlib,'datetime':datetime,'timezone':timezone}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'source','exec'),ns)
    return ns

class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.ns=load('allocated_market_fills','accumulate_expected_sell','expected_cycle_sells',
            'stats_pct','full_stats_snapshot','PnLLedger','_last_24h_profit')
        self.plan=[{'cycleId':'A','sz':'0.003'}, {'cycleId':'B','sz':'0.002'}, {'cycleId':'C','sz':'0.004'}]
        self.rows=[dict(ordId='M1',tradeId='t1',billId='b1',fillSz='0.004',fillPx='2510',fee='-0.0041',feeCcy='USDT',side='sell',execType='T',fillTime='100'),
                   dict(ordId='M1',tradeId='t2',billId='b2',fillSz='0.005',fillPx='2512',fee='-0.0053',feeCcy='USDT',side='sell',execType='T',fillTime='101')]
    def test_exact_allocation_conserves_money(self):
        split=[self.ns['allocated_market_fills'](self.rows,self.plan,c) for c in ('A','B','C')]
        self.assertEqual([sum((Decimal(f['fillSz']) for f in r),Decimal(0)) for r in split],
                         [Decimal('0.003'),Decimal('0.002'),Decimal('0.004')])
        self.assertEqual(sum((Decimal(f['fee']) for r in split for f in r),Decimal(0)),Decimal('-0.0094'))
        self.assertEqual(sum((Decimal(f['fillPx'])*Decimal(f['fillSz']) for r in split for f in r),Decimal(0)),
                         Decimal('0.004')*2510+Decimal('0.005')*2512)
        self.assertEqual([[f['tradeId'] for f in r] for r in split],[['t1'],['t1','t2'],['t2']])
    def test_rejects_overfill_and_duplicate_trade(self):
        rows=copy.deepcopy(self.rows)
        rows[1]['fillSz']='0.006'
        with self.assertRaises(ValueError):self.ns['allocated_market_fills'](rows,self.plan,'A')
        rows[1]['fillSz']='0.005';rows[1]['tradeId']='t1'
        with self.assertRaises(ValueError):self.ns['allocated_market_fills'](rows,self.plan,'A')
    def test_cancel_partial_fifo_allocation(self):
        rows=[self.rows[0]]
        self.assertEqual(sum(Decimal(f['fillSz']) for f in self.ns['allocated_market_fills'](rows,self.plan,'A')),Decimal('0.003'))
        self.assertEqual(sum(Decimal(f['fillSz']) for f in self.ns['allocated_market_fills'](rows,self.plan,'B')),Decimal('0.001'))
        self.assertEqual(self.ns['allocated_market_fills'](rows,self.plan,'C'),[])
    def test_expected_sell_aggregation(self):
        d={}
        for sz,cid in [('0.003','A'),('0.002','B'),('0.004','C')]:
            self.ns['accumulate_expected_sell'](d,dict(ordId='M1',source='aggregate_exit',filled=sz))
        self.assertEqual(d,{'M1':Decimal('0.009')})
        with self.assertRaises(ValueError):
            self.ns['accumulate_expected_sell'](d,dict(ordId='M1',source='attached',filled='0.01'))
    def test_parent_ledger_exactly_matches_exchange_net(self):
        p=[dict(cycleId='A',sz='0.003'),dict(cycleId='B',sz='0.006')]
        fills=self.rows
        def buy(oid,q,px,fee):
            return [dict(ordId=oid,tradeId='trade'+oid,fillSz=q,fillPx=px,fee=fee,
                 feeCcy='USDT',side='buy',execType='M')]
        def cycle(cid,oid,q,px):
            return dict(cycleId=cid,ordId=oid,filledBuy=q,filledTP=q,
                tpPx=str(Decimal(px)+2),buyPx=px,state='CLOSED',
                tpOrders=[dict(ordId='M1',filled=q,source='aggregate_exit',aggregatePlan=p)])
        ledger=object.__new__(self.ns['PnLLedger']);ledger.contract_value=Decimal(1)
        a=ledger.compute(cycle('A','B1','.003','2500'),buy('B1','.003','2500','-.001'),fills)
        b=ledger.compute(cycle('B','B2','.006','2490'),buy('B2','.006','2490','-.002'),fills)
        net=Decimal(a['net'])+Decimal(b['net'])
        expected=sum((Decimal(f['fillPx'])*Decimal(f['fillSz'])+Decimal(f['fee']) for f in fills),Decimal(0))
        expected-=Decimal('.003')*2500+Decimal('.006')*2490+Decimal('.003')
        self.assertEqual(net.quantize(Decimal("0.00000001")),expected.quantize(Decimal("0.00000001")))
        self.assertEqual(Decimal(a['fees'])+Decimal(b['fees']),Decimal('-.0124'))
        self.assertEqual(a['sell_id'],b['sell_id'])
        self.assertNotEqual(a['buy_id'],b['buy_id'])

    def test_last_24h_shared_market_no_double_count(self):
        """One SELL ordId closes two parents; count only each parent's shares."""
        plan=[dict(cycleId='A',sz='0.003'),dict(cycleId='B',sz='0.006')]
        a=dict(cycleId='A',ordId='B1',state='CLOSED',filledBuy='0.003',filledTP='0.003',
               tpOrders=[dict(ordId='M1',filled='0.003',source='aggregate_exit',aggregatePlan=plan)])
        b=dict(cycleId='B',ordId='B2',state='CLOSED',filledBuy='0.006',filledTP='0.006',
               tpOrders=[dict(ordId='M1',filled='0.006',source='aggregate_exit',aggregatePlan=plan)])
        buy_a=dict(ordId='B1',tradeId='ba',billId='ba',fillSz='0.003',fillPx='2500',
                   fee='-0.001',feeCcy='USDT',side='buy',execType='M',ts='1700000000000')
        buy_b=dict(ordId='B2',tradeId='bb',billId='bb',fillSz='0.006',fillPx='2490',
                   fee='-0.002',feeCcy='USDT',side='buy',execType='M',ts='1700000000000')
        sells=[dict(r,ts='1700000001000') for r in self.rows]
        # market has two fills; one spans the allocation boundary A->B.
        expected=sum((Decimal(f['fillPx'])*Decimal(f['fillSz'])+Decimal(f['fee'])
                      for f in sells),Decimal(0))
        expected-=Decimal('0.003')*Decimal('2500')+Decimal('0.006')*Decimal('2490')+Decimal('0.003')
        self.ns['all_stat_cycles']=lambda st:[a,b]
        got=self.ns['_last_24h_profit']({'cycles':{'A':a,'B':b}},
                                        [buy_a,buy_b]+sells,Decimal(1),1700000002000)
        self.assertEqual(got,expected)

    def test_last_24h_single_attached_unchanged(self):
        c=dict(cycleId='C',ordId='B3',state='CLOSED',filledBuy='0.01',filledTP='0.01',
               tpOrders=[dict(ordId='S3',filled='0.01',source='attached')])
        b=dict(ordId='B3',billId='b3',tradeId='b3',fillSz='0.01',fillPx='2500',fee='-.002',ts='1700000000000')
        s=dict(ordId='S3',billId='s3',tradeId='s3',fillSz='0.01',fillPx='2510',fee='-.003',ts='1700000001000')
        self.ns['all_stat_cycles']=lambda st:[c]
        self.assertEqual(self.ns['_last_24h_profit']({},[b,s],Decimal(1),1700000002000),Decimal('.095'))

    def test_full_stats_no_double_count(self):
        # One shared MARKET closes 2 unrelated BUY cycles. 2 trade-level fills.
        buy_a=dict(ordId='B1',tradeId='buy1',billId='buy1',fillSz='0.003',fillPx='2500',fee='-0.001',feeCcy='USDT',side='buy',execType='M')
        buy_b=dict(ordId='B2',tradeId='buy2',billId='buy2',fillSz='0.006',fillPx='2490',fee='-0.002',feeCcy='USDT',side='buy',execType='M')
        a=dict(cycleId='A',ordId='B1',state='CLOSED',filledBuy='0.003',filledTP='0.003',buyPx='2500',tpPx='2502',tpOrders=[dict(ordId='M1',filled='0.003',source='aggregate_exit',aggregatePlan=self.plan)])
        b=dict(cycleId='B',ordId='B2',state='CLOSED',filledBuy='0.006',filledTP='0.006',buyPx='2490',tpPx='2492',tpOrders=[dict(ordId='M1',filled='0.006',source='aggregate_exit',aggregatePlan=[dict(cycleId='A',sz='0.003'),dict(cycleId='B',sz='0.006')])])
        # Use same plan for both and updated quantity in original fixture.
        plan=[dict(cycleId='A',sz='0.003'),dict(cycleId='B',sz='0.006')]
        a['tpOrders'][0]['aggregatePlan']=plan
        rows=[buy_a,buy_b]+self.rows
        for f in rows:f.setdefault('ts','1')
        ns=self.ns
        ns['all_stat_cycles']=lambda state:[a,b]
        ns['FILL_STORE']=None
        result=ns['full_stats_snapshot']({'cycles':{'a':a,'b':b},'completed':2},rows,Decimal('2512'),Decimal('1'))
        self.assertTrue(result['complete'], result.get('missing'))
        realized=sum((Decimal(f['fillPx'])*Decimal(f['fillSz'])*(1 if f['side']=='sell' else -1)
                       +Decimal(f['fee']) for f in rows),Decimal(0))
        self.assertEqual(result['grid_gain'],realized)
        self.assertEqual(result['total_fees'],Decimal('-0.0124'))
        self.assertEqual(result['volume'],sum((Decimal(f['fillPx'])*Decimal(f['fillSz']) for f in rows),Decimal(0)))

if __name__=='__main__':unittest.main()
