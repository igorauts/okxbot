"""Worker-only 24-hour latency rollups; no trading or network operations.

Bucketed distributions preserve useful percentiles across rotations/restarts.
Percentiles derived from buckets are approximations, never represented as exact.
"""
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
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


def segment_dir(path):
    """Keep the old JSONL as a read-only migration source for at most 24 h."""
    path=Path(path)
    return path.with_name(path.stem + '.segments')


def read_rollups(path, now=None):
    """Read bounded 24h minute rows, including pre-upgrade legacy snapshots.

    Corrupt/torn trailing JSONL records are ignored, never treated as zeros.
    Data is never recovered by reading an exchange or bot trading state.
    """
    now=time.time() if now is None else now
    path=Path(path)
    candidates=[]
    if path.is_file() and not path.is_symlink():
        candidates.append(path)
    directory=segment_dir(path)
    if directory.is_dir() and not directory.is_symlink():
        earliest_hour=int((now-SECONDS-3600)//3600)
        latest_hour=int((now+3600)//3600)
        for item in sorted(directory.glob('*.jsonl')):
            if item.is_symlink() or not item.is_file():continue
            try:hour=int(item.stem)
            except ValueError:continue
            if earliest_hour<=hour<=latest_hour:
                candidates.append(item)
    rows=[]
    for candidate in candidates:
        try:
            with candidate.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        record=json.loads(line)
                        if isinstance(record,dict):rows.append(record)
                    except (ValueError,UnicodeDecodeError):continue
        except OSError:continue
    return fresh_rows(rows,now)


class MinuteWriter:
    """Append one completed minute to an hourly segment, never rewrite 24h.

    No trading-thread I/O. At most 25 hourly files are retained on disk,
    while consumer time filtering enforces the exact 24-hour query window.
    """
    def __init__(self,path):
        self.path=Path(path)
        self.directory=segment_dir(self.path)
        self.minute=None
        self.buckets={}
        self.rows=deque(maxlen=1600)  # bounded convenience for diagnostics
        self._verified=set()
        self._cleanup_hour=None

    def observe(self,record):
        minute=int(time.time()//60)
        if self.minute is None:self.minute=minute
        if minute!=self.minute:
            self.flush()
            self.minute=minute
        add_row(record,self.buckets)

    def _repair_tail_once(self,path):
        if path in self._verified:return
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            size=os.lseek(fd,0,os.SEEK_END)
            if size:
                os.lseek(fd,size-1,os.SEEK_SET)
                if os.read(fd,1)!=b'\n':
                    # An interrupted single-syscall append can leave a torn
                    # final line. Strip ONLY that incomplete final record.
                    pos=size; newline=-1
                    while pos>0:
                        begin=max(0,pos-65536)
                        os.lseek(fd,begin,os.SEEK_SET)
                        block=os.read(fd,pos-begin)
                        off=block.rfind(b'\n')
                        if off>=0:
                            newline=begin+off
                            break
                        pos=begin
                    os.ftruncate(fd,newline+1)
        finally:
            os.close(fd)
        self._verified.add(path)

    def _cleanup(self,now):
        hour=int(now//3600)
        if self._cleanup_hour==hour:return
        self._cleanup_hour=hour
        # Remove whole segments only after EVERY minute in them is older
        # than the 24h window. Up to 25 files including the partial hour.
        if self.directory.is_dir():
            for candidate in self.directory.glob('*.jsonl'):
                try:bucket=int(candidate.stem)
                except ValueError:continue
                if bucket*3600+3600 < now-SECONDS-60:
                    if candidate.is_file() and not candidate.is_symlink():
                        candidate.unlink()
        # Legacy file is read-only until its entire timeline expires.
        if self.path.is_file() and not self.path.is_symlink():
            if self.path.stat().st_mtime < now-SECONDS-180:
                self.path.unlink()

    def flush(self):
        if not self.buckets or self.minute is None:return
        row={'schema':1,'minute':self.minute,'series':self.buckets}
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=self.directory/(str(self.minute//60)+'.jsonl')
        self._repair_tail_once(path)
        data=(json.dumps(row,separators=(',',':'))+'\n').encode('utf-8')
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:
            # One minute is one line. A partial OS write raises an error;
            # next worker startup repairs the trailing incomplete record.
            written=os.write(fd,data)
            if written!=len(data):
                raise OSError('Incomplete minute append')
        finally:
            os.close(fd)
        self.rows.append(row)
        self.buckets={}
        self._cleanup(time.time())
