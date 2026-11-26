#!/usr/bin/env python3
"""Reader 230+1 forensic audit. READ-ONLY, stdlib, no OKX/API/IPC calls.

Reads running systemd service metadata, Reader's own Python source/backup hash,
bot-local logs/latency.jsonl (including .1 rotation) and Reader journal summary.
Does not import or run any bot/Reader code, read credentials or trading state,
write files, restart services or request positions.
"""
from __future__ import annotations
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

# Two previously observed intervals; separate their lengths explicitly.
WINDOWS = {
    'BEFORE': ('2026-11-17T16:45:37', '2026-11-17T17:02:00'),
    'AFTER':  ('2026-11-17T21:29:06', '2026-11-17T21:45:06'),
}
PHASES = {'position_reader_ipc','position_reader_quota','position_reader_http',
          'positions_gate', 'positions_gate_quota_wait','positions_gate_fifo_wait',
          'b3_event_to_reconcile','tp_recon_phase','tp_collect_to_intent',
          'tp_intent_to_submit','buy_batch'}
SERVICE='okx-position-reader.service'
READER_PATH=Path('/opt/okx-grid-live/okx_position_reader_service.py')
MANIFEST=Path('/srv/okx-reader-pacing-backups/current.json')


def command(argv, timeout=30):
    p=subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, timeout=timeout, check=False)
    if p.returncode:
        raise RuntimeError('command failed ('+str(p.returncode)+'): '+' '.join(argv[:3])+' '+p.stderr[-140:])
    return p.stdout


def zulu(s):
    return datetime.fromisoformat(s+'+00:00')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def active_bots():
    units=[]
    lines=command(['systemctl','list-units','--type=service','--state=running',
                   '--all','--plain','--no-legend','--no-pager']).splitlines()
    for line in lines:
        first=line.split()
        if first and re.fullmatch(r'okx-grid-[a-zA-Z0-9_.-]+\.service', first[0]):
            units.append(first[0])
    bots={}
    for unit in sorted(set(units)):
        fields={}
        output=command(['systemctl','show',unit,'--no-pager','-p','WorkingDirectory',
                        '-p','MainPID','-p','ActiveState','-p','SubState'])
        for line in output.splitlines():
            k,sep,v=line.partition('=')
            if sep: fields[k]=v
        if fields.get('ActiveState')!='active' or fields.get('SubState')!='running':
            continue
        path=Path(fields.get('WorkingDirectory') or '/')
        if not path.is_dir() or path == Path('/'):
            try: path=(Path('/proc')/fields['MainPID']/'cwd').resolve(strict=True)
            except (OSError,KeyError): path=None
        if path is not None and path.is_dir() and not path.is_symlink():
            bots[unit]=path
        else:
            print('BOT_PATH_MISSING',unit)
    return bots


def unique_named(nodes, name, typ):
    matches=[x for x in nodes if isinstance(x,typ) and x.name==name]
    if len(matches)!=1: raise RuntimeError('AST missing/duplicate '+name)
    return matches[0]


def source_check():
    print('===== READ-ONLY SOURCE AUDIT =====')
    pid_out=command(['systemctl','show',SERVICE,'--no-pager',
                     '-p','MainPID','-p','ActiveState','-p','NRestarts'])
    print('READER_SYSTEMD', ' '.join(pid_out.splitlines()))
    raw=READER_PATH.read_bytes()
    print('LIVE_READER_SHA256',sha(raw))
    tree=ast.parse(raw.decode('utf-8'))
    quota=unique_named(tree.body,'SharedOriginalQuota',ast.ClassDef)
    take=unique_named(quota.body,'take',ast.AsyncFunctionDef)
    init=unique_named(quota.body,'__init__',ast.FunctionDef)
    pacer=unique_named(tree.body,'_ReaderPacer230PlusOne',ast.ClassDef)
    first=ast.unparse(take.body[0])
    first_init=ast.unparse(init.body[0])
    print('QUOTA_TAKE_FIRST',first)
    print('QUOTA_INIT_FIRST',first_init)
    if 'await self._reader_pacer.take(priority_supplier)'!=first:
        print('ALERT_UNEXPECTED_QUOTA_CALL_LOCATION')
    for n in pacer.body:
        if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name):
            if n.targets[0].id in ('GAP','CAPACITY'):
                print('PACER_SETTING',n.targets[0].id,ast.literal_eval(n.value))
    if MANIFEST.is_file() and not MANIFEST.is_symlink():
        m=json.loads(MANIFEST.read_text())
        print('MANIFEST_INSTALLED_HASH_MATCH',sha(raw)==m.get('after'))
        backup=Path(m['backup'])
        print('BACKUP_HASH_MATCH',backup.is_file() and sha(backup.read_bytes())==m.get('before'))
    else:
        print('MANIFEST_MISSING_CANNOT_PROVE_BACKUP')
    core_file=READER_PATH.with_name('okx_position_reader_core.py')
    if core_file.is_file() and not core_file.is_symlink():
        root=ast.parse(core_file.read_text(encoding='utf8'))
        core=unique_named(root.body,'SharedReaderCore',ast.ClassDef)
        worker=unique_named(core.body,'_worker',ast.AsyncFunctionDef)
        text=ast.unparse(worker)
        qidx=text.find('quota_begin = time.monotonic_ns()')
        tidx=text.find('await asyncio.wait_for(self.quota.take(')
        eidx=text.find('quota_wait_ms = round(')
        print('QUOTA_METRIC_INCLUDES_PACER',0<=qidx<tidx<eidx)
    print('NO_SOURCE_CHANGE=YES')


def files_for_bot(path):
    folder=path/'logs'
    return [p for p in (folder/'latency.jsonl.1',folder/'latency.jsonl')
            if p.is_file() and not p.is_symlink()]


def collect(bot_paths):
    stats=defaultdict(list)
    loaded=Counter(); bad=Counter()
    for unit,path in bot_paths.items():
        files=files_for_bot(path)
        if not files:
            print('MISSING_LATENCY_JOURNAL',unit,str(path/'logs'/'latency.jsonl'))
            continue
        for file in files:
            with file.open('r',encoding='utf8',errors='replace') as handle:
                for line in handle:
                    if not any(('"'+p+'"') in line for p in PHASES):
                        continue
                    try:
                        r=json.loads(line)
                        phase=r.get('phase'); stamp=r.get('ts','')
                        ms=r.get('ms')
                        if phase not in PHASES or type(ms) not in (int,float) or not math.isfinite(ms) or ms<0:
                            continue
                        if not isinstance(stamp,str) or len(stamp)<19 or not stamp.endswith('+00:00'):
                            bad[unit]+=1;continue
                        ts=stamp[:19]
                        windows=[win for win,(start,end) in WINDOWS.items() if start<=ts<end]
                        if not windows:continue
                        priority=r.get('priority','unknown')
                        if priority not in ('normal','background','exit','emergency'):priority='unknown'
                        outcome=r.get('outcome','unknown')
                        item=(ms,unit,priority,str(outcome))
                        for win in windows: stats[(win,phase)].append(item)
                        loaded[unit]+=1
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        bad[unit]+=1
    return stats,loaded,bad


def quantile(s,p):
    if not s: return float('nan')
    a=sorted(s)
    return a[min(len(a)-1,max(0,math.ceil(len(a)*p)-1))]


def format_latency(vals):
    if not vals:return 'n=0 NO_DATA'
    v=[x[0] for x in vals]
    return ('n=%d p50=%.1f p95=%.1f p99=%.1f max=%.1f >200=%d >500=%d >1000=%d' %
            (len(v),quantile(v,.5),quantile(v,.95),quantile(v,.99),max(v),
             sum(x>200 for x in v),sum(x>500 for x in v),sum(x>1000 for x in v)))


def compare(stats):
    print('===== FULL IPC AND QUOTA: TWO WINDOWS, UTC =====')
    for win,(start,end) in WINDOWS.items():
        duration=(zulu(end)-zulu(start)).total_seconds()
        print('\n'+win, start,'to',end,'duration_s=%.0f'%duration)
        for phase in ('position_reader_ipc','position_reader_quota','position_reader_http',
                      'positions_gate','positions_gate_quota_wait','positions_gate_fifo_wait',
                      'b3_event_to_reconcile','tp_recon_phase','tp_collect_to_intent',
                      'tp_intent_to_submit','buy_batch'):
            vals=stats.get((win,phase),[])
            if vals:print(phase,format_latency(vals))
        vals=stats.get((win,'position_reader_ipc'),[])
        if vals:
            for priority in ('normal','background','exit','emergency','unknown'):
                group=[x for x in vals if x[2]==priority]
                if group:print('  IPC_'+priority.upper(),format_latency(group))
            for outcome in ('ok','unavailable'):
                group=[x for x in vals if x[3]==outcome]
                if group:print('  IPC_OUTCOME_'+outcome.upper(),len(group))
            for unit in sorted({x[1] for x in vals}):
                group=[x for x in vals if x[1]==unit]
                print('  IPC_BOT',unit,format_latency(group))
    print('\nNOTE: distributions are empirical, not matched-load causal A/B')
    print('NOTE: position_reader_quota is logged from server-reported quota_wait and includes pacing')
    print('NOTE: trading-event latency phases, when present, are separate from Reader IPC')


def reader_journal():
    print('\n===== READER JOURNAL OBSERVABILITY =====')
    for win,(start,end) in WINDOWS.items():
        # journalctl displays UTC when --utc specified; use absolute +0000 timestamps.
        j=command(['journalctl','-u',SERVICE,'--since',start.replace('T',' ')+' UTC',
                   '--until',end.replace('T',' ')+' UTC','--utc','-o','cat',
                   '--no-pager'],timeout=45)
        count=Counter();qmax=[]; qp95=[];fifo=[]
        for line in j.splitlines():
            if 'READER_OBSERVABILITY ' in line:
                try: obj=json.loads(line.split('READER_OBSERVABILITY ',1)[1])
                except ValueError:continue
                for key in ('http_attempts','http_ok','http_429','http_non200',
                            'okx_50011','transport_errors'):
                    count[key]+=obj.get('positions_http_delta',{}).get(key,0)
                for key in ('get_batches','coalesced_clients','quota_abandoned',
                            'quota_failed','fetch_failed'):
                    count[key]+=obj.get('core_delta',{}).get(key,0)
                wait=obj.get('interval',{}).get('quota_wait',{})
                if type(wait.get('max_ms')) in (int,float):qmax.append(wait['max_ms'])
                if type(wait.get('p95_ms')) in (int,float):qp95.append(wait['p95_ms'])
            if 'READER_FIFO_WAIT_DETAIL ' in line:
                try: obj=json.loads(line.split('READER_FIFO_WAIT_DETAIL ',1)[1]);fifo.append(obj)
                except ValueError:pass
        print(win,'OBSERVABILITY',dict(sorted(count.items())))
        print(win,'QUOTA_WAIT_MAX_ACROSS_MINUTES_MS',max(qmax) if qmax else 'NO_DATA',
              'P95_MINUTE_MAX_MS',max(qp95) if qp95 else 'NO_DATA')
        print(win,'FIFO_DETAILS_OVER_THRESHOLD',len(fifo),
              'FIFO_DETAIL_MAX_MS',max((f.get('total_ms',0) for f in fifo),default='NO_DATA'))
    print('NOTE: physical HTTP totals are only Reader calls; direct fallback GETs are not included')


def main():
    try: os=__import__('os');os.nice(10)
    except OSError:pass
    source_check()
    bots=active_bots()
    print('\nACTIVE_BOTS',len(bots))
    for unit,path in bots.items():print('BOT_SOURCE',unit,str(path))
    stats,loaded,bad=collect(bots)
    print('LATENCY_RECORDS_IN_WINDOWS',dict(loaded),'SKIPPED_RECORDS',dict(bad))
    compare(stats)
    reader_journal()
    print('\n===== READ_ONLY_AUDIT_COMPLETE =====')

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('AUDIT_INCOMPLETE',type(e).__name__,str(e),file=sys.stderr)
        sys.exit(2)
