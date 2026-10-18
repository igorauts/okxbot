"""v0.7.11 panic-cleanup regression tests, mock systemctl only."""
import json
import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import okx_panic_cleanup0711 as cleaner

class PanicCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)/'bots';self.base.mkdir()
        self.root=self.base/'near711';(self.root/'state').mkdir(parents=True)
        self.units=Path(self.tmp.name)/'units';self.units.mkdir()
        self.service='okx-grid-near711'
        self.write('config.json',{'version':'0.7.11','instrument':'NEAR-USDT-SWAP'})
        self.write('state/bot04_state.json',{'version':'0.7.11','instrument':'NEAR-USDT-SWAP',
                    'terminal_mode':'PANIC_DONE','terminal_snapshot':{'mode':'PANIC_DONE','position_contracts':'0'}})
        self.write('state/control_ack.json',{'mode':'PANIC','phase':'PANIC_DONE','remaining_position':'0'})
        self.write('state/control.json',{'mode':'PANIC'})
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(cleaner,'BOT_ROOT',self.base))
        self.stack.enter_context(mock.patch.object(cleaner,'service_root_matches',return_value=True))
        self.calls=[]
        self.stack.enter_context(mock.patch.object(cleaner,'call_systemctl',side_effect=lambda *a:self.calls.append(a)))

    def write(self,path,data):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(data))

    def test_requires_confirmed_zero_position(self):
        self.write('state/control_ack.json',{'mode':'PANIC','phase':'WAIT_PANIC_ORDER','remaining_position':'1'})
        self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[])

    def test_requires_consistent_terminal_state(self):
        self.write('state/bot04_state.json',{'version':'0.7.11','instrument':'NEAR-USDT-SWAP'})
        self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[])

    def test_requires_panic_control(self):
        self.write('state/control.json',{'mode':'HARD'})
        self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[])

    def test_does_not_touch_other_service(self):
        with self.assertRaises(ValueError):cleaner.cleanup(self.root,'okx-grid-eth710',self.units)
        self.assertEqual(self.calls,[])

    def test_disables_exactly_two_owned_monitors_and_bot(self):
        for kind in ('burst','execution'):
            name=f'observer-{self.service}-{kind}-v0711.service'
            (self.units/name).write_text(f'ExecStart=/usr/bin/python3 -B /opt/okx_execution_observer0710.py --root {self.root} --service {self.service} --mode {kind}\n')
        result=cleaner.cleanup(self.root,self.service,self.units)
        self.assertTrue(result)
        self.assertEqual(len(self.calls),3)
        self.assertIn(('disable','--no-reload',self.service+'.service'),self.calls)
        self.assertNotIn(('stop','okx-telegram-monitor.service'),self.calls)
        for kind in ('burst','execution'):
            self.assertIn(('disable','--now',f'observer-{self.service}-{kind}-v0711.service'),self.calls)

    def test_ownership_check_blocks_foreign_observer(self):
        unit=f'observer-{self.service}-burst-v0711.service'
        (self.units/unit).write_text('ExecStart=python3 /fake/okx_execution_observer0710.py --root /root/okx/other --service okx-grid-other')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[('disable','--no-reload',self.service+'.service')])

    def test_missing_ack_is_noop(self):
        (self.root/'state/control_ack.json').unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[])
