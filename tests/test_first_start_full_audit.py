"""Full first-start financial audit on real manager/engine code; no network or systemd.
Only external exchange/IPC, UI input, process launch and worker scheduling are faked.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import okxbot as manager
from okx_version import ENGINE_VERSION
from okx_source_attestation import verify_instance
from okx_live_upgrade import audit_output_complete

ROOT=Path(__file__).resolve().parents[1]
INST='BIGTIME-USDT-SWAP'
SPEC={'instId':INST,'instType':'SWAP','ctType':'linear','settleCcy':'USDT',
      'state':'live','ctValCcy':'BIGTIME','tickSz':'0.001','ctVal':'1',
      'lotSz':'1','minSz':'1','ruleType':'normal'}

class FirstStart(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.tmp=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.base=self.tmp/'bots';self.base.mkdir();self.units=self.tmp/'units';self.units.mkdir()
        self.stdout=io.StringIO();self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.calls=[];self.pending=[];self.pos='0';self.enable=[];self.audits=[];self.boot=[]
        self.confirm=True;self.seq=0
        self.raw='\n'.join(['OKX_API_KEY=fake','OKX_API_SECRET=fake','OKX_API_PASSPHRASE=fake',
            'OKX_POSITION_READER_SOCKET='+str(self.tmp/'reader.sock'),
            'OKX_ACCOUNT_SOFT_STOP_PATH='+str(self.tmp/'account_soft.json'),
            'OKX_ACCOUNT_POSITIONS_GATE_PATH='+str(self.tmp/'positions.json'),
            'OKX_FILL_HISTORY_GATE_PATH='+str(self.tmp/'fills.json')])+'\n'
        self.ctx=dict(name='bigtimefresh',coin='BIGTIME',instrument=INST,upper=D(1),price=D(1),
                      step=D('.01'),tp=D('.01'),qty=D(4),ct=D(1),lot=D(1),min_contracts=D(1),
                      spec=SPEC,tick=D('.001'),real_tp=3,buys=3,grids=20,maker=D('.0002'),taker=D('.0005'))
        for name,value in [('BASE',self.base),('REGISTRY',self.base/'registry.json'),('SYSTEMD_DIR',self.units),
                           ('SOURCE',ROOT),('service_running',lambda unit:False),
                           ('account_soft_active',lambda:False),('reader_registration_ready',lambda *a:True),
                           ('validated_newbot_env',lambda *a:({},self.raw)),('run',self.run_external),
                           ('provision_observers',lambda *a:None),
                           ('_read_start_confirmation',lambda name:self.confirm)]:
            self.stack.enter_context(patch.object(manager,name,value))
        self.stack.enter_context(patch.object(manager.wizard,'BASE',ROOT))
        self.stack.enter_context(patch.object(manager.wizard,'okx_req',return_value=[SPEC]))
        self.stack.enter_context(patch.object(manager.curses,'wrapper',return_value=self.ctx))
        self.stack.enter_context(patch.object(manager.sys.stdin,'isatty',return_value=True))
        self.stack.enter_context(patch.object(manager.sys.stdout,'isatty',return_value=True))
        # Hard tripwire: all actual sockets and non-mocked subprocesses forbidden.
        self.stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('real network forbidden')))
        self.stack.enter_context(patch('socket.socket.connect_ex',side_effect=AssertionError('real network forbidden')))
        self.stack.enter_context(patch('subprocess.run',side_effect=AssertionError('real subprocess forbidden')))
        self.stack.enter_context(patch('urllib.request.urlopen',side_effect=lambda *a,**kw:io.BytesIO(json.dumps({'code':'0','data':[{'bidPx':'1'}]}).encode())))

    def engine_run(self,root,*,live=False):
        self.seq+=1
        name='_offline_first_start_'+str(self.seq)
        env={k:v for k,v in os.environ.items() if not k.startswith('OKX_GRID_')}
        env.update(OKX_LATENCY_PROFILE='0',OKX_RECON_PROFILE='0')
        env.update({'OKX_GRID_LIVE':'CONFIRM','OKX_GRID_PROBE':'FULL_25'} if live else {'OKX_GRID_AUDIT':'READ_ONLY'})
        with patch.dict(os.environ,env,clear=True), patch.object(sys,'argv',[str(root/'okx_bot.py')]):
            spec=importlib.util.spec_from_file_location(name,root/'okx_bot.py')
            e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
            def req(method,path,params=None,*a,**kw):
                self.calls.append((method,path))
                self.assertEqual(method,'GET','unexpected exchange write')
                if path=='/api/v5/account/config':return [{'posMode':'net_mode','uid':'offline-test'}]
                if path=='/api/v5/public/instruments':return [SPEC]
                if path=='/api/v5/trade/orders-pending':return self.pending
                if path=='/api/v5/market/ticker':return [{'bidPx':'1'}]
                raise AssertionError('unexpected GET '+path)
            def reader(*a,**kw):
                self.calls.append(('IPC','positions'))
                return [{'instId':INST,'posSide':'net','pos':self.pos}]
            with patch.object(e.A,'req',side_effect=req),patch('okx_position_reader_client.read_positions',side_effect=reader):
                if live:
                    # Real main, real initial_state, real PnL/fill bootstrap;
                    # stop at the trading-loop boundary (no market simulation here).
                    def loop(st,*a):
                        self.assertTrue(st['bootstrapped']);self.assertTrue(e.DATA.is_file())
                        self.assertEqual(st['cycles'],{});self.boot.append(root.name)
                    with contextlib.ExitStack() as w:
                        for attr in ('WSWatcher','PublicBidWatcher','PnLWorker','StatsWorker','BotDrawdownSampler'):
                            w.enter_context(patch.object(e,attr,return_value=SimpleNamespace(start=lambda:None,wake=None)))
                        w.enter_context(patch.object(e,'live_loop',side_effect=loop));e.main()
                else:
                    report=io.StringIO()
                    with contextlib.redirect_stdout(report):
                        if self.pos!='0':
                            original=e.reconcile_position
                            with patch.object(e,'reconcile_position',side_effect=lambda st,**kw:original(st,timeout=0)):
                                e.main()
                        else:e.main()
                    out=report.getvalue();self.audits.append(out)
                    self.assertTrue(audit_output_complete(0,out),out)
                    self.assertIn('GRID PROFIT:               +$0.00000000',out)
                    self.assertIn('Открытая позиция:         0.00000000 BIGTIME',out)
                    self.assertIn('✅ Все закрытые циклы сопоставлены.',out)
                    self.assertFalse(e.DATA.exists(),'audit must not fabricate trading state')
            return e

    def run_external(self,args,**kw):
        args=[str(x) for x in args]
        if args[:3]==[sys.executable,'-m','venv']:
            p=Path(args[3])/'bin';p.mkdir(parents=True);(p/'python').touch()
        elif args[0].endswith('/pip'):pass
        elif args[:2]==['systemctl','daemon-reload']:pass
        elif args[:2]==['systemctl','status']:pass
        elif args[:3]==['systemctl','enable','--now']:
            unit=args[3];entry=next(e for e in manager.load_registry().values() if manager.unit_name(e)==unit)
            root=manager.bot_path(entry);self.assertEqual(json.loads((root/'config.json').read_text())['live_trading'],True)
            self.assertFalse((root/'state/bot_state.json').exists())
            self.enable.append(unit);self.engine_run(root,live=True)
        elif args[0].endswith('/python') and args[-1].endswith('/okx_bot.py'):
            self.assertEqual(kw['env']['OKX_GRID_AUDIT'],'READ_ONLY')
            self.assertNotIn('OKX_GRID_LIVE',kw['env']);self.engine_run(Path(args[-1]).parent)
        else:raise AssertionError('unexpected external command '+repr(args))
        return SimpleNamespace(stdout='',returncode=0)

    def create(self,answer='no'):
        with patch('builtins.input',return_value=answer):manager.cmd_new(None)
        root=manager.bot_path(manager.find_entry('bigtimefresh')[1]);self.assertTrue(verify_instance(root,ROOT))
        self.assert_partial_policy(root)
        return root

    def assert_partial_policy(self,root):
        cfg=json.loads((root/'config.json').read_text())
        self.assertEqual((cfg['partial_buy_timeout_sec'],cfg['partial_buy_distance_steps'],cfg['partial_buy_policy_revision']),(5,3,2))

    def test_new_yes_full_audit_confirmation_and_first_live_boot(self):
        root=self.create('yes')
        self.assertEqual(self.boot,[root.name]);self.assertEqual(len(self.audits),1)
        self.assertEqual(len(self.enable),1);self.assertEqual(self.calls[0],('GET','/api/v5/account/config'))
        self.assertTrue((root/'state/bot_state.json').is_file())
        self.assertEqual(json.loads((root/'state/bot_state.json').read_text())['version'],ENGINE_VERSION)

    def test_clone_full_audit_confirmation_and_first_live_boot(self):
        root=self.create();entries=manager.load_registry();entries['bigtimefresh']['retired']=True;manager.save_registry(entries)
        with patch('builtins.input',return_value='CLONE bigtimeclone'):manager.cmd_clone('bigtimefresh','bigtimeclone')
        clone=manager.bot_path(manager.find_entry('bigtimeclone')[1]);self.assertTrue(verify_instance(clone,ROOT))
        self.assert_partial_policy(clone)
        self.assertFalse((clone/'state/bot_state.json').exists());manager.cmd_start('bigtimeclone')
        self.assertEqual(self.boot,[clone.name]);self.assertEqual(len(self.enable),1)
        self.assertEqual(len(self.audits),1)

    def test_cancel_after_audit_and_retry_same_instance(self):
        root=self.create();self.confirm=False;manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable);self.assertFalse((root/'state/bot_state.json').exists())
        self.assertFalse(json.loads((root/'config.json').read_text())['live_trading'])
        self.confirm=True;manager.cmd_start('bigtimefresh');self.assertEqual(len(self.audits),2)
        self.assertEqual(self.boot,[root.name])

    def test_unknown_exchange_order_blocks_before_confirmation(self):
        self.create();self.pending=[{'ordId':'foreign','clOrdId':'foreign','side':'buy','instId':INST}]
        with self.assertRaisesRegex(RuntimeError,'Unrecognized pending'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable);self.assertFalse(self.audits)

    def test_orphan_archive_blocks_instead_of_zero_report(self):
        root=self.create()
        with contextlib.closing(sqlite3.connect(root/'state/cycles.sqlite')) as db:
            db.execute('INSERT INTO cycles VALUES (?,?)',('orphan','{}'));db.commit()
        with self.assertRaisesRegex(RuntimeError,'nonempty ledger'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable);self.assertFalse(self.audits)

    def test_previously_live_missing_state_blocks(self):
        root=self.create();cfg=json.loads((root/'config.json').read_text());cfg['live_trading']=True
        (root/'config.json').write_text(json.dumps(cfg))
        with self.assertRaisesRegex(RuntimeError,'not a pristine'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable);self.assertFalse(self.audits)

    def test_orphan_zero_pnl_row_still_blocks(self):
        root=self.create();self.confirm=False;manager.cmd_start('bigtimefresh')
        with contextlib.closing(sqlite3.connect(root/'state/pnl.sqlite')) as db:
            db.execute('INSERT INTO accounted VALUES (?,?,?,?)',('orphan','CLOSED','0','{}'));db.commit()
        with self.assertRaisesRegex(RuntimeError,'nonempty ledger'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable)

    def test_nonzero_exchange_position_blocks_first_start(self):
        self.create();self.pos='1'
        with self.assertRaisesRegex(RuntimeError,'Position mismatch persisted'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable);self.assertFalse(self.audits)

    def test_missing_archive_is_not_recreated_by_audit(self):
        root=self.create();(root/'state/cycles.sqlite').unlink()
        with self.assertRaisesRegex(RuntimeError,'Missing or unsafe first-start ledger'):manager.cmd_start('bigtimefresh')
        self.assertFalse((root/'state/cycles.sqlite').exists());self.assertFalse(self.enable)

    def test_orphan_fill_row_blocks(self):
        root=self.create();self.confirm=False;manager.cmd_start('bigtimefresh')
        with contextlib.closing(sqlite3.connect(root/'state/fills.sqlite')) as db:
            db.execute('INSERT INTO fills VALUES (?,?,?,?)',('orphan','bill','1','{}'));db.commit()
        with self.assertRaisesRegex(RuntimeError,'nonempty ledger'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable)

    def test_prior_live_event_blocks_even_if_config_false(self):
        root=self.create();(root/'logs/bot_events.jsonl').write_text(json.dumps({'event':'START','live':True})+'\n')
        with self.assertRaisesRegex(RuntimeError,'prior trading events'):manager.cmd_start('bigtimefresh')
        self.assertFalse(self.enable)

if __name__=='__main__':unittest.main()
