"""Integration regressions for opt-in reader (offline; fake Unix socket and REST)."""
import ast
import asyncio
import tempfile
import unittest
import time
import threading
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from okx_position_reader_core import SharedReaderCore, Registration
from okx_position_reader_ipc import ReaderIPCServer
from okx_position_reader_client import (read_positions,PositionReaderEmpty,
                                        PositionReaderUnavailable)
from okx_positions_fifo import AccountPositionsFIFO
from okx_api import position_trace_id

class Quota:
    def __init__(self): self.takes=0
    async def take(self, priority_supplier=None): self.takes+=1

class Provider:
    def __init__(self,empty=False):self.calls=0;self.empty=empty
    async def fetch(self,insts):
        self.calls+=1
        return {'code':'0','data':[] if self.empty else
                [{'instId':i,'pos':'2','posSide':'net'} for i in insts]}

class UnixClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.d=Path(self.t.name);self.d.chmod(0o700)
        self.path=self.d/'p.sock'
        self.quota=Quota();self.provider=Provider()
        self.core=SharedReaderCore([Registration('zec','ZEC-USDT-SWAP')],self.provider,self.quota,account_uid='TEST_UID')
        self.server=ReaderIPCServer(self.core,self.path)
        await self.server.start()
    async def asyncTearDown(self):
        await self.server.close();self.t.cleanup()
    async def test_sync_client_real_unix_roundtrip(self):
        rows=await asyncio.to_thread(read_positions,self.path,'ZEC-USDT-SWAP',trace_id='trace-1',account_uid='TEST_UID')
        self.assertEqual(rows[0]['instId'],'ZEC-USDT-SWAP')
        self.assertEqual(self.quota.takes,1)
        self.assertEqual(self.provider.calls,1)
    async def test_empty_batch_requires_individual_confirmation(self):
        self.provider.empty=True
        with self.assertRaises(PositionReaderEmpty):
            await asyncio.to_thread(read_positions,self.path,'ZEC-USDT-SWAP',trace_id='trace-2',account_uid='TEST_UID')
        self.assertEqual(self.quota.takes,1)
    async def test_wrong_instrument_is_rejected(self):
        with self.assertRaises(PositionReaderUnavailable):
            await asyncio.to_thread(read_positions,self.path,'BTC-USDT-SWAP',trace_id='trace-3',account_uid='TEST_UID')
        self.assertEqual(self.provider.calls,0)

class APIRouting(unittest.TestCase):
    def method(self,mode,phase,rows=None,exit_hint=None):
        """Execute only API.positions AST; never import LIVE engine or use HTTP."""
        p=Path(__file__).resolve().parents[1]/'okx_api.py'
        tree=ast.parse(p.read_text())
        factory=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_api')
        cls=next(n for n in factory.body if isinstance(n,ast.ClassDef) and n.name=='API')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='positions')
        ns={'ENV':{'OKX_POSITION_READER_SOCKET':'/private/p.sock'} if mode else {},
            'INST':'ZEC-USDT-SWAP','time':time,
            'control_mode':lambda:phase,
            'ExchangeTemporary':type('ExchangeTemporary',(RuntimeError,),{}),
            'latency_ms':lambda *a,**kw:None,
            'verify_reader_uid':lambda obj:'TEST_UID',
            'position_trace_id':position_trace_id}
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(p),'exec'),ns)
        calls=[]
        class OfflineAPIFixture(SimpleNamespace):
            # Match the real per-process API class emergency direct-GET limiter.
            _reader_direct_lock=threading.Lock()
            _reader_direct_next=0.0
        OfflineAPIFixture._reader_direct_next=0.0
        api=OfflineAPIFixture(req=lambda *a,**kw:calls.append((a,kw)) or [{'source':'original'}], _reader_account_uid=lambda:'TEST_UID')
        if exit_hint is not None:api._reader_exit_hint=lambda:exit_hint
        with patch('okx_position_reader_client.read_positions') as mock:
            if isinstance(rows,Exception):mock.side_effect=rows
            else:mock.return_value=[{'source':'reader'}] if rows is None else rows
            return ns['positions'](api),calls,mock.call_count,ns['ExchangeTemporary']
    def test_disabled_is_original_exact_get(self):
        data,calls,n,_=self.method(False,'RUN')
        self.assertEqual(data,[{'source':'original'}]);self.assertEqual(n,0)
        self.assertEqual(calls[0][0],('GET','/api/v5/account/positions',{'instId':'ZEC-USDT-SWAP'}))
    def test_reader_success_skips_original_gate_and_http(self):
        data,calls,n,_=self.method(True,'RUN')
        self.assertEqual(data,[{'source':'reader'}]);self.assertEqual(n,1);self.assertFalse(calls)
    def test_reader_down_in_run_must_fail_closed(self):
        from okx_position_reader_client import PositionReaderUnavailable
        # Process/socket really down: NORMAL must NOT bypass the Reader.
        with self.assertRaisesRegex(RuntimeError,'normal/background gated'):
            self.method(True,'RUN',rows=PositionReaderUnavailable('down',reason='reader_down'))
    def test_reader_down_emergency_uses_protected_direct_get(self):
        # Only a classified local IPC transport outage allows PANIC fallback.
        data,calls,n,_=self.method(True,'PANIC',rows=PositionReaderUnavailable('down',reason='reader_down'))
        self.assertEqual(data,[{'source':'original'}]);self.assertEqual(n,1);self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0][0:2],('GET','/api/v5/account/positions'))
    def test_reader_alive_rate_limited_emergency_must_not_direct_get(self):
        # A live Reader reporting OKX quota trouble cannot authorize more GET.
        with self.assertRaisesRegex(RuntimeError,'no direct GET'):
            self.method(True,'PANIC',rows=PositionReaderUnavailable('rate',reason='okx_rate_limit'))
    def test_virtual_exit_hint_only_affects_ipc_priority(self):
        # RUN + passive WS crossing does not create/authorize a POST.
        data,calls,n,_=self.method(True,'RUN',exit_hint=True)
        self.assertEqual(data,[{'source':'reader'}])
        self.assertFalse(calls)
        self.assertEqual(n,1)
        # A legacy or disabled Reader retains the original protected GET.
        data,calls,n,_=self.method(False,'RUN',exit_hint=True)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0][0],'GET')

    def test_omitted_instrument_fails_closed_without_zero_proof(self):
        # An unproven zero is never converted into a direct GET or new BUY.
        with self.assertRaisesRegex(RuntimeError,'cannot prove zero position'):
            self.method(True,'RUN',rows=PositionReaderEmpty('not zero'))

class SharedLegacyGate(unittest.TestCase):
    def test_original_single_quota_cross_process_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'q.json'
            gate_a=AccountPositionsFIFO(path)
            gate_b=AccountPositionsFIFO(path)
            # Schema-1 initial state is explicit, not assumed empty.
            import json
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            for i in range(gate_a.MAX_SHARED):
                self.assertTrue((gate_a if i%2 else gate_b)._reserve_if_available(time.time())[0])
            self.assertFalse(gate_a._reserve_if_available(time.time())[0])
            self.assertEqual(len(json.loads(path.read_text())['calls']),gate_a.MAX_SHARED)

if __name__=='__main__':unittest.main(verbosity=2)
