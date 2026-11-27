import hashlib
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from datetime import datetime, timezone
from okx_funding_collector import (BotInventory, inventory, run_once, HOUR,
    idle_inventory_stamp, idle_cache_hit, save_idle_cache)
from types import SimpleNamespace
from contextlib import closing
from okx_funding_view import terminal_report, terminal_funding, terminal_wait
from test_funding_collector import FakeClient, CREDS
import okx_telegram_followups as tg
from pathlib import Path
from okx_funding_ledger import (apply_complete_windows, read_snapshot,
    read_terminal_snapshot, read_terminal_snapshot_status, terminal_identity, LedgerError, terminal_failure)
from test_funding_ledger import bill, MMT, T

ACCOUNT = 'a'*64
CREATED = T-1000
END = T+120000
IDENT = ('old',MMT,CREATED,END,'b'*64,'PANIC_DONE')

class TerminalFundingLifetime(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.db=self.root/'funding.sqlite'
    def apply(self,begin,end,rows,inventory=None,terminals=(),sync=None,**kw):
        return apply_complete_windows(self.db,[(begin,end,rows)],inventory or {},
            sync if sync is not None else T+130000,account_fingerprint=ACCOUNT,terminals=terminals,**kw)
    def test_terminal_exact_cutoff_and_recreation(self):
        self.apply(CREATED,END,[bill(1,ts=CREATED),bill(2,ts=END-1,amount='0.05',subtype='174')],
                   terminals=[IDENT])
        self.assertEqual(read_terminal_snapshot(self.db,IDENT),('0.04827275',END-1,T+130000,1))
        self.apply(END,END+1000,[bill(3,ts=END)],inventory={MMT:END/1000},sync=T+131000)
        self.assertEqual(read_snapshot(self.db,MMT,END/1000)['amount'],'-0.00172725')
        self.assertEqual(read_terminal_snapshot(self.db,IDENT)[0],'0.04827275')
        self.assertIsNone(read_terminal_snapshot(self.db,('foreign',MMT,CREATED,END,'b'*64,'PANIC_DONE')))
    def test_incomplete_until_terminal_window_covered(self):
        self.apply(CREATED,T,[bill(1,ts=CREATED)],inventory={MMT:CREATED/1000})
        self.assertIsNone(read_terminal_snapshot(self.db,IDENT))
        self.apply(T,T+500,[],terminals=[IDENT],sync=T+131000)
        self.assertEqual(read_terminal_snapshot(self.db,IDENT)[3],0)
        self.apply(T+500,END,[bill(2,ts=END-1)],terminals=[IDENT],sync=T+132000)
        self.assertEqual(read_terminal_snapshot(self.db,IDENT)[0],'-0.0034545')
        self.assertEqual(read_terminal_snapshot(self.db,IDENT)[3],1)
        self.assertIsNone(read_snapshot(self.db,MMT,CREATED/1000))
    def test_discrepancy_prevents_verified_terminal(self):
        self.apply(CREATED,END,[bill(1)],terminals=[IDENT])
        self.apply(CREATED,END,[],terminals=[IDENT],sync=T+131000,reconcile_missing=True)
        row,conflict=read_terminal_snapshot_status(self.db,IDENT)
        self.assertEqual(row[3],1)
        self.assertTrue(conflict)
        self.assertEqual(row[2],T+130000)
    def test_verified_zero_is_not_missing(self):
        self.apply(CREATED,END,[],terminals=[IDENT])
        self.assertEqual(read_terminal_snapshot(self.db,IDENT),('0',None,T+130000,1))
    def test_invalid_terminal_identity_rejected(self):
        with self.assertRaises(LedgerError):
            self.apply(CREATED,END,[],terminals=[('old',MMT,END,CREATED,'b'*64,'PANIC_DONE')])
    def test_terminal_state_identity(self):
        root=self.root/'old'; (root/'state').mkdir(parents=True)
        (root/'config.json').write_text(json.dumps({'instrument':MMT,'created_at':CREATED/1000}))
        state={'terminal_mode':'PANIC_DONE','terminal_snapshot':{'mode':'PANIC_DONE','at_unix':END/1000,'position_contracts':'0'}}
        raw=json.dumps(state).encode();(root/'state/bot_state.json').write_bytes(raw)
        (root/'state/control_ack.json').write_text(json.dumps({'mode':'PANIC','phase':'PANIC_DONE','remaining_position':'0'}))
        final={'schema_version':1,'complete':True,'instrument':MMT,'mode':'PANIC_DONE','terminal_state_sha256':hashlib.sha256(raw).hexdigest(),'values':{'position_contracts':'0'}}
        (root/'state/final_stats.json').write_text(json.dumps(final))
        identity,pos=terminal_identity(root)
        self.assertEqual(identity[:4],IDENT[:4]);self.assertEqual(pos,0)
        (root/'state/bot_state.json').write_bytes(raw+b' ')
        with self.assertRaisesRegex(LedgerError,'TERMINAL_ACCOUNTING_MISMATCH'):
            terminal_identity(root)

class EndToEndTerminal(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.base=Path(tmp.name);self.db=self.base/'funding.sqlite'
        self.registry=self.base/'.okxbot_registry.json';self.units=self.base/'units';self.units.mkdir()
        self.root=self.base/'initold';(self.root/'state').mkdir(parents=True)
        self.created=T-24*HOUR+2*60000;self.end=T+42*60000
        self.env='\n'.join(k+'='+v for k,v in CREDS.items())
        self.reg={self.root.name:self.bot(self.root,self.created)}
        self.registry.write_text(json.dumps(self.reg))
        self.rows=[bill(1,ts=T+1717,amount='16.5398510299772328',subtype='174')]
        self.inv=lambda:inventory(self.base,self.registry,self.units,
            hashlib.sha256(CREDS['OKX_API_KEY'].encode()).hexdigest() if self.db.exists() else None)
    def bot(self,root,created):
        (root/'config.json').write_text(json.dumps({'instrument':MMT,'created_at':created/1000,'contract_value':'1'}))
        (root/'.env').write_text(self.env)
        service='okx-grid-'+root.name;(self.units/(service+'.service')).touch()
        return {'path':str(root),'service':service}
    def collect(self,now,mode='auto'):
        c=FakeClient(self.rows)
        return run_once(self.db,self.inv,lambda _:c,now=now,mode=mode),c
    def retire(self,mode='PANIC_DONE',position='0'):
        marker=datetime.fromtimestamp(self.end/1000,timezone.utc).isoformat()
        state={'terminal_mode':mode,'terminal_snapshot':{'mode':mode,'at_unix':self.end/1000,'at_utc':marker,'position_contracts':position}}
        raw=json.dumps(state).encode();(self.root/'state/bot_state.json').write_bytes(raw)
        (self.root/'state/control_ack.json').write_text(json.dumps({'mode':'PANIC' if mode=='PANIC_DONE' else 'HARD','phase':mode,'updated_at':marker,'remaining_position':position}))
        vals={'position_contracts':position,'position_base':position,'grid_profit':'27.1624205','other_closed_profit':'0','panic_pnl':'-0.0072774','panic_sell_qty':'1','panic_sell_fee':'-0.0026262','final_trade_pnl':'27.1551431' if mode=='PANIC_DONE' else None,'closed_cycles':'5'}
        self.final={'schema_version':1,'complete':True,'instrument':MMT,'mode':mode,'terminal_state_sha256':hashlib.sha256(raw).hexdigest(),'values':vals,'closed_at':marker,'report':'ИТОГОВЫЙ PnL: +$27.15514310\nФандинг не включён.' if mode=='PANIC_DONE' else '⚠️ Позиция оставлена открытой. ИТОГОВЫЙ PnL НЕ ОПРЕДЕЛЁН.'}
        (self.root/'state/final_stats.json').write_text(json.dumps(self.final))
        self.reg[self.root.name]['retired']=True
        self.registry.write_text(json.dumps(self.reg))
    def scan(self,st,outgoing,now=None,names=None):
        with patch('okx_funding_view.TERMINAL_DB',self.db),patch.object(tg,'one',side_effect=lambda api,st,key,msg,**kw:outgoing.append((key,msg))),patch.object(tg.time,'time',return_value=(self.end+1000 if now is None else now)/1000):
            tg.panic_scan(None,st,{self.root.name:self.root} if names is None else names,base=self.base)
    def monitor_state(self):
        return {'v0720_observing_since':1.,'v0720_panic_seen':{self.root.name:'observed_running'}}
    def test_one_immediate_final_after_hourly_collection_new_instance(self):
        r,c=self.collect(T+5*60000);self.assertEqual(r['status'],'PASS');self.assertGreater(c.requests,0)
        r,c=self.collect(T+6*60000);self.assertEqual(r['status'],'IDLE');self.assertEqual(c.requests,0)
        self.retire();ident,_=terminal_identity(self.root)
        new=self.base/'new';(new/'state').mkdir(parents=True)
        self.reg[new.name]=self.bot(new,self.end+1)
        self.registry.write_text(json.dumps(self.reg))
        st=self.monitor_state();outgoing=[]
        # The previous hourly snapshot is already sufficient. No collector tick.
        self.scan(st,outgoing)
        self.assertEqual(len(outgoing),1);self.assertIn('FUNDING: +$16.53985103',outgoing[0][1]);self.assertIn('+$43.69499413',outgoing[0][1])
        self.assertIn('+$43.69499413',terminal_report(self.root,self.final,path=self.db))
        r,c=self.collect(self.end+1000);self.assertEqual(r['terminal_published'],1);self.assertEqual(c.requests,0)
        self.assertEqual(read_terminal_snapshot(self.db,ident)[0],'16.5398510299772328')
        self.rows.append(bill(3,ts=T+HOUR+2000,amount='0.02',subtype='174'))
        r,c=self.collect(T+HOUR+5*60000)
        self.assertEqual(read_snapshot(self.db,MMT,(self.end+1)/1000)['amount'],'0.02')
        self.assertEqual(read_terminal_snapshot(self.db,ident)[0],'16.5398510299772328')
        self.scan(json.loads(json.dumps(st)),outgoing,now=T+HOUR+6*60000,names={})
        self.assertEqual(len(outgoing),1)
        self.assertEqual(json.loads((self.root/'state/final_stats.json').read_text()),self.final)
    def test_fast_proof_survives_live_snapshot_replacement_and_reads_no_keys(self):
        self.collect(T+5*60000);self.retire();ident,_=terminal_identity(self.root)
        new=self.base/'new';(new/'state').mkdir(parents=True)
        self.reg[new.name]=self.bot(new,self.end+1);self.registry.write_text(json.dumps(self.reg))
        apply_complete_windows(self.db,[(T+5*60000,T+6*60000,[])],{MMT:(self.end+1)/1000},self.end+2000,account_fingerprint=hashlib.sha256(CREDS['OKX_API_KEY'].encode()).hexdigest())
        self.assertIsNone(read_snapshot(self.db,MMT,self.created/1000))
        original_text=Path.read_text;original_bytes=Path.read_bytes
        def no_key_text(p,*a,**k):
            self.assertNotEqual(p.name,'.env');return original_text(p,*a,**k)
        def no_key_bytes(p,*a,**k):
            self.assertNotEqual(p.name,'.env');return original_bytes(p,*a,**k)
        st=self.monitor_state();outgoing=[]
        with patch.object(Path,'read_text',no_key_text),patch.object(Path,'read_bytes',no_key_bytes):
            self.scan(st,outgoing,now=self.end+3000)
        self.assertEqual(len(outgoing),1);self.assertIn('+$43.69499413',outgoing[0][1])
        r,c=self.collect(self.end+4000);self.assertEqual(c.requests,0);self.assertEqual(r['terminal_published'],1)
        self.assertEqual(read_terminal_snapshot(self.db,ident)[0],'16.5398510299772328')
    def test_sync_time_alone_is_not_a_coverage_proof(self):
        self.collect(T+5*60000);self.retire()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE coverage SET end_ms=? WHERE end_ms>?',(T,T));db.commit()
        self.assertIsNone(terminal_funding(self.root,path=self.db))
        st=self.monitor_state();outgoing=[];self.scan(st,outgoing)
        self.assertEqual(outgoing,[])
        r,c=self.collect(self.end+2000);self.assertEqual(c.requests,1);self.assertEqual(r['terminal_published'],1)
        self.scan(st,outgoing,now=self.end+3000);self.assertIn('+$43.69499413',outgoing[0][1])
    def test_late_ledger_contradiction_does_not_silently_replace_fast_proof(self):
        self.collect(T+5*60000);self.retire();ident,_=terminal_identity(self.root)
        st=self.monitor_state();outgoing=[];self.scan(st,outgoing)
        self.assertEqual(len(outgoing),1)
        apply_complete_windows(self.db,[(T,T+60000,[bill(2,ts=T+2500,amount='0.01',subtype='174')])],{},self.end+2000,account_fingerprint=hashlib.sha256(CREDS['OKX_API_KEY'].encode()).hexdigest(),publish_live=False)
        self.assertIsNone(terminal_funding(self.root,path=self.db))
        r,c=self.collect(self.end+3000);self.assertEqual(c.requests,0)
        self.assertIsNone(read_terminal_snapshot(self.db,ident))
        self.assertEqual(terminal_failure(self.db,ident),'TERMINAL_PROOF_CONTRADICTION')
        self.assertIn('TERMINAL_PROOF_CONTRADICTION',terminal_report(self.root,self.final,path=self.db))
        self.scan(st,outgoing,now=self.end+100000);self.assertEqual(len(outgoing),1)
    def test_targeted_terminal_get_does_not_refresh_live_snapshot(self):
        self.end=T+3*60000
        other=self.base/'eth';(other/'state').mkdir(parents=True)
        self.reg[other.name]=self.bot(other,self.created)
        cfg=json.loads((other/'config.json').read_text());cfg['instrument']='ETH-USDT-SWAP';(other/'config.json').write_text(json.dumps(cfg))
        self.registry.write_text(json.dumps(self.reg))
        self.collect(T-HOUR+5*60000);before=read_snapshot(self.db,'ETH-USDT-SWAP',self.created/1000)
        self.retire();r,c=self.collect(self.end+1000)
        self.assertEqual(c.requests,1);self.assertEqual(r['published'],0)
        self.assertEqual(read_snapshot(self.db,'ETH-USDT-SWAP',self.created/1000),before)
    def test_idle_stat_cache_never_reads_credentials_and_invalidates(self):
        now=T+6*60000;self.collect(T+5*60000)
        a=SimpleNamespace(mode='auto',base=self.base,registry=self.registry,systemd_dir=self.units)
        cache=self.base/'idle_inventory.sha256'
        original=Path.read_text
        def only_small(p,*a,**k):
            self.assertNotIn(p.name,('.env','bot_state.json','final_stats.json'));return original(p,*a,**k)
        with patch.object(Path,'read_text',only_small):
            stamp=idle_inventory_stamp(self.base,self.registry,self.units,self.db)
            save_idle_cache(cache,stamp);self.assertTrue(idle_cache_hit(a,self.db,cache,now))
            self.assertFalse(idle_cache_hit(a,self.db,cache,T+HOUR+5*60000))
            (self.root/'.env').write_text(self.env+'\n# changed')
            self.assertFalse(idle_cache_hit(a,self.db,cache,now))
        save_idle_cache(cache,idle_inventory_stamp(self.base,self.registry,self.units,self.db))
        self.assertTrue(idle_cache_hit(a,self.db,cache,now));self.retire()
        self.assertFalse(idle_cache_hit(a,self.db,cache,self.end+1000))
    def test_hard_inside_settlement_does_not_claim_post_hard_bill(self):
        self.end=T+1000;self.rows=[bill(1,ts=T+2500,amount='-0.42')]
        self.collect(T-HOUR+5*60000);self.retire('RETIRED','3');ident,_=terminal_identity(self.root)
        r,c=self.collect(T+60000)
        self.assertEqual(c.requests,1);self.assertEqual(read_terminal_snapshot(self.db,ident)[3],0)
        self.assertIn('Funding после остановки не включён',terminal_report(self.root,self.final,path=self.db))
    def test_missing_database_sends_verified_trade_final_without_funding(self):
        self.retire();st=self.monitor_state();outgoing=[];self.scan(st,outgoing)
        self.assertEqual(len(outgoing),1);self.assertIn('DB_UNAVAILABLE',outgoing[0][1]);self.assertIn('ТОРГОВЫЙ PnL: +$27.15514310',outgoing[0][1])
    def test_before_hourly_collection_one_gap_get(self):
        self.end=T+3*60000
        self.collect(T-HOUR+5*60000);self.retire()
        st=self.monitor_state();outgoing=[]
        self.scan(st,outgoing);self.assertEqual(outgoing,[])
        r,c=self.collect(self.end+1000)
        self.assertEqual(c.requests,1);self.assertEqual(r['terminal_published'],1)
        self.assertEqual(c.calls[0][2],T+60000)
        self.scan(st,outgoing,now=self.end+2000)
        self.assertEqual(len(outgoing),1);self.assertIn('FUNDING: +$16.53985103',outgoing[0][1])
    def test_panic_first_second_delayed_bill_included(self):
        self.end=T+1000
        self.rows=[bill(1,ts=T+2500,amount='-0.42')]
        self.collect(T-HOUR+5*60000);self.retire();ident,_=terminal_identity(self.root)
        st=self.monitor_state();outgoing=[]
        self.scan(st,outgoing,now=T+1500);self.assertEqual(outgoing,[])
        r,c=self.collect(T+59000);self.assertEqual(c.requests,0);self.assertIsNone(read_terminal_snapshot(self.db,ident))
        r,c=self.collect(T+60000);self.assertEqual(c.requests,1);self.assertEqual(r['terminal_published'],1)
        self.assertEqual(read_terminal_snapshot(self.db,ident)[0],'-0.42')
        self.scan(st,outgoing,now=T+61000);self.assertEqual(len(outgoing),1);self.assertIn('−$0.42000000',outgoing[0][1])
    def test_shared_settlement_minute_never_assigns_successor_bill(self):
        self.end=T+1000
        self.rows=[bill(1,ts=T+2500,amount='-0.42')]
        self.collect(T-HOUR+5*60000);self.retire();ident,_=terminal_identity(self.root)
        new=self.base/'new';(new/'state').mkdir(parents=True)
        self.reg[new.name]=self.bot(new,T+2000);self.registry.write_text(json.dumps(self.reg))
        st=self.monitor_state();outgoing=[]
        self.scan(st,outgoing,now=T+3000);self.assertEqual(len(outgoing),1)
        self.assertIn('OVERLAPPING_INSTRUMENT_LIFETIMES',outgoing[0][1])
        r,c=self.collect(T+60000);self.assertIsNone(read_terminal_snapshot(self.db,ident))
        self.assertEqual(terminal_failure(self.db,ident),'OVERLAPPING_INSTRUMENT_LIFETIMES')
        del self.reg[new.name];self.registry.write_text(json.dumps(self.reg))
        self.collect(T+5*60000);self.assertIsNone(read_terminal_snapshot(self.db,ident))
        self.scan(st,outgoing,now=T+HOUR);self.assertEqual(len(outgoing),1)
    def test_panic_exact_hour_does_not_include_new_hour(self):
        self.end=T
        self.rows=[bill(1,ts=T+2500,amount='-0.42')]
        self.collect(T-HOUR+5*60000);self.retire();ident,_=terminal_identity(self.root)
        st=self.monitor_state();outgoing=[];self.scan(st,outgoing)
        self.assertEqual(len(outgoing),1);self.assertIn('FUNDING: $0.00000000',outgoing[0][1])
        r,c=self.collect(T+1000);self.assertEqual(c.requests,0);self.assertEqual(read_terminal_snapshot(self.db,ident)[0],'0')
    def test_late_funding_proof_at_80_seconds_has_one_confirmed_report(self):
        self.end=T+3*60000
        self.collect(T-HOUR+5*60000);self.retire()
        st=self.monitor_state();outgoing=[]
        self.scan(st,outgoing,now=self.end+76000);self.assertEqual(outgoing,[])
        self.assertEqual(terminal_wait(self.root,path=self.db,now_ms=self.end+79999)[0],True)
        r,c=self.collect(self.end+80000);self.assertEqual(r['terminal_published'],1)
        self.scan(st,outgoing,now=self.end+81000)
        self.assertEqual(len(outgoing),1);self.assertIn('FUNDING: +$16.53985103',outgoing[0][1])
        self.scan(json.loads(json.dumps(st)),outgoing,now=self.end+91000);self.assertEqual(len(outgoing),1)

    def test_no_collection_cannot_silence_forever(self):
        self.end=T+3*60000
        self.collect(T-HOUR+5*60000);self.retire()
        st=self.monitor_state();outgoing=[]
        self.scan(st,outgoing,now=self.end+76000)
        self.assertEqual(outgoing,[])
        self.scan(st,outgoing,now=self.end+89999)
        self.assertEqual(outgoing,[])
        self.scan(st,outgoing,now=self.end+90000)
        self.assertEqual(len(outgoing),1);self.assertIn('INCOMPLETE_HISTORY_OR_COLLECTION_TIMEOUT',outgoing[0][1])
        self.collect(self.end+91000)
        self.scan(json.loads(json.dumps(st)),outgoing,now=self.end+92000)
        self.assertEqual(len(outgoing),1)
    def test_all_bots_terminal_and_hard_open(self):
        self.collect(T+5*60000);self.retire('RETIRED','3')
        r,c=self.collect(self.end+121000);self.assertEqual(r['terminal_published'],1)
        report=terminal_report(self.root,self.final,path=self.db)
        self.assertIn('FUNDING: +$16.53985103',report);self.assertIn('НЕ ОПРЕДЕЛЁН',report)
        self.assertNotIn('ИТОГОВЫЙ PnL:',report)
    def test_existing_notification_is_not_replayed(self):
        self.collect(T+5*60000);self.retire();self.collect(self.end+121000)
        marker=self.final['closed_at'];key=f'panic/final/{self.root.name}/{marker}'
        st={'v0720_observing_since':1.,'v0720_panic_seen':{self.root.name:'observed_running'},'incident_sent':{key:1.}}
        with patch('okx_funding_view.TERMINAL_DB',self.db),patch.object(tg,'one',side_effect=AssertionError('must not send a funding correction')):
            tg.panic_scan(None,st,{self.root.name:self.root},base=self.base)
    def test_terminal_api_failure_sends_one_unconfirmed_final(self):
        self.end=T+3*60000
        self.collect(T-HOUR+5*60000);self.retire();ident,_=terminal_identity(self.root)
        with self.assertRaisesRegex(LedgerError,'SIMULATED_FAILURE'):
            run_once(self.db,self.inv,lambda _:FakeClient(self.rows,fail_at=1),now=self.end+1000,mode='auto')
        self.assertIsNone(read_terminal_snapshot(self.db,ident))
        self.assertIsNone(terminal_funding(self.root,path=self.db))
        st=self.monitor_state();outgoing=[];self.scan(st,outgoing,now=self.end+2000)
        self.assertEqual(len(outgoing),1);self.assertIn('COLLECTION_SIMULATED_FAILURE',outgoing[0][1]);self.assertIn('ТОРГОВЫЙ PnL: +$27.15514310',outgoing[0][1])
        r,c=self.collect(self.end+60000);self.assertEqual(c.requests,0)
        self.collect(T+5*60000)
        self.scan(st,outgoing,now=T+6*60000);self.assertEqual(len(outgoing),1)
    def test_missing_old_archive_does_not_block_active(self):
        self.reg['missing']={'path':str(self.base/'missing'),'retired':True}
        self.registry.write_text(json.dumps(self.reg))
        r,c=self.collect(T+5*60000)
        self.assertEqual(r['published'],1)
        self.assertEqual(r['inventory_issues'],0)
        self.assertEqual(r['terminal_inventory_issues'],1)
    def test_mismatched_terminal_account_never_finalizes(self):
        self.collect(T+5*60000);self.retire();ident,_=terminal_identity(self.root)
        (self.root/'.env').write_text(self.env.replace('OKX_API_KEY=fake','OKX_API_KEY=foreign'))
        with self.assertRaisesRegex(LedgerError,'NO_TRUSTED_ACCOUNT_CREDENTIALS'):
            self.collect(self.end+121000)
        self.assertIsNone(read_terminal_snapshot(self.db,ident))
        st=self.monitor_state();outgoing=[];self.scan(st,outgoing)
        self.assertEqual(len(outgoing),1);self.assertIn('ACCOUNT_CREDENTIALS_NOT_ATTESTED',outgoing[0][1])

if __name__=='__main__':unittest.main()
