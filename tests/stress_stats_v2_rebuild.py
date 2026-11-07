#!/usr/bin/env python3
"""Synthetic offline load of StatsWorker-v2 rebuild. No network, no live bot state.

Purposely uses pinned original AST oracle on BOUNDED freeze components only,
never runs an ORIGINAL FULL history pass. Stress is isolated to temp SQLite.
"""
import argparse
import hashlib
import json
import os
import resource
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
sys.path.insert(0,str(SRC))
import ast
import gzip
ORACLE_SHA='9c6cf1b46ba8f0b045a80ea97b3178657fd296c2e6d564fb8579839e66057cbd'
ORACLE_ARCHIVE=HERE/'fixtures/okx_bot_original_full_v0760.py.gz'
raw=gzip.decompress(ORACLE_ARCHIVE.read_bytes())
assert hashlib.sha256(raw).hexdigest()==ORACLE_SHA, 'Pinned historical oracle mismatch'
need={'all_stat_cycles', 'accumulate_expected_sell', 'allocated_market_fills',
      'stats_pct', 'full_stats_snapshot', '_last_24h_profit'}
nodes=[n for n in ast.parse(raw).body if isinstance(n,ast.FunctionDef) and n.name in need]
assert len(nodes)==len(need)
O={'Decimal':Decimal, 'D': lambda v:Decimal(str(v or '0'))}
exec(compile(ast.Module(body=nodes,type_ignores=[]),'TEST_ONLY_FINANCIAL_AST','exec'),O)
NOW=1800000000000

def closed(cid,buy,sell):
    return {'cycleId':cid,'ordId':buy,'tpOrdId':sell,'filledBuy':'1',
            'filledTP':'1','state':'CLOSED',
            'tpOrders':[{'ordId':sell,'filled':'1'}]}

def fill(oid,bill,qty,side,px):
    return {'ordId':oid,'billId':bill,'tradeId':bill,'fillSz':str(qty),
            'fillPx':str(px),'side':side,'fee':'-0.01','feeCcy':'USDT',
            'execType':'M','ts':str(NOW-1000)}
from okx_stats_v2_contract import NotReady, Drift
from okx_stats_v2_production import SingleAuthorityStats
from okx_stats_v2_phase3 import DurableStager
from okx_stats_v2_recovery import quarantine_sidecar, resume_interrupted, finish_rebuild

INST='SYNTH-USDT-SWAP'

class DummyFills:
    def ensure_expected(self,expected):
        raise RuntimeError('Unexpected missing HOT fills on complete synthetic source')

def setup(root, count):
    state=root/'state';state.mkdir(parents=True)
    (state/'bot_state.json').write_text(json.dumps({'instrument':INST,'cycles':{},'archived_cycles':[],
                                                    'completed':count}))
    conns=[]
    schemas=(
        ('cycles.sqlite','CREATE TABLE cycles(id TEXT PRIMARY KEY, data TEXT NOT NULL)'),
        ('pnl.sqlite','CREATE TABLE accounted(id TEXT PRIMARY KEY, kind TEXT NOT NULL, net TEXT NOT NULL, data TEXT NOT NULL)'),
        ('fills.sqlite','CREATE TABLE fills(order_id TEXT NOT NULL, bill_id TEXT NOT NULL, qty TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(order_id,bill_id))'))
    for name,ddl in schemas:
        db=sqlite3.connect(state/name)
        db.execute(ddl)
        conns.append(db)
    ac,pnl,fills=conns
    t0=time.monotonic()
    for idx in range(count):
        ident=str(idx)
        buy='buy-'+ident;sell='sell-'+ident
        c=closed('cycle-'+ident,buy,sell)
        ac.execute('INSERT INTO cycles VALUES (?,?)',(c['cycleId'],json.dumps(c,separators=(',',':'))))
        row={'kind':'CLOSED','net':'0.98'}
        pnl.execute('INSERT INTO accounted VALUES (?,?,?,?)',(buy+':'+sell,'CLOSED','0.98',json.dumps(row)))
        a=fill(buy,'bill-buy-'+ident,1,'buy',100)
        b=fill(sell,'bill-sell-'+ident,1,'sell',101)
        for f in (a,b):
            fills.execute('INSERT INTO fills VALUES (?,?,?,?)',(f['ordId'],f['billId'],f['fillSz'],json.dumps(f,separators=(',',':'))))
    for db in conns:db.commit();db.close()
    return round(time.monotonic()-t0,2)

def digest_sources(root):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root/'state').iterdir()) if p.name in
                 ('bot_state.json','cycles.sqlite','pnl.sqlite','fills.sqlite')}

def atom(path,body):
    temp=path.with_name(path.name+'.temp')
    with open(temp,'w') as f:
        json.dump(body,f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def run(count, timeout_s):
    with tempfile.TemporaryDirectory(prefix='stats-v2-stress-') as dirname:
        root=Path(dirname)
        prepared=setup(root,count)
        source_before=digest_sources(root)
        sidecar=root/'state/stats_v2.sqlite'
        d=DurableStager(sidecar,INST);d.close()
        t0=time.monotonic()
        backup=quarantine_sidecar(root,write_json=atom)
        resume_interrupted(root,write_json=atom)
        print('SYNTHETIC_SOURCE_CREATED',count,'seconds',prepared,flush=True)
        print('REBUILD_BEGIN',count,'backup_bytes',backup.stat().st_size,flush=True)
        worker=SingleAuthorityStats(root,INST,sidecar,oracle=O,fill_store=DummyFills(),
                                     expected_filled_orders=lambda *a,**kw:{},
                                     contract_value=Decimal('1'),freeze_budget=64)
        attempts=0;failed=0;last=None
        try:
            while True:
                if time.monotonic()-t0>timeout_s:
                    raise TimeoutError('REBUILD_TIMEOUT after '+str(attempts)+' attempts')
                attempts+=1
                try:
                    result=worker.compute('110',now=NOW/1000)
                    last=result
                    if worker.stager.pending_count()==0:
                        break
                except NotReady as exc:
                    failed+=1
                    if 'HOT cycle bound reached' not in str(exc):raise
                if attempts%25==0:
                    print('REBUILD_PROGRESS attempts',attempts,'frozen',worker.stager.frozen_count(),
                          'pending',worker.stager.pending_count(),
                          'wall_s',round(time.monotonic()-t0,1),flush=True)
            # No source scans / replica ledger writes after verified result.
            assert last.stats['complete'] is True
            assert last.stats['grid_gain']==Decimal('0.98')*count, (last.stats['grid_gain'],count)
            assert last.stats['closed_count']==count
            assert worker.stager.frozen_count()==count
            worker.close()
            assert finish_rebuild(root,write_json=atom)
            elapsed=round(time.monotonic()-t0,2)
            assert source_before==digest_sources(root),'AUTHORITATIVE_SOURCE_CHANGED'
            print('===== OFFLINE_SYNTHETIC_REBUILD_PASS =====',flush=True)
            print('cycles',count,'attempts',attempts,'notready',failed,'elapsed_s',elapsed,
                'rss_peak_MiB',round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1),
                'sidecar_MiB',round(sidecar.stat().st_size/(1024**2),1),flush=True)
        finally:
            worker.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--cycles',type=int,default=3000)
    parser.add_argument('--timeout',type=int,default=180)
    a=parser.parse_args()
    if not 1<=a.cycles<=30000:raise SystemExit('Invalid cycle count')
    # Limit address space at process boundary; never touches live resources.
    resource.setrlimit(resource.RLIMIT_AS,(1024**3,1024**3))
    run(a.cycles,a.timeout)
