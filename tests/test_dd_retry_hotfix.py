"""v0.6.8 regression for queued drawdown snapshot retry. Fully mocked; no exchange I/O."""
import json
import time
import unittest
from decimal import Decimal as D
from unittest.mock import patch
import test_grid as helper
from test_v068 import row

class DrawdownRetryTest(unittest.TestCase):
    def setUp(self):
        self.td, self.e = helper.EngineTests().make_engine('SOL','106','115','.03','.13','.04')
        self.addCleanup(self.td.cleanup)

    def test_five_field_snapshot_after_missing_fills(self):
        e=self.e
        cycle={'cycleId':'id1','ordId':'B1','state':'BUY_PARTIAL',
               'filledBuy':'.02','filledTP':'0','tpOrders':[],
               'buyPx':'106','tpPx':'106.13'}
        state={'cycles':{'106.00':cycle},'archived_cycles':[], 'instrument':e.INST}
        execution=row('B1','bill1','106','.02',fee='-.000424')
        pnl=e.PnLLedger(D('1'))
        class Bid:
            @staticmethod
            def cached_bid():return D('100')
        worker=e.BotDrawdownWorker(Bid(),pnl)
        snap=(time.time(),D('100'),state,D('0'))
        # The initial attempt is too early: exchange fill is not yet visible.
        # A queued retry carries attempt count as the FIFTH tuple element.
        with patch.object(e.FILL_STORE,'ensure_expected',side_effect=[[],[execution]]) as get:
            with self.assertRaises(e.ExchangeTemporary):
                worker._process(snap)
            worker._process((*snap,1))
            self.assertEqual(get.call_count,2)
        saved=json.loads(e.BOT_DD_DATA.read_text())
        self.assertEqual(saved['max_drawdown_usdt'],'0.120424')
        self.assertEqual(saved['last_source'],'reconciled_live_fills')

if __name__=='__main__':unittest.main()
