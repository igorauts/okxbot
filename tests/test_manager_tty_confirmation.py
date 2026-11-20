"""v0.7.77: typed START confirmation survives curses/locale mismatch safely."""
import ast
import io
import pathlib
import unittest
from unittest import mock
import okxbot as m

ROOT=pathlib.Path(__file__).resolve().parents[1]

class FakeTTY:
    def isatty(self): return True

class MemoryTTY(io.BytesIO):
    def fileno(self): return 0  # no real terminal I/O; tcflush is mocked

class Confirmation(unittest.TestCase):
    def from_tty(self, data):
        # Real TTY is never opened in offline tests.
        with (mock.patch.object(m.sys, 'stdin', FakeTTY()),
              mock.patch('builtins.open', side_effect=lambda *a,**kw: MemoryTTY(data)),
              mock.patch('termios.tcflush', return_value=None),
              mock.patch.object(m.sys, 'stdout', io.StringIO())):
            return m._read_start_confirmation('mmt-test')

    def test_exact_phrase_accepted(self):
        self.assertTrue(self.from_tty(b'START mmt-test\n'))

    def test_wrong_phrase_rejected(self):
        self.assertFalse(self.from_tty(b'START other\n'))

    def test_invalid_utf8_rejected_without_exception(self):
        self.assertFalse(self.from_tty(b'\xd1START mmt-test\n'))

    def test_partial_cyrillic_rejected_without_exception(self):
        self.assertFalse(self.from_tty(b'START mmt-test\xd1\n'))

    def test_empty_and_unterminated_rejected(self):
        self.assertFalse(self.from_tty(b''))
        self.assertFalse(self.from_tty(b'START mmt-test'))

    def test_overlong_line_rejected(self):
        self.assertFalse(self.from_tty(b'x'*300+b'\n'))

    def test_missing_tty_cancels(self):
        with (mock.patch.object(m.sys,'stdin',FakeTTY()),
              mock.patch('builtins.open',side_effect=OSError('no tty')),
              mock.patch.object(m.sys,'stdout',io.StringIO())):
            self.assertFalse(m._read_start_confirmation('mmt-test'))

    def test_non_tty_preserves_existing_contract(self):
        with (mock.patch.object(m.sys,'stdin',io.StringIO()),
              mock.patch('builtins.input',return_value='START mmt-test')):
            self.assertTrue(m._read_start_confirmation('mmt-test'))
        with (mock.patch.object(m.sys,'stdin',io.StringIO()),
              mock.patch('builtins.input',return_value='NOT START')):
            self.assertFalse(m._read_start_confirmation('mmt-test'))

    def test_start_uses_helper_after_audit_before_second_reader_gate(self):
        tree=ast.parse((ROOT/'okxbot.py').read_text())
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='cmd_start')
        calls=[(n.lineno,n.func.id) for n in ast.walk(fn)
               if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
               and n.func.id in ('audit_entry','reader_registration_ready','_read_start_confirmation')]
        regs=sorted(ln for ln,name in calls if name=='reader_registration_ready')
        audits=[ln for ln,name in calls if name=='audit_entry']
        asks=[ln for ln,name in calls if name=='_read_start_confirmation']
        self.assertEqual((len(regs),len(audits),len(asks)),(2,1,1))
        self.assertLess(regs[0],audits[0])
        self.assertLess(audits[0],asks[0])
        self.assertLess(asks[0],regs[1])

if __name__=='__main__': unittest.main()
