import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import sqlite3
from contextlib import closing
import okx_ui074 as m

class UITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'state').mkdir();(self.root/'diagnostics/execution_v074').mkdir(parents=True)
        (self.root/'state/bot04_state.json').write_text(json.dumps({'version':'0.7.4','cycles':{},'aggregate_exits':[]}))
        self.cfg=dict(instrument='ETH-USDT-SWAP',direction='long',grid_step='0.5',
          take_profit='2.75',order_size='0.03',contract_value='0.1',
          max_grids=500,upper_price='2800',capital_reference_price='2800',
          active_buy_orders=25,active_tp_orders=75,created_at=1791900000)
        (self.root/'config.json').write_text(json.dumps(self.cfg))
        self.sys=dict(ActiveState='active',ActiveEnterTimestampMonotonic='10000000',
          ActiveEnterTimestamp='2026-10-14 22:29:17 UTC',
          ExecStart='{ path=/usr/bin/python3 ; argv[]=... okx_bot074.py ; }')
    def tearDown(self):self.temp.cleanup()
    def test_settings_show_effective_tp_and_hide_noise(self):
        with patch.object(m,'systemd_info',return_value=self.sys):
            text=m.settings('eth064',self.root,'okx-grid-eth064.service')
        self.assertIn('Версия',text);self.assertIn('v0.7.4',text)
        self.assertIn('25 (макс. 30)',text);self.assertIn('25 (макс. 55)',text)
        self.assertNotIn('75 (макс.',text)
        for forbidden in ('Maker','Taker','Маржа','Версия формата','Частичный BUY, таймаут','LIVE разрешён'):
            self.assertNotIn(forbidden,text)
    def test_settings_variable_buffer(self):
        self.cfg.update(active_buy_orders=3,real_tp_target=19)
        (self.root/'config.json').write_text(json.dumps(self.cfg))
        with patch.object(m,'systemd_info',return_value=self.sys):
            text=m.settings('test',self.root,'fake')
        self.assertIn('3 (макс. 8)',text);self.assertIn('19 (макс. 27)',text)
    def test_status_orders_are_current_not_config(self):
        st={'version':'0.7.4','cycles':{}}
        for i in range(24):st['cycles'][f'B{i}']={'state':'BUY_LIVE','buyPx':str(2400+i)}
        for i in range(22):st['cycles'][f'S{i}']={'state':'TP_WAIT','filledBuy':'0.03',
                          'tpOrders':[{'phase':'ACTIVE','sz':'0.03','filled':'0'}]}
        st['cycles']['V']={'state':'VIRTUAL_TP'}
        st['aggregate_exits']=[{'phase':'SUBMITTED'}]
        (self.root/'state/bot04_state.json').write_text(json.dumps(st))
        with patch.object(m,'systemd_info',return_value=self.sys),patch.object(m,'uptimes',return_value=(123,345)),patch.object(m,'history_multifill',return_value=11),patch.object(m,'monitor_service_enabled',return_value=False):
            text=m.status('eth064',self.root,'fake')
        self.assertIn('24/25 (макс. 30)',text)
        self.assertIn('22/25 (макс. 55)',text)
        self.assertIn('Незавершённые MARKET',text)
        self.assertIn('1',text)
        self.assertIn('Аптайм VPS',text)
        self.assertIn('Ордеров с 2+ сделками',text)
        self.assertNotIn('Торговля                RUN',text)
        self.assertIn('нет данных',text)
    def test_partial_counter_precise(self):
        st={'cycles':{'a':{'state':'BUY_PARTIAL','tpOrders':[]},
           'b':{'state':'TP_PARTIAL','tpOrders':[{'phase':'ACTIVE','sz':'0.03','filled':'0.02'}]},
           'c':{'state':'VIRTUAL_TP','tpOrders':[]}},'aggregate_exits':[]}
        r=m.read_local_orders(st)
        self.assertEqual(r['partial_buy'],1)
        self.assertEqual(r['partial_tp'],1)
        self.assertEqual(r['real_tp'],1)
        self.assertEqual(r['unresolved_market'],0)
    def test_older_engine_version_shows_warning(self):
        info=dict(self.sys,ExecStart='... okx_bot073.py')
        with patch.object(m,'systemd_info',return_value=info):
            self.assertIn('⚠ Менеджер v0.7.4',m.settings('eth064',self.root,'fake'))
    def test_monitor_stale_is_not_zero(self):
        mon=self.root/'diagnostics/execution_v074/summary_v074.json'
        mon.write_text(json.dumps({'ts':'2020-01-01T00:00:00+00:00','state':{'missing_buy':0}}))
        self.assertIsNone(m.read_monitor(self.root))
        with patch.object(m,'systemd_info',return_value=self.sys),patch.object(m,'uptimes',return_value=(10,100)),patch.object(m,'monitor_service_enabled',return_value=False):
            result=m.status('eth064',self.root,'fake')
        self.assertIn('нет данных',result)
    def test_monitor_current_zero_valid(self):
        mon=self.root/'diagnostics/execution_v074/summary_v074.json'
        mon.write_text(json.dumps({'ts':datetime.now(timezone.utc).isoformat(),
             'bid':'2415.5','state':{'missing_buy':0,'near_missing_buy':0},
             'event_counts':{'completed_bursts':0,'alerts_CRITICAL':0,'alerts_WARNING':0}}))
        self.assertEqual(m.read_monitor(self.root)['state']['missing_buy'],0)
    def test_bid_from_local_log_when_monitor_disabled(self):
        journal='[12:00:00] 📊 РЕЖИМ=RUN | BID=2415.55 | BUY 25/25\n'
        from unittest.mock import Mock
        result=Mock(returncode=0,stdout=journal)
        with patch.object(m.subprocess,'run',return_value=result):
            self.assertEqual(str(m.last_local_bid('okx-grid-test.service')),'2415.55')
        with patch.object(m,'systemd_info',return_value=self.sys),patch.object(m,'uptimes',return_value=(10,100)),patch.object(m,'monitor_service_enabled',return_value=False),patch.object(m,'last_local_bid',return_value=m.dec('2415.55')):
            self.assertIn('$2,415.55',m.status('eth064',self.root,'fake'))

    def test_history_partial_trade_count_only_if_bootstrap(self):
        p=self.root/'state/fills_v068.sqlite'
        with closing(sqlite3.connect(p)) as db, db:
            db.execute('CREATE TABLE meta(name TEXT,value TEXT)')
            db.execute('CREATE TABLE fills(order_id TEXT,bill_id TEXT)')
            db.executemany('INSERT INTO fills VALUES(?,?)',[('A','1'),('A','2'),('B','3'),('B','3')])
            db.execute("INSERT INTO meta VALUES('complete_bootstrap','1')")
        self.assertEqual(m.history_multifill(self.root),1)
    def test_duration(self):
        self.assertEqual(m.duration(None),'нет данных')
        self.assertEqual(m.duration(65),'1 мин 5 сек')

if __name__=='__main__':unittest.main()
