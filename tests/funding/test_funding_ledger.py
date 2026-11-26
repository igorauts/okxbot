import sqlite3
import tempfile
import unittest
from pathlib import Path
from okx_funding_ledger import apply_complete_windows, read_snapshot, creation_ms, LedgerError

T = 1795536000000
MMT = 'MMT-USDT-SWAP'
ETH = 'ETH-USDT-SWAP'

def bill(i, inst=MMT, ts=T, amount='-0.00172725', subtype='173', **kw):
    return dict(billId=str(i), instId=inst, ts=str(ts), pnl=amount, balChg=amount,
                type='8', subType=subtype, ccy='USDT', mgnMode='cross', **kw)

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'funding.sqlite'
        self.inv = {MMT: (T-1000)/1000, ETH: (T-1000)/1000}
    def apply(self, rows, begin=T-1000, end=T+1000, inv=None, synced=T+2000):
        return apply_complete_windows(self.path, [(begin,end,rows)], self.inv if inv is None else inv, synced)
    def snap(self, inst=MMT):
        return read_snapshot(self.path, inst, self.inv[inst])
    def test_precision_and_sign(self):
        self.apply([bill(1, amount='-0.000000000000000001'),bill(2, amount='0.1774310361330178',subtype='174')])
        self.assertEqual(self.snap()['amount'],'0.177431036133017799')
    def test_decimal_integer_and_fraction_width(self):
        big = "9"*120
        tiny = "0." + "0"*119 + "1"
        self.apply([bill(1,amount=big,subtype="174"),bill(2,amount=tiny,subtype="174")])
        self.assertEqual(self.snap()["amount"],big+"."+"0"*119+"1")
    def test_duplicate_cross_endpoints_and_same_time(self):
        a=bill(1); b=dict(a,balChg='-0.0017272500000000')
        self.assertEqual(self.apply([a,b,bill(2,ETH)])['duplicates'],1)
        self.assertEqual(self.apply([a,bill(2,ETH)],synced=T+3000)['inserted'],0)
        self.assertEqual(self.snap()['amount'],'-0.00172725')
    def test_new_bot_same_instrument_and_foreign(self):
        self.apply([bill(1,ts=T-500),bill(2,ts=T+500),bill(3,'SOL-USDT-SWAP')],inv={MMT:T/1000})
        self.assertIsNone(self.snap())
        self.assertEqual(read_snapshot(self.path,MMT,T/1000)['amount'],'-0.00172725')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bills').fetchone()[0],3)
    def test_archived_disappears_from_snapshot_only(self):
        self.apply([bill(1),bill(2,ETH)])
        self.apply([],inv={ETH:self.inv[ETH]},synced=T+3000)
        self.assertIsNone(self.snap())
        self.assertEqual(self.snap(ETH)['amount'],'-0.00172725')
    def test_conflicting_id_blocks_only_instrument(self):
        self.apply([bill(1)])
        before=self.snap()
        r=self.apply([bill(2,ETH),bill(1,amount='-9')],synced=T+3000)
        self.assertEqual(r['blocked'],1);self.assertEqual(self.snap(ETH)['amount'],'-0.00172725')
        self.assertEqual(self.snap(),before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bills').fetchone()[0],2)
    def test_pnl_balchg_mismatch(self):
        self.apply([bill(1)])
        before=self.snap()
        r=self.apply([dict(bill(2),balChg='0')],synced=T+3000)
        self.assertEqual(r['quarantined'],1)
        self.assertEqual(self.snap(),before)
    def test_sign_mismatch(self):
        r=self.apply([bill(1,amount='0.01')]);self.assertEqual(r['quarantined'],1)
        self.assertIsNone(self.snap())
    def test_coverage_gap_then_repair(self):
        self.apply([bill(1)],begin=T,end=T+1000)
        self.assertEqual(self.snap()['complete'],0)
        self.apply([],begin=T-1000,end=T,synced=T+3000)
        self.assertEqual(self.snap()['complete'],1)
        self.assertEqual(self.snap()['amount'],'-0.00172725')
    def test_empty_verified_window(self):
        self.apply([])
        self.assertEqual(self.snap()['amount'],'0')
        self.assertEqual(self.snap()['complete'],1)
        self.assertIsNone(self.snap()['last_bill_ms'])
    def test_exact_cutoff(self):
        self.assertEqual(creation_ms('1795536000.0001'),T+1)
        self.apply([bill(1,ts=T),bill(2,ts=T+1)],inv={MMT:'1795536000.0001'})
        self.assertEqual(read_snapshot(self.path,MMT,'1795536000.0001')['amount'],'-0.00172725')
    def test_stale_generation(self):
        self.apply([bill(1)],synced=T+4000)
        before=self.snap()
        with self.assertRaisesRegex(LedgerError,'STALE_GENERATION'):
            self.apply([],synced=T+3000)
        self.assertEqual(self.snap(),before)
    def test_outside_window(self):
        with self.assertRaisesRegex(LedgerError,'BILL_OUTSIDE_WINDOW'):
            self.apply([bill(1,ts=T+1000)])
    def test_stopped_bot_remains_when_inventory_contains_it(self):
        self.apply([bill(1)])
        self.apply([bill(2)],synced=T+3000)
        self.assertEqual(self.snap()['amount'],'-0.0034545')
    def test_no_coverage_without_windows(self):
        with self.assertRaisesRegex(LedgerError,'NO_COMPLETE_WINDOWS'):
            apply_complete_windows(self.path,[],self.inv,T+2000)
        self.assertFalse(self.path.exists())

if __name__ == '__main__': unittest.main()
