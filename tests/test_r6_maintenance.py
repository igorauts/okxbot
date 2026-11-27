"""R6/A8: real consumer paths, no LIVE or network I/O."""
import copy
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

import okxbot as manager
import okx_stats_ui as ui
import okx_telegram_views as views
import okx_funding_view as funding
from okx_version import ENGINE_VERSION
from tests.test_v0729_maintenance import _offline_monitor


class UnifiedFinance(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);(self.root/'state').mkdir()
        self.file=self.root/'state/stats.json';self.now=time.time()-1
        self.inst='DOOD-USDT-SWAP'
        self.cfg={'instrument':self.inst,'version':ENGINE_VERSION,'created_at':self.now-1000,
                  'tick_size':'0.000001','contract_value':'1','contract_lot':'1','max_grids':20}
        self.perf=dict(schema=1,bid='0.002304',top_open_buy='0.002580',open_base='621',
            open_nominal='1.430784',closed_count=12,grid_profit='10',limit_tp_net='7',market_tp_net='3',
            floating_pnl='5',partial_exit_net='2',realized_open_components='3',total_pnl='20',
            capital='100',volume='200',paid_fees='1.235',last_24h='1.2345',max_base='1000',
            grid_reference_top='0.003',grid_bottom='0.001',grid_average='0.002',
            launch_at=self.now-600,tp_waiting_all=1)
        self.anchor=dict(schema=1,instrument=self.inst,at=self.now,bid=self.perf['bid'],total_pnl_usdt='20')
        # Deliberately misleading text: all CURRENT finance data must ignore it.
        self.stats=dict(schema_version=1,complete=True,instrument=self.inst,bot_version=ENGINE_VERSION,
            generated_at_unix=self.now,financial_anchor=self.anchor,performance=self.perf,
            report='GRID PROFIT: +$999999\nИТОГОВЫЙ PnL: +$999999\nТекущий BID: $0.00')
        (self.root/'config.json').write_text(json.dumps(self.cfg));self.write()
        (self.root/'state/bot_state.json').write_text(json.dumps({'instrument':self.inst,'cycles':{}}))
        self.mon=_offline_monitor();self.mon.BOT_DIRS={'dood':str(self.root)}
        self.fund=funding.FundingDisplay(D('-4.82931012'),funding.FundingValue(self.inst,
            int(self.cfg['created_at']*1000),D('-4.82931012'),None,int(self.now*1000),True),False,False)
    def write(self):self.file.write_text(json.dumps(self.stats))
    def manager(self, active=True):
        with ExitStack() as s:
            for k,v in {'find_entry':('dood',{}),'bot_path':self.root,'describe':self.inst,
                        'unit_name':'unused','instance_terminal':False,'service_running':active}.items():
                s.enter_context(patch.object(manager,k,return_value=v))
            s.enter_context(patch.object(ui,'peak_line',return_value='PEAK'))
            s.enter_context(patch('okx_funding_view.display_funding',return_value=self.fund))
            return manager.stats_text('dood')
    def full(self):
        totals=[]
        with patch.object(self.mon,'_status_funding_amount',return_value=self.fund):
            text=self.mon.mode_and_stats('dood',total_sink=totals)
        return text,totals
    def compact(self):
        with patch.object(self.mon,'_status_funding_amount',return_value=self.fund):
            return self.mon._compact_bot_row('dood',self.root,time.time())
    def test_golden_three_consumers_structured_no_marker(self):
        m=self.manager();f,totals=self.full();c=self.compact()
        self.assertIn('ИТОГОВЫЙ PnL: +$15.17068988',m)
        self.assertIn('Floating PnL: +$5.00000000',m)
        self.assertIn('Floating PnL: +$5.00',f)
        self.assertIn('Realized Open: +$3.00000000',m)
        self.assertIn('Realized Open: +$3.00',f)
        self.assertIn('Partial MARKET Exit: +$2.00',f)
        self.assertIn('Итоговый PnL: +$15.17',f)
        self.assertIn('$0.002304',f)
        self.assertIn('−10.70%',f)
        self.assertIn('Грид профит: +$10.00',f)
        self.assertIn('Открытая позиция: 621 DOOD',f)
        self.assertNotIn('999999',m+f)
        self.assertEqual(totals,[D('15.17068988')]);self.assertEqual(c['net'],totals[0])
        self.assertEqual(c['expr'],'10.00 −4.83 +10.00')
    def test_generation_race_one_read_manager_and_full(self):
        initial=copy.deepcopy(self.stats);next_gen=copy.deepcopy(initial)
        next_gen['performance']['total_pnl']='999999'  # deliberately inconsistent
        for consumer in (self.manager,self.full):
            with self.subTest(consumer=consumer.__name__):
                self.stats=copy.deepcopy(initial);self.write();reads=[]
                original=Path.read_text
                def interpose(path,*a,**kw):
                    raw=original(path,*a,**kw)
                    if path==self.file:
                        reads.append(path);self.file.write_text(json.dumps(next_gen))
                    return raw
                with patch.object(Path,'read_text',interpose):result=consumer()
                text=result[0] if isinstance(result,tuple) else result
                self.assertEqual(len(reads),1);self.assertNotIn('999999',text)
                self.assertIn('15.17',text)
    def test_invalid_current_finance_withheld_by_all(self):
        original=copy.deepcopy(self.stats)
        mutations=(lambda s:s['performance'].__setitem__('total_pnl','21'),
                   lambda s:s['performance'].__setitem__('open_nominal','99'),
                   lambda s:s['performance'].__setitem__('closed_count',True),
                   lambda s:s['performance'].__setitem__('capital',True),
                   lambda s:s['performance'].__setitem__('floating_pnl',None),
                   lambda s:s.__setitem__('schema_version',True),
                   lambda s:s.__setitem__('generated_at_unix',self.now+901),
                   lambda s:s.__setitem__('complete',1),
                   lambda s:s['financial_anchor'].__setitem__('at',self.now+0.1),
                   lambda s:s['financial_anchor'].__setitem__('total_pnl_usdt','21'),
                   lambda s:s.__setitem__('performance',{}),
                   lambda s:s.pop('performance'))
        for idx,change in enumerate(mutations):
            with self.subTest(mutation=idx):
                self.stats=copy.deepcopy(original);change(self.stats);self.write()
                self.assertIn('PnL: н/д',self.manager())
                text,totals=self.full();self.assertIn('PnL: н/д',text);self.assertEqual(totals,[])
                self.assertNotIn('999999',text)
                with self.assertRaises((ValueError,OSError)):self.compact()
    def test_stale_valid_finance_visible_in_stopped_manager_only(self):
        for age in (901,86400,604800):
            with self.subTest(age=age):
                stamp=self.now-age
                self.stats['generated_at_unix']=stamp
                self.stats['financial_anchor']['at']=stamp
                self.write()
                text=self.manager(active=False)
                self.assertIn('ИТОГОВЫЙ PnL: +$15.17068988',text)
                self.assertIn('Финансовая статистика устарела',text)
                self.assertIn('PnL: н/д',self.full()[0]);self.assertEqual(self.full()[1],[])
                with self.assertRaisesRegex(ValueError,'stale'):self.compact()
                # Age does not excuse a corrupt financial identity.
                self.stats['performance']['total_pnl']='999';self.write()
                self.assertIn('PnL: н/д',self.manager())
                self.stats['performance']['total_pnl']='20';self.write()

    def test_watcher_common_validation_and_independent_threshold(self):
        self.stats['stats_v2']={'mode':'single_authority_incremental'}
        self.stats['generated_at_unix']=self.now-1000
        self.stats['financial_anchor']['at']=self.now-1000;self.write()
        events=[];state={};roots={'dood':str(self.root)};services={'dood':'dummy'}
        with patch.object(self.mon,'systemd_active',return_value=True),patch.object(self.mon,'one',side_effect=lambda *a:events.append(a[2])):
            scan=self.mon.stats_freshness_check
            scan(None,state,services,roots,now=self.now)
            self.assertEqual(events,[])
            self.stats['performance']['total_pnl']='999';self.write()
            scan(None,state,services,roots,now=self.now+900)
            scan(None,state,services,roots,now=self.now+901)
            self.assertEqual(len(events),1);self.assertIn('stale',events[0])
            self.stats['performance']['total_pnl']='20'
            self.stats['generated_at_unix']=self.now+901
            self.stats['financial_anchor']['at']=self.now+901;self.write()
            scan(None,state,services,roots,now=self.now+902)
            self.assertEqual(len(events),2);self.assertIn('recovered',events[1])

    def test_legacy_anchor_only_when_matching_and_never_masks_bad_embedded(self):
        self.stats.pop('financial_anchor');self.write()
        legacy=self.root/'state/stats_financial_anchor.json';legacy.write_text(json.dumps(self.anchor))
        self.assertEqual(self.full()[1],[D('15.17068988')])
        self.stats['financial_anchor']=dict(self.anchor,at=self.now+0.1);self.write()
        self.assertIn('PnL: н/д',self.manager());self.assertEqual(self.full()[1],[])
    def test_malformed_symlink_and_missing_record_fail_closed_everywhere(self):
        raw=self.file.read_text()
        foreign=self.root/'foreign.json';foreign.write_text(raw)
        for kind in ('invalid_json','non_object','missing','symlink'):
            with self.subTest(kind=kind):
                self.file.unlink(missing_ok=True)
                if kind=='invalid_json':self.file.write_text('{')
                elif kind=='non_object':self.file.write_text('[]')
                elif kind=='symlink':self.file.symlink_to(foreign)
                self.assertIn('PnL: н/д',self.manager())
                self.assertIn('PnL: н/д',self.full()[0]);self.assertEqual(self.full()[1],[])
                self.assertIn('PnL: н/д',views.status_one('dood',self.root))
                with self.assertRaises((OSError,ValueError)):self.compact()
        self.assertEqual(foreign.read_text(),raw)
    def test_mixed_engine_rollout_never_downgrades_structured_producer(self):
        manifest=json.loads((Path(__file__).parents[1]/'engine_manifest.json').read_text())
        current=manifest['current_version']
        previous=max((v for v,e in manifest['engines'].items()
            if v!=current and 'okx_stats_v2_production.py' in e.get('dependencies',[])),
            key=lambda v:tuple(map(int,v.split('.'))))
        self.stats.pop('performance')
        self.stats.pop('stats_v2',None)
        for consumer,producer in ((previous,current),(current,previous)):
            with self.subTest(consumer=consumer,producer=producer), \
                 patch('okx_version.ENGINE_VERSION',consumer):
                self.cfg['version']=producer
                self.stats['bot_version']=producer
                (self.root/'config.json').write_text(json.dumps(self.cfg));self.write()
                self.assertTrue(ui.requires_structured_snapshot(self.cfg,self.stats))
                self.assertIn('PnL: н/д',self.manager())
                full,sink=self.full()
                self.assertIn('PnL: н/д',full)
                self.assertNotIn('$999999',full)
                self.assertEqual(sink,[])
                with self.assertRaises((OSError,ValueError)):self.compact()
        # Known pre-StatsWorker historical producers keep their legacy path.
        old=next(v for v,e in manifest['engines'].items()
            if 'okx_stats_v2_production.py' not in e.get('dependencies',[]))
        self.assertFalse(ui.requires_structured_snapshot({'version':old},{'bot_version':old}))
        with patch.object(ui,'__file__',str(self.root/'missing'/'okx_stats_ui.py')):
            self.assertTrue(ui.requires_structured_snapshot({'version':old},{}))

    def test_v2_missing_atomic_anchor_is_not_rescued_by_legacy(self):
        self.stats['stats_v2']={'generation':1}
        self.stats.pop('financial_anchor');self.write()
        (self.root/'state/stats_financial_anchor.json').write_text(json.dumps(self.anchor))
        self.assertIn('PnL: н/д',self.manager());self.assertEqual(self.full()[1],[])

    def test_drawdown_no_text_fallback(self):
        self.stats['report']+='\nМАКС. ПРОСАДКА БОТА: -$999999';self.write()
        self.assertIn('Макс. просадка: н/д',self.manager())
        self.assertIn('Максимальная просадка бота: н/д',self.full()[0])
        dd=dict(schema=1,instrument=self.inst,max_drawdown_usdt='4.5',max_drawdown_at=self.now)
        (self.root/'state/bot_drawdown.json').write_text(json.dumps(dd))
        self.assertIn('Макс. просадка: −$4.50000000',self.manager())
        self.assertIn('Максимальная просадка бота: −$4.50',self.full()[0])

class SharedRetirement(unittest.TestCase):
    def test_extra_pinned_retirement_backs_up_and_preserves_unknown(self):
        import hashlib
        import okx_live_upgrade as upgrade
        source=Path(__file__).resolve().parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        names=('BUNDLE_SHA256.json','run_checks.sh','okx_release_contract.py','okx_snapshot.py')
        self.assertTrue(set(names)<=inventory['retire_shared'].keys())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'shared';root.mkdir();backup=Path(tmp)/'backup';backup.mkdir()
            for n in names:
                old=('legacy fixture '+n).encode();(root/n).write_bytes(old)
                inventory['retire_shared'][n]=hashlib.sha256(old).hexdigest()
            (root/'okx_snapshot.py').write_bytes(b'unrecognized bytes')
            with patch.object(upgrade,'STAGE',source),patch.object(upgrade,'SHARED',root),\
                 patch.object(upgrade,'stage_check'),patch.object(upgrade,'backup_dir',return_value=backup),\
                 patch.object(upgrade,'run',side_effect=AssertionError('no systemd')),\
                 patch('okx_install_manifest.load',return_value=inventory):
                upgrade.install_shared()
            for n in names[:-1]:
                self.assertFalse((root/n).exists());self.assertEqual((backup/'old'/n).read_bytes(),('legacy fixture '+n).encode())
            self.assertEqual((root/'okx_snapshot.py').read_bytes(),b'unrecognized bytes')
            self.assertTrue((source/'okx_release_contract.py').is_file())
            self.assertTrue((source/'run_checks.sh').is_file())
    def test_retirement_manifest_never_accepts_installed_or_arbitrary_paths(self):
        import hashlib
        import shutil
        import okx_install_manifest as inv
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'candidate';shutil.copytree(source,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            raw=json.loads((root/'install_manifest.json').read_text());m=json.loads((root/'engine_manifest.json').read_text())
            self.assertEqual(inv.load(root,m),raw)
            for name in ('okx_bot.py','okx_source_bundle.json','../run_checks.sh','arbitrary.sh','/etc/passwd'):
                with self.subTest(name=name):
                    mutated=copy.deepcopy(raw);mutated['retire_shared'][name]='a'*64
                    payload=json.dumps(mutated).encode();(root/'install_manifest.json').write_bytes(payload)
                    new_manifest=dict(m,install_manifest_sha256=hashlib.sha256(payload).hexdigest())
                    with self.assertRaisesRegex(RuntimeError,'Invalid retired inventory'):inv.load(root,new_manifest)

class TelegramInstall(unittest.TestCase):
    def test_shadow_transaction_preflight_failure_no_restart(self):
        import shutil
        import okx_live_upgrade as upgrade
        source=Path(__file__).resolve().parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'telegram';root.mkdir();shared=Path(tmp)/'shared';shared.mkdir()
            backup=Path(tmp)/'backup';backup.mkdir()
            for name in inventory['shared']:shutil.copy2(source/name,shared/name)
            old={name:b'old '+name.encode() for name in inventory['telegram']}
            for name in inventory['retire_telegram_shadows']:
                old[name]=(source/name).read_bytes()
            for name,data in old.items():(root/name).write_bytes(data)
            with patch.object(upgrade,'STAGE',source),patch.object(upgrade,'SHARED',shared),patch.object(upgrade,'TELEGRAM_ROOT',root),patch.object(upgrade,'stage_check'),patch.object(upgrade,'backup_dir',return_value=backup),patch.object(upgrade,'run',side_effect=AssertionError('no restart')),patch('okx_install_manifest.load',return_value=inventory),patch.object(upgrade,'telegram_import_preflight',side_effect=RuntimeError('probe failed')):
                with self.assertRaisesRegex(RuntimeError,'probe failed'):upgrade.install_telegram()
            self.assertEqual({p.name:p.read_bytes() for p in root.iterdir()},old)
            self.assertFalse((backup/'complete').exists())
    def test_unknown_shadow_preserved_and_blocked(self):
        import okx_live_upgrade as upgrade
        source=Path(__file__).resolve().parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'okx_reader_diagnostics.py';p.write_bytes(b'unknown')
            with patch.object(upgrade,'TELEGRAM_ROOT',root):
                with self.assertRaisesRegex(RuntimeError,'Unexpected Telegram root shadow'):upgrade.telegram_shadow_plan(inventory)
            self.assertEqual(p.read_bytes(),b'unknown')
    def test_real_import_probe_isolated_roots(self):
        import shutil
        import okx_live_upgrade as upgrade
        source=Path(__file__).resolve().parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'telegram';root.mkdir();shared=Path(tmp)/'shared';shared.mkdir()
            for name in inventory['shared']:shutil.copy2(source/name,shared/name)
            for name in inventory['telegram']:shutil.copy2(source/name,root/name)
            # Only replace absolute LIVE bootstrap in this test fixture; all
            # remaining module imports and preflight use the production code.
            monitor=root/'okx_telegram_monitor.py'
            monitor.write_text(monitor.read_text().replace("_SHARED_CODE = Path('/opt/okx-grid-live')",f"_SHARED_CODE = Path({str(shared)!r})"))
            with patch.object(upgrade,'SHARED',shared),patch.object(upgrade,'TELEGRAM_ROOT',root):
                upgrade.telegram_import_preflight(inventory)
                (root/'okx_stats_ui.py').write_text('raise AssertionError("shadow executed")')
                import subprocess
                with self.assertRaises(subprocess.CalledProcessError) as err:upgrade.telegram_import_preflight(inventory)
                self.assertIn('Telegram import resolution mismatch: okx_stats_ui',err.exception.stderr)

    def test_shadow_retirement_success_before_restart(self):
        import shutil
        import okx_live_upgrade as upgrade
        from types import SimpleNamespace
        source=Path(__file__).resolve().parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'telegram';root.mkdir();shared=Path(tmp)/'shared';shared.mkdir()
            backup=Path(tmp)/'backup';backup.mkdir()
            for name in inventory['shared']:shutil.copy2(source/name,shared/name)
            for name in inventory['retire_telegram_shadows']:shutil.copy2(source/name,root/name)
            ops=[]
            def probe(_):
                self.assertTrue(all(not (root/n).exists() for n in inventory['retire_telegram_shadows']))
                self.assertTrue(all((root/n).read_bytes()==(source/n).read_bytes() for n in inventory['telegram']))
                ops.append('probe')
            def run(*cmd,**kw):
                ops.append(cmd[:2]);return SimpleNamespace(stdout='active')
            with patch.object(upgrade,'STAGE',source),patch.object(upgrade,'SHARED',shared),patch.object(upgrade,'TELEGRAM_ROOT',root),patch.object(upgrade,'stage_check'),patch.object(upgrade,'backup_dir',return_value=backup),patch.object(upgrade,'run',side_effect=run),patch('okx_install_manifest.load',return_value=inventory),patch.object(upgrade,'telegram_import_preflight',side_effect=probe):upgrade.install_telegram()
            self.assertEqual(ops,['probe',('systemctl','restart'),('systemctl','show')])
            for n in inventory['retire_telegram_shadows']:self.assertEqual((backup/'old'/n).read_bytes(),(source/n).read_bytes())

if __name__=='__main__':unittest.main()
