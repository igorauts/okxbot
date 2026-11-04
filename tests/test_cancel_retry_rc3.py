"""Bounded duplicate-safe same-ordId CANCEL recovery; no OKX network."""
import ast
import pathlib
import unittest
from decimal import Decimal
from types import SimpleNamespace

SRC=pathlib.Path(__file__).resolve().parents[1]/'okx_bot.py'
TREE=ast.parse(SRC.read_text())
FN=next(n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='retry_stale_elective_cancel')
CODE=compile(ast.Module(body=[FN],type_ignores=[]),str(SRC),'exec')
class Unresolved(Exception):pass
class Temp(Exception):pass
class Limit(Exception):pass
class Transport:
 def __init__(self,busy=False,approve=True):self.busy=busy;self.approve=approve;self.calls=[]
 def submit(self,**kw):
  if self.approve:
   kw['durable_prepare']();self.calls.append({k:v for k,v in kw.items() if k!='durable_prepare'})
  return self.approve
class RetryTests(unittest.TestCase):
 def setUp(self):
  self.saved=[];self.logs=[]
  self.g={'D':lambda x:Decimal(str(x if x is not None else 0)),
    'INST':'TEST-USDT-SWAP','CLEANUP_LAST_FILL_AT':1900,
    'TP_BUFFER_COOLDOWN':10.0,'ELECTIVE_CANCEL_RETRY_AFTER_SEC':30.0,
    'ELECTIVE_CANCEL_MAX_RETRIES':2,'ExchangeUnresolved':Unresolved,
    'ExchangeTemporary':Temp,'RateLimitedError':Limit,
    'time':SimpleNamespace(time=lambda:5000.,monotonic=lambda:2000.),
    'save':lambda st:self.saved.append(1),
    'log':lambda name,**kw:self.logs.append(name),
    'tp_records':lambda c:c.get('tpOrders',[])}
  self.g['A']=SimpleNamespace(order=lambda oid:self.order)
  exec(CODE,self.g)
  self.t=Transport()
  self.c={'cycleId':'CYC1','state':'TP_WAIT','buyPx':'10','tpPx':'11',
      'tpOrders':[{'phase':'CANCELING','ordId':'SELL1','clOrdId':'CS1','sz':'1',
                   'filled':'0','cancelAt':4900,'electiveCancel':True}]}
  self.st={'cycles':{'10':self.c}}
  self.order={'instId':'TEST-USDT-SWAP','side':'sell','state':'live',
      'px':'11','sz':'1','accFillSz':'0','reduceOnly':'true','ordId':'SELL1','clOrdId':'CS1'}
 def run_retry(self,pending=True):
  return self.g['retry_stale_elective_cancel'](self.st,[self.order] if pending else [],self.t)
 def test_retry_same_identity_once_and_durable_counter(self):
  self.assertTrue(self.run_retry())
  self.assertEqual(self.t.calls[0]['ord_id'],'SELL1')
  self.assertEqual(self.t.calls[0]['kind'],'TP')
  self.assertEqual(self.c['tpOrders'][0]['phase'],'CANCELING')
  self.assertEqual(self.c['tpOrders'][0]['electiveCancelRetryCount'],1)
  self.assertTrue(self.saved)
  self.assertFalse(self.run_retry())
 def test_not_quiet(self):
  self.g['CLEANUP_LAST_FILL_AT']=1995
  self.assertFalse(self.run_retry());self.assertEqual(self.t.calls,[])
 def test_not_after_30_sec(self):
  self.c['tpOrders'][0]['cancelAt']=4985
  self.assertFalse(self.run_retry());self.assertEqual(self.t.calls,[])
 def test_busy_transport(self):
  self.t.busy=True
  self.assertFalse(self.run_retry());self.assertEqual(self.t.calls,[])
 def test_missing_from_pending(self):
  self.assertFalse(self.run_retry(False));self.assertEqual(self.t.calls,[])
 def test_changed_order_identity_is_fail_closed(self):
  self.order['clOrdId']='FOREIGN'
  with self.assertRaises(Unresolved):self.run_retry()
  self.assertEqual(self.t.calls,[])
 def test_terminal_exact_get_blocks_retry(self):
  latest=self.order.copy();latest['state']='canceled'
  self.g['A'].order=lambda oid:latest
  self.assertFalse(self.run_retry());self.assertEqual(self.t.calls,[])
 def test_retries_exhausted_warn_once_and_no_post(self):
  self.c['tpOrders'][0]['electiveCancelRetryCount']=2
  self.assertFalse(self.run_retry())
  self.assertFalse(self.run_retry())
  self.assertEqual(self.logs.count('ELECTIVE_CANCEL_RETRY_EXHAUSTED'),1)
  self.assertFalse(self.t.calls)
 def test_buy_same_id_retry_no_new_exposure(self):
  c={'cycleId':'CYC2','state':'CANCELING','ordId':'BUY2',
    'clOrdId':'CB2','sz':'1','filledBuy':'0','buyPx':'9',
    'electiveCancel':True,'cancelAt':4900,'tpOrders':[]}
  self.st={'cycles':{'9':c}}
  self.order={'instId':'TEST-USDT-SWAP','side':'buy','state':'live',
    'px':'9','sz':'1','accFillSz':'0','ordId':'BUY2','clOrdId':'CB2'}
  self.assertTrue(self.run_retry())
  self.assertEqual(self.t.calls[0]['kind'],'BUY')
  self.assertEqual(self.t.calls[0]['ord_id'],'BUY2')
  self.assertEqual(c['state'],'CANCELING')
 def test_transport_refusal_does_not_mutate_state(self):
  self.t.approve=False
  self.assertFalse(self.run_retry())
  self.assertEqual(self.c['tpOrders'][0].get('electiveCancelRetryCount'),None)

if __name__=='__main__':unittest.main(verbosity=2)
