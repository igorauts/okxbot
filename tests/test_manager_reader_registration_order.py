"""Offline contract for first-start Reader registration and subsequent user consent."""
import ast
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import json
import okxbot as m

ROOT=pathlib.Path(__file__).resolve().parents[1]

class ReaderFirstStart(unittest.TestCase):
    def test_order_by_ast(self):
        tree=ast.parse((ROOT/'okxbot.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_start')
        def calls(name):
            return [n.lineno for n in ast.walk(fn) if isinstance(n,ast.Call)
                    and isinstance(n.func,ast.Name) and n.func.id==name]
        regs=sorted(calls('reader_registration_ready'))
        audits=calls('audit_entry')
        asks=calls('_read_start_confirmation')
        self.assertEqual(len(regs),2)
        self.assertEqual(len(audits),1)
        self.assertEqual(len(asks),1)
        self.assertLess(regs[0],audits[0])
        self.assertLess(audits[0],asks[0])
        self.assertLess(asks[0],regs[1])

    def run_start(self, *, registration_error=False, approve=False):
        events=[]
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)
            (p/'.venv/bin').mkdir(parents=True)
            (p/'.venv/bin/python').touch()
            (p/'config.json').write_text(json.dumps({'instrument':'MMT-USDT-SWAP',
                'grid_step':'0.1','take_profit':'0.1','live_trading':True}))
            entry={'path':str(p),'service':'okx-grid-mmt-test'}
            def reg(*a,**k):
                events.append('register')
                if registration_error: raise RuntimeError('Reader refused')
                return True
            def audit(*a,**k): events.append('audit')
            def confirm(*a,**k):
                events.append('confirm')
                return 'START mmt-test' if approve else ''
            def call(*a,**k): events.append('systemctl')
            with (patch.object(m,'find_entry',return_value=('mmt-test',entry)),
                  patch.object(m,'bot_path',return_value=p),
                  patch.object(m,'unit_name',return_value='okx-grid-mmt-test.service'),
                  patch.object(m,'instance_terminal',return_value=False),
                  patch.object(m,'control_desired',return_value='RUN'),
                  patch.object(m,'service_running',return_value=False),
                  patch.object(m,'account_soft_active',return_value=False),
                  patch.object(m,'scan_other_live_same_instrument',return_value=None),
                  patch.object(m,'reader_registration_ready',side_effect=reg),
                  patch.object(m,'audit_entry',side_effect=audit),
                  patch('builtins.input',side_effect=confirm),
                  patch.object(m,'run',side_effect=call)):
                if registration_error:
                    with self.assertRaisesRegex(RuntimeError,'Reader refused'): m.cmd_start('mmt-test')
                else: m.cmd_start('mmt-test')
        return events

    def test_failed_registration_blocks_audit_and_start(self):
        self.assertEqual(self.run_start(registration_error=True), ['register'])

    def test_cancel_does_not_start(self):
        self.assertEqual(self.run_start(),['register','audit','confirm'])

    def test_start_double_registration_after_confirm(self):
        self.assertEqual(self.run_start(approve=True),
                         ['register','audit','confirm','register','systemctl','systemctl'])

if __name__=='__main__': unittest.main()
