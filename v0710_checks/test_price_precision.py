import ast
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'okx_bot0710.py'
UI = ROOT / 'okx_ui074.py'

source = ENGINE.read_text()
tree = ast.parse(source)

fmt_node = next(
    n for n in tree.body
    if isinstance(n, ast.FunctionDef) and n.name == 'fmt'
)

def die(message):
    raise RuntimeError(message)

namespace = {
    'Decimal': Decimal,
    'PRICE_STEP': Decimal('0.01'),
    'die': die,
}

exec(
    compile(
        ast.Module(body=[fmt_node], type_ignores=[]),
        str(ENGINE),
        'exec'
    ),
    namespace
)

def fmt(price, tick):
    namespace['PRICE_STEP'] = Decimal(tick)
    return namespace['fmt'](Decimal(price))

class PricePrecisionTests(unittest.TestCase):

    def test_legacy_precision(self):
        self.assertEqual(fmt('2400.50', '0.01'), '2400.50')
        self.assertEqual(fmt('1144.69', '0.01'), '1144.69')

    def test_near_precision(self):
        levels = [
            fmt(str(Decimal('1.234') - Decimal('0.001') * i), '0.001')
            for i in range(5)
        ]
        self.assertEqual(
            levels,
            ['1.234', '1.233', '1.232', '1.231', '1.230']
        )

    def test_sui_five_distinct_buy_and_tp(self):
        buys = [
            Decimal('1.0040') - Decimal('0.0010') * i
            for i in range(5)
        ]
        buy_prices = [fmt(str(p), '0.0001') for p in buys]
        tp_prices = [
            fmt(str(p + Decimal('0.0015')), '0.0001')
            for p in buys
        ]

        self.assertEqual(
            buy_prices,
            ['1.0040','1.0030','1.0020','1.0010','1.0000']
        )
        self.assertEqual(
            tp_prices,
            ['1.0055','1.0045','1.0035','1.0025','1.0015']
        )
        self.assertEqual(len(set(buy_prices)), 5)

    def test_off_tick_price_rejected(self):
        with self.assertRaises(RuntimeError):
            fmt('1.00405', '0.0001')

    def test_embedded_selftest_uses_formatter(self):
        self.assertIn('probe_key=fmt(probe_price)', source)

    def test_settings_supports_0710(self):
        ui = UI.read_text()
        self.assertIn(
            "protect = 5 if ver in ('v0.7.10'",
            ui
        )
        self.assertIn(
            "max_tp=150 if ver in",
            ui
        )

if __name__ == '__main__':
    unittest.main()
