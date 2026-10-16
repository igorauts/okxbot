#!/usr/bin/env python3
"""Offline crash-recovery checks using AST-extracted v075 functions.

No engine module import, .env access, LIVE state writes, or network API.
Requires stress_v075_reconcile.py and v075 RC1 engine files in same folder.
"""
import ast
import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

import stress_v075_reconcile as replay

class CrashRecovery(unittest.TestCase):
    def prepare(self, inst, n, scenario, root):
        st, buys, sells, pending, position = replay.fixture(inst,n,0,scenario)
        storage=Path(root)/'bot04_state.json'
        env,api,counts=replay.load_engine('075',storage,inst,buys,sells,pending,position)
        env['atomic_json'](storage,st)
        return st,storage,env,api,counts

    def replay_from_disk(self, inst, storage, buys, sells, pending, pos):
        engine,api,counts=replay.load_engine('075',storage,inst,buys,sells,pending,pos)
        durable=json.loads(storage.read_text())
        pending_ret,resolved,actual=engine['reconcile_position'](durable)
        self.assertTrue(resolved)
        self.assertEqual(actual,pos)
        self.assertEqual(pending_ret,pending)
        self.assertEqual(json.loads(storage.read_text()),durable)
        return durable,api

    def test_killed_mid_fill_batch_145(self):
        """SIGKILL-style crash means Python finally/flush never gets to run."""
        for inst in ('ETH-USDT-SWAP','SOL-USDT-SWAP'):
            with self.subTest(inst=inst), tempfile.TemporaryDirectory(prefix='v075_crash_') as tmp:
                st,buys,sells,pending,position=replay.fixture(inst,145,0,'closed')
                storage=Path(tmp)/'bot04_state.json'
                env,api,_=replay.load_engine('075',storage,inst,buys,sells,pending,position)
                env['atomic_json'](storage,st)
                initial=json.loads(storage.read_text())
                order_get=api.order
                class Interrupted(BaseException): pass
                def after_70_reads(*args,**kwargs):
                    if api.order_reads>=70:
                        raise Interrupted('process terminated before deferred fsync')
                    return order_get(*args,**kwargs)
                api.order=after_70_reads
                env['_PERF_LOCAL'].deferred_state=st
                env['_PERF_LOCAL'].deferred_dirty=False
                with self.assertRaises(Interrupted):
                    env['track'](st,pending)
                self.assertTrue(env['_PERF_LOCAL'].deferred_dirty)
                self.assertEqual(json.loads(storage.read_text()),initial,
                                 'Exchange facts were unexpectedly half-persisted')
                # Drop the entire process-like state, reconstruct from durable JSON.
                recovered,rest_api=self.replay_from_disk(inst,storage,buys,sells,pending,position)
                self.assertEqual(recovered['completed'],145)
                self.assertTrue(all(x['state']=='CLOSED' for x in recovered['cycles'].values()))
                self.assertEqual(rest_api.position_reads,1)
                print('PASS mid-batch crash; 145 cycles replayed, no POST:',inst,flush=True)

    def test_buy_ack_and_timeout(self):
        for inst in ('ETH-USDT-SWAP','SOL-USDT-SWAP'):
            for mode in ('ack_before_persist','timeout_unknown'):
                with self.subTest(inst=inst,mode=mode), tempfile.TemporaryDirectory(prefix='v075_buy_') as tmp:
                    st,storage,env,api,_=self.prepare(inst,0,'closed',tmp)
                    st['version']='0.7.5'
                    env['atomic_json'](storage,st)
                    engine=ast.parse((Path(__file__).resolve().parent.parent/'okx_bot075.py').read_text())
                    node=next(x for x in engine.body if isinstance(x,ast.FunctionDef) and x.name=='place')
                    exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual place>', 'exec'),env)
                    env.update({'fmt':lambda x:f'{x:.2f}', 'TP':Decimal('2.75' if inst.startswith('ETH') else '0.13'),
                                'QTY':Decimal('0.003' if inst.startswith('ETH') else '0.04'),
                                'newid':lambda prefix:prefix+'unique007',
                                'new_request_deadline_ms':lambda:2000000000000,
                                'margin_rejection':lambda c:False,
                                'trip_account_soft_stop':lambda *a:None,
                                'OrderExpiredBeforeSend':type('OrderExpiredBeforeSend',(RuntimeError,),{})})
                    submits=[]
                    def trade_req(method,path,**kw):
                        submits.append(copy.deepcopy((method,path,kw)))
                        if mode=='timeout_unknown':raise TimeoutError('ACK lost')
                        return [{'sCode':'0','ordId':'order-from-okx'}]
                    api.req=trade_req
                    price=Decimal('2400.00' if inst.startswith('ETH') else '102.00')
                    if mode=='timeout_unknown':
                        with self.assertRaises(TimeoutError):env['place'](st,price)
                    else:
                        self.assertTrue(env['place'](st,price))
                    self.assertEqual(len(submits),1)
                    durable=json.loads(storage.read_text())
                    self.assertEqual(len(durable['cycles']),1)
                    persisted_buy=next(iter(durable['cycles'].values()))
                    self.assertEqual(persisted_buy['state'],'PLACING')
                    # GET sees the original BUY using exact durable clOrdId.
                    buy={'instId':inst,'side':'buy','state':'live','accFillSz':'0',
                         'ordId':'order-from-okx','clOrdId':persisted_buy['clOrdId'],
                         'sz':persisted_buy['sz']}
                    restored,api2=self.replay_from_disk(inst,storage,[buy],[],[buy],Decimal(0))
                    self.assertEqual(next(iter(restored['cycles'].values()))['state'],'BUY_LIVE')
                    self.assertEqual(api2.position_reads,1)
                    print('PASS BUY recovery via clOrdId, 1 POST only:',inst,mode,flush=True)

    def test_cancel_ack_lost_zero_or_partial(self):
        for inst in ('ETH-USDT-SWAP','SOL-USDT-SWAP'):
            for outcome in ('canceled_zero','canceled_partial'):
                with self.subTest(inst=inst,outcome=outcome), tempfile.TemporaryDirectory(prefix='v075_cancel_') as tmp:
                    q='0.003' if inst.startswith('ETH') else '0.04'
                    filled='0' if outcome=='canceled_zero' else ('0.001' if inst.startswith('ETH') else '0.02')
                    st,storage,env,api,_=self.prepare(inst,0,'closed',tmp)
                    px='2400.00' if inst.startswith('ETH') else '102.00'
                    tp='2402.75' if inst.startswith('ETH') else '102.13'
                    cycle={'state':'BUY_LIVE','buyPx':px,'tpPx':tp,'sz':q,'filledBuy':'0','filledTP':'0',
                           'cycleId':'cycle-cancel-001','tpOrders':[],'clOrdId':'CB-cancel-1',
                           'tpClOrdId':'CS-cancel-1','ordId':'B-cancel-1','tpOrdId':''}
                    st['cycles'][px]=cycle
                    env['atomic_json'](storage,st)
                    parser=ast.parse((Path(__file__).resolve().parent.parent/'okx_bot075.py').read_text())
                    node=next(x for x in parser.body if isinstance(x,ast.FunctionDef) and x.name=='cancel_lowest_specific')
                    exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual cancel>', 'exec'),env)
                    submissions=[]
                    def cancel_req(method,path,**kw):
                        submissions.append((method,path))
                        raise TimeoutError('cancel ACK lost')
                    api.req=cancel_req
                    with self.assertRaises(TimeoutError):env['cancel_lowest_specific'](st,cycle)
                    self.assertEqual(submissions,[('POST','/api/v5/trade/cancel-order')])
                    self.assertEqual(json.loads(storage.read_text())['cycles'][px]['state'],'CANCELING')
                    buy={'instId':inst,'side':'buy','ordId':'B-cancel-1','clOrdId':'CB-cancel-1',
                         'state':'canceled','accFillSz':filled,'sz':q}
                    sell_orders=[]; pending=[]
                    if outcome=='canceled_partial':
                        sell={'instId':inst,'side':'sell','ordId':'S-partial-1','clOrdId':'CS-cancel-1',
                              'px':tp,'state':'live','accFillSz':'0','sz':filled,'reduceOnly':'true'}
                        sell_orders=[sell];pending=[sell]
                    restored,rest_api=self.replay_from_disk(inst,storage,[buy],sell_orders,pending,Decimal(filled))
                    final=restored['cycles'][px]
                    if outcome=='canceled_zero':self.assertEqual(final['state'],'CANCELED')
                    else:
                        self.assertEqual(final['state'],'TP_WAIT')
                        self.assertEqual(final['filledBuy'],filled)
                        self.assertEqual(final['tpOrders'][0]['ordId'],'S-partial-1')
                    print('PASS cancel uncertain recovered by exact IDs:',inst,outcome,flush=True)

if __name__=='__main__':
    unittest.main(verbosity=2)
