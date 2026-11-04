import threading,time,unittest
from okx_cleanup_transport import SingleOwnerCancelTransport

class Tests(unittest.TestCase):
    def setUp(self):
        self.wake=threading.Event();self.urgent=False;self.prepared=[];self.sent=[]
    def tr(self,send=None,interval=.1):
        if send is None:send=lambda oid:self.sent.append(oid) or [{'ordId':oid,'sCode':'0'}]
        return SingleOwnerCancelTransport(send,lambda:self.urgent,self.wake.set,
            interval_s=interval)
    def consume(self,t):
        self.assertTrue(self.wake.wait(2));self.wake.clear()
        item=t.consume();self.assertIsNotNone(item);return item
    def test_full_ack_requires_owner_to_reconcile(self):
        t=self.tr();self.assertTrue(t.submit(kind='TP',ord_id='77',token='cyc',
            durable_prepare=lambda:self.prepared.append('durable')))
        v=self.consume(t);self.assertEqual(self.prepared,['durable']);self.assertEqual(self.sent,['77']);self.assertEqual(v.reply[0]['ordId'],'77')
        self.assertIsNone(t.consume())
    def test_blocked_post_never_blocks_owner(self):
        gate=threading.Event();started=threading.Event()
        def send(oid):started.set();gate.wait(1);return [{'ordId':oid,'sCode':'0'}]
        t=self.tr(send);begin=time.monotonic()
        self.assertTrue(t.submit(kind='BUY',ord_id='123',token='cyc',durable_prepare=lambda:None))
        self.assertLess(time.monotonic()-begin,.1)
        self.assertTrue(started.wait(.5))
        self.assertTrue(t.busy)
        self.assertFalse(t.submit(kind='BUY',ord_id='2',token='cyc2',durable_prepare=lambda:self.fail('must not run')))
        gate.set();self.assertEqual(self.consume(t).ord_id,'123')
    def test_fsync_failure_does_not_send(self):
        t=self.tr()
        def fail():raise OSError('disk')
        with self.assertRaises(OSError):t.submit(kind='TP',ord_id='1',token='t',durable_prepare=fail)
        self.assertFalse(t.busy);self.assertEqual(self.sent,[])
    def test_event_preempts_before_durable_change(self):
        t=self.tr();self.urgent=True
        self.assertFalse(t.submit(kind='TP',ord_id='1',token='t',durable_prepare=lambda:self.fail('unexpected')))
        self.assertFalse(t.busy);self.assertFalse(self.sent)
    def test_ambiguous_result_no_retry(self):
        calls=[]
        def send(oid):calls.append(oid);raise TimeoutError('uncertain')
        t=self.tr(send)
        self.assertTrue(t.submit(kind='BUY',ord_id='1',token='t',durable_prepare=lambda:None))
        self.assertIsInstance(self.consume(t).error,TimeoutError)
        self.assertEqual(calls,['1'])
    def test_spacing_and_no_second_request(self):
        t=self.tr();self.assertTrue(t.submit(kind='TP',ord_id='1',token='t',durable_prepare=lambda:None))
        self.consume(t)
        self.assertFalse(t.submit(kind='BUY',ord_id='2',token='t2',durable_prepare=lambda:self.fail('unexpected')))
        time.sleep(.11)
        self.assertTrue(t.submit(kind='BUY',ord_id='2',token='t2',durable_prepare=lambda:None))
        self.consume(t)
        self.assertEqual(self.sent,['1','2'])
    def test_bad_order_rejected_without_io(self):
        t=self.tr()
        for kind,oid,tok in [('X','1','t'),('TP','','t'),('TP','1','')]:
            with self.assertRaises(ValueError):t.submit(kind=kind,ord_id=oid,token=tok,durable_prepare=lambda:None)
        self.assertFalse(self.sent)
    def test_refusal_when_cancel_thread_busy_even_after_urgent_is_cleared(self):
        g=threading.Event();t=self.tr(lambda oid:g.wait(.5) or [{'ordId':oid,'sCode':'0'}])
        self.assertTrue(t.submit(kind='TP',ord_id='1',token='t',durable_prepare=lambda:None))
        self.urgent=True
        self.assertFalse(t.submit(kind='TP',ord_id='2',token='t2',durable_prepare=lambda:None))
        self.urgent=False
        self.assertFalse(t.submit(kind='TP',ord_id='2',token='t2',durable_prepare=lambda:None))
        g.set();self.consume(t)
    def test_urgent_arrival_after_fsync_preempts_before_post(self):
        observations=[False,False,True]
        def urgent():return observations.pop(0) if observations else True
        t=SingleOwnerCancelTransport(lambda oid:self.fail('must not send'),
                                     urgent,self.wake.set,interval_s=.1)
        self.assertTrue(t.submit(kind='TP',ord_id='1',token='t',durable_prepare=lambda:self.prepared.append('saved')))
        completion=self.consume(t)
        self.assertFalse(completion.sent)
        self.assertIsNone(completion.error)
        self.assertEqual(self.prepared,['saved'])

    def test_capacity_emergency_ignores_only_ordinary_fill_wake(self):
        t=SingleOwnerCancelTransport(lambda oid:self.sent.append(oid) or [{'ordId':oid,'sCode':'0'}],
            urgent_pending=lambda:True,critical_pending=lambda:False,
            wake_owner=self.wake.set)
        self.assertTrue(t.submit(kind='TP',ord_id='capacity',token='t',
            durable_prepare=lambda:None,emergency=True))
        self.assertTrue(self.consume(t).sent)
        self.assertEqual(self.sent,['capacity'])

    def test_critical_exit_preempts_even_capacity_emergency(self):
        t=SingleOwnerCancelTransport(lambda oid:self.fail('must not send'),
            urgent_pending=lambda:False,critical_pending=lambda:True,
            wake_owner=self.wake.set)
        self.assertFalse(t.submit(kind='TP',ord_id='capacity',token='t',
            durable_prepare=lambda:self.fail('must not persist'),emergency=True))

    def test_100ms_floor_enforced(self):
        with self.assertRaises(ValueError):self.tr(interval=.099)

if __name__=='__main__':unittest.main(verbosity=2)
