"""RC8: mixed-version rollout, direct GET labels, Reader breaker, registry diagnostics.

All tests offline: fake REST and local temporary queue files. Never send trades.
"""
import ast
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from okx_api import direct_position_priority, position_trace_id
from okx_positions_fifo import AccountPositionsFIFO
from okx_positions_priority import PriorityPositionsFIFO, EMERGENCY, EXIT, NORMAL
from okx_position_reader_service import SharedOriginalQuota, load_registry
from okx_position_reader_client import PositionReaderUnavailable, PositionReaderEmpty

ROOT=Path(__file__).resolve().parents[1]


class FairMixedQuota(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'gate.json'
        self.path.write_text(json.dumps({'schema':1,'calls':[]}))

    def test_default_reader_is_unlabeled_legacy_fair(self):
        quota=SharedOriginalQuota(self.path)
        self.assertEqual(quota.scheduling,'mixed-fifo')
        self.assertIsInstance(quota.gate,AccountPositionsFIFO)
        self.assertNotIsInstance(quota.gate,PriorityPositionsFIFO)
        self.assertEqual(quota.gate.path,self.path)
        old=AccountPositionsFIFO(self.path)
        now=time.time()
        quota.gate.queue_path.write_text(json.dumps({'schema':1,'waiters':[{'id':'old','pid':os.getpid(),
            'created':now-0.2,'seen':now}]}))
        result,_=quota.gate._visit('reader',None)
        self.assertFalse(result)
        queue=json.loads(quota.gate.queue_path.read_text())['waiters']
        self.assertEqual([row['id'] for row in queue],['old','reader'])
        self.assertNotIn('priority',queue[1])
        self.assertTrue(old._visit('old',True)[0])
        self.assertTrue(quota.gate._visit('reader',True)[0])
        self.assertEqual(len(json.loads(self.path.read_text())['calls']),2)

    def test_new_direct_requests_have_priority_labels_and_same_quota(self):
        legacy=AccountPositionsFIFO(self.path)
        new=PriorityPositionsFIFO(self.path)
        self.assertEqual(new.path,legacy.path)
        self.assertEqual(new.queue_path,legacy.queue_path)
        new._thread_priority.priority_supplier=lambda:NORMAL
        try:
            self.assertTrue(new._visit('new_normal',None)[0])
        finally:
            new._thread_priority.priority_supplier=None
        self.assertTrue(legacy._visit('old',None)[0])
        self.assertEqual(len(json.loads(self.path.read_text())['calls']),2)

    def test_no_second_quota_in_priority_mode(self):
        quota=SharedOriginalQuota(self.path,scheduling='priority')
        self.assertIsInstance(quota.gate,PriorityPositionsFIFO)
        self.assertEqual(quota.gate.path,self.path)
        with self.assertRaises(ValueError):
            SharedOriginalQuota(self.path,scheduling='auto')

    def test_mixed_quota_abandons_dead_waiters_before_permit(self):
        from okx_position_reader_core import QuotaRequestAbandoned
        quota=SharedOriginalQuota(self.path)
        calls=0
        def gone():
            nonlocal calls
            calls+=1
            raise QuotaRequestAbandoned('offline canceled')
        import asyncio
        async def run():
            with self.assertRaises(QuotaRequestAbandoned):
                await quota.take(gone)
        asyncio.run(run())
        self.assertGreaterEqual(calls,1)
        self.assertEqual(json.loads(self.path.read_text())['calls'],[])
        queued=json.loads(quota.gate.queue_path.read_text())['waiters']
        self.assertEqual(queued,[])


class DirectPriority(unittest.TestCase):
    def test_mode_and_ws_hint(self):
        api=SimpleNamespace(_reader_exit_hint=lambda:True)
        self.assertEqual(direct_position_priority(api,lambda:'RUN'),EXIT)
        self.assertEqual(direct_position_priority(api,lambda:'PANIC'),EMERGENCY)
        self.assertEqual(direct_position_priority(api,lambda:'HARD'),EMERGENCY)
        api._reader_exit_hint=lambda:False
        self.assertEqual(direct_position_priority(api,lambda:'RUN'),NORMAL)
        api._reader_exit_hint=lambda:(_ for _ in ()).throw(RuntimeError('offline'))
        self.assertEqual(direct_position_priority(api,lambda:'RUN'),NORMAL)
        with self.assertRaises(RuntimeError):
            direct_position_priority(api,lambda:(_ for _ in ()).throw(RuntimeError('unknown mode')))

    def test_actual_api_req_routes_direct_positions_through_priority_supplier(self):
        tree=ast.parse((ROOT/'okx_api.py').read_text())
        factory=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_api')
        cls=next(n for n in factory.body if isinstance(n,ast.ClassDef) and n.name=='API')
        req=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='req')
        src=ast.unparse(req)
        self.assertIn('use_priority_positions_fifo(ENV)',src)
        self.assertIn('ACCOUNT_POSITIONS_GATE.take(lambda: direct_position_priority(self, control_mode))',src)
        self.assertIn('ACCOUNT_POSITIONS_GATE.take()',src)


class BreakerBehavior(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((ROOT/'okx_api.py').read_text())
        factory=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_api')
        cls=next(n for n in factory.body if isinstance(n,ast.ClassDef) and n.name=='API')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='positions')
        self.ns={'ENV':{'OKX_POSITION_READER_SOCKET':'/private/sock'},
                 'INST':'ZEC-USDT-SWAP','time':time,
                 'control_mode':lambda:'RUN',
                 'ExchangeTemporary':type('ExchangeTemporary',(RuntimeError,),{}),
                 'latency_ms':lambda *a,**kw:None,
                 'verify_reader_uid':lambda api:'OFFLINE_TEST_UID',
                 'position_trace_id':position_trace_id}
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(ROOT/'okx_api.py'),'exec'),self.ns)
        self.calls=[]
        self.api=SimpleNamespace(req=lambda *a,**kw:self.calls.append(a) or [{'source':'direct'}])

    def test_reader_failure_then_minute_direct_then_recovery(self):
        with patch('okx_position_reader_client.read_positions') as mock:
            mock.side_effect=PositionReaderUnavailable('offline')
            self.assertEqual(self.ns['positions'](self.api),[{'source':'direct'}])
            self.assertEqual(mock.call_count,1)
            self.assertGreater(self.api._reader_bypass_until,time.monotonic()+59)
            self.assertEqual(self.ns['positions'](self.api),[{'source':'direct'}])
            self.assertEqual(mock.call_count,1)
            self.api._reader_bypass_until=0.0
            mock.side_effect=None
            mock.return_value=[{'source':'reader'}]
            self.assertEqual(self.ns['positions'](self.api),[{'source':'reader'}])
            self.assertEqual(mock.call_count,2)
            self.assertEqual(self.api._reader_bypass_until,0.0)
            self.assertEqual(self.calls, [
                ('GET','/api/v5/account/positions',{'instId':'ZEC-USDT-SWAP'}),
                ('GET','/api/v5/account/positions',{'instId':'ZEC-USDT-SWAP'})])

    def test_empty_instrument_avoids_double_get_for_subsequent_passes(self):
        with patch('okx_position_reader_client.read_positions',side_effect=PositionReaderEmpty('zero unproven')) as mock:
            self.ns['positions'](self.api)
            self.ns['positions'](self.api)
            self.assertEqual(mock.call_count,1)
            self.assertEqual(len(self.calls),2)


class RetiredStartupError(unittest.TestCase):
    def test_missing_ack_error_names_bot_and_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            instance=root/'oldbot';instance.mkdir()
            (instance/'config.json').write_text(json.dumps({'instrument':'ABC-USDT-SWAP'}))
            registry=root/'registry.json'
            registry.write_text(json.dumps({'oldbot':{'path':str(instance),'retired':True}}))
            with self.assertRaisesRegex(RuntimeError,'Retired bot.*oldbot.*control_ack.json'):
                load_registry(registry)

if __name__=='__main__': unittest.main(verbosity=2)
