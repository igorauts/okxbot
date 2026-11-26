#!/usr/bin/env python3
"""Read-only local virtual TP -> MARKET fill correlation. NO OKX GET/POST.

Use on a *local copy* of an archived instance (e.g. completed ZEC), not to
trigger exits or collect new trades. Distinguishes VPS epoch and OKX fill clocks:
without independently verified clock-offset, cross-clock deltas are diagnostics
ONLY, never reported as calibrated end-to-end fill latency.
"""
import argparse
from contextlib import closing
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path


def tail(path, cap=64*1024*1024):
    if path.is_symlink() or not path.is_file():return []
    with path.open('rb') as f:
        size=f.seek(0,2);f.seek(max(0,size-cap))
        if size>cap:f.readline()
        lines=f.read(cap).splitlines()
    out=[]
    for line in lines:
        try:
            val=json.loads(line)
            if isinstance(val,dict):out.append(val)
        except (ValueError,UnicodeError):continue
    return out


def num(v):
    try:
        f=float(v)
        return f if math.isfinite(f) and f>0 else None
    except (ValueError,TypeError,OverflowError):return None


def row_fills(db_path,order_ids):
    """SQLite read-only, explicit exact order IDs, bounded query batches."""
    if not order_ids:return {}
    if db_path.is_symlink() or not db_path.is_file():
        raise RuntimeError('Missing/nonregular fills ledger; not a zero-fill proof')
    result=defaultdict(list)
    uri=db_path.resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=20)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for i in range(0,len(order_ids),200):
            ids=order_ids[i:i+200]
            sql='SELECT order_id,data FROM fills WHERE order_id IN ('+','.join('?'*len(ids))+')'
            for oid,data in db.execute(sql,ids):
                row=json.loads(data)
                if row.get('side')!='sell' or str(row.get('ordId'))!=str(oid):
                    continue
                ts=num(row.get('fillTime') or row.get('ts'))
                if ts is not None:
                    result[str(oid)].append((ts,str(row.get('tradeId') or '')))
    return result


def pct(v,p):
    v=sorted(v)
    return v[max(0,math.ceil(len(v)*p/100)-1)] if v else None


def correlate(events,fills,offset_ms=None):
    by_cl={}
    for event in events:
        kind=event.get('event')
        if kind not in ('AGG_MARKET_INTENT','AGG_MARKET_ACK','AGG_MARKET_TERMINAL'):
            continue
        clid=event.get('clOrdId')
        if not isinstance(clid,str) or not clid or len(clid)>128:
            continue
        current=by_cl.setdefault(clid,{})
        if kind in current:current['duplicate_'+kind]=True
        current[kind]=event
    results=[]
    for clid,item in by_cl.items():
        intent=item.get('AGG_MARKET_INTENT')
        ack=item.get('AGG_MARKET_ACK')
        terminal=item.get('AGG_MARKET_TERMINAL')
        if not (intent and ack and terminal) or any(k.startswith('duplicate_') for k in item):
            continue
        oid=str(terminal.get('ordId') or ack.get('ordId') or '')
        if not oid or oid not in fills:continue
        # Source records first crossing as local seconds converted to ms.
        cross=num(intent.get('first_cross_unix_ms'))
        it=num(intent.get('intent_unix_ms'))
        if cross is None or it is None or cross>it:
            continue
        ff=sorted(fills[oid]);first,last=ff[0][0],ff[-1][0]
        if len(set((t,id) for t,id in ff))!=len(ff):
            continue
        if first<1e11 or last<1e11:
            continue  # Exchange timestamps must be epoch milliseconds.
        result={'clOrdId':clid,'ordId':oid,'n_fills':len(ff),
                'cross_to_intent_local_ms':round(it-cross,3),
                'first_fill_exchange_unix_ms':first,'last_fill_exchange_unix_ms':last}
        if offset_ms is not None:
            # offset = OKX exchange clock epoch - local VPS epoch (ms).
            corrected=first-offset_ms-cross
            if corrected >= 0 and corrected < 3_600_000:
                result['cross_to_first_fill_estimate_ms']=round(corrected,3)
                result['intent_to_first_fill_estimate_ms']=round(first-offset_ms-it,3)
        results.append(result)
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('bot_root',type=Path)
    p.add_argument('--confirmed-clock-offset-ms',type=float,
        help='EXTERNAL confirmed OKX clock minus VPS clock; no automatic guesses')
    a=p.parse_args()
    root=a.bot_root
    if root.is_symlink() or not root.is_dir():raise SystemExit('Invalid root')
    events=tail(root/'logs/bot_events.jsonl')
    ids=sorted(set(str(e['ordId']) for e in events
                   if e.get('event')=='AGG_MARKET_TERMINAL' and e.get('ordId')))
    fills=row_fills(root/'state/fills.sqlite',ids)
    result=correlate(events,fills,a.confirmed_clock_offset_ms)
    durations=[x['cross_to_intent_local_ms'] for x in result]
    exchanges=[x['cross_to_first_fill_estimate_ms'] for x in result
               if 'cross_to_first_fill_estimate_ms' in x]
    print(json.dumps({'mode':'READ_ONLY_LOCAL_FILES',
        'clock_offset_proven':a.confirmed_clock_offset_ms is not None,
        'matched_n':len(result),
        'cross_to_intent':{'n':len(durations),'p50_ms':pct(durations,50),'p95_ms':pct(durations,95)},
        'cross_to_first_fill_ESTIMATED_IF_OFFSET_PROVEN':{
            'n':len(exchanges),'p50_ms':pct(exchanges,50),'p95_ms':pct(exchanges,95)},
        'warning':('No cross-clock estimate without operator-confirmed clock-offset; '
                   'small N is not an SLA'),
        'orders':result},indent=2,ensure_ascii=False))

if __name__=='__main__':main()
