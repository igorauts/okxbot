#!/usr/bin/env python3
"""Read-only P50/P95/P99 report for v0.7.11 local latency journal. No API."""
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def percentile(values, q):
    if not values:
        return None
    v=sorted(values)
    pos=(len(v)-1)*q
    lo=math.floor(pos);hi=math.ceil(pos)
    return v[lo]+(v[hi]-v[lo])*(pos-lo)


def load_records(root, since):
    path=Path(root)/'logs'/'latency_v0711.jsonl'
    for candidate in (path.with_name(path.name+'.1'),path):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        with candidate.open(encoding='utf-8',errors='replace') as f:
            for line in f:
                try:
                    item=json.loads(line)
                    stamp=datetime.fromisoformat(item['ts'].replace('Z','+00:00'))
                    if stamp.tzinfo is None or stamp < since:
                        continue
                    ms=float(item['ms'])
                    if not math.isfinite(ms) or ms < 0:
                        continue
                    yield item
                except (ValueError,TypeError,KeyError,OverflowError):
                    continue


def report(root, hours=4, now=None):
    now=datetime.now(timezone.utc) if now is None else now
    since=now-timedelta(hours=hours)
    values=defaultdict(list);drops=0;records=0
    for row in load_records(root,since):
        phase=row['phase']
        if phase in ('http','rest_gate','positions_gate'):
            key=phase+' '+row.get('method','')+' '+row.get('route','')
        else:
            key=phase
        values[key].append(float(row['ms']))
        drops=max(drops,int(row.get('dropped',0)))
        records+=1
    print(f'OKX v0.7.11 LATENCY | UTC {since.isoformat()} — {now.isoformat()}')
    print(f'Records: {records} | reported dropped since process start: {drops}')
    print(f'{"PHASE / ROUTE":64s} {"N":>7s} {"P50":>9s} {"P95":>9s} {"P99":>9s} {"MAX":>9s}')
    for key,nums in sorted(values.items()):
        if not nums:continue
        print(f'{key[:64]:64s} {len(nums):7d} '+
              ' '.join(f'{percentile(nums,q):9.1f}' for q in (.50,.95,.99,1.0)))
    if not records:
        print('NO DATA: engine may not be LIVE, telemetry disabled, or window empty')
    print('All latencies in ms. HTTP timing excludes subsequent WS exchange confirmation.')
    return records


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True,help='local bot directory')
    cli.add_argument('--hours',type=float,default=4)
    args=cli.parse_args()
    if not (0 < args.hours <= 24*30):
        cli.error('--hours must be between 0 and 720')
    report(args.root,args.hours)
