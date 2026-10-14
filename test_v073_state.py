#!/usr/bin/env python3
"""Offline simulated exchange and durable state for aggregate SELL lifecycle."""
import ast,copy,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
SOURCE=Path(__file__).parent/'okx_bot073.py'
TREE=ast.parse(SOURCE.read_text())
def compile_selected(*names):
    selected=[copy.deepcopy(n) for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {x.name for x in selected}==set(names)
    return compile(ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[])),'source','exec')

class MockExchange:
    def __init__(self):self.calls=[];self.observed=None;self.reply=[dict(sCode='0',ordId='MARKET01')]
    def req(self,method,url,body=None,**kwargs):
        self.calls.append((method,url,body))
        return copy.deepcopy(self.reply)
    def order(self,oid='',cid=''):return copy.deepcopy(self.observed)

class TestState(unittest.TestCase):
    def setUp(self):
        from datetime import datetime,timezone
        import time
        self.api=MockExchange();self.saved=[];self.events=[]
        self.ns={'Decimal':Decimal,'D':Decimal,'A':self.api,'time':time,
            'INST':'ETH-USDT-SWAP','ESCAPE_COALESCE_SEC':0.15,
            'save':lambda s:self.saved.append(copy.deepcopy(s)),
            'log':lambda *a,**kw:self.events.append((a,kw)),
            'newid':lambda prefix:'OID'+prefix+'007','new_request_deadline_ms':lambda:9999999999999,
            'margin_rejection':lambda s:False,
            'trip_account_soft_stop':lambda *a:None,
            'OrderExpiredBeforeSend':type('OrderExpiredBeforeSend',(Exception,),{}),
            'ExchangeUnresolved':type('ExchangeUnresolved',(RuntimeError,),{}),
            'die':lambda s: (_ for _ in ()).throw(ValueError(s)),
            'tp_quantity_placeable':lambda x:x>0,
        }
        exec(compile_selected('tp_records','cycle_sell_fill','remaining_tp','active_tp_records',
             'virtual_cycles','unresolved_aggregate','reconcile_aggregate','virtual_escape'),self.ns)
        self.st={'cycles':{f'px{i}':{
            'cycleId':f'c{i}','state':'VIRTUAL_TP','filledBuy':'0.003',
            'filledTP':'0','tpPx':'2510','buyPx':str(2500+i),'tpOrders':[]}
            for i in range(3)}}
    def test_market_one_order_many_cycle_links_and_idempotent(self):
        act=self.ns['virtual_escape'];reconcile=self.ns['reconcile_aggregate']
        self.assertEqual(act(self.st,Decimal('2520'),Decimal('0.009'),[], 'RUN'),'WAIT')
        self.assertEqual(len(self.api.calls),0)
        self.st['escape_collection']['startedAt']-=1
        self.assertEqual(act(self.st,Decimal('2520'),Decimal('0.009'),[], 'RUN'),'ACTION')
        self.assertEqual(len(self.api.calls),1)
        self.assertEqual(self.api.calls[0][2]['ordType'],'market')
        self.assertEqual(self.api.calls[0][2]['reduceOnly'],True)
        self.assertEqual(self.api.calls[0][2]['sz'],'0.009')
        self.assertEqual(len(self.st['aggregate_exits']),1)
        self.assertTrue(self.ns['unresolved_aggregate'](self.st))
        self.assertEqual(act(self.st,Decimal('2520'),Decimal('0.009'),[], 'RUN'),'WAIT')
        self.assertEqual(len(self.api.calls),1,'Pending MARKET must never be duplicated')
        self.api.observed=dict(instId='ETH-USDT-SWAP',side='sell',clOrdId='OIDE007',ordId='MARKET01',
                  reduceOnly='true',ordType='market',sz='0.009',accFillSz='0.009',state='filled')
        self.assertTrue(reconcile(self.st))
        self.assertFalse(self.ns['unresolved_aggregate'](self.st))
        self.assertEqual([x['tpOrders'][0]['filled'] for x in self.st['cycles'].values()],['0.003']*3)
        self.assertEqual([x['tpOrders'][0]['phase'] for x in self.st['cycles'].values()],['FILLED']*3)
        self.assertTrue(reconcile(self.st))
        self.assertEqual(len(self.api.calls),1)
    def test_partial_canceled_is_not_assumed_fully_filled(self):
        self.st['escape_collection']={'startedAt':0,'cycles':['c0','c1','c2']}
        self.ns['virtual_escape'](self.st,Decimal(2500),Decimal('.009'),[], 'RUN')
        self.api.observed=dict(instId='ETH-USDT-SWAP',side='sell',clOrdId='OIDE007',ordId='MARKET01',
                  reduceOnly='true',ordType='market',sz='0.009',accFillSz='0.004',state='canceled')
        self.assertTrue(self.ns['reconcile_aggregate'](self.st))
        self.assertEqual([x['tpOrders'][0]['filled'] for x in self.st['cycles'].values()],
                         ['0.003','0.001','0.000'])
        self.assertEqual([x['tpOrders'][0]['phase'] for x in self.st['cycles'].values()],
                         ['CANCELED','CANCELED','CANCELED'])
    def test_unknown_market_result_never_resends(self):
        self.api.reply=[]
        self.st['escape_collection']={'startedAt':0,'cycles':['c0','c1','c2']}
        with self.assertRaises(self.ns['ExchangeUnresolved']):
            self.ns['virtual_escape'](self.st,Decimal(2600),Decimal('0.009'),[], 'RUN')
        self.assertTrue(self.ns['unresolved_aggregate'](self.st))
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal(2600),Decimal('0.009'),[], 'RUN'),'WAIT')
        self.assertEqual(len(self.api.calls),1)
    def test_rejection_releases_virtuals_without_double_market(self):
        self.api.reply=[dict(sCode='51008',clOrdId='OIDE007',ordId='')]
        self.st['escape_collection']={'startedAt':0,'cycles':['c0','c1','c2']}
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal(2600),Decimal('.009'),[], 'RUN'),'WAIT')
        self.assertEqual([c['state'] for c in self.st['cycles'].values()],['VIRTUAL_TP']*3)
        self.assertFalse(self.ns['unresolved_aggregate'](self.st))
    def test_rearm_does_not_use_marketable_limit_after_tp_cross(self):
        ns={'D':Decimal,'REAL_TP_TARGET':25,'TP_ACTIONS_PER_PASS':5,'time':__import__('time')}
        ns['TPBufferScheduler']=type('IdleTPBuffer',(),{'observe':lambda self,a,t:None})
        ns['REAL_TP_MAX']=55
        calls=[]
        ns['coverage_audit']=lambda *a:(0,Decimal(0),Decimal('0.009'))
        ns['virtual_cycles']=lambda st:list(st['cycles'].items())
        ns['remaining_tp']=lambda c:Decimal('0.003')
        ns['tp_quantity_placeable']=lambda q:True
        ns['real_tp_active']=lambda st:[]
        ns['place_virtual_tp']=lambda st,c:calls.append(c['cycleId'])
        ns['cancel_tp']=lambda *a:None
        exec(compile_selected('maintain_virtual_tp'),ns)
        self.assertEqual(ns['maintain_virtual_tp'](self.st,[],Decimal('.009'),Decimal('2511')),'NONE')
        self.assertEqual(calls,[])
        self.assertEqual(ns['maintain_virtual_tp'](self.st,[],Decimal('.009'),Decimal('2500')),'RESTORE')
        self.assertEqual(len(calls),3)

    def test_real_tp_blocks_market(self):
        self.assertEqual(self.ns['virtual_escape'](self.st,Decimal(2600),Decimal('.009'),
            [dict(side='sell',ordId='EXISTING')],'RUN'),'NONE')
        self.assertEqual(len(self.api.calls),0)

if __name__=='__main__':unittest.main()
