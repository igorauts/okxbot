#!/usr/bin/env python3
"""Public offline multi-asset compatibility: no exchange writes or credentials."""
import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
import importlib.util

ROOT=Path(__file__).resolve().parent
ENGINE=ROOT/'okx_bot073.py'
OBS=ROOT/'okx_execution_observer073.py'

def fake_config(coin):
    spec={
        'BTC':('10','50','0.01','0.01','0.01','0.01','95000','0.0001'),
        'ETH':('0.50','2.75','0.3','0.1','0.01','0.01','2800','0.03'),
        'SOL':('0.03','0.13','0.4','0.1','0.1','0.1','115','0.04')
    }
    step,tp,size,ct,lot,minsz,upper,base=spec[coin]
    return dict(instrument=coin+'-USDT-SWAP',direction='long',td_mode='cross',
        buy_type='post_only',tp_type='attached_limit',grid_step=step,take_profit=tp,
        order_size=size,active_buy_orders=25,max_grids=500,max_exchange_orders=500,
        active_tp_orders=25,contract_value=ct,tick_size=('0.1' if coin=='BTC' else '0.01'),
        contract_lot=lot,contract_min=minsz,maker_fee_at_setup='0.0002',
        upper_price=upper,capital_reference_price=upper,buy_base_qty=base,
        buy_buffer_orders=5,buy_cancel_protect_steps=3,tp_buffer_orders=30,
        real_tp_target=25,live_trading=False)

def prepare_module(path, cfg, state=None):
    root=Path(path);(root/'state').mkdir(exist_ok=True);(root/'logs').mkdir(exist_ok=True)
    (root/'config.json').write_text(json.dumps(cfg))
    (root/'.env').write_text('OKX_API_KEY=FAKE_FOR_TEST\nOKX_API_SECRET=FAKE_FOR_TEST\nOKX_API_PASSPHRASE=FAKE_FOR_TEST\n')
    shutil.copy2(ENGINE,root/'okx_bot073.py')
    if state is not None:(root/'state/bot04_state.json').write_text(json.dumps(state))
    old=os.environ.get('OKX_BOT04_AUDIT')
    os.environ['OKX_BOT04_AUDIT']='READ_ONLY'
    try:
        spec=importlib.util.spec_from_file_location('universal_'+cfg['instrument'].split('-')[0],root/'okx_bot073.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:os.environ.pop('OKX_BOT04_AUDIT',None)
        else:os.environ['OKX_BOT04_AUDIT']=old

class UniversalTests(unittest.TestCase):
    def test_all_three_configs_import_and_pure_state_migration(self):
        for coin,oldver in [('BTC','0.6.10'),('ETH','0.7.2'),('SOL','0.6.10')]:
            with self.subTest(coin=coin),tempfile.TemporaryDirectory() as temp:
                cfg=fake_config(coin)
                st={'version':oldver,'instrument':cfg['instrument'],'cycles':{},
                    'completed':13,'aggregate_exits':[]}
                mod=prepare_module(temp,cfg,st)
                self.assertEqual(mod.VER,'0.7.3');self.assertEqual(mod.INST,cfg['instrument'])
                self.assertEqual(mod.TARGET,25);self.assertEqual(mod.BUY_MAX,30)
                self.assertEqual(mod.REAL_TP_TARGET,25);self.assertEqual(mod.REAL_TP_MAX,55)
                self.assertEqual(mod.BUY_CANCEL_PROTECT_STEPS,3)
                self.assertEqual(mod.ensure_state()['completed'],13)
                self.assertEqual(mod.ensure_state()['version'],'0.7.3')
                self.assertEqual(mod.ensure_state()['cycles'],{})
    def test_buy_buffer_independent_of_near_bid_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=fake_config('ETH');cfg.update(buy_buffer_orders=2,buy_cancel_protect_steps=6)
            mod=prepare_module(temp,cfg)
            self.assertEqual(mod.BUY_MAX,27);self.assertEqual(mod.BUY_CANCEL_PROTECT_STEPS,6)
            protected={'buyPx':'2429.00','state':'BUY_LIVE','ordId':'near'}
            far={'buyPx':'2411.00','state':'BUY_LIVE','ordId':'far'}
            selected,kept=mod.plan_buy_buffer_cleanup([protected,far],set(),Decimal('2430'),1)
            self.assertEqual([x['ordId'] for x in selected],['far'])
            self.assertEqual([x['ordId'] for x in kept],['near'])
    def test_config_upper_bound_refuses(self):
        for field,val in [('buy_buffer_orders',6),('buy_cancel_protect_steps',11),
                          ('tp_buffer_orders',31),('real_tp_target',26)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                cfg=fake_config('BTC');cfg[field]=val
                with self.assertRaises(RuntimeError):prepare_module(temp,cfg)
    def test_unsupported_instrument_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=fake_config('ETH');cfg['instrument']='DOGE-USDT-SWAP'
            with self.assertRaises(RuntimeError):prepare_module(temp,cfg)
    def test_monitor_all_three_assets_and_configurable_grid(self):
        from okx_execution_observer073 import Monitor, summarize_state
        for coin in ('BTC','ETH','SOL'):
            with self.subTest(coin=coin),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);(root/'config.json').write_text(json.dumps(fake_config(coin)))
                m=Monitor(root,root/'diagnostics/test',mode='both')
                self.assertEqual(m.target_buy,25)
                self.assertEqual(m.target_tp,25)
                state={'version':'0.7.3','instrument':coin+'-USDT-SWAP','cycles':{}}
                self.assertEqual(summarize_state(state)['buy'],0)
    def test_mode_independence(self):
        from okx_execution_observer073 import Monitor
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.json').write_text(json.dumps(fake_config('SOL')))
            m=Monitor(root,root/'diagnostics/only_burst',mode='burst')
            m.alert('SYNTHETIC','CRITICAL',{'price':'110'})
            self.assertEqual(m.counters['alerts_CRITICAL'],0)
            self.assertFalse((m.output/'alerts.jsonl').exists())
    def test_monitor_unit_binding(self):
        from okx_monitor073 import monitor_unit,unit_text
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.json').write_text(json.dumps(fake_config('SOL')))
            service=monitor_unit('okx-grid-sol065','execution')
            self.assertEqual(service,'okx-grid-sol065-execution-monitor073.service')
            unit=unit_text(root,'okx-grid-sol065','execution',OBS)
            self.assertIn('--mode execution',unit)
            self.assertIn('--service okx-grid-sol065',unit)
            self.assertNotIn('OKX_API_SECRET',unit)
            with self.assertRaises(ValueError):monitor_unit('okx-grid-sol065;reboot','burst')
    def test_migration_backups_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=fake_config('SOL')
            old={'version':'0.6.10','instrument':cfg['instrument'],'cycles':{},'completed':7}
            mod=prepare_module(temp,cfg,old)
            mod.ensure_state()
            backup=Path(temp)/'state/bot04_state_before_v070.json'
            self.assertTrue(backup.is_file())
            self.assertEqual(json.loads(backup.read_text()),old)
    def test_no_exchange_connections_from_observer(self):
        text=OBS.read_text()
        self.assertNotIn('import websocket',text)
        self.assertNotIn('OKX_API_KEY',text)
        self.assertNotIn('OKX_API_SECRET',text)
        self.assertNotIn('api/v5',text)
    def test_documented_versions(self):
        self.assertIn("ALLOWED = {'BTC','ETH','SOL'}",ENGINE.read_text())
        self.assertNotIn("if INST != 'ETH-USDT-SWAP'",ENGINE.read_text())
        self.assertNotIn("if INST != 'SOL-USDT-SWAP'",ENGINE.read_text())

if __name__=='__main__':unittest.main()
