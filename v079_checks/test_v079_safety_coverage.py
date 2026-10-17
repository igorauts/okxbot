import ast
import unittest
from pathlib import Path

class ExtraSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = (Path(__file__).resolve().parents[1] / 'okx_bot079.py').read_text()
        cls.src = src
        cls.tree = ast.parse(src)
        cls.loop = next(x for x in cls.tree.body if isinstance(x, ast.FunctionDef) and x.name == 'live_loop')

    def test_coverage_call_preserved_in_run_pre_buy(self):
        text = self.src
        marker = "            if mode=='RUN':\n                coverage_audit(st,pending,actual)"
        self.assertEqual(text.count(marker), 1)
        loop = text[text.index('def live_loop('):text.index('# OKX fill-based statistics run outside',text.index('def live_loop('))]
        self.assertLess(loop.index("if not all_resolved:"), loop.index(marker.strip('\n')))
        self.assertLess(loop.index("coverage_audit(st,pending,actual)"), loop.index('            bid=market.get_bid()'))
        self.assertLess(loop.index("coverage_audit(st,pending,actual)"), loop.index('if place(st,price):placed+=1'))

    def test_no_writes_in_added_guard(self):
        # AST: only independent coverage check in RUN. Trading ops remain elsewhere.
        nodes = [x for x in ast.walk(self.loop) if isinstance(x, ast.If)]
        matches = [x for x in nodes if isinstance(x.test, ast.Compare)
                   and ast.unparse(x.test) == "mode == 'RUN'"
                   and len(x.body) == 1 and isinstance(x.body[0], ast.Expr)
                   and isinstance(x.body[0].value, ast.Call)
                   and isinstance(x.body[0].value.func, ast.Name)
                   and x.body[0].value.func.id == 'coverage_audit']
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(matches[0].body[0].value.args), 3)

if __name__ == '__main__':unittest.main()
