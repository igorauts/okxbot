"""Worker-only 24-hour latency rollups; no trading or network operations.

Bucketed distributions preserve useful percentiles across rotations/restarts.
Percentiles derived from buckets are approximations, never represented as exact.
"""
import json
import math
import os
import time
from collections import deque
from pathlib import Path

# Upper bounds in milliseconds. Overflow is counted in final bucket.
BOUNDS=(.001,.005,.01,.02,.05,.1,.25,.5,1,2,5,10,20,50,100,250,
        500,1000,2000,5000,10000,30000,60000,120000,float('inf'))
SECONDS=24*3600


def key_for(record):
    phase=record.get('phase','unknown')
    route=record.get('route','') if phase in ('http','rest_gate','positions_gate') else ''
    method=record.get('method','') if route else ''
    return '|'.join((phase,str(method),str(route)))


def fresh_rows(rows,now=None):
    now=time.time() if now is None else now
    return [r for r in rows if (isinstance(r,dict)
            and isinstance(r.get('minute'),int)
            and now-SECONDS-60 <= r['minute']*60 <= now+60)]


def add_row(record,buckets):
    try:
        ms=float(record['ms'])
        if not math.isfinite(ms) or ms<0:return
    except (TypeError,ValueError,KeyError):return
    key=key_for(record)
    target=buckets.setdefault(key,{'count':0,'max':0.,'bins':[0]*len(BOUNDS)})
    target['count']+=1
    target['max']=max(target['max'],ms)
    for i,upper in enumerate(BOUNDS):
        if ms<=upper:
            target['bins'][i]+=1
            break


def distribution(rows,phase,now=None,route=None,window_seconds=SECONDS):
    """Merged bounded 24h histogram; route exact match when supplied."""
    now=time.time() if now is None else now
    bins=[0]*len(BOUNDS);count=0;maximum=0.; earliest=None; latest=None
    for row in fresh_rows(rows,now):
        if row['minute']*60 < now-window_seconds-60:
            continue
        for key,d in row.get('series',{}).items():
            parts=key.split('|',2)
            if len(parts)!=3 or parts[0]!=phase:continue
            if route is not None and parts[2]!=route:continue
            if not isinstance(d,dict) or len(d.get('bins',[]))!=len(BOUNDS):continue
            try:
                n=int(d['count']);mx=float(d['max'])
                values=list(map(int,d['bins']))
                if n<0 or sum(values)!=n:continue
            except (TypeError,ValueError,KeyError):continue
            count+=n;maximum=max(maximum,mx)
            bins=[a+b for a,b in zip(bins,values)]
            minute=row['minute'];earliest=minute if earliest is None else min(earliest,minute)
            latest=minute if latest is None else max(latest,minute)
    def q(p):
        if not count:return None
        rank=math.ceil(count*p/100);acc=0
        for bound,num in zip(BOUNDS,bins):
            acc+=num
            if acc>=rank:return bound if math.isfinite(bound) else maximum
        return maximum
    return {'n':count,'p50_upper_ms':q(50),'p95_upper_ms':q(95),
            'p99_upper_ms':q(99),'max_ms':maximum,'start_minute':earliest,
            'end_minute':latest,'approximate':True}


class MinuteWriter:
    def __init__(self,path):
        self.path=Path(path)
        self.minute=None;self.buckets={};self.rows=deque(maxlen=1600)
        try:
            with self.path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:self.rows.append(json.loads(line))
                    except ValueError:continue
            self.rows=deque(fresh_rows(self.rows),maxlen=1600)
        except OSError:pass

    def observe(self,record):
        minute=int(time.time()//60)
        if self.minute is None:self.minute=minute
        if minute!=self.minute:
            self.flush()
            self.minute=minute
        add_row(record,self.buckets)

    def flush(self):
        if not self.buckets or self.minute is None:return
        self.rows.append({'schema':1,'minute':self.minute,'series':self.buckets})
        self.buckets={}
        rows=fresh_rows(self.rows)
        self.rows=deque(rows,maxlen=1600)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        dest=self.path.with_name('.latency_rollup.tmp')
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                for row in rows:out.write(json.dumps(row,separators=(',',':'))+'\n')
                out.flush();os.fsync(out.fileno())
            os.replace(dest,self.path)
        finally:
            if dest.exists():dest.unlink()
