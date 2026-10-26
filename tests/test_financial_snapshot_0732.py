#!/usr/bin/env python3
"""Offline snapshot-consistency regressions. No OKX / service / LIVE imports."""
import ast
import hashlib
import json
import sqlite3
import sys
import tempfile
import time as real_time
import unittest
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_accounting import build_pnl_ledger


def functions(*names):
    tree=ast.parse((ROOT/'okx_bot.py').read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names),(names,[n.name for n in nodes])
    env={'Decimal':Decimal,'D':lambda v: Decimal(str(v or '0')),
         'json':json,'hashlib':hashlib,'datetime':datetime,'timezone':timezone,
         'time':real_time}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'okx_bot.py','exec'),env)
    return env


def row(oid,bill,qty,side='buy',px='100',fee='-0.01',at='1000'):
    return {'ordId':oid,'billId':bill,'tradeId':bill,'fillSz':str(qty),
            'fillPx':str(px),'fee':str(fee),'feeCcy':'USDT',
            'execType':'M','side':side,'ts':str(at)}


class FinancialSnapshotTests(unittest.TestCase):
    def test_trim_rejects_mid_fill_and_keeps_prior_whole_fills(self):
        trim=functions('trim_verified_fills_for_snapshot')['trim_verified_fills_for_snapshot']
        expected={'buy':Decimal('2'),'sell':Decimal('1')}
        rows=[row('buy','one','1'),row('buy','two','1',at='2000'),
              row('buy','three','1',at='3000'),row('sell','four','1',side='sell')]
        result=trim(rows,expected)
        self.assertEqual({r['billId'] for r in result},{'one','two','four'})
        self.assertEqual(len(result),3)
        self.assertIsNone(trim([row('buy','a','1'),row('buy','b','2')],{'buy':Decimal('2')}))
        self.assertIsNone(trim([row('buy','a','1')],{'buy':Decimal('2')}))

    def test_trim_duplicate_bill_and_extra_order_fail_closed(self):
        trim=functions('trim_verified_fills_for_snapshot')['trim_verified_fills_for_snapshot']
        with self.assertRaises(ValueError):
            trim([row('buy','one','1')]*2,{'buy':Decimal('2')})
        with self.assertRaises(ValueError):
            trim([row('unrelated','one','1')],{'buy':Decimal('1')})
        self.assertEqual(trim([],{}),[])

    def test_ledger_sum_uses_only_closed_snapshot_cycles(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'pnl.sqlite'
            Ledger=build_pnl_ledger(D=Decimal,STEP=Decimal('1'),A=None,
                RateLimitedError=Exception,allocated_market_fills=None,
                fmt_accounted_price=str,money=str,money_usdt=str,bold=str,
                die=lambda msg: (_ for _ in ()).throw(ValueError(msg)),PNL_DB=db)
            ledger=Ledger(Decimal('1'))
            with ledger._managed_conn() as con:
                con.executemany('INSERT INTO accounted VALUES (?,?,?,?)',[
                    ('b1:s1','CLOSED','0.25','{}'),
                    ('b2:s2','CLOSED','0.50','{}'),
                    ('new:later','CLOSED','0.75','{}')])
            snapshot={'archived_cycles':[{'cycleId':'1','ordId':'b1','tpOrdId':'s1','state':'CLOSED'}],
                      'cycles':{'2':{'cycleId':'2','ordId':'b2','tpOrdId':'s2','state':'CLOSED'}}}
            self.assertEqual(ledger.total_for_snapshot(snapshot,[]),Decimal('0.75'))
            # Later trade already durable in SQLite does not enter older report.
            with self.assertRaisesRegex(ValueError,'Missing/mismatched'):
                ledger.total_for_snapshot({'cycles':{'x':{'cycleId':'x','ordId':'absent',
                    'tpOrdId':'missing','state':'CLOSED'}}},[])
            # A snap archive must remain immutable even when newer rows arrive.
            duplicate_archive=[{'cycleId':'1','ordId':'older','tpOrdId':'other','state':'CLOSED'}]
            self.assertEqual(ledger.total_for_snapshot(snapshot,duplicate_archive),Decimal('0.75'))

    def test_real_closed_cycle_money_exact_after_later_fill(self):
        ns=functions('all_stat_cycles','accumulate_expected_sell',
            'expected_filled_orders','trim_verified_fills_for_snapshot',
            'full_stats_snapshot')
        ns['archived_rows']=lambda: []
        ns['allocated_market_fills']=lambda *args: None
        ns['stats_pct']=lambda *args:'—'
        cycle={'cycleId':'c','ordId':'buy','tpOrdId':'sell','state':'CLOSED',
               'filledBuy':'1','filledTP':'1',
               'tpOrders':[{'ordId':'sell','filled':'1'}]}
        state={'cycles':{},'archived_cycles':[cycle],'completed':1}
        expected=ns['expected_filled_orders'](state,snapshot_archive=[])
        self.assertEqual(expected,{'buy':Decimal('1'),'sell':Decimal('1')})
        rows=[row('buy','one','1',px='100',fee='-0.01'),
              row('sell','two','1',side='sell',px='101',fee='-0.01'),
              row('buy','late','1',px='110',at='2000')]
        pinned=ns['trim_verified_fills_for_snapshot'](rows,expected)
        self.assertEqual(len(pinned),2)
        stats=ns['full_stats_snapshot'](state,pinned,Decimal('105'),Decimal('1'),
                                        snapshot_archive=[])
        self.assertTrue(stats['complete'])
        self.assertEqual(stats['grid_gain'],Decimal('0.98'))
        self.assertEqual(stats['open_contracts'],Decimal('0'))

    def test_shared_market_sell_allocation_and_extra_later_fill(self):
        ns=functions('all_stat_cycles','accumulate_expected_sell',
            'expected_filled_orders','trim_verified_fills_for_snapshot',
            'full_stats_snapshot','allocated_market_fills')
        ns.update(archived_rows=lambda:[],stats_pct=lambda *args:'—')
        plan=[{'cycleId':'a','sz':'1'},{'cycleId':'b','sz':'1'}]
        def cycle(cid,oid):
            return {'cycleId':cid,'ordId':oid,'state':'CLOSED',
                    'filledBuy':'1','filledTP':'1',
                    'tpOrders':[{'ordId':'market','filled':'1','source':'aggregate_exit',
                                 'aggregatePlan':plan}]}
        st={'cycles':{},'archived_cycles':[cycle('a','buyA'),cycle('b','buyB')]}
        expected=ns['expected_filled_orders'](st,snapshot_archive=[])
        self.assertEqual(expected['market'],Decimal('2'))
        rows=[row('buyA','ba','1',px='100',fee='-0.01'),
              row('buyB','bb','1',px='100',fee='-0.01'),
              row('market','m1','1',side='sell',px='101',fee='-0.01',at='1500'),
              row('market','m2','1',side='sell',px='102',fee='-0.01',at='1600'),
              row('market','m3','1',side='sell',px='99',fee='-0.01',at='1700')]
        pinned=ns['trim_verified_fills_for_snapshot'](rows,expected)
        self.assertEqual(len(pinned),4)
        stats=ns['full_stats_snapshot'](st,pinned,Decimal('103'),Decimal('1'),
                                        snapshot_archive=[])
        self.assertTrue(stats['complete'])
        self.assertEqual(stats['closed_count'],2)
        self.assertEqual(stats['grid_gain'],Decimal('2.96'))
        self.assertEqual(stats['market_tp_net'],Decimal('2.96'))
        self.assertEqual(stats['limit_tp_net'],Decimal('0'))
        self.assertEqual(stats['total_fees'],Decimal('-0.04'))

    def test_publication_uses_captured_timestamp_even_if_trading_continues(self):
        ns=functions('print_full_stats','trim_verified_fills_for_snapshot','financial_signature')
        class Clock:
            def __init__(self):self.t=1000.0
            def time(self):return self.t
        clock=Clock()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            state=root/'state.json'; state.write_text(json.dumps({'instrument':'ZEC-USDT-SWAP',
                'cycles':{'buy':{'cycleId':'buy','ordId':'b','state':'TP_WAIT',
                                 'filledBuy':'1','filledTP':'0'}}}))
            newer={'instrument':'ZEC-USDT-SWAP','cycles':{'buy':{'cycleId':'buy',
                'ordId':'b','state':'CLOSED','filledBuy':'1','filledTP':'1'}}}
            ns.update(DATA=state,INST='ZEC-USDT-SWAP',A=SimpleNamespace(ticker=lambda:Decimal('100')),
                archived_rows=lambda:[],time=clock,LIVE=True,
                STATS_FINANCIAL_ANCHOR=root/'anchor.json',STATS_FILE=root/'stats.json',
                VER='current-engine',ExchangeUnresolved=RuntimeError)
            def fetch(st,**kwargs):
                state.write_text(json.dumps(newer))
                clock.t=1060.0  # financial REST finishes one minute later
                return [row('b','one','1'),row('b','later','1',at='2000')]
            ns['stats_exchange_fills']=fetch
            ns['expected_filled_orders']=lambda st,**kw:{'b':Decimal('1')}
            ns['full_stats_snapshot']=lambda st,f,b,v,**kw: {
                'complete':True,'grid_gain':Decimal('0'),'total_gain':Decimal('4.25'),
                'open_contracts':Decimal('1')}
            ns['CONTRACT_VALUE']=Decimal('1')
            ns['_first_anchor']=lambda fills:(100.0,None)
            ns['format_okx_report']=lambda *a,**kw:'REPORT'
            ns['performance_snapshot']=lambda *a,**kw:{'schema':1,'total_pnl':'4.25'}
            ns['atomic_json']=lambda path,obj:Path(path).write_text(json.dumps(obj))
            calls=[]
            class Pnl:
                def ingest_history(self,st,fills):
                    calls.append(('ingest',len(fills)))
                    return 0
                def total_for_snapshot(self,st,archive):
                    calls.append(('checked',len(archive)))
                    return Decimal('0')
                def total(self):raise AssertionError('Must never use moving global pnl.total()')
            self.assertTrue(ns['print_full_stats'](Pnl()))
            st=json.loads((root/'stats.json').read_text())
            anchor=json.loads((root/'anchor.json').read_text())
            self.assertEqual(st['generated_at_unix'],1000)
            self.assertEqual(anchor['at'],1000)
            self.assertEqual(st['performance']['total_pnl'],'4.25')
            self.assertEqual(calls,[('ingest',1),('checked',0)])
            self.assertNotEqual(anchor['signature'],ns['financial_signature'](newer))

    def test_account_pnl_mismatch_does_not_overwrite_valid_report(self):
        ns=functions('print_full_stats','trim_verified_fills_for_snapshot')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);state=root/'bot_state.json'
            state.write_text(json.dumps({'instrument':'ZEC-USDT-SWAP'}))
            ns.update(DATA=state,INST='ZEC-USDT-SWAP',A=SimpleNamespace(ticker=lambda:Decimal('100')),
                archived_rows=lambda:[],time=SimpleNamespace(time=lambda:1000.0),LIVE=True,
                STATS_FINANCIAL_ANCHOR=root/'anchor.json',STATS_FILE=root/'stats.json',
                VER='current-engine',ExchangeUnresolved=RuntimeError,
                expected_filled_orders=lambda *a,**k:{},
                stats_exchange_fills=lambda *a,**k:[],
                full_stats_snapshot=lambda *a,**k:{'complete':True,
                    'grid_gain':Decimal('1'),'total_gain':Decimal('1'),
                    'open_contracts':Decimal('0')},CONTRACT_VALUE=Decimal('1'))
            class Pnl:
                def ingest_history(self,*a):return 0
                def total_for_snapshot(self,*a):return Decimal('2')
            self.assertFalse(ns['print_full_stats'](Pnl()))
            self.assertFalse((root/'stats.json').exists())
            self.assertFalse((root/'anchor.json').exists())


if __name__=='__main__':unittest.main(verbosity=2)
