#!/usr/bin/env python3
"""Pure tests for incremental fill identity and append-only snapshots."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

SRC=Path(__file__).resolve().parents[1]/'okx_stats_incremental_core.py'
spec=importlib.util.spec_from_file_location('incremental_core', SRC)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class TestIndex(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/'fills.sqlite'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE fills (order_id TEXT NOT NULL, bill_id TEXT NOT NULL, qty TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(order_id,bill_id))')
        self.index=m.IncrementalFills(self.db,'TEST-USDT-SWAP')
    def tearDown(self):self.temp.cleanup()
    def put(self,oid,bill,qty='1',side='buy',fee='-0.01'):
        x={'ordId':oid,'billId':bill,'tradeId':bill,'instId':'TEST-USDT-SWAP',
           'side':side,'execType':'M','feeCcy':'USDT','fee':fee,
           'fillPx':'100','fillSz':qty}
        with sqlite3.connect(self.db) as db:
            db.execute('INSERT INTO fills VALUES (?,?,?,?)',(oid,bill,qty,json.dumps(x)))
    def test_empty(self):
        self.assertEqual(self.index.refresh()['count'],0)
        self.assertEqual(self.index.matching_rows({}),[])
    def test_prime_and_delta(self):
        self.put('a','1')
        self.assertEqual(self.index.refresh()['mode'],'full')
        self.assertEqual(self.index.refresh()['new'],0)
        self.put('b','2','2')
        self.assertEqual(self.index.refresh()['new'],1)
        self.assertEqual(len(self.index.matching_rows({'a':1,'b':2})),2)
    def test_partial_same_order(self):
        self.put('a','1','.25');self.index.refresh()
        self.put('a','2','.75');self.index.refresh()
        self.assertEqual(len(self.index.matching_rows({'a':1})),2)
    def test_missing_fails_closed(self):
        self.put('a','1','.25');self.index.refresh()
        with self.assertRaises(m.Incomplete):self.index.matching_rows({'a':1})
    def test_overfill_delegated(self):
        self.put('a','1','2');self.index.refresh()
        self.assertEqual(len(self.index.matching_rows({'a':1})),1)
    def test_new_copy_is_immutable_to_cache(self):
        self.put('a','1');self.index.refresh()
        self.index.matching_rows({'a':1})[0]['fee']='99999'
        self.assertEqual(self.index.matching_rows({'a':1})[0]['fee'],'-0.01')
    def test_truncation_fails(self):
        self.put('a','1');self.index.refresh()
        with sqlite3.connect(self.db) as db:db.execute('DELETE FROM fills')
        with self.assertRaises(m.Drift):self.index.refresh()
    def test_same_count_replacement_detected(self):
        self.put('a','1');self.index.refresh()
        with sqlite3.connect(self.db) as db:
            db.execute('DELETE FROM fills')
            db.execute('INSERT INTO fills VALUES (?,?,?,?)',('q','1','1',json.dumps({
                'ordId':'q','billId':'1','tradeId':'1','instId':'TEST-USDT-SWAP',
                'side':'buy','execType':'M','feeCcy':'USDT','fee':'-0.01','fillPx':'100','fillSz':'1'})))
        # Even if rowid count/highest are unchanged, check the prior tail.
        with self.assertRaises(m.Drift):self.index.refresh()
    def test_untrusted_bad_fee(self):
        self.put('a','1',fee='NaN')
        with self.assertRaises(m.Drift):self.index.refresh()
    def test_symlink_rejected(self):
        alias=Path(self.temp.name)/'alias'
        alias.symlink_to(self.db)
        with self.assertRaises(m.Drift):m.IncrementalFills(alias,'TEST-USDT-SWAP').refresh()
    def test_foreign_instrument_rejected(self):
        self.put('a','1')
        with self.assertRaises(m.Drift):m.IncrementalFills(self.db,'OTHER-USDT-SWAP').refresh()
    def test_invalid_qty_rejected(self):
        self.put('a','1')
        self.index.refresh()
        with self.assertRaises(m.Drift):self.index.matching_rows({'a':'NaN'})

if __name__=='__main__':unittest.main(verbosity=1)
