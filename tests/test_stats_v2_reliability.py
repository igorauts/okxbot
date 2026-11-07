"""Offline StatsWorker-v2 vNext incident, queue, bootstrap and safety contracts.

All paths are temporary; no OKX API, Telegram network or LIVE state.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from unittest import mock

BASE=pathlib.Path(__file__).resolve().parents[1]


def function_node(filename,name):
    tree=ast.parse((BASE/filename).read_text())
    return next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)


class AnchorBootstrapTests(unittest.TestCase):
    def test_first_anchor_preserves_original_cfg_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp); data=root/'bot_state.json'; anchor=root/'grid_reference.json'
            data.write_text(json.dumps({'started_at':1700000020.5}))
            env={
                'GRID_REFERENCE':anchor, 'DATA':data,'CFG':{'created_at':1700000000.5},
                'CAPITAL_REFERENCE':Decimal('0.1200'),'STEP':Decimal('0.0001'),
                'CAP':300,'UPPER':Decimal('0.12'),'D':lambda x:Decimal(str(x)),
                'json':json,'EVENTS':root/'missing-events.jsonl',
                'atomic_json':lambda p,v:p.write_text(json.dumps(v)),
            }
            exec(compile(ast.Module(body=[function_node('okx_bot.py','_first_anchor')],type_ignores=[]),'anchor-test','exec'),env)
            since,top=env['_first_anchor']([])
            self.assertEqual((since,top),(1700000020.5,Decimal('0.1200')))
            self.assertEqual(json.loads(anchor.read_text()),{
                'started_at':1700000020.5,'top_buy':'0.1200','grid_step':'0.0001',
                'max_grids':300,'upper':'0.12'})

    def test_first_publication_bootstraps_only_from_original_creation_config(self):
        source=ast.unparse(function_node('okx_bot.py','print_verified_incremental_stats'))
        self.assertIn('_first_anchor([])',source)
        self.assertIn("'created_at'",source)
        self.assertIn('Missing original creation-time capital anchor',source)
        self.assertIn('raise NotReady',source)
        self.assertLess(source.index('_first_anchor([])'),source.index('core.compute(bid)'))


class RaceTests(unittest.TestCase):
    def test_state_change_after_capture_is_not_drift(self):
        from okx_stats_v2_phase3 import DurableStager
        from okx_stats_v2_contract import NotReady
        obj=DurableStager.__new__(DurableStager)
        with mock.patch('okx_stats_v2_phase3.stable_state',return_value=({'cycles':{},'archived_cycles':[]},('after',))):
            with self.assertRaisesRegex(NotReady,'State changed during ingestion'):
                obj._stable_sources(pathlib.Path('/tmp'),('before',),{'cycles':{},'archived_cycles':[]})

    def test_malformed_state_is_still_drift(self):
        from okx_stats_v2_contract import stable_state,Drift
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'state.json'
            p.write_text('{bad json')
            with self.assertRaises(Drift):stable_state(p)


class FreezeQueueTests(unittest.TestCase):
    def _make(self,tmp):
        from okx_stats_v2_phase3 import DurableStager
        db=DurableStager(pathlib.Path(tmp)/'sidecar.sqlite','BEAT-USDT-SWAP')
        for i in range(1,11):
            db.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',(f'c{i}',i,'{}'))
        return db

    def test_deferred_head_does_not_starve_later_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=self._make(tmp)
            try:
                first=db.pending_batch(4)
                self.assertEqual([c for c,_ in first],['c1','c2','c3','c4'])
                db.db.execute("INSERT INTO meta VALUES ('freeze_after_rowid',?)",(str(first[-1][1]),))
                second=db.pending_batch(4)
                self.assertEqual([c for c,_ in second],['c5','c6','c7','c8'])
                db.db.execute("UPDATE meta SET val=? WHERE key='freeze_after_rowid'",(str(second[-1][1]),))
                self.assertEqual([c for c,_ in db.pending_batch(4)],['c9','c10','c1','c2'])
            finally:db.close()

    def test_cursor_persists_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=self._make(tmp)
            first=db.pending_batch(6)
            db.db.execute("INSERT INTO meta VALUES ('freeze_after_rowid',?)",(str(first[-1][1]),))
            db.close()
            from okx_stats_v2_phase3 import DurableStager
            reopened=DurableStager(pathlib.Path(tmp)/'sidecar.sqlite','BEAT-USDT-SWAP')
            try:self.assertEqual([c for c,_ in reopened.pending_batch(4)],['c7','c8','c9','c10'])
            finally:reopened.close()

    def test_invalid_checkpoint_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=self._make(tmp)
            db.db.execute("INSERT INTO meta VALUES ('freeze_after_rowid','garbage')")
            from okx_stats_v2_contract import Drift
            try:
                with self.assertRaises(Drift):db.pending_batch(4)
            finally:db.close()

    def test_freeze_batch_reuses_one_pinned_state_and_records_defer(self):
        from okx_stats_v2_production import SingleAuthorityStats
        from okx_stats_v2_phase3 import DurableStager
        from okx_stats_v2_contract import NotReady
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            db=DurableStager(root/'state/stats_v2.sqlite','BEAT-USDT-SWAP')
            db.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',('missing',1,'{}'))
            db.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',('ready',2,'{}'))
            original=db.freeze_one
            seen=[]
            def fake_freeze(root_arg,cid,**kwargs):
                seen.append((cid,kwargs['pinned_state']))
                if cid=='missing':raise NotReady('fills_incomplete')
                db.db.execute('DELETE FROM pending_cycles WHERE cycle_id=?',(cid,))
            @contextmanager
            def no_finance(_):yield {}
            db.freeze_one=fake_freeze
            db.finance_snapshots=no_finance
            db._stable_sources=lambda *_:None
            obj=SingleAuthorityStats.__new__(SingleAuthorityStats)
            obj.root=root;obj.stager=db;obj.freeze_budget=2;obj.cv=Decimal('1');obj.instrument='BEAT-USDT-SWAP'
            obj.oracle={'full_stats_snapshot':lambda *_:None,'allocated_market_fills':lambda *_:None}
            with mock.patch('okx_stats_v2_production.stable_state',return_value=({'instrument':'BEAT-USDT-SWAP'},('same',))) as pinned:
                done,delayed,reasons=obj._freeze_bounded(Decimal('1'),1)
            try:
                self.assertEqual((done,delayed,reasons),(1,1,{'fills_incomplete':1}))
                self.assertEqual(pinned.call_count,1)
                self.assertIs(seen[0][1],seen[1][1])
                self.assertEqual(db.pending_ids(),['missing'])
                self.assertEqual(db.db.execute("SELECT val FROM meta WHERE key='freeze_after_rowid'").fetchone()[0],'2')
            finally:db.close()

    def test_sixty_four_deferred_head_never_starves_newer_ready(self):
        from okx_stats_v2_production import SingleAuthorityStats
        from okx_stats_v2_phase3 import DurableStager
        from okx_stats_v2_contract import NotReady
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            db=DurableStager(root/'state/stats_v2.sqlite','BEAT-USDT-SWAP')
            for i in range(70):
                db.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',
                              (f'c{i:03}',i+1,'{}'))
            visited=[]
            def freeze_fake(_,cid,**kwargs):
                visited.append(cid)
                if int(cid[1:])<64:raise NotReady('fills_incomplete')
                db.db.execute('DELETE FROM pending_cycles WHERE cycle_id=?',(cid,))
            @contextmanager
            def dummy_views(_):yield {}
            db.freeze_one=freeze_fake; db.finance_snapshots=dummy_views
            db._stable_sources=lambda *_:None
            obj=SingleAuthorityStats.__new__(SingleAuthorityStats)
            obj.root=root;obj.stager=db;obj.freeze_budget=64
            obj.cv=Decimal(1);obj.instrument='BEAT-USDT-SWAP'
            obj.oracle={'full_stats_snapshot':lambda *_:None,
                        'allocated_market_fills':lambda *_:None}
            with mock.patch('okx_stats_v2_production.stable_state',
                            return_value=({'instrument':'BEAT-USDT-SWAP'},('sid',))):
                first=obj._freeze_bounded(Decimal(1),100)
                self.assertEqual(first[:2],(0,64))
                self.assertEqual(obj.freeze_stalled,1)
                second=obj._freeze_bounded(Decimal(1),100)
                self.assertEqual(second[0],6)
                self.assertEqual(obj.freeze_stalled,0)
                self.assertEqual(db.pending_count(),64)
                self.assertIn('c064',visited)
            db.close()


class QueueIncidentTests(unittest.TestCase):
    def test_successful_publication_still_emits_stalled_queue_incident(self):
        # A HOT-inclusive report can remain fresh while pending freeze makes
        # zero progress. The incident must not depend on NotReady exceptions.
        import sys, threading, types
        class StopRun(Exception):pass
        class StubTime:
            @staticmethod
            def monotonic():return 100.0
            @staticmethod
            def sleep(_):raise StopRun()
        class FakeCore:
            freeze_stalled=20
            def __init__(self,*args,**kwargs):pass
        engine=ast.parse((BASE/'okx_bot.py').read_text())
        worker=next(n for n in engine.body
                    if isinstance(n,ast.ClassDef) and n.name=='StatsWorker')
        captured=[]
        env={'threading':threading,'time':StubTime(),'print':lambda *a,**k:None,
             'log':lambda event,**kw:captured.append((event,kw)),
             'ROOT':pathlib.Path('/unused'), 'INST':'BEAT-USDT-SWAP',
             'FILL_STORE':object(), 'expected_filled_orders':lambda *a:None,
             'CONTRACT_VALUE':Decimal('1'),'STATS_INTERVAL':60,
             'STATS_FINANCIAL_CHANGE_MIN_SEC':30,
             'DATA':pathlib.Path('/unused/state.json'),
             'STATS_FILE':pathlib.Path('/unused/stats.json'),
             'atomic_json':lambda *a,**kw:None,
             'print_verified_incremental_stats':lambda *_a,**_kw:True,
             'full_stats_snapshot':lambda *_:None,
             'allocated_market_fills':lambda *_:None,
             '_last_24h_profit':lambda *_:None,
             'stats_pct':lambda *_:None, 'RateLimitedError':RuntimeError}
        exec(compile(ast.Module(body=[worker],type_ignores=[]),'worker-contract','exec'),env)
        modules={'okx_stats_v2_contract':types.SimpleNamespace(Drift=ValueError,NotReady=KeyError),
                 'okx_stats_v2_production':types.SimpleNamespace(SingleAuthorityStats=FakeCore),
                 'okx_stats_v2_recovery':types.SimpleNamespace(
                     resume_interrupted=lambda *a,**kw:None,
                     finish_rebuild=lambda *a,**kw:False)}
        with mock.patch.dict(sys.modules,modules):
            with self.assertRaises(StopRun):env['StatsWorker'](object()).run()
        self.assertEqual([x[0] for x in captured],['STATS_V2_STALLED'])


class RebuildTests(unittest.TestCase):
    def test_only_true_late_fill_can_trigger_auto_rebuild(self):
        from okx_stats_v2_recovery import eligible
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            self.assertFalse(eligible(root,'State changed during ingestion',now=1000000))
            self.assertFalse(eligible(root,'Invalid state',now=1000000))
            self.assertTrue(eligible(root,'LATE_FILL_FROZEN_ORDER: actual append',now=1000000))

    def test_quarantine_preserves_old_sidecar_and_throttles(self):
        from okx_stats_v2_recovery import quarantine_sidecar,eligible,finish_rebuild
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            old=root/'state/stats_v2.sqlite';old.write_bytes(b'original-derived-sidecar')
            target=quarantine_sidecar(root,now=1000000,
                write_json=lambda p,v:p.write_text(json.dumps(v)))
            self.assertFalse(old.exists())
            self.assertEqual(target.read_bytes(),b'original-derived-sidecar')
            self.assertFalse(eligible(root,'LATE_FILL_FROZEN_ORDER',now=1000001))
            self.assertFalse(eligible(root,'LATE_FILL_FROZEN_ORDER',now=1000000+6*3600))
            old.write_bytes(b'rebuilt-derived-sidecar')
            self.assertTrue(finish_rebuild(root,write_json=lambda p,v:p.write_text(json.dumps(v))))
            self.assertTrue(eligible(root,'LATE_FILL_FROZEN_ORDER',now=1000000+6*3600))

    def test_wal_detected_and_original_retained(self):
        from okx_stats_v2_recovery import quarantine_sidecar,RebuildRefused
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            old=root/'state/stats_v2.sqlite';old.write_bytes(b'original')
            pathlib.Path(str(old)+'-wal').write_bytes(b'unflushed')
            with self.assertRaises(RebuildRefused):
                quarantine_sidecar(root,now=1000000,write_json=lambda p,v:None)
            self.assertEqual(old.read_bytes(),b'original')


class PnLLedgerTests(unittest.TestCase):
    def test_hot_ledger_ingestion_must_repin_before_accounting_check(self):
        text=ast.unparse(function_node('okx_stats_v2_production.py','dummy')) if False else (BASE/'okx_stats_v2_production.py').read_text()
        self.assertLess(text.index('self._check_hot_ledger(cycles,hot'),
                        text.index('self.pnl_ledger.ingest_history(hot_state,fills)'))
        self.assertIn('next pin required',text)

    def test_real_pnl_ledger_ingestion_idempotent_with_complete_fills(self):
        import sys
        from okx_accounting import build_pnl_ledger
        with tempfile.TemporaryDirectory() as tmp:
            P=pathlib.Path(tmp)/'pnl.sqlite'
            ledger_type=build_pnl_ledger(
                D=lambda x:Decimal(str(x)),STEP=Decimal('0.1'),A=object(),
                RateLimitedError=RuntimeError,
                allocated_market_fills=lambda rows,plan,cid:rows,
                fmt_accounted_price=str,money=lambda *args: str(args[0]),
                money_usdt=str,bold=lambda x:x,die=lambda x:(_ for _ in ()).throw(ValueError(x)),
                PNL_DB=P)
            ledger=ledger_type(Decimal('1'))
            c={'cycleId':'c1','state':'CLOSED','ordId':'b1','filledBuy':'2',
               'filledTP':'2','buyPx':'1','tpPx':'1.1','tpOrders':[{'ordId':'s1','filled':'2'}]}
            fills=[{'ordId':'b1','side':'buy','tradeId':'b',
                    'fillPx':'1','fillSz':'2','fee':'-0.001','feeCcy':'USDT','execType':'M'},
                   {'ordId':'s1','side':'sell','tradeId':'s',
                    'fillPx':'1.1','fillSz':'2','fee':'-0.001','feeCcy':'USDT','execType':'T'}]
            self.assertEqual(ledger.ingest_history({'cycles':{'p':c},'archived_cycles':[]},fills),1)
            self.assertEqual(ledger.ingest_history({'cycles':{'p':c},'archived_cycles':[]},fills),0)
            with sqlite3.connect(P) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM accounted').fetchone()[0],1)
                amount=Decimal(db.execute('SELECT net FROM accounted').fetchone()[0])
            self.assertEqual(amount,Decimal('0.198'))


class TelegramStatusTests(unittest.TestCase):
    def test_verbose_full_never_shows_stale_pnl_as_current(self):
        import time
        from okx_telegram_views import status_one
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            (root/'config.json').write_text(json.dumps({'instrument':'BEAT-USDT-SWAP'}))
            (root/'state/stats.json').write_text(json.dumps({
                'complete':True,'instrument':'BEAT-USDT-SWAP',
                'generated_at_unix':time.time()-4000,
                'report':'ИТОГОВЫЙ PnL: +$9999999.00'}))
            result=status_one('beat',root)
            self.assertIn('устарела',result)
            self.assertIn('PnL: н/д',result)
            self.assertNotIn('$9999999.00',result)

    def test_stale_check_queues_one_alert_and_one_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'state').mkdir()
            (root/'config.json').write_text(json.dumps({'instrument':'BEAT-USDT-SWAP'}))
            anchor={'schema':1,'instrument':'BEAT-USDT-SWAP','at':100}
            (root/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
            (root/'state/stats.json').write_text(json.dumps({
                'complete':True,'instrument':'BEAT-USDT-SWAP',
                'generated_at_unix':100,'stats_v2':{'mode':'single_authority_incremental'},
                'financial_anchor':anchor}))
            node=function_node('okx_telegram_monitor.py','stats_freshness_check')
            events=[]
            env={'time':mock.Mock(time=lambda:2000), 'Path':pathlib.Path,
                 'systemd_active':lambda name:True,'read_json':lambda path:json.loads(path.read_text()) if path.exists() else {},
                 'one':lambda api,st,key,msg:events.append((key,msg))}
            exec(compile(ast.Module(body=[node],type_ignores=[]),'offline-telegram-stale','exec'),env)
            scan=env['stats_freshness_check'];state={}; bots={'beat':'dummy'}
            scan(None,state,bots,{'beat':str(root)},now=2000,stale_after=1800)
            scan(None,state,bots,{'beat':str(root)},now=2005,stale_after=1800)
            self.assertEqual(len(events),1)
            anchor={'schema':1,'instrument':'BEAT-USDT-SWAP','at':2005}
            (root/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
            (root/'state/stats.json').write_text(json.dumps({
                'complete':True,'instrument':'BEAT-USDT-SWAP',
                'generated_at_unix':2005,'stats_v2':{'mode':'single_authority_incremental'},
                'financial_anchor':anchor}))
            scan(None,state,bots,{'beat':str(root)},now=2010,stale_after=1800)
            self.assertEqual(len(events),2)
            self.assertIn('recovered',events[1][0])


if __name__=='__main__':unittest.main(verbosity=2)
