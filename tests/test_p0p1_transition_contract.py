"""Transitional old/new Reader/engine safety. Local mocks only; no sockets sent."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from okx_position_reader_client import read_positions, PositionReaderUnavailable
from tests.test_reader_narrow_fallback_contract import NarrowFallback, Temporary

class TransitionContract(unittest.TestCase):
    def fixture(self):
        case=NarrowFallback(methodName='test_reader_down_normal_never_direct_get')
        case.setUp()
        return case

    def test_old_reader_ok_false_without_reason_cannot_trigger_emergency_direct_get(self):
        # An old Reader replies ok:false WITHOUT reason. The new client must
        # NEVER mistake that for a dead process / unavailable socket.
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);base.chmod(0o700)
            class Sock:
                def __enter__(self):return self
                def __exit__(self,*a):pass
                def settimeout(self,*a):pass
                def connect(self,*a):pass
                def sendall(self,*a):pass
                def recv(self,*a):return b'{"ok":false,"error":"ReaderError"}\n'
            with mock.patch('okx_position_reader_client.socket.socket',return_value=Sock()):
                with self.assertRaises(PositionReaderUnavailable) as cm:
                    read_positions(str(base/'reader.sock'),'BTC-USDT-SWAP',account_uid='TEST')
            self.assertEqual(cm.exception.reason,'reader_rejected')
        c=self.fixture();c.mode='PANIC'
        with mock.patch('okx_position_reader_client.read_positions',side_effect=cm.exception):
            with self.assertRaises(Temporary):c.obj.positions()
        self.assertEqual(c.calls,[])

    def test_new_engine_reads_successful_old_reader_without_extra_direct_get(self):
        c=self.fixture();c.exit=True
        expected=[{'source':'reader','validated':True}]
        with mock.patch('okx_position_reader_client.read_positions',return_value=expected) as reader:
            self.assertEqual(c.obj.positions(),expected)
        self.assertEqual(reader.call_count,1)
        self.assertEqual(c.calls,[])

    def test_old_reader_dead_does_not_unblock_normal_get(self):
        c=self.fixture()
        with mock.patch('okx_position_reader_client.read_positions',
                        side_effect=PositionReaderUnavailable('socket failed',reason='reader_down')):
            with self.assertRaises(Temporary):c.obj.positions()
        self.assertEqual(c.calls,[])

    def test_reader_upstream_rate_limit_fails_closed_in_emergency(self):
        c=self.fixture();c.mode='HARD'
        with mock.patch('okx_position_reader_client.read_positions',
                        side_effect=PositionReaderUnavailable('upstream',reason='okx_rate_limit')):
            with self.assertRaises(Temporary):c.obj.positions()
        self.assertEqual(c.calls,[])
