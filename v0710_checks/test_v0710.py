"""v0.7.10 offline tests. Absolutely no OKX API or systemctl execution."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import okx_setup0710 as wizard
import okx_panic_cleanup0710 as cleaner
import okxbot

ROOT=Path(__file__).resolve().parents[1]


class WizardTests(unittest.TestCase):
    def test_six_symbols(self):
        self.assertEqual(list(wizard.ASSETS.values()),['BTC','ETH','SOL','NEAR','ZEC','SUI'])

    def test_engine_selected_is_0710(self):
        self.assertEqual(okxbot.latest_engine(ROOT),('okx_bot0710.py','0.7.10'))
        self.assertEqual(okxbot.latest_dependencies('okx_bot0710.py','0.7.10',ROOT),['okx_terminal_accounting_v077.py'])

    def test_contract_sizes_queried_not_assumed(self):
        for coin,ct,lot in [('NEAR','10','1'),('ZEC','0.1','0.01'),('SUI','100','1')]:
            with self.subTest(coin=coin):
                queries=[]
                def query(path,env=None,params=None):
                    queries.append((path,params))
                    if path.endswith('/instruments'):
                        return [dict(state='live',ctValCcy=coin,ctType='linear',
                                     tickSz='0.001',ctVal=ct,lotSz=lot,minSz=lot,
                                     instFamily=f'{coin}-USDT')]
                    if path.endswith('/ticker'):
                        return [{'bidPx':'10'}]
                    if path.endswith('/trade-fee'):
                        return [{'maker':'-0.0002','taker':'-0.0005','level':'VIP0'}]
                    raise AssertionError(path)
                with mock.patch.object(wizard,'okx_req',side_effect=query):
                    ctx=wizard.load_contract(coin,{})
                self.assertEqual(ctx['instrument'],f'{coin}-USDT-SWAP')
                self.assertEqual(str(ctx['min_qty']),str(wizard.D(ct)*wizard.D(lot)))
                self.assertEqual(len(queries),3)
                self.assertEqual(queries[0][1]['instId'],f'{coin}-USDT-SWAP')

    def test_new_asset_examples_do_not_raise(self):
        for coin in ('NEAR','ZEC','SUI'):
            ctx={'coin':coin,'price':wizard.D('15'),'tick':wizard.D('0.001'),
                 'min_tp':wizard.D('0.1'),'break_even':wizard.D('0.08')}
            self.assertTrue(wizard.lines_for('step',ctx))
            self.assertTrue(wizard.lines_for('tp',ctx))

    def test_old_wizard_file_retained(self):
        self.assertTrue((ROOT/'okx_setup068.py').is_file())

    def test_systemd_execstoppost_present_for_new_instances(self):
        rendered=okxbot.unit_text(Path('/root/okx/near710'),'okx_bot0710.py','near710')
        self.assertIn('ExecStopPost=/usr/bin/python3 -B ',rendered)
        self.assertIn('okx_panic_cleanup0710.py --root /root/okx/near710 --service okx-grid-near710',rendered)
        self.assertIn('ExecStart=/root/okx/near710/.venv/bin/python -u /root/okx/near710/okx_bot0710.py',rendered)


class PanicCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)/'bots';self.base.mkdir()
        self.root=self.base/'near710';(self.root/'state').mkdir(parents=True)
        self.units=Path(self.tmp.name)/'units';self.units.mkdir()
        self.service='okx-grid-near710'
        self.write('config.json',{'version':'0.7.10','instrument':'NEAR-USDT-SWAP'})
        self.write('state/bot04_state.json',{'version':'0.7.10','instrument':'NEAR-USDT-SWAP',
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
        self.write('state/bot04_state.json',{'version':'0.7.10','instrument':'NEAR-USDT-SWAP'})
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
            name=f'observer-{self.service}-{kind}-v0710.service'
            (self.units/name).write_text(f'ExecStart=/usr/bin/python3 -B /opt/okx_execution_observer0710.py --root {self.root} --service {self.service} --mode {kind}\n')
        result=cleaner.cleanup(self.root,self.service,self.units)
        self.assertTrue(result)
        self.assertEqual(len(self.calls),3)
        self.assertIn(('disable','--no-reload',self.service+'.service'),self.calls)
        self.assertNotIn(('stop','okx-telegram-monitor.service'),self.calls)
        for kind in ('burst','execution'):
            self.assertIn(('disable','--now',f'observer-{self.service}-{kind}-v0710.service'),self.calls)

    def test_ownership_check_blocks_foreign_observer(self):
        unit=f'observer-{self.service}-burst-v0710.service'
        (self.units/unit).write_text('ExecStart=python3 /fake/okx_execution_observer0710.py --root /root/okx/other --service okx-grid-other')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[('disable','--no-reload',self.service+'.service')])

    def test_missing_ack_is_noop(self):
        (self.root/'state/control_ack.json').unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cleaner.cleanup(self.root,self.service,self.units))
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
