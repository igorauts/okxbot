"""F6 regression checks. All processes and files are disposable; no real network."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from okx_funding_collector import (run_once, plan_windows, read_state, archive_floor,
    RECENT, ARCHIVE, DAY, HOUR, inventory, LedgerError)
from okx_funding_ledger import apply_complete_windows, read_snapshot, writer
from okx_funding_view import load_funding
from okx_funding_backup import daily_backup
from test_funding_collector import FakeClient, CREDS, NOW, END
from test_funding_ledger import T, MMT, ETH, bill

class F6Tests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.db=self.root/'funding.sqlite'
        self.bots={MMT:(T-DAY)/1000}
    def seed(self):
        return run_once(self.db,lambda:(self.bots,CREDS),lambda _:FakeClient([bill(1)]),now=NOW,mode='backfill')
    def test_account_binding_atomic_and_no_request_on_change(self):
        self.seed(); before=read_snapshot(self.db,MMT,self.bots[MMT])
        with self.assertRaisesRegex(LedgerError,'ACCOUNT_BINDING_MISMATCH'):
            run_once(self.db,lambda:(self.bots,dict(CREDS,OKX_API_KEY='changed')),lambda _:self.fail('network forbidden'),now=NOW+HOUR)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT]),before)
        with closing(sqlite3.connect(self.db)) as d:
            self.assertEqual(d.execute("SELECT value FROM meta WHERE key='account_fingerprint'").fetchone()[0],hashlib.sha256(CREDS['OKX_API_KEY'].encode()).hexdigest())
    def test_unbound_nonempty_database_not_adopted(self):
        apply_complete_windows(self.db,[(T-1000,T+1000,[bill(1)])],self.bots,NOW)
        with self.assertRaisesRegex(LedgerError,'UNBOUND_EXISTING_HISTORY'):
            run_once(self.db,lambda:(self.bots,CREDS),lambda _:self.fail('network forbidden'),now=NOW+HOUR)
    def test_account_binding_rolls_back_with_bill_conflict(self):
        self.seed(); fp='a'*64
        with self.assertRaisesRegex(LedgerError,'ACCOUNT_BINDING_MISMATCH'):
            apply_complete_windows(self.db,[(T-1000,T+1000,[bill(2)])],self.bots,NOW+HOUR,account_fingerprint=fp)
        with closing(sqlite3.connect(self.db)) as d:
            self.assertEqual(d.execute('SELECT count(*) FROM bills').fetchone()[0],1)
    def test_sigkill_before_commit_preserves_wal_snapshot(self):
        self.seed(); before=read_snapshot(self.db,MMT,self.bots[MMT]); state=read_state(self.db)
        code='''import sys,time
from okx_funding_ledger import writer
with writer(sys.argv[1]) as db:
 db.execute("UPDATE snapshot SET amount='999'")
 db.execute("UPDATE meta SET value='999' WHERE key='synced_ms'")
 db.execute("DELETE FROM bills")
 print('TRANSACTION_OPEN',flush=True)
 time.sleep(60)
'''
        with subprocess.Popen([sys.executable,'-B','-c',code,str(self.db)],stdout=subprocess.PIPE,text=True) as p:
            try:
                import select
                self.assertTrue(select.select([p.stdout],[],[],5)[0])
                self.assertEqual(p.stdout.readline().strip(),'TRANSACTION_OPEN')
                self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT]),before)
            finally:
                p.kill();p.wait(timeout=5)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT]),before)
        self.assertEqual(read_state(self.db),state)
        with closing(sqlite3.connect(self.db)) as d:
            self.assertEqual(d.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(d.execute('SELECT count(*) FROM bills').fetchone()[0],1)
        result=run_once(self.db,lambda:(self.bots,CREDS),lambda _:FakeClient([bill(1),bill(2)]),now=NOW+HOUR)
        self.assertEqual(result['inserted'],1)
    def test_lock_contention_cli_exits_without_network(self):
        import fcntl
        lock=self.root/'collector.lock'
        with lock.open('w') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            p=subprocess.run([sys.executable,'-B','okx_funding_collector.py','--base',str(self.root/'unused'), '--registry',str(self.root/'missing'), '--systemd-dir',str(self.root/'missing-units'),'--data-dir',str(self.root)],capture_output=True,text=True,timeout=5)
            self.assertEqual(p.returncode,0);self.assertIn('LOCK_BUSY',p.stdout);self.assertFalse(self.db.exists())
    def test_window_seam_no_hole_and_same_id_dedup(self):
        start=NOW-11*DAY; self.bots={MMT:start/1000}
        plan,_=plan_windows(self.bots,[],None,None,NOW,'backfill')
        self.assertEqual(plan[0][1],start)
        a=[v for v in plan if v[0]==ARCHIVE][-1];r=[v for v in plan if v[0]==RECENT][0]
        self.assertLess(r[1],a[2]);self.assertEqual(r[2],END)
        rows=[bill(1,ts=r[1]-1),bill(2,ts=r[1]),bill(3,ts=a[2]-1),bill(4,ts=a[2])]
        result=run_once(self.db,lambda:(self.bots,CREDS),lambda _:FakeClient(rows),now=NOW,mode='backfill')
        self.assertEqual(result['inserted'],4);self.assertEqual(result['duplicates'],2)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'-0.006909')
    def test_older_gap_not_falsely_claimed_complete(self):
        self.bots={MMT:(NOW-110*DAY)/1000}
        run_once(self.db,lambda:(self.bots,CREDS),lambda _:FakeClient([bill(1)]),now=NOW,mode='backfill')
        v=load_funding(self.db,MMT,self.bots[MMT],terminal=False)
        self.assertFalse(v.complete);self.assertEqual(v.amount,Decimal('-0.00172725'))
        self.assertEqual(min(a for a,b in read_state(self.db)[0]),archive_floor(NOW))
    def test_update_with_late_bill_and_reconcile(self):
        self.seed()
        run_once(self.db,lambda:(self.bots,CREDS),lambda _:FakeClient([bill(1),bill(2,ts=T-2*HOUR)]),now=NOW+HOUR)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'-0.0034545')
    def test_foreign_instruments_do_not_change_active_total(self):
        rows=[bill(1)]+[bill(i,'OTHER'+str(i)+'-USDT-SWAP',amount='100',subtype='174') for i in range(2,205)]
        result=run_once(self.db,lambda:(self.bots,CREDS),lambda _:FakeClient(rows),now=NOW,mode='backfill')
        self.assertEqual(result['requests'],3);self.assertEqual(result['inserted'],204)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'-0.00172725')
    def test_account_bound_backup_restores_and_resumes(self):
        self.seed();daily_backup(self.db)
        restored=next((self.root/'backups').glob('*.sqlite'))
        result=run_once(restored,lambda:(self.bots,CREDS),lambda _:FakeClient([bill(1),bill(2)]),now=NOW+HOUR)
        self.assertEqual(result['inserted'],1)
        self.assertEqual(read_snapshot(restored,MMT,self.bots[MMT])['amount'],'-0.0034545')
    def test_f1_shaped_synthetic_bills_cross_endpoint(self):
        fixture=Path(__file__).with_name('f1_bills_fixture.json')
        data=json.loads(fixture.read_text())
        self.assertEqual(len(data['recent']),10);self.assertEqual(len(data['archive']),10)
        self.bots={inst:(T-DAY)/1000 for inst in {r['instId'] for r in data['recent']}}
        result=apply_complete_windows(self.db,[(T-DAY,T+10000,data['recent']),(T-DAY,T+10000,data['archive'])],self.bots,NOW)
        self.assertEqual(result['inserted'],10);self.assertEqual(result['duplicates'],10)
        self.assertEqual(read_snapshot(self.db,'TEST2-USDT-SWAP',self.bots['TEST2-USDT-SWAP'])['amount'],'-0.0009876543120984')
        self.assertEqual(read_snapshot(self.db,'TEST3-USDT-SWAP',self.bots['TEST3-USDT-SWAP'])['amount'],'0.0018518518351845')

if __name__=='__main__': unittest.main()
