"""v0.7.70 offline Telegram reliability and multi-leg accounting regressions.

Only temporary files, AST-extracted bridge; no live Telegram/OKX IO.
"""
import ast
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import okx_telegram_delivery as delivery
import okx_telegram_views as views
import okx_telegram_events as events

ROOT=Path(__file__).resolve().parents[1]


def bridge_namespace(path):
    tree=ast.parse((ROOT/'okx_telegram_monitor.py').read_text())
    names={'_preacked_delivery_notice','recover_orphaned_preack','_CommandBridge'}
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    assert len(nodes)==len(names)
    class Telegram:
        def __init__(self,*_args):pass
        def _close_http(self):pass
    def save(st):delivery.persist(st,path=path)
    ns={'_cmd_queue':queue,'_cmd_threading':threading,'time':time,
        'Telegram':Telegram,'save_state':save,'_dispatch_command':lambda *_:None,
        'print':lambda *a,**k:None,
        '_PREACK_INFLIGHT_KEY':'telegram_preacked_inflight_v0770'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),
                 '<offline-bridge>','exec'),ns)
    return ns


class PreackNotice(unittest.TestCase):
    def test_ambiguous_response_enqueued_once_and_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'st.json'
            ns=bridge_namespace(path)
            with patch.object(delivery,'STATE_PATH',path):
                b=ns['_CommandBridge']('none','none',500)
                st={'schema':1,'updates_offset':500}
                self.assertTrue(b.prepare_dispatch(st,500))
                self.assertTrue(b.permit.get_nowait())
                persisted=json.loads(path.read_text())
                self.assertEqual(persisted['updates_offset'],501)
                self.assertEqual(persisted['telegram_preacked_inflight_v0770']['update_id'],500)
                b.done.put((500,False))
                self.assertTrue(b.commit_completion(st))
                self.assertEqual(b.ack.get_nowait(),501)
                self.assertEqual(len(st['alert_outbox_v0722']),1)
                self.assertIn('результат доставки',st['alert_outbox_v0722'][0]['text'])
                self.assertNotIn('telegram_preacked_inflight_v0770',json.loads(path.read_text()))
                self.assertFalse(b.prepare_dispatch(st,500))
                self.assertFalse(b.permit.get_nowait())
                self.assertEqual(len(st['alert_outbox_v0722']),1)

    def test_restart_after_preack_creates_durable_notice(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'st.json'
            ns=bridge_namespace(path)
            with patch.object(delivery,'STATE_PATH',path):
                b=ns['_CommandBridge']('none','none',20)
                st={'schema':1,'updates_offset':20}
                b.prepare_dispatch(st,20)
                self.assertTrue(b.permit.get_nowait())
                # Simulated process crash: discard in-memory state and bridge.
                fresh=json.loads(path.read_text())
                self.assertTrue(ns['recover_orphaned_preack'](fresh))
                self.assertFalse(ns['recover_orphaned_preack'](fresh))
                self.assertEqual(json.loads(path.read_text())['updates_offset'],21)
                self.assertEqual(len(json.loads(path.read_text())['alert_outbox_v0722']),1)

    def test_persist_failure_never_dispatches_command(self):
        with tempfile.TemporaryDirectory() as td:
            ns=bridge_namespace(Path(td)/'state.json')
            ns['save_state']=lambda st:(_ for _ in ()).throw(OSError('fsync failure'))
            b=ns['_CommandBridge']('none','none',10)
            st={'schema':1,'updates_offset':10}
            with self.assertRaises(OSError):b.prepare_dispatch(st,10)
            self.assertFalse(b.permit.get_nowait())
            self.assertEqual(st,{'schema':1,'updates_offset':10})


class MultiLeg(unittest.TestCase):
    def setup_chain(self,root,*,missing_market=False,ledger_bad=False):
        (root/'state').mkdir()
        (root/'config.json').write_text(json.dumps({
            'instrument':'SOPH-USDT-SWAP','contract_value':'1',
            'maker_fee_at_setup':'0.001','tick_size':'0.001'}))
        plan=[{'cycleId':'c1','sz':'3'}]
        cycle={'cycleId':'c1','state':'CLOSED','ordId':'buy','filledBuy':'5',
               'filledTP':'5','tpPx':'110','tpOrders':[
                 {'ordId':'limit','filled':'2','source':'limit_tp'},
                 {'ordId':'market','filled':'3','source':'aggregate_exit','aggregatePlan':plan}]}
        (root/'state/bot_state.json').write_text(json.dumps({
            'cycles':{'c1':cycle},'archived_cycles':[],
            'aggregate_exits':[{'ordId':'market','phase':'FILLED','plan':plan,'sz':'3'}]}))
        with closing(sqlite3.connect(root/'state/pnl.sqlite')) as db:
            db.execute('CREATE TABLE accounted(data TEXT)')
            row={'buy_id':'buy','sell_ids':['limit','market'],'kind':'CLOSED',
                 'qty_contracts':'5','qty_base':'5','buy_px':'100','sell_px':'107',
                 'net':'0' if ledger_bad else '33.965','planned_gross':'50'}
            db.execute('INSERT INTO accounted VALUES (?)',(json.dumps(row),))
            db.commit()
        with closing(sqlite3.connect(root/'state/fills.sqlite')) as db:
            db.execute('CREATE TABLE fills(order_id TEXT,data TEXT)')
            for order,side,qty,px,fee in [
                ('buy','buy',5,100,'-0.5'),('limit','sell',2,110,'-0.22'),
                ('market','sell',3,105,'-0.315')]:
                if missing_market and order=='market':continue
                db.execute('INSERT INTO fills VALUES (?,?)',(order,json.dumps({
                    'ordId':order,'tradeId':order+'trade','billId':order+'bill',
                    'fillSz':str(qty),'fillPx':str(px),'fee':fee,
                    'side':side,'execType':'M','feeCcy':'USDT','fillTime':'1000'})))
            db.commit()

    def test_mixed_limit_tp_and_market_exact(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.setup_chain(root)
            reasons=[]
            got=views.market_ledger(root,{'ordId':'market','state':'FILLED'},reason_sink=reasons)
            self.assertEqual(reasons,[])
            self.assertEqual(got['actual'],Decimal('14.385'))
            self.assertEqual(got['plan'],Decimal('29.370'))
            self.assertEqual(got['execution'],Decimal('-14.985'))
            self.assertEqual(got['qty_base'],Decimal('3'))
            self.assertEqual(got['avg_price'],Decimal('105'))
            self.assertEqual(got['cycles'],1)

    def test_missing_market_is_informational_not_fake_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.setup_chain(root,missing_market=True)
            reasons=[]
            self.assertIsNone(views.market_ledger(root,{'ordId':'market','state':'FILLED'},reason_sink=reasons))
            self.assertTrue(reasons[0].startswith('multi_leg_'))

    def test_shared_market_fill_split_across_two_cycles(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.setup_chain(root)
            state_path=root/'state/bot_state.json'
            st=json.loads(state_path.read_text())
            plan=[{'cycleId':'c1','sz':'3'},{'cycleId':'c2','sz':'5'}]
            st['aggregate_exits'][0]['plan']=plan
            st['aggregate_exits'][0]['sz']='8'
            st['cycles']['c1']['tpOrders'][-1]['aggregatePlan']=plan
            st['cycles']['c2']={
                'cycleId':'c2','state':'CLOSED','ordId':'buyB',
                'filledBuy':'5','filledTP':'5','tpPx':'110',
                'tpOrders':[{'ordId':'market','filled':'5',
                             'source':'aggregate_exit','aggregatePlan':plan}]}
            state_path.write_text(json.dumps(st))
            with closing(sqlite3.connect(root/'state/pnl.sqlite')) as db:
                db.execute('INSERT INTO accounted VALUES (?)',(json.dumps({
                    'buy_id':'buyB','sell_ids':['market'],'kind':'CLOSED',
                    'qty_contracts':'5','qty_base':'5','buy_px':'100',
                    'sell_px':'105','net':'23.975','planned_gross':'50'}),))
                db.commit()
            with closing(sqlite3.connect(root/'state/fills.sqlite')) as db:
                db.execute('UPDATE fills SET data=? WHERE order_id=?',
                    (json.dumps({'ordId':'market','tradeId':'markettrade',
                    'billId':'marketbill','fillSz':'8','fillPx':'105',
                    'fee':'-0.84','side':'sell','execType':'M',
                    'feeCcy':'USDT','fillTime':'1000'}),'market'))
                db.execute('INSERT INTO fills VALUES (?,?)',('buyB',json.dumps({
                    'ordId':'buyB','tradeId':'buyBtrade','billId':'buyBbill',
                    'fillSz':'5','fillPx':'100','fee':'-0.5','side':'buy',
                    'execType':'M','feeCcy':'USDT','fillTime':'1000'})))
                db.commit()
            result=views.market_ledger(root,{'ordId':'market','state':'FILLED'})
            self.assertIsNotNone(result)
            self.assertEqual(result['actual'],Decimal('38.360'))
            self.assertEqual(result['plan'],Decimal('78.320'))
            self.assertEqual(result['cycles'],2)
            self.assertEqual(result['qty_base'],Decimal('8'))

    def test_ledger_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.setup_chain(root,ledger_bad=True)
            reasons=[]
            self.assertIsNone(views.market_ledger(root,{'ordId':'market','state':'FILLED'},reason_sink=reasons))
            self.assertIn('multi_leg_fills_or_ledger_unverified',reasons)

    def test_multileg_24h_never_sends_danger_alarm(self):
        st={'market_events':{'soph/e':{'terminal':{'state':'FILLED','ordId':'market'},
                 'terminal_at':100,'_tg_accounting_last_reason':'multi_leg_fills_or_ledger_unverified'}}}
        notices=[]
        with patch.object(events,'one',side_effect=lambda _api,_st,key,msg,**kw:notices.append((key,msg))), \
             patch.object(events.time,'time',return_value=86501):
            events.market_process(None,st,{'soph':Path('/offline')})
        self.assertEqual(len(notices),1)
        self.assertIn('multileg_unverified_24h',notices[0][0])
        self.assertNotIn('🚨',notices[0][1])
        self.assertEqual(st['market_events'],{})


class PartialDiscovery(unittest.TestCase):
    def test_partial_scan_retains_prior_bot_mapping(self):
        tree=ast.parse((ROOT/'okx_telegram_monitor.py').read_text())
        node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='refresh_bots')
        ns={'BOT_DIRS':{'soph':'/previous/soph','bard':'/previous/bard'},
            'SERVICES':{'soph':'okx-grid-soph','bard':'okx-grid-bard'},
            '_DISCOVERY_LAST':0.0,'_DISCOVERY_LAST_WARN':0.0,
            '_DISCOVERY_PARTIAL_ERROR':True,
            'discover_bots':lambda:{'soph':('/current/soph','okx-grid-soph')},
            '_confirmed_archived':lambda *_:False,
            'time':time,'print':lambda *a,**kw:None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<test>','exec'),ns)
        ns['refresh_bots'](force=True)
        self.assertEqual(set(ns['BOT_DIRS']),{'soph','bard'})
        self.assertEqual(ns['BOT_DIRS']['bard'],'/previous/bard')
        self.assertEqual(ns['BOT_DIRS']['soph'],'/current/soph')

    def test_bad_registry_flags_partial_even_when_unit_is_readable(self):
        tree=ast.parse((ROOT/'okx_telegram_monitor.py').read_text())
        node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='discover_bots')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'bad.json').write_text('not json')
            (root/'okx-grid-soph.service').write_text('WorkingDirectory=/fake/soph\n')
            import re
            ns={'_DISCOVERY_PARTIAL_ERROR':False,
                'REGISTRY':root/'bad.json','SYSTEMD_DIR':root,'json':json,
                'Path':Path,'re':re,'_inside_base':lambda p:True}
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<test>','exec'),ns)
            self.assertEqual(set(ns['discover_bots']()),{'soph'})
            self.assertTrue(ns['_DISCOVERY_PARTIAL_ERROR'])


if __name__=='__main__':unittest.main()
