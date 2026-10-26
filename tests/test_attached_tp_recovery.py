#!/usr/bin/env python3
"""Failure-driven attached TP recovery (no OKX network, no live writes)."""
import ast
import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]


def defs(*names):
    tree=ast.parse((ROOT/'okx_bot.py').read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names),(names,[x.name for x in nodes])
    ns={'D':Decimal,'INST':'DOOD-USDT-SWAP','Decimal':Decimal,
        'REAL_TP_TARGET':2,'RateLimitedError':type('RateLimitedError',(Exception,),{}),
        'ExchangeTemporary':type('ExchangeTemporary',(Exception,),{}),
        'ExchangeUnresolved':type('ExchangeUnresolved',(Exception,),{}),
        'time':SimpleNamespace(time=lambda:1000,monotonic=lambda:1000)}
    def die(message):raise RuntimeError(message)
    ns['die']=die
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(ROOT/'okx_bot.py'),'exec'),ns)
    return ns


def sample(code='51006', reason='Order price is not within the price limit',filled='1'):
    c={'state':'TP_WAIT','cycleId':'dood-cycle','ordId':'BUY-1','clOrdId':'BUY-CLIENT',
       'tpClOrdId':'TP-CLIENT','buyPx':'0.002280','tpPx':'0.002290',
       'filledBuy':filled,'filledTP':'0','tpOrders':[{'source':'attached',
       'phase':'PENDING','ordId':'','clOrdId':'TP-CLIENT','sz':filled,'filled':'0'}]}
    p={'instId':'DOOD-USDT-SWAP','side':'buy','state':'filled','ordId':'BUY-1',
       'clOrdId':'BUY-CLIENT','accFillSz':filled,
       'attachAlgoOrds':[{'attachAlgoClOrdId':'TP-CLIENT','tpOrdKind':'limit',
       'tpOrdPx':'0.002290','failCode':code,'failReason':reason}]}
    return c,p


class AttachedTPTests(unittest.TestCase):
    def test_verified_51006_becomes_virtual_with_original_price(self):
        c,parent=sample()
        ns=defs('parent_tp_failure','record_attached_tp_failure','tp_records',
                'active_tp_records','cycle_sell_fill','track','tp_failure_buy_hold')
        events=[];st={'cycles':{'0.002280':c},'completed':0}
        def order(oid='',cid=''):
            if oid=='BUY-1':return parent
            return None
        ns.update(A=SimpleNamespace(order=order),reconcile_aggregate=lambda st:True,
                  paged_order=lambda cid:None,save=lambda st:None,
                  journal_tp=lambda *a,**kw:events.append(('journal',kw)),
                  log=lambda event,**kw:events.append((event,kw)),
                  margin_rejection=lambda code:str(code).split('_')[0]=='51008',
                  trip_account_soft_stop=lambda *a: self.fail('No account latch on 51006'))
        result=ns['track'](st,[])
        self.assertTrue(result)
        self.assertEqual(c['state'],'VIRTUAL_TP')
        self.assertEqual(c['tpPx'],'0.002290')
        self.assertEqual(c['tpOrders'][0]['phase'],'FAILED')
        self.assertEqual(c['tpOrders'][0]['failCode'],'51006')
        self.assertEqual(ns['tp_failure_buy_hold'](st),[])
        self.assertIn('ATTACHED_TP_REJECTED',[e[0] for e in events])

    def test_no_failed_code_stays_reserved_and_blocks_buy(self):
        c,parent=sample(code='')
        ns=defs('parent_tp_failure','record_attached_tp_failure','tp_records',
                'active_tp_records','cycle_sell_fill','track')
        ns.update(A=SimpleNamespace(order=lambda oid='',cid='':parent if oid else None),
                  reconcile_aggregate=lambda st:True,paged_order=lambda cid:None,
                  save=lambda st:None,log=lambda *a,**kw:None,
                  journal_tp=lambda *a,**kw:None)
        self.assertFalse(ns['track']({'cycles':{'p':c}},[]))
        self.assertEqual(c['tpOrders'][0]['phase'],'PENDING')

    def test_unknown_failure_held_for_review_not_retried(self):
        c,parent=sample(code='51000',reason='Invalid order parameter')
        ns=defs('parent_tp_failure','record_attached_tp_failure','tp_records',
                'active_tp_records','cycle_sell_fill','track','tp_failure_buy_hold')
        ns.update(A=SimpleNamespace(order=lambda oid='',cid='':parent if oid else None),
                  reconcile_aggregate=lambda st:True,paged_order=lambda cid:None,
                  save=lambda st:None,log=lambda *a,**kw:None,
                  journal_tp=lambda *a,**kw:None,
                  margin_rejection=lambda code:False)
        st={'cycles':{'p':c}}
        self.assertTrue(ns['track'](st,[]))
        self.assertEqual(c['state'],'VIRTUAL_TP')
        self.assertEqual(ns['tp_failure_buy_hold'](st),[('dood-cycle','51000')])

    def test_margin_attached_failure_sets_account_wide_soft(self):
        c,parent=sample(code='51008',reason='Insufficient margin')
        ns=defs('parent_tp_failure','record_attached_tp_failure','tp_records',
                'active_tp_records','cycle_sell_fill','track')
        latch=[]
        ns.update(A=SimpleNamespace(order=lambda oid='',cid='':parent if oid else None),
                  reconcile_aggregate=lambda st:True,paged_order=lambda cid:None,
                  save=lambda st:None,log=lambda *a,**kw:None,
                  journal_tp=lambda *a,**kw:None,
                  margin_rejection=lambda code:str(code)=='51008',
                  trip_account_soft_stop=lambda *a:latch.append(a))
        self.assertTrue(ns['track']({'cycles':{'p':c}},[]))
        self.assertEqual(len(latch),1)
        self.assertEqual(latch[0][-1],'ATTACHED_TP')

    def test_parent_and_tp_identity_required_and_partial_cancel_covered(self):
        c,parent=sample()
        ns=defs('parent_tp_failure')
        ns['A']=SimpleNamespace(order=lambda oid='':parent)
        self.assertEqual(ns['parent_tp_failure'](c,c['tpOrders'][0])['code'],'51006')
        parent['state']='canceled';parent['accFillSz']='0.5'
        c['filledBuy']='0.5';c['tpOrders'][0]['sz']='0.5'
        self.assertEqual(ns['parent_tp_failure'](c,c['tpOrders'][0])['code'],'51006')
        parent['clOrdId']='FOREIGN'
        with self.assertRaises(RuntimeError):ns['parent_tp_failure'](c,c['tpOrders'][0])

    def test_price_limit_never_changes_strategy_price(self):
        ns=defs('tp_price_limit_allows')
        c={'tpPx':'0.002290'}
        lim=['0.002292']
        ns['A']=SimpleNamespace(req=lambda method,path,params:[{'instId':'DOOD-USDT-SWAP','sellLmt':lim[0]}])
        self.assertEqual(ns['tp_price_limit_allows'](c),(False,Decimal('0.002292')))
        lim[0]='0.002167'
        self.assertEqual(ns['tp_price_limit_allows'](c),(True,Decimal('0.002167')))
        self.assertEqual(c['tpPx'],'0.002290')

    def test_price_limited_virtual_does_not_cancel_valid_real_tp(self):
        ns=defs('maintain_virtual_tp','tp_price_limit_allows')
        virtual={'cycleId':'virtual','state':'VIRTUAL_TP','tpPx':'0.002290',
                 'filledBuy':'1','tpOrders':[]}
        real_cycle={'cycleId':'real','tpPx':'0.002400'}
        real_orders=[('a',real_cycle,{'phase':'ACTIVE','clOrdId':'a'}),
                     ('b',real_cycle,{'phase':'ACTIVE','clOrdId':'b'})]
        st={'cycles':{'v':virtual}}
        calls=[];sell_limit=['0.002292']
        class Buffer:
            overflow_notified=False
            quiet_since=0
            last_cancel_at=None
            def observe(self,*a):pass
            def cancel_due(self,*a):return False
            def canceled(self,*a):pass
        ns.update(A=SimpleNamespace(req=lambda *a,**kw:[{'instId':'DOOD-USDT-SWAP',
                  'sellLmt':sell_limit[0]}]),
                  REAL_TP_MAX=150,coverage_audit=lambda *a:(2,0,1),
                  real_tp_active=lambda st:real_orders,
                  virtual_cycles=lambda st:[('v',virtual)],
                  tp_quantity_placeable=lambda sz:True,
                  remaining_tp=lambda c:Decimal('1'),
                  cancel_tp=lambda *a:calls.append('cancel'),
                  save=lambda *a:None,log=lambda *a,**kw:None)
        self.assertEqual(ns['maintain_virtual_tp'](st,[],Decimal(1),
                         bid=Decimal('0.002200'),tp_buffer=Buffer()),'NONE')
        self.assertEqual(calls,[])
        self.assertEqual(virtual['tpPx'],'0.002290')
        sell_limit[0]='0.002100'
        virtual['tpRestoreRetryAt']=0
        self.assertEqual(ns['maintain_virtual_tp'](st,[],Decimal(1),
                         bid=Decimal('0.002200'),tp_buffer=Buffer()),'CANCEL')
        self.assertEqual(calls,['cancel'])

    def test_real_tp_restore_obeys_cap_and_price_limit(self):
        ns=defs('place_virtual_tp','tp_price_limit_allows','tp_records')
        c={'state':'VIRTUAL_TP','cycleId':'cycle','tpPx':'0.002290',
            'filledBuy':'1','filledTP':'0','tpOrders':[]}
        st={'cycles':{'a':c}}
        calls=[];msgs=[];lim=['0.002292'];slots=[0]
        def request(method,path,params=None,body=None,exp_time_ms=None):
            calls.append((method,path,body))
            if method=='GET':return [{'instId':'DOOD-USDT-SWAP','sellLmt':lim[0]}]
            return [{'sCode':'0','ordId':'SELL-1'}]
        ns.update(A=SimpleNamespace(req=request),active_tp_records=lambda c:[],
                  real_tp_active=lambda st:range(slots[0]),
                  tp_quantity_placeable=lambda sz:True,
                  remaining_tp=lambda c:Decimal(c['filledBuy'])-sum((Decimal(r['filled']) for r in c['tpOrders']),Decimal(0)),
                  newid=lambda t:'NEW-SELL',new_request_deadline_ms=lambda:100000,
                  save=lambda st:None,journal_tp=lambda *a,**kw:None,
                  log=lambda event,**kw:msgs.append(event))
        self.assertFalse(ns['place_virtual_tp'](st,c))
        self.assertFalse(any(row[0]=='POST' for row in calls))
        self.assertEqual(c['tpPx'],'0.002290')
        c['tpRestoreRetryAt']=0
        slots[0]=2
        self.assertFalse(ns['place_virtual_tp'](st,c))
        self.assertFalse(any(row[0]=='POST' for row in calls))
        slots[0]=0;lim[0]='0.002167'
        self.assertTrue(ns['place_virtual_tp'](st,c))
        self.assertEqual(c['state'],'TP_WAIT')
        self.assertEqual(c['tpOrders'][0]['source'],'recovery')
        self.assertEqual([row[2]['px'] for row in calls if row[0]=='POST'],['0.002290'])

if __name__=='__main__':unittest.main(verbosity=2)
