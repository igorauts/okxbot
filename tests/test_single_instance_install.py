"""Offline installer transaction regression, all service and exchange work mocked."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack, redirect_stdout
import io
import okx_live_upgrade as U

class SingleInstance(unittest.TestCase):
    def exercise(self, success=True, drift=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            p=Path(tmp); root=p/'bot';stage=p/'stage';backup=p/'backup'
            for d in (root/'state', stage, backup):d.mkdir(parents=True)
            for f in ('okx_bot.py','okx_version.py'):
                (root/f).write_text('old '+f);(stage/f).write_text('new '+f)
            (root/'config.json').write_text(json.dumps({'version':'old'}))
            state=root/'state/bot_state.json';state.write_text(json.dumps({'version':'old','cycles':{}}))
            hb=root/'state/runtime_stats.json';hb.write_text('{"last":0}')
            commands=[];ops=[];active=True
            def unit(bot,need_active=True):
                return root,'unit',{'ActiveState':'active' if active else 'inactive','MainPID':'20' if active else '0'}
            def run(*args,**kw):
                nonlocal active
                commands.append(args)
                if args[:2]==('systemctl','stop'):active=False
                elif args[:2]==('systemctl','start'):
                    active=True;hb.write_text('{"last":1000}')
                else:raise AssertionError(args)
            def check(*args,**kw):
                if drift:raise RuntimeError('dependency drift')
            def audit(*args,**kw):
                self.assertFalse(active)
                self.assertEqual(kw['env']['OKX_GRID_AUDIT'],'READ_ONLY')
                self.assertEqual(kw['env']['OKX_GRID_ISOLATION_AUDIT'],'1')
                self.assertNotIn('OKX_GRID_LIVE',kw['env'])
                x=json.loads(state.read_text());x['confirmed_fact']=True;state.write_text(json.dumps(x))
                return SimpleNamespace(returncode=0,stdout=('AUDIT ISOLATED COVERAGE PASS\nTP links resolved: False\n✅ Все закрытые циклы сопоставлены.\nUPGRADE AUDIT COMPLETE ISOLATED\n' if success else 'AUDIT ISOLATED COVERAGE PASS\n'))
            def prop(bot,need_active=True):
                r,u,k=unit(bot,need_active)
                if active and any(c[:2]==('systemctl','start') for c in commands):k['MainPID']='21'
                return r,u,k
            def copy(src,dst):ops.append(str(dst));Path(dst).write_bytes(Path(src).read_bytes())
            for key,value in {'STAGE':stage,'OLD_VERSION':'old','NEW_VERSION':'new','SHARED':p/'DO_NOT_TOUCH','stage_check':lambda:None,'files_required':lambda:['okx_bot.py','okx_version.py','other.py'],'instance_check':lambda *a,**kw:prop(a[0]),'unit_check':prop,'stop_safety_readonly':lambda *a:True,'verify_single_instance_sources':check,'backup_dir':lambda _:backup,'run':run,'atomic_replace_file':copy}.items():
                stack.enter_context(patch.object(U,key,value))
            stack.enter_context(patch.object(U.subprocess,'run',side_effect=audit))
            stack.enter_context(patch.object(U.time,'sleep',return_value=None))
            stack.enter_context(patch.object(U.time,'time',return_value=1000))
            with redirect_stdout(io.StringIO()):
                if success and not drift:U.upgrade_bot('bot',single_instance=True)
                else:
                    with self.assertRaises(RuntimeError):U.upgrade_bot('bot',single_instance=True)
            if drift:
                self.assertEqual(commands,[]);self.assertEqual(ops,[]);return
            self.assertEqual(commands,[('systemctl','stop','unit')]+([('systemctl','start','unit')] if success else []))
            self.assertEqual(set(ops),{str(root/'okx_bot.py'),str(root/'okx_version.py')})
            self.assertTrue(json.loads(state.read_text())['confirmed_fact'])
            self.assertEqual(json.loads(state.read_text())['version'],'new')
            self.assertEqual(json.loads((backup/'state/bot_state.json').read_text())['version'],'old')
            self.assertFalse((p/'DO_NOT_TOUCH').exists())
    def test_success(self):self.exercise()
    def test_failed_audit_no_start_or_stale_rollback(self):self.exercise(success=False)
    def test_drift_before_stop(self):self.exercise(drift=True)

if __name__=='__main__':unittest.main()
