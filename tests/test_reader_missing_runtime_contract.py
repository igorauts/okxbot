"""OFFLINE tests using the REAL Unix IPC client, mocked OKX req only."""
import decimal, os, socket, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock
from okx_api import build_api
from okx_position_reader_client import read_positions,PositionReaderUnavailable

class Temporary(RuntimeError):pass
class Gate:
    def begin(self,*a,**k):return False
    def end(self,*a,**k):pass
    def take(self,*a,**k):pass
class Flight:
    def run(self,*a):return a[-1]()

class ReaderRuntimeFilesystem(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.runtime=self.base/'okx-position-reader'
        self.sock=self.runtime/'positions.sock'
        endpoint=mock.patch('okx_position_reader_client.READER_SYSTEMD_SOCKET',self.sock)
        endpoint.start()
        self.addCleanup(endpoint.stop)
        self.call_log=[]
        self.mode='RUN'
        self.exit=False
        self.metrics=[]
        decimal_one=decimal.Decimal(1)
        cls=build_api(ENV={'OKX_POSITION_READER_SOCKET':str(self.sock)},
            CFG={},COIN='BTC',INST='BTC-USDT-SWAP',LIVE=True,VER='offline',D=decimal.Decimal,
            die=lambda m: (_ for _ in ()).throw(RuntimeError(m)),
            CONTRACT_LOT=decimal_one,CONTRACT_MIN=decimal_one,
            CONTRACT_VALUE=decimal_one,PRICE_STEP=decimal_one,QTY=decimal_one,
            STEP=decimal_one,TP=decimal_one,ExchangeTemporary=Temporary,
            ExchangeUnresolved=RuntimeError,OrderExpiredBeforeSend=RuntimeError,
            RateLimitedError=Temporary,GateUnavailable=RuntimeError,
            REST_GATE=Gate(),FILL_HISTORY_GATE=Gate(),ACCOUNT_POSITIONS_GATE=Gate(),
            FILL_FETCH_FLIGHT=Flight(),_PERF_LOCAL=threading.local(),
            _PERF_RECON_ENABLED=False,_flush_confirmed_facts=lambda:None,
            latency_ms=lambda *a,**k:self.metrics.append((a,k)),
            fill_store=lambda:None,control_mode=lambda:self.mode)
        cls._reader_direct_next=0.0
        self.api=cls();self.api._reader_uid_verified='GOOD_UID'
        self.api._reader_exit_hint=lambda:self.exit
        self.api.req=lambda method,path,params=None:self.call_log.append((method,path)) or [{'source':'offline-stub'}]

    def client(self,**kw):
        return read_positions(str(self.sock),'BTC-USDT-SWAP',account_uid='GOOD_UID',**kw)

    def test_real_missing_runtime_directory_is_reader_down(self):
        self.assertFalse(self.runtime.exists())
        with self.assertRaises(PositionReaderUnavailable) as cm:self.client()
        self.assertEqual(cm.exception.reason,'reader_down')
        self.exit=True
        self.assertEqual(self.api.positions(),[{'source':'offline-stub'}])
        self.assertEqual(len(self.call_log),1)

    def test_missing_directory_normal_never_direct(self):
        with self.assertRaises(Temporary):self.api.positions()
        self.assertEqual(self.call_log,[])

    def test_missing_directory_panic_and_hard_direct(self):
        for mode in ('HARD','PANIC'):
            self.api._reader_bypass_until=0.0
            self.api._reader_bypass_reason=None
            self.mode=mode
            self.api.positions()
        self.assertEqual(len(self.call_log),2)

    def test_second_exit_waits_local_spacing_without_error(self):
        self.exit=True
        self.api.positions()
        begin=time.monotonic()
        with mock.patch('okx_api.time.sleep',wraps=time.sleep) as sleep:
            self.api.positions()
        elapsed=time.monotonic()-begin
        self.assertEqual(len(self.call_log),2)
        self.assertTrue(sleep.called)
        self.assertGreaterEqual(elapsed,0.70)
        self.assertLess(elapsed,2.0)

    def test_runtime_exists_but_socket_missing_is_reader_down(self):
        self.runtime.mkdir(mode=0o700)
        with self.assertRaises(PositionReaderUnavailable) as cm:self.client()
        self.assertEqual(cm.exception.reason,'reader_down')

    def test_invalid_socket_path_is_not_reader_down(self):
        with self.assertRaises(PositionReaderUnavailable) as cm:
            read_positions(str(self.base/'wrong-directory'/'wrong-name'),'BTC-USDT-SWAP',account_uid='GOOD_UID')
        self.assertEqual(cm.exception.reason,'invalid_request')
        other=self.base/'wrong-root'/'okx-position-reader'/'positions.sock'
        with self.assertRaises(PositionReaderUnavailable) as cm:
            read_positions(str(other),'BTC-USDT-SWAP',account_uid='GOOD_UID')
        self.assertEqual(cm.exception.reason,'invalid_request')
        with self.assertRaises(PositionReaderUnavailable) as cm:
            read_positions(str(self.sock),'BTC-USDT-SWAP',account_uid='')
        self.assertEqual(cm.exception.reason,'invalid_request')

    def test_symlink_parent_and_insecure_permissions_never_authorize_fallback(self):
        target=self.base/'real';target.mkdir(mode=0o700)
        self.runtime.symlink_to(target,target_is_directory=True)
        with self.assertRaises(PositionReaderUnavailable) as cm:self.client()
        self.assertEqual(cm.exception.reason,'invalid_request')
        self.runtime.unlink()
        self.runtime.mkdir(mode=0o755)
        with self.assertRaises(PositionReaderUnavailable) as cm:self.client()
        self.assertEqual(cm.exception.reason,'invalid_request')

    def test_systemd_cleanup_must_remain_enabled_due_to_stale_socket_guard(self):
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        unit=(root/'deploy/reader/okx-position-reader.service').read_text()
        ipc=(root/'okx_position_reader_ipc.py').read_text()
        self.assertIn('RuntimeDirectory=okx-position-reader\n',unit)
        self.assertIn('RuntimeDirectoryMode=0700\n',unit)
        self.assertNotIn('RuntimeDirectoryPreserve',unit)
        self.assertIn('self.socket_path.exists() or self.socket_path.is_symlink()',ipc)

    def test_live_reader_okx_error_never_direct(self):
        self.runtime.mkdir(mode=0o700)
        s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        s.bind(str(self.sock));s.listen(1)
        def serve():
            try:
                conn,_=s.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b'{"ok":false,"reason":"okx_rate_limit"}\n')
            finally:s.close()
        worker=threading.Thread(target=serve,daemon=True)
        worker.start()
        self.mode='PANIC'
        with self.assertRaises(Temporary):self.api.positions()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.call_log,[])
