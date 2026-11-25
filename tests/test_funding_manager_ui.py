import ast
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import okx_stats_ui as ui
from okx_funding_ledger import SCHEMA, SCHEMA_SQL, creation_ms

class FundingManagerUI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.db=self.root/'funding.sqlite'
        self.inst='BTC-USDT-SWAP'; self.created=1794589444.6692786
        self.now=1795541066.0
        (self.root/'config.json').write_text(json.dumps({'instrument':self.inst,'created_at':self.created,'tick_size':'0.1'}))
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript(SCHEMA_SQL); c.execute(f'PRAGMA user_version={SCHEMA}'); c.commit()
        self.payload=dict(schema=1,bid='100',open_base='1',total_pnl='20',grid_profit='10',floating_pnl='5',limit_tp_net='7',market_tp_net='3',partial_exit_net='2',realized_open_components='3',open_nominal='100',capital='100',closed_count=1,max_base=None,tp_waiting_all=1)
        self.addCleanup(patch.stopall)
        patch.object(ui,'peak_line',return_value='PEAK').start()
        patch.object(ui,'drawdown_lines',return_value=['DD']).start()
    def put(self,amount,created=None,complete=1,synced=None):
        if created is None: created=self.created
        if synced is None: synced=int(self.now*1000)-15150000
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?)',(self.inst,creation_ms(created),amount,None,synced,complete)); c.commit()
    def render(self,enabled=True,**kw):
        return ui.format_stats(self.root,self.inst,self.payload,include_funding=enabled,funding_db=self.db,now=self.now,**kw)
    def test_default_unchanged_no_read(self):
        with patch('okx_funding_view.load_funding',side_effect=AssertionError('must not read')):
            text=self.render(False)
        self.assertNotIn('FUNDING:',text); self.assertIn('ИТОГОВЫЙ PnL: +$20.00000000 (+20.0000% от 1x)',text)
    def test_exact_negative_and_only_two_lines_change(self):
        self.put('-4.829310123456789')
        original=self.render(False).splitlines(); updated=self.render().splitlines()
        self.assertIn('FUNDING: −4.829310, 4ч 12м 30с назад',updated)
        self.assertIn('ИТОГОВЫЙ PnL: +$15.17068988 (+15.1707% от 1x)',updated)
        filtered=lambda xs:[x for x in xs if not x.startswith(('FUNDING:','ИТОГОВЫЙ PnL:'))]
        self.assertEqual(filtered(original),filtered(updated))
    def test_positive_stale_incomplete_and_stopped(self):
        self.put('19.164214',complete=0,synced=int(self.now*1000)-192*3600000)
        text=self.render(bot_active=False)
        self.assertIn('FUNDING: +19.164214, 192ч 00м 00с назад · ⚠ Funding:',text)
        self.assertIn('ИТОГОВЫЙ PnL: +$39.16421400',text)
    def test_new_instance_does_not_inherit(self):
        self.put('99',created=self.created-100)
        text=self.render(); self.assertIn('FUNDING: +0.000000, ещё не обновлялся',text)
        self.assertIn('ИТОГОВЫЙ PnL: +$20.00000000',text)
    def test_read_error_keeps_existing_total(self):
        with patch('okx_funding_view.load_funding',side_effect=sqlite3.OperationalError('busy')):
            text=self.render()
        self.assertIn('FUNDING: +0.000000, ещё не обновлялся · ⚠ Funding:',text); self.assertIn('ИТОГОВЫЙ PnL: +$20.00000000',text)
    def test_existing_reconciliation_not_bypassed(self):
        self.put('5'); self.payload['total_pnl']='21'
        with self.assertRaises(ValueError): self.render()
    def test_manager_opt_in_after_archive_returns(self):
        tree=ast.parse(Path(ui.__file__).with_name('okxbot.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='stats_text')
        calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='format_stats']
        self.assertEqual(len(calls),1)
        self.assertTrue(any(k.arg=='include_funding' and isinstance(k.value,ast.Constant) and k.value.value is True for k in calls[0].keywords))
        guard=next(n for n in fn.body if isinstance(n,ast.If) and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Name) and n.test.func.id=='instance_terminal')
        self.assertLess(guard.lineno,calls[0].lineno); self.assertTrue(any(isinstance(n,ast.Return) for n in ast.walk(guard)))
