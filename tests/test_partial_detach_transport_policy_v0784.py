"""Real API wrapper with mocked transport; no external IO."""
import unittest
from tests import test_stats_light_contract as transport
from okx_live_upgrade import migrate_partial_policy, stop_safety_readonly

class AmendmentAPI(unittest.TestCase):
    def setUp(self):
        h=transport.IdleGetConnectionTest();h.setUp();self.api=h.obj;self.gate=h.gate
        self.calls=[];self.body={'ordId':'parent','reqId':'request'}
    def run_reply(self, reply):
        def fake(method,path,payload,headers):
            self.calls.append((method,path,payload))
            if isinstance(reply,BaseException):raise reply
            return reply
        self.api._http=fake
        return self.api.req('POST','/api/v5/trade/amend-order',body=self.body)
    def test_definite_rejection(self):
        row={'ordId':'parent','reqId':'request','sCode':'51500'}
        self.assertEqual(self.run_reply((200,{'code':'1','data':[row]})),[row])
        self.assertEqual(len(self.calls),1)
    def test_mismatched_rejection(self):
        with self.assertRaisesRegex(RuntimeError,'identity mismatch'):
            self.run_reply((200,{'code':'1','data':[{'ordId':'parent','reqId':'other','sCode':'51500'}]}))
        self.assertEqual(len(self.calls),1)
    def test_timeout_no_retry(self):
        with self.assertRaisesRegex(RuntimeError,'UNKNOWN POST OUTCOME'):
            self.run_reply(TimeoutError())
        self.assertEqual(len(self.calls),1)
    def test_rate_limit_no_retry(self):
        with self.assertRaisesRegex(RuntimeError,'Uncertain POST'):
            self.run_reply((429,{'code':'50011','data':[]}))
        self.assertEqual(len(self.calls),1)
    def test_ack_passed_through(self):
        row={'ordId':'parent','reqId':'request','sCode':'0'}
        self.assertEqual(self.run_reply((200,{'code':'0','data':[row]})),[row])
        self.assertEqual(len(self.calls),1)

class PolicyMigration(unittest.TestCase):
    def test_existing_policy_migrates(self):
        c={'partial_buy_timeout_sec':60,'partial_buy_distance_steps':5,'target':1}
        migrate_partial_policy(c)
        self.assertEqual(c,{'partial_buy_timeout_sec':5,'partial_buy_distance_steps':3,'partial_buy_policy_revision':2,'target':1})
    def test_future_upgrade_preserves_explicit_revision2(self):
        c={'partial_buy_timeout_sec':7,'partial_buy_distance_steps':4,'partial_buy_policy_revision':2}
        before=dict(c);migrate_partial_policy(c);self.assertEqual(c,before)
    def test_unknown_revision_fails_closed(self):
        c={'partial_buy_policy_revision':3};before=dict(c)
        with self.assertRaises(RuntimeError):migrate_partial_policy(c)
        self.assertEqual(c,before)

class UpgradeSafety(unittest.TestCase):
    def test_open_unconfirmed_detachment_blocks_upgrade_before_io(self):
        with self.assertRaisesRegex(RuntimeError,'Unresolved partial TP amendment'):
            stop_safety_readonly(None,{'cycles':{'c':{'state':'BUY_PARTIAL','partialDetach':{'phase':'REQUESTED'}}}})


class AuditAlerts(unittest.TestCase):
    def test_three_critical_events_enqueued_and_deduplicated(self):
        from unittest.mock import patch
        import okx_telegram_events as e
        st={};out=[]
        with patch.object(e,'enqueue',side_effect=lambda st,key,msg,html_mode=False: out.append((key,msg)) or True):
            for typ in ('PARTIAL_DETACH_UNRESOLVED','PARTIAL_DETACH_REJECTED','PARTIAL_DETACH_TERMINAL_UNRESOLVED'):
                obj={'event':typ,'ordId':'parent','severity':'CRITICAL'}
                e.event_alert(None,st,'test-bot',obj)
                e.event_alert(None,st,'test-bot',obj)
        self.assertEqual(len(out),3)
        self.assertTrue(all('parent' in msg for _,msg in out))


class ResumeGuard(unittest.TestCase):
    def test_window_and_exact_durable_identity(self):
        import time, tempfile
        from pathlib import Path
        from unittest.mock import patch
        import okx_live_upgrade as u
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'.env').write_text('OKX_DEMO=0\n');(root/'.env').chmod(0o600)
            c={'state':'BUY_PARTIAL','ordId':'parent','clOrdId':'buy','tpClOrdId':'tp','tpOrders':[],
               'sz':'4','filledBuy':'2'}
            c['partialDetach']={'phase':'REQUESTED','ordId':'parent','clOrdId':'buy','tpClOrdId':'tp',
                'reqId':'amend','attachAlgoId':'algo','beforeUTime':'100','beforeFill':'2','requestedAt':time.time()-20}
            st={'cycles':{'c':c}}
            with patch.object(u,'read_gate_snapshot',return_value=None):
                self.assertTrue(u.detach_resume_required(st))
                self.assertTrue(u.stop_safety_readonly(root,st,allow_detach_resume=True))
                with self.assertRaisesRegex(RuntimeError,'Unresolved partial TP amendment'):
                    u.stop_safety_readonly(root,st)
                c['partialDetach']['requestedAt']=time.time()-1
                with self.assertRaisesRegex(RuntimeError,'diagnostic window'):
                    u.stop_safety_readonly(root,st,allow_detach_resume=True)
                c['partialDetach']['requestedAt']=time.time()-20;c['partialDetach']['ordId']='wrong'
                with self.assertRaisesRegex(RuntimeError,'not durable'):
                    u.stop_safety_readonly(root,st,allow_detach_resume=True)
    def test_policy_logs_custom_values_without_changing_approved_migration(self):
        from contextlib import redirect_stdout
        import io
        c={'partial_buy_timeout_sec':300,'partial_buy_distance_steps':10}
        with redirect_stdout(io.StringIO()) as log:migrate_partial_policy(c)
        self.assertIn('300 -> 5',log.getvalue());self.assertIn('10 -> 3',log.getvalue())
        self.assertEqual((c['partial_buy_timeout_sec'],c['partial_buy_distance_steps']),(5,3))


class ResumeTransaction(unittest.TestCase):
    def exercise(self, compatible=True, success=True):
        import json, shutil, tempfile, time, io
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from contextlib import ExitStack, redirect_stdout
        import okx_live_upgrade as u
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            p=Path(td);root=p/'bot';stage=p/'stage';shared=p/'shared';backup=p/'backup'
            for d in (root/'state',stage,shared,backup):d.mkdir(parents=True)
            files=['okx_bot.py','okx_version.py']
            for name in files:
                (root/name).write_text('old '+name)
                (stage/name).write_text('new '+name)
                shutil.copy2(stage/name,shared/name)
            manifest={'current_version':'new','engines':{'new':{'partial_detach_resume_schema':1} if compatible else {}}}
            for d in (stage,shared):(d/'engine_manifest.json').write_text(json.dumps(manifest))
            (root/'config.json').write_text(json.dumps({'version':'old','partial_buy_policy_revision':2}))
            (root/'.env').write_text('OKX_DEMO=0\n');(root/'.env').chmod(0o600)
            c={'state':'BUY_PARTIAL','ordId':'parent','clOrdId':'buy','tpClOrdId':'tp','tpOrders':[],
               'sz':'4','filledBuy':'2'}
            c['partialDetach']={'phase':'REQUESTED','ordId':'parent','clOrdId':'buy','tpClOrdId':'tp',
                'reqId':'amend','attachAlgoId':'algo','beforeUTime':'100','beforeFill':'2','requestedAt':980}
            state=root/'state/bot_state.json';state.write_text(json.dumps({'version':'old','cycles':{'c':c}}))
            hb=root/'state/runtime_stats.json';hb.write_text('{"last":0}')
            active=True;commands=[]
            def unit(bot,need_active=True):
                return root,'unit',{'ActiveState':'active' if active else 'inactive',
                    'MainPID':('21' if any(x[:2]==('systemctl','start') for x in commands) else '20') if active else '0'}
            def run(*args,**kw):
                nonlocal active
                commands.append(args)
                if args[:2]==('systemctl','stop'):active=False
                elif args[:2]==('systemctl','start'):active=True;hb.write_text('{"last":1000}')
                else:raise AssertionError(args)
            def audit(*args,**kw):
                self.assertFalse(active)
                self.assertEqual(kw['env']['OKX_GRID_AUDIT'],'READ_ONLY')
                self.assertEqual(kw['env']['OKX_GRID_ISOLATION_AUDIT'],'1')
                self.assertEqual(kw['env']['OKX_GRID_DETACH_RESUME_AUDIT'],'1')
                self.assertNotIn('OKX_GRID_LIVE',kw['env'])
                st=json.loads(state.read_text());self.assertEqual(st['cycles']['c']['partialDetach'],c['partialDetach'])
                st['confirmed_fact']=True;state.write_text(json.dumps(st))
                return SimpleNamespace(returncode=0,stdout=('AUDIT ISOLATED COVERAGE PASS\nTP links resolved: False\n✅ Все закрытые циклы сопоставлены.\nUPGRADE AUDIT COMPLETE ISOLATED\n' if success else 'AUDIT ISOLATED COVERAGE PASS\n'))
            for name,value in {'STAGE':stage,'SHARED':shared,'OLD_VERSION':'old','NEW_VERSION':'new',
                'stage_check':lambda:None,'files_required':lambda:files,'instance_check':lambda *a,**kw:unit(a[0]),
                'unit_check':unit,'backup_dir':lambda _:backup,'run':run,'read_gate_snapshot':lambda *a,**kw:None}.items():
                stack.enter_context(patch.object(u,name,value))
            stack.enter_context(patch.object(u.subprocess,'run',side_effect=audit))
            stack.enter_context(patch.object(u.time,'sleep',return_value=None));stack.enter_context(patch.object(u.time,'time',return_value=1000))
            with redirect_stdout(io.StringIO()):
                if compatible and success:u.upgrade_bot('bot')
                else:
                    with self.assertRaises(RuntimeError):u.upgrade_bot('bot')
            if not compatible:
                self.assertEqual(commands,[]);self.assertEqual(json.loads(state.read_text())['version'],'old');return
            self.assertEqual(commands,[('systemctl','stop','unit')]+([('systemctl','start','unit')] if success else []))
            self.assertTrue(json.loads(state.read_text())['confirmed_fact'])
            self.assertEqual(json.loads(state.read_text())['version'],'new')
            self.assertEqual(json.loads((backup/'state/bot_state.json').read_text())['version'],'old')
    def test_compatible_resume(self):self.exercise()
    def test_no_downgrade_to_incompatible_target(self):self.exercise(compatible=False)
    def test_failed_audit_retains_confirmed_facts_and_stops(self):self.exercise(success=False)
