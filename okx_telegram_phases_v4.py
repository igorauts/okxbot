"""Monitor-only low-overhead wall & process CPU phase timings (no payloads)."""
import collections
import json
from pathlib import Path
import time

_BUCKETS={}
_COUNT=0
_INTERVAL_START=time.monotonic()
_LAG_MAX=0.0

def _bucket(label):
    return _BUCKETS.setdefault(label, [0,0.,0.,0.,0.])  # n,wall sum/max,cpu sum/max

def measure(name, fn, *args, **kwargs):
    t=time.monotonic();c=time.process_time()
    try:
        return fn(*args, **kwargs)
    finally:
        wall=max(0.,time.monotonic()-t)
        cpu=max(0.,time.process_time()-c)
        b=_bucket(name);b[0]+=1;b[1]+=wall;b[2]=max(b[2],wall);b[3]+=cpu;b[4]=max(b[4],cpu)

def loop_end(duration):
    global _COUNT,_INTERVAL_START,_LAG_MAX,_BUCKETS
    _COUNT+=1
    _LAG_MAX=max(_LAG_MAX,duration)
    now=time.monotonic()
    if now-_INTERVAL_START<600:
        return
    out={k:{'n':v[0], 'wall_avg_ms':round(1000*v[1]/max(v[0],1),2),
            'wall_max_ms':round(1000*v[2],2),
            'cpu_sum_ms':round(1000*v[3],2)} for k,v in sorted(_BUCKETS.items())}
    try:
        from okx_telegram_journal_stream_v4 import snapshot
        stream=snapshot()
        out['journal_stream']={k:stream[k] for k in ('running','restarts','overflow','parse_errors','line_count','queue','last_error')}
    except Exception:
        out['journal_stream']={'running':False}
    try:
        cpu=Path('/sys/fs/cgroup/system.slice/okx-telegram-monitor.service/cpu.stat')
        if cpu.is_file():
            rows=dict(line.split() for line in cpu.read_text().splitlines() if len(line.split())==2)
            out['cgroup_cpu_usage_usec_total']=int(rows.get('usage_usec',0))
    except (OSError,ValueError):
        pass
    print('MONITOR_V4_PHASES '+json.dumps({'cycles':_COUNT,'window_s':round(now-_INTERVAL_START,1),
          'max_cycle_ms':round(_LAG_MAX*1000,1),'phases':out},ensure_ascii=False,separators=(',',':')),flush=True)
    _COUNT=0;_BUCKETS={};_LAG_MAX=0.;_INTERVAL_START=now
