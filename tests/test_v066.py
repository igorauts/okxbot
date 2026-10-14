"""Release v0.6.7 safety regression tests: no network, mock OKX exact IDs."""
import unittest
import json
import time
from decimal import Decimal as D
from unittest.mock import patch
from test_grid import EngineTests


class PartialExitV067(unittest.TestCase):
    def setUp(self):
        self.td,self.e=EngineTests().make_engine('SOL','106','120','.03','.13','.04')
        self.addCleanup(self.td.cleanup)
        self.c={'state':'BUY_PARTIAL','buyPx':'106.00','tpPx':'106.13',
                'sz':'0.04','filledBuy':'0.02','filledTP':'0','ordId':'B1',
                'clOrdId':'CB1','tpClOrdId':'CT1','cycleId':'sol-CB1',
                'tpOrders':[],'partial_since':time.time()-100}
        self.st={'version':self.e.VER,'instrument':self.e.INST,
                 'cycles':{'106.00':self.c},'archived_cycles':[],
                 'completed':0}
        self.buy={'instId':self.e.INST,'side':'buy','ordId':'B1',
                  'clOrdId':'CB1','state':'canceled','accFillSz':'0.02',
                  'sz':'0.04','px':'106.00'}
        self.sell={'instId':self.e.INST,'side':'sell','ordId':'S1',
                   'clOrdId':'CT1','state':'live','accFillSz':'0',
                   'sz':'0.02','px':'106.13','reduceOnly':'true','ordType':'limit'}

    def test_time_distance_and_only_up(self):
        self.assertFalse(self.e.partial_cleanup_due(self.c,D('106.14')))
        self.assertTrue(self.e.partial_cleanup_due(self.c,D('106.15')))
        self.assertFalse(self.e.partial_cleanup_due(self.c,D('105')))
        self.assertFalse(self.e.partial_cleanup_due(self.c,D('106.15'),now=self.c['partial_since']+59))
        self.assertTrue(self.e.partial_cleanup_due(self.c,D('106.15'),now=self.c['partial_since']+60))

    def test_cleanup_exact_sequence_and_no_duplicate_market_on_restart(self):
        calls=[]
        def fake_req(method,route,body=None,**kwargs):
            calls.append((method,route,body))
            if body and body.get('ordType')=='market':return [{'sCode':'0','ordId':'S2'}]
            return [{'sCode':'0'}]
        # 60 seconds elapsed and 5 grid steps above BUY -> cancel BUY remainder.
        with patch.object(self.e.A,'req',side_effect=fake_req):
            self.assertEqual(self.e.partial_cleanup_action(self.st,D('107'),D('.02'),'RUN'),'ACTION')
        self.assertEqual(self.c['state'],'CANCELING')
        self.assertTrue(self.c['partial_cleanup'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1],'/api/v5/trade/cancel-order')
        # Exchange generates real, unique attached TP after canceling partial BUY.
        self.e.register_cancel_attached(self.st,[self.sell])
        with patch.object(self.e.A,'order',side_effect=lambda oid='',cid='': self.buy if oid=='B1' else self.sell):
            self.assertTrue(self.e.track(self.st,[self.sell]))
        self.assertEqual(self.c['state'],'TP_WAIT')
        self.e.coverage_audit(self.st,[self.sell],D('.02'))
        with patch.object(self.e.A,'req',side_effect=fake_req):
            self.assertEqual(self.e.partial_cleanup_action(self.st,D('107'),D('.02'),'RUN'),'ACTION')
        self.assertEqual(self.c['tpOrders'][0]['phase'],'CANCELING')
        self.assertEqual(calls[1][2]['ordId'],'S1')
        # A cancellation ACK alone must NEVER trigger MARKET order.
        with patch.object(self.e.A,'req',side_effect=AssertionError('Too early for market')):
            self.assertEqual(self.e.partial_cleanup_action(self.st,D('107'),D('.02'),'RUN'),'WAIT')
        # Exchange confirms TP cancellation. No TP fill.
        self.sell['state']='canceled'
        with patch.object(self.e.A,'order',return_value=self.sell):
            self.assertTrue(self.e.track(self.st,[]))
        self.assertEqual(self.c['state'],'VIRTUAL_TP')
        with patch.object(self.e.A,'req',side_effect=fake_req):
            self.assertEqual(self.e.partial_cleanup_action(self.st,D('107'),D('.02'),'RUN'),'ACTION')
        self.assertEqual(calls[-1][2]['ordType'],'market')
        self.assertEqual(calls[-1][2]['sz'],'0.02')
        self.assertEqual(calls[-1][2]['reduceOnly'],True)
        self.assertEqual(self.c['state'],'TP_WAIT')
        # Pretend the process restarted after the ACK but before REST status.
        with patch.object(self.e.A,'req',side_effect=AssertionError('NO SECOND MARKET')):
            self.assertEqual(self.e.partial_cleanup_action(self.st,D('107'),D('.02'),'RUN'),'WAIT')
        market={'instId':self.e.INST,'side':'sell','ordId':'S2',
                'clOrdId':self.c['tpOrders'][-1]['clOrdId'],'state':'filled',
                'accFillSz':'0.02','sz':'0.02','px':'0',
                'ordType':'market','reduceOnly':'true'}
        with patch.object(self.e.A,'order',return_value=market):
            self.assertTrue(self.e.track(self.st,[]))
        self.assertEqual(self.c['state'],'EXITED')
        self.assertEqual(self.st.get('partial_exits'),1)
        self.assertEqual(self.st['completed'],0)
        self.assertEqual(self.e.count(self.st),0)
        self.e.coverage_audit(self.st,[],D('0'))

    def test_legacy_v065_virtual_tp_adopts_old_attached_tp(self):
        self.c.update(state='VIRTUAL_TP',filledBuy='0.02')
        self.e.register_cancel_attached(self.st,[self.sell])
        self.assertEqual(self.c['state'],'TP_WAIT')
        self.assertEqual(self.c['tpOrders'][0]['clOrdId'],'CT1')
        self.e.verify_existing(self.st,[self.sell])
        with patch.object(self.e.A,'order',return_value=self.sell):
            self.assertTrue(self.e.track(self.st,[self.sell]))
        with patch.object(self.e.A,'req',side_effect=AssertionError('No duplicate REST SELL')):
            self.assertEqual(self.e.maintain_virtual_tp(self.st,[self.sell],D('.02')),'NONE')

    def test_cancel_lost_race_full_fill_clears_cleanup_intent(self):
        self.c.update(state='CANCELING',partial_cleanup=True)
        self.buy.update(state='filled',accFillSz='0.04')
        attached=dict(self.sell,sz='0.04')
        with patch.object(self.e.A,'order',side_effect=lambda oid='',cid='':self.buy if oid=='B1' else attached):
            self.assertTrue(self.e.track(self.st,[attached]))
        self.assertEqual(self.c['state'],'TP_WAIT')
        self.assertEqual(self.c['filledBuy'],'0.04')
        self.assertFalse(self.c.get('partial_cleanup'))
        self.assertEqual(self.e.partial_cleanup_action(self.st,D('107'),D('.04'),'RUN'),'NONE')

    def test_market_exit_rejected_for_dust_or_active_tp(self):
        self.c.update(state='VIRTUAL_TP',partial_cleanup=True)
        with patch.object(self.e.A,'req',side_effect=AssertionError('No market for dust')):
            self.assertFalse(self.e.market_exit_partial(self.st,self.c,D('.005')))
        self.assertEqual(self.c['state'],'VIRTUAL_TP')
        self.c['tpOrders']=[{'clOrdId':'CT1','ordId':'S1','sz':'.02',
                             'filled':'0','phase':'ACTIVE','source':'attached'}]
        with self.assertRaisesRegex(RuntimeError,'SELL TP is active'):
            self.e.market_exit_partial(self.st,self.c,D('.02'))

    def test_critical_third_party_sell_refused(self):
        third=dict(self.sell,clOrdId='UNEXPECTED',ordId='OTHER')
        with self.assertRaisesRegex(RuntimeError,'Unrecognized'):
            self.e.verify_existing(self.st,[third])

    def test_market_exit_profit_accounted_but_not_grid_profit(self):
        self.c.update(state='EXITED',partial_cleanup=True,filledTP='.02')
        self.c['tpOrders']=[{'clOrdId':'CEXIT','ordId':'S2','sz':'.02',
                'filled':'.02','phase':'FILLED','source':'partial_exit'}]
        f=[{'ordId':'B1','tradeId':'1','billId':'1','fee':'-.0005',
             'feeCcy':'USDT','execType':'M','fillPx':'106','fillSz':'.02','side':'buy'},
           {'ordId':'S2','tradeId':'2','billId':'2','fee':'-.0025',
             'feeCcy':'USDT','execType':'T','fillPx':'106.2','fillSz':'.02','side':'sell'}]
        d=self.e.full_stats_snapshot(self.st,f,D('106.2'),D('1'))
        self.assertTrue(d['complete'])
        self.assertEqual(d['closed_count'],0)
        self.assertEqual(d['grid_gain'],D('0'))
        self.assertEqual(d['partial_exit_count'],1)
        self.assertEqual(d['partial_exit_net'],D('.001'))
        self.assertEqual(d['total_gain'],D('.001'))
        ledger=self.e.PnLLedger(D('1'))
        result=ledger.compute(self.c,[f[0]],[f[1]])
        self.assertEqual(result['kind'],'EXITED')
        ledger.commit_completed(self.c,result)
        self.assertEqual(ledger.total(),D(0))


if __name__=='__main__':unittest.main()
