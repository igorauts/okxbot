"""v0.6.8 offline regressions: exchange calls mocked, no network, no trades."""
import json
import sqlite3
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from test_grid import EngineTests


def row(order, bill, px, qty, side='buy',fee='-.01'):
    return {'ordId':order,'billId':bill,'tradeId':bill,'instId':'SOL-USDT-SWAP',
            'fee':fee,'feeCcy':'USDT','side':side,'execType':'M',
            'fillPx':str(px),'fillSz':str(qty),'ts':'1791936000000'}


class FillStoreTests(unittest.TestCase):
    def setUp(self):
        self.td,self.e=EngineTests().make_engine('SOL','106','115','.03','.13','.04')
        self.addCleanup(self.td.cleanup)
        self.e.CFG['version']='0.6.8'
        self.st={'instrument':'SOL-USDT-SWAP','cycles':{'106.00':{
            'cycleId':'ONE','state':'BUY_PARTIAL','ordId':'B1','filledBuy':'.02',
            'filledTP':'0','tpOrders':[],'buyPx':'106.00','tpPx':'106.13'}},
            'archived_cycles':[],'completed':0}

    def test_initial_full_history_import_only_once_and_no_duplicate(self):
        e=self.e
        buy=row('B1','bill1','106','.02',fee='-.000424')
        with patch.object(e,'_legacy_exchange_fills',return_value=[buy]) as full, \
             patch.object(e.A,'fills_by_order',side_effect=AssertionError('unwanted GET')):
            self.assertEqual(len(e.stats_exchange_fills(self.st)),1)
            self.assertEqual(len(e.stats_exchange_fills(self.st)),1)
        full.assert_called_once()
        self.assertTrue(e.FILL_STORE.initialized())
        self.assertEqual(e.FILL_STORE.insert([buy,buy]),0)
        self.assertEqual(len(e.FILL_STORE.get_rows(['B1'])),1)

    def test_never_migrate_missing_historical_fill(self):
        e=self.e
        with patch.object(e,'_legacy_exchange_fills',return_value=[]):
            with self.assertRaises(e.ExchangeUnresolved):
                e.FILL_STORE.bootstrap(self.st)
        self.assertFalse(e.FILL_STORE.initialized())

    def test_fetch_only_missing_order_id_after_bootstrap(self):
        e=self.e
        self.st['cycles']={}
        with patch.object(e,'_legacy_exchange_fills',return_value=[]):
            self.assertEqual(e.stats_exchange_fills(self.st),[])
        self.st['cycles']={'106.00':{'cycleId':'A','state':'BUY_PARTIAL',
            'ordId':'B2','filledBuy':'.01','filledTP':'0','tpOrders':[]}}
        response=row('B2','bill2','106','.01',fee='-.000212')
        with patch.object(e.A,'fills_by_order',return_value=[response]) as get:
            self.assertEqual(len(e.stats_exchange_fills(self.st)),1)
            self.assertEqual(len(e.stats_exchange_fills(self.st)),1)
            get.assert_called_once_with('B2')

    def test_same_bill_different_financial_content_rejected(self):
        e=self.e
        x=row('B1','bill1','106','.02')
        e.FILL_STORE.insert([x])
        with self.assertRaisesRegex(ValueError,'Conflicting exchange fill'):
            e.FILL_STORE.insert([dict(x,fillSz='.03')])

    def test_archive_closed_cycle_without_losing_history(self):
        e=self.e
        cycle={'cycleId':'C-1','ordId':'B1','state':'CANCELED',
               'filledBuy':'0','filledTP':'0'}
        self.st['cycles']={}
        self.st['archived_cycles']=[cycle]
        e.save(self.st)
        pn=e.PnLLedger(D('1'))
        self.assertEqual(e.compact_archived(self.st,pn),1)
        self.assertEqual(self.st['archived_cycles'],[])
        self.assertEqual(e.all_stat_cycles(self.st),[cycle])
        self.assertEqual(e.compact_archived(self.st,pn),0)

    def test_24h_profit_includes_closed_cycles_compacted_to_sqlite(self):
        """A CLOSED cycle moved into cycles_v068.sqlite must still count for 24h."""
        e=self.e
        buy=row('B99','buy-bill','106','.04',fee='-.000848')
        sell=row('S99','sell-bill','106.13','.04',side='sell',fee='-.00084904')
        c={'cycleId':'C-99','ordId':'B99','state':'CLOSED','filledBuy':'.04',
           'filledTP':'.04','buyPx':'106','tpPx':'106.13',
           'tpOrders':[{'ordId':'S99','filled':'.04'}]}
        self.st['cycles']={}
        self.st['archived_cycles']=[c]
        pn=e.PnLLedger(D('1'))
        pn.commit_completed(c,{'kind':'CLOSED','net':'.00350296',
                 'buy_px':'106','sell_px':'106.13','qty_base':'.04',
                 'qty_contracts':'.04','gross':'.0052','planned_gross':'.0052',
                 'execution':'0','fees':'-.00169704',
                 'buy_liquidity':'M','sell_liquidity':'M'})
        self.assertEqual(e.compact_archived(self.st,pn),1)
        self.assertEqual(self.st['archived_cycles'],[])
        self.assertEqual(len(e.all_stat_cycles(self.st)),1)
        result=e._last_24h_profit(self.st,[buy,sell],D(1),1791936000000+60000)
        self.assertEqual(result,D('.00350296'))

    def test_sqlite_pnl_migrates_old_json_preserves_totals_and_writes_only_new(self):
        e=self.e
        old={'schema':1,'instrument':e.INST,'accounted':{'B0:S0':{
            'kind':'CLOSED','net':'0.12345678'}}}
        e.PNL_DATA.write_text(json.dumps(old))
        pn=e.PnLLedger(D('1'))
        self.assertEqual(pn.total(),D('.12345678'))
        self.assertEqual(pn.total_all(),D('.12345678'))
        c={'ordId':'B1','tpOrdId':'S1','tpOrders':[]}
        r={'kind':'EXITED','net':'-.01234567','buy_px':'106',
           'sell_px':'105.9'}
        self.assertTrue(pn.commit_completed(c,r))
        self.assertFalse(pn.commit_completed(c,r))
        recovered=e.PnLLedger(D('1'))
        self.assertEqual(recovered.total(),D('.12345678'))
        self.assertEqual(recovered.total_all(),D('.11111111'))
        self.assertTrue(recovered.recorded(c))
        self.assertEqual(json.loads(e.PNL_DATA.read_text()),old)

    def test_dd_after_new_partial_fill_no_wait_for_ten_min_stats(self):
        e=self.e
        self.st['cycles']['106.00'].update(
            buyPx='106.00',tpPx='106.13',sz='.04')
        e.save(self.st)
        x=row('B1','X1','106','.02',fee='-.000424')
        with patch.object(e,'_legacy_exchange_fills',return_value=[x]):
            e.FILL_STORE.bootstrap(self.st)
        pnl=e.PnLLedger(D('1'))
        snapshot=e.full_stats_snapshot({'cycles':self.st['cycles'],'archived_cycles':[]},
            e.FILL_STORE.ensure_expected({'B1':D('.02')}),D('100'),D('1'),
            include_durable_archive=False)
        self.assertTrue(snapshot['complete'])
        self.assertEqual(snapshot['total_gain'],D('-.120424'))
        e.record_bot_drawdown(pnl.total_all()+snapshot['total_gain'],source='test_fill')
        self.assertEqual(json.loads(e.BOT_DD_DATA.read_text())['max_drawdown_usdt'],'0.120424')


class RestSafetyTests(unittest.TestCase):
    def setUp(self):
        self.td,self.e=EngineTests().make_engine('SOL','106','115','.03','.13','.04')
        self.addCleanup(self.td.cleanup)

    def test_get_retries_and_succeeds_no_new_order(self):
        e=self.e
        with patch.object(e.A,'_http',side_effect=[TimeoutError('offline'),(200,{'code':'0','data':[{'ok':1}]})]) as h, \
             patch.object(e.REST_GATE,'throttle'):
            self.assertEqual(e.A.req('GET','/api/v5/test'),[{'ok':1}])
            self.assertEqual(h.call_count,2)

    def test_post_timeout_never_retried(self):
        e=self.e
        with patch.object(e,'LIVE',True),patch.object(e.A,'_http',side_effect=TimeoutError('offline')) as h:
            with self.assertRaisesRegex(e.ExchangeUnresolved,'UNKNOWN POST OUTCOME'):
                e.A.req('POST','/api/v5/trade/order',body={'a':1})
            self.assertEqual(h.call_count,1)

    def test_5xx_get_retries_but_persistent_fault_raises_temporary(self):
        e=self.e
        with patch.object(e.A,'_http',return_value=(503,{'code':'500','msg':'unavailable'})) as h, \
             patch.object(e.REST_GATE,'throttle'):
            with self.assertRaises(e.ExchangeTemporary):
                e.A.req('GET','/api/v5/test')
            self.assertEqual(h.call_count,4)

    def test_99_orders_total_with_legacy_75_tp_config(self):
        e=self.e
        self.assertEqual(e.TARGET,25)
        self.assertEqual(e.REAL_TP_CONFIG,75)
        self.assertEqual(e.REAL_TP_TARGET,74)
        self.assertLessEqual(e.REAL_TP_TARGET+e.TARGET,99)


if __name__=='__main__':unittest.main()

class FrozenDrawdownSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.td,self.e=EngineTests().make_engine('SOL','106','115','.03','.13','.04')
        self.addCleanup(self.td.cleanup)

    def test_snapshot_kept_after_newer_fill_on_same_order(self):
        e=self.e
        c={'cycleId':'id1','ordId':'B1','state':'BUY_PARTIAL',
           'filledBuy':'.02','filledTP':'0','tpOrders':[],
           'buyPx':'106','tpPx':'106.13'}
        state={'cycles':{'106.00':c},'archived_cycles':[], 'instrument':e.INST}
        e.FILL_STORE.insert([row('B1','bill1','106','.02',fee='-.000424')])
        with patch.object(e,'_legacy_exchange_fills',return_value=[row('B1','bill1','106','.02',fee='-.000424')]):
            e.FILL_STORE.bootstrap(state)
        pnl=e.PnLLedger(D('1'))
        class Bid:
            @staticmethod
            def cached_bid():return D('100')
        watcher=e.BotDrawdownWorker(Bid(),pnl)
        watcher.submit_reconciled(state)
        sample=watcher.pending.get_nowait()
        # A second, newer BUY fill arrives BEFORE the background PnL worker.
        c['filledBuy']='.04'
        e.FILL_STORE.insert([row('B1','bill2','105','.02',fee='-.00042')])
        watcher._process(sample)
        saved=json.loads(e.BOT_DD_DATA.read_text())
        self.assertEqual(saved['max_drawdown_usdt'],'0.120424')
        self.assertEqual(saved['last_source'],'reconciled_live_fills')
