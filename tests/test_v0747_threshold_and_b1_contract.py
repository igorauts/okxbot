"""vnext scope checks: no unauthorized trade, quota or Reader changes."""
import ast,pathlib,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Contracts(unittest.TestCase):
    def test_missing_fills_threshold_in_ensure_expected(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FillStore')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='ensure_expected')
        src=ast.unparse(fn)
        self.assertIn('len(missing) >= 2',src)
        self.assertIn('len(unresolved) >= 2',src)
        self.assertIn('A.fills_by_order(oid)',src)
        self.assertIn("raise ExchangeUnresolved('Invalid bulk fills-history response')",src)
    def test_b1_does_not_use_active_buy_count_as_protection(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop')
        src=ast.unparse(fn)
        policy=src[src.index('def b1_policy('):src.index('if _b1_local is not None:',src.index('def b1_policy('))]
        self.assertNotIn('active_buy(st)',policy)
        self.assertIn("c.get('state') == 'BUY_PARTIAL'",policy)
        self.assertIn('real_tp_active(st)',policy)
    def test_reader_code_not_changed(self):
        self.assertIn('background_grace_sec: float = 0.020',(ROOT/'okx_position_reader_core.py').read_text())
if __name__=='__main__':unittest.main()
