#!/usr/bin/env python3
"""Offline /status tests: no Telegram, exchange, systemd or live state writes."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('okx_status',Path(__file__).resolve().parent.parent/'okx_telegram_monitor.py')
mod=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

NOW=1792200000.0
START=NOW-(((12*24+7)*60+13)*60) # 12d 7h 13min

REPORT='''[12:00:00 UTC] 📊 OKX ETH GRID STATISTICS
Запущен: 15.10.2026 13:55:19 UTC
Текущий BID:                $2,456.23
Закрытых циклов:            3842
BUY LIMIT:                  25
  ждут TP:                  25
  виртуальных TP:           100
GRID PROFIT:                +$20.56488570
Уплачено комиссий:         $1.23450000
Общий объём:               $23,400.5000
Прибыль за последние 24ч:  +$13.0572 (0.3254%)
Открытая позиция:          0.20000000 ETH
Нереализованный PnL:       -$12.97202430
ИТОГОВЫЙ PnL:              +$7.59286140 (0.1892%)
Средний темп:
  сутки:                    +$9.4542 (0.2356%)
  30 дней:                  +$283.62 (7.0679%)
  год:                      +$3450.77 (85.9925%)
МАКС. ПРОСАДКА БОТА:       -$96.17391585 (-2.3966%)
  Зафиксирована:           15.10.2026 13:55:19 UTC
  max position:             50.00000000 ETH
'''

def setup(root, *, complete=True, mismatch=False):
    (root/'state').mkdir(parents=True)
    (root/'state/control.json').write_text(json.dumps({'mode':'RUN'}))
    (root/'state/bot04_state.json').write_text(json.dumps({
        'version':'0.7.5','instrument':'ETH-USDT-SWAP','started_at':START,
    }))
    (root/'state/okx_stats_grid_reference_v059.json').write_text(json.dumps({
        'started_at':START,'top_buy':'2600',
    }))
    (root/'state/stats.json').write_text(json.dumps({
        'complete':complete,'instrument':'ETH-USDT-SWAP','bot_version':'0.7.5',
        'generated_at_unix':NOW-30,'report':REPORT if not mismatch else REPORT.replace('0.20000000 ETH','0.30000000 ETH'),
    }))
    (root/'config.json').write_text(json.dumps({'instrument':'ETH-USDT-SWAP','contract_value':'0.1'}))

class TestStatus(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.root=Path(self.temp.name)/'eth064'
        setup(self.root)
        self.a=patch.dict(mod.BOT_DIRS,{'ETH':str(self.root)},clear=True)
        self.b=patch.dict(mod.SERVICES,{'ETH':'mock-service'},clear=True)
        self.c=patch.object(mod,'_service_status',return_value=('🟢','running'))
        self.d=patch.object(mod.time,'time',return_value=NOW)
        self.e=patch.object(mod,'_historical_max_position',return_value='1.14300000 ETH')
        for p in (self.a,self.b,self.c,self.d,self.e):p.start()
    def tearDown(self):
        for p in (self.e,self.d,self.c,self.b,self.a):p.stop()
        self.temp.cleanup()

    def test_status_all_sections(self):
        s=mod.status_report()
        self.assertIn('🟢 eth064 · v0.7.5 · running 12d 7h 13min',s)
        self.assertIn('Закрытых циклов: 3842',s)
        self.assertIn('Торговый объём: $23,400.5000',s)
        self.assertIn('Уплаченная комиссия: $1.23450000',s)
        self.assertIn('Прибыль за последние 24ч: +$13.0572',s)
        self.assertIn('  30 дней: +$283.62 (7.0679%)',s)
        self.assertIn('Максимальная просадка бота: -$96.17391585 (-2.3966% от 1x)',s)
        self.assertIn('Максимальная позиция в истории: 1.14300000 ETH',s)
        self.assertNotIn('BUY LIMIT',s)
        self.assertNotIn('ждут TP',s)
        self.assertNotIn('виртуальных TP',s)
        self.assertNotIn('50.00000000 ETH',s)
        self.assertLess(len(s),3900)

    def test_restart_does_not_reset_age(self):
        # A systemd process could be one minute old; state start stays original.
        self.assertEqual(mod._age_label(mod._launch_epoch(self.root,mod._report_values(REPORT))),'12d 7h 13min')

    def test_position_peak_uses_manager_result_only(self):
        from types import ModuleType
        import sys
        helper=ModuleType('okx_position_peak')
        calls=[]
        def format_peak(path,instrument):
            calls.append((path,instrument))
            return ['Расчётная вместимость сетки: 50.00000000 ETH',
                    'МАКС. ПОЗИЦИЯ В ИСТОРИИ: 1.14300000 ETH']
        helper.format_peak=format_peak
        self.e.stop()
        try:
            with patch.dict(sys.modules,{'okx_position_peak':helper}):
                self.assertEqual(mod._historical_max_position(self.root,'ETH-USDT-SWAP'),'1.14300000 ETH')
        finally:
            self.e.start()
        self.assertEqual(calls,[(self.root,'ETH-USDT-SWAP')])

    def test_position_peak_absent_shows_nd(self):
        from types import ModuleType
        import sys
        helper=ModuleType('okx_position_peak')
        helper.format_peak=lambda path,inst:['max position: 50.00000000 ETH']
        self.e.stop()
        try:
            with patch.dict(sys.modules,{'okx_position_peak':helper}):
                self.assertEqual(mod._historical_max_position(self.root,'ETH-USDT-SWAP'),'н/д')
        finally:
            self.e.start()

    def test_unknown_start_has_no_made_up_age(self):
        (self.root/'state/bot04_state.json').unlink()
        (self.root/'state/okx_stats_grid_reference_v059.json').unlink()
        self.assertIsNone(mod._launch_epoch(self.root,{}))
        self.assertEqual(mod._age_label(None),'н/д')

    def test_event_alarm_code_unchanged(self):
        s=mod.format_alert('ETH',{'event':'BUY_POST_UNRESOLVED','severity':'WARNING','reason':'timeout'})
        self.assertIn('BUY POST unresolved',s)

if __name__=='__main__':unittest.main()
