"""OFFLINE synthetic fixtures, no engine imports/OKX reads/live file writes."""
import json
import sqlite3
from contextlib import closing, contextmanager

@contextmanager
def write_db(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db

import tempfile
import unittest
from pathlib import Path

from okx_stats_v2_contract import (
    Drift, TableSnapshot, connected_components, freeze_readiness,
    pnl_identity, source_snapshots,
)


class V2Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'state').mkdir()
        (self.root / 'state/bot_state.json').write_text(json.dumps({
            'instrument': 'DEMO-USDT-SWAP', 'cycles': {}, 'archived_cycles': []
        }))
        for name, table, schema in (
            ('cycles.sqlite', 'cycles', 'id TEXT PRIMARY KEY,data TEXT NOT NULL'),
            ('pnl.sqlite', 'accounted', 'id TEXT PRIMARY KEY,kind TEXT NOT NULL,net TEXT NOT NULL,data TEXT NOT NULL'),
            ('fills.sqlite', 'fills', 'order_id TEXT NOT NULL,bill_id TEXT NOT NULL,qty TEXT NOT NULL,data TEXT NOT NULL, PRIMARY KEY(order_id,bill_id)'),
        ):
            with write_db(self.root/'state'/name) as db:
                db.execute(f'CREATE TABLE {table} ({schema})')

    def add(self, table, params):
        path = {'cycles':'cycles.sqlite','accounted':'pnl.sqlite','fills':'fills.sqlite'}[table]
        with write_db(self.root/'state'/path) as db:
            if table == 'cycles':
                db.execute('INSERT INTO cycles VALUES (?,?)', (params['cycleId'], json.dumps(params)))
            elif table == 'accounted':
                db.execute('INSERT INTO accounted VALUES (?,?,?,?)', (params['id'],params['kind'],params['net'],json.dumps(params)))
            else:
                db.execute('INSERT INTO fills VALUES (?,?,?,?)', (params['ordId'],params['billId'],params['fillSz'],json.dumps(params)))

    def test_append_increment_only(self):
        first = source_snapshots(self.root)['cursors']
        self.add('cycles', {'cycleId':'A','state':'CLOSED'})
        second = source_snapshots(self.root, first)['cursors']
        self.assertEqual(second['cycles']['new'], 1)
        self.assertEqual(second['fills']['new'], 0)
        third = source_snapshots(self.root, second)['cursors']
        self.assertEqual(third['cycles']['new'], 0)
        self.assertEqual(third['cycles']['chain_sha256'], second['cycles']['chain_sha256'])

    def test_anchor_change_fails_closed(self):
        self.add('cycles', {'cycleId':'A','state':'CLOSED'})
        prev = source_snapshots(self.root)['cursors']
        with write_db(self.root/'state/cycles.sqlite') as db:
            db.execute('UPDATE cycles SET data=? WHERE id=?', ('{"cycleId":"A","state":"EXITED"}', 'A'))
        with self.assertRaisesRegex(Drift, 'anchor'):
            source_snapshots(self.root, prev)

    def test_delete_and_append_detected_by_count(self):
        self.add('cycles', {'cycleId':'A','state':'CLOSED'})
        self.add('cycles', {'cycleId':'B','state':'CLOSED'})
        prev = source_snapshots(self.root)['cursors']
        with write_db(self.root/'state/cycles.sqlite') as db:
            db.execute('DELETE FROM cycles WHERE id=?', ('A',))
            db.execute('INSERT INTO cycles VALUES (?,?)', ('C','{"cycleId":"C"}'))
        with self.assertRaisesRegex(Drift, 'count'):
            source_snapshots(self.root, prev)

    def test_swapped_db_detected_by_inode(self):
        self.add('cycles', {'cycleId':'A','state':'CLOSED'})
        prev = source_snapshots(self.root)['cursors']
        old = self.root/'state/cycles.sqlite'
        replacement = self.root/'state/cycles.new'
        with write_db(replacement) as db:
            db.execute('CREATE TABLE cycles (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
            db.execute('INSERT INTO cycles VALUES (?,?)', ('A','{"cycleId":"A"}'))
        replacement.replace(old)
        with self.assertRaisesRegex(Drift, 'identity'):
            source_snapshots(self.root, prev)

    def test_future_state_wins(self):
        c = {'cycleId':'A','state':'CLOSED','ordId':'buyA','tpOrdId':'sellA',
             'filledBuy':'1','filledTP':'1'}
        ready, reason = freeze_readiness([c], {'A'}, {'A'}, {}, {})
        self.assertFalse(ready)
        self.assertEqual(reason, 'state_overrides_archive')

    def test_late_fill_blocks_and_then_unblocks(self):
        c = {'cycleId':'A','state':'CLOSED','ordId':'buyA','tpOrdId':'sellA',
             'filledBuy':'1','filledTP':'1'}
        ledger = {'buyA:sellA':{'kind':'CLOSED','net':'2'}}
        buy = {'ordId':'buyA','billId':'b1','fillSz':'1','fee':'-0.1','feeCcy':'USDT'}
        sell = {'ordId':'sellA','billId':'s1','fillSz':'1','fee':'-0.1','feeCcy':'USDT'}
        a,b = freeze_readiness([c], {'A'}, set(), {'buyA':[buy]}, ledger)
        self.assertFalse(a)
        self.assertEqual(b, 'fills_incomplete_or_overfilled')
        a,b = freeze_readiness([c], {'A'}, set(), {'buyA':[buy],'sellA':[sell]}, ledger)
        self.assertTrue(a)
        self.assertEqual(b, 'ready_for_full_component_oracle')

    def test_market_shared_requires_all_cycle_ids(self):
        plan = [{'cycleId':'A','sz':'1'},{'cycleId':'B','sz':'1'}]
        a = {'cycleId':'A','state':'CLOSED','ordId':'buyA','filledBuy':'1','filledTP':'1',
             'tpOrders':[{'ordId':'marketM','filled':'1','source':'aggregate_exit','aggregatePlan':plan}]}
        b = {'cycleId':'B','state':'CLOSED','ordId':'buyB','filledBuy':'1','filledTP':'1',
             'tpOrders':[{'ordId':'marketM','filled':'1','source':'aggregate_exit','aggregatePlan':plan}]}
        self.assertEqual(len(connected_components([a,b])), 1)
        self.assertEqual(freeze_readiness([a], {'A'}, set(), {}, {})[1], 'accounted_missing_or_wrong_kind')
        ledger = {'buyA:marketM':{'kind':'CLOSED','net':'1'},'buyB:marketM':{'kind':'CLOSED','net':'1'}}
        self.assertEqual(freeze_readiness([a], {'A'}, set(), {}, ledger)[1], 'aggregate_plan_incomplete')
        def fill(oid,bill,qty):
            return {'ordId':oid,'billId':bill,'fillSz':qty,'fee':'-0.01','feeCcy':'USDT'}
        rows = {'buyA':[fill('buyA','1','1')],'buyB':[fill('buyB','2','1')],
                'marketM':[fill('marketM','3','2')]}
        self.assertTrue(freeze_readiness([a,b], {'A','B'}, set(), rows, ledger)[0])

    def test_chain_ledger_key(self):
        c = {'ordId':'B','tpOrders':[{'ordId':'S1','filled':'1'},{'ordId':'S2','filled':'1'}]}
        self.assertEqual(pnl_identity(c), 'chain:B')

    def test_cancel_without_fills_is_terminal(self):
        c = {'cycleId':'A','state':'CANCELED','filledBuy':'0'}
        self.assertEqual(freeze_readiness([c], {'A'}, set(), {}, {}),
                         (True,'ready_for_full_component_oracle'))

    def test_no_source_writes(self):
        before = [p.stat().st_mtime_ns for p in sorted((self.root/'state').iterdir())]
        source_snapshots(self.root)
        after = [p.stat().st_mtime_ns for p in sorted((self.root/'state').iterdir())]
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
