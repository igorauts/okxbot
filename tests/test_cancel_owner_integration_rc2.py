"""Exec the actual owner-side functions from staged candidate AST, mocked effects only."""
import ast,unittest,time,threading
from pathlib import Path
from types import SimpleNamespace
from okx_cleanup_transport import SingleOwnerCancelTransport

SRC=Path(__file__).resolve().parents[1]/'okx_bot.py'
T=ast.parse(SRC.read_text())
FN={n.name:n for n in T.body if isinstance(n,ast.FunctionDef)}
NAMES=('queue_elective_tp_cancel','queue_elective_buy_cancel','consume_elective_cancel')
class Test(unittest.TestCase):
    def setUp(self):
        self.saved=[]; self.logs=[];self.journal=[]; self.sent=[];self.wake=threading.Event()
        self.state={'cycles':{'100':{'cycleId':'id1','ordId':'BUY1','state':'BUY_LIVE','buyPx':'100',
                              'tpOrders':[{'ordId':'TP1','clOrdId':'tcl1','phase':'ACTIVE'}]}}}
        self.ns={'save':lambda st:self.saved.append('saved'),
                'log':lambda *a,**k:self.logs.append((a,k)),
                'journal_tp':lambda *a,**k:self.journal.append((a,k)),
                'tp_records':lambda c:c['tpOrders'],
                'ExchangeUnresolved':type('ExchangeUnresolved',(Exception,),{}),
                'die':lambda msg:(_ for _ in ()).throw(RuntimeError(msg)),
                'time':time}
        exec(compile(ast.Module(body=[FN[n] for n in NAMES],type_ignores=[]),str(SRC),'exec'),self.ns)
    def transport(self,send):
        return SingleOwnerCancelTransport(send,lambda:False,self.wake.set,interval_s=.1)
    def completion(self,t):
        self.assertTrue(self.wake.wait(3));self.wake.clear()
        return self.ns['consume_elective_cancel'](self.state,t)
    def test_tp_cancel_intent_is_durable_before_post(self):
        def send(oid):
            self.assertEqual(self.saved,['saved']);self.assertEqual(self.journal[0][0][0],'CANCEL_INTENT')
            self.sent.append(oid);return [{'ordId':oid,'sCode':'0'}]
        t=self.transport(send);c=self.state['cycles']['100'];r=c['tpOrders'][0]
        self.assertTrue(self.ns['queue_elective_tp_cancel'](self.state,c,r,t))
        self.assertEqual(r['phase'],'CANCELING')
        self.assertEqual(self.completion(t),'ACK_PENDING_RECONCILIATION')
        self.assertEqual(self.sent,['TP1'])
        self.assertEqual(r['phase'],'CANCELING') # only GET can finalize
    def test_buy_cancel_fsync_before_post_and_no_premature_fill(self):
        def send(oid):
            self.assertEqual(self.saved,['saved']);return [{'ordId':oid,'sCode':'0'}]
        t=self.transport(send);c=self.state['cycles']['100']
        self.assertTrue(self.ns['queue_elective_buy_cancel'](self.state,c,t))
        self.assertEqual(c['state'],'CANCELING')
        self.assertEqual(self.completion(t),'ACK_PENDING_RECONCILIATION')
        self.assertEqual(c['state'],'CANCELING')
    def test_rejected_cancel_keeps_intent_and_persists_reject(self):
        t=self.transport(lambda oid:[{'ordId':oid,'sCode':'51000'}]);c=self.state['cycles']['100'];r=c['tpOrders'][0]
        self.assertTrue(self.ns['queue_elective_tp_cancel'](self.state,c,r,t))
        self.assertEqual(self.completion(t),'REJECTED')
        self.assertEqual(r['phase'],'CANCELING')
        self.assertEqual(r['cancelRejectCode'],'51000')
        self.assertEqual(len(self.saved),2)
    def test_uncertain_post_never_frees_reservation(self):
        def send(oid):raise TimeoutError('unknown')
        t=self.transport(send);c=self.state['cycles']['100']
        self.assertTrue(self.ns['queue_elective_buy_cancel'](self.state,c,t))
        self.assertEqual(self.completion(t),'UNKNOWN')
        self.assertEqual(c['state'],'CANCELING')
    def test_identity_mismatch_refused(self):
        t=self.transport(lambda oid:[{'ordId':'wrong','sCode':'0'}]);c=self.state['cycles']['100']
        self.assertTrue(self.ns['queue_elective_buy_cancel'](self.state,c,t))
        with self.assertRaises(self.ns['ExchangeUnresolved']):self.completion(t)
        self.assertEqual(c['state'],'CANCELING')
    def test_late_preempt_known_no_post_rearms_for_fresh_reconcile(self):
        observations=[False,False,True]
        def urgent():return observations.pop(0) if observations else True
        t=SingleOwnerCancelTransport(lambda oid:self.fail('must not send'),urgent,self.wake.set)
        c=self.state['cycles']['100'];r=c['tpOrders'][0]
        self.assertTrue(self.ns['queue_elective_tp_cancel'](self.state,c,r,t))
        self.assertEqual(self.completion(t),'NOT_SENT_RECONCILE_REQUIRED')
        self.assertEqual(r['phase'],'ACTIVE')
        self.assertEqual(len(self.saved),2)

    def test_bypass_urgent_via_transport_check(self):
        t=SingleOwnerCancelTransport(lambda oid:self.fail('must not send'),lambda:True,self.wake.set)
        c=self.state['cycles']['100']
        self.assertFalse(self.ns['queue_elective_tp_cancel'](self.state,c,c['tpOrders'][0],t))
        self.assertEqual(c['tpOrders'][0]['phase'],'ACTIVE')
        self.assertEqual(self.saved,[])

if __name__=='__main__':unittest.main(verbosity=2)
