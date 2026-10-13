"""Offline v0.6.5 regression and lifecycle tests. No OKX API writes."""
import importlib.util
import os
import json
import tempfile
import unittest
import shutil
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('wizard065',ROOT/'okx_setup065.py')
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
spec=importlib.util.spec_from_file_location('controller065',ROOT/'okxbot.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class SafeCases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name)
        self.base=base/'okx';self.base.mkdir()
        self.systemd=base/'systemd';self.systemd.mkdir()
        self.bot=self.base/'sol64';self.bot.mkdir()
        for path in ('state','logs','.venv/bin'): (self.bot/path).mkdir(parents=True)
        shutil.copy2(ROOT/'okx_bot065.py',self.bot/'okx_bot065.py')
        (self.bot/'.venv/bin/python').write_text('fake')
        cfg={'instrument':'SOL-USDT-SWAP','direction':'long','td_mode':'cross','buy_type':'post_only',
             'tp_type':'attached_limit','grid_step':'0.05','take_profit':'0.20','order_size':'0.01',
             'buy_base_qty':'0.01','contract_value':'1','contract_lot':'0.01','contract_min':'0.01',
             'tick_size':'0.01','upper_price':'120.00','capital_reference_price':'120.00',
             'maker_fee_at_setup':'0.0002','active_buy_orders':25,'active_tp_orders':75,
             'max_grids':500,'max_exchange_orders':500,'created_at':1720000000,'live_trading':True}
        (self.bot/'config.json').write_text(json.dumps(cfg))
        (self.bot/'.env').write_text('OKX_API_KEY=x\nOKX_API_SECRET=y\nOKX_API_PASSPHRASE=z\nOKX_DEMO=0\n')
        self.service=self.systemd/'okx-grid-sol64.service'
        self.service.write_text('WorkingDirectory='+str(self.bot)+'\nExecStart='+str(self.bot)+
                                '/.venv/bin/python -u '+str(self.bot)+'/okx_bot065.py\n')
        for var,value in [('BASE',self.base),('SYSTEMD_DIR',self.systemd),
                          ('REGISTRY',self.base/'registry.json'),('SOURCE',ROOT)]:
            p=patch.object(m,var,value);p.start();self.addCleanup(p.stop)
        entries={'sol64':{'slug':'sol64','path':str(self.bot),'service':'okx-grid-sol64'}}
        m.save_registry(entries)
        spec=importlib.util.spec_from_file_location('engine64_test',self.bot/'okx_bot065.py')
        self.e=importlib.util.module_from_spec(spec)
        with patch.dict(os.environ,{'OKX_BOT04_LIVE':'CONFIRM','OKX_BOT04_PROBE':'FULL_25'}):
            spec.loader.exec_module(self.e)
        self.state={'version':'0.6.5','instrument':'SOL-USDT-SWAP','cycles':{},
                    'archived_cycles':[],'completed':0}

    def test_engine_controls_are_persistent(self):
        self.assertEqual(self.e.control_mode(),'RUN')
        self.e.atomic_json(self.e.CONTROL_FILE,{'mode':'PAUSE'})
        self.assertEqual(self.e.control_mode(),'PAUSE')
        self.e.control_ack('PAUSE','PAUSED')
        self.assertEqual(json.loads(self.e.CONTROL_ACK.read_text())['phase'],'PAUSED')
        self.e.control_ack('PAUSE','PAUSED')
        self.assertEqual(json.loads(self.e.CONTROL_ACK.read_text())['phase'],'PAUSED')
        self.e.atomic_json(self.e.CONTROL_FILE,{'mode':'PANIC'})
        self.assertEqual(self.e.control_mode(),'PANIC')
        self.e.atomic_json(self.e.CONTROL_FILE,{'mode':'evil'})
        with self.assertRaises(RuntimeError):self.e.control_mode()

    def test_existing_bot_support_requires_service_execstart(self):
        entry=m.find_entry('sol64')[1]
        self.assertTrue(m.control_supported(entry))
        self.service.write_text('ExecStart=/root/okx/bot01/okx_bot059.py\n')
        self.assertFalse(m.control_supported(entry))

    def test_pause_does_not_write_any_exchange_orders(self):
        self.e.atomic_json(self.e.CONTROL_FILE,{'mode':'PAUSE'})
        class W:
            class EV:
                def empty(self):return True
            events=EV()
            class Wake:
                def clear(self):pass
                def wait(self,x):raise StopIteration
            wake=Wake()
        class Market:
            def get_bid(self):return Decimal('106')
        class Pnl:
            def status(self,st):return 'ok'
        class Worker:
            def submit(self,st):pass
        captured=[]
        def on_time_sleep(_):raise StopIteration
        with patch.object(self.e,'reconcile_position',return_value=([],True,Decimal('0'))), \
             patch.object(self.e,'maintain_virtual_tp',return_value='NONE'), \
             patch.object(self.e,'place',side_effect=AssertionError('BUY CREATED')), \
             patch.object(self.e,'cancel_lowest_specific',side_effect=AssertionError('BUY CANCELLED')), \
             patch.object(self.e.A,'maker_fee',return_value=Decimal('.0002')), \
             patch.object(self.e.time,'sleep',side_effect=on_time_sleep), \
             patch.object(self.e,'runtime_heartbeat'):
            with self.assertRaises(StopIteration):
                self.e.live_loop(self.state,W(),Market(),Pnl(),Worker())

    def test_soft_stop_cancels_only_buys_and_keeps_tp(self):
        self.state['cycles']={'106.00':{'state':'BUY_LIVE','ordId':'BUY','buyPx':'106.00'}}
        with patch.object(self.e,'cancel_lowest_specific',return_value=True) as cancel:
            self.assertEqual(self.e.cancel_buys_for_soft_stop(self.state,True),'CANCEL_BUYS')
            cancel.assert_called_once()
        self.state['cycles']['106.00']['state']='CANCELED'
        self.assertEqual(self.e.cancel_buys_for_soft_stop(self.state,True),'SOFT_STOPPED')
        self.assertEqual(self.e.cancel_buys_for_soft_stop(self.state,False),'WAIT_RECONCILIATION')

    def test_hard_stop_cancels_buys_then_tp_then_retires_without_selling(self):
        buy={'state':'BUY_LIVE','ordId':'B','buyPx':'106.00'}
        tp={'state':'TP_WAIT','buyPx':'105.00','tpPx':'105.20','cycleId':'T',
            'tpOrders':[{'phase':'ACTIVE','ordId':'S'}]}
        self.state['cycles']={'106.00':buy,'105.00':tp}
        fakepending=[{'side':'buy','ordId':'B'},{'side':'sell','ordId':'S'}]
        with patch.object(self.e,'cancel_lowest_specific') as cb, \
             patch.object(self.e,'cancel_tp') as ct, \
             patch.object(self.e,'place_panic_sell') as panic:
            self.assertFalse(self.e.controlled_shutdown(self.state,fakepending,Decimal('.01'),True,'HARD'))
            cb.assert_called_once();ct.assert_not_called();panic.assert_not_called()
            buy['state']='CANCELED'
            self.assertFalse(self.e.controlled_shutdown(self.state,[fakepending[-1]],Decimal('.01'),True,'HARD'))
            ct.assert_called_once();panic.assert_not_called()
            tp['tpOrders'][0]['phase']='CANCELED'
            self.assertTrue(self.e.controlled_shutdown(self.state,[],Decimal('.01'),True,'HARD'))
            panic.assert_not_called()
            self.assertEqual(self.state['terminal_mode'],'RETIRED')
            self.assertEqual(json.loads(self.e.CONTROL_ACK.read_text())['remaining_position'],'0.01')

    def test_panic_sends_single_reduce_only_market_order_after_cancel(self):
        actual=Decimal('0.13')
        calls=[]
        def order(method,path,body=None,**kw):
            self.assertEqual((method,path),('POST','/api/v5/trade/order'))
            self.assertEqual(body['side'],'sell');self.assertEqual(body['reduceOnly'],True)
            self.assertEqual(body['ordType'],'market');self.assertEqual(body['sz'],'0.13')
            self.assertEqual(json.loads(self.e.DATA.read_text())['panic_orders'][0]['clOrdId'],body['clOrdId'])
            calls.append(body)
            return [{'sCode':'0','ordId':'PANIC123'}]
        with patch.object(self.e.A,'req',side_effect=order):
            self.assertFalse(self.e.controlled_shutdown(self.state,[],actual,True,'PANIC'))
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.state['panic_orders']),1)
        self.assertEqual(self.state['panic_orders'][0]['ordId'],'PANIC123')
        with patch.object(self.e,'reconcile_panic_orders',return_value=False), \
             patch.object(self.e.A,'req',side_effect=AssertionError('DUPLICATE POST')):
            self.assertFalse(self.e.controlled_shutdown(self.state,[],actual,True,'PANIC'))

    def test_panic_finishes_only_at_zero_position(self):
        self.state['panic_orders']=[{'clOrdId':'P','ordId':'PANIC','filled':'0.13','sz':'0.13','state':'filled'}]
        with patch.object(self.e,'place_panic_sell',side_effect=AssertionError('NO NEW POST')):
            self.assertTrue(self.e.controlled_shutdown(self.state,[],Decimal('0'),True,'PANIC'))
        self.assertEqual(self.state['terminal_mode'],'PANIC_DONE')
        self.assertEqual(json.loads(self.e.CONTROL_ACK.read_text())['phase'],'PANIC_DONE')

    def test_panic_failed_api_never_resubmits_or_closes_untracked_position(self):
        with patch.object(self.e.A,'req',side_effect=TimeoutError('unknown')) as req:
            with self.assertRaises(TimeoutError):
                self.e.controlled_shutdown(self.state,[],Decimal('.11'),True,'PANIC')
        self.assertEqual(len(self.state['panic_orders']),1)
        with patch.object(self.e,'reconcile_panic_orders',return_value=False), \
             patch.object(self.e.A,'req',side_effect=AssertionError('DO NOT RETRY')):
            self.assertFalse(self.e.controlled_shutdown(self.state,[],Decimal('.11'),True,'PANIC'))

    def test_manager_confirmation_and_mode_changes(self):
        with patch.object(m,'service_running',return_value=True), \
             patch('builtins.input',return_value='not confirmed'):
            m.cmd_control('sol64','HARD')
        self.assertFalse((self.bot/'state/control.json').exists())
        with patch.object(m,'service_running',return_value=True), \
             patch('builtins.input',return_value='PAUSE sol64'):
            m.cmd_control('sol64','PAUSE')
        self.assertEqual(m.read_control(m.find_entry('sol64')[1])['mode'],'PAUSE')
        with patch.object(m,'service_running',return_value=True), \
             patch('builtins.input',return_value='RESUME sol64'):
            m.cmd_control('sol64','RUN')
        self.assertEqual(m.read_control(m.find_entry('sol64')[1])['mode'],'RUN')

    def test_hard_intent_cannot_be_revoked(self):
        with patch.object(m,'service_running',return_value=True), \
             patch('builtins.input',side_effect=['HARD sol64','FINAL sol64']):
            m.cmd_control('sol64','HARD')
        with patch.object(m,'service_running',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'завершение'):
                m.cmd_control('sol64','RUN')

    def test_manager_old_engine_refuses_destructive_control(self):
        (self.bot/'okx_bot065.py').unlink()
        (self.bot/'okx_bot060.py').write_text('old')
        with patch.object(m,'service_running',return_value=True), \
             patch.object(m,'run') as proc:
            with self.assertRaisesRegex(RuntimeError,'старом движке'):
                m.cmd_control('sol64','PANIC')
            proc.assert_not_called()

    def test_manager_never_starts_retired_instance(self):
        (self.bot/'state/control_ack.json').write_text(json.dumps({'phase':'RETIRED'}))
        with patch.object(m,'service_running',return_value=False), \
             patch.object(m,'run') as proc:
            with self.assertRaisesRegex(RuntimeError,'завершён'):
                m.cmd_start('sol64')
            proc.assert_not_called()

    def test_navigation_has_no_page_up_down_or_end(self):
        s=(ROOT/'okxbot.py').read_text()
        self.assertNotIn('PgUp',s);self.assertNotIn('PgDn',s)
        self.assertNotIn('curses.KEY_NPAGE',s)
        self.assertNotIn('curses.KEY_PPAGE',s)
        self.assertNotIn('curses.KEY_END',s)
        self.assertNotIn('End —',s)

if __name__=='__main__':unittest.main()
