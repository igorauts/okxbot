"""OFFLINE model of OKX order lifecycle; actual engine functions via AST.
No exchange, no real filesystem writes, no services. The simulated venue
response shape is a test assumption, not confirmation from OKX LIVE.
"""
import ast, copy, json, unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
NAMES={'D','track','tp_records','cycle_sell_fill','active_tp_records','remaining_tp',
       'virtual_cycles','unprotected_partial_buy','real_tp_active','coverage_audit',
       'partial_detach_identity','partial_detach_empty','partial_detach_proof',
       'observe_partial_detach','remember_partial_detach_event',
       'resolve_detached_terminal','partial_detach_reservation_proof','partial_detach_terminal_shape','partial_detach_action','partial_cleanup_due',
       'partial_cleanup_action','market_exit_partial','cancel_lowest_specific',
       'resolve_partial_cancel','register_cancel_attached','tp_record_update','verify_existing',
       'isolated_partial_coverage','unresolved_aggregate'}
TREE=ast.parse((ROOT/'okx_bot.py').read_text())
CODE=compile(ast.Module(body=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES],type_ignores=[]),'<actual-engine-functions>','exec')
class Unresolved(RuntimeError):pass

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.now=100.0;self.mode='RUN';self.posts=[];self.events=[];self.durable=None
        self.c={'state':'BUY_PARTIAL','cycleId':'new-cycle','ordId':'parent','clOrdId':'buy-client',
                'tpClOrdId':'attached-client','buyPx':'0.3954','tpPx':'0.3960','sz':'4',
                'filledBuy':'2','filledTP':'0','tpOrders':[],'partial_since':95.0}
        self.st={'cycles':{'0.3954':self.c},'completed':0}
        self.parent={'instId':'TEST-USDT-SWAP','ordId':'parent','clOrdId':'buy-client','side':'buy',
                     'posSide':'net','reduceOnly':'false','px':'0.3954','sz':'4','accFillSz':'2',
                     'state':'partially_filled','uTime':'99999','attachAlgoOrds':[
                        {'attachAlgoId':'attachment-id','attachAlgoClOrdId':'attached-client',
                         'tpOrdKind':'limit','tpOrdPx':'0.3960'}]}
        self.history_calls=[];self.child=None;self.ack='ok';self.market=None;self.serial=0;self.order_calls=[]
        self.ns={'Decimal':Decimal,'INST':'TEST-USDT-SWAP','time':SimpleNamespace(time=lambda:self.now,monotonic=lambda:self.now),
                 'A':SimpleNamespace(order=self.order,req=self.req),'ExchangeUnresolved':Unresolved,
                 'CoverageMismatch':Unresolved,'die':lambda m:(_ for _ in ()).throw(Unresolved(m)),
                 'save':self.save,'log':lambda name,**kw:self.events.append(name),
                 'journal_tp':lambda *a,**kw:None,'note_cleanup_fill':lambda:None,
                 'reconcile_aggregate':lambda st:True,'trip_if_margin_cancel':lambda *a:None,
                 'parent_tp_failure':lambda *a,**kw:None,'paged_order':lambda *a:self.history_calls.append(a),
                 'post_absence_confirmed':lambda *a:False,'margin_rejection':lambda *a:False,
                 'control_mode':lambda:self.mode,'tp_quantity_placeable':lambda q:q>0,
                 'newid':self.newid,'PARTIAL_AGE':5,'PARTIAL_STEPS':Decimal(3),'STEP':Decimal('.0006'),
                 'REDUCE_ONLY_HARD_CAP':150}
        exec(CODE,self.ns)
    def newid(self,prefix):
        self.serial+=1;return prefix+str(self.serial)
    def save(self,st):self.durable=json.loads(json.dumps(st))
    def restart(self):
        self.st=json.loads(json.dumps(self.durable));self.c=self.st['cycles']['0.3954']
    def order(self,oid='',cid=''):
        self.order_calls.append((oid,cid))
        if oid=='parent':return copy.deepcopy(self.parent)
        if self.child and (oid==self.child['ordId'] or cid==self.child['clOrdId']):return copy.deepcopy(self.child)
        if self.market and (oid==self.market['ordId'] or cid==self.market['clOrdId']):return copy.deepcopy(self.market)
        return None
    def req(self,method,route,body):
        self.assertEqual(method,'POST');self.posts.append((route,copy.deepcopy(body)))
        durable=self.durable['cycles']['0.3954']
        if route.endswith('amend-order'):
            self.assertEqual(durable['partialDetach']['phase'],'REQUESTED')
            self.assertEqual(durable['partialDetach']['reqId'],body['reqId'])
            self.assertEqual(body['attachAlgoOrds'],[{'attachAlgoId':'attachment-id','newTpOrdKind':'limit','newTpOrdPx':'0'}])
            self.assertEqual(body['newPx'],'0.3954');self.assertFalse(body['cxlOnFail'])
            if self.ack=='lost':raise Unresolved('transport lost')
            return [{'ordId':'parent','reqId':body['reqId'],'sCode':'0' if self.ack=='ok' else '51500'}]
        if route.endswith('cancel-order'):
            self.assertEqual(durable['state'],'CANCELING')
            self.assertEqual(durable['partialDetach']['phase'],'CONFIRMED')
            return [{'ordId':'parent','sCode':'0'}]
        self.assertTrue(route.endswith('/order'));self.assertEqual(body['ordType'],'market')
        self.assertTrue(body['reduceOnly']);self.assertEqual(body['side'],'sell');self.assertEqual(body['posSide'],'net')
        self.assertEqual(durable['tpOrders'][-1]['phase'],'PLACING')
        self.assertEqual(durable['tpOrders'][-1]['clOrdId'],body['clOrdId'])
        if self.ack=='market-lost':raise Unresolved('MARKET transport lost')
        self.market={'instId':'TEST-USDT-SWAP','side':'sell','state':'filled','ordId':'market',
                     'clOrdId':body['clOrdId'],'ordType':'market','reduceOnly':'true',
                     'sz':body['sz'],'accFillSz':body['sz']}
        return [{'ordId':'market','sCode':'0'}]
    def action(self,qty='2',bid='.3972'):
        return self.ns['partial_cleanup_action'](self.st,Decimal(bid),Decimal(qty),self.mode)
    def detached(self,**kw):
        self.parent.update(attachAlgoOrds=[],uTime='100001',**kw);self.now+=2
    def track(self,pending=None,**kwargs):
        pending=[] if pending is None else pending
        self.ns['register_cancel_attached'](self.st,pending)
        resolved=self.ns['track'](self.st,pending,**kwargs)
        self.ns['verify_existing'](self.st,pending)
        return resolved
    def complete_cancel(self,fill='2'):
        self.parent.update(state='canceled',accFillSz=fill,uTime='100002')
        self.assertTrue(self.track());self.assertEqual(self.c['state'],'VIRTUAL_TP')
        self.assertEqual(self.c['tpOrders'],[])
        self.ns['coverage_audit'](self.st,[],Decimal(fill))
    def test_normal_detach_cancel_market_no_phantom(self):
        self.assertEqual(self.action(),'ACTION');self.assertEqual(len(self.posts),1)
        self.assertEqual(self.action(),'NONE');self.assertEqual(len(self.posts),1) # ACK not proof
        self.detached();self.assertEqual(self.action(),'ACTION');self.assertEqual(len(self.posts),2)
        self.complete_cancel();self.assertEqual(self.action(),'ACTION');self.assertEqual(len(self.posts),3)
        self.assertEqual(self.posts[-1][1]['sz'],'2');self.assertTrue(self.track())
        self.assertEqual(self.c['state'],'EXITED');self.assertEqual(self.st['partial_exits'],1)
        self.assertEqual(self.action(),'NONE');self.assertEqual(len(self.posts),3)
        self.ns['coverage_audit'](self.st,[],Decimal(0));self.assertEqual(self.history_calls,[])
    def test_lost_ack_restart_no_duplicate_amend(self):
        self.ack='lost'
        with self.assertRaises(Unresolved):self.action()
        self.restart();self.ack='ok';self.detached()
        self.assertEqual(self.action(),'ACTION');self.complete_cancel();self.assertEqual(self.action(),'ACTION')
        self.assertEqual([r.split('/')[-1] for r,_ in self.posts],['amend-order','cancel-order','order'])
    def test_missing_stale_or_nonempty_attachment_not_proof(self):
        self.action();self.now+=2;del self.parent['attachAlgoOrds']
        self.assertEqual(self.action(),'NONE');self.now+=2
        self.parent['attachAlgoOrds']=[];self.assertEqual(self.action(),'NONE');self.now+=2
        self.parent.update(uTime='100001',tpOrdPx='0.3960');self.assertEqual(self.action(),'NONE')
        self.assertEqual(len(self.posts),1)
    def test_unresolved_amend_ends_extra_get_polling_but_normal_reconcile_recovers(self):
        self.action();self.now+=11
        before=len(self.order_calls)
        self.assertEqual(self.action(),'NONE');self.assertEqual(len(self.order_calls),before)
        self.restart();self.now+=3600
        for _ in range(3):self.assertEqual(self.action(),'NONE')
        self.assertEqual(len(self.order_calls),before);self.assertEqual(len(self.posts),1)
        self.assertEqual(self.events.count('PARTIAL_DETACH_UNRESOLVED'),1)
        self.parent.update(attachAlgoOrds=[],uTime='100001')
        self.assertTrue(self.track([self.parent]))
        self.assertEqual(self.c['partialDetach']['phase'],'CONFIRMED')
        self.assertEqual(self.action(),'ACTION');self.complete_cancel();self.assertEqual(self.action(),'ACTION')
        self.assertEqual(self.history_calls,[])
    def test_delayed_matching_ws_witness_after_later_rest_fill(self):
        self.action();self.c['filledBuy']='3';self.save(self.st)
        event=dict(self.parent,attachAlgoOrds=[],uTime='100001',
                   reqId=self.c['partialDetach']['reqId'],amendResult='0')
        self.assertTrue(self.ns['remember_partial_detach_event'](self.st,event))
        self.assertEqual(self.c['filledBuy'],'3')
        self.restart();self.parent.update(state='filled',accFillSz='4',uTime='100002',attachAlgoOrds=[])
        self.assertTrue(self.track());self.assertEqual(self.c['state'],'VIRTUAL_TP')
        self.assertFalse(self.c.get('partial_cleanup'));self.assertIn('BUY_FILLED',self.events)
        self.assertEqual(len(self.posts),1)
    def test_terminal_canceled_with_full_quantity_returns_normal_virtual(self):
        self.action();self.detached();self.action()
        self.parent.update(state='canceled',accFillSz='4',uTime='100002')
        self.assertTrue(self.track());self.assertFalse(self.c.get('partial_cleanup'))
        self.assertEqual(self.c['state'],'VIRTUAL_TP');self.assertEqual(self.action(qty='4'),'NONE')
        self.assertEqual(len(self.posts),2)
    def test_definite_reject_does_not_cancel_or_market(self):
        self.ack='reject';self.assertEqual(self.action(),'NONE');self.restart();self.now+=60
        self.assertEqual(self.action(),'NONE');self.assertEqual(len(self.posts),1)
        self.assertEqual(self.c['partialDetach']['phase'],'REJECTED')
    def test_more_buy_fills_during_cancel_sell_actual_qty(self):
        self.action();self.detached();self.action();self.complete_cancel('3');self.action(qty='3')
        self.assertEqual(self.posts[-1][1]['sz'],'3');self.assertTrue(self.track())
        self.assertEqual(self.c['state'],'EXITED')
    def test_full_fill_before_detach_keeps_original_attached(self):
        self.action();self.parent.update(state='filled',accFillSz='4',uTime='100001')
        self.child={'instId':'TEST-USDT-SWAP','ordId':'child','clOrdId':'attached-client','side':'sell',
                    'state':'live','px':'0.3960','sz':'4','accFillSz':'0','reduceOnly':'true'}
        self.assertTrue(self.track([self.child]));self.assertFalse(self.c.get('partial_cleanup'))
        self.assertEqual(self.c['tpOrders'][0]['ordId'],'child');self.assertEqual(self.action(qty='4'),'NONE')
        self.assertEqual(len(self.posts),1)
    def test_full_fill_after_confirmed_detach_returns_normal_virtual(self):
        self.action();self.detached();self.action();self.parent.update(state='filled',accFillSz='4',uTime='100002')
        self.assertTrue(self.track());self.assertEqual(self.c['state'],'VIRTUAL_TP')
        self.assertFalse(self.c.get('partial_cleanup'));self.assertEqual(self.action(qty='4'),'NONE')
        self.ns['coverage_audit'](self.st,[],Decimal(4));self.assertEqual(len(self.posts),2)
    def test_ws_correlated_removal_then_terminal_rest_without_live_get(self):
        self.action();event=dict(self.parent,attachAlgoOrds=[],uTime='100001',
                                  reqId=self.c['partialDetach']['reqId'],amendResult='0')
        self.assertTrue(self.ns['remember_partial_detach_event'](self.st,event))
        self.assertEqual(self.c['state'],'BUY_PARTIAL');self.assertEqual(len(self.posts),1)
        self.restart();self.parent.update(state='filled',accFillSz='4',uTime='100002',attachAlgoOrds=[])
        self.assertTrue(self.track());self.assertEqual(self.c['state'],'VIRTUAL_TP')
        self.assertFalse(self.c.get('partial_cleanup'));self.assertEqual(len(self.posts),1)
    def test_ws_wrong_request_not_proof(self):
        self.action();event=dict(self.parent,attachAlgoOrds=[],uTime='100001',reqId='wrong',amendResult='0')
        self.assertFalse(self.ns['remember_partial_detach_event'](self.st,event))
        self.assertEqual(self.c['partialDetach']['phase'],'REQUESTED')
    def test_rest_live_observation_is_durable_before_cancel(self):
        self.action();self.detached();self.assertTrue(self.track([self.parent]))
        self.assertEqual(self.c['partialDetach']['phase'],'CONFIRMED');self.assertEqual(len(self.posts),1)
        self.restart();self.assertEqual(self.action(),'ACTION');self.complete_cancel()
    def test_terminal_without_confirmation_does_not_market(self):
        self.action();self.parent.update(state='filled',accFillSz='4',uTime='100001',attachAlgoOrds=[])
        # Strict result is false, but the exact isolated cycle no longer throws.
        self.assertFalse(self.track());self.assertEqual(self.c['state'],'TP_WAIT')
        rec=self.c['tpOrders'][0];self.assertEqual(rec['sz'],'4')
        self.assertFalse(rec['ordId']);self.assertIn('detachTerminalProof',rec)
        self.restart();isolated=set();poll={}
        self.assertTrue(self.track(isolation=isolated,reservation_poll=poll))
        self.assertEqual(isolated,{'attached-client'})
        self.assertEqual(self.events.count('PARTIAL_DETACH_TERMINAL_UNRESOLVED'),1)
        pos=[{'instId':'TEST-USDT-SWAP','posSide':'net','pos':'4'}]
        self.assertTrue(self.ns['isolated_partial_coverage'](self.st,[],pos,Decimal(4),isolated))
        self.assertFalse(self.ns['isolated_partial_coverage'](self.st,[],pos,Decimal(3),isolated))
        before=len(self.order_calls)
        self.assertTrue(self.track(isolation=isolated,reservation_poll=poll))
        self.assertEqual(len(self.order_calls),before)
        self.now+=3600
        self.assertTrue(self.track(isolation=isolated,reservation_poll=poll))
        self.assertEqual(len(self.order_calls),before) # no periodic query after diagnostic window
        self.assertEqual(self.history_calls,[]);self.assertEqual(len(self.posts),1)
    def test_terminal_contradiction_never_market(self):
        self.action();self.detached();self.action();self.parent.update(state='canceled',uTime='100002')
        del self.parent['attachAlgoOrds']
        isolated=set();self.assertTrue(self.track(isolation=isolated,reservation_poll={}))
        self.assertEqual(isolated,{'attached-client'})
        self.assertEqual(self.ns['partial_cleanup_action'](self.st,Decimal('.4'),Decimal(2),'RUN',
                         isolated=isolated,reserved_qty=Decimal(2)),'NONE')
        self.assertEqual(len(self.posts),2);self.assertEqual(self.history_calls,[])
    def test_panic_cancel_before_confirmation_terminal_isolation(self):
        self.action();self.mode='PANIC';self.c['state']='CANCELING';self.save(self.st)
        self.parent.update(state='canceled',uTime='100002',attachAlgoOrds=[])
        isolated=set();self.assertTrue(self.track(isolation=isolated,reservation_poll={}))
        other={'cycleId':'other','state':'VIRTUAL_TP','buyPx':'0.4','tpPx':'0.4006','sz':'4',
               'ordId':'other-buy','clOrdId':'other-cid','filledBuy':'4','filledTP':'0','tpOrders':[]}
        self.st['cycles']['0.4']=other
        pos=[{'instId':'TEST-USDT-SWAP','posSide':'net','pos':'6'}]
        self.assertTrue(self.ns['isolated_partial_coverage'](self.st,[],pos,Decimal(6),isolated,emergency=True))
        self.assertEqual(self.c['tpOrders'][0]['sz'],'2');self.assertEqual(len(self.posts),1)
    def test_late_correlated_ws_recovers_quarantine(self):
        self.action();self.parent.update(state='canceled',uTime='100003',attachAlgoOrds=[])
        self.assertFalse(self.track());self.restart()
        event=dict(self.parent,state='partially_filled',uTime='100002',
                   reqId=self.c['partialDetach']['reqId'],amendResult='0')
        self.assertTrue(self.ns['remember_partial_detach_event'](self.st,event))
        self.restart();self.assertTrue(self.track());self.assertEqual(self.c['state'],'VIRTUAL_TP')
        self.assertEqual(self.c['tpOrders'][0]['resolution'],'CONFIRMED_DETACHMENT')
        self.assertEqual(self.action(),'ACTION');self.assertEqual(self.posts[-1][1]['sz'],'2')
        self.assertTrue(self.track());self.assertEqual(self.c['state'],'EXITED')
        self.assertEqual(self.history_calls,[])
    def test_late_child_discovery_wins_over_late_ws(self):
        self.action();self.parent.update(state='canceled',uTime='100003',attachAlgoOrds=[])
        self.assertFalse(self.track());event=dict(self.parent,state='partially_filled',uTime='100002',
                   reqId=self.c['partialDetach']['reqId'],amendResult='0')
        self.assertTrue(self.ns['remember_partial_detach_event'](self.st,event))
        self.child={'instId':'TEST-USDT-SWAP','ordId':'child','clOrdId':'attached-client','side':'sell',
                    'state':'filled','px':'0.3960','sz':'2','accFillSz':'2','reduceOnly':'true'}
        self.assertTrue(self.track());self.assertEqual(self.c['state'],'EXITED')
        self.assertEqual(len(self.posts),1);self.assertEqual(self.history_calls,[])
    def test_invalid_terminal_identity_does_not_get_isolation(self):
        self.action();self.parent.update(state='canceled',uTime='100003',attachAlgoOrds=[],clOrdId='wrong')
        with self.assertRaises(Unresolved):self.track(isolation=set(),reservation_poll={})
        self.assertEqual(self.c['state'],'BUY_PARTIAL');self.assertEqual(len(self.posts),1)
    def test_unknown_extra_attachment_never_gets_isolation(self):
        self.action();self.parent.update(state='canceled',uTime='100003')
        self.parent['attachAlgoOrds'][0]['attachAlgoId']='different'
        with self.assertRaises(Unresolved):self.track(isolation=set(),reservation_poll={})
        self.assertEqual(self.c['state'],'BUY_PARTIAL');self.assertEqual(len(self.posts),1)
    def test_market_lost_ack_no_repeat(self):
        self.action();self.detached();self.action();self.complete_cancel();self.ack='market-lost'
        with self.assertRaises(Unresolved):self.action()
        self.restart();self.assertEqual(self.action(),'WAIT');self.assertEqual(len(self.posts),3)
    def test_mode_change_no_amend_or_cancel(self):
        self.action();self.detached();self.mode='PAUSE';self.assertEqual(self.action(),'NONE')
        self.assertEqual(len(self.posts),1)
    def test_wrong_parent_never_amend(self):
        self.parent['clOrdId']='different'
        with self.assertRaises(Unresolved):self.action()
        self.assertEqual(self.posts,[])
    def test_unreserved_qty_guard_remains(self):
        self.action();self.detached();self.action();self.complete_cancel()
        with self.assertRaises(Unresolved):self.action(qty='1')
        self.assertEqual(len(self.posts),2)

if __name__=='__main__':unittest.main(verbosity=2)
