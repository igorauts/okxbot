"""OFFLINE only, no engine import, network, LIVE paths or exchange credentials.

Loads actual source functions by AST and tests durable 15-min observation,
aggregate/TP protections, market component finance parity, crash/restart.
"""
import ast
import contextlib
import gzip
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def extract(functions, *, file='okx_bot.py', other=None):
    tree=ast.parse((ROOT/file).read_text())
    selected=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
    assert {n.name for n in selected}==set(functions)
    env={'time':time, 'Decimal':Decimal, 'D':lambda x:Decimal(str(x)),
         'os':os, 'sqlite3':sqlite3,'json':json,'contextlib':contextlib}
    if other:env.update(other)
    exec(compile(ast.Module(body=selected,type_ignores=[]),file,'exec'),env)
    return env


def terminal(cid, *, status='CLOSED', amount='1', phase='FILLED', aggregate=None):
    return {'state':status,'cycleId':cid,'ordId':'BUY-'+cid,
            'clOrdId':'B-'+cid,'buyPx':'0.009',
            'filledBuy':amount,'filledTP':amount,
            'tpOrders':[{'ordId': 'MARKET' if aggregate else 'TP-'+cid,
                         'clOrdId':'T-'+cid,
                         'filled':amount, 'phase':phase,
                         'source':'aggregate_exit' if aggregate else 'attached',
                         **({'aggregatePlan':aggregate} if aggregate else {})}]}


class EngineEagerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.events=[]
        self.env=extract(['eager_archive_terminals'],other={
            'save':lambda st:self.events.append(json.loads(json.dumps(st))),
            'TERMINAL_ARCHIVE_GRACE_SEC':900})
        self.migrate=self.env['eager_archive_terminals']
        self.pnl=mock.Mock()
        self.pnl.recorded.return_value=True

    def test_first_seen_then_exact_grace_persisted_once(self):
        c=terminal('a')
        s={'cycles':{'0.009':c},'archived_cycles':[]}
        self.assertEqual(self.migrate(s,self.pnl,[],now=1000),0)
        self.assertEqual(s['terminal_seen'],{'a':1000.0})
        self.assertEqual(len(self.events),1)
        self.assertEqual(self.migrate(s,self.pnl,[],now=1899.99),0)
        self.assertEqual(len(self.events),1)
        self.assertEqual(self.migrate(s,self.pnl,[],now=1900),1)
        self.assertEqual(len(self.events),2)
        self.assertEqual(s['cycles'],{})
        self.assertEqual(s['archived_cycles'],[{'level':'0.009',**c}])
        self.assertNotIn('terminal_seen',s)
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        self.assertEqual(len(self.events),2)
        self.pnl.recorded.assert_called_once_with(c)

    def test_terminal_seen_does_not_change_financial_signature_or_cycle(self):
        import hashlib
        fn=extract(['financial_signature'],other={'hashlib':hashlib})['financial_signature']
        cycle=terminal('a')
        st={'cycles':{'0.09':cycle},'archived_cycles':[]}
        before=fn(st)
        payload=json.dumps(cycle,sort_keys=True)
        self.assertEqual(self.migrate(st,self.pnl,[],now=1000),0)
        self.assertEqual(fn(st),before)
        self.assertEqual(json.dumps(cycle,sort_keys=True),payload)
        self.assertEqual(self.migrate(st,self.pnl,[],now=2000),1)
        self.assertEqual(json.dumps(cycle,sort_keys=True),payload)

    def test_new_terminal_epoch_after_reopened_cycle(self):
        c=terminal('a')
        s={'cycles':{'0.09':c},'archived_cycles':[]}
        self.assertEqual(self.migrate(s,self.pnl,[],now=1000),0)
        c['state']='TP_WAIT'
        self.assertEqual(self.migrate(s,self.pnl,[],now=1100),0)
        self.assertNotIn('terminal_seen',s)
        c['state']='CLOSED'
        self.assertEqual(self.migrate(s,self.pnl,[],now=1850),0)
        self.assertEqual(s['terminal_seen'],{'a':1850.0})
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        self.assertEqual(self.migrate(s,self.pnl,[],now=2750),1)

    def test_fail_closed_unknown_aggregate_and_collection_shape(self):
        c=terminal('a')
        s={'cycles':{'0.09':c}, 'archived_cycles':[],
           'terminal_seen':{'a':1000}, 'aggregate_exits':[{'phase':'PLACING'}]}
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        self.assertEqual(s['cycles']['0.09'],c)
        s['aggregate_exits']=[]
        s['escape_collection']={'cycles':{'invalid':'structure'}}
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        s.pop('escape_collection')
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),1)

    def test_corrupted_observation_timestamp_never_archives(self):
        c=terminal('a')
        for invalid in ('yesterday',0,-1,float('nan')):
            with self.subTest(invalid=repr(invalid)):
                s={'cycles':{'0.09':c},'archived_cycles':[],
                   'terminal_seen':{'a':invalid}}
                with self.assertRaises(RuntimeError):
                    self.migrate(s,self.pnl,[],now=2000)
                self.assertEqual(len(s['cycles']),1)
                self.assertEqual(s['archived_cycles'],[])

    def test_observed_timestamp_survives_reboot(self):
        state={'cycles':{'0.09':terminal('a')},'archived_cycles':[]}
        self.migrate(state,self.pnl,[],now=1000)
        rebooted=json.loads(json.dumps(state))
        self.assertEqual(self.migrate(rebooted,self.pnl,[],now=1500),0)
        self.assertEqual(self.migrate(rebooted,self.pnl,[],now=1900),1)

    def test_actual_exchange_order_identity_protects_cycle(self):
        c=terminal('a')
        s={'cycles':{'0.09':c},'archived_cycles':[],'terminal_seen':{'a':1000}}
        c.update({'tpOrdId':'TP-top-level-a', 'tpClOrdId':'ATTACH-top-level-a'})
        for matching in ('ordId','clOrdId','tpOrdId','tpClOrdId'):
            with self.subTest(matching=matching):
                exchange_key={'ordId':'ordId','clOrdId':'clOrdId',
                              'tpOrdId':'ordId','tpClOrdId':'clOrdId'}[matching]
                self.assertEqual(self.migrate(s,self.pnl,[{exchange_key: c[matching]}],now=2000),0)
                self.assertIn('0.09',s['cycles'])
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),1)

    def test_partial_manual_and_tp_phases_guarded(self):
        for state,changes in (
            ('CANCELED',{}),('CLOSED',{'partial_cleanup':True}),
            ('CLOSED',{'tpFailureManualReview':'UNKNOWN'}),
            ('CLOSED',{'filledTP':'0'}),
        ):
            with self.subTest(state=state,changes=changes):
                c=terminal('c',status=state)
                c.update(changes)
                s={'cycles':{'0.01':c},'archived_cycles':[],
                   'terminal_seen':{'c':1000}}
                self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
                self.assertEqual(s['cycles']['0.01'],c)
        for phase in ('PENDING','PLACING','ACTIVE','CANCELING','FAILED'):
            with self.subTest(phase=phase):
                c=terminal('c',phase=phase)
                s={'cycles':{'0.01':c},'archived_cycles':[],
                   'terminal_seen':{'c':1000}}
                self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)

    def test_nonterminal_aggregate_references_cannot_be_archived(self):
        c=terminal('c')
        s={'cycles':{'0.01':c},'archived_cycles':[],
           'terminal_seen':{'c':1000},
           'aggregate_exits':[{'phase':'ACTIVE', 'plan':[{'cycleId':'c','sz':'1'}]}]}
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        s['aggregate_exits'][0]['phase']='FILLED'
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),1)

    def test_escape_collection_both_lists_protect_parents(self):
        c=terminal('c')
        s={'cycles':{'0.01':c},'archived_cycles':[],
           'terminal_seen':{'c':1000},
           'escape_collection':{'cycles':[], 'initialCycleIds':['c']}}
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        s.pop('escape_collection')
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),1)

    def test_no_ledger_no_archive(self):
        self.pnl.recorded.return_value=False
        s={'cycles':{'0.01':terminal('a')},'archived_cycles':[],
           'terminal_seen':{'a':1000}}
        self.assertEqual(self.migrate(s,self.pnl,[],now=2000),0)
        self.assertIn('0.01',s['cycles'])

    def test_terminal_seen_pruned_after_place(self):
        src=(ROOT/'okx_bot.py').read_text()
        node=next(n for n in ast.parse(src).body if isinstance(n,ast.FunctionDef) and n.name=='place')
        body=ast.unparse(node)
        self.assertIn("st.get('terminal_seen', {}).pop",body)
        self.assertLess(body.index("st.get('terminal_seen', {}).pop"),body.index("st['cycles'][k] = x"))

    def test_tick_only_after_all_resolved(self):
        src=ast.unparse(ast.parse((ROOT/'okx_bot.py').read_text()))
        self.assertIn('if started - last_archive_compact >= 30:',src)
        anchor=src.index('if started - last_archive_compact >= 30:')
        self.assertIn('eager_archive_terminals(st, pnl, pending)',src[anchor:anchor+240])

    def test_durable_crash_between_state_save_and_sqlite_compaction(self):
        with tempfile.TemporaryDirectory() as temp:
            base=pathlib.Path(temp)
            statefile=base/'state.json';archivefile=base/'cycles.sqlite'
            s={'cycles':{'0.01':terminal('a')},'archived_cycles':[],
               'terminal_seen':{'a':1000}}
            def persistent_save(st):
                tmp=base/'temp.json';tmp.write_text(json.dumps(st));os.replace(tmp,statefile)
            live=extract(['eager_archive_terminals','compact_archived'],other={
                'save':persistent_save,'ARCHIVE_DB':archivefile,
                'TERMINAL_ARCHIVE_GRACE_SEC':900})
            self.assertEqual(live['eager_archive_terminals'](s,self.pnl,[],now=2000),1)
            reopened=json.loads(statefile.read_text())
            self.assertEqual(len(reopened['archived_cycles']),1)
            # Crash here: only durable state exists, not cycles.sqlite.
            self.assertFalse(archivefile.exists())
            self.assertEqual(live['compact_archived'](reopened,self.pnl),1)
            after=json.loads(statefile.read_text())
            self.assertEqual(after['archived_cycles'],[])
            with contextlib.closing(sqlite3.connect(archivefile)) as db, db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM cycles').fetchone()[0],1)
            self.assertEqual(live['compact_archived'](after,self.pnl),0)


class StallSuppressionTests(unittest.TestCase):
    def test_30_benign_cycles_do_not_raise(self):
        from okx_stats_v2_production import SingleAuthorityStats
        from okx_stats_v2_phase3 import DurableStager
        from okx_stats_v2_contract import NotReady
        with tempfile.TemporaryDirectory() as d:
            base=pathlib.Path(d);(base/'state').mkdir()
            side=DurableStager(base/'state'/'stats_v2.sqlite','BLUR-USDT-SWAP')
            for i in range(18):
                side.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',(f'c{i}',i+1,'{}'))
            obj=SingleAuthorityStats.__new__(SingleAuthorityStats)
            obj.root=base; obj.stager=side;obj.freeze_budget=64
            obj.cv=Decimal('1');obj.instrument='BLUR-USDT-SWAP'
            obj.oracle={'full_stats_snapshot':lambda *_:None,
                        'allocated_market_fills':lambda *_:None}
            @contextlib.contextmanager
            def views(_):yield {}
            side.finance_snapshots=views
            side._stable_sources=lambda *_:None
            def blocked(_root,_cid,**kw):raise NotReady('Shared MARKET component not fully archived')
            side.freeze_one=blocked
            with mock.patch('okx_stats_v2_production.stable_state',
                            return_value=({'instrument':'BLUR-USDT-SWAP'},('same',))):
                for _ in range(30):
                    done,deferred,reasons=obj._freeze_bounded(Decimal('1'),1000)
                    self.assertEqual((done,deferred,reasons),(0,18,{'shared_component':18}))
                    self.assertEqual(obj.freeze_stalled,0)
                self.assertEqual(side.db.execute("SELECT val FROM meta WHERE key='freeze_no_progress'").fetchone()[0],'0')
            side.close()

    def test_real_causes_still_trigger_alert_counter(self):
        from okx_stats_v2_production import SingleAuthorityStats
        from okx_stats_v2_phase3 import DurableStager
        from okx_stats_v2_contract import NotReady
        for message in ('Missing ledger row','Missing PnL identity',
                        'unknown NotReady context','fills_incomplete'):
            with self.subTest(message=message),tempfile.TemporaryDirectory() as d:
                base=pathlib.Path(d);(base/'state').mkdir()
                side=DurableStager(base/'state'/'stats_v2.sqlite','BLUR-USDT-SWAP')
                side.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',('c',1,'{}'))
                obj=SingleAuthorityStats.__new__(SingleAuthorityStats)
                obj.root=base;obj.stager=side;obj.freeze_budget=1
                obj.cv=Decimal('1');obj.instrument='BLUR-USDT-SWAP'
                obj.oracle={'full_stats_snapshot':lambda *_:None,
                            'allocated_market_fills':lambda *_:None}
                @contextlib.contextmanager
                def views(_):yield {}
                side.finance_snapshots=views; side._stable_sources=lambda *_:None
                side.freeze_one=lambda *_a,**_kw:(_ for _ in ()).throw(NotReady(message))
                with mock.patch('okx_stats_v2_production.stable_state',
                                return_value=({'instrument':'BLUR-USDT-SWAP'},('same',))):
                    for _ in range(21):
                        obj._freeze_bounded(Decimal('1'),1000)
                    self.assertEqual(obj.freeze_stalled,21)
                side.close()


class SharedMarketFullParityTests(unittest.TestCase):
    """Pinned ORIGINAL FULL oracle; four siblings, two initially in state."""
    def test_four_shared_market_before_after_compact_restart(self):
        from okx_stats_v2_phase3 import DurableStager
        from okx_stats_v2_contract import NotReady
        from okx_stats_v2_money import compose_with_hot
        gz=(ROOT/'tests/fixtures/okx_bot_original_full_v0760.py.gz').read_bytes()
        oracle=gzip.decompress(gz)
        self.assertEqual(hashlib.sha256(oracle).hexdigest(),
                         '9c6cf1b46ba8f0b045a80ea97b3178657fd296c2e6d564fb8579839e66057cbd')
        needed={'all_stat_cycles','accumulate_expected_sell','allocated_market_fills',
                'stats_pct','full_stats_snapshot','_last_24h_profit'}
        tree=ast.parse(oracle)
        funcs=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in needed]
        self.assertEqual({x.name for x in funcs},needed)
        o={'Decimal':Decimal,'D':lambda x:Decimal(str(x or '0'))}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),'PINNED_ORIGINAL_FULL','exec'),o)
        inst='BLUR-USDT-SWAP'; stamp=1800000000000
        plan=[{'cycleId':cid,'sz':'1'} for cid in 'abcd']
        cycles={}
        for cid in 'abcd':
            c=terminal(cid,aggregate=plan)
            c['buyPx']='100';c['tpPx']='101';c['tpOrdId']='MARKET'
            cycles[cid]=c
        fills=[]
        for cid in 'abcd':
            fills.append({'ordId':'BUY-'+cid,'billId':'bill-'+cid,'tradeId':'bill-'+cid,
                          'fillSz':'1','fillPx':'100','side':'buy','fee':'-0.01',
                          'feeCcy':'USDT','execType':'M','ts':str(stamp-1000)})
        fills.append({'ordId':'MARKET','billId':'sell-shared','tradeId':'sell-shared',
                      'fillSz':'4','fillPx':'101','side':'sell','fee':'-0.04',
                      'feeCcy':'USDT','execType':'M','ts':str(stamp-1000)})
        def original_full(hot, archive):
            state={'cycles':hot,'archived_cycles':[], 'completed':0}
            return o['full_stats_snapshot'](state,fills,Decimal('110'),Decimal('1'),
                          include_durable_archive=True,snapshot_archive=archive)
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);(root/'state').mkdir()
            source=root/'state'; statepath=source/'bot_state.json'
            def save(st):
                tmp=source/'tmp';tmp.write_text(json.dumps(st));os.replace(tmp,statepath)
            state={'instrument':inst, 'cycles':{'0.009':cycles['c'],'0.008':cycles['d']},
                   'archived_cycles':[],'terminal_seen':{'c':1000,'d':1000},'completed':0}
            save(state)
            with contextlib.closing(sqlite3.connect(source/'cycles.sqlite')) as db, db:
                db.execute('CREATE TABLE cycles(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                for cid in 'ab':
                    db.execute('INSERT INTO cycles VALUES (?,?)',(cid,json.dumps(cycles[cid])))
            with contextlib.closing(sqlite3.connect(source/'pnl.sqlite')) as db, db:
                db.execute('CREATE TABLE accounted(id TEXT PRIMARY KEY,kind TEXT,net TEXT,data TEXT)')
                for cid in 'abcd':
                    key='BUY-'+cid+':MARKET'
                    db.execute('INSERT INTO accounted VALUES (?,?,?,?)',
                               (key,'CLOSED','0.98',json.dumps({'kind':'CLOSED','net':'0.98'})))
            with contextlib.closing(sqlite3.connect(source/'fills.sqlite')) as db, db:
                db.execute('CREATE TABLE fills(order_id TEXT,bill_id TEXT,qty TEXT,data TEXT,PRIMARY KEY(order_id,bill_id))')
                for row in fills:
                    db.execute('INSERT INTO fills VALUES (?,?,?,?)',
                               (row['ordId'],row['billId'],row['fillSz'],json.dumps(row)))
            side=DurableStager(root/'state'/'stats_v2.sqlite',inst)
            try:
                self.assertEqual(side.ingest(root)['new']['cycles'],2)
                self.assertEqual(side.pending_count(),2)
                with self.assertRaises(NotReady):
                    side.freeze_one(root,'a',full_oracle=o['full_stats_snapshot'],
                                    allocate=o['allocated_market_fills'],
                                    contract_value=Decimal(1),bid=Decimal(110),now_ms=stamp)
                expected=original_full({'0.009':cycles['c'],'0.008':cycles['d']},
                                       [cycles['a'],cycles['b']])
                env=extract(['eager_archive_terminals','compact_archived'],other={
                    'save':save,'ARCHIVE_DB':source/'cycles.sqlite',
                    'TERMINAL_ARCHIVE_GRACE_SEC':900})
                pnl=mock.Mock();pnl.recorded.return_value=True
                moved=env['eager_archive_terminals'](state,pnl,[],now=2000)
                self.assertEqual(moved,2)
                self.assertEqual(state['cycles'],{})
                self.assertEqual(original_full({},[cycles['a'],cycles['b']]+state['archived_cycles']),expected)
                # Crash/restart with un-compacted state is safe.
                reopened=json.loads(statepath.read_text())
                self.assertEqual(len(reopened['archived_cycles']),2)
                self.assertEqual(env['compact_archived'](reopened,pnl),2)
                self.assertEqual(reopened['archived_cycles'],[])
                with contextlib.closing(sqlite3.connect(source/'cycles.sqlite')) as db, db:
                    rows=[json.loads(row[0]) for row in db.execute('SELECT data FROM cycles ORDER BY id')]
                self.assertEqual(len(rows),4)
                self.assertEqual(original_full({},rows),expected)
                self.assertEqual(side.ingest(root)['new']['cycles'],2)
                self.assertEqual(side.pending_count(),4)
                out=side.freeze_one(root,'a',full_oracle=o['full_stats_snapshot'],
                                    allocate=o['allocated_market_fills'],
                                    contract_value=Decimal(1),bid=Decimal(110),now_ms=stamp)
                self.assertEqual(out['cycles'],4)
                self.assertEqual(side.pending_count(),0)
                snap=side.totals_snapshot()
                frozen=side._restore_totals(snap)
                empty_hot=original_full({},[])
                after=compose_with_hot(frozen,empty_hot,Decimal(0),Decimal(0),
                        {'cycles':{},'completed':0},Decimal('110'),Decimal('1'),
                        stats_pct=o['stats_pct'])
                self.assertEqual(after,expected)
            finally:
                side.close()
            # Sidecar and exchange-source SQLite survive independent restart.
            again=DurableStager(root/'state'/'stats_v2.sqlite',inst)
            try:
                self.assertEqual(again.pending_count(),0)
                self.assertEqual(again.frozen_count(),1)
                self.assertEqual(again.totals_snapshot(),snap)
            finally:again.close()

if __name__=='__main__':unittest.main()
