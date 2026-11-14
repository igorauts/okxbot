#!/usr/bin/env python3
"""Read-only bounded per-minute VPS process/cgroup history for OKX grid.

Never reads API credentials, bot state, trading orders or IP sockets.
Only output: root-private daily JSONL in /srv/okx-vps-resources.
"""
import os,time,json,subprocess,shutil,datetime,pathlib,math,re
ROOT=pathlib.Path('/srv/okx-vps-resources')
UNIT_FILE_CACHE=ROOT/'enabled_unit_files_cache_v1.json'
UNIT_CACHE_SECONDS=3600
BOT_UNIT_RE=re.compile(r'^okx-grid-[A-Za-z0-9][A-Za-z0-9_-]*\.service$')
BASE_UNITS=('okx-position-reader.service','okx-telegram-monitor.service','okx-position-reader-watchdog.service')
ENABLED_STATES=('enabled','enabled-runtime','linked','linked-runtime')
OBSERVED_STATES=('active','activating','reloading','deactivating','failed')


def _enabled_unit_files():
    """Durable last-good discovery, refreshed hourly. Fail closed if stale/bad."""
    now=time.time()
    if UNIT_FILE_CACHE.is_symlink():
        raise RuntimeError('Unsafe systemd cache symlink')
    if UNIT_FILE_CACHE.exists():
        st=UNIT_FILE_CACHE.stat()
        if not UNIT_FILE_CACHE.is_file() or st.st_mode & 0o077:
            raise RuntimeError('Unsafe systemd cache permissions')
        try:
            obj=json.loads(UNIT_FILE_CACHE.read_text())
            if (isinstance(obj,dict) and obj.get('schema')==1
                    and isinstance(obj.get('at'),(int,float)) and
                    0 <= now-obj['at']<UNIT_CACHE_SECONDS and
                    isinstance(obj.get('data'),str)):
                return obj['data']
        except (ValueError,TypeError):
            pass
    res=subprocess.run(['systemctl','list-unit-files','--type=service','--plain',
           '--no-legend','--no-pager','okx-grid-*.service'],
           capture_output=True,text=True,timeout=7,check=False)
    if res.returncode:
        raise RuntimeError('systemd unit-files discovery failed')
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    if ROOT.is_symlink():
        raise RuntimeError('Unsafe systemd cache root')
    import tempfile
    fd,tmp=tempfile.mkstemp(dir=ROOT,prefix='.unit-files-')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump({'schema':1,'at':now,'data':res.stdout},f)
            f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,UNIT_FILE_CACHE)
        dir_fd=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(dir_fd)
        finally:os.close(dir_fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return res.stdout


def discover_units():
    """Fresh systemd discovery every sample; never trust cached/deleted unit IDs.

    Track real active/failed loaded units, plus enabled installed units. A
    registered enabled unit that is unexpectedly unavailable fails closed.
    """
    def query(argv):
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('systemd resource discovery unavailable') from exc
        if result.returncode:
            raise RuntimeError('systemd resource discovery failed')
        return result.stdout

    active_text = query(['systemctl', 'list-units', '--all', '--type=service',
                         '--plain', '--no-legend', '--no-pager',
                         'okx-grid-*.service'])
    observed = set()
    for line in active_text.splitlines():
        cols = line.strip().lstrip('●').strip().split()
        # Reject the misleading "not-found failed" entry that systemd retains
        # for a removed trading service; this is not a real installed unit.
        if (len(cols) >= 4 and BOT_UNIT_RE.fullmatch(cols[0])
                and cols[1] == 'loaded' and cols[2] in OBSERVED_STATES):
            observed.add(cols[0])

    enabled_text = query(['systemctl', 'list-unit-files', '--type=service',
                          '--plain', '--no-legend', '--no-pager',
                          'okx-grid-*.service'])
    enabled = set()
    for line in enabled_text.splitlines():
        cols = line.split()
        if (len(cols) >= 2 and BOT_UNIT_RE.fullmatch(cols[0])
                and cols[1] in ENABLED_STATES):
            enabled.add(cols[0])

    for unit in sorted(enabled.difference(observed)):
        result = query(['systemctl', 'show', unit, '--no-pager',
                        '-p', 'LoadState', '-p', 'ActiveState'])
        props = dict(line.split('=', 1) for line in result.splitlines()
                     if '=' in line)
        if props.get('LoadState') != 'loaded' or not props.get('ActiveState'):
            raise RuntimeError('Enabled grid unit unexpectedly unloaded: ' + unit)

    bots = observed | enabled
    if not bots:
        raise RuntimeError('No installed/observed OKX grid bot units discovered')
    return tuple(sorted(bots)) + BASE_UNITS



def active_units(units):
    """All expected units must be resolved; never silently return partial data."""
    fields='--property=Id,LoadState,ActiveState,MainPID,NRestarts,ControlGroup'
    cmd=['systemctl','show','--no-pager',fields,*units]
    try:
        res=subprocess.run(cmd,capture_output=True,text=True,timeout=8,check=False)
    except (OSError,subprocess.TimeoutExpired) as e:
        raise RuntimeError('systemd batch show unavailable: '+type(e).__name__) from e
    if res.returncode:
        raise RuntimeError('systemd batch show failed')
    def parse(raw, expected):
        parsed={}
        for block in raw.strip().split('\n\n'):
            props={}
            for line in block.splitlines():
                if '=' in line:
                    key,val=line.split('=',1)
                    props[key]=val
            ident=props.get('Id')
            if ident not in expected:
                continue
            if ident in parsed:
                raise RuntimeError('duplicate systemd unit response: '+ident)
            if props.get('LoadState') != 'loaded' or not props.get('ActiveState'):
                raise RuntimeError('invalid systemd unit response: '+ident)
            parsed[ident]=props
        return parsed
    expected=set(units)
    found=parse(res.stdout,expected)
    if len(found)!=len(expected):
        for unit in units:
            if unit in found:
                continue
            try:
                one=subprocess.run(['systemctl','show','--no-pager',unit,fields],
                    capture_output=True,text=True,timeout=2,check=False)
            except (OSError,subprocess.TimeoutExpired):
                continue
            if one.returncode:
                continue
            found.update(parse(one.stdout,{unit}))
    missing=expected.difference(found)
    if missing:
        raise RuntimeError('Incomplete systemd resource visibility: '+','.join(sorted(missing)))
    return found


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

def tick(units):
    c=cpu();v=kv(read('/proc/vmstat'))
    info=active_units(units)
    cgs={name:cg_stats(props) for name,props in info.items()}
    return time.monotonic(),c,v,cgs,info

def sample():
    units=discover_units()
    t0,c0,v0,g0,_=tick(units)
    time.sleep(.25)
    t1,c1,v1,g1,info1=tick(units)
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
    for name,props in info1.items():
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
    if not any(BOT_UNIT_RE.fullmatch(u) for u in row['units']):
        raise RuntimeError('No dynamically discovered grid bot in resource sample; refuse sample')
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
