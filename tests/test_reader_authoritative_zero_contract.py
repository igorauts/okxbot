import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from okx_position_reader_core import build_snapshot, SnapshotError
from okx_position_reader_client import read_positions, PositionReaderEmpty, PositionReaderUnavailable
from okx_position_reader_service import ReadOnlyOKXProvider

A='INIT-USDT-SWAP'
B='BTC-USDT-SWAP'
SCOPE={'endpoint':'/api/v5/account/positions','instType':'SWAP',
       'instIds':[A,B],'complete':True}

def snap(rows, marker=None, instruments=(A,B)):
    response={'code':'0','data':rows}
    if marker is not None: response['_reader_transport_scope']=marker
    now=time.monotonic_ns()
    return build_snapshot(response=response,instruments=instruments,
                          started_ns=now-1000,completed_ns=now)

class ZeroProof(unittest.TestCase):
    def test_only_complete_scope_proves_zero(self):
        s=snap([{'instId':B,'posSide':'net','pos':'1'}],SCOPE)
        self.assertTrue(s.slice(A,not_before_ns=s.started_ns)['absence_is_proven_zero'])
        self.assertEqual(s.slice(A,not_before_ns=s.started_ns)['rows'],[])
        self.assertFalse(s.slice(B,not_before_ns=s.started_ns)['absence_is_proven_zero'])
    def test_valid_empty_complete(self):
        s=snap([],SCOPE)
        self.assertTrue(s.slice(A,not_before_ns=s.started_ns)['absence_is_proven_zero'])
    def test_no_scope_must_not_prove_zero(self):
        s=snap([])
        self.assertFalse(s.slice(A,not_before_ns=s.started_ns)['absence_is_proven_zero'])
    def test_invalid_scopes_rejected_as_zero_proofs(self):
        bad=[{},dict(SCOPE,complete=False),dict(SCOPE,instIds=[B,A]),
             dict(SCOPE,instType='FUTURES'),dict(SCOPE,endpoint='/bad'),
             dict(SCOPE,instIds=[A]),dict(SCOPE,complete='true')]
        for marker in bad:
            with self.subTest(marker=marker):
                s=snap([],marker)
                self.assertFalse(s.slice(A,not_before_ns=s.started_ns)['absence_is_proven_zero'])
    def test_unexpected_instrument_invalidates_zero_proof(self):
        s=snap([{'instId':'ETH-USDT-SWAP','posSide':'net','pos':'2'}],SCOPE)
        self.assertFalse(s.slice(A,not_before_ns=s.started_ns)['absence_is_proven_zero'])
    def test_malformed_unknown_row_fail_closed(self):
        with self.assertRaises(SnapshotError):snap([{'pos':'1'}],SCOPE)
    def test_bad_net_row_isolated(self):
        s=snap([{'instId':A,'posSide':'long','pos':'3'}],SCOPE)
        with self.assertRaises(SnapshotError):s.slice(A,not_before_ns=s.started_ns)
        self.assertTrue(s.slice(B,not_before_ns=s.started_ns)['absence_is_proven_zero'])
    def test_duplicate_rows_isolated(self):
        row={'instId':A,'posSide':'net','pos':'3'}
        s=snap([row,row],SCOPE)
        with self.assertRaises(SnapshotError):s.slice(A,not_before_ns=s.started_ns)
    def test_stale_barrier_rejected(self):
        s=snap([],SCOPE)
        with self.assertRaises(SnapshotError):s.slice(A,not_before_ns=s.started_ns+1)
    def test_provider_marker_exact_scope_and_no_http(self):
        provider=ReadOnlyOKXProvider('x','y','z')
        called=[]
        def fake(path):
            called.append(path)
            return {'code':'0','data':[]}
        provider._sync_get_path=fake
        actual=provider._sync_get((A,B))
        self.assertEqual(actual['_reader_transport_scope'],SCOPE)
        self.assertEqual(len(called),1)
        self.assertIn('instType=SWAP',called[0])
        self.assertIn('instId=',called[0])

class IPCZero(unittest.TestCase):
    def exchange(self, proof):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'r.sock')
            server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            errors=[]
            def peer():
                try:
                    sock,_=server.accept()
                    with sock:
                        buf=b''
                        while not buf.endswith(b'\n'):
                            buf+=sock.recv(4096)
                        import json
                        msg=json.loads(buf)
                        started=time.monotonic_ns()
                        response={'ok':True,'data':{
                            'instrument':A,'trace_id':msg['trace_id'],
                            'get_started_mono_ns':started,
                            'get_completed_mono_ns':time.monotonic_ns(),
                            'covered_instruments':[A], 'rows':[],
                            'absence_is_proven_zero':proof}}
                        sock.sendall((json.dumps(response)+'\n').encode())
                except Exception as e:errors.append(e)
            thread=threading.Thread(target=peer,daemon=True)
            thread.start()
            try:
                if proof:
                    self.assertEqual(read_positions(path,A,account_uid='verified',trace_id='trace'),[])
                else:
                    with self.assertRaises(PositionReaderEmpty):
                        read_positions(path,A,account_uid='verified',trace_id='trace')
            finally:
                thread.join(2)
                server.close()
            self.assertFalse(errors,errors)
    def test_zero_with_proof(self): self.exchange(True)
    def test_zero_without_proof(self): self.exchange(False)

if __name__=='__main__':unittest.main()
