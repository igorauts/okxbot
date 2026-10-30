"""No-network regression contracts for Reader-side physical HTTP accounting.

Deliberately exercises the real vNext service provider against fake HTTP objects;
no exchange credentials or production IPC.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from okx_position_reader_core import Registration, SharedReaderCore, Priority
from okx_position_reader_ipc import ReaderIPCServer
from okx_position_reader_service import ReadOnlyOKXProvider


class FakeResponse:
    def __init__(self,status,code='0'):
        self.status=status
        self.will_close=False
        self.payload=json.dumps({'code':code,'data':[]}).encode()
    def read(self,n):
        return self.payload

class FakeConn:
    def __init__(self,response):
        self.response=response
        self.calls=0
    def request(self,method,path,headers):
        assert method=='GET'
        self.calls+=1
    def getresponse(self):
        return self.response
    def close(self):
        pass

class WireAccounting(unittest.TestCase):
    PATH='/api/v5/account/positions?instType=SWAP&instId=BTC-USDT-SWAP'
    def test_counts_one_actual_http_attempt_and_ignores_uid_startup(self):
        p=ReadOnlyOKXProvider('fixturekey','fixturesecret','fixturepass')
        c=FakeConn(FakeResponse(200))
        p._connection=lambda:c
        p._sync_get_path(self.PATH)
        self.assertEqual(c.calls,1)
        self.assertEqual(p.positions_stats_snapshot()['http_attempts'],1)
        self.assertEqual(p.positions_stats_snapshot()['http_ok'],1)
        p._sync_get_path('/api/v5/account/config')
        self.assertEqual(c.calls,2)
        self.assertEqual(p.positions_stats_snapshot()['http_attempts'],1)

    def test_exact_http429_and_okx_50011_no_digit_search(self):
        p=ReadOnlyOKXProvider('k','s','p')
        c=FakeConn(FakeResponse(429))
        p._connection=lambda:c
        with self.assertRaises(RuntimeError):
            p._sync_get_path(self.PATH)
        self.assertEqual(p.positions_stats_snapshot()['http_429'],1)
        self.assertEqual(p.positions_stats_snapshot()['http_non200'],1)
        c.response=FakeResponse(200,code='50011')
        with self.assertRaises(RuntimeError):
            p._sync_get_path(self.PATH)
        self.assertEqual(p.positions_stats_snapshot()['okx_50011'],1)
        self.assertEqual(p.positions_stats_snapshot()['okx_rejected'],1)
        self.assertEqual(p.positions_stats_snapshot()['http_attempts'],2)
        self.assertEqual(p.positions_stats_snapshot()['http_ok'],0)

class NoNetworkCoreAndIPC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.parent=Path(self.tmp.name)
        self.parent.chmod(0o700)
        self.sock=self.parent/'reader.sock'
        self.provider=self.Provider()
        self.core=SharedReaderCore(
            [Registration('btc','BTC-USDT-SWAP'),
             Registration('zec','ZEC-USDT-SWAP')],
            self.provider,self.Quota(),account_uid='fakeuid')
        self.server=ReaderIPCServer(self.core,self.sock)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        self.tmp.cleanup()

    class Quota:
        async def take(self,priority_supplier):
            await asyncio.sleep(.02)

    class Provider:
        def __init__(self):
            self.n=0
        async def fetch(self,instruments):
            self.n+=1
            return {'code':'0','data':[{'instId':x,'posSide':'net','pos':'1'}
                                       for x in instruments]}
        def positions_stats_snapshot(self):
            return {'http_attempts':self.n,'http_ok':self.n,
                    'http_non200':0,'http_429':0,'okx_50011':0}

    async def test_batched_clients_and_counters_and_diag_ring(self):
        results=await asyncio.gather(
            self.core.request('BTC-USDT-SWAP',Priority.NORMAL,account_uid='fakeuid'),
            self.core.request('ZEC-USDT-SWAP',Priority.EXIT,account_uid='fakeuid'))
        self.assertEqual(len(results),2)
        self.assertEqual(self.provider.n,1)
        self.assertEqual(self.core.counters['quota_granted'],1)
        self.assertEqual(self.core.counters['provider_fetch_calls'],1)
        self.assertEqual(self.core.counters['get_batches'],1)
        self.assertEqual(self.core.counters['coalesced_clients'],2)
        sample=self.core.interval_diagnostics(0)
        self.assertEqual(sample['batches'],1)
        self.assertEqual(sample['served_clients'],2)
        self.assertEqual(sample['priority_batches']['1'],1)
        self.assertEqual(self.core.interval_diagnostics(sample['seq'])['batches'],0)
        with self.assertRaises(ValueError):
            self.core.interval_diagnostics(-1)

    async def test_health_get_free_never_shows_secrets(self):
        reader,writer=await asyncio.open_unix_connection(str(self.sock))
        try:
            writer.write(b'{"schema":1,"op":"health"}\n')
            await writer.drain()
            data=await asyncio.wait_for(reader.readline(),1.0)
            obj=json.loads(data)
            self.assertTrue(obj['ok'])
            h=obj['data']
            self.assertEqual(h['get_batches'],0)
            self.assertEqual(h['positions_http']['http_attempts'],0)
            self.assertNotIn('fakeuid',data.decode())
            self.assertNotIn('BTC-USDT-SWAP?instId=',data.decode())
            self.assertEqual(self.provider.n,0)
        finally:
            writer.close()
            await writer.wait_closed()

if __name__=='__main__':
    unittest.main()
