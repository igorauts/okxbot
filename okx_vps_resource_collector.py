#!/usr/bin/env python3
"""Read-only bounded per-minute VPS process/cgroup history for OKX grid.

Never reads API credentials, bot state, trading orders or IP sockets.
Only output: root-private daily JSONL in /srv/okx-vps-resources.
"""
import os,time,json,subprocess,shutil,datetime,pathlib,math
ROOT=pathlib.Path('/srv/okx-vps-resources')
BOTS=('near0721','dood0735','wld0728','init0738','btc0721','blur0738','zec0742')
UNITS=tuple('okx-grid-'+b+'.service' for b in BOTS)+('okx-position-reader.service','okx-telegram-monitor.service','okx-position-reader-watchdog.service')

def read(path):
    try: return pathlib.Path(path).read_text()[:32768]
    except (OSError,UnicodeError): return ''

def kv(text):
    result={}
    for line in text.splitlines():
        parts=line.replace(':',' ').split()
        if len(parts)<2:continue
        try:result[parts[0]]=int(parts[1])
        except ValueError:pass
    return result

def cpu():
    text=read('/proc/stat').splitlines()
    if not text:return None
    x=text[0].split()
    if x[0]!='cpu':return None
    return [int(v) for v in x[1:]]

def pressure():
    out={}
    for kind in ('cpu','memory','io'):
        rec={}
        for line in read('/proc/pressure/'+kind).splitlines():
            parts=line.split()
            if not parts:continue
            mode=parts[0]
            fields={}
            for part in parts[1:]:
                if '=' not in part:continue
                k,v=part.split('=',1)
                try:fields[k]=float(v)
                except ValueError:pass
            rec[mode]=fields
        out[kind]=rec
    return out

def proc_mem():
    m=kv(read('/proc/meminfo'))
    return {k:round(m.get(k,0)/1024,2) for k in ('MemTotal','MemAvailable','MemFree','Cached','Buffers','SwapTotal','SwapFree','Dirty','Writeback')}

def active_units():
    cmd=['systemctl','show','--no-pager','--property=Id,ActiveState,MainPID,NRestarts,ControlGroup',*UNITS]
    try:r=subprocess.run(cmd,capture_output=True,text=True,timeout=8,check=False)
    except (OSError,subprocess.TimeoutExpired):return {}
    if r.returncode:return {}
    result={}
    for block in r.stdout.split('\n\n'):
        props={}
        for line in block.splitlines():
            if '=' in line:
                k,v=line.split('=',1);props[k]=v
        name=props.get('Id')
        if name in UNITS:result[name]=props
    # A minority of systemd versions omit multi-unit details; try exact
    # individual reads only when the aggregated reply was incomplete.
    if len(result)<len(UNITS):
        for unit in UNITS:
            if unit in result:continue
            try:
                single=subprocess.run(['systemctl','show','--no-pager',unit,
                    '--property=Id,ActiveState,MainPID,NRestarts,ControlGroup'],
                    capture_output=True,text=True,timeout=2,check=False)
            except (OSError,subprocess.TimeoutExpired):continue
            if single.returncode:continue
            props={}
            for line in single.stdout.splitlines():
                if '=' in line:
                    k,v=line.split('=',1);props[k]=v
            if props.get('Id')==unit:result[unit]=props
    return result

def cg_stats(props):
    cg=props.get('ControlGroup','')
    if not cg.startswith('/') or '..' in cg.split('/') or len(cg)>200:
        return {}
    base=pathlib.Path('/sys/fs/cgroup')/cg.lstrip('/')
    if not base.is_dir():return {}
    cpu_s=kv(read(base/'cpu.stat'));io_s=read(base/'io.stat')
    io={'rbytes':0,'wbytes':0,'rios':0,'wios':0}
    for line in io_s.splitlines():
        for field in line.split():
            if '=' in field:
                k,v=field.split('=',1)
                if k in io:
                    try:io[k]+=int(v)
                    except ValueError:pass
    def integer(name):
        txt=read(base/name).strip()
        return int(txt) if txt.isdigit() else None
    return {'cpu_usage_usec':cpu_s.get('usage_usec'),
            'cpu_nr_throttled':cpu_s.get('nr_throttled'),
            'cpu_throttled_usec':cpu_s.get('throttled_usec'),
            'memory_current':integer('memory.current'),
            'memory_peak':integer('memory.peak'),
            'swap_current':integer('memory.swap.current'),
            'memory_events':kv(read(base/'memory.events')),
            'io_bytes':io}

def tick():
    c=cpu();v=kv(read('/proc/vmstat'))
    info=active_units()
    cgs={name:cg_stats(props) for name,props in info.items()}
    return time.monotonic(),c,v,cgs,info

def sample():
    t0,c0,v0,g0,_=tick()
    time.sleep(.25)
    t1,c1,v1,g1,units=tick()
    elapsed=max(.01,t1-t0)
    cp={}
    if c0 and c1 and len(c0)==len(c1):
        diff=[max(0,y-x) for x,y in zip(c0,c1)]
        total=sum(diff)
        if total:
            cp={'user_pct':round(100*(diff[0]+diff[1])/total,2),
                'system_pct':round(100*diff[2]/total,2),
                'idle_pct':round(100*diff[3]/total,2),
                'iowait_pct':round(100*diff[4]/total,2),
                'steal_pct':round(100*diff[7]/total,2) if len(diff)>7 else None}
    blocks={}
    for name,props in units.items():
        curr=g1.get(name,{})
        prev=g0.get(name,{})
        res={'active':props.get('ActiveState'),'pid':props.get('MainPID'),'restarts':props.get('NRestarts'),
             'memory_bytes':curr.get('memory_current'),'memory_peak_bytes':curr.get('memory_peak'),
             'swap_bytes':curr.get('swap_current'),'memory_events':curr.get('memory_events'),
             'cpu_usage_usec_total':curr.get('cpu_usage_usec'),
             'cpu_nr_throttled_total':curr.get('cpu_nr_throttled'),
             'cpu_throttled_usec_total':curr.get('cpu_throttled_usec')}
        for k in ('cpu_usage_usec','cpu_nr_throttled','cpu_throttled_usec'):
            x=prev.get(k);y=curr.get(k)
            if isinstance(x,int) and isinstance(y,int):
                res[k+'_per_sec']=round(max(0,y-x)/elapsed,3)
        for k in ('rbytes','wbytes','rios','wios'):
            x=(prev.get('io_bytes') or {}).get(k);y=(curr.get('io_bytes') or {}).get(k)
            if isinstance(x,int) and isinstance(y,int):
                res['io_'+k+'_per_sec']=round(max(0,y-x)/elapsed,3)
        blocks[name]=res
    usage=shutil.disk_usage('/')
    swap={}
    for k in ('pswpin','pswpout'):
        swap[k+'_pages_per_sec']=round(max(0,v1.get(k,0)-v0.get(k,0))/elapsed,3)
    return {'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'duration_sec':round(elapsed,3),'host_cpu':cp,
            'loadavg':tuple(round(x,3) for x in os.getloadavg()),
            'mem_mib':proc_mem(),'pressure':pressure(),'swap_io':swap,
            'disk_free_gib':round(usage.free/(1024**3),3),'units':blocks}

def main():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(ROOT,0o700)
    row=sample()
    missing=[u for u in UNITS[:len(BOTS)] if u not in row['units']]
    if missing:
        raise RuntimeError('No authoritative seven-bot systemd resource visibility; refuse sample: '+','.join(missing))
    day=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')
    name=ROOT/('vps-'+day+'.jsonl')
    if name.is_symlink():raise RuntimeError('unsafe diagnostics symlink')
    fd=os.open(str(name),os.O_APPEND|os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        if os.fstat(fd).st_size > 24_000_000:
            raise RuntimeError('daily VPS diagnostics file exceeded 24MB cap')
        os.write(fd,(json.dumps(row,separators=(',',':'))+'\n').encode())
    finally:os.close(fd)
    # Keep two weeks of history; never touch other files or follow links.
    today=datetime.datetime.now(datetime.timezone.utc).date()
    for old in ROOT.glob('vps-????????.jsonl'):
        try:
            date=datetime.datetime.strptime(old.name[4:12],'%Y%m%d').date()
            if (today-date).days>14 and old.is_file() and not old.is_symlink():
                old.unlink()
        except (ValueError,OSError):pass
    print('RESOURCE_SAMPLE_PASS '+row['utc']+' units='+str(len(row['units'])))

if __name__=='__main__':main()
