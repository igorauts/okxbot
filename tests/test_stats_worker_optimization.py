#!/usr/bin/env python3
"""Offline StatsWorker CPU and wire compatibility. No exchange or LIVE access."""
import ast
import json
import hashlib
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def isolated(*names):
    tree=ast.parse((ROOT/'okx_bot.py').read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names), names
    env={'Decimal':Decimal,'D':lambda value:Decimal(str(value or '0')),
         'time':time,'json':json,'hashlib':hashlib,'datetime':datetime,
         'timezone':timezone,'stats_pct':lambda n,d:'—' if not d else str(n/d*100),
         'archived_rows':lambda:[]}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(ROOT/'okx_bot.py'),'exec'),env)
    return env


def closed_fixture(count):
    cycles=[]
    fills=[]
    for i in range(count):
        key=str(i)
        buy='b'+key;sell='s'+key
        cycles.append({'cycleId':'cycle'+key,'ordId':buy,'tpOrdId':sell,
                       'state':'CLOSED','filledBuy':'1','filledTP':'1',
                       'tpOrders':[{'ordId':sell,'filled':'1','source':'attached'}]})
        for oid,side,price in ((buy,'buy','100'),(sell,'sell','101')):
            fills.append({'ordId':oid,'tradeId':'t'+oid,'billId':'bill'+oid,
                          'fillSz':'1','fillPx':price,'fee':'-0.01',
                          'feeCcy':'USDT','execType':'M','side':side,
                          'ts':'1700000000000'})
    return {'cycles':{},'archived_cycles':cycles,'completed':count},fills


class StatsCPU(unittest.TestCase):
    def test_single_index_not_rebuilt_per_cycle(self):
        tree=ast.parse((ROOT/'okx_bot.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='full_stats_snapshot')
        # The formerly quadratic fill_by_id construction must stay before
        # the per-cycle SELL loop. Structural guard: no nested full-fill scan.
        lines=(ROOT/'okx_bot.py').read_text().splitlines()
        fragment='\n'.join(lines[fn.lineno-1:fn.end_lineno])
        assert fragment.count('fill_by_id={}')==1
        self.assertLess(fragment.index('fill_by_id={}'),fragment.index('for c in cycles:\n        b=grouped'))
        self.assertIn('for f in unique_fills:\n        fill_by_id.setdefault',fragment)

    def test_many_closed_cycles_exact_net_and_ledger_fields(self):
        calc=isolated('all_stat_cycles','accumulate_expected_sell','allocated_market_fills',
                      'full_stats_snapshot')['full_stats_snapshot']
        st,fills=closed_fixture(1500)
        t0=time.perf_counter()
        result=calc(st,fills,Decimal('100'),Decimal('1'),snapshot_archive=[])
        elapsed=time.perf_counter()-t0
        self.assertTrue(result['complete'])
        self.assertEqual(result['grid_gain'],Decimal('1470'))
        self.assertEqual(result['closed_count'],1500)
        self.assertEqual(result['limit_tp_net'],Decimal('1470'))
        self.assertEqual(result['market_tp_net'],Decimal(0))
        self.assertEqual(result['total_fees'],Decimal('-30'))
        self.assertEqual(result['maker_volume'],Decimal('301500'))
        self.assertEqual(result['open_contracts'],Decimal(0))
        # Deliberately generous; catches the old quadratic regression without
        # relying on sub-second CI / VPS timing on shared CPUs.
        self.assertLess(elapsed,5.0,f'Expected O(cycles+fills), got {elapsed:.2f}s')

    def test_one_24h_calculation_for_text_and_json(self):
        ns=isolated('print_full_stats','trim_verified_fills_for_snapshot')
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            p=Path(tmp)/'state.json'
            p.write_text(json.dumps({'instrument':'TEST-USDT-SWAP','cycles':{}}))
            ns.update(DATA=p,INST='TEST-USDT-SWAP',A=SimpleNamespace(ticker=lambda:Decimal('100')),
                archived_rows=lambda:[],LIVE=False,CONTRACT_VALUE=Decimal('1'),
                VER='candidate',expected_filled_orders=lambda *a,**k:{},
                stats_exchange_fills=lambda *a,**k:[],
                full_stats_snapshot=lambda *a,**k:{'complete':True,
                    'grid_gain':Decimal('1'),'total_gain':Decimal('1'),
                    'open_contracts':Decimal('0')},
                _first_anchor=lambda *a:(1234.0,Decimal('100')))
            ns['_last_24h_profit']=Mock(return_value=Decimal('1.2345'))
            seen=[]
            ns['format_okx_report']=lambda *a,**k:seen.append(k['precomputed_24h']) or 'REPORT'
            ns['performance_snapshot']=lambda *a,**k:seen.append(k['precomputed_24h']) or {'schema':1}
            self.assertTrue(ns['print_full_stats']())
            self.assertEqual(seen,[Decimal('1.2345'),Decimal('1.2345')])
            self.assertEqual(ns['_last_24h_profit'].call_count,1)


if __name__=='__main__':unittest.main(verbosity=2)
