"""Maintenance-specific regression contracts; all trading/systemd edges mocked."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import okx_live_upgrade as u
import okx_dependency_attestation as attestation
import okx_install_manifest as inv
from okx_manager_version import RELEASE_VERSION, RELEASE_KIND
from okx_version import ENGINE_VERSION
from tools.export_component import export

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'engine_manifest.json').read_text())
INVENTORY=json.loads((ROOT/'install_manifest.json').read_text())

def read(path):return json.loads(path.read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

class ManifestMaintenance(unittest.TestCase):
    def test_engine_pins_and_release_mode(self):
        self.assertIn(RELEASE_KIND,('maintenance','engine'))
        if RELEASE_KIND=='maintenance':self.assertNotEqual(RELEASE_VERSION,ENGINE_VERSION)
        else:self.assertEqual(RELEASE_VERSION,ENGINE_VERSION)
        self.assertEqual(INVENTORY['engine_version'],ENGINE_VERSION)
        self.assertEqual(INVENTORY['release_version'],RELEASE_VERSION)
        self.assertEqual(inv.load(ROOT,MANIFEST),INVENTORY)
        self.assertEqual(attestation.verify(ROOT,MANIFEST),len(MANIFEST['source_modules_sha256']))
        self.assertEqual(attestation.verify(ROOT,MANIFEST,bot_only=True),len(INVENTORY['engine']))
        # Python import closure of all installed modules must be in the explicit shared inventory.
        import ast
        shared=set(INVENTORY['shared']); package=set(MANIFEST['source_modules_sha256'])
        for filename in shared & package:
            for n in ast.walk(ast.parse((ROOT/filename).read_text())):
                mods=[]
                if isinstance(n,ast.Import):mods=[v.name for v in n.names]
                if isinstance(n,ast.ImportFrom) and n.module:mods=[n.module]
                for mod in mods:
                    candidate=mod.split('.')[0]+'.py'
                    if candidate in package:self.assertIn(candidate,shared,(filename,candidate))
        self.assertIn('okx_per_bot_speed_readonly.py',shared)
        self.assertIn('okx_telegram_account_balance_getonly.py',INVENTORY['telegram'])
        self.assertNotIn('okx_shared_runtime_deploy.py',shared)
        self.assertNotIn('okx_release_contract.py',shared)
        self.assertNotIn('okx_setup_presets.py',shared)

    def test_shared_install_does_not_copy_source_only_and_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'shared';root.mkdir();backup=Path(td)/'backup';backup.mkdir()
            (root/'operator_custom.py').write_text('CUSTOM')
            retired='okx_shared_runtime_deploy.py'
            (root/retired).write_text('CUSTOMIZED OLD SCRIPT')
            # Existing byte-identical pinned engine must not be replaced.
            shutil.copy2(ROOT/'okx_bot.py',root/'okx_bot.py')
            ino=(root/'okx_bot.py').stat().st_ino
            with patch.object(u,'STAGE',ROOT),patch.object(u,'SHARED',root),patch.object(u,'stage_check'),patch.object(u,'backup_dir',return_value=backup),patch.object(u,'run',side_effect=AssertionError('systemd forbidden')):
                u.install_shared()
            self.assertEqual((root/'operator_custom.py').read_text(),'CUSTOM')
            self.assertEqual((root/retired).read_text(),'CUSTOMIZED OLD SCRIPT')
            self.assertEqual((root/'okx_bot.py').stat().st_ino,ino)
            for n in INVENTORY['source_only']:self.assertFalse((root/n).exists())
            self.assertEqual(attestation.verify(root,MANIFEST,installed_shared=True),sum(n.endswith('.py') for n in INVENTORY['shared']))
            with self.assertRaisesRegex(RuntimeError,'inventory mismatch'):attestation.verify(root,MANIFEST)
            (root/'okxbot.py').write_text('tampered')
            with self.assertRaisesRegex(RuntimeError,'Package module SHA mismatch'):attestation.verify(root,MANIFEST,installed_shared=True)

    def test_known_retired_file_backup_and_rollback(self):
        # Exercise actual hash-matching removal with a temporary, re-pinned fixture
        # (original legacy CLI source is no longer shipped in active paths).
        for fail_after in (False,True):
            with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as s:
                root=Path(td)/'shared';root.mkdir();backup=Path(td)/'backup';backup.mkdir()
                retired='okx_shared_runtime_deploy.py';old=b'fixture exact retired bytes'
                (root/retired).write_bytes(old)
                (root/'okxbot.py').write_bytes(b'old manager')
                inventory=dict(INVENTORY,retire_shared={retired:hashlib.sha256(old).hexdigest()})
                for k,v in dict(STAGE=ROOT,SHARED=root,stage_check=lambda:None,backup_dir=lambda _:backup,run=lambda *a,**k:self.fail('systemd forbidden')).items():s.enter_context(patch.object(u,k,v))
                original=inv.load
                def load(path,*a,**kw):return inventory if Path(path)==ROOT else original(path,*a,**kw)
                s.enter_context(patch.object(inv,'load',side_effect=load))
                if fail_after:
                    s.enter_context(patch('okx_source_attestation.verify_bundle',side_effect=RuntimeError('postcopy failure')))
                    with self.assertRaisesRegex(RuntimeError,'postcopy failure'):u.install_shared()
                    self.assertEqual((root/retired).read_bytes(),old)
                    self.assertEqual((root/'okxbot.py').read_bytes(),b'old manager')
                else:
                    u.install_shared();self.assertFalse((root/retired).exists())
                self.assertEqual((backup/'old'/retired).read_bytes(),old)

    def test_exports_exact_canonical_sources(self):
        with tempfile.TemporaryDirectory() as td:
            for component in ('funding','reader'):
                out=Path(td)/component
                export(component,out)
                self.assertEqual(set(x.name for x in out.iterdir()),set(INVENTORY[component]))
                for name,src in INVENTORY[component].items():self.assertEqual(sha(out/name),sha(ROOT/src))
                with self.assertRaisesRegex(RuntimeError,'must not exist'):export(component,out)
            self.assertFalse((ROOT/'funding/okx_funding_ledger.py').exists())
            self.assertFalse((ROOT/'funding/okx_funding_view.py').exists())
            self.assertFalse((ROOT/'deploy/reader/okx_position_reader_watchdog.py').exists())

    def test_maintenance_cli_only_nontrading_actions(self):
        base=['upgrader','--bot','testbot','--package','x.zip','--stage',str(ROOT),'--sha256','0'*64,'--from-engine',ENGINE_VERSION]
        for action,handler in [('preflight','verify_staging'),('shared','install_shared'),('telegram','install_telegram')]:
            with tempfile.TemporaryDirectory() as td,contextlib.ExitStack() as s:
                s.enter_context(patch('okx_manager_version.RELEASE_KIND','maintenance'))
                s.enter_context(patch('okx_manager_version.validate_manager_version',return_value=True))
                s.enter_context(patch.object(u.os,'geteuid',return_value=0))
                s.enter_context(patch.object(u,'LOCK',Path(td)/'lock'))
                s.enter_context(patch.object(u.sys,'argv',base+[action]))
                h=s.enter_context(patch.object(u,handler))
                s.enter_context(patch.object(u,'upgrade_bot',side_effect=AssertionError('trading forbidden')))
                u.main();h.assert_called_once_with()
        with contextlib.ExitStack() as s:
            s.enter_context(patch('okx_manager_version.RELEASE_KIND','maintenance'))
            s.enter_context(patch('okx_manager_version.validate_manager_version',return_value=True))
            s.enter_context(patch.object(u.os,'geteuid',return_value=0))
            s.enter_context(patch.object(u,'upgrade_bot',side_effect=AssertionError('trading forbidden')))
            with patch.object(u.sys,'argv',base+['bot','--confirm-live-stop']):
                with self.assertRaisesRegex(RuntimeError,'Maintenance release forbids'):u.main()
            earlier=next(v for v in MANIFEST['engines'] if v!=ENGINE_VERSION)
            other=list(base);other[-1]=earlier
            with patch.object(u.sys,'argv',other+['bot','--confirm-live-stop']):
                with self.assertRaisesRegex(RuntimeError,'Maintenance release forbids'):u.main()
            with patch.object(u.sys,'argv',other+['shared']):
                with self.assertRaisesRegex(RuntimeError,'identical source and target'):u.main()
            with patch('okx_manager_version.RELEASE_KIND','engine'),patch.object(u.sys,'argv',base+['shared']):
                with self.assertRaisesRegex(RuntimeError,'declared maintenance'):u.main()
        with patch.object(u,'OLD_VERSION',ENGINE_VERSION),patch.object(u,'NEW_VERSION',ENGINE_VERSION),patch.object(u,'stage_check',side_effect=AssertionError('must fail before IO')):
            with self.assertRaisesRegex(RuntimeError,'Same-engine'):u.upgrade_bot('testbot')

class AuditTransaction(unittest.TestCase):
    def exercise(self,kind):
        with tempfile.TemporaryDirectory() as td,contextlib.ExitStack() as s:
            p=Path(td);root=p/'bot';stage=p/'stage';shared=p/'shared';backup=p/'backup'
            for d in (root/'state',stage,shared,backup):d.mkdir(parents=True)
            names=['okx_bot.py','okx_version.py']
            for n in names:
                (root/n).write_text('old '+n);(stage/n).write_text('new '+n);shutil.copy2(stage/n,shared/n)
            for d in (stage,shared):(d/'engine_manifest.json').write_text(json.dumps({'current_version':'new'}))
            cfg=root/'config.json';cfg.write_text('{"version":"old","partial_buy_policy_revision":2}')
            state=root/'state/bot_state.json';state.write_text('{"version":"old","cycles":{}}')
            hb=root/'state/runtime_stats.json';hb.write_text('{"last":0}')
            fills=root/'state/fills.sqlite';fills.write_text('OLD')
            archive=root/'state/cycles.sqlite';archive.write_text('OLD')
            commands=[];active=True;audited=False
            def unit(*a,**kw):return root,'unit',{'ActiveState':'active' if active else 'inactive','MainPID':('21' if audited else '20') if active else '0'}
            def instance(bot,desired,**kw):
                if kind=='before_audit' and desired=='new':raise RuntimeError('preaudit injected')
                return unit()
            def run(*args,**kw):
                nonlocal active
                commands.append(args)
                if args[:2]==('systemctl','stop'):active=False
                elif args[:2]==('systemctl','start'):
                    if kind=='start_failure':raise RuntimeError('start injected')
                    active=True;hb.write_text('{"last":1000}')
                else:raise AssertionError(args)
            def audit(*args,**kw):
                nonlocal audited
                audited=True
                self.assertFalse(active);self.assertEqual(kw['env']['OKX_GRID_AUDIT'],'READ_ONLY')
                self.assertNotIn('OKX_GRID_LIVE',kw['env']);self.assertNotIn('OKX_GRID_ISOLATION_AUDIT',kw['env'])
                d=read(state);d['confirmed_fact']=True;state.write_text(json.dumps(d));fills.write_text('NEW');archive.write_text('NEW')
                if kind=='timeout':raise subprocess.TimeoutExpired(args[0],180,output=b'FULL TIMEOUT DIAGNOSTIC')
                out='AUDIT CONSISTENT\nOLD FUNDING-LESS REPORT\n✅ Все закрытые циклы сопоставлены.\n'
                if kind=='audit_failure':out='FULL FAILURE DIAGNOSTIC\nAUDIT CONSISTENT\n'
                return SimpleNamespace(returncode=0,stdout=out)
            for k,v in dict(STAGE=stage,SHARED=shared,OLD_VERSION='old',NEW_VERSION='new',stage_check=lambda:None,files_required=lambda:names,instance_check=instance,unit_check=unit,stop_safety_readonly=lambda *a,**kw:True,backup_dir=lambda _:backup,run=run).items():s.enter_context(patch.object(u,k,v))
            s.enter_context(patch.object(u.subprocess,'run',side_effect=audit));s.enter_context(patch.object(u.time,'sleep'));s.enter_context(patch.object(u.time,'time',return_value=1000))
            capture=io.StringIO()
            with contextlib.redirect_stdout(capture):
                if kind=='success':u.upgrade_bot('bot')
                else:
                    with self.assertRaises((RuntimeError,subprocess.TimeoutExpired)):u.upgrade_bot('bot')
            out=capture.getvalue()
            self.assertEqual(commands,[('systemctl','stop','unit')]+([('systemctl','start','unit')] if kind in ('success','start_failure') else []))
            if kind=='before_audit':
                self.assertFalse(audited);self.assertEqual(read(state)['version'],'old');self.assertEqual(read(cfg)['version'],'old')
                self.assertEqual(fills.read_text(),'OLD');self.assertEqual(archive.read_text(),'OLD');self.assertEqual((root/'okx_bot.py').read_text(),'old okx_bot.py')
            else:
                self.assertTrue(read(state)['confirmed_fact']);self.assertEqual(read(state)['version'],'new');self.assertEqual(read(cfg)['version'],'new')
                self.assertEqual(fills.read_text(),'NEW');self.assertEqual(archive.read_text(),'NEW');self.assertEqual((root/'okx_bot.py').read_text(),'new okx_bot.py')
                self.assertEqual(read(backup/'state/bot_state.json')['version'],'old')
            if kind=='success':
                self.assertNotIn('OLD FUNDING-LESS REPORT',out);self.assertIn('FINANCIAL AUDIT PASS',out);self.assertIn('POSITION COVERAGE PASS STRICT',out)
            if kind=='audit_failure':self.assertIn('FULL FAILURE DIAGNOSTIC',out)
            if kind=='timeout':self.assertIn('FULL TIMEOUT DIAGNOSTIC',out)
    def test_before_audit_rollback(self):self.exercise('before_audit')
    def test_ordinary_audit_failure_keeps_all_facts(self):self.exercise('audit_failure')
    def test_ordinary_timeout_keeps_all_facts(self):self.exercise('timeout')
    def test_start_failure_no_rollback(self):self.exercise('start_failure')
    def test_success_prints_only_audit_markers(self):self.exercise('success')

if __name__=='__main__':unittest.main()
