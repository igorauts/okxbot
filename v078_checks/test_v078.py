import ast, importlib.util, json, os, sys, tempfile, time, types, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import okx_execution_observer078 as observer
import okx_monitor078 as ctl
import okx_monitor_compat078 as compat

def import_name(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for d in ('state','logs'):(self.root/d).mkdir()
        (self.root/'config.json').write_text(json.dumps({'instrument':'SOL-USDT-SWAP','active_buy_orders':5,'real_tp_target':5,'grid_step':'0.02'}))
        self.write_state(4)
    def tearDown(self):self.tmp.cleanup()
    def write_state(self,count):
        cycles={f'{i}':{'state':'BUY_LIVE','buyPx':str(100-i*.02),'tpOrders':[]} for i in range(count)}
        (self.root/'state/bot04_state.json').write_text(json.dumps({'version':'0.7.7','instrument':'SOL-USDT-SWAP','cycles':cycles}))

class MonitorTests(Base):
    def test_targets_and_backward_compatibility(self):
        o=observer.Observer(self.root,'okx-grid-sol077','execution')
        self.assertEqual((o.target_buy,o.target_tp),(5,5))
        self.assertIn('v078',str(o.out))
        self.assertEqual(ctl.monitor_unit('okx-grid-sol077','burst'),'observer-okx-grid-sol077-burst-v078.service')
        unit=ctl.unit_text(ROOT,self.root,'okx-grid-sol077','burst')
        self.assertIn('UMask=0077',unit)
        self.assertIn('IPAddressDeny=any',unit)
        self.assertIn('NoNewPrivileges=true',unit)
        self.assertNotIn('OKX_BOT04_LIVE',unit)
    def test_real_journal_episode_instead_of_delayed_state(self):
        o=observer.Observer(self.root,'okx-grid-sol077','execution')
        origin=datetime.now(timezone.utc)-timedelta(seconds=2)
        def msg(offset,buy):
            ts=(origin+timedelta(seconds=offset)).isoformat(timespec='microseconds')
            return f'{ts} vultr python[42]: 📊 РЕЖИМ=RUN | BID=100.5 | BUY {buy}/5 | TP 5 (реальных 5)'
        o.read_bid(msg(0,5))
        o.snapshot()  # JSON has only four; must NOT start deficit
        self.assertEqual(o.engine_below_periods,0)
        self.assertIsNone(o.engine_episode_first_ts)
        self.assertGreater(o.state_engine_disagreements,0)
        o.read_bid(msg(.3,4));o.read_bid(msg(.9,5))
        self.assertEqual(o.engine_below_periods,1)
        self.assertAlmostEqual(o.engine_below_total_sec,.6,delta=.02)
        o.summary()
        doc=json.loads((o.out/'summary.json').read_text())
        self.assertEqual(doc['engine_count_source'],'engine_journal')
        self.assertEqual(doc['last_state']['engine_buy'],5)
    def test_true_engine_shortage_not_masked(self):
        self.write_state(5)
        o=observer.Observer(self.root,'okx-grid-sol077','burst')
        now=datetime.now(timezone.utc)-timedelta(seconds=2)
        for delta,n in ((0,3),(.5,4),(1.5,5)):
            t=(now+timedelta(seconds=delta)).isoformat()
            o.read_bid(f'{t} vultr python[42]: 📊 РЕЖИМ=RUN | BID=100.0 | BUY {n}/5 | TP 5 (реальных 5)')
        self.assertAlmostEqual(o.engine_below_total_sec,1.5,delta=.05)
    def test_legacy_partial_event_is_unverified(self):
        o=observer.Observer(self.root,'okx-grid-sol077','execution')
        o.event({'event':'PARTIAL_ATTACHED_TP_DISCOVERED','ts':datetime.now(timezone.utc).isoformat(),'price':'100.0'})
        self.assertEqual(o.counters['ATTACHED_TP_LEGACY_UNVERIFIED'],1)
        self.assertEqual(o.counters['PARTIAL_ATTACHED_TP_DISCOVERED'],0)
    def test_nonblocking_buffer_dropped_if_full(self):
        o=observer.Observer(self.root,'okx-grid-sol077','burst')
        o.timeline_buffer=[{}]*1024
        o.event({'ts':datetime.now(timezone.utc).isoformat(),'event':'BUY_PLACED','price':'100','ordId':'1'})
        self.assertEqual(o.dropped_timeline,1)
    def test_version_078_accepted(self):
        d=json.loads((self.root/'state/bot04_state.json').read_text());d['version']='0.7.8'
        (self.root/'state/bot04_state.json').write_text(json.dumps(d))
        self.assertIsInstance(observer.Observer(self.root,'okx-grid-sol078','burst'),observer.Observer)
    def test_no_legacy_observer_collision_with_trade_engine(self):
        self.assertFalse(ctl.monitor_unit('okx-grid-sol077','execution').startswith('okx'))

class EngineTests(unittest.TestCase):
    def test_trade_decisions_ast_equal_to_v077(self):
        a=ast.parse((ROOT/'v078_checks/baseline/okx_bot077.py').read_text())
        b=ast.parse((ROOT/'okx_bot078.py').read_text())
        def defs(t):return {i.name:i for i in t.body if isinstance(i,(ast.FunctionDef,ast.ClassDef))}
        old,new=defs(a),defs(b)
        self.assertEqual(set(old),set(new))
        for key in old:
            if key in ('ensure_state','register_cancel_attached'):continue
            self.assertEqual(ast.dump(old[key],include_attributes=False),ast.dump(new[key],include_attributes=False),key)
    def test_partial_label_requires_confirmed_fraction(self):
        t=(ROOT/'okx_bot078.py').read_text()
        self.assertIn("D('0') < observed_buy < D(c['sz'])",t)
        self.assertIn("else 'ATTACHED_TP_DISCOVERED'",t)
        self.assertIn("VER = '0.7.8'",t)
        self.assertIn("('0.7.6','0.7.7')",t)
    def test_manifest_dependencies(self):
        data=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertEqual(data['engines']['0.7.8']['file'],'okx_bot078.py')
        for dep in data['engines']['0.7.8']['dependencies']:
            self.assertTrue((ROOT/dep).exists(),dep)
    def test_manager_newest_engine(self):
        sys.modules['okx_setup068']=types.ModuleType('okx_setup068')
        manager=import_name('mgr078',ROOT/'okxbot.py')
        with tempfile.TemporaryDirectory() as d:
            dir=Path(d)
            for v,n in [('0.7.7','okx_bot077.py'),('0.7.8','okx_bot078.py')]:
                (dir/n).write_text(f"VER = '{v}'\n")
            self.assertEqual(manager.latest_engine(dir),('okx_bot078.py','0.7.8'))
        txt=(ROOT/'okxbot.py').read_text()
        self.assertIn('ИСТОРИЧЕСКАЯ СТАТИСТИКА',txt)
        self.assertIn("final['report']",txt)

class TelegramTests(unittest.TestCase):
    def test_plain_status_active_only_named_historical(self):
        tg=import_name('tg078',ROOT/'okx_telegram_monitor.py')
        tg.BOT_DIRS.clear();tg.BOT_DIRS.update({'eth064':'/fake/eth064','eth077':'/fake/eth077'})
        with patch.object(tg,'refresh_bots',lambda **kwargs:None),patch.object(tg,'_active_service',lambda n:n=='eth077'),patch.object(tg,'mode_and_stats',lambda n:'DATA:'+n):
            text=tg.status_report()
            self.assertIn('DATA:eth077',text)
            self.assertNotIn('DATA:eth064',text)
            self.assertIn('DATA:eth064',tg.status_report('eth064'))
    def test_full_historical_named_report(self):
        tg=import_name('tg078b',ROOT/'okx_telegram_monitor.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state').mkdir()
            (root/'config.json').write_text(json.dumps({'instrument':'ETH-USDT-SWAP'}))
            (root/'state/stats.json').write_text(json.dumps({'instrument':'ETH-USDT-SWAP','complete':True,'report':'Грид профит: +$20.00'}))
            tg.BOT_DIRS.clear();tg.BOT_DIRS['eth064']=str(root)
            with patch.object(tg,'_terminal_report',lambda n:'🏁 ИТОГО +$14.00'):
                result=tg.mode_and_stats('eth064')
            self.assertIn('Грид профит: +$20.00',result)
            self.assertIn('ИТОГО +$14.00',result)
            self.assertLess(result.index('Грид профит'),result.index('ИТОГО'))

if __name__=='__main__':unittest.main()
