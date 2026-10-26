#!/usr/bin/env python3
"""Pure read-only tests of Telegram Market SELL presentation."""
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import okx_telegram_views as views


class MarketTelegramMessageTests(unittest.TestCase):
    def render(self, actual, plan, *, price='1215.24', execution=None, tp_ms=2452):
        a, p = Decimal(actual), Decimal(plan)
        delta = a-p if execution is None else Decimal(execution)
        obj = {'qty_base':Decimal('0.02'), 'cycles':1,
               'avg_price':Decimal(price), 'actual':a, 'plan':p,
               'execution':delta}
        timing={'fill':19,'ack':42,'full':113}
        if tp_ms is not None:
            timing['tp_to_intent_ms']=tp_ms
        with patch.object(views,'market_ledger',return_value=obj),\
             patch.object(views,'read_json',return_value={
                 'instrument':'ZEC-USDT-SWAP','tick_size':'0.01'}):
            return views.market_message('zec0724',Path('/not/used'),{},timing)

    def test_screenshot_example_yellow(self):
        msg=self.render('0.007792','0.010284')
        self.assertEqual(msg,
            '<b>🟨 Market SELL 0.02 ZEC (1) по 1215.24\n'
            '+$0.007792 к профиту (75.8%)</b>\n'
            'Исполнение −$0.002492 (-24.2%)\n'
            '<code>⚡ Fill ≈19 мс | ACK 42 мс | Full ≈113 мс</code>\n'
            '<code>⏱ BID TP→Intent ≈2452 мс</code>')

    def test_green_if_100_percent_or_more(self):
        for a,p,expected in [('1','1','100.0%'),('1.1','1','110.0%')]:
            with self.subTest(a=a,p=p):
                msg=self.render(a,p)
                self.assertIn('🟩 Market SELL',msg)
                self.assertIn(expected,msg)
                self.assertNotIn('🟥',msg)
                self.assertNotIn('🟨',msg)

    def test_yellow_zero_to_below_100(self):
        for a,p in [('0','1'),('0.001','1'),('0.999999','1')]:
            with self.subTest(a=a,p=p):
                self.assertIn('🟨 Market SELL',self.render(a,p))

    def test_red_when_pnl_negative(self):
        msg=self.render('-0.00429','0.010348')
        self.assertIn('🟥 Market SELL',msg)
        self.assertIn('к профиту',msg)
        self.assertNotIn('🟩',msg)

    def test_nonpositive_plan_uses_no_fabricated_percentage(self):
        for actual,plan,indicator in [
            ('0.1','0','🟨'),('0.1','-1','🟨'),('0','0','🟨'),
            ('-0.1','0','🟥'),('-0.1','-1','🟥')]:
            with self.subTest(actual=actual,plan=plan):
                msg=self.render(actual,plan)
                self.assertIn(indicator+' Market SELL',msg)
                self.assertIn('к профиту (н/д)',msg)
                self.assertIn('Исполнение ',msg)
                self.assertNotIn('🟩 Исполнение',msg)
                self.assertNotIn('🟥 Исполнение',msg)

    def test_missing_optional_tp_metric_keeps_four_lines(self):
        msg=self.render('0.1','0.1',tp_ms=None)
        self.assertNotIn('BID TP→Intent',msg)
        self.assertEqual(msg.count('\n'),3)


if __name__=='__main__':
    unittest.main(verbosity=2)
