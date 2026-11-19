"""Real API/Reader client narrow-fallback contracts, no OKX and no LIVE I/O."""
import asyncio
import decimal
import errno
import json
import os
import socket
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from okx_api import build_api
from okx_position_reader_client import (PositionReaderUnavailable,PositionReaderPreempted,
                                        read_positions)
from okx_position_reader_ipc import reader_ipc_error_reason
from okx_position_reader_core import ReaderError, SharedReaderCore, Registration
from okx_position_reader_service import ReaderUpstreamError

class Temporary(RuntimeError):pass
class Unresolved(RuntimeError):pass

class Gate:
    def begin(self,*a,**kw):return False
    def end(self,*a):pass
    def throttle(self,*a):raise AssertionError('No throttle expected')
    def take(self,*a,**kw):pass
class Flight:
    def run(self,*a):return a[-1]()

class NarrowFallback(unittest.TestCase):
    def setUp(self):
        self.mode='RUN'
        self.exit=False
        self.calls=[]
        self.metrics=[]
        env={'OKX_API_KEY':'TEST','OKX_API_SECRET':'TEST','OKX_API_PASSPHRASE':'TEST',
             'OKX_POSITION_READER_SOCKET':'/tmp/test-reader-sock'}
        api=build_api(ENV=env,CFG={},COIN='BTC',INST='BTC-USDT-SWAP',LIVE=True,
          VER='offline',D=decimal.Decimal,die=lambda x:(_ for _ in ()).throw(RuntimeError(x)),
          CONTRACT_LOT=decimal.Decimal(1),CONTRACT_MIN=decimal.Decimal(1),
          CONTRACT_VALUE=decimal.Decimal(1),PRICE_STEP=decimal.Decimal(1),
          QTY=decimal.Decimal(1),STEP=decimal.Decimal(1),TP=decimal.Decimal(1),
          ExchangeTemporary=Temporary,ExchangeUnresolved=Unresolved,
          OrderExpiredBeforeSend=RuntimeError,RateLimitedError=Temporary,
          GateUnavailable=RuntimeError,REST_GATE=Gate(),FILL_HISTORY_GATE=Gate(),
          ACCOUNT_POSITIONS_GATE=Gate(),FILL_FETCH_FLIGHT=Flight(),
          _PERF_LOCAL=threading.local(),_PERF_RECON_ENABLED=False,
          _flush_confirmed_facts=lambda:None,latency_ms=lambda *a,**kw:self.metrics.append((a,kw)),
          fill_store=lambda:None,control_mode=lambda:self.mode)
        self.obj=api()
        self.obj._reader_uid_verified='TEST'
        self.obj._reader_exit_hint=lambda:self.exit
        api._reader_direct_next=0.0
        self.obj.req=lambda method,route,params=None:self.calls.append((method,route)) or []

    def fake(self,reason,clock=100.0):
        def raises(*a,**k):raise PositionReaderUnavailable('fake',reason=reason)
        return mock.patch('okx_position_reader_client.read_positions',side_effect=raises)

    def test_reader_down_normal_never_direct_get(self):
        with self.fake('reader_down'):
            with self.assertRaises(Temporary):self.obj.positions()
        self.assertEqual(self.calls,[])
        self.assertGreater(self.obj._reader_next_nonurgent_probe,0)

    def test_reader_down_exit_direct_then_local_paced(self):
        self.exit=True
        with self.fake('reader_down'):
            self.obj.positions()
            with self.assertRaises(Temporary):self.obj.positions()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][1],'/api/v5/account/positions')
        self.assertTrue(any(a[0]=='position_reader_fallback' and kw['priority']=='exit' for a,kw in self.metrics))

    def test_reader_down_panic_uses_emergency(self):
        self.mode='PANIC'
        with self.fake('reader_down'):self.obj.positions()
        self.assertEqual(len(self.calls),1)

    def test_reader_ok_false_never_direct_even_exit(self):
        self.exit=True
        for reason in ('okx_error','okx_rate_limit','quota_timeout','reader_rejected','invalid_request'):
            self.obj._reader_bypass_until=0.0
            with self.fake(reason):
                with self.assertRaises(Temporary):self.obj.positions()
        self.assertEqual(self.calls,[])

    def test_preempted_does_not_fallback_and_metric_is_precise(self):
        self.obj._reader_background_hint=lambda:True
        self.obj._reader_background_interrupt=lambda:False
        with mock.patch('okx_position_reader_client.read_positions',side_effect=PositionReaderPreempted('interrupt')):
            with self.assertRaises(PositionReaderPreempted):self.obj.positions()
        self.assertEqual(self.calls,[])
        self.assertTrue(any(a[0]=='position_reader_ipc' and kw['outcome']=='preempted' for a,kw in self.metrics))

    def test_restoration_after_five_seconds(self):
        self.exit=True
        clock=[100.0]
        seen=[]
        def reader(*a,**k):seen.append(clock[0]);return []
        with mock.patch('okx_api.time.monotonic',side_effect=lambda:clock[0]):
            with self.fake('reader_down'):
                self.obj.positions()
            clock[0]=106.0
            with mock.patch('okx_position_reader_client.read_positions',side_effect=reader):
                self.obj.positions()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(seen,[106.0])
        self.assertEqual(self.obj._reader_bypass_until,0.0)

    def test_fallback_not_started_on_invalid_snapshot(self):
        self.exit=True
        with self.fake('invalid_request'):
            with self.assertRaises(Temporary):self.obj.positions()
        self.assertEqual(self.calls,[])

    def test_readonly_client_rejected_429_is_not_reader_down(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);p.chmod(0o700);sockpath=p/'s'
            class Sock:
                def __enter__(self):return self
                def __exit__(self,*a):pass
                def settimeout(self,*a):pass
                def connect(self,*a):pass
                def sendall(self,*a):pass
                def recv(self,*a):return b'{"ok":false,"reason":"okx_rate_limit"}\n'
            with mock.patch('okx_position_reader_client.socket.socket',return_value=Sock()):
                with self.assertRaises(PositionReaderUnavailable) as cm:
                    read_positions(str(sockpath),'BTC-USDT-SWAP',account_uid='TEST')
            self.assertEqual(cm.exception.reason,'okx_rate_limit')

    def test_transport_refused_classified(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);p.chmod(0o700)
            class Sock:
                def __enter__(self):return self
                def __exit__(self,*a):pass
                def settimeout(self,*a):pass
                def connect(self,*a):raise ConnectionRefusedError(errno.ECONNREFUSED,'refused')
            with mock.patch('okx_position_reader_client.socket.socket',return_value=Sock()):
                with self.assertRaises(PositionReaderUnavailable) as cm:
                    read_positions(str(p/'s'),'BTC-USDT-SWAP',account_uid='TEST')
            self.assertEqual(cm.exception.reason,'reader_down')

    def test_provider_50011_is_propagated_as_upstream_limit(self):
        from okx_position_reader_service import ReadOnlyOKXProvider
        provider=ReadOnlyOKXProvider('FAKE_KEY','FAKE_SECRET','FAKE_PASS')
        class Conn:
            def request(self,*a,**kw):pass
            def getresponse(self):
                return types.SimpleNamespace(status=200,will_close=False,
                    read=lambda *a:b'{"code":"50011","data":[]}')
        provider._connection=lambda:Conn()
        with self.assertRaises(ReaderUpstreamError) as cm:
            provider._sync_get_path('/api/v5/account/positions?instType=SWAP&instId=BTC-USDT-SWAP')
        self.assertEqual(cm.exception.reader_reason,'okx_rate_limit')

    def test_core_propagates_upstream_failure_not_reader_down(self):
        from okx_position_reader_core import Priority
        class BadProvider:
            async def fetch(self,instruments):
                raise ReaderUpstreamError('fake','okx_rate_limit')
        class Quota:
            async def take(self,priority_supplier):pass
        async def check():
            core=SharedReaderCore([Registration('testbot','BTC-USDT-SWAP')],
                                  BadProvider(),Quota(),account_uid='TEST',
                                  background_grace_sec=0.0)
            await core.start()
            try:
                with self.assertRaises(ReaderError) as cm:
                    await core.request('BTC-USDT-SWAP',Priority.EXIT,
                                       trace_id='abc',account_uid='TEST',timeout_sec=1.0)
                self.assertEqual(getattr(cm.exception,'reader_reason',None),'okx_rate_limit')
            finally:
                await core.close()
        asyncio.run(check())

    def test_error_reason_is_sanitized(self):
        x=ReaderError('opaque');x.reader_reason='okx_rate_limit'
        self.assertEqual(reader_ipc_error_reason(x),'okx_rate_limit')
        x.reader_reason='reader_down'
        self.assertEqual(reader_ipc_error_reason(x),'reader_rejected')
        self.assertEqual(ReaderUpstreamError('x','okx_rate_limit').reader_reason,'okx_rate_limit')

if __name__=='__main__':unittest.main()
