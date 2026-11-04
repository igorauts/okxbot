"""Exact order/coverage isolation regressions. Offline: no OKX API or disk writes."""
import ast
import copy
import pathlib
import unittest
from decimal import Decimal
from types import SimpleNamespace

SOURCE=pathlib.Path(__file__).resolve().parents[1]/'okx_bot.py'
TREE=ast.parse(SOURCE.read_text(encoding='utf-8'))
NAMES=('track','tp_record_update','tp_records','cycle_sell_fill','active_tp_records',
       'real_tp_active','active_buy','virtual_cycles','coverage_audit','remaining_tp',
       'unprotected_partial_buy')
code=compile(ast.Module(body=[x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name in NAMES],type_ignores=[]),str(SOURCE),'exec')

class FailClosed(RuntimeError):pass

class Cases(unittest.TestCase):
 def setUp(self):
  self.calls=[]; self.orders={}
  self.g={'INST':'TEST-USDT-SWAP','D':lambda x:Decimal(str(x if x is not None else 0)),
      'REDUCE_ONLY_HARD_CAP':150,'Decimal':Decimal,
      'die':lambda msg:(_ for _ in ()).throw(FailClosed(msg)),
      'reconcile_aggregate':lambda st:True,'trip_if_margin_cancel':lambda *a:None,
      'note_cleanup_fill':lambda :None,'save':lambda st:self.calls.append('save'),
      'log':lambda *a,**kw:self.calls.append(a[0]),
      'journal_tp':lambda *a,**kw:None,
      'A':SimpleNamespace(order=lambda oid=None,cid=None:self.orders.get(oid or cid)),
      'paged_order':lambda *a:None,
      'parent_tp_failure':lambda *a:None,
      'post_absence_confirmed':lambda *a:False,
      'resolve_partial_cancel':lambda st,c,filled:None,
      'time':SimpleNamespace(time=lambda:1_000_000.),
  }
  exec(code,self.g)
 def sell(self,elective=True,phase='CANCELING'):
  rec={'phase':phase,'ordId':'SELL1','clOrdId':'CSELL','source':'attached',
       'sz':'1','filled':'0','cancelAt':999_000}
  if elective:rec['electiveCancel']=True
  c={'state':'TP_WAIT','cycleId':'CYCLE1','buyPx':'10','tpPx':'11',
     'sz':'1','filledBuy':'1','filledTP':'0','tpOrders':[rec]}
  st={'cycles':{'10':c},'aggregate_exits':[]}
  order={'ordId':'SELL1','clOrdId':'CSELL','instId':'TEST-USDT-SWAP','side':'sell','state':'live',
         'px':'11','sz':'1','accFillSz':'0','reduceOnly':'true'}
  return st,order
 def buy(self,elective=True):
  c={'state':'CANCELING','cycleId':'CYCLE1','buyPx':'10','tpPx':'11',
     'clOrdId':'CBUY','ordId':'BUY1','sz':'1','filledBuy':'0','filledTP':'0','tpOrders':[],
     'cancelAt':999_000}
  if elective:c['electiveCancel']=True
  st={'cycles':{'10':c},'aggregate_exits':[]}
  o={'ordId':'BUY1','clOrdId':'CBUY','instId':'TEST-USDT-SWAP','side':'buy','state':'live',
     'px':'10','sz':'1','accFillSz':'0','reduceOnly':'false'}
  return st,o
 def test_tp_pending_verified_isolated_and_reserved(self):
  st,o=self.sell()
  self.assertTrue(self.g['track'](st,[o]))
  self.assertEqual(st['cycles']['10']['tpOrders'][0]['phase'],'CANCELING')
  self.assertEqual(len(self.g['real_tp_active'](st)),1)
  self.assertEqual(self.g['coverage_audit'](st,[o],Decimal('1')),
                   (1,Decimal('1'),Decimal('0')))
 def test_tp_without_elective_marker_remains_frozen(self):
  st,o=self.sell(elective=False)
  self.assertFalse(self.g['track'](st,[o]))
 def test_tp_not_in_fresh_pending_must_remain_frozen(self):
  st,o=self.sell();self.orders['SELL1']=o
  self.assertFalse(self.g['track'](st,[]))
 def test_tp_wrong_price_rejected_not_isolated(self):
  st,o=self.sell();o['px']='12'
  with self.assertRaises(FailClosed):self.g['track'](st,[o])
 def test_tp_wrong_client_id_rejected(self):
  st,o=self.sell();o['clOrdId']='OTHER'
  with self.assertRaises(FailClosed):self.g['track'](st,[o])
 def test_tp_not_elective_other_placing_blocks(self):
  st,o=self.sell();st['cycles']['20']={'state':'PLACING','cycleId':'CYCLE2',
   'clOrdId':'PENDINGBUY','sz':'1','buyPx':'20','tpPx':'21','filledBuy':'0','filledTP':'0','tpOrders':[]}
  self.assertFalse(self.g['track'](st,[o]))
 def test_tp_cancel_approved_reduces_exchange_coverage_and_virtualizes(self):
  st,o=self.sell();o['state']='canceled'
  self.assertTrue(self.g['track'](st,[o]))
  self.assertEqual(st['cycles']['10']['state'],'VIRTUAL_TP')
  self.assertEqual(st['cycles']['10']['tpOrders'][0]['phase'],'CANCELED')
  self.assertNotIn('electiveCancel',st['cycles']['10']['tpOrders'][0])
 def test_buy_pending_verified_isolated_and_reserved(self):
  st,o=self.buy();self.assertTrue(self.g['track'](st,[o]))
  self.assertEqual(len(self.g['active_buy'](st)),1)
  self.assertEqual(st['cycles']['10']['state'],'CANCELING')
 def test_buy_unmarked_cancellation_blocks(self):
  st,o=self.buy(False);self.assertFalse(self.g['track'](st,[o]))
 def test_buy_not_in_fresh_pending_blocks(self):
  st,o=self.buy();self.orders['BUY1']=o
  self.assertFalse(self.g['track'](st,[]))
 def test_buy_wrong_size_blocks(self):
  st,o=self.buy();o['sz']='2';self.assertFalse(self.g['track'](st,[o]))
 def test_buy_wrong_client_blocks(self):
  st,o=self.buy();o['clOrdId']='DIFFERENT';self.assertFalse(self.g['track'](st,[o]))
 def test_invalid_coverage_still_blocks(self):
  st,o=self.sell();self.assertTrue(self.g['track'](st,[o]))
  with self.assertRaises(Exception):self.g['coverage_audit'](st,[o],Decimal('2'))
 def test_rest_in_flight_blocks_strategy_even_if_live(self):
  s=SOURCE.read_text()
  self.assertIn('if elective_cancel_transport.busy:\n                all_resolved=False',s)
  self.assertLess(s.index('if elective_cancel_transport.busy:\n                all_resolved=False'),s.index('if not all_resolved:\n                buy_cancel_count'))

if __name__=='__main__':unittest.main(verbosity=2)
