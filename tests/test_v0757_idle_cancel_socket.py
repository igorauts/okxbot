"""Candidate-only isolated regressions: elective CANCEL stale TLS, no OKX network."""
import os
import pathlib
import sys
import time
import unittest
from unittest import mock

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from tests.test_stats_light_contract import IdleGetConnectionTest
from okx_api import _elective_cancel_idle_socket
from okx_latency import LatencyJournal


class ElectiveCancelIdleTest(IdleGetConnectionTest):
    def send(self):
        return self.obj.req('POST','/api/v5/trade/cancel-order',
                            body={'instId':'BTC-USDT-SWAP','ordId':'123'},
                            background=True,elective_cancel=True)

    def test_15s_idle_preclosed_and_only_one_post(self):
        self.obj.req('GET','/api/v5/market/ticker')
        self.obj._local.conn_last_used=time.monotonic()-16.0
        self.send()
        self.assertEqual((self.new_count,self.close_count),(2,1))
        self.assertEqual(self.attempts,['GET','POST'])
        self.assertEqual(self.gate.begins,self.gate.ends)

    def test_fresh_elective_cancel_reuses_socket(self):
        self.send()
        self.send()
        self.assertEqual((self.new_count,self.close_count),(1,0))
        self.assertEqual(self.attempts,['POST','POST'])

    def test_exact_15s_boundary_is_not_preclosed(self):
        self.send()
        self.obj._local.conn_last_used=time.monotonic()-14.0
        self.send()
        self.assertEqual(self.close_count,0)

    def test_unknown_last_used_precloses(self):
        self.send()
        self.obj._local.conn_last_used=None
        self.send()
        self.assertEqual((self.new_count,self.close_count),(2,1))

    def test_no_opt_in_does_not_rotate_cancel(self):
        self.send()
        self.obj._local.conn_last_used=time.monotonic()-30
        self.obj.req('POST','/api/v5/trade/cancel-order',
                     body={'instId':'BTC-USDT-SWAP','ordId':'123'},background=True)
        self.assertEqual((self.new_count,self.close_count),(1,0))

    def test_no_other_post_can_opt_in(self):
        with self.assertRaisesRegex(ValueError,'exact background CANCEL'):
            self.obj.req('POST','/api/v5/trade/order',body={'clOrdId':'123'},
                         background=True,elective_cancel=True)
        with self.assertRaisesRegex(ValueError,'exact background CANCEL'):
            self.obj.req('GET','/api/v5/trade/cancel-order',elective_cancel=True)
        with self.assertRaisesRegex(ValueError,'exact background CANCEL'):
            self.obj.req('POST','/api/v5/trade/cancel-order',
                         background=False,elective_cancel=True)
        self.assertEqual(self.attempts,[])
        self.assertEqual(self.gate.begins,0)

    def test_uncertain_post_never_retries(self):
        parent=self
        class Broken(self.Connection):
            def request(self,method,*args,**kw):
                parent.attempts.append(method+'-broken')
                raise ConnectionResetError('fake reset')
        self.obj._local.conn=Broken()
        self.obj._local.conn_last_used=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'UNKNOWN POST OUTCOME'):
            self.send()
        self.assertEqual(self.attempts,['POST-broken'])
        self.assertEqual((self.gate.begins,self.gate.ends),(1,1))
        self.assertIsNone(self.obj._local.conn)

    def test_stale_broken_conn_is_never_sent_to(self):
        parent=self
        class Broken(self.Connection):
            def request(self,method,*args,**kw):
                parent.attempts.append('SHOULD_NOT_SEND')
                raise ConnectionResetError('fake reset')
        self.obj._local.conn=Broken()
        self.obj._local.conn_last_used=time.monotonic()-70
        self.send()
        self.assertEqual(self.attempts,['POST'])
        self.assertEqual(self.close_count,1)

    def test_no_reconnect_on_other_mutating_routes(self):
        self.obj.req('GET','/api/v5/market/ticker')
        self.obj._local.conn_last_used=time.monotonic()-70
        self.obj.req('POST','/api/v5/trade/order',body={'clOrdId':'TEST'})
        self.assertEqual((self.close_count,self.new_count),(0,1))

    def test_idle_socket_diagnostic_fields_are_sanitized(self):
        j=LatencyJournal('/unused',enabled=True)
        j._thread=object()  # no filesystem writer
        j.record('cancel_idle_socket',0,route='/api/v5/trade/cancel-order',
                 method='POST',idle_ms=16500.0,outcome='rotated_idle',
                 secret='DO_NOT_LOG')
        rec=j.queue.get_nowait()
        self.assertEqual((rec['idle_ms'],rec['outcome']),(16500.,'rotated_idle'))
        self.assertNotIn('secret',rec)
        j.record('cancel_post_transport_error',0,route='/api/v5/trade/cancel-order',
                 outcome='RemoteDisconnected')
        self.assertEqual(j.queue.get_nowait()['outcome'],'RemoteDisconnected')


if __name__=='__main__': unittest.main(verbosity=2)
