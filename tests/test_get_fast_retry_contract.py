"""Real okx_api.build_api GET retry contract: pure offline fake transport."""
import http.client
import time
import types
import unittest
from pathlib import Path
from tests.test_stats_light_contract import IdleGetConnectionTest

class GetFirstTransportRetryContract(IdleGetConnectionTest):
    """Run existing baseline transport tests plus new real API.req fault injections."""
    def setUp(self):
        super().setUp()
        self.throttle_calls=[]
        self.gate.throttle=lambda route,sec:self.throttle_calls.append((route,sec))
        self.http_attempts=[]
        self.http_records=[]

    def scripted(self, steps):
        actions=list(steps)
        def fake_http(api, method, path, payload, headers):
            self.http_attempts.append((method, path.split('?',1)[0]))
            action=actions.pop(0)
            if isinstance(action, BaseException):
                raise action
            return action
        self.obj._http=types.MethodType(fake_http,self.obj)
        return actions

    @staticmethod
    def ok():
        return 200, {'code':'0','data':[]}

    def test_first_remote_disconnected_get_no_throttle_and_bounded(self):
        self.scripted([http.client.RemoteDisconnected('stale'),self.ok()])
        t=time.monotonic()
        self.assertEqual(self.obj.req('GET','/api/v5/trade/orders-pending'),[])
        self.assertLess(time.monotonic()-t,0.10)
        self.assertEqual(self.throttle_calls,[])
        self.assertEqual(len(self.http_attempts),2)
        self.assertEqual((self.gate.begins,self.gate.ends),(2,2))

    def test_two_transport_failures_then_success_backoff_two_seconds_only(self):
        self.scripted([ConnectionResetError('lost'),TimeoutError('lost'),self.ok()])
        self.assertEqual(self.obj.req('GET','/api/v5/trade/orders-pending'),[])
        self.assertEqual([s for _,s in self.throttle_calls],[2])
        self.assertEqual(len(self.http_attempts),3)
        self.assertEqual((self.gate.begins,self.gate.ends),(3,3))

    def test_three_transport_failures_have_2_then_4_second_backoff(self):
        self.scripted([OSError('broken'),OSError('broken'),OSError('broken'),self.ok()])
        self.assertEqual(self.obj.req('GET','/api/v5/trade/orders-pending'),[])
        self.assertEqual([s for _,s in self.throttle_calls],[2,4])
        self.assertEqual(len(self.http_attempts),4)

    def test_all_get_routes_share_same_rule(self):
        for route in ('/api/v5/trade/order','/api/v5/trade/fills-history',
                      '/api/v5/market/ticker'):
            with self.subTest(route=route):
                self.http_attempts.clear()
                self.throttle_calls.clear()
                self.scripted([ConnectionResetError('lost'),self.ok()])
                self.assertEqual(self.obj.req('GET',route),[])
                self.assertEqual(len(self.http_attempts),2)
                self.assertEqual(self.throttle_calls,[])

    def test_post_unknown_outcome_never_retries(self):
        for route,body in (('/api/v5/trade/order',{'clOrdId':'offline'}),
                           ('/api/v5/trade/cancel-order',{'ordId':'offline'})):
            with self.subTest(route=route):
                self.http_attempts.clear()
                self.throttle_calls.clear()
                self.scripted([http.client.RemoteDisconnected('stale')])
                with self.assertRaisesRegex(RuntimeError,'UNKNOWN POST OUTCOME'):
                    self.obj.req('POST',route,body=body)
                self.assertEqual(len(self.http_attempts),1)
                self.assertEqual(self.throttle_calls,[])

    def test_429_and_50011_keep_original_backoff(self):
        for status,code in ((429,'50011'),(200,'50011'),(503,'50011')):
            with self.subTest(status=status,code=code):
                self.http_attempts.clear()
                self.throttle_calls.clear()
                self.scripted([(status,{'code':code,'data':[]}),self.ok()])
                self.assertEqual(self.obj.req('GET','/api/v5/trade/orders-pending'),[])
                self.assertEqual([s for _,s in self.throttle_calls],[2])
                self.assertEqual(len(self.http_attempts),2)

    def test_http5xx_invalid_json_temporary_keeps_backoff(self):
        # The genuine _http parse path raises ExchangeTemporary BEFORE status
        # is assigned in req; it must not be confused with a transport failure.
        class Bad(self.Connection):
            def getresponse(_):
                return types.SimpleNamespace(status=503,will_close=True,
                                             read=lambda:b'not-json')
        self.obj._local.conn=Bad()
        self.assertEqual(self.obj.req('GET','/api/v5/trade/orders-pending'),[])
        self.assertEqual([s for _,s in self.throttle_calls],[2])
        self.assertEqual((self.gate.begins,self.gate.ends),(2,2))

    def test_transport_retry_uses_new_socket_real_http_path(self):
        parent=self
        class Broken(self.Connection):
            def request(_,method,*args,**kw):
                parent.attempts.append(method+'-broken')
                raise http.client.RemoteDisconnected('stale')
        self.obj._local.conn=Broken()
        self.assertEqual(self.obj.req('GET','/api/v5/trade/orders-pending'),[])
        self.assertEqual(self.attempts,['GET-broken','GET'])
        self.assertEqual((self.close_count,self.new_count),(1,1))
        self.assertEqual(self.throttle_calls,[])

    def test_transport_telemetry_exception_class_only(self):
        from okx_api import build_api
        # Static source guard complements real runtime fault-injection: prevent
        # accidental logging of str(exc), secrets or raw request text.
        text=(Path(__file__).resolve().parents[1]/'okx_api.py').read_text()
        self.assertIn("http_outcome='transport_'+type(exc).__name__",text)
        self.assertIn('trace_id=tp_trace,outcome=http_outcome',text)
        self.assertNotIn('outcome=str(exc)',text)
        self.assertNotIn('attempts=4 if method==\'POST\'',text)

if __name__ == '__main__':unittest.main()
