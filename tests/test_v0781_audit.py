"""Offline v0.7.80 audit regression. No actual exchange or systemd operations."""
from contextlib import ExitStack, closing, redirect_stdout
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io,json,sqlite3,tempfile,time,unittest
import okx_funding_view as funding
from okx_funding_ledger import SCHEMA,SCHEMA_SQL,creation_ms,apply_complete_windows,read_snapshot
import okx_telegram_views as views
from tests.test_v0729_maintenance import _offline_monitor
import okxbot as manager
import okx_live_upgrade as upgrade
from okx_source_attestation import verify_bundle,verify_instance
from okx_version import ENGINE_VERSION

ROOT=Path(__file__).resolve().parents[1]
INST='ENSO-USDT-SWAP'
T=1795600000000
H=3600000

class FundingPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'funding.sqlite'
        self.created=(T-1000)/1000
        funding._LAST_GOOD.clear();self.addCleanup(funding._LAST_GOOD.clear)
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript(SCHEMA_SQL);c.execute(f'PRAGMA user_version={SCHEMA}');c.commit()
    def put(self,amount='2.3456789',complete=1,synced=T,mark=None,created=None):
        created=self.created if created is None else created
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?)',(INST,creation_ms(created),amount,None,synced,complete))
            if mark is not None:c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(f'complete_at:{INST}:{creation_ms(created)}',str(mark)))
            c.commit()
    def get(self,now=T,created=None):return funding.display_funding(self.db,INST,self.created if created is None else created,now_ms=now)
    def test_missing_zero_is_not_verified_zero_and_strict_warning_boundary(self):
        born=creation_ms(self.created)
        x=self.get(born+8*H);self.assertEqual(x.amount,0);self.assertTrue(x.estimated);self.assertFalse(x.warning)
        self.assertTrue(self.get(born+8*H+1).warning)
        self.put('0');x=self.get();self.assertEqual(x.amount,0);self.assertFalse(x.estimated)
    def test_stale_keeps_exact_amount(self):
        self.put('-3.125');self.assertFalse(self.get(T+8*H).warning)
        x=self.get(T+8*H+1);self.assertTrue(x.warning);self.assertEqual(x.amount,D('-3.125'))
    def test_incomplete_refresh_cannot_reset_last_complete(self):
        self.put(complete=0,synced=T+9*H,mark=T)
        x=self.get(T+9*H);self.assertTrue(x.warning);self.assertEqual(x.amount,D('2.3456789'))
        self.put(complete=1,synced=T+9*H);self.assertFalse(self.get(T+9*H).warning)
    def test_read_failure_cache_identity_and_restart(self):
        self.put();self.assertEqual(self.get().amount,D('2.3456789'))
        with patch.object(funding,'load_funding',side_effect=sqlite3.OperationalError('busy')):
            x=self.get(T+H);self.assertEqual(x.amount,D('2.3456789'));self.assertTrue(x.fallback);self.assertFalse(x.warning)
            self.assertEqual(self.get(T+H,created=T/1000).amount,0)
            funding._LAST_GOOD.clear();self.assertEqual(self.get(T+H).amount,0)
    def test_future_and_invalid_snapshot_not_used(self):
        self.put(synced=T+1);self.assertEqual(self.get().amount,0)
        self.put('NaN');self.assertEqual(self.get().amount,0)
    def test_legacy_incomplete_without_watermark(self):
        self.put(complete=0,synced=T+9*H)
        self.assertTrue(self.get(T+9*H).warning)
    def test_collector_persists_complete_watermark(self):
        # Fresh DB, actual collector ledger transaction, then force a gap
        # by widening coverage into a disjoint interval; repeated partial
        # publications must not move the last successful full verification.
        p=self.db.with_name('collector.sqlite');inv={INST:self.created}
        apply_complete_windows(p,[(T-1000,T+1000,[])],inv,T+2000)
        a=read_snapshot(p,INST,self.created);self.assertEqual(a['last_complete_ms'],T+2000)
        apply_complete_windows(p,[(T+5000,T+6000,[])],inv,T+7000)
        a=read_snapshot(p,INST,self.created);self.assertFalse(a['complete']);self.assertEqual(a['last_complete_ms'],T+2000)
        apply_complete_windows(p,[(T+6000,T+9000,[])],inv,T+10000)
        self.assertEqual(read_snapshot(p,INST,self.created)['last_complete_ms'],T+2000)
        apply_complete_windows(p,[(T+1000,T+5000,[])],inv,T+11000)
        self.assertEqual(read_snapshot(p,INST,self.created)['last_complete_ms'],T+11000)
        apply_complete_windows(p,[(T+9000,T+10000,[])],{},T+12000)
        with closing(sqlite3.connect(p)) as c:self.assertEqual(c.execute("SELECT count(*) FROM meta WHERE key LIKE 'complete_at:%'").fetchone()[0],0)

class TelegramAudit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'state').mkdir();self.now=time.time()-1
        self.cfg={'instrument':INST,'created_at':self.now-10,'tick_size':'0.00001','max_grids':200,'contract_value':'1','contract_lot':'1'}
        self.anchor={'schema':1,'instrument':INST,'at':self.now,'bid':'0.403129','total_pnl_usdt':'20'}
        self.perf={'schema':1,'total_pnl':'20','grid_profit':'10','limit_tp_net':'7','market_tp_net':'3','partial_exit_net':'2','floating_pnl':'5','realized_open_components':'3','bid':'0.403129','open_base':'621','volume':'22806.3819','capital':'100'}
        self.stats={'complete':True,'instrument':INST,'generated_at_unix':self.now,'financial_anchor':self.anchor,'performance':self.perf,'report':'ИТОГОВЫЙ PnL: +$20.00 (+20.00% от 1x)\nОткрытая позиция: 621 ENSO'}
        (self.root/'config.json').write_text(json.dumps(self.cfg));self.write()
        (self.root/'state/bot_state.json').write_text(json.dumps({'instrument':INST,'cycles':{'x':{'state':'VIRTUAL_TP'}}}))
        (self.root/'state/stats_financial_anchor.json').write_text(json.dumps(dict(self.anchor,at=self.now-600,bid='999')))
        self.m=_offline_monitor()
    def write(self):(self.root/'state/stats.json').write_text(json.dumps(self.stats))
    def render(self,f):
        sink=[];out=views.status_one('enso',self.root,funding_loader=lambda cfg:f,total_sink=sink);return out,sink
    def test_atomic_anchor_wins_over_racing_legacy_file(self):
        out,sink=self.render(None);self.assertEqual(sink,[D(20)]);self.assertIn('Funding: +$0.00',out);self.assertIn('пока нет данных',out);self.assertIn('+$20.00',out)
        with patch.object(self.m,'_status_funding_amount',return_value=None):
            row=self.m._compact_bot_row('enso',self.root,time.time())
        self.assertEqual(row['net'],D(20));self.assertEqual(row['left'],'+20.00');self.assertEqual(row['expr'],'10.00 +0.00 +10.00');self.assertTrue(row['funding_estimated']);self.assertEqual(row['bid'],'0.40312')
    def test_funding_positive_negative_and_same_sum(self):
        for a in (D('2.345'),D('-5.129'),D(0)):
            x=funding.FundingDisplay(a,funding.FundingValue(INST,creation_ms(self.cfg['created_at']),a,None,int(self.now*1000),True),False,False)
            out,sink=self.render(x);self.assertEqual(sink,[D(20)+a])
            with patch.object(self.m,'_status_funding_amount',return_value=x):row=self.m._compact_bot_row('enso',self.root,time.time())
            self.assertEqual(row['net'],sink[0]);self.assertFalse(row['funding_estimated'])
    def test_missing_funding_account_total_still_numeric(self):
        with patch.object(self.m,'_status_funding_amount',return_value=None),patch.object(self.m,'refresh_bots'),patch.object(self.m,'_active_service',return_value=True),patch.object(self.m,'BOT_DIRS',{'enso':str(self.root)}),patch.object(self.m,'_verified_account_balances',return_value=None):
            result=self.m.compact_status_report()
        self.assertIn('TOTAL PnL: +$20.00',result);self.assertIn('Funding: оценка',result);self.assertNotIn('⚠️ Funding',result)
    def test_bad_embedded_anchor_never_falls_back(self):
        (self.root/'state/stats_financial_anchor.json').write_text(json.dumps(self.anchor))
        self.stats['financial_anchor']=dict(self.anchor,at=self.now+0.1);self.write()
        out,sink=self.render(None);self.assertIn('PnL: н/д',out);self.assertEqual(sink,[])
        with patch.object(self.m,'_status_funding_amount',return_value=None),self.assertRaises(ValueError):self.m._compact_bot_row('enso',self.root,time.time())
    def test_terminal_does_not_invent_final_funding(self):
        (self.root/'state/final_stats.json').write_text('{}')
        out,sink=self.render(None);self.assertEqual(sink,[None]);self.assertIn('Funding: н/д',out)
    def test_bad_trading_total_not_masked_by_funding(self):
        self.perf['total_pnl']='999';self.write();out,sink=self.render(None);self.assertEqual(sink,[None]);self.assertIn('Итоговый PnL: н/д',out)
    def test_legacy_anchor_accepted_only_if_matching(self):
        del self.stats['financial_anchor'];self.write();out,sink=self.render(None);self.assertIn('PnL: н/д',out)
        (self.root/'state/stats_financial_anchor.json').write_text(json.dumps(self.anchor));self.assertEqual(self.render(None)[1],[D(20)])
    def test_bid_truncation(self):
        for v,expected in [('0.000001234567','0.0000012345'),('123456.789','123,450'),('123.45678','123.45'),('0.999999','0.99999'),('1.0','1')]:
            self.assertEqual(self.m._precise_bid(D(v),{'tick_size':'0.0000000001'}),expected)

class BundleInstall(unittest.TestCase):
    def test_shared_install_replaces_bundle_and_requirements(self):
        with tempfile.TemporaryDirectory() as t:
            dest=Path(t)/'shared';dest.mkdir();backup=Path(t)/'backup';backup.mkdir()
            (dest/'okx_source_bundle.json').write_text('OLD');(dest/'requirements.txt').write_text('OLD')
            with patch.object(upgrade,'STAGE',ROOT),patch.object(upgrade,'SHARED',dest),patch.object(upgrade,'stage_check'),patch.object(upgrade,'backup_dir',return_value=backup),patch.object(upgrade,'run',side_effect=AssertionError('systemd forbidden')):
                upgrade.install_shared()
            self.assertEqual(verify_bundle(dest)['engine_version'],ENGINE_VERSION);self.assertEqual((backup/'old/okx_source_bundle.json').read_text(),'OLD')
    def test_new_and_clone_real_source_pin_with_mocked_external_edges(self):
        with tempfile.TemporaryDirectory() as t, ExitStack() as st:
            base=Path(t)/'bots';units=Path(t)/'units';base.mkdir();units.mkdir();registry={}
            for name,value in [('BASE',base),('SYSTEMD_DIR',units),('SOURCE',ROOT),('load_registry',lambda:dict(registry)),('save_registry',lambda e:registry.update(e)),('require_account_capacity',lambda *a,**kw:0),('validated_newbot_env',lambda *a:({},'FAKE_CREDS=placeholder')),('verify_fresh_contract',lambda *a:True),('run',lambda *a,**kw:SimpleNamespace(stdout='',returncode=0)),('provision_observers',lambda *a:None)]:st.enter_context(patch.object(manager,name,value))
            st.enter_context(patch.object(manager.wizard,'BASE',ROOT))
            ctx=dict(name='testnew',coin='ENSO',instrument=INST,upper=D(1),price=D(1),step=D('.01'),tp=D('.01'),qty=D(4),ct=D(1),lot=D(1),min_contracts=D(1),spec={},tick=D('.001'),real_tp=3,buys=3,grids=20,maker=D('.0002'),taker=D('.0005'))
            st.enter_context(patch.object(manager.curses,'wrapper',return_value=ctx));st.enter_context(patch.object(manager.sys.stdin,'isatty',return_value=True));st.enter_context(patch.object(manager.sys.stdout,'isatty',return_value=True))
            with patch('builtins.input',return_value='no'):manager.cmd_new.__wrapped__(None)
            new=Path(registry['testnew']['path']);self.assertTrue(verify_instance(new,ROOT));self.assertTrue((new/'state/cycles.sqlite').is_file())
            st.enter_context(patch.object(manager,'instance_terminal',return_value=True));st.enter_context(patch.object(manager,'bot_path',return_value=new));st.enter_context(patch.object(manager.wizard,'okx_req',return_value=[{'ruleType':'normal'}]));st.enter_context(patch.object(manager.wizard,'validate_spec',return_value='ENSO'))
            st.enter_context(patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'code':'0','data':[{'bidPx':'0.99'}]}).encode())))
            with patch('builtins.input',return_value='CLONE testclone'):manager.cmd_clone('testnew','testclone')
            clone=Path(registry['testclone']['path']);self.assertTrue(verify_instance(clone,ROOT));cfg=json.loads((clone/'config.json').read_text());self.assertFalse(cfg['live_trading']);self.assertEqual(cfg['version'],ENGINE_VERSION);self.assertTrue((clone/'state/cycles.sqlite').is_file())
            with closing(sqlite3.connect(clone/'state/cycles.sqlite')) as c:self.assertEqual(c.execute('SELECT count(*) FROM cycles').fetchone()[0],0)

if __name__=='__main__':unittest.main()
