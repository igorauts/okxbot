"""v0.6.5 BUY partial fill safety: entirely mocked OKX, no network/writes."""
import unittest
from decimal import Decimal
from unittest.mock import patch
from test_grid import EngineTests


class PartialBuyTests(unittest.TestCase):
    def setUp(self):
        helper=EngineTests()
        self.td,self.e=helper.make_engine('SOL','106','120','.05','.2','.03')
        self.addCleanup(self.td.cleanup)
        self.cycle={'state':'BUY_LIVE','buyPx':'106.00','tpPx':'106.20',
                    'sz':'0.03','filledBuy':'0','filledTP':'0',
                    'ordId':'B1','clOrdId':'CB1','tpClOrdId':'CT1',
                    'cycleId':'sol-CB1','tpOrders':[]}
        self.st={'version':'0.6.5','instrument':self.e.INST,
                 'cycles':{'106.00':self.cycle},'archived_cycles':[],
                 'completed':0}
        self.buy={'instId':self.e.INST,'side':'buy','ordId':'B1',
                  'clOrdId':'CB1','state':'partially_filled','accFillSz':'0.01',
                  'sz':'0.03','px':'106.00'}
        self.sell={'instId':self.e.INST,'side':'sell','ordId':'S1',
                   'clOrdId':'CT1','state':'live','accFillSz':'0',
                   'sz':'0.03','px':'106.20','reduceOnly':'true'}

    def fake_order(self,oid='',cid=''):
        if oid=='B1' or cid=='CB1':return self.buy
        if oid=='S1' or cid=='CT1':return self.sell
        raise AssertionError('Unknown order '+str((oid,cid)))

    def partial(self,size='0.01'):
        self.buy['state']='partially_filled'
        self.buy['accFillSz']=size
        with patch.object(self.e.A,'order',side_effect=self.fake_order):
            return self.e.track(self.st,[self.buy])

    def test_partial_buy_is_reserved_not_mistaken_for_tp(self):
        self.assertTrue(self.partial('0.01'))
        self.assertEqual(self.cycle['state'],'BUY_PARTIAL')
        self.assertEqual(self.e.unprotected_partial_buy(self.st),Decimal('0.01'))
        self.assertEqual(self.e.coverage_audit(self.st,[self.buy],Decimal('.01')),
                         (0,Decimal(0),Decimal(0)))
        with self.assertRaisesRegex(RuntimeError,'Coverage mismatch'):
            self.e.coverage_audit(self.st,[self.buy],Decimal('0.02'))

    def test_partial_then_complete_and_partial_tp_sell(self):
        self.partial('.01')
        self.buy['state']='filled';self.buy['accFillSz']='0.03'
        with patch.object(self.e.A,'order',side_effect=self.fake_order):
            self.assertTrue(self.e.track(self.st,[self.buy,self.sell]))
        self.assertEqual(self.cycle['state'],'TP_WAIT')
        self.assertEqual(self.e.unprotected_partial_buy(self.st),Decimal('0'))
        self.assertEqual(self.e.coverage_audit(self.st,[self.sell],Decimal('.03'))[1],Decimal('.03'))
        self.sell['state']='partially_filled';self.sell['accFillSz']='0.01'
        with patch.object(self.e.A,'order',side_effect=self.fake_order):
            self.assertTrue(self.e.track(self.st,[self.sell]))
        self.assertEqual(self.cycle['state'],'TP_PARTIAL')
        self.assertEqual(self.cycle['filledTP'],'0.01')
        self.e.coverage_audit(self.st,[self.sell],Decimal('.02'))
        self.sell['state']='filled';self.sell['accFillSz']='0.03'
        with patch.object(self.e.A,'order',side_effect=self.fake_order):
            self.assertTrue(self.e.track(self.st,[]))
        self.assertEqual(self.cycle['state'],'CLOSED')
        self.e.coverage_audit(self.st,[],Decimal('0'))

    def test_partial_cancel_adopts_exchange_attached_tp_instead_of_second_sell(self):
        self.partial('.01')
        with patch.object(self.e.A,'req',return_value=[{'sCode':'0'}]):
            self.assertTrue(self.e.cancel_lowest_specific(self.st,self.cycle))
        self.buy.update(state='canceled',accFillSz='0.01')
        attached=dict(self.sell,sz='0.01')
        # Exchange opens the TP during the BUY cancel acknowledgement race.
        self.e.register_cancel_attached(self.st,[attached])
        with patch.object(self.e.A,'order',side_effect=lambda oid='',cid='':self.buy if oid=='B1' else attached):
            self.assertTrue(self.e.track(self.st,[attached]))
        self.assertEqual(self.cycle['state'],'TP_WAIT')
        self.assertEqual(self.cycle['filledBuy'],'0.01')
        self.assertEqual(self.cycle['tpOrders'][0]['ordId'],'S1')
        self.e.coverage_audit(self.st,[attached],Decimal('.01'))
        with patch.object(self.e.A,'req',side_effect=AssertionError('Never duplicate auto-attached TP')):
            self.assertEqual(self.e.maintain_virtual_tp(self.st,[attached],Decimal('.01')),'NONE')

    def test_sub_lot_partial_cancel_waits_for_attached_resolution(self):
        self.partial('.005')
        self.buy.update(state='canceled',accFillSz='.005')
        with patch.object(self.e.A,'order',side_effect=lambda oid='',cid='':self.buy if oid=='B1' else None), \
             patch.object(self.e,'paged_order',return_value=None), \
             patch.object(self.e,'parent_tp_failure',return_value=''):
            self.assertFalse(self.e.track(self.st,[]))
        self.assertEqual(self.cycle['state'],'TP_WAIT')
        self.assertEqual(self.cycle['tpOrders'][0]['sz'],'0.005')
        self.assertFalse(self.e.tp_quantity_placeable(Decimal('.005')))
        # Cannot send another SELL until attached outcome is known.
        with patch.object(self.e.A,'req',side_effect=AssertionError('Do not submit another SELL')):
            self.assertEqual(self.e.partial_cleanup_action(self.st,Decimal('110'),Decimal('.005'),'RUN'),'NONE')

    def test_live_reported_with_accfill_is_partial_not_ignored(self):
        self.buy['state']='live';self.buy['accFillSz']='0.01'
        with patch.object(self.e.A,'order',side_effect=self.fake_order):
            self.assertTrue(self.e.track(self.st,[self.buy]))
        self.assertEqual(self.cycle['state'],'BUY_PARTIAL')
        self.e.coverage_audit(self.st,[self.buy],Decimal('.01'))

    def test_soft_hard_can_request_cancel_of_partial_order(self):
        self.partial('.01')
        with patch.object(self.e,'cancel_lowest_specific',return_value=True) as canc:
            self.assertEqual(self.e.cancel_buys_for_soft_stop(self.st,True),'CANCEL_BUYS')
            self.assertEqual(canc.call_count,1)

if __name__=='__main__':unittest.main()
