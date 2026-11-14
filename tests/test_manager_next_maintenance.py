"""Maintenance regression tests: only offline mocks; no network/systemd mutations."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import okx_setup as s
import okxbot as m


def spec(coin,rule='normal'):
    return {'instId':coin+'-USDT-SWAP','instType':'SWAP','instFamily':coin+'-USDT',
            'settleCcy':'USDT','ctType':'linear','state':'live',
            'ruleType':rule,'tickSz':'0.1','lotSz':'1','minSz':'1','ctVal':'1'}


class TickerSafety(unittest.TestCase):
    def test_classification_fail_closed(self):
        self.assertEqual(s.contract_rule_type(spec('BTC')),'normal')
        self.assertEqual(s.contract_rule_type(spec('XYZ','pre_market')),'pre_market')
        for x in ({}, {'ruleType':'new'}, {'ruleType':None}):
            with self.assertRaises(ValueError):s.contract_rule_type(x)

    def test_public_catalog_filters_unknown(self):
        data=[spec('BTC'),spec('PRE','pre_market'),spec('BAD','unknown')]
        with patch.object(s,'okx_req',return_value=data),patch.object(s,'validate_spec',side_effect=lambda inst,item:inst.split('-')[0]):
            cat=s.available_usdt_swaps()
        self.assertEqual(set(cat),{'BTC','PRE'})
        self.assertEqual([x for x in cat if s.contract_rule_type(cat[x])=='normal'],['BTC'])

    def test_exact_and_name_and_24h(self):
        cat={'ETH':spec('ETH'),'ETHFI':spec('ETHFI'),'ETC':spec('ETC')}
        self.assertEqual(s.find_ticker_matches(cat,'ETH'),['ETH','ETHFI'])
        self.assertEqual(s.normalize_ticker('btc-usdt-swap'),'BTC')
        self.assertEqual(s.suggest_bot_name('BTC',('btc'+''.join(s.MANAGER_RELEASE_VERSION.split('.')),)),
                         'btc'+''.join(s.MANAGER_RELEASE_VERSION.split('.'))+'-2')
        self.assertEqual(s.ticker_24h_range({'low24h':'1','high24h':'2'}),
                         (s.D('1'),s.D('2')))

    def test_fresh_spec_rule_change_rejected(self):
        ctx={'coin':'BTC','instrument':'BTC-USDT-SWAP','spec':spec('BTC')}
        fresh=spec('BTC','pre_market')
        with patch.object(s,'okx_req',return_value=[fresh]),patch.object(s,'validate_spec',return_value='BTC'):
            with self.assertRaises(RuntimeError):m.verify_fresh_contract(ctx)


class FailureCleanup(unittest.TestCase):
    def test_quarantine_on_unregistered_inactive_install(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)/'bots';base.mkdir()
            units=Path(td)/'units';units.mkdir()
            name='testbot';service='okx-grid-testbot';root=base/'testbot';root.mkdir()
            (root/'state').mkdir()
            (root/'config.json').write_text(json.dumps({'live_trading':False}))
            (root/'.env').write_text('TEST_SECRET=never_print')
            unit=units/(service+'.service')
            unit.write_text(m.unit_text(root, 'okx_bot.py', name))
            observer=units/('observer-'+service+'-combined.service')
            observer.write_text(f'[Service]\nWorkingDirectory={root}\nDescription={service}\n')
            from types import SimpleNamespace
            def fake_run(cmd,**kwargs):
                if cmd[:2]==['systemctl','show']:
                    return SimpleNamespace(stdout='ActiveState=inactive\nMainPID=0\n',returncode=0)
                return SimpleNamespace(stdout='',returncode=0)
            with patch.object(m,'BASE',base),patch.object(m,'SYSTEMD_DIR',units),\
                 patch.object(m,'load_registry',return_value={}),patch.object(m,'run',side_effect=fake_run),\
                 patch.object(m.subprocess,'run',side_effect=fake_run):
                dest=m.cleanup_unregistered_creation(name,root,service,unit,RuntimeError('bootstrap'))
            self.assertFalse(root.exists())
            self.assertFalse(unit.exists())
            self.assertFalse(observer.exists())
            self.assertTrue((dest/'.env').exists())
            self.assertEqual((dest/'.env').read_text(),'TEST_SECRET=never_print')

    def test_foreign_unit_identity_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)/'bots';base.mkdir()
            units=Path(td)/'units';units.mkdir()
            name='testbot';service='okx-grid-testbot';root=base/name;root.mkdir()
            (root/'state').mkdir()
            (root/'config.json').write_text(json.dumps({'live_trading':False}))
            # Correct root but intentionally NO service identity: fail closed.
            unit=units/(service+'.service')
            unit.write_text(f'[Service]\nWorkingDirectory={root}\nExecStart=/bin/true\n')
            from types import SimpleNamespace
            def fake_systemctl(cmd,**kwargs):
                if cmd[:2]==['systemctl','show']:
                    return SimpleNamespace(stdout='ActiveState=inactive\nMainPID=0\n',returncode=0)
                raise AssertionError('cleanup must not mutate systemd')
            with patch.object(m,'BASE',base),patch.object(m,'SYSTEMD_DIR',units),\
                 patch.object(m,'load_registry',return_value={}),\
                 patch.object(m,'run',side_effect=AssertionError('unexpected mutation')),\
                 patch.object(m.subprocess,'run',side_effect=fake_systemctl):
                with self.assertRaisesRegex(RuntimeError,'unit identity mismatch'):
                    m.cleanup_unregistered_creation(name,root,service,unit,RuntimeError('test'))
            self.assertTrue(root.exists())
            self.assertTrue(unit.exists())

    def test_registered_instance_never_removed(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'testbot';root.mkdir()
            with patch.object(m,'BASE',base),patch.object(m,'load_registry',return_value={'testbot':{'service':'okx-grid-testbot'}}):
                with self.assertRaises(RuntimeError):
                    m.cleanup_unregistered_creation('testbot',root,'okx-grid-testbot',
                                                    Path(td)/'unit',RuntimeError('fail'))


class RealCreationPathFaults(unittest.TestCase):
    def test_cmd_new_bootstrap_failure_cleans_units_and_keeps_secret(self):
        import sqlite3
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)/'bots';base.mkdir()
            units=Path(td)/'units';units.mkdir()
            src=Path(td)/'source';src.mkdir()
            ctx={'name':'testbot','instrument':'BTC-USDT-SWAP','coin':'BTC',
                 'spec':spec('BTC')}
            def persist(context,path,raw):
                path.mkdir();(path/'state').mkdir();(path/'logs').mkdir()
                (path/'config.json').write_text(json.dumps({'live_trading':False}))
                (path/'.env').write_text('FAKE_CREDS=placeholder')
                return path
            def provision(root,service,*args):
                (units/('observer-'+service+'-combined.service')).write_text(
                    f'WorkingDirectory={root}\nService={service}\n')
            def fake_run(cmd,**kwargs):
                if cmd[:2]==['systemctl','show']:
                    return SimpleNamespace(stdout='ActiveState=inactive\nMainPID=0\n',returncode=0)
                return SimpleNamespace(stdout='',returncode=0)
            tty=SimpleNamespace(isatty=lambda:True)
            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(patch.object(m,'BASE',base))
                stack.enter_context(patch.object(m,'SYSTEMD_DIR',units))
                stack.enter_context(patch.object(m,'SOURCE',src))
                stack.enter_context(patch.object(m,'load_registry',return_value={}))
                stack.enter_context(patch.object(m,'require_account_capacity',return_value=0))
                stack.enter_context(patch.object(m,'latest_engine',return_value=('okx_bot.py','engine')))
                stack.enter_context(patch.object(m,'validated_newbot_env',return_value=({},'FAKE_CREDS=placeholder')))
                stack.enter_context(patch.object(m,'verify_fresh_contract',return_value=True))
                stack.enter_context(patch.object(m.sys,'stdin',tty))
                stack.enter_context(patch.object(m.sys,'stdout',tty))
                stack.enter_context(patch.object(m.curses,'wrapper',return_value=ctx))
                stack.enter_context(patch.object(m.wizard,'persist',side_effect=persist))
                stack.enter_context(patch.object(m,'provision_latest_engine',return_value=('okx_bot.py','engine')))
                stack.enter_context(patch.object(m,'unit_text',side_effect=lambda root,engine,name:
                    f'WorkingDirectory={root}\nService=okx-grid-{name}\n'))
                stack.enter_context(patch.object(m,'provision_observers',side_effect=provision))
                stack.enter_context(patch.object(m,'bootstrap_fresh_cycles_archive',side_effect=sqlite3.OperationalError('injected')))
                saved=stack.enter_context(patch.object(m,'save_registry'))
                stack.enter_context(patch.object(m,'run',side_effect=fake_run))
                stack.enter_context(patch.object(m.subprocess,'run',side_effect=fake_run))
                with self.assertRaises(sqlite3.OperationalError):
                    m.cmd_new.__wrapped__(None)
                saved.assert_not_called()
            self.assertFalse((base/'testbot').exists())
            self.assertFalse((units/'okx-grid-testbot.service').exists())
            self.assertFalse((units/'observer-okx-grid-testbot-combined.service').exists())
            dirs=list((base/'.failed_provisioning').iterdir())
            self.assertEqual(len(dirs),1)
            self.assertEqual((dirs[0]/'.env').read_text(),'FAKE_CREDS=placeholder')

    def test_cmd_clone_bootstrap_failure_cleans_units_and_keeps_secret(self):
        import io, sqlite3
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)/'bots';base.mkdir()
            units=Path(td)/'units';units.mkdir()
            src=Path(td)/'source';src.mkdir()
            (src/'okx_bot.py').write_text('# engine\n')
            (src/'requirements.txt').write_text('')
            oldroot=base/'oldbot';oldroot.mkdir()
            cfg={'instrument':'BTC-USDT-SWAP','tick_size':'1','upper_price':'110',
                 'grid_step':'1','max_grids':10,'active_buy_orders':1,
                 'active_tp_orders':1}
            (oldroot/'config.json').write_text(json.dumps(cfg))
            entry={'path':str(oldroot),'service':'okx-grid-oldbot','slug':'oldbot'}
            def provision(root,service,*args):
                (units/('observer-'+service+'-combined.service')).write_text(
                    f'WorkingDirectory={root}\nService={service}\n')
            def fake_run(cmd,**kwargs):
                if cmd[:2]==['systemctl','show']:
                    return SimpleNamespace(stdout='ActiveState=inactive\nMainPID=0\n',returncode=0)
                return SimpleNamespace(stdout='',returncode=0)
            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(patch.object(m,'BASE',base))
                stack.enter_context(patch.object(m,'SOURCE',src))
                stack.enter_context(patch.object(m,'SYSTEMD_DIR',units))
                stack.enter_context(patch.object(m,'find_entry',return_value=('oldbot',entry)))
                stack.enter_context(patch.object(m,'instance_terminal',return_value=True))
                stack.enter_context(patch.object(m,'load_registry',return_value={}))
                stack.enter_context(patch.object(m,'bot_path',return_value=oldroot))
                stack.enter_context(patch.object(m,'latest_engine',return_value=('okx_bot.py','engine')))
                stack.enter_context(patch.object(m,'latest_dependencies',return_value=[]))
                stack.enter_context(patch.object(m,'validated_newbot_env',return_value=({},'FAKE_CREDS=placeholder')))
                stack.enter_context(patch.object(m.wizard,'okx_req',return_value=[spec('BTC')]))
                stack.enter_context(patch.object(m.wizard,'validate_spec',return_value='BTC'))
                stack.enter_context(patch('urllib.request.urlopen',return_value=io.BytesIO(
                    json.dumps({'code':'0','data':[{'bidPx':'100'}]}).encode())))
                stack.enter_context(patch('builtins.input',return_value='CLONE testclone'))
                stack.enter_context(patch.object(m,'unit_text',side_effect=lambda root,engine,name:
                    f'WorkingDirectory={root}\nService=okx-grid-{name}\n'))
                stack.enter_context(patch.object(m,'provision_observers',side_effect=provision))
                stack.enter_context(patch.object(m,'bootstrap_fresh_cycles_archive',side_effect=sqlite3.OperationalError('injected')))
                saved=stack.enter_context(patch.object(m,'save_registry'))
                stack.enter_context(patch.object(m,'run',side_effect=fake_run))
                stack.enter_context(patch.object(m.subprocess,'run',side_effect=fake_run))
                with self.assertRaises(sqlite3.OperationalError):
                    m.cmd_clone('oldbot','testclone')
                saved.assert_not_called()
            self.assertFalse((base/'testclone').exists())
            self.assertFalse((units/'okx-grid-testclone.service').exists())
            self.assertFalse((units/'observer-okx-grid-testclone-combined.service').exists())
            dirs=list((base/'.failed_provisioning').iterdir())
            self.assertEqual(len(dirs),1)
            self.assertEqual((dirs[0]/'.env').read_text(),'FAKE_CREDS=placeholder')

if __name__=='__main__':unittest.main()
