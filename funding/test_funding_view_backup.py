import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from okx_funding_ledger import apply_complete_windows, read_snapshot, LedgerError
from okx_funding_view import load_funding
from okx_funding_backup import daily_backup
from test_funding_ledger import T, MMT, bill

class ViewBackupTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.db=self.root/'funding.sqlite'
        self.created=(T-1000)/1000;self.inv={MMT:self.created}
        self.now=datetime.fromtimestamp(T/1000,timezone.utc)
    def seed(self, rows=None, begin=T-1000, synced=T+2000):
        return apply_complete_windows(self.db,[(begin,T+1000,rows if rows is not None else [bill(1,amount='-4.829310')])],self.inv,synced)
    def view(self, terminal=False, created=None):
        return load_funding(self.db,MMT,self.created if created is None else created,terminal=terminal)
    def test_stale_never_expires(self):
        self.seed();v=self.view();self.assertEqual(v.line(v.synced_ms+(17*3600+5*60+12)*1000),'FUNDING: −4.829310, 17ч 05м 12с назад')
        self.assertEqual(v.add_to(Decimal('65')),Decimal('60.170690'))
    def test_4h12m30s_age_is_sync_not_bill(self):
        self.seed();v=self.view();self.assertEqual(v.line(v.synced_ms+(4*3600+12*60+30)*1000),'FUNDING: −4.829310, 4ч 12м 30с назад')
    def test_age_changes_without_io(self):
        self.seed();v=self.view()
        with patch('sqlite3.connect',side_effect=AssertionError('unexpected IO')):
            self.assertEqual(v.add_to(Decimal('1')),Decimal('-3.829310'))
            self.assertEqual(v.age_text(v.synced_ms+1000),'0ч 00м 01с назад')
    def test_terminal_does_not_open_db(self):
        self.seed()
        with patch('sqlite3.connect',side_effect=AssertionError('archive read')):
            self.assertIsNone(self.view(terminal=True))
    def test_missing_not_invented_zero(self):
        self.assertIsNone(self.view());self.assertFalse(self.db.exists())
        self.seed();self.assertIsNone(self.view(created=T/1000))
    def test_incomplete_known_amount_is_still_returned(self):
        self.seed(begin=T);v=self.view();self.assertFalse(v.complete)
        self.assertEqual(v.amount,Decimal('-4.829310'))
    def test_confirmed_zero_no_payment(self):
        self.seed([]);v=self.view();self.assertEqual(v.amount,0);self.assertIsNone(v.last_bill_ms)
        self.assertEqual(v.line(v.synced_ms),'FUNDING: +0.000000, 0ч 00м 00с назад')
    def test_aggregate_wider_than_source(self):
        self.seed([bill(1,amount='9'*120,subtype='174'),bill(2,amount='0.'+'0'*119+'1',subtype='174')]);v=self.view()
        self.assertEqual(str(v.amount),'9'*120+'.'+'0'*119+'1')
    def test_backup_contains_wal_generation(self):
        self.seed()
        with closing(sqlite3.connect(self.db)) as hold:
            hold.execute('SELECT * FROM bills').fetchall()
            self.seed([bill(2,amount='-1')],synced=T+3000)
            self.assertTrue(Path(str(self.db)+'-wal').exists())
            self.assertEqual(daily_backup(self.db,now=self.now),'CREATED')
        dest=self.root/'backups'/f'funding-{self.now:%Y-%m-%d}.sqlite'
        self.assertEqual(read_snapshot(dest,MMT,self.created)['amount'],'-5.82931')
        self.assertEqual(dest.stat().st_mode & 0o777,0o600)
        self.assertEqual(daily_backup(self.db,now=self.now),'ALREADY_DONE')
        self.assertEqual(daily_backup(self.db,now=self.now+timedelta(days=1)),'CREATED')
        self.assertEqual(len(list(dest.parent.glob('*.sqlite'))),2)
    def test_backup_failure_preserves_existing_and_removes_temp(self):
        self.seed();daily_backup(self.db,now=self.now)
        dest=next((self.root/'backups').glob('*.sqlite'));before=dest.read_bytes()
        with patch('okx_funding_backup.os.replace',side_effect=OSError('test')):
            with self.assertRaises(OSError):daily_backup(self.db,now=self.now+timedelta(days=1))
        self.assertEqual(dest.read_bytes(),before)
        self.assertEqual(list(dest.parent.iterdir()),[dest]);self.assertEqual(self.view().amount,Decimal('-4.829310'))
    def test_backup_no_db_and_symlink(self):
        self.assertEqual(daily_backup(self.db,now=self.now),'NO_DATABASE');self.assertFalse((self.root/'backups').exists())
        self.seed();(self.root/'backups').symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(LedgerError,'BACKUP_DIR_INVALID'):daily_backup(self.db,now=self.now)
    def test_backup_corrupt_source_rejected(self):
        self.db.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):daily_backup(self.db,now=self.now)
        self.assertFalse(list((self.root/'backups').iterdir()))

if __name__=='__main__': unittest.main()
