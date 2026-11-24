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
    bill_envelope, merged_coverage, apply_complete_windows, SCHEMA)

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
    records, choices = [], []
    seen = set()
    for name, entry in sorted(entries.items()):
        inst = None
        try:
            require(isinstance(name, str) and isinstance(entry, dict), 'MALFORMED_ENTRY')
            if entry.get('retired'):
                continue
            root = Path(entry['path']).resolve()
            require(root != base and base in root.parents, 'INVALID_BOT_PATH')
            # Read cfg first so errors in ack/unit can be isolated by instrument.
            cfg = read_json(root/'config.json')
            inst = instrument(cfg['instrument'])
            created = cfg['created_at']
            creation_ms(created)
            require(creation_ms(created) <= int(time.time()*1000), 'FUTURE_CREATED_AT')
            ack_path = root/'state/control_ack.json'
            # Terminal entries are excluded before reading credentials.
            try:
                ack = read_json(ack_path) if ack_path.exists() else {}
                require(isinstance(ack, dict), 'MALFORMED_ACK')
            except (OSError,ValueError,TypeError):
                ack = None
            if ack is not None and ack.get('phase') in ('RETIRED','PANIC_DONE'):
                continue
            bots.candidates.append(created)
            if inst in seen:
                bots.blocked.add(inst)
                bots.issues.append((inst,'DUPLICATE_NONTERMINAL_INSTRUMENT'))
            seen.add(inst)
            current = read_credentials(root/'.env')
            choices.append(current)
            require(ack is not None, 'MALFORMED_ACK')
            require(not (root/'state/final_stats.json').exists(), 'AMBIGUOUS_TERMINAL_STATE')
            service = entry.get('service')
            require(isinstance(service,str) and service == 'okx-grid-'+root.name and
                    re.fullmatch(r'okx-grid-[A-Za-z0-9_-]+', service) and
                    (Path(systemd_dir)/(service+'.service')).is_file(), 'INVALID_SERVICE')
            records.append((inst,created,current))
        except (OSError,ValueError,KeyError,TypeError,ArithmeticError) as exc:
            if inst is not None:
                bots.blocked.add(inst)
            reason = str(exc) if isinstance(exc,LedgerError) and re.fullmatch('[A-Z0-9_]+',str(exc)) else type(exc).__name__
            bots.issues.append((inst or ('registry:'+str(name)[:80]),reason))
    # Use a previously bound key when one exists; otherwise require consensus.
    eligible = [c for c in choices if expected is None or hashlib.sha256(c[KEYS[0]].encode()).hexdigest()==expected]
    if not eligible:
        require(not choices and not bots.issues, 'NO_TRUSTED_ACCOUNT_CREDENTIALS')
        return bots,None
    creds = eligible[0]
    require(all(c==creds for c in eligible), 'ACCOUNT_CREDENTIALS_AMBIGUOUS')
    for inst,created,current in records:
        if current != creds:
            bots.blocked.add(inst)
            bots.issues.append((inst,'ACCOUNT_CREDENTIALS_MISMATCH'))
        if inst not in bots.blocked:
            bots[inst]=created
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


def run_once(db_path, inventory_fn, client_factory, *, mode='update', now=None):
    real_clock = now is None
    now=int(time.time()*1000) if real_clock else now
    bots, creds=inventory_fn()
    if creds is None:
        require(not bots, 'NO_TRUSTED_ACCOUNT_CREDENTIALS')
        return {'status':'NO_NONTERMINAL_BOTS','requests':0}
    fingerprint=hashlib.sha256(creds[KEYS[0]].encode()).hexdigest()
    intervals, last, reconciled=read_state(db_path, fingerprint)
    plan, reconcile=plan_windows(bots,intervals,last,reconciled,now,mode)
    client=client_factory(creds)
    windows=[fetch_window(client,*p) for p in plan]
    bots_after, creds_after=inventory_fn()
    # A change of account is a global error. A change of inventory is local.
    require(creds_after==creds or creds_after is None,'ACCOUNT_CHANGED_DURING_FETCH')
    stable={i:v for i,v in bots_after.items() if i in bots and creation_ms(bots[i])==creation_ms(v)}
    blocked=set(getattr(bots_after,'blocked',()))
    issues=list(getattr(bots_after,'issues',()))
    for inst in set(bots_after)-set(stable):
        blocked.add(inst)
        issues.append((inst,'INVENTORY_CHANGED_DURING_FETCH'))
    synced=int(time.time()*1000) if real_clock else now
    result=apply_complete_windows(db_path,windows,stable,synced,
                                  reconciled_ms=synced if reconcile else None, account_fingerprint=fingerprint,
                                  blocked=blocked,issues=issues,reconcile_missing=True)
    return dict(result,status='PASS',requests=client.requests,windows=len(windows),reconcile=reconcile,inventory_issues=len(issues))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--base',type=Path,required=True)
    p.add_argument('--registry',type=Path,required=True)
    p.add_argument('--systemd-dir',type=Path,required=True)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--mode',choices=['backfill','update','reconcile'],default='update')
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
        result=run_once(a.data_dir/'funding.sqlite',lambda:inventory(a.base,a.registry,a.systemd_dir,binding(a.data_dir/'funding.sqlite')),BillsClient,mode=a.mode)
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
