import ast
import unittest
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
ROOT=BASE/'v077' if (BASE/'v077').exists() else BASE
REF=BASE/'reference'/'okx_bot076.py'

class EngineInvariants(unittest.TestCase):
    def test_no_changes_to_trading_decisions(self):
        if not REF.exists():self.skipTest('reference not packaged')
        a=ast.parse(REF.read_text());b=ast.parse((ROOT/'okx_bot077.py').read_text())
        def find(tree):return {x.name:x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.ClassDef))}
        old,new=find(a),find(b)
        allow={'controlled_shutdown','live_loop','main','ensure_state'}
        self.assertEqual(set(old),set(new))
        for name in old:
            if name in allow:continue
            self.assertEqual(ast.dump(old[name],include_attributes=False),
                             ast.dump(new[name],include_attributes=False),
                             msg='Trading function modified: '+name)
    def test_terminal_code_does_not_post_orders(self):
        text=(ROOT/'okx_terminal_accounting_v077.py').read_text()
        self.assertNotIn("'POST'",text)
        self.assertNotIn('place_panic_sell(',text)
        self.assertNotIn('cancel_lowest_specific(',text)

if __name__=='__main__':unittest.main()

class SafetyGuards(unittest.TestCase):
    def test_terminal_cli_forces_audit_before_live_config_guard(self):
        text=(ROOT/'okx_bot077.py').read_text()
        self.assertIn("TERMINAL_ONLY = '--terminal-finalize' in sys.argv",text)
        self.assertIn("or STATS_ONLY or TERMINAL_ONLY",text)
        self.assertLess(text.index("or STATS_ONLY or TERMINAL_ONLY"),text.index("if CFG.get('live_trading') is True and not LIVE and not AUDIT"))
        self.assertIn("if method != 'GET' and not LIVE:",text)

    def test_manifest_dependencies_are_complete(self):
        import json
        data=json.loads((ROOT/'engine_manifest.json').read_text())
        entry=data['engines']['0.7.7']
        self.assertEqual(entry['file'],'okx_bot077.py')
        for name in entry['dependencies']:
            self.assertTrue((ROOT/name).is_file(),name)
