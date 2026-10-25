#!/usr/bin/env python3
"""OKX Grid Bot 24-hour OFFLINE diagnostic collector.
Reads log files, observer sidecars and systemd journal only.
Never reads .env, API keys, config contents (except a few benign labels), orders via OKX,
or private fills SQLite. Never touches LIVE files or processes.
"""
import argparse
import collections
from array import array
from datetime import datetime, timezone, timedelta
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import zipfile

BASE=Path('/root/okx')
OUT=Path('/srv')
MAX_JSON_LINES=6000000
MAX_EVENT_EXPORT=5000
MAX_SLOW_EXPORT=2500
MAX_OBSERVER_LINES=35000
MAX_SCAN_SIZE=800*1024*1024  # don't block indefinitely on enormous historic logs
DANGER=re.compile(r'(?i)(?:authorization|api[_-]?key|api[_-]?secret|secret[_-]?key|passphrase|password|private[_-]?key|telegram[_-]?bot[_-]?token|bearer|chat[_-]?id)')
TOKEN=re.compile(r'(?i)(?:https?://api\.telegram\.org/bot)[^/\s]+')
EVENT_FIELDS=frozenset(('ts','event','reason','duration_sec','elapsed_sec','elapsed_ms','age_ms','status','mode','phase','error','exception','attempted','placed','count','partial','resolved','price','tp','buy','bid','fillPx','fillSz','qty','size','outcome','side','result','reason_code','active_buys','target_buys'))
ALERT_MARKERS=('ERROR','EXCEPTION','REJECT','UNRESOLVED','FAILED','FAILURE','TIMEOUT','STALE','PANIC','HARD','SYNC_WAIT','COVERAGE','GATE_DEGRADED','GATE_UNAVAILABLE','BUY_DEFICIT','BUY_BELOW','QUEUE_FULL','MANUAL_REVIEW','TP_MISSING','TP_RESTORE','RECONCILIATION_LONG','RECONCILIATION_RECOVERED','RATE_LIMIT','429','Traceback')
ENGINE_SKIP=('BUY_CANCEL_PROTECTED',)


def utc(ts): return datetime.fromtimestamp(ts,timezone.utc).isoformat(timespec='seconds')
def dump(o): return json.dumps(o,ensure_ascii=False,indent=2,sort_keys=True,default=str)
def iso_ts(v):
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return d.timestamp() if d.tzinfo else None
    except (ValueError,TypeError,OverflowError):return None

def clean_str(value,maxlen=300):
    text=str(value)[:maxlen]
    if DANGER.search(text) or TOKEN.search(text):return '[REDACTED: possible credential]'
    return text

def compact_json(obj):
    if isinstance(obj,dict):
        return {str(k): (compact_json(v) if not isinstance(v,str) else clean_str(v,150))
                for k,v in obj.items() if not DANGER.search(str(k)) and len(str(k))<=50}
    if isinstance(obj,list):return [compact_json(x) for x in obj[:150]]
    if isinstance(obj,(int,float,bool)) or obj is None:return obj
    return clean_str(obj,150)


def safe_file(path, root):
    # Only regular files inside explicitly allowed root; never follow symlinks.
    try:
        if path.is_symlink() or not path.is_file():return False
        path.relative_to(root)
        return True
    except (OSError,ValueError):return False


def ndjson_scan(path, root, start, end, stats):
    if not safe_file(path,root):return
    size=path.stat().st_size
    if size>MAX_SCAN_SIZE:
        stats['warnings'].append(f'{path.name}: size {size} exceeds scan limit; oldest data may be unavailable')
    try:
        with path.open('rb') as f:
            if size>MAX_SCAN_SIZE:
                f.seek(size-MAX_SCAN_SIZE)
                f.readline()
            count=0
            for line in f:
                if not line.strip():continue
                count+=1
                if count>MAX_JSON_LINES:
                    stats['warnings'].append(f'{path.name}: MAX_JSON_LINES reached')
                    break
                try:
                    obj=json.loads(line)
                except (ValueError,UnicodeError):
                    stats['invalid_json']+=1
                    continue
                if not isinstance(obj,dict):continue
                ts=iso_ts(obj.get('ts'))
                if ts is not None and start<=ts<=end:
                    yield obj,ts
    except (OSError,PermissionError) as e:
        stats['warnings'].append(f'{path.name}: {type(e).__name__}')


def file_collection(base, filename):
    # Journal rotates 256 MiB .1; chronological order.
    return [base/(filename+'.1'),base/filename]


def pct(xs,percent):
    if not xs:return None
    s=sorted(xs)
    return round(float(s[max(0,math.ceil(len(s)*percent/100)-1)]),3)


def aggreg(xs):
    if not xs:return {'n':0,'p50_ms':None,'p95_ms':None,'p99_ms':None,'max_ms':None}
    return {'n':len(xs),'p50_ms':pct(xs,50),'p95_ms':pct(xs,95),
            'p99_ms':pct(xs,99),'max_ms':pct(xs,100)}


class Collector:
    def __init__(self,hours,outdir,bots):
        self.bots=tuple(bots)
        self.end=time.time();self.start=self.end-hours*3600;self.hours=hours
        self.outdir=outdir;self.zip=None;self.errors=[];self.lines=[]
    def add(self,name,text):
        self.zip.writestr(name,text if isinstance(text,bytes) else str(text).encode('utf-8'))
    def json(self,name,obj):self.add(name,dump(obj)+'\n')
    def noted(self,line):self.lines.append(line)
    def cmd(self,args,timeout=40):
        try:
            proc=subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=False)
            return proc.stdout, proc.returncode, clean_str(proc.stderr[:400])
        except (OSError,subprocess.TimeoutExpired) as e:return '',-1,type(e).__name__
    def services(self):
        names=['okx-telegram-monitor.service']
        for bot in self.bots:
            names.append(f'okx-grid-{bot}.service')
            for kind in ('burst','execution'):
                names.append(f'observer-okx-grid-{bot}-{kind}.service')
        rows={}
        for unit in names:
            out,rc,err=self.cmd(['systemctl','show',unit,'--no-pager','-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID','-p','NRestarts','-p','ExecMainStartTimestamp','-p','ExecMainExitTimestamp'],timeout=6)
            rows[unit]={k:v.strip() for x in out.splitlines() if '=' in x for k,v in [x.split('=',1)]}
            if rc:rows[unit]['error']=err or f'rc={rc}'
        self.json('services.json',rows)
        return rows
    def latency(self, bot):
        root=BASE/bot
        stats={'invalid_json':0,'warnings':[]}
        per=collections.defaultdict(lambda:array('f'))
        hourly=collections.defaultdict(lambda:array('f'))
        intervals=collections.defaultdict(lambda:array('f'))
        counts=collections.Counter(); reasons=collections.Counter(); routes=collections.Counter()
        min_ts=None;max_ts=None; samples=0;slow=[];first_last={}
        for fp in file_collection(root/'logs','latency.jsonl'):
            for r,ts in ndjson_scan(fp,root,self.start,self.end,stats):
                try:
                    ms=float(r['ms']);phase=str(r['phase'])[:60]
                    if not math.isfinite(ms) or ms<0:continue
                except (KeyError,ValueError,TypeError):continue
                per[phase].append(ms)
                h=datetime.fromtimestamp(ts,timezone.utc).strftime('%Y-%m-%dT%H:00Z')
                m=datetime.fromtimestamp(ts,timezone.utc)
                q=m.minute//15*15
                block=f'{m:%Y-%m-%dT%H}:{q:02d}Z'
                hourly[(h,phase)].append(ms)
                intervals[(block,phase)].append(ms)
                min_ts=ts if min_ts is None else min(min_ts,ts)
                max_ts=ts if max_ts is None else max(max_ts,ts)
                samples+=1;counts[phase]+=1
                if phase=='positions_gate_error':reasons[str(r.get('reason') or 'unknown')[:90]]+=1
                if phase=='http':routes[str(r.get('route') or 'unknown')[:95]]+=1
                # bounded top spikes per phase; never export payloads or order IDs
                key=(ms,ts,phase)
                if len(slow)<MAX_SLOW_EXPORT:heapq.heappush(slow,key)
                elif ms>slow[0][0]:heapq.heapreplace(slow,key)
        hfile=root/'logs'/'latency_health.json'
        health=None
        if safe_file(hfile,root):
            try:
                raw=json.loads(hfile.read_text())
                health={k:raw.get(k) for k in ('schema','dropped','write_errors','at')}
            except (OSError,ValueError):health={'error':'invalid sidecar'}
        sidecars={}
        for p in (root/'logs'/'latency.jsonl',root/'logs'/'latency.jsonl.1'):
            if safe_file(p,root):sidecars[p.name]={'size_bytes':p.stat().st_size,'mtime_utc':utc(p.stat().st_mtime)}
        result={'window_utc':[utc(self.start),utc(self.end)],'read_sources':sidecars,'n':samples,
                'first_ts':utc(min_ts) if min_ts else None,'last_ts':utc(max_ts) if max_ts else None,
                'observed_span_hours':round((max_ts-min_ts)/3600,3) if min_ts else 0,
                'full_24h_coverage_NOT_guaranteed':True,
                'by_phase':{p:aggreg(xs) for p,xs in sorted(per.items())},
                'by_hour':[{'hour':h,'phase':p,**aggreg(xs)} for (h,p),xs in sorted(hourly.items())],
                'by_15min':[{'bucket':h,'phase':p,**aggreg(xs)} for (h,p),xs in sorted(intervals.items())],
                'fifo_gate_error_reasons':dict(reasons),'http_route_counts':dict(routes),
                'health_sidecar_lifetime_counters':health,**stats}
        self.json(f'bots/{bot}/latency_24h.json',result)
        self.add(f'bots/{bot}/top_latency_spikes.jsonl', ''.join(json.dumps({'ts':utc(t),'phase':p,'ms':round(ms,3)})+'\n' for ms,t,p in sorted(slow,reverse=True)))
        return result
    def reconcile_profile(self,bot):
        """Optional profiling (if OKX_RECON_PROFILE=1), from existing file only."""
        root=BASE/bot
        stats={'invalid_json':0,'warnings':[]}
        phases=collections.defaultdict(lambda:array('f'))
        errors=collections.Counter(); n=0
        slow=collections.deque(maxlen=500)
        for fp in file_collection(root/'logs','reconcile_profile.jsonl'):
            for r,ts in ndjson_scan(fp,root,self.start,self.end,stats):
                n+=1
                try:
                    d=float(r.get('duration_ms',0))
                    if math.isfinite(d) and d>=0:
                        phases['total'].append(d)
                        if d>=500:
                            slow.append({'ts':utc(ts),'duration_ms':round(d,3),
                                         'passes':r.get('passes'), 'mismatches':r.get('mismatches')})
                except (ValueError,TypeError):pass
                if r.get('exception'):
                    errors[clean_str(str(r['exception']),100)]+=1
                for k,v in (r.get('timings_ms') or {}).items():
                    if not isinstance(k,str) or not isinstance(v,(int,float)) or len(k)>60:continue
                    if math.isfinite(v) and v>=0:phases[k].append(float(v))
        self.json(f'bots/{bot}/reconciliation_profile_24h.json',{
            'available':bool(n),'samples':n,'phase_statistics':{k:aggreg(x) for k,x in sorted(phases.items())},
            'exceptions':dict(errors),'slow_recent_samples':list(slow),**stats})

    def events(self,bot):
        root=BASE/bot
        stat={'invalid_json':0,'warnings':[]};cnt=collections.Counter();hour=collections.defaultdict(collections.Counter)
        matched=collections.deque(maxlen=MAX_EVENT_EXPORT)
        recent=collections.deque(maxlen=2500);times=[]
        for fp in file_collection(root/'logs','bot_events.jsonl'):
            for r,ts in ndjson_scan(fp,root,self.start,self.end,stat):
                ev=str(r.get('event','UNKNOWN'))[:100];cnt[ev]+=1
                h=datetime.fromtimestamp(ts,timezone.utc).strftime('%Y-%m-%dT%H:00Z')
                hour[h][ev]+=1
                safe={k:compact_json(v) for k,v in r.items() if k in EVENT_FIELDS}
                safe['ts']=utc(ts)
                if any(t in ev.upper() for t in ALERT_MARKERS):matched.append(safe)
                if ev in ('BUY_FILLED','BUY_FILLED_DURING_CANCEL','TP_FILLED','BUY_PLACED','BUY_CANCEL_CONFIRMED','TP_LINKED','AGG_MARKET_TERMINAL','RECONCILIATION_RECOVERED','BUY_POST_UNRESOLVED','TP_POST_UNRESOLVED'):
                    recent.append(safe)
                times.append(ts)
        self.json(f'bots/{bot}/events_summary_24h.json',{'window_utc':[utc(self.start),utc(self.end)],
                  'total_events':sum(cnt.values()),'counts':dict(cnt),'by_hour':{k:dict(v) for k,v in sorted(hour.items())},
                  'first_ts':utc(min(times)) if times else None,'last_ts':utc(max(times)) if times else None,
                  'warnings':stat['warnings'],'invalid_json':stat['invalid_json']})
        self.add(f'bots/{bot}/alert_events_24h.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in matched))
        self.add(f'bots/{bot}/recent_execution_events_24h.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in recent))
        return cnt
    def observer(self,bot,kind):
        root=BASE/bot;folder=root/'diagnostics'/kind
        if safe_file(folder/'summary.json',root):
            try:self.json(f'bots/{bot}/observer_{kind}_summary.json',compact_json(json.loads((folder/'summary.json').read_text())))
            except (OSError,ValueError) as e:self.errors.append(f'{bot} {kind} summary: {type(e).__name__}')
        for filename in ('alerts.jsonl','bursts.jsonl','timeline.jsonl','snapshots.jsonl'):
            buf=collections.deque(maxlen=MAX_OBSERVER_LINES)
            warnings={'invalid_json':0,'warnings':[]};total=0
            for fp in file_collection(folder,filename):
                for r,ts in ndjson_scan(fp,root,self.start,self.end,warnings):
                    total+=1
                    row=compact_json(r)
                    if filename=='snapshots.jsonl':
                        row={k:row.get(k) for k in ('ts','buy','tp','tp_other','virtual_tp','engine_buy','engine_tp','engine_observed_ts','bid_above_top_steps') if k in row}
                    buf.append(row)
            # Full retained file only if within cap, otherwise last cap with explicit warning.
            if total>MAX_OBSERVER_LINES:warnings['warnings'].append(f'{filename}: retained most recent {MAX_OBSERVER_LINES}/{total}')
            self.add(f'bots/{bot}/observer_{kind}_{filename}', ''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in buf))
            self.json(f'bots/{bot}/observer_{kind}_{filename}.meta.json',{'records_found_24h':total,'records_exported':len(buf),**warnings})
    def snapshot(self,bot):
        root=BASE/bot;now=time.time()
        meta={'bot':bot}
        for name,rel,keys in (
            ('config','config.json',('version','instrument','active_buy_orders','grid_step','take_profit_step','max_grids')),
            ('state','state/bot_state.json',('version','schema_version','mode','started_at')),
            ('runtime','state/runtime_stats.json',('version','mode','updated_at','heartbeat','ts','pid')),
            ('latency_health','logs/latency_health.json',('schema','dropped','write_errors','at')),
        ):
            p=root/rel
            if not safe_file(p,root):meta[name]={'available':False};continue
            try:
                obj=json.loads(p.read_text())
                if isinstance(obj,dict):
                    sel={k:obj.get(k) for k in keys if k in obj}
                    if name=='state' and isinstance(obj.get('cycles'),dict):
                        sel['cycle_state_counts']=dict(collections.Counter(str(v.get('state')) for v in obj['cycles'].values() if isinstance(v,dict)))
                    meta[name]={'available':True,'mtime_utc':utc(p.stat().st_mtime),'file_age_seconds':round(max(0.0,time.time()-p.stat().st_mtime),1),'selected':compact_json(sel)}
                else:meta[name]={'available':False,'error':'not dict'}
            except (OSError,ValueError):meta[name]={'available':False,'error':'read failed'}
        self.json(f'bots/{bot}/state_metadata_only.json',meta)
    def journal(self,unit,name,kind='engine'):
        start=datetime.fromtimestamp(self.start,timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        end=datetime.fromtimestamp(self.end,timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        # json mode preserves timestamp and avoids locale-specific parsing.
        proc=None
        try:
            proc=subprocess.Popen(['journalctl','-u',unit,'--since',start,'--until',end,'-o','json','--no-pager'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            summaries=collections.Counter();alerts=collections.deque(maxlen=1700);recent=collections.deque(maxlen=500);count=0;filtered=0
            for raw in proc.stdout:
                count+=1
                try:r=json.loads(raw)
                except (ValueError,UnicodeError):continue
                message=str(r.get('MESSAGE',''))
                # Do not send irrelevant engine heartbeat lines or private payloads.
                is_action=any(w in message.upper() for w in ALERT_MARKERS)
                if kind=='engine' and ('РЕЖИМ=RUN' in message or 'BUY_CANCEL_PROTECTED' in message):
                    summaries['run_status_or_protected_lines']+=1
                    continue
                if DANGER.search(message) or TOKEN.search(message):
                    summaries['possible_sensitive_line_excluded']+=1
                    continue
                tag=message.split(' ',1)[0][:70] if message else '(empty)'
                summaries[tag]+=1
                if is_action:
                    alerts.append({'ts_us':r.get('__REALTIME_TIMESTAMP'),'text':clean_str(message,480)})
                elif kind in ('observer','telegram') and any(w in message for w in ('MONITOR','RECOVERED','STATUS','ACTIVE','DELIVERY')):
                    recent.append({'ts_us':r.get('__REALTIME_TIMESTAMP'),'text':clean_str(message,350)})
            proc.wait(timeout=15)
            self.json(f'{name}_journal_summary.json',{'unit':unit,'lines_scanned':count,'tags':dict(summaries),'exit_code':proc.returncode})
            self.add(f'{name}_journal_alerts.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in alerts))
            self.add(f'{name}_journal_diagnostic.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in recent))
        except (OSError,ValueError,subprocess.TimeoutExpired) as e:
            self.errors.append(f'journal {unit}: {type(e).__name__}')
        finally:
            if proc and proc.poll() is None:proc.kill()
    def telegram(self):
        p=Path('/var/lib/okxbot/telegram_monitor_state.json')
        out={}
        if p.is_file() and not p.is_symlink():
            try:
                st=json.loads(p.read_text())
                out={'outbox_len':len(st.get('alert_outbox',[])),
                     'dead_letter_total':st.get('alert_dead_letter_total'),
                     'dead_letter_count':len(st.get('alert_dead_letters',[])),
                     'incident_dedupe_key_count':len(st.get('incident_sent',{})),
                     'offsets_count':len(st.get('offsets',{})),'file_age_sec':round(max(0.0,time.time()-p.stat().st_mtime),1),
                     'NOTICE':'No messages, tokens, identifiers or raw Telegram state exported'}
            except (OSError,ValueError) as e:out={'error':type(e).__name__}
        self.json('telegram_delivery_metadata_only.json',out)
        self.journal('okx-telegram-monitor.service','telegram','telegram')
    def run(self):
        stamp=datetime.fromtimestamp(self.end,timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        final=self.outdir/f'okxbot_diagnostics_{self.hours}h_{stamp}.zip'
        self.outdir.mkdir(parents=True,exist_ok=True)
        if final.exists():raise RuntimeError('Output already exists; refusing overwrite')
        os.umask(0o077)
        with zipfile.ZipFile(final,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
            self.zip=z
            self.json('manifest.json',{'collector':'OKX read-only diagnostic collector v1',
                'window_hours':self.hours,'start_utc':utc(self.start),'end_utc':utc(self.end),
                'bots':list(self.bots),'script_exec_args':'no API/REST/POST, no trading actions, no service modifications',
                'secrets':'No env, raw configs, raw state, SQLite, Telegram text, order IDs or API credentials copied',
                'limits':{'event_lines_per_bot':MAX_EVENT_EXPORT,'top_latency_spikes_per_bot':MAX_SLOW_EXPORT,
                          'observer_lines_per_kind_file':MAX_OBSERVER_LINES,'journal_priority_lines_per_unit':1700},
                'note':'Actual coverage can be under 24h due to log rotation, truncation, service restart, monitoring gaps.'})
            self.services();self.telegram()
            for bot in self.bots:
                self.noted(f'{bot}: collecting metadata, latency, execution, observers, journals')
                self.snapshot(bot)
                self.reconcile_profile(bot)
                lat=self.latency(bot)
                cnt=self.events(bot)
                self.noted(f'  latency N={lat["n"]:,}, covered_span={lat["observed_span_hours"]}h, events={sum(cnt.values()):,}')
                for kind in ('burst','execution'):
                    self.observer(bot,kind)
                    self.journal(f'observer-okx-grid-{bot}-{kind}.service',f'bots/{bot}/observer_{kind}',kind='observer')
                self.journal(f'okx-grid-{bot}.service',f'bots/{bot}/engine',kind='engine')
            self.json('collector_errors.json',self.errors)
            self.add('README.txt','OKX Grid Bot / read-only diagnostic report\n'
                     'Window UTC: '+utc(self.start)+' to '+utc(self.end)+'\n'
                     'Latency percentiles are from available on-disk samples; 15-minute and hourly breakdown.\n'
                     'Event counts are local emitted events, not independently OKX-confirmed unique fills.\n'
                     'Summary files are raw evidence. Do not interpret monitor disagreements as exchange deficits.\n'
                     'Telegram messages, credentials, full configurations/state, exchange API data and SQLite are excluded.\n'
                     'See manifest.json for sampling limits. No LIVE actions performed.\n')
        digest=hashlib.sha256()
        with final.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
        os.chmod(final,0o600)
        print('ARCHIVE:',final)
        print('SHA256:',digest.hexdigest())
        print('BYTES:',final.stat().st_size)
        print('COMMAND TO DOWNLOAD ON MAC:')
        print(f'scp root@<vps-host>:{final} "$HOME/Downloads/"')
        print('\n'.join(self.lines))
        if self.errors:print('COLLECTOR WARNINGS:',len(self.errors),'(see collector_errors.json)')
        return final


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--hours',type=int,default=24,choices=(24,))
    ap.add_argument('--output-dir',type=Path,default=OUT)
    ap.add_argument('--bot',action='append',dest='bots',required=True,
                    help='Explicit bot instance; repeat for each instrument')
    args=ap.parse_args()
    if (not args.bots or len(args.bots)>25 or len(set(args.bots))!=len(args.bots)
        or any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,19}',b) for b in args.bots)):
        ap.error('Provide unique valid --bot names (1..25)')
    if os.geteuid()!=0:
        raise SystemExit('Run as root on VPS for read-only systemd + local log access')
    Collector(args.hours,args.output_dir,args.bots).run()

if __name__=='__main__':main()
