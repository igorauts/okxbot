"""Offline finance snapshot contract: no exchange or trading imports."""
import ast
import contextlib
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

ENGINE = Path(__file__).resolve().parent.parent / 'okx_bot.py'

def method_source(parent, name):
    source=ENGINE.read_text(encoding='utf-8')
    body=ast.parse(source).body
    if parent is not None:
        body=next(n for n in body if isinstance(n,ast.ClassDef) and n.name==parent).body
    node=next(n for n in body if isinstance(n,ast.FunctionDef) and n.name==name)
    return ast.get_source_segment(source,node)

class FinanceSnapshotContract(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.rows={
            'a':[{'ordId':'a','billId':'a1','fillSz':'1','fee':'-0.001'}],
            'b':[{'ordId':'b','billId':'b1','fillSz':'0.4','fee':'-0.0004'}]
        }
        self.all_reads=[]
        self.perf=type('Perf',(),{'last':{}})()
        self.api=type('API',(),{'fills_by_order':self.recover})()
        src=method_source('FillStore','ensure_expected')
        namespace={'time':time,'Decimal':Decimal,'D':lambda x:Decimal(str(x)),'A':self.api}
        code='class Isolated:\n'+''.join('    '+line+'\n' for line in src.splitlines())
        exec(compile(code,'<production-function>','exec'),namespace)
        self.subject=namespace['Isolated']()
        self.subject.perf=self.perf
        self.subject.get_rows=self.get_rows

    def get_rows(self,ids):
        keys=list(ids)
        self.all_reads.append(keys)
        return [dict(r) for oid in keys for r in self.rows.get(str(oid),[])]

    def recover(self,oid):
        self.calls.append(str(oid))
        if oid=='b':
            self.rows['b'].append({'ordId':'b','billId':'b2','fillSz':'0.6','fee':'-0.0006'})

    def test_refresh_only_missing_and_unique_bill_ids(self):
        result=self.subject.ensure_expected({'a':Decimal(1),'b':Decimal(1)})
        self.assertEqual(self.all_reads,[['a','b'],['b']])
        self.assertEqual(self.calls,['b'])
        self.assertEqual(len(result),3)
        self.assertEqual(len({x['billId'] for x in result}),3)
        self.assertEqual(sum(Decimal(x['fee']) for x in result),Decimal('-0.002'))
        self.assertEqual(sum(Decimal(x['fillSz']) for x in result),Decimal(2))

    def test_complete_history_does_not_retry(self):
        result=self.subject.ensure_expected({'a':Decimal(1)})
        self.assertEqual(len(result),1)
        self.assertEqual(len(self.all_reads),1)
        self.assertFalse(self.calls)

    def test_failure_from_exchange_is_not_silenced(self):
        def raise_error(oid):raise RuntimeError('mock network failure')
        self.api.fills_by_order=raise_error
        with self.assertRaisesRegex(RuntimeError,'mock network failure'):
            self.subject.ensure_expected({'b':Decimal(3)})

    def test_archive_reads_without_ddl(self):
        archive_source=method_source(None,'archived_rows')
        with tempfile.TemporaryDirectory() as tmp:
            dbfile=Path(tmp)/'archive.sqlite'
            with sqlite3.connect(dbfile) as db:
                db.execute('CREATE TABLE cycles(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
                db.execute('INSERT INTO cycles VALUES (?,?)',('1',json.dumps({'cycleId':'1'})))
            env={'ARCHIVE_DB':dbfile,'contextlib':contextlib,'sqlite3':sqlite3,'json':json}
            exec(compile(archive_source,'<archive-function>','exec'),env)
            self.assertEqual(env['archived_rows'](),[{'cycleId':'1'}])
            with sqlite3.connect(dbfile) as db:
                db.execute('DROP TABLE cycles')
            with self.assertRaises(sqlite3.OperationalError):
                env['archived_rows']()
            with sqlite3.connect(dbfile) as db:
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='cycles'").fetchone())

if __name__=='__main__':unittest.main()
