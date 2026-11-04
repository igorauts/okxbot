"""Memory fallback contract; no OKX or service access."""
import ast
import pathlib
import threading
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class TestStatsNoRetainedCache(unittest.TestCase):
    def test_constructor_without_global_cache(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        worker=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='StatsWorker')
        ns={'threading':threading}
        exec(compile(ast.Module(body=[worker],type_ignores=[]),'offline-stats-worker','exec'),ns)
        obj=ns['StatsWorker'](object(),market=object())
        self.assertIsNone(obj.stats_cache)
        self.assertIsNone(obj.finance_cache)
    def test_orig_full_is_present_and_trade_loop_unchanged_guarded_by_builder(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='print_full_stats')
        code=ast.unparse(fn)
        self.assertIn('full_stats_snapshot(st, fills, bid, CONTRACT_VALUE, snapshot_archive=archive)',code)
        self.assertIn('stats_exchange_fills(st, snapshot_archive=archive, expected=expected)',code)
        ledger_calls=[c for c in ast.walk(fn) if isinstance(c,ast.Call)
                      and isinstance(c.func,ast.Attribute)
                      and c.func.attr=='total_for_snapshot'
                      and isinstance(c.func.value,ast.Name)
                      and c.func.value.id=='pnl']
        self.assertEqual(len(ledger_calls),1)
        self.assertEqual([ast.dump(a,include_attributes=False) for a in ledger_calls[0].args],
                         [ast.dump(ast.Name(id=n,ctx=ast.Load()),include_attributes=False) for n in ('st','archive')])
        self.assertIn("finance_mode = 'original_full'",code)
        self.assertIn('if LIVE and stats_cache is not None:',code)
if __name__=='__main__':unittest.main(verbosity=2)
