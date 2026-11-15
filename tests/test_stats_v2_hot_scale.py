"""Stats v2 throughput and correctness regressions: synthetic temp SQLite only.

No imports of executable OKX engine, network, Telegram, or LIVE directories.
"""
import json
import math
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from contextlib import closing
from pathlib import Path
from unittest import mock

from okx_stats_v2_contract import NotReady, TableSnapshot
from okx_stats_v2_production import SingleAuthorityStats
from tests.stress_stats_v2_rebuild import O, NOW, INST, fill


def make_db(path, ddl):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(ddl)


class LocalStore:
    def __init__(self, path):
        self.path = path
        self.api_calls = 0
        self.local_calls = 0

    def get_rows(self, ids):
        self.local_calls += 1
        ids = list(ids)
        if not ids:
            return []
        with closing(sqlite3.connect(self.path)) as db, db:
            placeholders = ','.join('?' for _ in ids)
            return [json.loads(raw) for (raw,) in db.execute(
                'SELECT data FROM fills WHERE order_id IN (' + placeholders + ')', ids)]

    def ensure_expected(self, expected):
        self.api_calls += 1
        raise NotReady('EXCHANGE_UNAVAILABLE_SYNTHETIC')


class HotScaleTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix='offline-stats-hot-')
        self.addCleanup(t.cleanup)
        self.root = Path(t.name) / 'bot'
        state = self.root / 'state'
        state.mkdir(parents=True)
        make_db(state / 'cycles.sqlite',
                'CREATE TABLE cycles(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        make_db(state / 'fills.sqlite',
                'CREATE TABLE fills(order_id TEXT NOT NULL, bill_id TEXT NOT NULL, '
                'qty TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(order_id,bill_id))')
        make_db(state / 'pnl.sqlite',
                'CREATE TABLE accounted(id TEXT PRIMARY KEY,kind TEXT NOT NULL,'
                'net TEXT NOT NULL,data TEXT NOT NULL)')
        self.path = state
        self.fill_store = LocalStore(state / 'fills.sqlite')
        self.write_state({})

    def write_state(self, cycles):
        (self.path / 'bot_state.json').write_text(json.dumps({
            'instrument': INST, 'cycles': cycles, 'archived_cycles': [],
            'completed': 0}))

    def write_fill(self, row):
        with closing(sqlite3.connect(self.path / 'fills.sqlite')) as db, db:
            db.execute('INSERT INTO fills VALUES (?,?,?,?)', (
                row['ordId'], row['billId'], row['fillSz'], json.dumps(row)))

    def worker(self, oracle=None, fill_store=None):
        worker = SingleAuthorityStats(
            self.root, INST, self.path / 'stats-v2.sqlite',
            oracle=oracle or O, fill_store=fill_store or self.fill_store,
            expected_filled_orders=lambda st, **kw: {
                str(c['ordId']): Decimal(str(c['filledBuy']))
                for c in st['cycles'].values() if Decimal(str(c['filledBuy'])) > 0},
            contract_value='1', freeze_budget=64)
        self.addCleanup(worker.close)
        return worker

    def test_local_fill_after_pin_repins_and_publishes_without_api(self):
        cycle={'cycleId':'hot', 'state':'TP_WAIT', 'ordId':'new-buy',
               'filledBuy':'1','filledTP':'0','tpOrders':[]}
        self.write_state({'0.01':cycle})
        called=[0]
        oracle=dict(O)
        original=O['full_stats_snapshot']

        def after_pin(st, rows, bid, cv, **kwargs):
            result=original(st, rows, bid, cv, **kwargs)
            called[0] += 1
            if called[0] == 1:
                self.assertFalse(result['complete'])
                self.write_fill(fill('new-buy','bill-new',1,'buy',100))
            return result

        oracle['full_stats_snapshot']=after_pin
        worker=self.worker(oracle)
        result=worker.compute('110',now=NOW/1000)
        self.assertTrue(result.stats['complete'])
        self.assertEqual(result.hot_cycles,1)
        self.assertGreaterEqual(called[0],2)
        self.assertEqual(self.fill_store.api_calls,0)
        self.assertEqual(result.stats['open_contracts'],Decimal('1'))
        self.assertEqual(worker.stager._cursor('fills')['count'],1)

    def test_real_missing_still_calls_original_fail_closed_recovery(self):
        cycle={'cycleId':'hot', 'state':'TP_WAIT', 'ordId':'never-filled',
               'filledBuy':'1','filledTP':'0','tpOrders':[]}
        self.write_state({'0.01':cycle})
        worker=self.worker()
        with self.assertRaisesRegex(NotReady,'EXCHANGE_UNAVAILABLE_SYNTHETIC'):
            worker.compute('110',now=NOW/1000)
        self.assertEqual(self.fill_store.api_calls,1)

    def test_repin_is_bounded_even_when_oracle_cannot_complete(self):
        cycle={'cycleId':'hot', 'state':'TP_WAIT', 'ordId':'already-local',
               'filledBuy':'1','filledTP':'0','tpOrders':[]}
        self.write_state({'0.01':cycle})
        self.write_fill(fill('already-local','bill-h',1,'buy',100))
        oracle=dict(O)
        call_count=[0]
        def oracle_never_complete(*args,**kwargs):
            call_count[0] +=1
            return {'complete':False, 'missing':[
                ('already-local',Decimal('1'),Decimal(0))]}
        oracle['full_stats_snapshot']=oracle_never_complete
        worker=self.worker(oracle)
        with self.assertRaisesRegex(NotReady,'HOT local fills arrived'):
            worker.compute('110',now=NOW/1000)
        self.assertEqual(call_count[0],2)
        self.assertEqual(self.fill_store.api_calls,0)
        # A continuing inconsistency MUST reach original fill recovery;
        # the local shortcut may not mask a permanent fault forever.
        with self.assertRaisesRegex(NotReady,'EXCHANGE_UNAVAILABLE_SYNTHETIC'):
            worker.compute('110',now=NOW/1000)
        self.assertEqual(self.fill_store.api_calls,1)

    def test_overfull_new_local_wal_cannot_publish_old_money(self):
        worker=self.worker()
        self.write_fill(fill('x','bill-x',2,'buy',100))
        worker.expected_filled_orders=lambda st,**kw:{'x':Decimal('1')}
        with self.assertRaisesRegex(NotReady,'EXCHANGE_UNAVAILABLE_SYNTHETIC'):
            worker._recover_hot_fills({},missing_sample=[('x',Decimal('1'),Decimal(0))])
        self.assertEqual(self.fill_store.api_calls,1)

    def test_ledger_batching_and_exact_parity_through_1000_keys(self):
        count=1003
        cycles={str(i):{'cycleId':str(i), 'state':'CLOSED',
                        'ordId':f'b{i}', 'tpOrdId':f's{i}'} for i in range(count)}
        with closing(sqlite3.connect(self.path / 'pnl.sqlite')) as db, db:
            db.executemany('INSERT INTO accounted VALUES (?,?,?,?)', [
                (f'b{i}:s{i}','CLOSED','1',json.dumps({'net':'1'}))
                for i in range(count)])
        trace=[]
        class Counted(TableSnapshot):
            def __enter__(self):
                x=super().__enter__()
                self.db.set_trace_callback(trace.append)
                return x
        with TableSnapshot(self.path / 'pnl.sqlite','accounted') as db:
            cursor=db.incremental_cursor(None)
        worker=self.worker()
        with mock.patch('okx_stats_v2_production.TableSnapshot', Counted):
            worker._check_hot_ledger(cycles,{
                'grid_gain':Decimal(count),'partial_exit_net':Decimal(0)},cursor)
        selects=[q for q in trace if q.startswith('SELECT id,data FROM accounted')]
        self.assertEqual(len(selects),math.ceil(count/200))
        # A missing journal row must never silently contribute zero.
        with closing(sqlite3.connect(self.path / 'pnl.sqlite')) as db, db:
            db.execute('DELETE FROM accounted WHERE id=?',('b444:s444',))
        with TableSnapshot(self.path / 'pnl.sqlite','accounted') as db:
            newer=db.incremental_cursor(None)
        with self.assertRaisesRegex(NotReady,'HOT PnL ledger row not committed'):
            worker._check_hot_ledger(cycles,{
                'grid_gain':Decimal(count),'partial_exit_net':Decimal(0)},newer)

    def test_ledger_wrong_money_still_fails_closed(self):
        cycles={'1':{'cycleId':'1','state':'CLOSED','ordId':'b1','tpOrdId':'s1'}}
        with closing(sqlite3.connect(self.path / 'pnl.sqlite')) as db, db:
            db.execute('INSERT INTO accounted VALUES (?,?,?,?)',
                       ('b1:s1','CLOSED','1',json.dumps({'net':'1'})))
        with TableSnapshot(self.path / 'pnl.sqlite','accounted') as db:
            cursor=db.incremental_cursor(None)
        worker=self.worker()
        from okx_stats_v2_contract import Drift
        with self.assertRaisesRegex(Drift,'HOT ledger'):
            worker._check_hot_ledger(cycles,{
                'grid_gain':Decimal('2'), 'partial_exit_net':Decimal('0')},cursor)

    def test_hot_gap_diagnostics_throttled_and_private(self):
        worker=SingleAuthorityStats.__new__(SingleAuthorityStats)
        worker.stager=mock.Mock()
        worker.stager._cursor.return_value={'rowid':123}
        worker.stager.pending_count.return_value=0
        hot={'missing':[('sensitive-order-id',Decimal('1'),Decimal(0))],
             'fill_orders_expected':54,'fill_orders_seen':38}
        with mock.patch('okx_stats_v2_production.time.monotonic',side_effect=[1000,1100,1301]), \
                mock.patch('builtins.print') as out:
            worker._trace_hot_gap(hot,{'a':{},'b':{}})
            worker._trace_hot_gap(hot,{'a':{},'b':{}})
            worker._trace_hot_gap(hot,{'a':{},'b':{}})
        self.assertEqual(out.call_count,2)
        for args,kwargs in out.call_args_list:
            self.assertEqual(args[0],'STATS_V2_HOT_GAP')
            msg=json.loads(args[1])
            self.assertEqual(msg['pinned_fill_rowid'],123)
            self.assertEqual(msg['missing_sample'],1)
            self.assertEqual(msg['fill_orders_expected'],54)
            self.assertNotIn('sensitive-order-id',args[1])


if __name__=='__main__':
    unittest.main()
