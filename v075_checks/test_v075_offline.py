#!/usr/bin/env python3
"""Offline regression tests extracting real engine functions by AST.

Does not import the module (no .env, live state or OKX connection).
"""
import ast
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from decimal import Decimal

HERE=Path(__file__).resolve().parent
OLD=ast.parse((HERE/'original_okx_bot074.py').read_text())
NEW=ast.parse((HERE.parent/'okx_bot075.py').read_text())

def node(tree,name):
    return next(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name==name)

def only(name,env,tree=NEW):
    n=node(tree,name)
    exec(compile(ast.Module(body=[n],type_ignores=[]),'<isolated real engine>', 'exec'),env)
    return env[name]

class IntegrityTests(unittest.TestCase):
    def test_unmodified_trade_safety_functions(self):
        for name in ('cancel_tp','cancel_lowest_specific','coverage_audit',
                     'verify_existing','tp_record_update','reconcile_aggregate',
                     'place_panic_sell','controlled_shutdown','virtual_escape'):
            self.assertEqual(ast.dump(node(OLD,name),include_attributes=False),
                             ast.dump(node(NEW,name),include_attributes=False),name)

    def test_safety_limits_and_fsynchronization(self):
        old=(HERE/'original_okx_bot074.py').read_text()
        new=(HERE.parent/'okx_bot075.py').read_text()
        for x in ('BUY_BUFFER = 5','BUY_CANCEL_PROTECT_STEPS = 5',
                  'REAL_TP_RESERVE = TARGET + BUY_BUFFER',
                  'REAL_TP_MAX = REAL_TP_TARGET + REAL_TP_RESERVE',
                  'TP_BUFFER_COOLDOWN = 1.5','TP_BUFFER_CANCEL_INTERVAL = 0.1',
                  'REDUCE_ONLY_HARD_CAP = 150', 'if method != \'GET\' and not LIVE:',
                  "st['cycles'][k]=x; save(st)  # durable intent BEFORE network request"):
            self.assertIn(x,new,x)
        self.assertIn('os.fsync(f.fileno())',new)
        self.assertIn('os.replace(tmp,path)',new)
        self.assertIn('os.fsync(dirfd)',new)
        self.assertEqual(new.count('time.sleep(0.05)  # v0.7.5: BUY pacing 50ms'),1)
        self.assertIn('def reconcile_position(st, timeout=30, pause=0.5',new)
        self.assertIn("json.dump(value, f, separators=(',', ':'), ensure_ascii=False)",new)
        self.assertNotIn('REAL_TP_MAX = 69',new)
        self.assertIn("VER = '0.7.5'",new)
        self.assertIn("VER = '0.7.4'",old)

    def make_batch(self, writer):
        env={'_PERF_LOCAL':threading.local(), '_PERF_RECON_ENABLED':False,
             'atomic_json':writer,'DATA':Path('fake_state'),
             'contextlib':__import__('contextlib')}
        for n in ('_durable_save','_flush_confirmed_facts','save','_batch_confirmed_facts'):
            only(n,env)
        return env

    def test_many_fills_one_durable_write(self):
        writes=[]
        env=self.make_batch(lambda path,st:writes.append(copy.deepcopy(st)))
        st={'cycles':{'A':{'state':'PLACING'}}}
        with env['_batch_confirmed_facts'](st):
            for i in range(145):
                st['cycles'][str(i)]={'state':'TP_WAIT','ordId':str(i)}
                env['save'](st)
            self.assertEqual(writes,[])
        self.assertEqual(len(writes),1)
        self.assertEqual(len(writes[0]['cycles']),146)
        env['save'](st)
        self.assertEqual(len(writes),2)

    def test_write_barrier_and_exception_flush(self):
        writes=[]
        env=self.make_batch(lambda path,st:writes.append(copy.deepcopy(st)))
        st={'cycles':[]}
        with env['_batch_confirmed_facts'](st):
            st['cycles'].append('filled');env['save'](st)
            env['_flush_confirmed_facts']() # write barrier before a POST
            self.assertEqual(writes,[{'cycles':['filled']}])
            st['cycles'].append('TP closed');env['save'](st)
        self.assertEqual(len(writes),2)
        with self.assertRaisesRegex(RuntimeError,'fake'):
            with env['_batch_confirmed_facts'](st):
                st['cycles'].append('new fill');env['save'](st)
                raise RuntimeError('fake exchange parsing error')
        self.assertEqual(writes[-1]['cycles'][-1],'new fill')
        self.assertIsNone(getattr(env['_PERF_LOCAL'],'deferred_state',None))

    def test_fail_closed_on_disk_error(self):
        env=self.make_batch(lambda path,st: (_ for _ in ()).throw(OSError('disk full')))
        st={'cycles':{}}
        with self.assertRaisesRegex(OSError,'disk full'):
            with env['_batch_confirmed_facts'](st):
                st['cycles']['B']={'state':'TP_WAIT'};env['save'](st)
        self.assertIsNone(getattr(env['_PERF_LOCAL'],'deferred_state',None))

    def test_thread_local_does_not_defer_unrelated_save(self):
        writes=[]
        env=self.make_batch(lambda p,s:writes.append(copy.deepcopy(s)))
        st={'a':1}
        with env['_batch_confirmed_facts'](st):
            st['a']=2;env['save'](st)
            t=threading.Thread(target=lambda: env['save'](st))
            t.start();t.join()
            self.assertEqual(len(writes),1)
        self.assertEqual(len(writes),2)

    def place_fixture(self):
        snapshots=[];sent=[]
        class API:
            def req(self,method,path,**kw):
                sent.append((method,path,copy.deepcopy(kw)))
                return [{'ordId':'1234','sCode':'0'}]
        n=0
        def new_id(p):
            nonlocal n
            n+=1
            return p+str(n)
        env={'fmt':lambda x:f'{x:.2f}', 'TP':Decimal('1'),
             'QTY':Decimal('.003'),'COIN':'eth','INST':'ETH-USDT-SWAP',
             'time':time, 'newid':new_id,'new_request_deadline_ms':lambda:2000000000000,
             'A':API(), 'save':lambda st:snapshots.append(copy.deepcopy(st)),
             'log':lambda *a,**k:None,'margin_rejection':lambda x:False,
             'trip_account_soft_stop':lambda *a:None,
             'OrderExpiredBeforeSend':type('OrderExpiredBeforeSend',(RuntimeError,),{}),
             'die':lambda msg: (_ for _ in ()).throw(RuntimeError(msg))}
        return only('place',env),env,snapshots,sent

    def test_one_pre_post_durable_write_with_archive(self):
        fun,env,writes,sent=self.place_fixture()
        st={'cycles':{'100.00':{'cycleId':'old','state':'CLOSED'}}}
        self.assertTrue(fun(st,Decimal('100')))
        self.assertEqual(len(writes),1)
        self.assertEqual(len(sent),1)
        self.assertEqual(writes[0]['cycles']['100.00']['state'],'PLACING')
        self.assertEqual(writes[0]['archived_cycles'][0]['cycleId'],'old')
        self.assertEqual(st['cycles']['100.00']['state'],'BUY_LIVE')
        self.assertEqual(writes[0]['cycles']['100.00']['clOrdId'],st['cycles']['100.00']['clOrdId'])

    def test_disk_error_never_posts(self):
        fun,env,writes,sent=self.place_fixture()
        env['save']=lambda st: (_ for _ in ()).throw(OSError('disk full'))
        with self.assertRaises(OSError):fun({'cycles':{}},Decimal('100'))
        self.assertEqual(sent,[])

    def test_uncertain_post_keeps_durable_placing(self):
        fun,env,writes,sent=self.place_fixture()
        env['A'].req=lambda *a,**k: (_ for _ in ()).throw(TimeoutError('uncertain'))
        st={'cycles':{}}
        with self.assertRaises(TimeoutError):fun(st,Decimal('100'))
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0]['cycles']['100.00']['state'],'PLACING')
        self.assertEqual(st['cycles']['100.00']['state'],'PLACING')

    def test_real_state_upgrade_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'bot04_state.json'
            legacy={'version':'0.7.3','instrument':'ETH-USDT-SWAP','cycles':{
                '120.00':{'state':'TP_WAIT','cycleId':'CYCLE1','ordId':'B','tpOrders':[]}},
                'completed':333}
            blob=json.dumps(legacy).encode()
            state.write_bytes(blob)
            events=[]
            def durable_save(st):state.write_text(json.dumps(st))
            env={'DATA':state,'INST':'ETH-USDT-SWAP','VER':'0.7.5',
                 'save':durable_save,'log':lambda *a,**kw:events.append((a,kw)),
                 'migrate_cycle_records':lambda st:None,'shutil':shutil,'os':os,
                 'json':json,'die':lambda s:(_ for _ in ()).throw(RuntimeError(s)),'LIVE':True}
            only('ensure_state',env)
            st=env['ensure_state']()
            self.assertEqual(st['cycles'],legacy['cycles'])
            self.assertEqual(st['version'],'0.7.5')
            self.assertEqual(json.loads(state.read_text())['version'],'0.7.5')
            backup=state.with_name('bot04_state_before_v075.json')
            self.assertEqual(backup.read_bytes(),blob)

    def test_position_read_after_fill_reconciliation(self):
        fn=node(NEW,'reconcile_position')
        body=ast.unparse(fn)
        self.assertLess(body.index("pending ="), body.index("with _batch_confirmed_facts(st):"))
        self.assertLess(body.index("with _batch_confirmed_facts(st):"), body.index("positions ="))
        # Panic reconciliation remains outside deferred-facts scope.
        self.assertLess(body.index("if st.get('panic_orders'):"),body.index("positions ="))

    def test_manager_dispatch_and_read_only_ui(self):
        manager=(HERE.parent/'okxbot.py').read_text()
        ui=(HERE.parent/'okx_ui074.py').read_text()
        self.assertIn("('okx_bot075.py','okx_bot074.py'",manager)
        self.assertIn("cfg.update(version='0.7.5'",manager)
        self.assertIn("unit_text(dest,'okx_bot075.py',new_name)",manager)
        self.assertIn("MANAGER_VERSION='v0.7.5'",ui)
        # The PUBLIC v0.7.4 UI already calculates a fixed +5 buffer.
        # Keep that implementation, merely update version detection.
        self.assertIn('buf=buy+5',ui)
        self.assertIn("'0.7.5':'075'",ui)
        # Manager retains the read-only monitor; it does not execute engine code.
        self.assertNotIn('import okx_bot075',ui)

    def test_wizard_creates_v075_not_old_engine(self):
        wizard=(HERE.parent/'okx_setup068.py').read_text()
        self.assertIn("VER='0.7.5'",wizard)
        self.assertIn("shutil.copy2(BASE/'okx_bot075.py',out/'okx_bot075.py')",wizard)
        self.assertNotIn("shutil.copy2(BASE/'okx_bot074.py',out/'okx_bot074.py')",wizard)
        ast.parse(wizard)

    def test_original_074_and_candidate_075_parse(self):
        for name in ('okx_bot075.py','okxbot.py','okx_ui074.py',
                     'okx_monitor074.py','okx_monitor_compat074.py'):
            ast.parse((HERE.parent/name).read_text())
        self.assertEqual((HERE/'original_okx_bot074.py').read_bytes()[:2], b'#!')

    def test_shared_account_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate_path=str(Path(tmp)/'positions.json')
            env={'Path':Path,'ENV':{'OKX_ACCOUNT_POSITIONS_GATE_PATH':gate_path},
                 'os':os,'time':time,'json':json,'fcntl':fcntl,
                 'ExchangeUnresolved':type('ExchangeUnresolved',(RuntimeError,),{}),
                 'die':lambda s:(_ for _ in ()).throw(RuntimeError(s))}
            Gate=only('_AccountPositionsGate',env)
            a=Gate();b=Gate()
            for i in range(4):a.take();b.take()
            self.assertEqual(len(json.loads(Path(gate_path).read_text())['calls']),8)
            start=time.monotonic();b.take()
            self.assertGreater(time.monotonic()-start,1.7)
            Path(gate_path).write_text('broken')
            with self.assertRaises(env['ExchangeUnresolved']):a.take()

if __name__=='__main__':unittest.main(verbosity=2)
