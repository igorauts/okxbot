import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

class VersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src=(ROOT/'okx_bot079.py').read_text()
        cls.tree=ast.parse(cls.src)
        cls.methods={n.name:n for n in cls.tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}

    def test_manifest_and_version(self):
        m=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertEqual(m['engines']['0.7.9']['file'],'okx_bot079.py')
        self.assertIn("VER = '0.7.9'",self.src)
        self.assertIn("REAL_TP_MAX = 150",self.src)
        self.assertIn("REDUCE_ONLY_HARD_CAP = 150",self.src)

    def test_no_pre_buy_surplus_cancellation_even_at_150(self):
        segment=self.src.split('            # v0.7.9: all TP surplus waits',1)[1].split('            waiting=bool',1)[0]
        self.assertIn('if real_tp_now>REAL_TP_TARGET:',segment)
        self.assertIn("tp_action='NONE'",segment)
        self.assertNotIn('real_tp_now <= REAL_TP_MAX',segment)
        self.assertIn('maintain_virtual_tp',segment) # under-target SELL still rearmed

    def test_buy_limits_unchanged(self):
        for token in ('BUY_BUFFER = 5', 'BUY_MAX = TARGET + BUY_BUFFER',
            'allowed_buys=max(0,min(BUY_MAX,REDUCE_ONLY_HARD_CAP-real_tp_count-5))',
            'if not level_free(st,price):continue',
            'if len(active_buy(st))>=allowed_buys or count(st)>=CAP:break',
            'if not all_resolved:', "and 1<=TARGET<=min(CAP,25)"):
            self.assertIn(token,self.src,token)

    def test_virtual_escape_and_orders_unchanged_ast(self):
        base=ast.parse((ROOT/'okx_bot078.py').read_text())
        funcs=lambda t:{n.name:n for n in t.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
        old,new=funcs(base),funcs(self.tree)
        for name in ('virtual_escape','reconcile_position','place','place_virtual_tp','cancel_tp',
                     'coverage_audit','plan_buy_buffer_cleanup','missing_buy_levels_for_addition'):
            self.assertIn(name,old)
            self.assertEqual(ast.dump(old[name],include_attributes=False),
                             ast.dump(new[name],include_attributes=False), name)

    def test_scheduler_15_25_55_and_cap(self):
        cls=self.methods['TPBufferScheduler']
        namespace={'REAL_TP_TARGET':5,'REAL_TP_MAX':150,'TP_BUFFER_CANCEL_INTERVAL':.1,
                   'TP_BUFFER_COOLDOWN':1.5}
        exec(compile(ast.Module(body=[cls],type_ignores=[]),'<scheduler>','exec'),namespace)
        S=namespace['TPBufferScheduler']
        for count in (15,25,55,149):
            x=S();x.quiet_since=100.
            self.assertFalse(x.cancel_due(count,100.1),count)
            self.assertTrue(x.cancel_due(count,101.51),count)
            x.canceled(101.51)
            self.assertFalse(x.cancel_due(count,101.55),count)
        self.assertTrue(S().cancel_due(150,100.0))

    def test_new_engine_reads_old_state_without_reset(self):
        self.assertIn("previous_version == '0.7.8'",self.src)
        self.assertIn("bot04_state_before_v079.json",self.src)
        self.assertIn("'0.7.5','0.7.6','0.7.7','0.7.8') and AUDIT",self.src)

    def test_observer_079_accepts_new_state_and_tp150(self):
        src=(ROOT/'okx_execution_observer079.py').read_text()
        ast.parse(src)
        self.assertIn("('0.7.7','0.7.8','0.7.9')",src)
        self.assertIn("if s['tp'] > 150:",src)
        self.assertNotIn("if s['tp'] > self.target_tp + self.target_buy + 5:",src)

if __name__=='__main__':unittest.main()
