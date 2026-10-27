"""RC6 read-only resilience tests: no network, credentials, VPS or trading."""
import ast
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from okx_position_reader_core import (
    Priority, ReaderError, Registration, SharedReaderCore, SnapshotError,
    build_snapshot,
)
from okx_position_reader_service import ReadOnlyOKXProvider
from okx_position_reader_ipc import ReaderIPCServer
from okx_position_reader_client import read_positions, PositionReaderUnavailable
from okx_api import verify_reader_uid

UID = 'OFFLINE_TEST_ACCOUNT'

class Quota:
    def __init__(self):
        self.event = asyncio.Event()
        self.calls = 0
    async def take(self, priority_supplier):
        self.calls += 1
        await self.event.wait()

class OneBrokenPairProvider:
    def __init__(self):
        self.calls = 0
    async def fetch(self, insts):
        self.calls += 1
        return {'code': '0', 'data': [
            {'instId': 'ZEC-USDT-SWAP', 'posSide':'net','pos':'2'},
            {'instId': 'NEAR-USDT-SWAP','posSide':'net','pos':'3'},
            {'instId': 'NEAR-USDT-SWAP','posSide':'net','pos':'4'},
            {'instId': 'BTC-USDT-SWAP','posSide':'net','pos':'5'},
            {'instId': 'UNKNOWN-USDT-SWAP','posSide':'net','pos':'11'}]}

class IsolatedCorruption(unittest.IsolatedAsyncioTestCase):
    async def test_one_bad_near_does_not_blind_btc_zec(self):
        q=Quota();p=OneBrokenPairProvider()
        c=SharedReaderCore([
            Registration('z','ZEC-USDT-SWAP'),
            Registration('b','BTC-USDT-SWAP'),
            Registration('n','NEAR-USDT-SWAP')],p,q,account_uid=UID)
        await c.start()
        try:
            tasks=[asyncio.create_task(c.request(inst,Priority.EXIT,account_uid=UID))
                   for inst in ('ZEC-USDT-SWAP','NEAR-USDT-SWAP','BTC-USDT-SWAP')]
            await asyncio.sleep(0.01);q.event.set()
            out=await asyncio.gather(*tasks,return_exceptions=True)
            self.assertEqual(out[0]['rows'][0]['instId'],'ZEC-USDT-SWAP')
            self.assertIsInstance(out[1],SnapshotError)
            self.assertEqual(out[2]['rows'][0]['instId'],'BTC-USDT-SWAP')
            self.assertEqual(q.calls,1)
            self.assertEqual(p.calls,1)
        finally:
            await c.close()

    async def test_wrong_uid_refused_before_quota_or_http(self):
        q=Quota();p=OneBrokenPairProvider()
        c=SharedReaderCore([Registration('z','ZEC-USDT-SWAP')],p,q,account_uid=UID)
        await c.start()
        try:
            with self.assertRaisesRegex(ReaderError,'UID mismatch'):
                await c.request('ZEC-USDT-SWAP',Priority.EXIT,account_uid='WRONG_ACCOUNT')
            self.assertEqual(q.calls,0)
            self.assertEqual(p.calls,0)
        finally:
            await c.close()

    async def test_real_unix_ipc_wrong_account_rejected(self):
        q=Quota();q.event.set();p=OneBrokenPairProvider()
        c=SharedReaderCore([Registration('z','ZEC-USDT-SWAP')],p,q,account_uid=UID)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700)
            path=root/'sock';service=ReaderIPCServer(c,path)
            await service.start()
            try:
                with self.assertRaises(PositionReaderUnavailable):
                    await asyncio.to_thread(read_positions,path,'ZEC-USDT-SWAP',
                                            account_uid='WRONG_ACCOUNT')
                self.assertEqual(p.calls,0)
                result=await asyncio.to_thread(read_positions,path,'ZEC-USDT-SWAP',
                                              account_uid=UID)
                self.assertEqual(result[0]['pos'],'2')
            finally:
                await service.close()

class ReaderHealth(unittest.IsolatedAsyncioTestCase):
    async def test_health_and_down_event_do_not_touch_okx(self):
        import okx_telegram_health as health
        q=Quota();q.event.set();p=OneBrokenPairProvider()
        core=SharedReaderCore([Registration('z','ZEC-USDT-SWAP')],p,q,account_uid=UID)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700)
            socketpath=root/'reader.sock'
            srv=ReaderIPCServer(core,socketpath)
            await srv.start()
            notices=[];state={}
            with patch.dict('os.environ',{'OKX_POSITION_READER_SOCKET':str(socketpath)}), \
                 patch.object(health,'one',side_effect=lambda a,st,k,msg:notices.append((k,msg))):
                try:
                    await asyncio.to_thread(health.position_reader_health_check,None,state)
                    self.assertEqual([k for k,_ in notices],['reader/watchdog/armed'])
                    self.assertEqual(p.calls,0)
                finally:
                    await srv.close()
                await asyncio.to_thread(health.position_reader_health_check,None,state)
                await asyncio.to_thread(health.position_reader_health_check,None,state)
                self.assertEqual(len(notices),2)
                self.assertIn('unavailable',notices[1][0])

class VerifiedAccountUID(unittest.TestCase):
    def test_config_uid_verified_once_per_engine_process(self):
        hits=[]
        def fake_req(*args):
            hits.append(args)
            return [{'uid': UID}]
        instance=SimpleNamespace(req=fake_req)
        self.assertEqual(verify_reader_uid(instance),UID)
        self.assertEqual(verify_reader_uid(instance),UID)
        self.assertEqual(hits,[('GET','/api/v5/account/config')])
    def test_uid_unavailable_fail_closed(self):
        instance=SimpleNamespace(req=lambda *args: [{'uid':''}])
        with self.assertRaises(RuntimeError):
            verify_reader_uid(instance)
        self.assertFalse(hasattr(instance,'_reader_uid_verified'))

class HTTPTransport(unittest.TestCase):
    def test_connection_reused_and_closed_on_server_close(self):
        provider=ReadOnlyOKXProvider('fake','fake','fake')
        objects=[]
        class FakeResponse:
            status=200
            will_close=False
            def read(self,n):return b'{"code":"0","data":[]}'
        class FakeConnection:
            def __init__(self,*a,**kw):
                objects.append(self)
                self.requests=[]
            def request(self,*args,**kwargs):self.requests.append((args,kwargs))
            def getresponse(self):return FakeResponse()
            def close(self):pass
        with patch('okx_position_reader_service.http.client.HTTPSConnection',FakeConnection):
            provider._sync_get(('ZEC-USDT-SWAP',))
            provider._sync_get(('ZEC-USDT-SWAP',))
        self.assertEqual(len(objects),1)
        self.assertEqual(len(objects[0].requests),2)
        self.assertIn('instType=SWAP',objects[0].requests[0][0][1])

class ManagerRegression(unittest.TestCase):
    def test_start_existing_running_bot_ignores_stale_unrelated_entry(self):
        import okxbot
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config.json').write_text(
                '{"instrument":"ZEC-USDT-SWAP"}')
            entry={'path':str(root),'service':'okx-grid-zec'}
            broken={'stale':{'path':str(root/'moved'),'service':'okx-grid-gone'},
                    'zec':entry}
            with patch.object(okxbot,'load_registry',return_value=broken), \
                 patch.object(okxbot,'find_entry',return_value=('zec',entry)), \
                 patch.object(okxbot,'bot_path',return_value=root), \
                 patch.object(okxbot,'unit_name',return_value='okx-grid-zec.service'), \
                 patch.object(okxbot,'instance_terminal',return_value=False), \
                 patch.object(okxbot,'control_desired',return_value='RUN'), \
                 patch.object(okxbot,'service_running',return_value=True):
                okxbot.cmd_start('zec')  # no trading or service action

    def test_start_does_not_check_entire_registry_capacity(self):
        import okxbot
        tree=ast.parse(Path(okxbot.__file__).read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_start')
        calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and
               isinstance(n.func,ast.Name) and n.func.id=='require_account_capacity']
        self.assertEqual(calls,[])
        new=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_new')
        attach=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_attach')
        self.assertTrue(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                            and n.func.id=='require_account_capacity' for n in ast.walk(new)))
        self.assertTrue(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                            and n.func.id=='require_account_capacity' for n in ast.walk(attach)))

if __name__=='__main__':unittest.main()
