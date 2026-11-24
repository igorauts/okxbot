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

def apply_complete_windows(path, windows, inventory, synced_ms, *, reconciled_ms=None, account_fingerprint=None, blocked=(), issues=(), reconcile_missing=False):
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
                published.append((inst, created, canonical(total), last, synced_ms, int(complete)))
        # Preserve blocked instruments' last good snapshot (identity checked by reader).
        # Drop retired/removed identities, never modify any bot or archive file.
        for (inst,) in db.execute('SELECT inst_id FROM snapshot').fetchall():
            if inst not in blocked:
                db.execute('DELETE FROM snapshot WHERE inst_id=?',(inst,))
        db.executemany('INSERT INTO snapshot VALUES (?,?,?,?,?,?)', published)
        db.execute("INSERT INTO meta VALUES ('synced_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(synced_ms),))
        db.execute("INSERT INTO meta VALUES ('covered_until_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(covered_until),))
        if reconciled_ms is not None:
            db.execute("INSERT INTO meta VALUES ('reconciled_ms',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(reconciled_ms),))
    return {'inserted': inserted, 'duplicates': duplicates, 'published': len(published), 'blocked': len(blocked), 'quarantined': len(bad), 'missing': sum(bid not in observed for bid,_,_ in missing)}

def read_snapshot(path, inst, created_at):
    """Read one ready amount; caller must first confirm the bot is nonterminal."""
    inst, created = instrument(inst), creation_ms(created_at)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=0.1)) as db:
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA, 'UNSUPPORTED_SCHEMA')
        row = db.execute('SELECT amount,last_bill_ms,synced_ms,complete FROM snapshot WHERE inst_id=? AND created_ms=?', (inst,created)).fetchone()
    if row is None:
        return None
    return dict(zip(('amount','last_bill_ms','synced_ms','complete'), row))
