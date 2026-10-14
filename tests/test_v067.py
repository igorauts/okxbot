"""Bot-only negative total PnL drawdown; no account equity or trading actions."""
import json
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from test_grid import EngineTests


class BotDrawdownV067(unittest.TestCase):
    def setUp(self):
        self.td, self.e = EngineTests().make_engine('ETH','2500','2800','.5','2.75','.03')
        self.addCleanup(self.td.cleanup)

    def test_records_worst_negative_total_pnl_and_persists(self):
        e=self.e
        zero=e.record_bot_drawdown(D('0.10'),100)
        self.assertEqual(zero['max_drawdown_usdt'],'0')
        snap=e.record_bot_drawdown(D('-0.70431315'),200)
        self.assertEqual(snap['current_drawdown_usdt'],'0.70431315')
        self.assertEqual(snap['max_drawdown_usdt'],'0.70431315')
        self.assertEqual(snap['worst_total_pnl_usdt'],'-0.70431315')
        self.assertEqual(snap['max_drawdown_at'],200)
        recovered=e.record_bot_drawdown(D('0.25'),300)
        self.assertEqual(recovered['current_drawdown_usdt'],'0')
        self.assertEqual(recovered['max_drawdown_usdt'],'0.70431315')
        new_worst=e.record_bot_drawdown(D('-0.80'),400)
        self.assertEqual(new_worst['max_drawdown_usdt'],'0.80')
        self.assertEqual(new_worst['max_drawdown_at'],400)
        self.assertEqual(new_worst['samples'],4)
        self.assertEqual(json.loads(e.BOT_DD_DATA.read_text())['max_drawdown_usdt'],'0.80')

    def test_not_peak_to_trough_and_not_account_equity(self):
        e=self.e
        for ix,x in enumerate((D('10'),D('4'),D('7'))):
            e.record_bot_drawdown(x,ix+100)
        self.assertEqual(json.loads(e.BOT_DD_DATA.read_text())['max_drawdown_usdt'],'0')
        self.assertFalse(hasattr(e.A,'equity_usd'))

    def test_two_bot_files_are_independent(self):
        other,sol=EngineTests().make_engine('SOL','106','115','.03','.13','.04')
        self.addCleanup(other.cleanup)
        self.e.record_bot_drawdown(D('-3'),100)
        sol.record_bot_drawdown(D('-1'),101)
        self.assertEqual(json.loads(self.e.BOT_DD_DATA.read_text())['max_drawdown_usdt'],'3')
        self.assertEqual(json.loads(sol.BOT_DD_DATA.read_text())['max_drawdown_usdt'],'1')
        self.assertNotEqual(self.e.BOT_DD_DATA,sol.BOT_DD_DATA)

    def test_live_bid_uses_authoritative_base_until_new_fill(self):
        e=self.e
        st={'instrument':e.INST,'cycles':{'2500':{
            'cycleId':'A','ordId':'B','state':'TP_WAIT','filledBuy':'.03',
            'filledTP':'0','tpOrders':[]}},'archived_cycles':[]}
        anchor={'schema':1,'instrument':e.INST,'signature':e.financial_signature(st),
                'total_pnl_usdt':'-0.25','open_contracts':'.03','bid':'2500'}
        # ETH ctVal=.1; .03 contracts=>.003 ETH, move $10 increases PnL by $.03.
        self.assertEqual(e.bot_dd_mark(anchor,st,D('2510')),D('-0.22'))
        self.assertEqual(e.bot_dd_mark(anchor,st,D('2490')),D('-0.28'))
        st['cycles']['2500']['filledTP']='.01'
        self.assertIsNone(e.bot_dd_mark(anchor,st,D('2490')))
        self.assertIsNone(e.bot_dd_mark(anchor,st,D('0')))

    def test_report_shows_individual_worst_not_global_account(self):
        e=self.e
        e.record_bot_drawdown(D('-2.35'),100)
        st={'instrument':e.INST,'cycles':{},'archived_cycles':[],
            'started_at':1720000000,'completed':0}
        stats={'bid':D('2490'),'closed_count':0,'active_buys':0,
            'grid_gain':D('0'),'partial_exit_count':0,'partial_exit_net':D('0'),
            'total_fees':D('0'),'volume':D('0'),'maker_volume':D('0'),
            'taker_volume':D('0'),'open_base':D('0'),
            'open_gain':D('0'),'total_gain':D('0')}
        with patch.object(e,'_first_anchor',return_value=(1720000000,D('2800'))), \
             patch.object(e,'_last_24h_profit',return_value=D('0')):
            report=e.format_weex_report(st,[],stats,now=1720000100)
        self.assertIn('МАКС. ПРОСАДКА БОТА:',report)
        self.assertIn('−$2.35000000',report)
        self.assertNotIn('EQUITY ВСЕГО СЧЁТА',report)

    def test_broken_anchor_rejected_and_no_state_writes_from_audit(self):
        e=self.e
        empty={'instrument':e.INST,'cycles':{},'archived_cycles':[]}
        anchor={'schema':1,'instrument':e.INST,'signature':'wrong',
                'total_pnl_usdt':'0','open_contracts':'0','bid':'2500'}
        self.assertIsNone(e.bot_dd_mark(anchor,empty,D('2490')))
        self.assertFalse(e.BOT_DD_DATA.exists())
        self.assertFalse(e.BOT_DD_ANCHOR.exists())

if __name__ == '__main__':unittest.main()

# Manager must show the newest drawdown even when cached full report is older.
class ManagerDrawdownV067(unittest.TestCase):
    def test_read_only_overlay_uses_latest_dd_file(self):
        import tempfile
        from pathlib import Path
        import importlib.util
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('manager_dd67',root/'okxbot.py')
        m=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'state';folder.mkdir()
            dd={'schema':1,'instrument':'ETH-USDT-SWAP',
                'max_drawdown_usdt':'2.35','max_drawdown_at':100,
                'last_at':101}
            (folder/'bot_drawdown_v067.json').write_text(json.dumps(dd))
            original='Расчётный капитал для 1x: $4,000.00\nМАКС. ПРОСАДКА БОТА:       −$0.50000000\n  Зафиксирована:          old\n✅ Все закрытые циклы сопоставлены.'
            result=m.with_latest_bot_dd(original,Path(td),'ETH-USDT-SWAP')
            self.assertIn('−$2.35000000',result)
            self.assertIn('−0.0588% от 1x',result)
            self.assertNotIn('−$0.50000000',result)
            self.assertIn('Последний замер DD:',result)
            self.assertEqual(m.with_latest_bot_dd(original,Path(td),'SOL-USDT-SWAP'),original)
