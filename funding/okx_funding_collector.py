"""F3 candidate. Standard library only; no engine imports or order methods."""
import argparse
import base64
import calendar
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import time
from urllib.parse import urlencode
from okx_funding_backup import daily_backup
from okx_funding_ledger import (LedgerError, require, instrument, creation_ms,
    bill_envelope, merged_coverage, apply_complete_windows, SCHEMA,
    terminal_identity, read_terminal_snapshot, terminal_horizon,
    finalize_covered_terminals, record_terminal_attempts, terminal_failure, terminal_env_token)

DAY = 86400000
HOUR = 3600000
RECENT = '/api/v5/account/bills'
ARCHIVE = '/api/v5/account/bills-archive'
KEYS = ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'), parse_float=Decimal)


def read_credentials(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key in KEYS:
            require(key not in result, 'DUPLICATE_CREDENTIAL_FIELD')
            result[key] = value.strip().strip('"').strip("'")
    require(all(result.get(k) for k in KEYS), 'CREDENTIALS_INCOMPLETE')
    return result


class BotInventory(dict):
    """Publishable bots plus isolated record failures and history planning candidates."""
    def __init__(self):
        super().__init__()
        self.blocked = set()
        self.issues = []
        self.candidates = []
        self.terminals = []
        self.terminal_issues = []
        self.terminal_rejected = []
        self.live_proofs = {}


def binding(path):
    path = Path(path)
    require(not path.is_symlink(), 'DB_SYMLINK')
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        require(db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA,'UNSUPPORTED_SCHEMA')
        row = db.execute("SELECT value FROM meta WHERE key='account_fingerprint'").fetchone()
        return row[0] if row else None


def inventory(base, registry, systemd_dir, expected=None):
    """Isolate bad registry entries. Never guess which account is intended."""
    base = Path(base).resolve()
    entries = read_json(Path(registry))
    require(isinstance(entries, dict), 'MALFORMED_REGISTRY')
    bots = BotInventory()
    records, choices, terminal_records = [], [], []
    uncertain_terminals = []
    seen = set()
    for name, entry in sorted(entries.items()):
        inst = None
        retired = isinstance(entry,dict) and bool(entry.get('retired'))
        try:
            require(isinstance(name, str) and isinstance(entry, dict), 'MALFORMED_ENTRY')
            root = Path(entry['path']).resolve()
            require(root != base and base in root.parents, 'INVALID_BOT_PATH')
            # Read cfg first so errors in ack/unit can be isolated by instrument.
            cfg = read_json(root/'config.json')
            inst = instrument(cfg['instrument'])
            created = cfg['created_at']
            creation_ms(created)
            require(creation_ms(created) <= int(time.time()*1000), 'FUTURE_CREATED_AT')
            ack_path = root/'state/control_ack.json'
            # A terminal instance needs its own proven identity and account binding.
            try:
                ack = read_json(ack_path) if ack_path.exists() else {}
                require(isinstance(ack, dict), 'MALFORMED_ACK')
            except (OSError,ValueError,TypeError):
                ack = None
            if entry.get('retired') or (ack is not None and ack.get('phase') in ('RETIRED','PANIC_DONE')):
                # Incomplete historical archives do not contaminate the live inventory.
                try:
                    identity, position = terminal_identity(root)
                    token=terminal_env_token(root)
                    current = read_credentials(root/'.env')
                    require(token==terminal_env_token(root),'CREDENTIAL_FILE_CHANGED')
                    terminal_records.append((identity,current))
                except (OSError,ValueError,KeyError,TypeError,ArithmeticError):
                    bots.terminal_issues.append(('terminal:'+str(name)[:80],'TERMINAL_EVIDENCE_UNAVAILABLE'))
                    uncertain_terminals.append((inst,creation_ms(created)))
                continue
            bots.candidates.append(created)
            if inst in seen:
                bots.blocked.add(inst)
                bots.issues.append((inst,'DUPLICATE_NONTERMINAL_INSTRUMENT'))
            seen.add(inst)
            token=terminal_env_token(root)
            current = read_credentials(root/'.env')
            require(token==terminal_env_token(root),'CREDENTIAL_FILE_CHANGED')
            choices.append(current)
            require(ack is not None, 'MALFORMED_ACK')
            require(not (root/'state/final_stats.json').exists(), 'AMBIGUOUS_TERMINAL_STATE')
            service = entry.get('service')
            require(isinstance(service,str) and service == 'okx-grid-'+root.name and
                    re.fullmatch(r'okx-grid-[A-Za-z0-9_-]+', service) and
                    (Path(systemd_dir)/(service+'.service')).is_file(), 'INVALID_SERVICE')
            records.append((inst,created,current,root.name,token))
        except (OSError,ValueError,KeyError,TypeError,ArithmeticError) as exc:
            reason = str(exc) if isinstance(exc,LedgerError) and re.fullmatch('[A-Z0-9_]+',str(exc)) else type(exc).__name__
            if retired:
                bots.terminal_issues.append(('terminal:'+str(name)[:80],reason))
                continue
            if inst is not None:
                bots.blocked.add(inst)
            bots.issues.append((inst or ('registry:'+str(name)[:80]),reason))
    # Use a previously bound key when one exists; otherwise require consensus.
    # If all bots have stopped, a verified terminal instance can still supply
    # credentials for the existing bound account. Never let an old credential
    # override the live account selection.
    eligible = [c for c in choices if expected is None or hashlib.sha256(c[KEYS[0]].encode()).hexdigest()==expected]
    if not eligible and not choices:
        eligible = [c for _,c in terminal_records if expected is None or hashlib.sha256(c[KEYS[0]].encode()).hexdigest()==expected]
    if not eligible:
        require(not choices and not terminal_records and not bots.issues, 'NO_TRUSTED_ACCOUNT_CREDENTIALS')
        return bots,None
    creds = eligible[0]
    require(all(c==creds for c in eligible), 'ACCOUNT_CREDENTIALS_AMBIGUOUS')
    for inst,created,current,name,token in records:
        if current != creds:
            bots.blocked.add(inst)
            bots.issues.append((inst,'ACCOUNT_CREDENTIALS_MISMATCH'))
        if inst not in bots.blocked:
            bots[inst]=created
            bots.live_proofs[inst]=(name,creation_ms(created),token)
    trusted=[]
    for ident,current in terminal_records:
        if current!=creds:
            bots.terminal_issues.append(('terminal:'+ident[0],'ACCOUNT_CREDENTIALS_MISMATCH'))
            bots.terminal_rejected.append((ident,'ACCOUNT_CREDENTIALS_MISMATCH'))
            continue
        trusted.append(ident)
    # NET mode permits only disjoint lifetimes for one instrument. A malformed
    # historical overlap is not a basis for allocating account bills twice.
    for ident in trusted:
        name,inst,created,end,_,_=ident
        # A shared settlement minute is ambiguous even when lifetimes do not
        # literally overlap: an account bill does not identify the bot.
        stop=max(end,terminal_horizon(ident))
        overlap=any(other!=ident and other[1]==inst and other[2]<stop
                    and created<max(other[3],terminal_horizon(other))
                    for other,_ in terminal_records)
        overlap=overlap or any(i==inst and creation_ms(c)<stop for i,c,_,_,_ in records)
        overlap=overlap or any(i==inst and c<stop for i,c in uncertain_terminals)
        if overlap:
            bots.terminal_issues.append(('terminal:'+name,'OVERLAPPING_INSTRUMENT_LIFETIMES'))
            bots.terminal_rejected.append((ident,'OVERLAPPING_INSTRUMENT_LIFETIMES'))
            continue
        bots.terminals.append(ident)
    return bots,creds


class BillsClient:
    def __init__(self, creds, *, clock=time.monotonic, sleep=time.sleep, connection=None):
        self.creds = creds
        self.clock = clock
        self.sleep = sleep
        self.connection = connection or (lambda: http.client.HTTPSConnection(
            'www.okx.com', timeout=12, context=ssl.create_default_context()))
        self.last_end = None
        self.requests = 0

    def page(self, endpoint, begin, end, after=None):
        require(endpoint in (RECENT, ARCHIVE), 'ENDPOINT_NOT_ALLOWED')
        require(begin < end, 'INVALID_WINDOW')
        require(self.requests < 1024, 'REQUEST_BUDGET_EXCEEDED')
        # One second after the previous response, across both endpoints.
        if self.last_end is not None:
            delay = 1 - (self.clock() - self.last_end)
            if delay > 0:
                self.sleep(delay)
        params = dict(instType='SWAP', ccy='USDT', type='8', limit='100',
                      begin=str(begin), end=str(end-1))
        if after is not None:
            params['after'] = after
        path = endpoint+'?'+urlencode(params)
        stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        signature = base64.b64encode(hmac.new(self.creds[KEYS[1]].encode(),
            (stamp+'GET'+path).encode(), hashlib.sha256).digest()).decode()
        headers = {'OK-ACCESS-KEY':self.creds[KEYS[0]], 'OK-ACCESS-SIGN':signature,
                   'OK-ACCESS-TIMESTAMP':stamp, 'OK-ACCESS-PASSPHRASE':self.creds[KEYS[2]],
                   'User-Agent':'okx-funding-collector', 'Content-Type':'application/json'}
        conn = self.connection()
        self.requests += 1
        try:
            conn.request('GET', path, headers=headers)
            response = conn.getresponse()
            require(response.status == 200, 'HTTP_'+str(response.status))
            body = response.read(4*1024*1024+1)
            require(len(body) <= 4*1024*1024, 'RESPONSE_TOO_LARGE')
            data = json.loads(body)
            require(isinstance(data, dict) and data.get('code') == '0', 'OKX_API_ERROR')
            rows = data.get('data')
            require(isinstance(rows,list) and len(rows)<=100, 'INVALID_PAGE')
            return rows
        finally:
            conn.close()
            self.last_end = self.clock()


def fetch_window(client, endpoint, begin, end):
    rows, ids = [], set()
    cursor = None
    while True:
        page = client.page(endpoint, begin, end, cursor)
        previous = int(cursor) if cursor is not None else None
        for row in page:
            bid, _, _ = bill_envelope(row,begin,end)
            require(bid not in ids, 'REPEATED_PAGE_BILL')
            # Documented newest-first pagination by billId; no time cursor.
            require(previous is None or int(bid) < previous, 'NONMONOTONIC_BILL_CURSOR')
            previous = int(bid)
            ids.add(bid)
            rows.append(row)
        if len(page) < 100:
            return begin,end,rows
        cursor = page[-1]['billId']


def read_state(path, account_fingerprint=None):
    path = Path(path)
    require(not path.is_symlink(), 'DB_SYMLINK')
    if not path.exists():
        return [], None, None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        require(db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA,'UNSUPPORTED_SCHEMA')
        intervals = db.execute('SELECT begin_ms,end_ms FROM coverage').fetchall()
        meta = dict(db.execute('SELECT key,value FROM meta'))
        if account_fingerprint is not None:
            bound = meta.get('account_fingerprint')
            if bound is None:
                require('synced_ms' not in meta and db.execute('SELECT count(*) FROM bills').fetchone()[0] == 0,
                        'UNBOUND_EXISTING_HISTORY')
            else:
                require(bound == account_fingerprint, 'ACCOUNT_BINDING_MISMATCH')
    return merged_coverage(intervals), int(meta['synced_ms']) if 'synced_ms' in meta else None, int(meta['reconciled_ms']) if 'reconciled_ms' in meta else None


def archive_floor(now):
    """Conservative interpretation of rolling 3 months; safety margin at moving edge."""
    dt = datetime.fromtimestamp(now/1000,timezone.utc)
    y, m = divmod(dt.year*12+dt.month-1-3,12)
    m += 1
    prior = dt.replace(year=y,month=m,day=min(dt.day,calendar.monthrange(y,m)[1]))
    return max(int(prior.timestamp()*1000),now-90*DAY)+HOUR


def gaps(intervals, start, end):
    cursor = start
    for a,b in merged_coverage(intervals):
        if b <= cursor:
            continue
        if a >= end:
            break
        if a > cursor:
            yield cursor,min(a,end)
        cursor=max(cursor,b)
        if cursor >= end:
            return
    if cursor < end:
        yield cursor,end


def plan_windows(bots, intervals, last_sync, last_reconcile, now, mode):
    end=now-120000
    oldest=min((creation_ms(v) for v in list(bots.values())+getattr(bots,"candidates",[])), default=now-DAY)
    require(oldest <= now, 'FUTURE_CREATED_AT')
    require(last_sync is None or last_sync <= now,'CLOCK_ROLLBACK')
    floor=archive_floor(now)
    recent_floor=now-7*DAY+HOUR
    reconcile=(mode!='backfill' and (mode=='reconcile' or last_reconcile is None or now-last_reconcile>=DAY))
    # Never advertise inaccessible history as covered. Keep permanent gaps in ledger.
    if mode=='backfill':
        needed=[(max(oldest,floor),end)]
    else:
        recent_start=end-7*DAY if reconcile else end-DAY
        needed=list(gaps(intervals,max(oldest,floor),end))
        if recent_start<end:
            needed.append((recent_start,end))
    result=[]
    for a,b in merged_coverage([(a,b) for a,b in needed if a<b]):
        if a < recent_floor:
            archive_end=min(b,recent_floor+HOUR)
            # Bound archive time ranges, overlap latest history on the endpoint seam.
            cursor=a
            while cursor<archive_end:
                stop=min(cursor+7*DAY,archive_end)
                result.append((ARCHIVE,cursor,stop));cursor=stop
        if b>recent_floor:
            result.append((RECENT,max(a,recent_floor),b))
    # Newly-created bots inside the 2-minute settlement cushion need a ready zero
    # only after an actual completed account window. Fetch the most recent day.
    if not result:
        result=[(RECENT,end-DAY,end)]
    return result,reconcile


def pending_terminals(db_path,bots,now):
    result=[]
    for ident in getattr(bots,'terminals',()):
        if max(ident[3],terminal_horizon(ident))>now:
            continue
        row=read_terminal_snapshot(db_path,ident) if Path(db_path).exists() else None
        # Incomplete archives are retried by regular collection, not every minute.
        if row is None or (not row[3] and row[2]<=now-HOUR):
            result.append(ident)
    return result


def regular_due(path,last,now):
    stamp=None
    if Path(path).exists():
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            row=db.execute("SELECT value FROM meta WHERE key='regular_synced_ms'").fetchone()
            if row:
                stamp=int(row[0])
    if stamp is None:
        stamp=last
    due=(now-5*60000)//HOUR*HOUR+5*60000
    return stamp is None or stamp<due


def terminal_windows(identities, intervals, now):
    floor=archive_floor(now)
    recent_floor=now-7*DAY+HOUR
    needed=[]
    for ident in identities:
        created=ident[2]; stop=terminal_horizon(ident)
        missing=list(gaps(intervals,max(created,floor),stop)) if max(created,floor)<stop else []
        needed.extend(missing)
        # No forced fetch through PANIC time. If only contradictory history
        # prevents completion, the regular reconciliation will revisit it.
    plan=[]
    for a,b in merged_coverage([(a,b) for a,b in needed if a<b]):
        original=a
        if a<recent_floor:
            stop=min(b,recent_floor+HOUR)
            while a<stop:
                nxt=min(a+7*DAY,stop);plan.append((ARCHIVE,a,nxt));a=nxt
        if b>recent_floor:
            plan.append((RECENT,max(original,recent_floor),b))
    return plan


def run_once(db_path, inventory_fn, client_factory, *, mode='update', now=None):
    real_clock = now is None
    now=int(time.time()*1000) if real_clock else now
    bots, creds=inventory_fn()
    if creds is None:
        require(not bots, 'NO_TRUSTED_ACCOUNT_CREDENTIALS')
        return {'status':'NO_NONTERMINAL_BOTS','requests':0}
    fingerprint=hashlib.sha256(creds[KEYS[0]].encode()).hexdigest()
    intervals, last, reconciled=read_state(db_path, fingerprint)
    pending=pending_terminals(db_path,bots,now)
    # Publish already-covered terminal accounting before any API request,
    # without advancing regular collection or changing live snapshots.
    covered=[i for i in pending if any(a<=i[2] and b>=terminal_horizon(i) for a,b in intervals) or terminal_horizon(i)==i[2]]
    local=finalize_covered_terminals(db_path,covered,fingerprint,now) if covered and Path(db_path).exists() else 0
    pending=pending_terminals(db_path,bots,now)
    for identity,reason in getattr(bots,'terminal_rejected',()):
        if Path(db_path).exists() and terminal_failure(db_path,identity,fingerprint)!=reason:
            record_terminal_attempts(db_path,[identity],fingerprint,reason,now)
    if mode=='auto':
        if regular_due(db_path,last,now):
            mode='update'
        elif pending:
            mode='terminal'
        elif local:
            return {'status':'PASS','requests':0,'published':0,'terminal_published':local,'local_only':True}
        else:
            # Cache eligibility only when there are no time-dependent pending
            # terminals. The CLI still checks the regular schedule every tick.
            settled=all((read_terminal_snapshot(db_path,i) or (None,None,None,0))[3] for i in getattr(bots,'terminals',()))
            return {'status':'IDLE','requests':0,'idle_safe':settled}
    if mode=='terminal':
        if not pending:
            return {'status':'PASS','requests':0,'published':0,'terminal_published':local,'local_only':True} if local else {'status':'IDLE','requests':0}
        # Failed attempts may be recovered at the regular hourly collection.
        # Do not retry a failing terminal GET on every minute tick.
        pending=[i for i in pending if not Path(db_path).exists() or not terminal_failure(db_path,i,fingerprint)]
        plan=terminal_windows(pending,intervals,now)
        reconcile=False
        if not plan:
            for identity in pending:
                record_terminal_attempts(db_path,[identity],fingerprint,'INCOMPLETE_HISTORY',now)
            return {'status':'PASS','requests':0,'published':0,'terminal_published':local,'local_only':True} if local else {'status':'IDLE','requests':0}
    else:
        plan,reconcile=plan_windows(bots,intervals,last,reconciled,now,mode)
        if pending:
            plan+=terminal_windows(pending,intervals,now)
            combined=[]
            for endpoint in (ARCHIVE,RECENT):
                for a,b in merged_coverage([(a,b) for e,a,b in plan if e==endpoint]):
                    while a<b:
                        stop=min(a+7*DAY,b) if endpoint==ARCHIVE else b
                        combined.append((endpoint,a,stop));a=stop
            plan=combined
    try:
        client=client_factory(creds)
        windows=[fetch_window(client,*p) for p in plan]
    except Exception as exc:
        reason=str(exc) if isinstance(exc,LedgerError) and re.fullmatch('[A-Z0-9_]+',str(exc)) else type(exc).__name__.upper()
        record_terminal_attempts(db_path,pending,fingerprint,'COLLECTION_'+reason,now)
        raise
    bots_after, creds_after=inventory_fn()
    # A change of account is a global error. A change of inventory is local.
    require(creds_after==creds or creds_after is None,'ACCOUNT_CHANGED_DURING_FETCH')
    stable={i:v for i,v in bots_after.items() if i in bots and creation_ms(bots[i])==creation_ms(v)}
    before_proofs=getattr(bots,'live_proofs',{})
    after_proofs=getattr(bots_after,'live_proofs',{})
    proofs={i:after_proofs[i] for i in stable if i in after_proofs and after_proofs[i]==before_proofs.get(i)}
    blocked=set(getattr(bots_after,'blocked',()))
    issues=list(getattr(bots_after,'issues',()))
    terminal_issues=list(getattr(bots_after,'terminal_issues',()))
    for inst in set(bots_after)-set(stable):
        blocked.add(inst)
        issues.append((inst,'INVENTORY_CHANGED_DURING_FETCH'))
    stable_terminals=sorted(set(getattr(bots,'terminals',())) & set(getattr(bots_after,'terminals',())))
    # No terminal record is finalized during its possible settlement minute.
    stable_terminals=[i for i in stable_terminals if max(i[3],terminal_horizon(i))<=now]
    synced=int(time.time()*1000) if real_clock else now
    result=apply_complete_windows(db_path,windows,stable,synced,
                                  reconciled_ms=synced if reconcile else None, account_fingerprint=fingerprint,
                                  blocked=blocked,issues=issues+terminal_issues,reconcile_missing=True,
                                  terminals=stable_terminals,regular_sync=mode!='terminal',live_proofs=proofs,publish_live=mode!='terminal')
    for identity in stable_terminals:
        row=read_terminal_snapshot(db_path,identity)
        if row is not None and not row[3]:
            record_terminal_attempts(db_path,[identity],fingerprint,'INCOMPLETE_HISTORY',synced)
    result['terminal_published']+=local
    return dict(result,status='PASS',requests=client.requests,windows=len(windows),reconcile=reconcile,inventory_issues=len(issues),terminal_inventory_issues=len(terminal_issues))


def idle_inventory_stamp(base,registry,systemd_dir,db_path):
    """Cheap nonsecret stat fingerprint; terminal evidence hashes only on change.

    Registry and small ACKs are read to detect terminal transitions. Active
    bot_state is deliberately not read/stat'ed. A changed config, credentials,
    terminal state, accounting, unit, registry, or ledger invalidates the cache.
    Any ambiguous metadata bypasses the optimization, never the full inventory.
    """
    import stat
    def file_token(p):
        try:
            st=p.lstat()
        except FileNotFoundError:
            return None
        require(stat.S_ISREG(st.st_mode),'CACHE_UNSAFE_PATH')
        return [st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
    base=Path(base).resolve();registry=Path(registry)
    before=file_token(registry)
    entries=read_json(registry)
    require(before==file_token(registry) and isinstance(entries,dict),'CACHE_REGISTRY_CHANGED')
    wal=file_token(Path(str(db_path)+'-wal'))
    # A read-only SQLite connection may create a zero-length WAL; it carries
    # no ledger generation and must not invalidate an otherwise idle stamp.
    result=[before,file_token(Path(db_path)),wal if wal is not None and wal[2] else None]
    for name,entry in sorted(entries.items()):
        root=Path(entry['path']).resolve()
        require(root!=base and base in root.parents,'CACHE_UNSAFE_PATH')
        ack=root/'state/control_ack.json';ack_token=file_token(ack)
        phase=read_json(ack).get('phase') if ack_token is not None else None
        require(ack_token==file_token(ack),'CACHE_ACK_CHANGED')
        terminal=bool(entry.get('retired')) or phase in ('RETIRED','PANIC_DONE')
        final=root/'state/final_stats.json';final_token=file_token(final)
        # A final file can arrive later than ACK; both must invalidate.
        tokens=[name,str(root),file_token(root/'config.json'),file_token(root/'.env'),ack_token,final_token]
        if terminal or final_token is not None:
            tokens.append(file_token(root/'state/bot_state.json'))
        else:
            unit=entry.get('service')
            require(isinstance(unit,str) and re.fullmatch(r'okx-grid-[A-Za-z0-9_-]+',unit),'CACHE_UNSAFE_UNIT')
            tokens.append(file_token(Path(systemd_dir)/(unit+'.service')))
        result.append(tokens)
    return hashlib.sha256(json.dumps(result,separators=(',',':')).encode()).hexdigest()


def idle_stamp(a,db_path):
    try:
        return idle_inventory_stamp(a.base,a.registry,a.systemd_dir,db_path)
    except (OSError,ValueError,TypeError,KeyError,ArithmeticError):
        return None


def idle_cache_hit(a,db_path,cache,now):
    if a.mode!='auto' or not cache.is_file() or cache.is_symlink():
        return False
    try:
        _,last,_=read_state(db_path)
        if last is None or last>now or regular_due(db_path,last,now):
            return False
        stamp=idle_stamp(a,db_path)
        return stamp is not None and cache.read_text(encoding='ascii').strip()==stamp
    except (OSError,ValueError,TypeError,sqlite3.Error):
        return False


def save_idle_cache(cache,stamp):
    import tempfile
    if stamp is None or cache.is_symlink():
        return
    tmp=None
    try:
        fd,tmp=tempfile.mkstemp(prefix='.idle-inventory-',dir=cache.parent)
        with os.fdopen(fd,'w',encoding='ascii') as out:
            out.write(stamp+'\n')
        os.replace(tmp,cache)
    except OSError:
        pass  # Optimization only; a missing cache forces the full scan.
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--base',type=Path,required=True)
    p.add_argument('--registry',type=Path,required=True)
    p.add_argument('--systemd-dir',type=Path,required=True)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--mode',choices=['backfill','update','reconcile','auto','terminal'],default='update')
    a=p.parse_args()
    # Candidate only. Explicit deployment path; no implicit writes to bot tree.
    require(a.data_dir.is_dir() and not a.data_dir.is_symlink(),'DATA_DIR_NOT_READY')
    require(a.data_dir.resolve()!=a.base.resolve() and a.base.resolve() not in a.data_dir.resolve().parents,'DATA_DIR_INSIDE_BOT_BASE')
    os.umask(0o077)
    fd=os.open(a.data_dir/'collector.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('FUNDING_SKIP: LOCK_BUSY');return
        db_path=a.data_dir/'funding.sqlite'
        cache=a.data_dir/'idle_inventory.sha256'
        if idle_cache_hit(a,db_path,cache,int(time.time()*1000)):
            return
        stamp=idle_stamp(a,db_path) if a.mode=='auto' else None
        # Invalidate old cache before any full collection; do not reuse a stale
        # safe decision after an unsuccessful or partial inventory.
        if cache.is_file() and not cache.is_symlink():
            try: cache.unlink()
            except OSError: pass
        result=run_once(db_path,lambda:inventory(a.base,a.registry,a.systemd_dir,binding(db_path)),BillsClient,mode=a.mode)
        if result.get('idle_safe') and stamp is not None and stamp==idle_stamp(a,db_path):
            save_idle_cache(cache,stamp)
        if result['status']!='PASS':
            if result['status']!='IDLE':
                print(json.dumps(result,sort_keys=True))
            return
        # Backup runs under the same flock, after commit. Failure cannot undo a valid sync.
        try:
            result['backup'] = daily_backup(a.data_dir/'funding.sqlite')
        except Exception as exc:
            result['backup'] = 'FAILED_'+type(exc).__name__
            print(json.dumps(result,sort_keys=True))
            raise LedgerError('LOCAL_BACKUP_FAILED') from None
        print(json.dumps(result,sort_keys=True))
    finally:
        os.close(fd)

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        # Only own enumerated messages; never print raw response or credential path.
        msg=str(e) if isinstance(e,LedgerError) and re.fullmatch(r'[A-Z0-9_]+',str(e)) else type(e).__name__
        print('FUNDING_FAILED: '+msg)
        raise SystemExit(1) from None
