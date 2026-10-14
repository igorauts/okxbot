#!/usr/bin/env python3
"""Offline mock tests for transformed okx_bot069.py; NO network or exchange keys."""
import ast
import base64
import copy
from datetime import datetime,timezone
from decimal import Decimal
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import secrets
import ssl
import time
import unittest
import urllib.parse

SOURCE = Path(__file__).parent / 'okx_bot069.py'

def pick(*names):
    tree=ast.parse(SOURCE.read_text())
    selected=[copy.deepcopy(n) for n in tree.body
              if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    assert set(n.name for n in selected)==set(names), f'Missing functions: {set(names)-set(n.name for n in selected)}'
    return compile(ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[])),str(SOURCE),'exec')

class ExchangeTemporary(RuntimeError):pass
class ExchangeUnresolved(RuntimeError):pass
class RateLimitedError(ExchangeTemporary):pass
class FakeGate:
    def begin(self,*args,**kw):return False
    def end(self,*args):pass
    def throttle(self,*args):pass

def base_ns():
    return dict(datetime=datetime,timezone=timezone,Decimal=Decimal,
        base64=base64,hashlib=hashlib,hmac=hmac,time=time,os=os,fcntl=fcntl,
        json=json,Path=Path,urllib=urllib,http=http,threading=threading,ssl=ssl,secrets=secrets,
        ENV={'OKX_API_KEY':'test','OKX_API_SECRET':'test','OKX_API_PASSPHRASE':'test'},
        ExchangeTemporary=ExchangeTemporary,ExchangeUnresolved=ExchangeUnresolved,
        RateLimitedError=RateLimitedError,REST_GATE=FakeGate(),LIVE=True,
        CFG={'name':'mock'},INST='ETH-USDT-SWAP',
        log=lambda *args,**kw:None,die=lambda text:(_ for _ in ()).throw(RuntimeError(text)))

class SafetyTests(unittest.TestCase):
    def test_api_explicit_refusal_and_uncertain_are_distinct(self):
        ns=base_ns();exec(pick('API'),ns)
        api=ns['API']()
        request={'clOrdId':'OX4BTEST'}
        for s_code in ['51008','51008_1000','51165']:
            api._http=lambda *a: (200,{'code':'1','msg':'error',
                'data':[{'clOrdId':'OX4BTEST','sCode':s_code,'sMsg':'reject'}]})
            self.assertEqual(api.req('POST','/api/v5/trade/order',body=request)[0]['sCode'],s_code)
        api._http=lambda *a: (200,{'code':'0','data':[{'sCode':'0','ordId':'123'}]})
        self.assertEqual(api.req('POST','/api/v5/trade/order',body=request)[0]['ordId'],'123')
        api._http=lambda *a: (500,{'code':'1','data':[{'sCode':'51008'}]})
        with self.assertRaises(ExchangeUnresolved):api.req('POST','/api/v5/trade/order',body=request)
        api._http=lambda *a: (200,{'code':'1','data':[]})
        with self.assertRaises(ExchangeUnresolved):api.req('POST','/api/v5/trade/order',body=request)
        def timeout(*a):raise TimeoutError('ACK lost')
        api._http=timeout
        with self.assertRaises(ExchangeUnresolved):api.req('POST','/api/v5/trade/order',body=request)

    def test_shared_trip_both_instances_persist_soft(self):
        with tempfile.TemporaryDirectory() as d:
            shared=Path(d)/'shared'/'stop.json'
            code=pick('atomic_json','prepare_account_safety','account_soft_active',
                      'margin_rejection','trip_account_soft_stop','control_mode')
            ns=[]
            for name in ['eth064','sol065']:
                state=Path(d)/name
                state.mkdir()
                env=base_ns()
                env.update(ACCOUNT_SOFT_STOP=shared,
                           ACCOUNT_SOFT_LOCK=shared.with_name('stop.json.lock'),
                           CONTROL_FILE=state/'control.json',CONTROL_MODES={'RUN','PAUSE','SOFT','HARD','PANIC'})
                exec(code,env)
                ns.append(env)
            eth,sol=ns
            self.assertEqual(eth['control_mode'](),'RUN')
            self.assertEqual(sol['control_mode'](),'RUN')
            self.assertTrue(eth['margin_rejection']('51008_1000'))
            self.assertFalse(eth['margin_rejection']('51000'))
            eth['trip_account_soft_stop']('51008','Insufficient margin','BUY')
            self.assertEqual(eth['control_mode'](),'SOFT')
            self.assertEqual(sol['control_mode'](),'SOFT')
            self.assertTrue(shared.exists())
            self.assertEqual(json.loads(shared.read_text())['manual_reset_required'],True)
            # Removing the common latch alone MUST NOT reactivate either bot.
            shared.unlink()
            self.assertEqual(eth['control_mode'](),'SOFT')
            self.assertEqual(sol['control_mode'](),'SOFT')

    def test_buy_refusal_marks_canceled_then_trips_account(self):
        ns=base_ns();exec(pick('place'),ns)
        ns.update(fmt=lambda x:f'{x:.2f}',TP=Decimal('0.5'),QTY=Decimal('0.03'),
                  COIN='ETH',newid=lambda prefix:prefix+'00001',save=lambda st:None)
        trip=[]
        ns['trip_account_soft_stop']=lambda *args:trip.append(args)
        ns['margin_rejection']=lambda code:str(code).startswith('51008')
        class API:
            def req(self,*a,**kw):return [{'sCode':'51008','sMsg':'Margin insufficient'}]
        ns['A']=API()
        st={'cycles':{},'archived_cycles':[]}
        self.assertFalse(ns['place'](st,Decimal('2500')))
        self.assertEqual(st['cycles']['2500.00']['state'],'CANCELED')
        self.assertEqual(len(trip),1)
        self.assertEqual(trip[0][0],'51008')

    def test_rejected_virtual_tp_stays_virtual_and_backs_off(self):
        ns=base_ns();exec(pick('place_virtual_tp'),ns)
        ns.update(remaining_tp=lambda c:Decimal('0.03'),
            active_tp_records=lambda c:[r for r in c['tpOrders'] if r['phase'] in ('ACTIVE','PLACING')],
            real_tp_active=lambda st:[],REAL_TP_TARGET=75,
            tp_quantity_placeable=lambda q:True,
            tp_records=lambda c:c.setdefault('tpOrders',[]),newid=lambda prefix:'T00001',
            save=lambda st:None,journal_tp=lambda *args,**kw:None)
        trip=[];ns['trip_account_soft_stop']=lambda *args:trip.append(args)
        ns['margin_rejection']=lambda code: str(code).startswith('51008')
        class API:
            def req(self,*a,**kw):return [{'sCode':'51008','sMsg':'Insufficient margin'}]
        ns['A']=API()
        c={'cycleId':'C1','buyPx':'2500.00','tpPx':'2501.00',
           'state':'VIRTUAL_TP','tpOrders':[]}
        self.assertFalse(ns['place_virtual_tp']({'cycles':{'a':c}},c))
        self.assertEqual(c['state'],'VIRTUAL_TP')
        self.assertEqual(c['tpOrders'][0]['phase'],'FAILED')
        self.assertGreater(c['tpRestoreRetryAt'],time.time())
        self.assertEqual(len(trip),1)

    def test_unresolved_buy_does_not_crash_other_tracking(self):
        ns=base_ns();exec(pick('track'),ns)
        ns['migrate_cycle_records']=lambda st:None
        ns['save']=lambda st:None
        class API:
            def order(self,**kwargs):return None
        ns['A']=API()
        cid='B1'
        st={'cycles':{'2500.00':{'state':'PLACING','clOrdId':cid,
                                 'placingWarnAt':time.time()}},'archived_cycles':[]}
        self.assertFalse(ns['track'](st,[]))
        self.assertEqual(st['cycles']['2500.00']['state'],'PLACING')

    def test_soft_cancels_other_known_buys_even_if_unresolved(self):
        ns=base_ns();exec(pick('cancel_buys_for_soft_stop'),ns)
        c1={'state':'PLACING','clOrdId':'Unknown'}
        c2={'state':'BUY_LIVE','ordId':'Existing','buyPx':'100'}
        ns['active_buy']=lambda st:[c1,c2]
        canceled=[]
        ns['cancel_lowest_specific']=lambda st,c:canceled.append(c['ordId'])
        self.assertEqual(ns['cancel_buys_for_soft_stop']({'cycles':{}},False),'CANCEL_BUYS')
        self.assertEqual(canceled,['Existing'])

    def test_cancel_api_explicit_rejection_requires_order_id(self):
        ns=base_ns();exec(pick('API'),ns)
        api=ns['API']()
        request={'ordId':'123'}
        api._http=lambda *a: (200, {'code':'1','data':[{'ordId':'123','sCode':'51400','sMsg':'already canceled'}]})
        self.assertEqual(api.req('POST','/api/v5/trade/cancel-order',body=request)[0]['sCode'],'51400')
        api._http=lambda *a: (200, {'code':'1','data':[{'ordId':'WRONG','sCode':'51400'}]})
        with self.assertRaises(ExchangeUnresolved):
            api.req('POST','/api/v5/trade/cancel-order',body=request)

    def test_ambiguous_conflicting_buy_rejection_cannot_be_marked_canceled(self):
        ns=base_ns();exec(pick('API'),ns)
        api=ns['API']()
        for row in ({'clOrdId':'SOMEONE_ELSE','ordId':'','sCode':'51008'},
                    {'clOrdId':'OX4BTEST','ordId':'ALREADY_ACCEPTED','sCode':'51008'}):
            api._http=lambda *args, row=row:(200, {'code':'1','data':[row]})
            with self.assertRaises(ExchangeUnresolved):
                api.req('POST','/api/v5/trade/order',body={'clOrdId':'OX4BTEST'})

    def test_explicit_buy_cancel_refusal_keeps_pending_intent(self):
        ns=base_ns();exec(pick('cancel_lowest_specific'),ns)
        ns.update(save=lambda *a:None,log=lambda *a,**kw:None)
        class API:
            def req(self,*a,**kw):return [{'ordId':'B123','sCode':'51400','sMsg':'already canceled'}]
        ns['A']=API()
        c={'state':'BUY_LIVE','ordId':'B123','buyPx':'100','filledBuy':'0','sz':'1'}
        self.assertFalse(ns['cancel_lowest_specific']({'cycles':{'100':c}},c))
        self.assertEqual(c['state'],'CANCELING')
        self.assertEqual(c['cancelRejectCode'],'51400')
        self.assertGreater(c['cancelRejectAt'],0)

    def test_rejected_cancel_rearms_after_fresh_live_exchange_status(self):
        ns=base_ns();exec(pick('track'),ns)
        ns.update(migrate_cycle_records=lambda st:None,save=lambda st:None,
                  trip_if_margin_cancel=lambda *a:None,D=lambda x:Decimal(str(x or '0')),
                  tp_records=lambda c:c.get('tpOrders',[]))
        c={'state':'CANCELING','ordId':'B123','buyPx':'100','sz':'1','filledBuy':'0',
           'cancelRejectAt':time.time()-15,'tpOrders':[]}
        pending=[{'ordId':'B123','clOrdId':'CB','side':'buy','instId':'ETH-USDT-SWAP',
                  'state':'live','accFillSz':'0'}]
        st={'cycles':{'100':c}}
        self.assertFalse(ns['track'](st,pending))
        self.assertEqual(c['state'],'BUY_LIVE')
        self.assertNotIn('cancelRejectAt',c)

    def test_unresolved_buy_does_not_skip_other_known_tp(self):
        ns=base_ns();exec(pick('track'),ns)
        inspected=[]
        ns.update(migrate_cycle_records=lambda st:None,save=lambda st:None,
                  D=lambda x:Decimal(str(x or '0')),
                  tp_records=lambda c:c.get('tpOrders',[]),
                  tp_record_update=lambda st,c,rec,o:inspected.append(o['ordId']) or 'ACTIVE',
                  active_tp_records=lambda c:[x for x in c.get('tpOrders',[]) if x['phase']=='ACTIVE'],
                  cycle_sell_fill=lambda c:Decimal('0'))
        class API:
            def order(self,**kwargs):return None
        ns['A']=API()
        rec={'clOrdId':'TP567','ordId':'TP999','sz':'0.03','filled':'0','phase':'ACTIVE'}
        st={'cycles':{
            '2500':{'state':'PLACING','clOrdId':'B0','placingWarnAt':time.time()},
            '2499':{'state':'TP_WAIT','cycleId':'CY1','filledBuy':'0.03',
                    'filledTP':'0','tpOrders':[rec]}}}
        pending=[{'ordId':'TP999','clOrdId':'TP567','side':'sell','instId':'ETH-USDT-SWAP'}]
        self.assertFalse(ns['track'](st,pending))
        self.assertEqual(inspected,['TP999'])
        self.assertEqual(st['cycles']['2500']['state'],'PLACING')

    def test_unknown_recovery_tp_after_60_seconds_is_reserved_not_resubmitted(self):
        ns=base_ns();exec(pick('track'),ns)
        ns.update(migrate_cycle_records=lambda st:None,save=lambda st:None,
                  D=lambda x:Decimal(str(x or '0')),
                  paged_order=lambda cid:None,
                  tp_records=lambda c:c.get('tpOrders',[]),
                  active_tp_records=lambda c:[x for x in c.get('tpOrders',[])
                      if x['phase'] in ('PLACING','PENDING','ACTIVE','CANCELING')],
                  cycle_sell_fill=lambda c:Decimal('0'))
        class API:
            def order(self,**kwargs):return None
        ns['A']=API()
        rec={'clOrdId':'TP123','ordId':'','sz':'0.03','filled':'0','phase':'PLACING',
             'source':'recovery','placedAt':time.time()-75}
        c={'cycleId':'C1','state':'TP_WAIT','filledBuy':'0.03','filledTP':'0',
           'tpOrders':[rec]}
        self.assertFalse(ns['track']({'cycles':{'2499':c}},[]))
        self.assertEqual(rec['phase'],'PLACING')
        self.assertEqual(c['state'],'TP_WAIT')

    def test_margin_canceled_by_exchange_has_strict_codes(self):
        ns=base_ns();exec(pick('exchange_cancel_margin','trip_if_margin_cancel'),ns)
        trips=[];ns['trip_account_soft_stop']=lambda *args:trips.append(args)
        for source in ['2','3','6','9']:
            o={'state':'canceled','cancelSource':source,
               'cancelSourceReason':'Insufficient margin'}
            self.assertTrue(ns['exchange_cancel_margin'](o))
            ns['trip_if_margin_cancel'](o,'BUY_CANCELED')
        for source in ['0','1','15','17','22']:
            self.assertFalse(ns['exchange_cancel_margin']({'state':'canceled','cancelSource':source}))
        self.assertFalse(ns['exchange_cancel_margin']({'state':'live','cancelSource':'3'}))
        self.assertEqual(len(trips),4)

    def test_safe_tp_when_buy_uncertain_requires_exact_coverage(self):
        ns=base_ns();exec(pick('safe_tp_during_buy_wait'),ns)
        called=[]
        ns.update(tp_records=lambda c:c.get('tpOrders',[]),
            coverage_audit=lambda *args:called.append('coverage'),
            maintain_virtual_tp=lambda *args:called.append('maintain') or 'RESTORE')
        st={'cycles':{'A':{'tpOrders':[]}}}
        self.assertEqual(ns['safe_tp_during_buy_wait'](st,[],Decimal(0)),'RESTORE')
        self.assertEqual(called,['coverage','maintain'])
        st['cycles']['B']={'tpOrders':[{'phase':'PLACING'}]}
        self.assertEqual(ns['safe_tp_during_buy_wait'](st,[],Decimal(0)),'TP_UNRESOLVED')
        self.assertEqual(called,['coverage','maintain'])
        st['cycles']['B']['tpOrders'][0]['phase']='FAILED'
        ns['coverage_audit']=lambda *a:(_ for _ in ()).throw(RuntimeError('coverage mismatch'))
        self.assertEqual(ns['safe_tp_during_buy_wait'](st,[],Decimal(0)),'COVERAGE_UNRESOLVED')

    def test_virtual_tp_cooldown_skips_without_extra_order(self):
        ns=base_ns();exec(pick('maintain_virtual_tp'),ns)
        orders=[]
        c={'tpPx':'2600','tpRestoreRetryAt':time.time()+100,'partial_cleanup':False}
        ns.update(coverage_audit=lambda *a:(0,Decimal(0),Decimal('0.03')),
              D=lambda x:Decimal(str(x or '0')),
              virtual_cycles=lambda st:[('A',c)],
              remaining_tp=lambda c:Decimal('0.03'),
              tp_quantity_placeable=lambda q:True,
              real_tp_active=lambda st:[],REAL_TP_TARGET=74,TP_ACTIONS_PER_PASS=5,
              place_virtual_tp=lambda st,c:orders.append('TP'))
        self.assertEqual(ns['maintain_virtual_tp']({'cycles':{'A':c}},[],Decimal('0.03')),'NONE')
        self.assertEqual(orders,[])
        c['tpRestoreRetryAt']=time.time()-1
        self.assertEqual(ns['maintain_virtual_tp']({'cycles':{'A':c}},[],Decimal('0.03')),'RESTORE')
        self.assertEqual(orders,['TP'])

    def test_soft_only_cancels_orders_confirmed_in_fresh_exchange_snapshot(self):
        ns=base_ns();exec(pick('cancel_buys_for_soft_stop'),ns)
        ns.update(active_buy=lambda st:list(st['cycles'].values()))
        canceled=[]
        ns['cancel_lowest_specific']=lambda st,c:canceled.append(c['ordId'])
        c1={'state':'BUY_LIVE','ordId':'123','clOrdId':'C123','buyPx':'2500'}
        c2={'state':'BUY_LIVE','ordId':'456','clOrdId':'C456','buyPx':'2501'}
        st={'cycles':{'2500':c1,'2501':c2}}
        pending=[{'instId':'ETH-USDT-SWAP','clOrdId':'C123','ordId':'123',
                  'state':'live','side':'buy'}]
        self.assertEqual(ns['cancel_buys_for_soft_stop'](st,False,pending),'CANCEL_BUYS')
        self.assertEqual(canceled,['123'])
        bad=[{'instId':'ETH-USDT-SWAP','clOrdId':'NOT_OURS','ordId':'123',
              'state':'live','side':'buy'}]
        with self.assertRaises(RuntimeError):ns['cancel_buys_for_soft_stop'](st,False,bad)

    def test_soft_position_mismatch_returns_for_known_order_cancellation(self):
        ns=base_ns();exec(pick('reconcile_position'),ns)
        ns.update(verify_existing=lambda *a:None,register_cancel_attached=lambda *a:None,
                  track=lambda *a:False,D=lambda x:Decimal(str(x or '0')),
                  panic_executions=lambda st:Decimal(0))
        class API:
            def pending(self):return []
            def positions(self):return [{'pos':'1'}]
        ns['A']=API()
        st={'cycles':{}}
        result=ns['reconcile_position'](st,timeout=0,allow_mismatch=True)
        self.assertEqual(result,([],False,Decimal(1)))
        with self.assertRaises(ExchangeUnresolved):
            ns['reconcile_position'](st,timeout=0,allow_mismatch=False)

    def test_manual_reset_requires_two_bots_both_confirmed_soft(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);latch=root/'account_soft_stop.json'
            latch.write_text(json.dumps({'schema':1,'mode':'SOFT','reason':'MARGIN_REJECTION'}))
            roots=[]
            for name in ('ETH','SOL'):
                bot=root/name
                (bot/'state').mkdir(parents=True)
                (bot/'state'/'control.json').write_text(json.dumps({'mode':'SOFT'}))
                (bot/'state'/'control_ack.json').write_text(json.dumps({'mode':'SOFT','phase':'SOFT_STOPPED'}))
                roots.append(bot)
            script=Path(__file__).with_name('manual_clear_account_soft.py')
            args=[sys.executable,str(script),'--bot',str(roots[0]),'--bot',str(roots[1]),'--shared',str(latch)]
            audit=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(audit.returncode,0,audit.stderr)
            self.assertTrue(latch.exists())
            (roots[1]/'state'/'control_ack.json').write_text(json.dumps({'mode':'SOFT','phase':'CANCEL_BUYS'}))
            denied=subprocess.run(args+['--confirm','I_REVIEWED_MARGIN_AND_BOTH_BOTS'],
                                  capture_output=True,text=True)
            self.assertNotEqual(denied.returncode,0)
            self.assertTrue(latch.exists())
            (roots[1]/'state'/'control_ack.json').write_text(json.dumps({'mode':'SOFT','phase':'SOFT_STOPPED'}))
            allowed=subprocess.run(args+['--confirm','I_REVIEWED_MARGIN_AND_BOTH_BOTS'],
                                   capture_output=True,text=True)
            self.assertEqual(allowed.returncode,0,allowed.stderr)
            self.assertFalse(latch.exists())
            self.assertTrue(all(json.loads((b/'state'/'control.json').read_text())['mode']=='SOFT'
                                for b in roots))

if __name__=='__main__':unittest.main(verbosity=2)
