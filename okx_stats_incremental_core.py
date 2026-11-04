#!/usr/bin/env python3
"""Offline-only incremental SQLite fills index for v0.7.51 finance parity.

Use alongside the existing FULL authoritative calculation; never publish
financial totals from this module until exact cross-check passes.
All SQL queries use mode=ro/query_only; no network calls or trading changes.
"""
import hashlib
import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

class Drift(Exception):
    """Inconsistent snapshot: rebuild from source, never publish this index."""

class Incomplete(Exception):
    """Expected fills incomplete; use existing bounded recovery path."""

class IncrementalFills:
    def __init__(self, path, instrument):
        self.path = Path(path)
        self.instrument = str(instrument)
        if not self.instrument or not self.instrument.endswith('-USDT-SWAP'):
            raise ValueError('Invalid instrument')
        self.identity = None
        self.max_rowid = 0
        self.count = 0
        self.rows = {}
        self.by_order = {}
        self.last_new = 0
        self.tail_digest = None

    @staticmethod
    def _num(x):
        d = Decimal(str(x))
        if not d.is_finite():
            raise Drift('Nonfinite fill number')
        return d

    def _validate(self, item):
        if not isinstance(item, dict):
            raise Drift('Invalid fill JSON')
        oid = str(item.get('ordId') or '')
        bill = str(item.get('billId') or '')
        if (not oid or not bill or not str(item.get('tradeId') or '') or
            item.get('instId', self.instrument) != self.instrument or
            item.get('side') not in ('buy','sell') or
            item.get('execType') not in ('M','T') or
            item.get('feeCcy') != 'USDT' or item.get('fee') in (None, '')):
            raise Drift('Invalid/foreign fill')
        if self._num(item['fillPx']) <= 0 or self._num(item['fillSz']) <= 0:
            raise Drift('Invalid fill price/quantity')
        self._num(item['fee'])
        return oid,bill

    def _load(self, pairs, reset=False):
        old = {} if reset else dict(self.rows)
        orders = {} if reset else {k:dict(v) for k,v in self.by_order.items()}
        cursor = 0 if reset else self.max_rowid
        for rowid,raw in pairs:
            if type(rowid) is not int or rowid <= cursor:
                raise Drift('Unordered/reused SQLite ROWID')
            row = json.loads(raw)
            ident = self._validate(row)
            if ident in old:
                raise Drift('Duplicate fill identity')
            old[ident] = row
            by = orders.setdefault(ident[0],{})
            by[ident[1]]=row
            cursor = rowid
        return old,orders,cursor

    def refresh(self, force_full=False):
        if not self.path.is_file() or self.path.is_symlink():
            raise Drift('Missing or symlink fills database')
        st = self.path.stat()
        fingerprint = (st.st_dev,st.st_ino)
        # Shared writer is append-only and immutable by existing FillStore
        # contract. Detect DB replacement, truncation and rowid reuse.
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',
                             uri=True,timeout=4) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            schema = db.execute('PRAGMA table_info(fills)').fetchall()
            columns = [str(x[1]) for x in schema]
            if columns != ['order_id','bill_id','qty','data']:
                raise Drift('Unexpected fills schema')
            count,highest = db.execute('SELECT count(*),coalesce(max(rowid),0) FROM fills').fetchone()
            full = bool(force_full or self.identity != fingerprint or self.count == 0)
            if not full and (count < self.count or highest < self.max_rowid):
                raise Drift('SQLite history shrank: require FULL rebuild')
            if not full and self.max_rowid:
                tail = db.execute('SELECT data FROM fills WHERE rowid=?',
                                  (self.max_rowid,)).fetchone()
                if (not tail or hashlib.sha256(tail[0].encode('utf-8')).hexdigest()
                        != self.tail_digest):
                    raise Drift('Prior append-tail changed: require FULL rebuild')
            query = ('SELECT rowid,data FROM fills ORDER BY rowid' if full
                     else 'SELECT rowid,data FROM fills WHERE rowid>? ORDER BY rowid')
            records = db.execute(query,() if full else (self.max_rowid,)).fetchall()
            if (len(records) != count if full else self.count+len(records) != count):
                raise Drift('SQLite history changed below cursor')
            raw,by,cursor = self._load(records,reset=full)
            if cursor != highest:
                raise Drift('SQLite cursor is inconsistent')
            tail_now = db.execute('SELECT data FROM fills WHERE rowid=?',
                                  (cursor,)).fetchone() if cursor else None
            new_tail_digest = (hashlib.sha256(tail_now[0].encode('utf-8')).hexdigest()
                               if tail_now else None)
        self.rows=raw
        self.by_order=by
        self.identity=fingerprint
        self.count=count
        self.max_rowid=cursor
        self.last_new=len(records)
        self.tail_digest=new_tail_digest
        return {'mode':'full' if full else 'delta', 'new':len(records),
                'count':count,'max_rowid':cursor}

    def matching_rows(self, expected):
        """Return rows if source counts cover pinned quantities; no mutations.

        Overfills are intentionally returned for existing
        trim_verified_fills_for_snapshot to handle on its FULL path.
        """
        if not isinstance(expected,dict):
            raise TypeError('Expected mapping required')
        result=[]
        for oid,qty in expected.items():
            oid=str(oid)
            target=self._num(qty)
            if target<=0:raise Drift('Invalid expected quantity')
            bucket=list(self.by_order.get(oid,{}).values())
            found=sum((self._num(x['fillSz']) for x in bucket),Decimal(0))
            if found < target:
                raise Incomplete('Missing verified fills for pinned order '+oid)
            # Ensure callers never mutate cached rows.
            result.extend(dict(x) for x in bucket)
        return result

    def diagnostic(self):
        return {'schema':1,'instrument':self.instrument,
                'row_count':self.count,'max_rowid':self.max_rowid,
                'cache_mode':'memory_verified_not_financial_authority'}
