#!/usr/bin/env python3
"""Read-only bounded virtual TP correlation report (RC10).

Inspect local logs only. No exchange requests, credentials, filesystem writes,
position math, or trading actions. A trace_id is a diagnostics cohort, NOT an
order identity; clOrdId links AGG_MARKET_INTENT to ACK/TERMINAL in event logs.
"""
import argparse
from bisect import bisect_left
import json
import math
from pathlib import Path

PHASES=('tp_ws_cross','tp_collect_started','rest_gate','http','positions_gate',
        'position_reader_ipc','position_reader_quota','position_reader_http',
        'tp_cross_to_collect','tp_collect_to_intent','tp_to_intent',
        'tp_intent_to_submit','tp_recon_phase','stats_stage')


def tail_jsonl(path, *, limit=64*1024*1024):
    """Read only a bounded tail; avoid following symlinks or partial JSONL."""
    path=Path(path)
    if not path.is_file() or path.is_symlink():return []
    with path.open('rb') as stream:
        end=stream.seek(0,2)
        stream.seek(max(0,end-limit))
        if end>limit:stream.readline()  # partial first line
        buf=stream.read(limit)
    out=[]
    for line in buf.splitlines():
        try:
            rec=json.loads(line)
            if isinstance(rec,dict):out.append(rec)
        except (ValueError,UnicodeError):continue
    return out


def percentile(samples,p):
    if not samples:return None
    values=sorted(samples)
    return values[max(0,math.ceil(len(values)*p/100)-1)]


def valid_interval(row):
    try:
        a=float(row['start_unix_ms']);b=float(row['end_unix_ms'])
        if math.isfinite(a) and math.isfinite(b) and 0<a<=b and b-a<=3_600_000:
            return a,b
    except (ValueError,TypeError,KeyError,OverflowError):
        pass
    return None


def overlap_stats(tp_intervals,stats_intervals):
    """Correlation only. Local wall clock; no causal/performance claim.

    Prefix maxima make overlap counting O((N+M) log M), even for large
    bounded tails. Missing or corrupt intervals are excluded, not zeros.
    """
    intervals=sorted(stats_intervals)
    starts=[a for a,b in intervals]
    max_ends=[];latest=0.0
    for _,b in intervals:
        latest=max(latest,b);max_ends.append(latest)
    overlap=[];no_overlap=[]
    for a,b,ms in tp_intervals:
        idx=bisect_left(starts,b)-1
        target=overlap if idx>=0 and max_ends[idx]>a else no_overlap
        target.append(ms)
    def dist(values):
        return {'n':len(values),'p50_ms':percentile(values,50),
                'p95_ms':percentile(values,95)}
    return {'eligible_tp_intervals':len(tp_intervals),
            'stats_intervals':len(stats_intervals),
            'with_stats':dist(overlap),'without_stats':dist(no_overlap),
            'interpretation':'overlap_only_not_causality'}


def summarize(latency,events=()):
    phases={name:[] for name in PHASES}
    traces={}
    position_http_traces=set()
    recon_steps={}
    stats_steps={}
    stats_intervals=[]
    tp_intervals=[]
    for row in latency:
        phase=row.get('phase')
        if phase=='stats_stage':
            interval=valid_interval(row)
            if interval is not None:
                stats_intervals.append(interval)
            try:
                name=row.get('step');ms=float(row['ms'])
                if name in ('capture','fills','calculation','ledger','report','write') and math.isfinite(ms) and ms>=0:
                    stats_steps.setdefault(name,[]).append(ms)
            except (TypeError,ValueError,KeyError):pass
        elif phase=='tp_to_intent':
            interval=valid_interval(row)
            if interval is not None:
                try:
                    ms=float(row['ms'])
                    if math.isfinite(ms) and ms>=0 and isinstance(row.get('trace_id'),str) and len(row['trace_id'])==24:
                        tp_intervals.append((*interval,ms))
                except (TypeError,ValueError,KeyError):pass
        elif phase=='tp_recon_phase':
            try:
                name=row.get('step');ms=float(row['ms'])
                if name in ('pending','register_attached','track','verify_existing',
                            'positions','rest_governor','rest_http','retry_sleep') and math.isfinite(ms) and ms>=0:
                    recon_steps.setdefault(name,[]).append(ms)
            except (TypeError,ValueError,KeyError):pass
        trace=row.get('trace_id')
        if not isinstance(trace,str) or len(trace)!=24 or not trace.isalnum() or phase not in phases:
            continue
        try:
            ms=float(row['ms'])
            if ms<0 or not math.isfinite(ms):continue
        except (TypeError,ValueError,KeyError):continue
        traces.setdefault(trace,{}).setdefault(phase,[]).append(ms)
        if phase=='http' and row.get('route')=='/api/v5/account/positions':
            position_http_traces.add(trace)
        phases[phase].append(ms)
    by_clid={}
    event_count={'AGG_MARKET_INTENT':0,'AGG_MARKET_ACK':0,'AGG_MARKET_TERMINAL':0}
    for event in events:
        kind=event.get('event')
        if kind not in event_count:continue
        event_count[kind]+=1
        cid=event.get('clOrdId')
        trace=event.get('trace_id')
        if isinstance(cid,str) and cid and len(cid)<=128:
            obj=by_clid.setdefault(cid,{})
            obj[kind]=True
            if isinstance(trace,str) and len(trace)==24 and trace.isalnum():obj['trace_id']=trace
    completed=sum(bool(row.get('AGG_MARKET_INTENT') and row.get('AGG_MARKET_ACK'))
                  for row in by_clid.values())
    # Missing ACK is NOT a rejected/failed order and must never be classified as such.
    return {'traces':len(traces),'phase_stats':{
                name:{'n':len(xs),'p50_ms':percentile(xs,50),
                      'p95_ms':percentile(xs,95),'max_ms':max(xs) if xs else None}
                for name,xs in phases.items()},
            'events':event_count,'linked_intent_ack_count':completed,
            'event_order_ids_seen':len(by_clid),
            'traces_with_intent':sum('tp_to_intent' in row for row in traces.values()),
            'traces_with_position_http':len(position_http_traces),
            'reconciliation_steps':{name:{'n':len(xs),'p50_ms':percentile(xs,50),
                                      'p95_ms':percentile(xs,95)} for name,xs in recon_steps.items()},
            'stats_steps':{name:{'n':len(xs),'p50_ms':percentile(xs,50),
                           'p95_ms':percentile(xs,95)} for name,xs in stats_steps.items()},
            'tp_stats_overlap':overlap_stats(tp_intervals,stats_intervals)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bot_root',type=Path,help='Bot instance directory; GET-free offline report')
    parser.add_argument('--json',action='store_true',help='Machine-readable output')
    args=parser.parse_args(argv)
    root=args.bot_root
    summary=summarize(tail_jsonl(root/'logs/latency.jsonl'),
                      tail_jsonl(root/'logs/bot_events.jsonl'))
    if args.json:
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return
    print(f'TP TRACE — {root.name} | локальные журналы, без OKX')
    print(f'Всего trace_id: {summary["traces"]} | с BID→Intent: {summary["traces_with_intent"]}')
    print(f'С HTTP /positions: {summary["traces_with_position_http"]}')
    print(f'Связанные MARKET Intent+ACK (по clOrdId): {summary["linked_intent_ack_count"]}')
    print('Фаза                     N     P50(ms)  P95(ms)  MAX(ms)')
    for name,s in summary['phase_stats'].items():
        if not s['n']:continue
        def val(k):return f'{s[k]:.1f}' if s[k] is not None else 'н/д'
        print(f'{name:<25} {s["n"]:<5} {val("p50_ms"):<8} {val("p95_ms"):<8} {val("max_ms")}')
    for title,key in (('Reconciliation при активном WS crossing','reconciliation_steps'),
                      ('StatsWorker','stats_steps')):
        if summary[key]:
            print(title)
            for name,d in sorted(summary[key].items()):
                print(f'  {name}: N={d["n"]} P50={d["p50_ms"]:.1f}ms P95={d["p95_ms"]:.1f}ms')
    overlap=summary['tp_stats_overlap']
    print(f'StatsWorker overlap (пары с валидным временем): {overlap["eligible_tp_intervals"]} TP, '
          f'{overlap["stats_intervals"]} интервалов StatsWorker')
    for k,label in (('with_stats','совпали'),('without_stats','не совпали')):
        d=overlap[k]
        if d['n']:
            print(f'  {label}: N={d["n"]} P50={d["p50_ms"]:.1f}ms P95={d["p95_ms"]:.1f}ms')
    print('Пересечение интервалов НЕ доказывает влияние StatsWorker; измерения локальны для одного VPS.')
    print('Замеры P50 разных фаз нельзя складывать; нет clock-sync с OKX, не выводим причинность.')
    print('Пропущенные, повреждённые и неидентифицированные события НЕ считаются нулевой задержкой.')


if __name__=='__main__':main()
