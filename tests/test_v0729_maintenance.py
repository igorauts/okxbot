#!/usr/bin/env python3
"""Offline-only tests for v0.7.29 maintenance overlay. Never calls OKX/systemctl."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest.mock import patch
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import okx_version
import okx_release_contract
import okx_observers
import okx_telegram_views as views
import okx_ui

# Load the ACTUAL monitor code in a clean extracted release, redirecting only
# its absolute shared-runtime bootstrap to this offline source tree. The rest
# of the module, imports, command handler, formatter, and business logic run
# unchanged. This avoids reading anything from the VPS LIVE shared runtime.
def _offline_monitor():
    source=Path(__file__).resolve().parents[1]/'okx_telegram_monitor.py'
    original=source.read_text(encoding='utf-8')
    old="_SHARED_CODE = Path('/opt/okx-grid-live')"
    if original.count(old)!=1:
        raise AssertionError('Unexpected monitor bootstrap: cannot isolate offline')
    adapted=original.replace(old,"_SHARED_CODE = Path(__file__).resolve().parent")
    module=types.ModuleType('okx_telegram_monitor_v0729_offline')
    module.__file__=str(source)
    exec(compile(adapted,str(source),'exec'),module.__dict__)
    return module
monitor=_offline_monitor()


class Maintenance0729(unittest.TestCase):
    def test_version_contract_without_literal_pins(self):
        self.assertTrue(okx_release_contract.check())
        release=okx_version.RELEASE_VERSION
        engine=okx_version.ENGINE_VERSION
        self.assertTrue(okx_version.validate_version_ids())
        for pair in [(release+'-invalid',engine), (release,engine+'.1'),
                     ('invalid',engine), (engine,engine+'.1')]:
            with self.subTest(pair=pair), self.assertRaises(RuntimeError):
                okx_version.validate_version_ids(*pair)
        manifest=json.loads((Path(__file__).resolve().parents[1]/'engine_manifest.json').read_text())
        self.assertEqual(manifest['current_version'],engine)
        self.assertEqual(manifest['schema_version'],okx_version.STATE_SCHEMA)
        for file in ('test_offline.py','test_v0724.py','test_v0725.py',
                     'test_v0726.py','test_v0727.py','test_v0728.py'):
            text=(Path(__file__).resolve().parent/file).read_text()
            self.assertIn('okx_release_contract',text)

    def test_observer_greenfield_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)/'bots'; base.mkdir()
            src=Path(tmp)/'shared'; src.mkdir()
            (src/'okx_bot.py').write_bytes((Path(__file__).resolve().parents[1]/'okx_bot.py').read_bytes())
            root=base/'fresh'; root.mkdir()
            (root/'state').mkdir()
            (root/'okx_bot.py').write_bytes((src/'okx_bot.py').read_bytes())
            config={'version':okx_version.ENGINE_VERSION,'instrument':'WLD-USDT-SWAP','live_trading':False}
            (root/'config.json').write_text(json.dumps(config))
            service='okx-grid-fresh'
            fn=lambda:okx_observers.supports_unstarted_current(root,service,src,base)
            self.assertTrue(fn())
            (root/'state'/'bot_state.json').write_text('{')
            self.assertFalse(fn())
            (root/'state'/'bot_state.json').unlink()
            (root/'okx_bot.py').write_text('modified engine bytes')
            self.assertFalse(fn())
            (root/'okx_bot.py').write_bytes((src/'okx_bot.py').read_bytes())
            config['version']=okx_version.ENGINE_VERSION+'.1'
            (root/'config.json').write_text(json.dumps(config))
            self.assertFalse(fn())

    def test_market_sell_format_single_indicator(self):
        inputs=[('2.0','-0.1','1.0','🟩'),
                ('0.5','-0.5','1.0','🟨'),
                ('-2.0','0.1','1.0','🟥'),
                ('0','0','1.0','🟨')]
        for actual,execution,plan,indicator in inputs:
            data={'qty_base':Decimal('0.02'), 'cycles':1,
                  'actual':Decimal(actual), 'execution':Decimal(execution),
                  'plan':Decimal(plan), 'avg_price':Decimal('1215.24')}
            with self.subTest(actual=actual),patch.object(views,'market_ledger',return_value=data),\
                 patch.object(views,'read_json',return_value={'instrument':'ZEC-USDT-SWAP'}):
                msg=views.market_message('zec0724',Path('/not/read'),{},
                    {'fill':3,'ack':33,'full':100})
                self.assertIn(indicator+' Market SELL 0.02 ZEC (1) по 1215.24',msg)
                self.assertIn('Исполнение',msg)
                self.assertNotIn('🟩 Исполнение',msg)
                self.assertNotIn('🟥 Исполнение',msg)
                self.assertIn('Fill ≈3 мс | ACK 33 мс | Full ≈100 мс',msg)
                self.assertNotIn('План',msg)

    def test_account_total_status_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            pairs=[]
            for name,amount in [('b1','1.2511'),('b2','-3.8260')]:
                root=Path(tmp)/name; (root/'state').mkdir(parents=True)
                inst=name.upper()+'-USDT-SWAP'
                (root/'config.json').write_text(json.dumps({'instrument':inst}))
                dec=Decimal(amount)
                perf={'schema':1,'total_pnl':str(dec),'grid_profit':'1',
                      'limit_tp_net':'1','market_tp_net':'0',
                      'partial_exit_net':'0','floating_pnl':str(dec-1),
                      'realized_open_components':'0'}
                generated=time.time()
                (root/'state'/'stats.json').write_text(json.dumps({'instrument':inst,'complete':True,
                    'generated_at_unix':generated,'performance':perf}))
                (root/'state'/'stats_financial_anchor.json').write_text(json.dumps({'schema':1,
                    'instrument':inst,'at':str(generated),'total_pnl_usdt':str(dec)}))
                pairs.append((name,root))
            total=monitor._account_total_pnl(pairs)
            self.assertEqual(total,Decimal('-2.5749'))
            line=monitor._account_total_line(pairs)
            self.assertEqual(line,'<b>🔴 ACCOUNT TOTAL PnL: −$2.57 🔴</b>')
            dispatched=[]
            class FakeApi:
                def send(self,message,parse_mode=None):
                    dispatched.append((message,parse_mode))
            with patch.object(monitor,'refresh_bots'),\
                 patch.object(monitor,'_active_service',return_value=True),\
                 patch.object(monitor,'mode_and_stats',side_effect=lambda n:'STAT '+n),\
                 patch.object(monitor,'BOT_DIRS',{n:str(r) for n,r in pairs}):
                monitor.send_report(FakeApi(),monitor.status_report(include_account=True))
            self.assertTrue(dispatched)
            self.assertIn('ACCOUNT TOTAL PnL: −$2.57 🔴',dispatched[-1][0])
            self.assertEqual(dispatched[-1][1],'HTML')
            # Missing input must not swallow /status.
            (pairs[0][1]/'state'/'stats_financial_anchor.json').unlink()
            self.assertIn('н/д',monitor._account_total_line(pairs))

    def test_uptime_process_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            uptime_file=Path(tmp)/'uptime';uptime_file.write_text('10000.0 100.0')
            info={'ActiveState':'active','ActiveEnterTimestampMonotonic':'9990000000'}
            proc,host=okx_ui.uptimes(info,uptime_file)
            self.assertAlmostEqual(proc,10.0)
            self.assertAlmostEqual(host,10000.0)


if __name__=='__main__':
    unittest.main(verbosity=2)
