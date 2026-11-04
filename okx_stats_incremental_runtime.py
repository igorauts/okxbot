"""Incremental *SQLite source* for StatsWorker, not a finance oracle.

The financial calculator, pinned order quantities, historic REST recovery,
24h rolling computation and ledger parity remain the original engine functions.
A durable checkpoint is observational only and NEVER authorizes skipped audits
on restart. A new process starts with FULL validation of its SQLite indexes.
"""
from __future__ import annotations
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import time
from okx_stats_sqlite_delta_v0751 import AppendCursor, Drift

class StatsSourceCache:
    def __init__(self, fills_db, cycles_db, instrument):
        self.instrument = str(instrument)
        if not self.instrument.endswith('-USDT-SWAP'):
            raise ValueError('Unknown stats instrument')
        self.fills = AppendCursor(fills_db, 'fills')
        self.cycles = AppendCursor(cycles_db, 'cycles')
        self.by_order = {}
        self.fills_initialised = False
        self.archived_initialised = False
        self.last_fills_mode = None
        self.last_cycles_mode = None
        self.recovery_calls = 0
        self.poisoned = False
        self.last_added_order_ids = set()
        self._archive_rows_list = None
        self._archive_max_rowid = 0

    @staticmethod
    def _number(value):
        number=Decimal(str(value))
        if not number.is_finite():
            raise Drift('Nonfinite cached quantity')
        return number

    def archive_rows(self):
        if self.poisoned: raise Drift('Stats cache poisoned; process restart and audit required')
        # Historical behavior when the archive has never been created.
        if not self.cycles.path.exists() and not self.archived_initialised:
            return []
        result=self.cycles.refresh()
        self.archived_initialised=True
        self.last_cycles_mode=result['mode']
        # All rows were individually JSON validated by the cursor. The source
        # is append-only; return a new list without re-reading/re-parsing SQL.
        # Durable append-only archive: keep the list object between
        # StatsWorker reports. The cursor itself verifies historical rows at
        # each scheduled FULL audit; no O(history) list rebuild on idle delta.
        if self._archive_rows_list is None:
            self._archive_rows_list=[v[1] for v in self.cycles.rows.values()]
            self._archive_max_rowid=result['cursor']
        else:
            if result['mode']=='delta':
                newly=result['added_rowids']
            else:
                newly=tuple(k for k in self.cycles.rows if k>self._archive_max_rowid)
            for rid in newly:
                if rid<=self._archive_max_rowid:
                    self.poisoned=True
                    raise Drift('Archive append cursor not increasing')
                self._archive_rows_list.append(self.cycles.rows[rid][1])
                self._archive_max_rowid=rid
            if len(self._archive_rows_list)!=len(self.cycles.rows):
                self.poisoned=True
                raise Drift('Archive cache rowcount invariant changed')
        return self._archive_rows_list

    def _refresh_fills(self):
        if self.poisoned: raise Drift('Stats cache poisoned; process restart and audit required')
        old=self.fills.cursor
        result=self.fills.refresh()
        if result['mode']=='full':
            # FULL audit of historical rows is performed by AppendCursor;
            # rebuild only the in-memory ordId mapping after that audit.
            mapped={}
            population=self.fills.rows.items()
        else:
            mapped={}  # validate only the O(delta) additions before cache mutation
            # The cursor already knows exact newly appended rowids; do not
            # iterate every historic fill merely to discover O(delta) rows.
            population=((rid,self.fills.rows[rid]) for rid in result['added_rowids'])
        try:
            for rowid,(_,row) in population:
                self._validate_into(mapped, rowid, row)
        except Exception:
            self.poisoned=True
            raise
        # Publish only after every new row passed validation.
        if result['mode']=='full':
            self.by_order=mapped
        else:
            for oid,values in mapped.items():
                self.by_order.setdefault(oid,[]).extend(values)
        # Old records are audited by AppendCursor. Any real newly
        # appended fill can invalidate a previously frozen finance group.
        self.last_added_order_ids.update(
            str(row.get('ordId') or '') for rid,(_,row) in self.fills.rows.items()
            if rid>old and row.get('ordId')) if result['mode']=='full' else self.last_added_order_ids.update(mapped)
        self.fills_initialised=True
        self.last_fills_mode=result['mode']
        return result

    def _validate_into(self, mapped, rowid, row):
        oid=str(row.get('ordId') or '')
        bill=str(row.get('billId') or '')
        if not oid or not bill or row.get('instId',self.instrument)!=self.instrument:
            raise Drift('Foreign or invalid cached fill')
        if (row.get('side') not in ('buy','sell') or row.get('execType') not in ('M','T')
            or row.get('feeCcy')!='USDT' or row.get('fee') in (None,'')
            or not str(row.get('tradeId') or '')):
            raise Drift('Unverified cached fill identity or fee')
        if self._number(row.get('fillPx'))<=0 or self._number(row.get('fillSz'))<=0:
            raise Drift('Invalid cached fill execution')
        self._number(row['fee'])
        mapped.setdefault(oid, []).append((rowid,row))

    def expected_rows(self, expected, fallback):
        """Return real immutable rows, never extrapolate or synthesize fills.

        On a missing pinned quantity, use the EXISTING FillStore exact-order /
        bounded bulk history recovery, not a new REST mechanism or assumption.
        """
        if not isinstance(expected,dict):
            raise TypeError('Expected order quantities must be a mapping')
        if not expected and not self.fills.path.exists():
            # Original FillStore returns [] without creating an empty ledger.
            return []
        self._refresh_fills()
        missing=[]
        selected=[]
        for oid,quantity in expected.items():
            oid=str(oid)
            target=self._number(quantity)
            if not oid or target<=0:
                raise Drift('Invalid pinned order quantity')
            observations=self.by_order.get(oid,())
            observed=sum((self._number(row['fillSz']) for _,row in observations),Decimal(0))
            if observed<target:
                missing.append(oid)
            selected.extend(observations)
        if missing:
            self.recovery_calls+=1
            # Use exactly the incumbent fallback. It may legitimately return
            # a pinned-straddling set, which original trim rejects as incomplete.
            recovered=fallback(expected)
            # Persisted newly-recovered fills are available for next attempt.
            self._refresh_fills()
            return recovered
        # SQLite rowid reflects append order, not necessarily FillStore's IN
        # query ordering. Never alter the authoritative trim by order quantity.
        selected.sort(key=lambda pair:pair[0])
        return [row for _,row in selected]

    def checkpoint_metadata(self, signature, last_stats_unix):
        """Do not include monetary aggregates or trust metadata on restart."""
        if not self.archived_initialised or not self.fills_initialised:
            # Fresh empty bot may not yet have a fills DB. Do not assert a
            # checkpoint source binding that has never been observed.
            return None
        def entry(cursor):
            return {'device':cursor.identity[0], 'inode':cursor.identity[1],
                    'rowid':cursor.cursor, 'row_count':len(cursor.rows),
                    'last_row_sha256':cursor.tail,
                    'full_audit_at_monotonic':cursor.last_full}
        return {'schema':1,'instrument':self.instrument,
                'published_stats_at':last_stats_unix,
                'signature_sha256':hashlib.sha256(json.dumps(
                    signature,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest(),
                'cycles':entry(self.cycles),'fills':entry(self.fills),
                'financial_authority':'FULL_AND_LEDGER_CHECKS_IN_ENGINE',
                'usable_for_restore':False}
