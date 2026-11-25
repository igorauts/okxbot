"""ENSO 2026-11-25 regression. AST only; no real API, files, or services."""
import ast
import contextlib
import copy
import json
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from tests.test_live_loop_chaos_rc4 import Harness, StopSimulation, FakeLatency
from okx_strategy import build_grid_rules

ROOT=Path(__file__).resolve().parents[1]
NAMES={'D','track','tp_records','cycle_sell_fill','remaining_tp','active_tp_records',
       'virtual_cycles','unprotected_partial_buy','unresolved_aggregate','real_tp_active',
       'active_buy','count','parent_tp_failure','partial_attached_isolatable',
       'isolated_partial_coverage','tp_record_update','verify_existing',
       'register_cancel_attached','resolve_partial_cancel','reconcile_position',
       'record_attached_tp_failure','safe_tp_during_buy_wait','partial_cleanup_action',
       'controlled_shutdown','partial_cleanup_due','virtual_escape','maintain_virtual_tp','coverage_audit','audit_once','tp_failure_buy_hold'}
TREE=ast.parse((ROOT/'okx_bot.py').read_text())
CODE=compile(ast.Module(body=[n for n in TREE.body if isinstance(n,ast.FunctionDef)
                            and n.name in NAMES],type_ignores=[]),'<offline-engine>','exec')
INST='ENSO-USDT-SWAP'
CID='enso-OX4B1795596369856ed6ef1a3'
TPID='OX4T17955963698564944c213'
BUYID='4130348242148536321'

class Temporary(Exception):pass
class Unresolved(Exception):pass

def fixture():
    problem={'state':'TP_WAIT','buyPx':'0.3954','tpPx':'0.3960','sz':'4',
             'cycleId':CID,'clOrdId':'OX4B1795596369856ed6ef1a3','ordId':BUYID,
             'tpClOrdId':TPID,'tpOrdId':'','filledBuy':'2','filledTP':'0',
             'partial_cleanup':True,'partial_cleanup_at':1795596770.6270394,
             'cancelAt':1795596770.645008,
             'tpOrders':[{'clOrdId':TPID,'ordId':'','sz':'2','filled':'0',
                          'phase':'PENDING','source':'attached','placedAt':1795596770.7686288}]}
    parent={'instId':INST,'ordId':BUYID,'clOrdId':problem['clOrdId'],'side':'buy',
            'state':'canceled','accFillSz':'2','sz':'4','px':'0.3954','cancelSource':'1',
            'posSide':'net','reduceOnly':'false','ordType':'post_only',
            'attachAlgoOrds':[{'attachAlgoClOrdId':TPID,'attachAlgoId':'4130348242148536322',
                              'tpOrdKind':'limit','tpOrdPx':'0.396','failCode':'','failReason':'',
                              'sz':'','percent':'','slTriggerPx':'','slOrdPx':''}]}
    st={'cycles':{'0.3954':problem},'completed':6083}
    pending=[]
    # 152 ordinary virtual cycles, one historical partial SELL (remaining 3),
    # two real TP cycles of four: exact 611 virtual + 8 real + 2 unknown = 621.
    for i in range(155):
        price=str(Decimal('0.4200')+Decimal('0.0006')*i)
        tp=str(Decimal(price)+Decimal('0.0006'))
        c={'cycleId':f'normal-{i}','state':'VIRTUAL_TP','buyPx':price,'tpPx':tp,
           'sz':'4','ordId':f'buy-{i}','clOrdId':f'cbuy-{i}','filledBuy':'4','filledTP':'0',
           'tpOrders':[]}
        if i==145:
            c['filledTP']='1';c['tpOrders']=[{'phase':'CANCELED','source':'attached',
                'ordId':'old-partial-sell','clOrdId':'cold-partial-sell','sz':'4','filled':'1'}]
        if i>=153:
            c['state']='TP_WAIT';r={'phase':'ACTIVE','source':'recovery','ordId':f'sell-{i}',
                'clOrdId':f'csell-{i}','sz':'4','filled':'0'};c['tpOrders']=[r]
            pending.append({'instId':INST,'ordId':r['ordId'],'clOrdId':r['clOrdId'],
                'side':'sell','state':'live','px':tp,'sz':'4','accFillSz':'0','reduceOnly':'true'})
        st['cycles'][price]=c
    return st,parent,pending

class Cases(unittest.TestCase):
    def setUp(self):
        self.st,self.parent,self.pending=fixture();self.pos=Decimal(621);self.mode='RUN'
        self.child=None;self.extra={};self.logs=[];self.sells=[];self.reqs=[]
        def order(oid='',cid=''):
            if oid==BUYID:return self.parent
            if cid==TPID or (self.child and oid==self.child['ordId']):return self.child
            return self.extra.get(oid or cid)
        def pos():return [{'instId':INST,'posSide':'net','pos':str(self.pos)}]
        def fail(s):raise RuntimeError(s)
        def req(*args,**kwargs):self.fail('unexpected exchange request')
        self.ns={'Decimal':Decimal,'INST':INST,'COIN':'ENSO','VER':'test','json':json,'time':time,'sys':__import__('sys'),
                 'A':SimpleNamespace(order=order,pending=lambda:self.pending,positions=pos,req=req),
                 '_PERF_LOCAL':threading.local(),'ExchangeTemporary':Temporary,
                 'ExchangeUnresolved':Unresolved,'REDUCE_ONLY_HARD_CAP':150,'REAL_TP_TARGET':3,
                 'reconcile_aggregate':lambda st:True,'reconcile_panic_orders':lambda *a:False,
                 'paged_order':lambda cid:None,'_batch_confirmed_facts':lambda st:contextlib.nullcontext(),
                 'post_absence_confirmed':lambda *a:False,'save':lambda *a:None,
                 'log':lambda event,**kw:self.logs.append(event),'journal_tp':lambda *a,**kw:None,
                 'trip_if_margin_cancel':lambda *a:None,'margin_rejection':lambda c:False,
                 'note_cleanup_fill':lambda:None,'die':fail,'panic_executions':lambda st:Decimal(0),
                 'control_mode':lambda:self.mode,'tp_quantity_placeable':lambda qty:qty>0,
                 'place_virtual_tp':lambda st,c:self.sells.append(c['cycleId']) or True,
                 'control_ack':lambda *a,**k:None,'PARTIAL_AGE':300,'PARTIAL_STEPS':5,
                 'STEP':Decimal('.0006'),'ESCAPE_COALESCE_SEC':.1,
                 'LATENCY':FakeLatency(),'newid':lambda kind:'aggregate-id',
                 'new_request_deadline_ms':lambda:1795619000000,
                 'CoverageMismatch':Temporary,
                 'OrderExpiredBeforeSend':type('OrderExpiredBeforeSend',(Exception,),{}),
                 'REAL_TP_MAX':100,'BUY_MAX':8,'TP_ACTIONS_PER_PASS':5,
                 'TP_BUFFER_COOLDOWN':12,'CLEANUP_LAST_FILL_AT':time.monotonic(),
                 'virtual_escape':lambda *a:self.fail('unexpected market exit'),
                 'market_exit_partial':lambda *a:self.fail('unexpected partial exit')}
        exec(CODE,self.ns)
    @property
    def rec(self):return self.st['cycles']['0.3954']['tpOrders'][0]
    def pass_(self,**kwargs):
        out=set();result=self.ns['reconcile_position'](self.st,timeout=0,run_isolation=out,**kwargs)
        return result,out
    def test_exact_enso_621_reservation_and_restart(self):
        original=copy.deepcopy(self.rec)
        result,out=self.pass_();self.assertEqual(result,(self.pending,False,Decimal(621)))
        self.assertEqual(out,{id(self.rec)})
        self.assertEqual(self.rec['phase'],'PENDING');self.assertEqual(self.rec['ordId'],'')
        self.assertEqual(self.rec['sz'],'2');self.assertNotIn('failCode',self.rec)
        self.assertEqual(self.ns['count'](self.st),156)
        self.assertEqual(self.ns['safe_tp_during_buy_wait'](self.st,self.pending,self.pos),'TP_LIMIT_RESERVED')
        self.st=json.loads(json.dumps(self.st));result,out=self.pass_()
        self.assertEqual(out,{id(self.rec)});self.assertEqual(result[1],False)
        self.assertEqual(self.rec['clOrdId'],original['clOrdId'])
    def test_upgrade_audit_explicit_isolation_and_strict_default(self):
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.ns['audit_once'](self.st,allow_partial_isolation=True),'ISOLATED')
        self.assertIn('RESERVED unknown TP contracts: 2',out.getvalue())
        self.assertIn('TP links resolved: False',out.getvalue())
        self.assertIn('AUDIT ISOLATED COVERAGE PASS',out.getvalue())
        self.assertNotIn('AUDIT CONSISTENT',out.getvalue())
        self.assertEqual(self.rec['phase'],'PENDING')
        with self.assertRaises(RuntimeError):self.ns['audit_once'](self.st)
    def test_upgrade_audit_rejects_incomplete_or_nonrun(self):
        self.mode='PANIC'
        with self.assertRaises(RuntimeError):self.ns['audit_once'](self.st,allow_partial_isolation=True)
        self.mode='RUN'
        row=self.pending.pop();self.extra[row['ordId']]=row
        with self.assertRaises(RuntimeError):self.ns['audit_once'](self.st,allow_partial_isolation=True)
    def test_upgrade_audit_resolved_uses_strict_coverage(self):
        import io
        self.parent['attachAlgoOrds'][0]['failCode']='51006'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.ns['audit_once'](self.st,allow_partial_isolation=True),'STRICT')
        self.assertIn('AUDIT CONSISTENT',out.getvalue())
        self.assertIn('RESERVED unknown TP contracts: 0',out.getvalue())
    def test_strict_audit_and_emergency_stay_unresolved(self):
        self.assertFalse(self.ns['reconcile_position'](self.st,timeout=0)[1])
        for mode in ('HARD','PANIC','PAUSE','SOFT'):
            self.mode=mode;result,out=self.pass_();self.assertFalse(result[1]);self.assertFalse(out)
        self.assertFalse(self.ns['controlled_shutdown'](self.st,self.pending,self.pos,False,'PANIC'))
    def test_position_mismatch_and_invalid_position_block(self):
        self.pos=Decimal(619)
        with self.assertRaises(Unresolved):self.pass_()
        self.assertFalse(self.pass_(allow_mismatch=True)[1])
        self.pos=Decimal(621);self.ns['A'].positions=lambda:[{'instId':INST,'posSide':'long','pos':'621'}]
        self.assertFalse(self.pass_()[1])
    def test_incomplete_snapshot_even_if_exact_active_get_returns_order(self):
        row=self.pending.pop();self.extra[row['ordId']]=row
        self.assertFalse(self.pass_()[1])
    def test_duplicate_pending_and_foreign_identity(self):
        self.pending.append(copy.deepcopy(self.pending[0]));self.assertFalse(self.pass_()[1])
        self.pending.pop();self.pending.append(dict(self.pending[0],ordId='foreign',clOrdId='foreign'))
        with self.assertRaisesRegex(RuntimeError,'Unrecognized pending'):self.pass_()
    def test_other_unknown_buy_blocks(self):
        self.st['cycles']['0.30']={'state':'PLACING','cycleId':'unknown-buy','clOrdId':'unknown',
             'sz':'4','filledBuy':'0','filledTP':'0','buyPx':'0.30','tpPx':'0.31','tpOrders':[]}
        self.assertFalse(self.pass_()[1])
    def test_parent_evidence_must_be_fresh_and_exact(self):
        parent=copy.deepcopy(self.parent)
        for key,value in [('state','filled'),('cancelSource','31'),('posSide','long'),('sz','5'),('px','0.3955')]:
            self.parent=copy.deepcopy(parent);self.parent[key]=value
            self.assertFalse(self.pass_()[1],key)
        self.parent=copy.deepcopy(parent);self.parent['attachAlgoOrds']=[]
        self.assertFalse(self.pass_()[1])
        self.parent=None
        with self.assertRaises(Unresolved):self.pass_()
    def test_missing_or_duplicate_child_identity_cannot_isolate(self):
        self.parent['attachAlgoOrds'][0]['attachAlgoId']=''
        self.assertFalse(self.pass_()[1])
        self.parent['attachAlgoOrds'][0]['attachAlgoId']='a'
        self.st['cycles'][next(k for k in self.st['cycles'] if k!='0.3954')]['clOrdId']=TPID
        self.assertFalse(self.pass_()[1])
    def test_nonmatching_sell_size_and_bad_fill_proof(self):
        self.pending[0]['sz']='5'
        with self.assertRaisesRegex(RuntimeError,'TP size changed'):self.pass_()
    def test_late_child_live_and_filled_resolves_not_duplicates(self):
        self.assertTrue(self.pass_()[1]);self.child={'instId':INST,'ordId':'late-real-child',
            'clOrdId':TPID,'side':'sell','state':'live','px':'0.3960','sz':'2',
            'accFillSz':'0','reduceOnly':'true'}
        self.pending.append(self.child);result,out=self.pass_()
        self.assertTrue(result[1]);self.assertFalse(out);self.assertEqual(self.rec['phase'],'ACTIVE')
        self.pending.pop();self.child.update(state='filled',accFillSz='2');self.pos=Decimal(619)
        result,out=self.pass_();self.assertTrue(result[1]);self.assertFalse(out)
        self.assertEqual(self.st['cycles']['0.3954']['state'],'EXITED')
        self.assertEqual(self.rec['filled'],'2');self.assertEqual(self.st['partial_exits'],1)
    def test_late_child_partial_fill_updates_reserve_through_strict_path(self):
        self.child={'instId':INST,'ordId':'late-real-child','clOrdId':TPID,'side':'sell',
                    'state':'partially_filled','px':'0.3960','sz':'2','accFillSz':'1','reduceOnly':'true'}
        self.pending.append(self.child);self.pos=Decimal(620)
        result,out=self.pass_();self.assertTrue(result[1]);self.assertFalse(out)
        self.assertEqual(self.rec['phase'],'ACTIVE');self.assertEqual(self.rec['filled'],'1')
    def test_exchange_rejection_still_uses_existing_failure_path(self):
        self.parent['attachAlgoOrds'][0]['failCode']='51006'
        result,out=self.pass_();self.assertTrue(result[1]);self.assertFalse(out)
        self.assertEqual(self.rec['phase'],'FAILED')
        self.assertEqual(self.st['cycles']['0.3954']['state'],'VIRTUAL_TP')
    def test_only_other_virtual_limit_may_restore(self):
        self.ns['REAL_TP_TARGET']=4;self.assertTrue(self.pass_()[1])
        self.assertEqual(self.ns['safe_tp_during_buy_wait'](self.st,self.pending,self.pos),'RESTORE')
        self.assertEqual(len(self.sells),1);self.assertNotIn(CID,self.sells)
        self.assertEqual(self.rec['phase'],'PENDING')
    def test_independent_partial_cleanup_excludes_reserved_cycle(self):
        result,out=self.pass_();self.assertTrue(out)
        other=self.st['cycles']['0.5070'];other['partial_cleanup']=True
        exits=[];self.ns['market_exit_partial']=lambda st,c,q:exits.append((c['cycleId'],q)) or True
        fn=self.ns['partial_cleanup_action']
        self.assertEqual(fn(self.st,Decimal('.4131'),self.pos,'RUN',isolated=out,reserved_qty=Decimal(2)),'ACTION')
        self.assertEqual(exits,[(other['cycleId'],Decimal(3))]);self.assertEqual(self.rec['phase'],'PENDING')
        with self.assertRaisesRegex(RuntimeError,'unreserved position'):
            fn(self.st,Decimal('.4131'),Decimal(4),'RUN',isolated=out,reserved_qty=Decimal(2))
    def test_independent_aggregate_market_never_allocates_reserved_two(self):
        for row in self.pending:
            c=next(c for c in self.st['cycles'].values() if any(r.get('ordId')==row['ordId'] for r in c['tpOrders']))
            c['tpOrders'][0]['phase']='CANCELED';c['state']='VIRTUAL_TP'
        self.pending.clear();result,out=self.pass_();self.assertTrue(out)
        fn=self.ns['virtual_escape'];self.st['escape_collection']={'startedAt':time.time()-1,
             'cycles':[c['cycleId'] for _,c in self.ns['virtual_cycles'](self.st)]}
        posts=[]
        self.ns['A'].req=lambda method,route,**kw:posts.append((method,route,kw)) or [{'sCode':'0','ordId':'agg-order'}]
        with self.assertRaisesRegex(RuntimeError,'unreserved position'):
            fn(copy.deepcopy(self.st),Decimal(1),Decimal(620),[],'RUN',reserved_qty=Decimal(2))
        self.assertEqual(posts,[])
        self.assertEqual(fn(self.st,Decimal(1),self.pos,[],'RUN',reserved_qty=Decimal(2)),'ACTION')
        self.assertEqual(len(posts),1);self.assertEqual(posts[0][2]['body']['sz'],'619')
        self.assertTrue(posts[0][2]['body']['reduceOnly'])
        self.assertNotIn(CID,[r['cycleId'] for r in self.st['aggregate_exits'][0]['plan']])
        self.assertEqual(self.rec['phase'],'PENDING')
    def test_maintenance_reservation_counts_in_target(self):
        result,out=self.pass_();self.assertTrue(out)
        self.ns['REAL_TP_TARGET']=4
        self.assertEqual(self.ns['maintain_virtual_tp'](self.st,self.pending,self.pos,Decimal('.4131'),
                         reserved_qty=Decimal(2),reserved_count=1),'RESTORE')
        self.assertEqual(len(self.sells),1);self.assertNotIn(CID,self.sells)
        self.ns['REAL_TP_TARGET']=3;self.sells.clear()
        self.assertEqual(self.ns['maintain_virtual_tp'](self.st,self.pending,self.pos,Decimal('.4131'),
                         tp_buffer=SimpleNamespace(observe=lambda *a:None,quiet_since=time.monotonic(),overflow_notified=False),
                         reserved_qty=Decimal(2),reserved_count=1),'NONE')
        self.assertFalse(self.sells)
    def live_case(self,*,ws=True,cap=200,mode='RUN'):
        h=Harness(allow_buy=True);h.state=self.st;h.mode=mode;self.mode=mode
        h.max_reconciles=1;h.watcher._healthy=ws;h.market.get_bid=lambda:Decimal('0.4131')
        def reconcile(*a,**kw):
            h.reconciles+=1
            if h.reconciles>1:raise StopSimulation()
            return self.ns['reconcile_position'](*a,**kw)
        def place(st,px):self.reqs.append(px);h.buy_calls+=1;return True
        level_free=build_grid_rules(STEP=Decimal('.0006'),TARGET=3,PRICE_STEP=Decimal('.0001'),
            BUY_CANCEL_PROTECT_STEPS=5,REAL_TP_TARGET=3,CONTRACT_MIN=Decimal(1),CONTRACT_LOT=Decimal(1),
            D=self.ns['D'],fmt=lambda p:f'{p:.4f}',die=self.ns['die'],ExchangeUnresolved=Unresolved)[0]
        h.env.update(reconcile_position=reconcile,active_buy=self.ns['active_buy'],
            real_tp_active=self.ns['real_tp_active'],tp_records=self.ns['tp_records'],
            count=self.ns['count'],level_free=level_free,place=place,
            desired_buy_levels=lambda *a:[Decimal('.3954'),Decimal('.4122')],
            missing_buy_levels_for_addition=lambda desired,active:desired,
            safe_tp_during_buy_wait=self.ns['safe_tp_during_buy_wait'],
            coverage_audit=lambda *a:self.fail('strict coverage must not be bypassed by fake result'),
            partial_cleanup_action=self.ns['partial_cleanup_action'],
            virtual_escape=self.ns['virtual_escape'],
            maintain_virtual_tp=lambda *a,**kw:'NONE',
            required_safe_tp=lambda *a:Decimal('0'),TP=Decimal('.0006'),
            CAP=cap,TARGET=3,REAL_TP_TARGET=3,BUY_MAX=8,
            controlled_shutdown=lambda st,pending,actual,resolved,mode,*a:
                 self.assertFalse(resolved) or False)
        h.run();return h
    def test_actual_live_loop_buy_only_other_level(self):
        h=self.live_case();self.assertEqual(h.buy_calls,1);self.assertEqual(self.reqs,[Decimal('.4122')])
        self.assertIn('PARTIAL_ATTACHED_TP_ISOLATED',[e[0][0] for e in h.logs])
        self.assertEqual(self.rec['phase'],'PENDING')
    def test_actual_live_loop_ws_and_grid_cap_still_block(self):
        self.assertEqual(self.live_case(ws=False).buy_calls,0)
        self.assertEqual(self.live_case(cap=156).buy_calls,0)
    def test_actual_live_loop_panic_does_not_gain_permission(self):
        self.assertEqual(self.live_case(mode='PANIC').buy_calls,0)

if __name__=='__main__':unittest.main(verbosity=2)
