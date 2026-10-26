#!/usr/bin/env python3
"""Offline tests for TP timing and Telegram presentation; no bot bootstrap or exchange."""
import ast
from contextlib import closing
import json
import threading
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))
import okx_tp_telemetry as tp
import okx_telegram_views as views
from okx_telegram_events import market_event
from okx_latency import LatencyJournal
from okx_latency_rollup import add_row, distribution


class FixedClock:
    def __init__(self, at=1000.0):self.at=at
    def time(self):return self.at
    def monotonic(self):return self.at


def engine_definitions(*names, time=None):
    """Compile ONLY functions/classes; never import/execute the LIVE engine."""
    tree=ast.parse((ROOT/'okx_bot.py').read_text())
    selected=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    assert len(selected)==len(names),[n.name for n in selected]
    ns={'time':time or FixedClock(),'threading':threading,'Decimal':Decimal,
        'D':Decimal,'tp_quantity_placeable':lambda q:True}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'okx_bot.py','exec'),ns)
    return ns


class TPObservability(unittest.TestCase):
    def test_original_market_guards_and_window_are_intact(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        constants={t.id:node.value.value for node in tree.body
                   if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant)
                   for t in node.targets if isinstance(t,ast.Name)}
        self.assertEqual(constants['ESCAPE_COALESCE_SEC'],0.15)
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
                and n.name=='virtual_escape')
        body=ast.get_source_segment((ROOT/'okx_bot.py').read_text(),fn)
        for required in (
            "if mode!='RUN':return 'NONE'",
            "if unresolved_aggregate(st):return 'WAIT'",
            "if any(o.get('side')=='sell' for o in pending):",
            "if now-float(collect['startedAt'])<ESCAPE_COALESCE_SEC:",
            "if qty<=0 or qty>actual:die('Aggregate exit exceeds reconciled position')",
            'save(st)  # fsync all parent links + intent BEFORE the network POST',
            "rows=A.req('POST','/api/v5/trade/order',body=body,exp_time_ms=exp)",
        ):
            self.assertIn(required,body)
        self.assertEqual(body.count("A.req('POST','/api/v5/trade/order'"),1)

    def test_public_bid_first_observation_survives_retrace_and_delayed_rest(self):
        clock=FixedClock(1000)
        ns=engine_definitions('PublicBidWatcher',time=clock)
        watcher=ns['PublicBidWatcher']()
        watcher.set_virtual_targets({'cycle-a':Decimal('100'),'cycle-b':Decimal('101')})
        watcher._capture_crossings(Decimal('100.50'))
        clock.at=1000.012
        watcher._capture_crossings(Decimal('99'))  # retraced
        clock.at=1000.040
        watcher._capture_crossings(Decimal('101.5'))
        crossed=watcher.take_crossed_virtual_stamps()
        self.assertAlmostEqual(crossed['cycle-a'],1000)
        self.assertAlmostEqual(crossed['cycle-b'],1000.040)
        self.assertEqual(watcher.take_crossed_virtual_stamps(),{})
        self.assertFalse(watcher._virtual_crossed)

    def test_persisted_crossing_time_precedes_main_loop(self):
        clock=FixedClock(1000.65)
        st={'cycles':{'100':{'state':'VIRTUAL_TP','cycleId':'a','tpPx':'100',
                             'tpOrders':[]}}}
        class Watch:
            def take_crossed_virtual_stamps(self):return {'a':1000.001}
            def set_virtual_targets(self,targets):self.targets=targets
        saved=[]
        namespace=engine_definitions('persist_crossed_virtual_tps',time=clock)
        namespace.update(virtual_cycles=lambda st:[('100',st['cycles']['100'])],
                         save=lambda st:saved.append(1),D=Decimal)
        watcher=Watch()
        namespace['persist_crossed_virtual_tps'](st,watcher)
        self.assertEqual(len(saved),1)
        self.assertTrue(st['cycles']['100']['virtualTpCrossed'])
        self.assertEqual(st['cycles']['100']['virtualTpCrossedAt'],1000.001)
        self.assertEqual(watcher.targets,{'a':Decimal('100')})

    def test_intent_timing_and_added_cycles(self):
        group={'startedAt':1000.075,'initialCycleIds':['a','b'],
               'cycles':['a','b','c']}
        wanted=[{'cycleId':'a','virtualTpCrossedAt':1000.0},
                {'cycleId':'b','virtualTpCrossedAt':1000.05},
                {'cycleId':'c','virtualTpCrossedAt':1000.17}]
        v=tp.intent_timing(wanted,group,1000.26)
        self.assertEqual(v['cross_source'],'ws_bid')
        self.assertAlmostEqual(v['tp_to_intent_ms'],260,places=2)
        self.assertAlmostEqual(v['cross_to_collect_ms'],75,places=2)
        self.assertAlmostEqual(v['collect_to_intent_ms'],185,places=2)
        self.assertEqual(v['additional_cycles'],1)
        self.assertEqual(v['added_during_window'],1)
        self.assertEqual(v['added_after_window'],0)
        self.assertEqual(group['cycles'],['a','b','c'])
        self.assertEqual(len(wanted),3)

    def test_unknown_or_skewed_bid_cross_time_is_not_fabricated(self):
        group={'startedAt':1000,'initialCycleIds':['a']}
        v=tp.intent_timing([{'cycleId':'a'}],group,1000.25)
        self.assertEqual(v['cross_source'],'crossing_time_unknown')
        self.assertNotIn('tp_to_intent_ms',v)
        self.assertAlmostEqual(v['collect_to_intent_ms'],250)
        v=tp.intent_timing([{'cycleId':'a','virtualTpCrossedAt':1002}],group,1000.25)
        self.assertNotIn('tp_to_intent_ms',v)
        v=tp.intent_timing([{'cycleId':'a','virtualTpCrossedAt':'NaN'}],group,1000.25)
        self.assertNotIn('tp_to_intent_ms',v)

    def test_notification_math_positive_negative_zero_plan(self):
        datasets=[
            ('0.011134','0.010252','0.000882','🟩','108.6%','+8.6%'),
            ('0.004143','0.020656','-0.016513','🟨','20.1%','-79.9%'),
            ('-0.004290','0.010348','-0.014638','🟥','-41.5%','-141.5%'),
            ('0.1','0','0.1','🟨','н/д','н/д'),
            ('0.1','-1','1.1','🟨','н/д','н/д'),
        ]
        for actual,plan,execution,indicator,pct,delta in datasets:
            data={'qty_base':Decimal('0.02'),'cycles':1,
                  'avg_price':Decimal('1209.1332'),
                  'actual':Decimal(actual),'plan':Decimal(plan),
                  'execution':Decimal(execution)}
            with self.subTest(actual=actual,plan=plan),\
                 patch.object(views,'market_ledger',return_value=data),\
                 patch.object(views,'read_json',return_value={
                     'instrument':'ZEC-USDT-SWAP','tick_size':'0.01'}):
                msg=views.market_message('zec',Path('/not/used'),{},
                    {'fill':31,'ack':143,'full':292,'tp_to_intent_ms':276})
                self.assertIn(f'{indicator} Market SELL 0.02 ZEC (1) по 1209.13',msg)
                self.assertIn(f'{pct})',msg)
                self.assertIn('Исполнение',msg)
                self.assertNotIn('🟩 Исполнение',msg)
                self.assertNotIn('🟥 Исполнение',msg)
                self.assertIn(f'({delta})',msg)
                self.assertIn('к профиту',msg)
                self.assertIn('⏱ BID TP→Intent ≈276 мс',msg)
                self.assertIn('⚡ Fill ≈31 мс | ACK 143 мс | Full ≈292 мс',msg)
                self.assertNotIn('в копилку',msg)

    def test_actual_weighted_sell_price_from_persisted_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'state').mkdir()
            (root/'config.json').write_text(json.dumps({
                'instrument':'ZEC-USDT-SWAP','contract_value':'0.01',
                'tick_size':'0.01','maker_fee_at_setup':'0.0002'}))
            state={'cycles':{
                'a':{'cycleId':'a','ordId':'buyA','tpPx':'1209.0'},
                'b':{'cycleId':'b','ordId':'buyB','tpPx':'1210.0'}},
                'archived_cycles':[],
                'aggregate_exits':[{'ordId':'sell1','phase':'FILLED',
                    'sz':'3','plan':[{'cycleId':'a','sz':'1'},
                                      {'cycleId':'b','sz':'2'}]}]}
            (root/'state'/'bot_state.json').write_text(json.dumps(state))
            with closing(sqlite3.connect(root/'state'/'pnl.sqlite')) as db, db:
                db.execute('CREATE TABLE accounted(data TEXT)')
                for buyid,qty,base,px,net in [
                    ('buyA','1','0.01','1209.10','0.090'),
                    ('buyB','2','0.02','1210.00','0.190')]:
                    item={'sell_ids':['sell1'],'kind':'CLOSED',
                          'qty_contracts':qty,'qty_base':base,
                          'sell_px':px,'buy_px':'1200.0',
                          'planned_gross':net,'net':net,'buy_id':buyid}
                    db.execute('INSERT INTO accounted VALUES (?)',(json.dumps(item),))
            with closing(sqlite3.connect(root/'state'/'fills.sqlite')) as db, db:
                db.execute('CREATE TABLE fills(order_id TEXT,data TEXT)')
                for buyid,qty in [('buyA','1'),('buyB','2')]:
                    db.execute('INSERT INTO fills VALUES (?,?)',
                        (buyid,json.dumps({'fillSz':qty,'side':'buy',
                                           'feeCcy':'USDT','fee':'-0.0001'})))
            value=views.market_ledger(root,{'ordId':'sell1','state':'FILLED'})
            self.assertIsNotNone(value)
            self.assertEqual(value['avg_price'],Decimal('1209.70'))
            self.assertEqual(value['qty_base'],Decimal('0.03'))
            self.assertEqual(value['cycles'],2)

    def test_legacy_events_do_not_get_fabricated_cross_metric(self):
        data={'qty_base':Decimal('0.02'),'cycles':1,'avg_price':Decimal('1209.13'),
              'actual':Decimal('0.02'),'execution':Decimal('0'),
              'plan':Decimal('0.02')}
        with patch.object(views,'market_ledger',return_value=data),\
             patch.object(views,'read_json',return_value={'instrument':'ZEC-USDT-SWAP'}):
            msg=views.market_message('zec',Path('/not/used'),{},
                {'fill':13,'ack':31,'full':116})
        self.assertNotIn('TP→Intent',msg)

    def test_telegram_event_keeps_new_timings_under_existing_state_schema(self):
        state={'schema':1}
        market_event(state,'zec',{'event':'AGG_MARKET_INTENT',
            'ts':'2026-10-25T12:00:00.000Z','clOrdId':'int1',
            'tp_to_intent_ms':243.5,'cross_to_collect_ms':44.2,
            'collect_to_intent_ms':199.3,'additional_cycles':2,'cross_source':'ws_bid'})
        key='zec/int1'
        self.assertEqual(state['market_events'][key]['tp_to_intent_ms'],243.5)
        market_event(state,'zec',{'event':'AGG_MARKET_ACK',
            'ts':'2026-10-25T12:00:00.050Z','clOrdId':'int1',
            'intent_to_submit_ms':1.1})
        self.assertEqual(state['market_events'][key]['intent_to_submit_ms'],1.1)
        self.assertEqual(state['schema'],1)
        state=json.loads(json.dumps(state))
        self.assertEqual(state['market_events'][key]['additional_cycles'],2)

    def test_async_telemetry_guard_allows_only_scalar_diagnostic_fields(self):
        class Dummy:
            pass
        latency=LatencyJournal(Path('/invalid/path'),enabled=True,max_queue=2)
        # Avoid creating any filesystem writer: provide an inert sentinel.
        latency._thread=Dummy()
        latency.record('tp_to_intent',267.5,source='ws_bid',cycles=3,added=2,token='SECRET')
        rec=latency.queue.get_nowait()
        self.assertEqual(rec['phase'],'tp_to_intent')
        self.assertEqual(rec['ms'],267.5)
        self.assertNotIn('token',rec)
        self.assertEqual(rec['added'],2)


if __name__=='__main__':
    unittest.main(verbosity=2)
