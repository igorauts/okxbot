"""Append-only in-memory SQLite cursors, offline candidate, NOT financial authority.

No LIVE caller until checkpoint lifecycle and FULL parity are implemented.
Only reads existing SQLite DBs (mode=ro/query_only) and never queries OKX.
It depends on verified insert-only FillStore/archive writer contracts. Detects
count/tail drift immediately and checks all rows every FULL_AUDIT_SEC; never
pretends that rowid/count prove arbitrary older rows immutable between audits.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import sqlite3
import time
from pathlib import Path

class Drift(RuntimeError): pass
FULL_AUDIT_SEC = 1800
SPECS = {
    'fills': ('fills', ('order_id','bill_id','qty','data')),
    'cycles': ('cycles', ('id','data')),
}

def sha(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

class AppendCursor:
    def __init__(self, path, kind, clock=time.monotonic):
        if kind not in SPECS: raise ValueError('Unknown ledger kind')
        self.path=Path(path)
        self.kind=kind
        self.clock=clock
        self.identity=None
        self.cursor=0
        self.rows={}
        self.keys={}
        self.tail=None
        self.last_full=None
        self.full_scans=0
        self.delta_scans=0

    def _check(self, row):
        if not isinstance(row, dict): raise Drift('Malformed JSON row')
        if self.kind=='fills':
            a=str(row.get('ordId') or '')
            b=str(row.get('billId') or '')
            if not a or not b: raise Drift('Missing fill identity')
            return (a,b)
        ident=str(row.get('cycleId') or '')
        if not ident: raise Drift('Missing cycle identity')
        return ident

    def refresh(self, force_full=False):
        """Append-only O(delta) hot path; periodic FULL validates immutable history.

        This index is NOT financial authority. A non-tail historical UPDATE or
        DELETE is detectable by periodic FULL, not between FULL audits. Caller
        must use verified fill recovery, source-pinned financial snapshot and
        mandatory FULL reconciliation before trusting durable checkpoints.
        """
        if not self.path.is_file() or self.path.is_symlink():
            raise Drift('Missing/symlink ledger file')
        st=self.path.stat()
        identity=(st.st_dev,st.st_ino)
        if self.identity is not None and self.identity!=identity:
            raise Drift('SQLite database identity changed: invalidate checkpoint')
        table,columns=SPECS[self.kind]
        now=self.clock()
        full=(force_full or self.last_full is None or
              now-self.last_full>=FULL_AUDIT_SEC)
        uri=self.path.resolve().as_uri()+'?mode=ro'
        with contextlib.closing(sqlite3.connect(uri,uri=True,timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            observed=tuple(r[1] for r in db.execute('PRAGMA table_info('+table+')'))
            if observed!=columns: raise Drift('Unexpected table schema')
            # Indexed MAX(rowid) and indexed tail lookup; no COUNT(*) or
            # copying entire cache on the normal append-only hot path.
            highest=db.execute('SELECT coalesce(max(rowid),0) FROM '+table).fetchone()[0]
            if highest < self.cursor: raise Drift('SQLite rowid regressed')
            if self.cursor:
                previous=db.execute('SELECT data FROM '+table+' WHERE rowid=?',
                                    (self.cursor,)).fetchone()
                if previous is None or sha(previous[0])!=self.tail:
                    raise Drift('Previous SQLite tail changed')
            if full:
                fetched=list(db.execute('SELECT rowid,data FROM '+table+' ORDER BY rowid'))
                newrows={};newkeys={};last=0
                for rowid,raw in fetched:
                    if not isinstance(rowid,int) or rowid<=last:
                        raise Drift('Invalid full rowid order')
                    try:value=json.loads(raw)
                    except (TypeError,ValueError) as exc:
                        raise Drift('Malformed SQLite data') from exc
                    key=self._check(value)
                    if key in newkeys:raise Drift('Duplicate ledger identity')
                    newrows[rowid]=(raw,value)
                    newkeys[key]=rowid
                    last=rowid
                if last!=highest:raise Drift('Full snapshot max-rowid mismatch')
                # FULL audit detects edits and deletions of previously trusted
                # immutable rows, even when size and max-rowid are unchanged.
                if self.identity is not None:
                    if len(newrows)<len(self.rows):
                        raise Drift('Historical SQLite rows removed')
                    for oldid,prior in self.rows.items():
                        if newrows.get(oldid)!=prior:
                            raise Drift('Previously trusted immutable row changed')
                new_tail=sha(newrows[last][0]) if last else None
            else:
                fetched=list(db.execute('SELECT rowid,data FROM '+table+
                                        ' WHERE rowid>? ORDER BY rowid',
                                        (self.cursor,)))
                additions={};addkeys={};last=self.cursor
                for rowid,raw in fetched:
                    if not isinstance(rowid,int) or rowid<=last:
                        raise Drift('Invalid append rowid order')
                    try:value=json.loads(raw)
                    except (TypeError,ValueError) as exc:
                        raise Drift('Malformed SQLite data') from exc
                    key=self._check(value)
                    if key in self.keys or key in addkeys:
                        raise Drift('Duplicate appended ledger identity')
                    additions[rowid]=(raw,value)
                    addkeys[key]=rowid
                    last=rowid
                if last!=highest:raise Drift('Append snapshot max-rowid mismatch')
                new_tail=sha(additions[last][0]) if additions else self.tail
        # No cache field is modified until the pinned SQLite read succeeds.
        if full:
            self.rows=newrows;self.keys=newkeys
            self.last_full=now;self.full_scans+=1
        else:
            # No O(N) copy and no mutation when there are zero new rows.
            self.rows.update(additions)
            self.keys.update(addkeys)
            self.delta_scans+=1
        self.identity=identity
        self.cursor=last
        self.tail=new_tail
        return {'mode':'full' if full else 'delta','rows':len(self.rows),
                'added':len(fetched),'added_rowids':tuple(rid for rid,_ in fetched),'cursor':last}


    def snapshot(self):
        # This returns a view only for OFFLINE comparisons; production must
        # pass a stable SQL transaction and an authoritative state pinning.
        return [value for _, value in self.rows.values()]
