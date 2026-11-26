"""Regression cases from independent F6 audit; no LIVE or network."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from okx_funding_collector import inventory, run_once, fetch_window, RECENT, ARCHIVE, HOUR, DAY, LedgerError, binding
from okx_funding_ledger import read_snapshot
from okx_funding_backup import daily_backup
from okx_funding_view import initial_line
from test_funding_collector import FakeClient, CREDS, NOW, END
from test_funding_ledger import bill, T, MMT, ETH

class AuditRegression(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.db=self.root/'funding.sqlite'
        self.bots={MMT:(T-DAY)/1000,ETH:(T-DAY)/1000}
        self.base=self.root/'bots';self.base.mkdir();self.units=self.root/'units';self.units.mkdir()
        self.registry=self.root/'registry.json';self.entries={}
    def run_fake(self,rows,now=NOW,mode='update',inv=None):
        return run_once(self.db,inv or (lambda:(self.bots,CREDS)),lambda _:FakeClient(rows),now=now,mode=mode)
    def snap(self,inst):return read_snapshot(self.db,inst,self.bots[inst])
    def seed(self):self.run_fake([bill(1),bill(2,ETH)],mode='backfill')
    def query(self,sql):
        with closing(sqlite3.connect(self.db)) as d:return d.execute(sql).fetchall()
    def add(self,name,inst,creds=CREDS):
        p=self.base/name;p.mkdir();(p/'state').mkdir()
        (p/'config.json').write_text(json.dumps(dict(instrument=inst,created_at=(T-DAY)/1000)))
        (p/'.env').write_text('\n'.join(k+'='+v for k,v in creds.items()))
        unit='okx-grid-'+name;(self.units/(unit+'.service')).touch()
        self.entries[name]=dict(path=str(p),service=unit)
        self.registry.write_text(json.dumps(self.entries));return p
    def inv(self):return inventory(self.base,self.registry,self.units,binding(self.db))
    def test_fixture_order_not_sorted_by_test(self):
        fixture=json.loads(Path(__file__).with_name('f1_bills_fixture.json').read_text())
        class Ordered:
            def __init__(self,rows):self.rows=rows
            def page(self,*args):return self.rows
        for endpoint,key in ((RECENT,'recent'),(ARCHIVE,'archive')):
            rows=fixture[key]
            self.assertTrue(all(int(a['billId'])>int(b['billId']) for a,b in zip(rows,rows[1:])))
            self.assertEqual(fetch_window(Ordered(rows),endpoint,T-DAY,END)[2],rows)
        self.assertEqual(fixture['recent'],fixture['archive'])
    def test_nonmonotonic_real_contract_still_fails_closed(self):
        class Unordered:
            def page(self,*args):return [bill(1),bill(2)]
        with self.assertRaisesRegex(LedgerError,'NONMONOTONIC_BILL_CURSOR'):
            fetch_window(Unordered(),RECENT,T-1,T+1)
    def test_foreign_isolated_24h_and_daily_reconcile(self):
        self.seed();foreign=dict(bill(3,'OTHER-USDT-SWAP'),mgnMode='isolated')
        for i in range(1,4):
            r=self.run_fake([bill(1),bill(2,ETH),foreign,bill(4,ETH)],NOW+i*HOUR,mode='reconcile')
            self.assertEqual(r['published'],2);self.assertEqual(r['quarantined'],1)
            self.assertEqual(self.snap(ETH)['amount'],'-0.0034545')
        self.assertEqual(self.query('SELECT inst_id,reason FROM quarantine'),[('OTHER-USDT-SWAP','UNEXPECTED_BILL_CONTRACT')])
    def test_local_bad_payment_recovers_when_corrected(self):
        self.seed();before=self.snap(MMT)
        r=self.run_fake([bill(1),bill(2,ETH),dict(bill(3),balChg='0'),bill(4,ETH)],NOW+HOUR)
        self.assertEqual(r['published'],1);self.assertEqual(self.snap(MMT),before)
        self.assertEqual(self.snap(ETH)['amount'],'-0.0034545')
        r=self.run_fake([bill(1),bill(2,ETH),bill(3),bill(4,ETH)],NOW+2*HOUR)
        self.assertEqual(r['published'],2);self.assertEqual(self.snap(MMT)['amount'],'-0.0034545')
        self.assertEqual(self.query('SELECT count(*) FROM quarantine'),[(0,)])
    def test_prior_quarantine_identity_conflict_is_not_reassigned(self):
        self.seed();foreign=dict(bill(3,'OTHER-USDT-SWAP'),mgnMode='isolated')
        self.run_fake([bill(1),bill(2,ETH),foreign],NOW+HOUR)
        old=self.snap(MMT)
        r=self.run_fake([bill(1),bill(2,ETH),bill(3),bill(4,ETH)],NOW+2*HOUR)
        self.assertEqual(r['published'],1);self.assertEqual(self.snap(MMT),old)
        self.assertEqual(self.snap(ETH)['amount'],'-0.0034545')
        self.assertEqual(self.query("SELECT count(*) FROM bills WHERE bill_id='3'"),[(0,)])
        self.assertEqual(set(self.query("SELECT DISTINCT inst_id FROM quarantine WHERE reason='CONFLICTING_BILL_ID'")),{(MMT,),('OTHER-USDT-SWAP',)})
    def test_missing_kept_then_reappears(self):
        self.seed()
        for i in range(1,4):
            r=self.run_fake([bill(2,ETH),bill(3)],NOW+i*HOUR,mode='reconcile')
            self.assertEqual(r['published'],2);self.assertEqual(self.snap(MMT)['amount'],'-0.0034545')
            self.assertFalse(self.snap(MMT)['complete'])
        self.assertEqual(self.query('SELECT bill_id FROM discrepancy'),[('1',)])
        self.run_fake([bill(1),bill(2,ETH),bill(3)],NOW+4*HOUR)
        self.assertTrue(self.snap(MMT)['complete']);self.assertEqual(self.query('SELECT count(*) FROM discrepancy'),[(0,)])
    def test_missing_service_and_bad_ack_are_local(self):
        a=self.add('a',MMT);b=self.add('b',ETH)
        self.run_fake([bill(1),bill(2,ETH)],mode='backfill',inv=self.inv);before=self.snap(MMT)
        for problem in ('bad_ack','missing_service','final_report'):
            if problem=='bad_ack':(a/'state/control_ack.json').write_text('[]')
            elif problem=='missing_service':(a/'state/control_ack.json').unlink();(self.units/'okx-grid-a.service').unlink()
            else:(self.units/'okx-grid-a.service').touch();(a/'state/final_stats.json').write_text('{}')
            r=self.run_fake([bill(1),bill(2,ETH),bill(3,ETH)],NOW+HOUR,inv=self.inv)
            self.assertEqual(r['published'],1);self.assertEqual(self.snap(MMT),before)
            self.assertEqual(self.snap(ETH)['amount'],'-0.0034545')
    def test_duplicate_local_and_collect_when_no_publishable_bots(self):
        self.add('a',MMT);self.add('b',MMT);self.add('c',ETH)
        r=self.run_fake([bill(1),bill(2,ETH)],mode='backfill',inv=self.inv)
        self.assertEqual(r['published'],1);self.assertIsNone(self.snap(MMT))
        self.add('d',ETH)
        r=self.run_fake([bill(1),bill(2,ETH),bill(3,ETH)],NOW+HOUR,inv=self.inv)
        self.assertEqual(r['published'],0);self.assertEqual(r['inserted'],1)
    def test_bound_account_excludes_mismatch(self):
        a=self.add('a',MMT);b=self.add('b',ETH)
        self.run_fake([bill(1),bill(2,ETH)],mode='backfill',inv=self.inv);before=self.snap(MMT)
        (a/'.env').write_text('\n'.join(k+'='+v for k,v in dict(CREDS,OKX_API_KEY='other').items()))
        r=self.run_fake([bill(1),bill(2,ETH),bill(3,ETH)],NOW+HOUR,inv=self.inv)
        self.assertEqual(r['published'],1);self.assertEqual(self.snap(MMT),before)
        self.assertEqual(self.snap(ETH)['amount'],'-0.0034545')
    def test_unbound_ambiguous_account_stops_before_network(self):
        self.add('a',MMT);self.add('b',ETH,dict(CREDS,OKX_API_KEY='other'))
        with self.assertRaisesRegex(LedgerError,'ACCOUNT_CREDENTIALS_AMBIGUOUS'):
            run_once(self.db,self.inv,lambda _:self.fail('network'),now=NOW)
        self.assertFalse(self.db.exists())
    def test_inventory_race_keeps_stable_and_history(self):
        self.seed();old=self.bots.copy();new={MMT:(T+1000)/1000,ETH:old[ETH]}
        seq=iter([(old,CREDS),(new,CREDS)])
        r=self.run_fake([bill(1),bill(2,ETH),bill(3,ETH)],NOW+HOUR,inv=lambda:next(seq))
        self.assertEqual(r['published'],1);self.assertIsNone(read_snapshot(self.db,MMT,new[MMT]))
        self.assertEqual(self.snap(ETH)['amount'],'-0.0034545')
    def test_backup_rotation_and_unrelated_files(self):
        self.seed();start=datetime(2026,11,1,tzinfo=timezone.utc)
        daily_backup(self.db,now=start);folder=self.root/'backups'
        (folder/'unrelated.sqlite').write_text('keep')
        (folder/'funding-2020-01-01.sqlite').symlink_to(folder/'unrelated.sqlite')
        for i in range(1,10):daily_backup(self.db,now=start+timedelta(days=i))
        copies=[p for p in folder.glob('funding-*.sqlite') if not p.is_symlink()]
        self.assertEqual(len(copies),7);self.assertEqual(min(p.name for p in copies),'funding-2026-11-04.sqlite')
        self.assertEqual((folder/'unrelated.sqlite').read_text(),'keep')
        self.assertTrue((folder/'funding-2020-01-01.sqlite').is_symlink())
    def test_initial_display_not_confirmed_value(self):
        self.assertEqual(initial_line(),'FUNDING: +0.000000, ещё не обновлялся')
        self.assertFalse(self.db.exists())

if __name__=='__main__':unittest.main()
