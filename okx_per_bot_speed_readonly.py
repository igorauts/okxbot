#!/usr/bin/env python3
"""Read-only LIVE per-bot trading speed report for OKX Grid Bot v0.7.47.

Uses bounded minute histogram files for complete-minute percentile coverage.
Reads at most 8 MiB of each latency.jsonl for BUY batch attempted/placed details.
No OKX API, service, state, orders, database, or configuration writes.
"""
import argparse
import collections
import datetime as dt
import json
import math
from pathlib import Path
import time

BOTS = ('near0721','wld0728','dood0735','init0738','blur0738','zec0742','btc0721')
ROOT = Path('/root/okx')
BOUNDS = (.001,.005,.01,.02,.05,.1,.25,.5,1,2,5,10,20,50,100,250,
          500,1000,2000,5000,10000,30000,60000,120000,float('inf'))
PHASES = (
    ('loop_pass||', 'ТОРГОВЫЙ ЦИКЛ (loop_pass)'),
    ('position_reconcile||', 'СВЕРКА ПОЗИЦИИ (position_reconcile)'),
    ('buy_post||', 'ВЫСТАВЛЕНИЕ BUY (buy_post: вызов до ответа)'),
    ('buy_batch||', 'ПАЧКА BUY (buy_batch: включая pacing)'),
    ('http|POST|/api/v5/trade/order', 'BUY/SELL POST HTTP (только сетевой RTT)'),
    ('http|POST|/api/v5/trade/cancel-order', 'ОТМЕНА BUY/TP POST HTTP (только RTT)'),
    ('position_reader_ipc||', 'SHARED READER IPC (клиентское время)'),
    ('tp_to_intent||', 'VIRTUAL TP: WS CROSS -> INTENT'),
    ('tp_intent_to_submit||', 'VIRTUAL TP: INTENT -> SUBMIT'),
)
RAW_LIMIT = 8*1024*1024


def new_dist():
    return {'n':0,'bins':[0]*len(BOUNDS),'max':0.0}


def add_dist(target, obj):
    if not isinstance(obj,dict):return
    n=obj.get('count'); bins=obj.get('bins')
    if type(n)!=int or n<0 or not isinstance(bins,list) or len(bins)!=len(BOUNDS):return
    if any(type(x)!=int or x<0 for x in bins) or sum(bins)!=n:return
    try:
        mx=float(obj.get('max',0))
        if not math.isfinite(mx) or mx<0:return
    except (ValueError,TypeError):return
    target['n']+=n
    for i,x in enumerate(bins):target['bins'][i]+=x
    target['max']=max(target['max'],mx)


def percentile_ceiling(d,p):
    n=d['n']
    if not n:return '-'
    rank=math.ceil(n*p)
    passed=0
    for bound, count in zip(BOUNDS,d['bins']):
        passed+=count
        if passed>=rank:
            return str(round(bound,3)) if math.isfinite(bound) else str(round(d['max'],1))
    return '?'


def safe_file(p,limit):
    try:return p.is_file() and not p.is_symlink() and p.stat().st_size<=limit
    except OSError:return False


def rollups(bot,first_minute,last_minute):
    base=ROOT/bot/'logs'
    seg=base/'latency_24h_rollups.segments'
    hours=range(first_minute//60,last_minute//60+1)
    paths=[seg/(str(h)+'.jsonl') for h in hours]
    if not any(safe_file(p,8*1024*1024) for p in paths):
        paths=[base/'latency_24h_rollups.jsonl']
    result={key:new_dist() for key,_ in PHASES}
    seen_minutes=set()
    malformed=0
    for path in paths:
        if not safe_file(path,8*1024*1024):continue
        try:
            with path.open(encoding='utf8') as f:
                for line in f:
                    try:row=json.loads(line)
                    except (ValueError,UnicodeDecodeError):malformed+=1;continue
                    minute=row.get('minute')
                    if type(minute)!=int or not first_minute<=minute<last_minute:continue
                    if minute in seen_minutes:
                        malformed+=1;continue
                    series=row.get('series')
                    if not isinstance(series,dict):malformed+=1;continue
                    seen_minutes.add(minute)
                    for key in result:add_dist(result[key],series.get(key))
        except OSError:continue
    return result,seen_minutes,malformed


def raw_buy_batches(bot,first_minute,last_minute):
    """Bounded supplementary counts; never treat incomplete raw tails as full hour."""
    path=ROOT/bot/'logs'/'latency.jsonl'
    summary=collections.Counter();last=[]
    try:
        if path.is_symlink() or not path.is_file():return summary,last,True
        with path.open('rb') as f:
            size=f.seek(0,2);pos=max(0,size-RAW_LIMIT)
            f.seek(pos)
            if pos:f.readline()  # skip torn first line
            for line in f:
                if b'"buy_batch"' not in line:continue
                try:
                    o=json.loads(line)
                    if o.get('phase')!='buy_batch':continue
                    t=dt.datetime.fromisoformat(o['ts']).timestamp()
                    if not first_minute*60<=t<last_minute*60:continue
                    ms=float(o['ms'])
                    if not math.isfinite(ms) or ms<0:continue
                except (ValueError,TypeError,KeyError,UnicodeDecodeError):continue
                summary['batches']+=1
                for key in ('attempted','placed'):
                    if type(o.get(key))==int and o[key]>=0:
                        summary[key]+=o[key]
                    else:summary['missing_fields']+=1
                if len(last)==3:last.pop(0)
                last.append((o.get('ts','')[-18:],round(ms,1),o.get('attempted'),o.get('placed')))
        return summary,last,pos>0
    except OSError:return summary,last,True


def emit_table(title,key,data):
    print('\n=== '+title+' ===')
    print(f'{"BOT":<11} {"N":>6} {"P50<=" :>9} {"P95<=" :>9} {"P99<=" :>9} {"MAX":>9} ms')
    for bot in BOTS:
        d=data[bot][0][key]
        print(f'{bot:<11} {d["n"]:>6} {percentile_ceiling(d,.50):>9} '
              f'{percentile_ceiling(d,.95):>9} {percentile_ceiling(d,.99):>9} '
              f'{d["max"]:>9.1f}' if d['n'] else f'{bot:<11} {"0":>6} {"-":>9} {"-":>9} {"-":>9} {"-":>9}')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--minutes',type=int,default=60)
    args=ap.parse_args()
    if not 15<=args.minutes<=180:ap.error('--minutes must be 15..180')
    last=int(time.time()//60);first=last-args.minutes
    utc=lambda minute:dt.datetime.fromtimestamp(minute*60,dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print('OKX LIVE PER-BOT SPEED | READ-ONLY | NO API | NO WRITES')
    print('WINDOW (complete minutes):',utc(first),'->',utc(last),f'({args.minutes} min)')
    data={bot:rollups(bot,first,last) for bot in BOTS}
    print('\nCOVERAGE (complete recorded minutes):')
    for bot in BOTS:
        _,minutes,invalid=data[bot]
        print(f'  {bot:<11} {len(minutes):>3}/{args.minutes} minutes'+
              (f' | duplicate/corrupt rows {invalid}' if invalid else '')+
              (' | INCOMPLETE' if len(minutes)<args.minutes-2 else ''))
    for key,title in PHASES:emit_table(title,key,data)
    print('\n=== BUY REPLACEMENT / REPLENISHMENT EVENTS (bounded raw detail) ===')
    print(f'{"BOT":<11} {"BATCHES":>7} {"ATTEMPT":>8} {"PLACED":>7} {"SCOPE":>10}')
    for bot in BOTS:
        counts,_,truncated=raw_buy_batches(bot,first,last)
        n_rollup=data[bot][0]['buy_batch||']['n']
        scope='FULL' if counts['batches']==n_rollup else 'PARTIAL'
        print(f'{bot:<11} {counts["batches"]:>7} {counts["attempted"]:>8} '
              f'{counts["placed"]:>7} {scope:>10}')
        if counts['missing_fields']:
            print('  WARNING:',counts['missing_fields'],'BUY batch records missing fields')
        if scope!='FULL':
            print(f'  NOTE: raw {counts["batches"]} of rollup {n_rollup} batches; counts are LOWER BOUNDS (tail limited to 8 MiB).')
    print('\nNOTES: P50/P95/P99 are UPWARD histogram-bin ceilings, MAX exact from minute rollups.')
    print('BUY batch includes intentional 50ms pacing between placed BUYs; counted even when placed=0.')
    print('BUY POST measures call->API result (not market move->reposted BUY).')
    print('POST /trade/order includes BUY and SELL; cancel-order includes BUY and TP cancellations.')
    print('loop_pass records only completed normal trading-loop passes, not early continue paths.')
    print('Very small N makes P99 unreliable. Missing minute rows/phase samples do not mean zero activity.')
    print('END READ ONLY.')

if __name__=='__main__':main()
