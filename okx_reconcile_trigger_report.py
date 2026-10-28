#!/usr/bin/env python3
"""Read-only wake-source report for one bot; no credentials, REST or trade actions.

For use AFTER approval of diagnostic engine and accumulation of real telemetry.
Every reconcile_trigger is emitted just before a reconcile_position call.
Matching is conservative: only a trigger with exactly one completion before the
next trigger is paired; unmatched records are reported, never silently guessed.
"""
import argparse
import bisect
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _ts(v):
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return d.timestamp() if d.tzinfo else None
    except (ValueError,TypeError):
        return None


def _pct(data,p):
    if not data:return None
    ordered=sorted(data)
    return ordered[max(0,math.ceil(len(ordered)*p/100)-1)]


def _ms(v):
    try:
        x=float(v)
        return x if math.isfinite(x) and 0<=x<=3600000 else None
    except (ValueError,TypeError):
        return None


def _read(path,after,before,max_size=48*1024*1024):
    if not path.is_file() or path.is_symlink():return []
    result=[]
    with path.open('rb') as f:
        size=f.seek(0,2)
        offset=max(0,size-max_size)
        f.seek(offset)
        if offset:f.readline()
        for b in f:
            try:
                row=json.loads(b)
                if not isinstance(row,dict):continue
                t=_ts(row.get('ts'))
                if t is not None and after<=t<=before:
                    phase=row.get('phase')
                    if phase in ('reconcile_trigger','position_reconcile'):
                        result.append((t,row))
            except (ValueError,UnicodeError,TypeError):
                pass
    return result


def report(logdir,hours):
    now=datetime.now(timezone.utc).timestamp()
    after=now-hours*3600
    lines=[]
    for name in ('latency.jsonl.1','latency.jsonl'):
        lines+=_read(logdir/name,after,now)
    lines.sort(key=lambda x:x[0])
    totals=Counter()
    timings=defaultdict(list)
    unmatched=0
    pending=None
    for t,row in lines:
        phase=row.get('phase')
        if phase=='reconcile_trigger':
            if pending is not None:unmatched+=1
            pending=(t,str(row.get('source','unknown')),
                     str(row.get('reason','unknown')))
            totals['triggers']+=1
        elif phase=='position_reconcile':
            if pending is None:
                unmatched+=1
                continue
            start,source,detail=pending
            pending=None
            value=_ms(row.get('ms'))
            if value is None or t<start:
                unmatched+=1
                continue
            totals[source]+=1
            totals['reconciles']+=1
            timings[source].append(value)
    if pending is not None:unmatched+=1
    print('DOOD (or supplied bot) RECONCILE WAKE ANALYSIS -- LOCAL READ ONLY')
    print('UTC window:',datetime.fromtimestamp(after,timezone.utc).isoformat(),
          'to',datetime.fromtimestamp(now,timezone.utc).isoformat())
    print('Triggers:',totals['triggers'],'| paired:',totals['reconciles'],
          '| unmatched:',unmatched)
    print('Wake reasons: source is first priority when multiple causes overlap')
    print(f"{'SOURCE':<17} {'N':>7} {'SHARE':>8} {'P50 ms':>10} {'P95 ms':>10} {'MAX ms':>10}")
    for source in ('private','virtual','grid','timer','wake','internal','overrun','recovery'):
        vals=timings[source]
        n=len(vals)
        if n:
            print(f'{source:<17} {n:>7} {n/max(1,totals["reconciles"])*100:>7.1f}% '
                  f'{_pct(vals,50):>10.1f} {_pct(vals,95):>10.1f} {max(vals):>10.1f}')
    print('No trading orders, no position reads, no OKX calls.')


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log-dir',type=Path,default=Path('/root/okx/dood0735/logs'))
    ap.add_argument('--hours',type=float,default=1.0)
    args=ap.parse_args()
    if not (0<args.hours<=24):ap.error('--hours must be 0..24')
    report(args.log_dir,args.hours)
