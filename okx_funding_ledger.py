"""Offline ledger core. No network, systemd, engine imports or bot writes."""
from contextlib import closing, contextmanager
from decimal import Decimal, localcontext, ROUND_CEILING
import json
import re
import sqlite3
from pathlib import Path

SCHEMA = 2
SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS bills (
 bill_id TEXT PRIMARY KEY, inst_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
 amount TEXT NOT NULL, subtype TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bills_inst_ts ON bills(inst_id, ts_ms);
CREATE TABLE IF NOT EXISTS coverage (
 begin_ms INTEGER PRIMARY KEY, end_ms INTEGER NOT NULL CHECK(end_ms > begin_ms)
);
CREATE TABLE IF NOT EXISTS snapshot (
 inst_id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL, amount TEXT NOT NULL,
 last_bill_ms INTEGER, synced_ms INTEGER NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1))
);
CREATE INDEX IF NOT EXISTS bills_ts ON bills(ts_ms);
CREATE TABLE IF NOT EXISTS quarantine (
 bill_id TEXT NOT NULL, inst_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
 reason TEXT NOT NULL, raw_json TEXT NOT NULL, PRIMARY KEY(bill_id,inst_id,reason,raw_json)
);
CREATE INDEX IF NOT EXISTS quarantine_inst_ts ON quarantine(inst_id,ts_ms);
CREATE TABLE IF NOT EXISTS discrepancy (
 bill_id TEXT PRIMARY KEY, inst_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
 first_seen_ms INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS discrepancy_inst_ts ON discrepancy(inst_id,ts_ms);
CREATE TABLE IF NOT EXISTS inventory_issue (
 subject TEXT NOT NULL, reason TEXT NOT NULL, synced_ms INTEGER NOT NULL,
 PRIMARY KEY(subject,reason)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''

class LedgerError(ValueError):
    pass

def require(test, message):
    if not test:
        raise LedgerError(message)

def integer_ms(value):
    require(type(value) is int and 0 <= value <= 253402300799999,
            'INVALID_TIMESTAMP')
    return value

def instrument(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', value) is not None,
            'INVALID_INSTRUMENT')
    return value

def money(value):
    # API amounts are decimal strings. Bound resource use and retain full source precision.
    require(isinstance(value, str) and len(value) <= 128 and
            re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', value) is not None, 'INVALID_AMOUNT')
    d = Decimal(value)
    require(d.is_finite(), 'INVALID_AMOUNT')
    return d

def canonical(d):
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return '0' if d == 0 else s

def creation_ms(value):
    # The config timestamp is seconds; integer bill timestamps at/after it are eligible.
    with localcontext() as ctx:
        ctx.prec = 80
        require(type(value) in (int, float, str, Decimal), 'INVALID_CREATED_AT')
        d = Decimal(str(value))
        require(d.is_finite() and d >= 1000000000, 'INVALID_CREATED_AT')
        return integer_ms(int((d * 1000).to_integral_value(rounding=ROUND_CEILING)))

def bill_envelope(row, begin, end):
    require(isinstance(row, dict), 'INVALID_BILL')
    bid = row.get('billId')
    require(isinstance(bid, str) and re.fullmatch(r'[0-9]{1,40}', bid) is not None,
            'INVALID_BILL_ID')
    inst = row.get('instId')
    require(isinstance(inst,str) and 0 < len(inst) <= 128, 'INVALID_BILL_INSTRUMENT')
    ts = row.get('ts')
    require(isinstance(ts, str) and re.fullmatch(r'[0-9]{1,15}', ts) is not None,
            'INVALID_BILL_TIMESTAMP')
    ts = integer_ms(int(ts))
    require(begin <= ts < end, 'BILL_OUTSIDE_WINDOW')
    return bid, inst, ts

def validated_bill(row, begin, end):
    bid, inst, ts = bill_envelope(row, begin, end)
    instrument(inst)
    require(row.get('type') == '8' and row.get('subType') in ('173', '174') and
            row.get('ccy') == 'USDT' and row.get('mgnMode') == 'cross', 'UNEXPECTED_BILL_CONTRACT')
    pnl = money(row.get('pnl'))
    change = money(row.get('balChg'))
    require(pnl == change, 'PNL_BALCHG_MISMATCH')
    sub = row['subType']
    require((sub == '173' and pnl <= 0) or (sub == '174' and pnl >= 0), 'BILL_SIGN_MISMATCH')
    return (bid, inst, ts, canonical(pnl), sub,
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))

@contextmanager
def writer(path):
    path = Path(path)
    require(not path.is_symlink(), 'DB_SYMLINK')
    with closing(sqlite3.connect(str(path), timeout=2, isolation_level=None)) as db:
        db.execute('PRAGMA busy_timeout=2000')
        require(db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal', 'WAL_UNAVAILABLE')
        db.execute('PRAGMA synchronous=FULL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        require(version in (0, SCHEMA), 'UNSUPPORTED_SCHEMA')
        if version == 0:
            require(not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                    'UNRECOGNIZED_DATABASE')
            db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA_SQL + f'\nPRAGMA user_version={SCHEMA};\nCOMMIT;')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise

def merged_coverage(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged

def fully_covered(intervals, start, end):
    return start >= end or any(a <= start and b >= end for a, b in intervals)

def apply_complete_windows(path, windows, inventory, synced_ms, *, reconciled_ms=None, account_fingerprint=None, blocked=(), issues=(), reconcile_missing=False, terminals=(), regular_sync=False, live_proofs=None, publish_live=True):
    """Commit fully fetched [begin,end) windows and publish one snapshot atomically.

    F3 caller MUST attest all pages of each window succeeded; partial page sets are
    not an input to this function. inventory is the validated nonterminal mapping
    instrument -> creation timestamp in seconds, not a list of running services.
    No writes to any bot directory. Last good snapshot stays on transaction error.
    """
    synced_ms = integer_ms(synced_ms)
    if reconciled_ms is not None:
        require(integer_ms(reconciled_ms) == synced_ms, "INVALID_RECONCILE_TIMESTAMP")
    require(isinstance(inventory, dict), 'INVALID_INVENTORY')
    bots = {instrument(inst): creation_ms(created) for inst, created in inventory.items()}
    require(all(t <= synced_ms for t in bots.values()), 'FUTURE_CREATED_AT')
    require(isinstance(windows, (tuple, list)) and len(windows) > 0, 'NO_COMPLETE_WINDOWS')
    normalized = []
    for begin, end, rows in windows:
        begin, end = integer_ms(begin), integer_ms(end)
        require(begin < end <= synced_ms, 'INVALID_WINDOW')
        require(isinstance(rows, (list, tuple)), 'INVALID_ROWS')
        # Envelope is a global pagination/identity contract; financial errors are local.
        normalized.append((begin, end, [(bill_envelope(r, begin, end), r) for r in rows]))
    inserted = duplicates = 0
    with writer(path) as db:
        if account_fingerprint is not None:
            require(isinstance(account_fingerprint, str) and re.fullmatch(r'[0-9a-f]{64}', account_fingerprint), 'INVALID_ACCOUNT_BINDING')
            bound = db.execute("SELECT value FROM meta WHERE key='account_fingerprint'").fetchone()
            if bound is None:
                require(db.execute('SELECT count(*) FROM bills').fetchone()[0] == 0 and
                        db.execute("SELECT value FROM meta WHERE key='synced_ms'").fetchone() is None,
                        'UNBOUND_EXISTING_HISTORY')
                db.execute("INSERT INTO meta VALUES ('account_fingerprint',?)", (account_fingerprint,))
            else:
                require(bound[0] == account_fingerprint, 'ACCOUNT_BINDING_MISMATCH')
        old = db.execute("SELECT value FROM meta WHERE key='synced_ms'").fetchone()
        require(old is None or synced_ms >= int(old[0]), 'STALE_GENERATION')
        intervals = db.execute('SELECT begin_ms,end_ms FROM coverage').fetchall()
        blocked = set(blocked)
        bad = {}
        valid = {}
        missing = set()
        observed = set()
        for begin, end, rows in normalized:
            intervals.append((begin, end))
            incoming = {env[0] for env, _ in rows}
            observed.update(incoming)
            if reconcile_missing:
                for bid, inst, ts in db.execute('SELECT bill_id,inst_id,ts_ms FROM bills WHERE ts_ms>=? AND ts_ms<?', (begin,end)):
                    if bid not in incoming:
                        missing.add((bid,inst,ts))
            for (bid, inst, ts), raw in rows:
                try:
                    r = validated_bill(raw, begin, end)
                except LedgerError as exc:
                    bad.setdefault(bid, []).append((bid,inst,ts,str(exc),json.dumps(raw,ensure_ascii=False,sort_keys=True,allow_nan=False)))
                    continue
                old_row = valid.get(bid)
                if old_row is not None and old_row[:5] != r[:5]:
                    for v in (old_row,r):
                        bad.setdefault(bid, []).append((bid,v[1],v[2],'CONFLICTING_BILL_ID',v[5]))
                elif old_row is not None:
                    duplicates += 1
                valid[bid] = r
        for bid,r in valid.items():
            existing = db.execute('SELECT bill_id,inst_id,ts_ms,amount,subtype,raw_json FROM bills WHERE bill_id=?', (bid,)).fetchone()
            for old_inst, old_ts, old_raw in db.execute('SELECT inst_id,ts_ms,raw_json FROM quarantine WHERE bill_id=?', (bid,)):
                if old_inst != r[1] or old_ts != r[2]:
                    bad.setdefault(bid, []).extend([
                        (bid,old_inst,old_ts,'CONFLICTING_BILL_ID',old_raw),
                        (bid,r[1],r[2],'CONFLICTING_BILL_ID',r[5])])
            if existing is not None and existing[:5] != r[:5]:
                for v in (existing,r):
                    bad.setdefault(bid, []).append((bid,v[1],v[2],'CONFLICTING_BILL_ID',v[5]))
            if bid in bad:
                # Preserve valid prior history and quarantine all implicated identities.
                bad[bid].append((bid,r[1],r[2],'CONFLICTING_OR_INVALID_BILL',r[5]))
                continue
            if existing is not None:
                duplicates += 1
            else:
                db.execute('INSERT INTO bills VALUES (?,?,?,?,?,?)', r)
                inserted += 1
            # Only a clean matching bill can clear its prior quarantine.
            # Cross-instrument conflicts must not clear another identity implicitly.
            db.execute("DELETE FROM quarantine WHERE bill_id=? AND inst_id=? AND reason != 'CONFLICTING_BILL_ID'", (bid,r[1]))
        for bid, entries in bad.items():
            existing = db.execute('SELECT inst_id,ts_ms,raw_json FROM bills WHERE bill_id=?',(bid,)).fetchone()
            if existing:
                entries.append((bid,existing[0],existing[1], 'CONFLICTING_BILL_ID' if any(v[1]!=existing[0] for v in entries) else 'CONFLICTING_OR_INVALID_BILL',existing[2]))
            db.executemany('INSERT OR IGNORE INTO quarantine VALUES (?,?,?,?,?)', entries)
        for bid,inst,ts in missing:
            if bid not in observed:
                db.execute('INSERT INTO discrepancy VALUES (?,?,?,?,?) ON CONFLICT(bill_id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms', (bid,inst,ts,synced_ms,synced_ms))
        for bid in observed - bad.keys():
            db.execute('DELETE FROM discrepancy WHERE bill_id=?',(bid,))
        db.execute('DELETE FROM inventory_issue')
        db.executemany('INSERT OR IGNORE INTO inventory_issue VALUES (?,?,?)', [(subject,reason,synced_ms) for subject,reason in issues])
        intervals = merged_coverage(intervals)
        db.execute('DELETE FROM coverage')
        db.executemany('INSERT INTO coverage VALUES (?,?)', intervals)
        covered_until = max(b for _, b in intervals)
        # Decimal exactness: allow both maximum integer and fractional widths
        # across different 128-character amounts, plus SQLite row count bounds.
        # No SQL SUM over text (which would coerce to floating point).
        published=[]
        if publish_live:
            with localcontext() as ctx:
                ctx.prec = 320
                published = []
                for inst, created in sorted(bots.items()):
                    if inst in blocked or db.execute('SELECT 1 FROM quarantine WHERE inst_id=? AND ts_ms>=? LIMIT 1',(inst,created)).fetchone():
                        blocked.add(inst)
                        continue
                    total = Decimal(0)
                    last = None
                    for amount, ts in db.execute('SELECT amount,ts_ms FROM bills WHERE inst_id=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms,bill_id', (inst, created, covered_until)):
                        total += money(amount)
                        last = ts
                    complete = fully_covered(intervals, created, covered_until) and not db.execute('SELECT 1 FROM discrepancy WHERE inst_id=? AND ts_ms>=? LIMIT 1',(inst,created)).fetchone()
                    # Track last complete history verification per identity without a schema migration.
                    # Never reset the clock merely because an incomplete snapshot is refreshed.
                    key = f'complete_at:{inst}:{created}'
                    if complete:
                        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,str(synced_ms)))
                    else:
                        old_complete = db.execute('SELECT synced_ms FROM snapshot WHERE inst_id=? AND created_ms=? AND complete=1',(inst,created)).fetchone()
                        if old_complete is not None:
                            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',(key,str(old_complete[0])))
                    published.append((inst, created, canonical(total), last, synced_ms, int(complete)))
            # Preserve blocked instruments' last good snapshot (identity checked by reader).
            # Drop retired/removed identities, never modify any bot or archive file.
            for (inst,) in db.execute('SELECT inst_id FROM snapshot').fetchall():
                if inst not in blocked:
                    db.execute('DELETE FROM snapshot WHERE inst_id=?',(inst,))
            db.executemany('INSERT INTO snapshot VALUES (?,?,?,?,?,?)', published)
            # Bind read-only fast-path proof to the exact published live snapshot.
            # It contains only file metadata, not the key or any credential digest.
            db.execute(LIVE_PROOF_SQL)
            for inst,created,amount,last,_,complete in published:
                proof=(live_proofs or {}).get(inst)
                if complete and proof is not None:
                    name,c,token=proof
                    require(account_fingerprint is not None and c==created and re.fullmatch(r'[A-Za-z0-9_-]+',name) and isinstance(token,str),'INVALID_LIVE_PROOF')
                    db.execute('INSERT INTO terminal_live_proof VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,inst_id,created_ms) DO UPDATE SET env_token=excluded.env_token,synced_ms=excluded.synced_ms,covered_until_ms=excluded.covered_until_ms,amount=excluded.amount,last_bill_ms=excluded.last_bill_ms',(name,inst,created,token,synced_ms,covered_until,amount,last))
            live_keys = {f'complete_at:{inst}:{created}' for inst,created in bots.items()}
            for (key,) in db.execute("SELECT key FROM meta WHERE key LIKE 'complete_at:%'").fetchall():
                if key not in live_keys:
                    db.execute('DELETE FROM meta WHERE key=?',(key,))
        # Publish bounded terminal lifetime records in the same committed generation.
        # Never prune these when a new instance claims the same instrument.
        terminal_published=publish_terminals(db,terminals,intervals,synced_ms,account_fingerprint)
        db.execute("INSERT INTO meta VALUES ('synced_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(synced_ms),))
        db.execute("INSERT INTO meta VALUES ('covered_until_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(covered_until),))
        if not publish_live and old is not None:
            db.execute("INSERT OR IGNORE INTO meta VALUES ('regular_synced_ms',?)",(old[0],))
        if regular_sync:
            db.execute("INSERT INTO meta VALUES ('regular_synced_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(synced_ms),))
        if reconciled_ms is not None:
            db.execute("INSERT INTO meta VALUES ('reconciled_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(reconciled_ms),))
    return {'inserted': inserted, 'duplicates': duplicates, 'published': len(published), 'terminal_published': terminal_published, 'blocked': len(blocked), 'quarantined': len(bad), 'missing': sum(bid not in observed for bid,_,_ in missing)}

def read_snapshot(path, inst, created_at):
    """Read one ready amount; caller must first confirm the bot is nonterminal."""
    inst, created = instrument(inst), creation_ms(created_at)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=0.1)) as db:
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA, 'UNSUPPORTED_SCHEMA')
        # Same read transaction pins the snapshot and completeness watermark.
        db.execute('BEGIN')
        row = db.execute('SELECT amount,last_bill_ms,synced_ms,complete FROM snapshot WHERE inst_id=? AND created_ms=?', (inst,created)).fetchone()
        mark = db.execute('SELECT value FROM meta WHERE key=?',(f'complete_at:{inst}:{created}',)).fetchone()
    if row is None:
        return None
    result = dict(zip(('amount','last_bill_ms','synced_ms','complete'), row))
    result['last_complete_ms'] = row[2] if row[3] else (int(mark[0]) if mark is not None else None)
    return result

# Terminal funding is separate from mutable instrument-keyed live snapshots.
# Additive table within schema 2: old readers/backup tooling remain compatible.
TERMINAL_SQL = '''CREATE TABLE IF NOT EXISTS terminal_funding (
 bot_id TEXT NOT NULL, inst_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL, state_sha TEXT NOT NULL, mode TEXT NOT NULL,
 amount TEXT NOT NULL, last_bill_ms INTEGER, synced_ms INTEGER NOT NULL,
 complete INTEGER NOT NULL CHECK(complete IN (0,1)),
 PRIMARY KEY(bot_id,inst_id,created_ms,end_ms,state_sha)
)'''


def terminal_identity(root):
    """Read immutable engine evidence; never infer terminal from systemd alone."""
    import hashlib
    root = Path(root)
    def raw(name):
        p = root/name
        require(p.is_file() and not p.is_symlink() and p.resolve()==root.resolve()/name,
                'TERMINAL_FILE_UNAVAILABLE')
        return p.read_bytes()
    cfg = json.loads(raw('config.json'), parse_float=Decimal)
    state_raw = raw('state/bot_state.json')
    state = json.loads(state_raw, parse_float=Decimal)
    ack = json.loads(raw('state/control_ack.json'))
    final = json.loads(raw('state/final_stats.json'))
    inst, created = instrument(cfg['instrument']), creation_ms(cfg['created_at'])
    mode = state['terminal_mode']
    snap = state['terminal_snapshot']
    sha = hashlib.sha256(state_raw).hexdigest()
    require(mode in ('PANIC_DONE','RETIRED') and snap['mode']==mode and ack['phase']==mode
            and ack['mode']==('PANIC' if mode=='PANIC_DONE' else 'HARD'),
            'TERMINAL_MODE_MISMATCH')
    require(final['schema_version']==1 and final['complete'] is True and final['instrument']==inst
            and final['mode']==mode and final['terminal_state_sha256']==sha,
            'TERMINAL_ACCOUNTING_MISMATCH')
    end = creation_ms(snap['at_unix'])
    require(created < end, 'INVALID_TERMINAL_WINDOW')
    position = Decimal(str(snap['position_contracts']))
    require(position.is_finite() and position>=0 and
            position==Decimal(str(final['values']['position_contracts'])) and
            position==Decimal(str(ack['remaining_position'])) and
            (mode!='PANIC_DONE' or position==0), 'TERMINAL_POSITION_MISMATCH')
    require(re.fullmatch(r'[A-Za-z0-9_-]+',root.name) is not None,'INVALID_BOT_ID')
    return (root.name,inst,created,end,sha,mode), position


HOUR_MS = 3600000
SETTLEMENT_MS = 60000


def terminal_horizon(identity):
    """Exclusive coverage required for last possible hourly settlement.

    The exact PANIC boundary belongs to the previous hour if it is exactly :00.
    A bill posted during the settlement minute may follow the terminal state.
    Do not use this to allocate a bill across two bot identities.
    """
    _,_,created,end,_,_=identity
    return max(created, ((end-1)//HOUR_MS)*HOUR_MS+SETTLEMENT_MS)


def terminal_amount(db, identity, intervals, synced_ms):
    """Signed, covered lifetime; PANIC's isolated settlement tail only.

    Caller must establish account and non-overlapping settlement ownership.
    HARD never claims bills posted after its terminal boundary.
    """
    _,inst,created,end,_,mode=identity
    horizon=terminal_horizon(identity)
    stop=max(end,horizon) if mode=='PANIC_DONE' else end
    with localcontext() as ctx:
        ctx.prec=320
        total=Decimal(0)
        last=None
        for amount,ts in db.execute('SELECT amount,ts_ms FROM bills WHERE inst_id=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms,bill_id',(inst,created,stop)):
            total+=money(amount)
            last=ts
    complete=synced_ms>=horizon and fully_covered(intervals,created,horizon) and not any(
        db.execute('SELECT 1 FROM '+table+' WHERE inst_id=? AND ts_ms>=? AND ts_ms<? LIMIT 1',(inst,created,max(stop,horizon))).fetchone()
        for table in ('quarantine','discrepancy'))
    # A HARD position can continue accruing while settlement is still running;
    # its post-terminal bill cannot be safely split by the account timestamp.
    if mode=='RETIRED' and horizon>end and db.execute('SELECT 1 FROM bills WHERE inst_id=? AND ts_ms>=? AND ts_ms<? LIMIT 1',(inst,end,horizon)).fetchone():
        complete=False
    return (canonical(total),last,synced_ms,int(complete))


PERMANENT_REASONS=frozenset(('OVERLAPPING_INSTRUMENT_LIFETIMES','TERMINAL_PROOF_CONTRADICTION'))


def _attempt_reason(db,identity):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_attempt'").fetchone():
        return None
    row=db.execute('SELECT reason FROM terminal_attempt WHERE bot_id=? AND inst_id=? AND created_ms=? AND end_ms=? AND state_sha=? AND mode=?',identity).fetchone()
    return row[0] if row else None


def _record_attempt(db,identity,reason,now):
    old=_attempt_reason(db,identity)
    if old==reason or old in PERMANENT_REASONS:
        return
    db.execute('INSERT INTO terminal_attempt VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,inst_id,created_ms,end_ms,state_sha,mode) DO UPDATE SET reason=excluded.reason,at_ms=excluded.at_ms',identity+(reason,now))


def _lifetime_overlap(a,b):
    return a!=b and a[1]==b[1] and a[2]<max(b[3],terminal_horizon(b)) and b[2]<max(a[3],terminal_horizon(a))


def publish_terminals(db, identities, intervals, synced_ms, account_fingerprint):
    db.execute(TERMINAL_SQL)
    db.execute(TERMINAL_ATTEMPT_SQL)
    published=0
    identities=list(identities)
    existing=db.execute('SELECT bot_id,inst_id,created_ms,end_ms,state_sha,mode FROM terminal_funding').fetchall()
    for identity in identities:
        bot_id, inst, created, end, sha, mode = identity
        require(account_fingerprint is not None and instrument(inst)==inst
                and integer_ms(created)<integer_ms(end)<=synced_ms
                and re.fullmatch(r'[A-Za-z0-9_-]+',bot_id)
                and re.fullmatch(r'[0-9a-f]{64}',sha)
                and mode in ('PANIC_DONE','RETIRED'),'INVALID_TERMINAL_IDENTITY')
        if any(_lifetime_overlap(identity,other) for other in identities+existing):
            _record_attempt(db,identity,'OVERLAPPING_INSTRUMENT_LIFETIMES',synced_ms)
            for other in existing:
                if _lifetime_overlap(identity,other):
                    _record_attempt(db,other,'OVERLAPPING_INSTRUMENT_LIFETIMES',synced_ms)
            continue
        if _attempt_reason(db,identity) in PERMANENT_REASONS:
            continue
        old=db.execute('SELECT complete FROM terminal_funding WHERE bot_id=? AND inst_id=? AND created_ms=? AND end_ms=? AND state_sha=? AND mode=?',identity).fetchone()
        if old and old[0]==1:
            continue
        value=terminal_amount(db,identity,intervals,synced_ms)
        proof=live_proof(db,identity)
        horizon=terminal_horizon(identity)
        # If the already durable hourly proof could have been used for a fast
        # financial report, never silently freeze a different result.
        if mode=='PANIC_DONE' and horizon<=end and proof and horizon<=proof[2]<=proof[1]<=end and value[3] and value[:2]!=proof[3:5]:
            _record_attempt(db,identity,'TERMINAL_PROOF_CONTRADICTION',synced_ms)
            continue
        db.execute('INSERT INTO terminal_funding VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,inst_id,created_ms,end_ms,state_sha) DO UPDATE SET amount=excluded.amount,last_bill_ms=excluded.last_bill_ms,synced_ms=excluded.synced_ms,complete=excluded.complete WHERE terminal_funding.complete=0',identity+value)
        if value[3]:
            db.execute('DELETE FROM terminal_attempt WHERE bot_id=? AND inst_id=? AND created_ms=? AND end_ms=? AND state_sha=? AND mode=?',identity)
        published+=value[3]
    return published


TERMINAL_ATTEMPT_SQL = '''CREATE TABLE IF NOT EXISTS terminal_attempt (
 bot_id TEXT NOT NULL, inst_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL, state_sha TEXT NOT NULL, mode TEXT NOT NULL,
 reason TEXT NOT NULL, at_ms INTEGER NOT NULL,
 PRIMARY KEY(bot_id,inst_id,created_ms,end_ms,state_sha,mode)
)'''


def _bound(db, fingerprint):
    row=db.execute("SELECT value FROM meta WHERE key='account_fingerprint'").fetchone()
    require(row is not None and row[0]==fingerprint,'ACCOUNT_BINDING_MISMATCH')


def record_terminal_attempts(path, identities, fingerprint, reason, now):
    if not identities or not Path(path).is_file():
        return
    with writer(path) as db:
        _bound(db,fingerprint)
        db.execute(TERMINAL_ATTEMPT_SQL)
        for identity in identities:
            _record_attempt(db,identity,reason,now)


def finalize_covered_terminals(path, identities, fingerprint, now):
    """No API, no live snapshot mutation, no sync watermark advancement."""
    if not identities or not Path(path).is_file():
        return 0
    with writer(path) as db:
        _bound(db,fingerprint)
        intervals=merged_coverage(db.execute('SELECT begin_ms,end_ms FROM coverage').fetchall())
        ready=[i for i in identities if terminal_amount(db,i,intervals,now)[3]]
        if not ready:
            return 0
        return publish_terminals(db,ready,intervals,now,fingerprint)


LIVE_PROOF_SQL = '''CREATE TABLE IF NOT EXISTS terminal_live_proof (
 bot_id TEXT NOT NULL, inst_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
 env_token TEXT NOT NULL, synced_ms INTEGER NOT NULL,
 covered_until_ms INTEGER NOT NULL, amount TEXT NOT NULL, last_bill_ms INTEGER,
 PRIMARY KEY(bot_id,inst_id,created_ms)
)'''


def terminal_env_token(root):
    """Stat-only identity of the credentials file; never read the secret."""
    import stat
    p=Path(root)/'.env'
    st=p.lstat()
    require(stat.S_ISREG(st.st_mode) and p.resolve()==Path(root).resolve()/'.env','TERMINAL_CREDENTIALS_UNAVAILABLE')
    return ':'.join(str(x) for x in (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))


def live_proof(db,identity):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_live_proof'").fetchone():
        return None
    return db.execute('SELECT env_token,synced_ms,covered_until_ms,amount,last_bill_ms FROM terminal_live_proof WHERE bot_id=? AND inst_id=? AND created_ms=?',identity[:3]).fetchone()


def terminal_registry_conflict(root, identity):
    """Read-only guard for direct publication from the previous live snapshot."""
    root=Path(root).resolve();base=root.parent
    reg=base/'.okxbot_registry.json'
    require(reg.is_file() and not reg.is_symlink(),'TERMINAL_REGISTRY_UNAVAILABLE')
    entries=json.loads(reg.read_text(encoding='utf-8'))
    require(isinstance(entries,dict) and root.name in entries and
            Path(entries[root.name]['path']).resolve()==root,'TERMINAL_REGISTRY_MISMATCH')
    _,inst,created,end,_,_=identity
    stop=max(end,terminal_horizon(identity))
    for name,entry in entries.items():
        if name==root.name:
            continue
        try:
            other=Path(entry['path']).resolve()
            if other==base or base not in other.parents:
                continue
            cfg=json.loads((other/'config.json').read_text(encoding='utf-8'),parse_float=Decimal)
            if cfg['instrument']!=inst:
                continue
            c=creation_ms(cfg['created_at'])
        except (OSError,ValueError,TypeError,KeyError,ArithmeticError):
            # Unknown unrelated archive; collector isolates it too.
            continue
        if c>=stop:
            continue
        try:
            previous,_=terminal_identity(other)
        except (OSError,ValueError,TypeError,KeyError,ArithmeticError):
            return True
        if created<max(previous[3],terminal_horizon(previous)):
            return True
    return False


def read_terminal_local(path,identity,env_token):
    """Fast read of an already covered lifetime from an exact live snapshot.

    No writes. Only outside the settlement minute, and only with a matching
    previously verified live identity. The proof (amount, coverage and identity)
    is itself durable and independent of mutable live snapshots. The collector
    freezes the terminal record, rejecting any contradiction with this proof.
    """
    path=Path(path)
    require(path.is_file() and not path.is_symlink(),'DB_UNAVAILABLE')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.1)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        require(db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA,'UNSUPPORTED_SCHEMA')
        # A previous successful Collector generation must be account-bound.
        require(db.execute("SELECT value FROM meta WHERE key='account_fingerprint'").fetchone() is not None,'ACCOUNT_BINDING_UNAVAILABLE')
        proof=live_proof(db,identity)
        if not proof or proof[0]!=env_token:
            return None
        _,inst,created,end,_,mode=identity
        horizon=terminal_horizon(identity)
        if horizon>end or mode!='PANIC_DONE':
            return None
        if not (horizon<=proof[2]<=proof[1]<=end) or _attempt_reason(db,identity) in PERMANENT_REASONS:
            return None
        if db.execute('SELECT 1 FROM inventory_issue WHERE subject=? OR subject=? LIMIT 1',(inst,'terminal:'+identity[0])).fetchone():
            return None
        intervals=merged_coverage(db.execute('SELECT begin_ms,end_ms FROM coverage').fetchall())
        row=terminal_amount(db,identity,intervals,proof[1])
        if row[3] and row[:2]==proof[3:5]:
            return row
        return None


def terminal_failure(path,identity,fingerprint=None,*,env_token=None):
    path=Path(path)
    require(path.is_file() and not path.is_symlink(),'DB_UNAVAILABLE')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.1)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        require(db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA,'UNSUPPORTED_SCHEMA')
        if fingerprint is not None:
            _bound(db,fingerprint)
        reason=_attempt_reason(db,identity)
        if reason:return reason
        proof=live_proof(db,identity)
        if env_token is not None and proof and proof[0]!=env_token:
            return 'ACCOUNT_CREDENTIALS_NOT_ATTESTED'
        return None


def read_terminal_snapshot_status(path, identity, *, audit=True):
    """Read a frozen terminal result and expose any later ledger contradiction."""
    path=Path(path)
    require(path.is_file() and not path.is_symlink(),'DB_UNAVAILABLE')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        require(db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA,'UNSUPPORTED_SCHEMA')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_funding'").fetchone():
            return None,False
        row=db.execute('SELECT amount,last_bill_ms,synced_ms,complete FROM terminal_funding WHERE bot_id=? AND inst_id=? AND created_ms=? AND end_ms=? AND state_sha=? AND mode=?',identity).fetchone()
        conflict=False
        if audit and row and row[3]==1:
            intervals=merged_coverage(db.execute('SELECT begin_ms,end_ms FROM coverage').fetchall())
            current=terminal_amount(db,identity,intervals,row[2])
            conflict=(current[:2]!=row[:2] or current[3]!=1 or _attempt_reason(db,identity) in PERMANENT_REASONS)
        return row,conflict


def read_terminal_snapshot(path, identity):
    """For scheduler: a finalized record is never a retry candidate."""
    return read_terminal_snapshot_status(path,identity,audit=False)[0]
