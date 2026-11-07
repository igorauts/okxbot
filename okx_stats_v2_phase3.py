"""StatsWorker v2, phase 3 OFFLINE transactional staging/freeze prototype.

Never import in trading engine or run against LIVE while still a prototype.
Only reads the three *source* SQLite files; all writes are to its own sidecar.
No requests to OKX. No financial report publication. No trading operations.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from contextlib import contextmanager, ExitStack
from pathlib import Path
import sqlite3
import uuid

from okx_stats_v2_contract import (
    Drift, NotReady, TableSnapshot, SCHEMAS, stable_state, digest, json_compact,
    component_orders, pnl_identity,
)
from okx_stats_v2_money import (
    FrozenTotals, verified_frozen_chunk,
)

SOURCES = {'cycles': 'cycles.sqlite', 'accounted': 'pnl.sqlite', 'fills': 'fills.sqlite'}
SIDECAR_SCHEMA = 5
DEFAULT_COMPONENT_CAP = 512


def state_cycles(state):
    """Trading state is keyed by GRID PRICE, NOT cycleId. Map by cycle identity."""
    result = {}
    for cycle in list(state.get('cycles', {}).values()) + list(state.get('archived_cycles', [])):
        if not isinstance(cycle, dict):
            raise Drift('Malformed state cycle')
        cid = str(cycle.get('cycleId') or '')
        if not cid:
            raise Drift('State cycle without cycleId')
        if cid in result and result[cid] != cycle:
            raise Drift('Conflicting state cycle identity')
        result[cid] = cycle
    return result


def _metarow(db, key, default=None):
    row = db.execute('SELECT val FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def _setmeta(db, key, value):
    db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val',
               (key, str(value)))


class DurableStager:
    """Single SQLite transactional checkpoint, no JSON/SQLite split-brain.

    Each ingest commits ALL cursors+pending rows+prior-state membership in ONE
    sidecar transaction, or none of them. Each freeze commits component membership
    and money in ONE transaction. Crashes cannot advance one without the other.
    """
    def __init__(self, path: Path, instrument: str):
        self.path = Path(path)
        if (not isinstance(instrument, str) or not instrument.endswith('-USDT-SWAP')
            or len(instrument) > 120):
            raise Drift('Invalid instrument')
        self.instrument = instrument
        if self.path.is_symlink():
            raise Drift('Symlink sidecar refused')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, val TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors(kind TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending_cycles(
                cycle_id TEXT PRIMARY KEY, rowid_source INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending_orders(
                order_id TEXT NOT NULL, cycle_id TEXT NOT NULL,
                PRIMARY KEY(order_id, cycle_id));
            CREATE INDEX IF NOT EXISTS ix_pending_orders_cycle ON pending_orders(cycle_id);
            CREATE TABLE IF NOT EXISTS pending_targets(
                src_id TEXT NOT NULL, target_id TEXT NOT NULL,
                PRIMARY KEY(src_id, target_id));
            CREATE INDEX IF NOT EXISTS ix_pending_targets_target ON pending_targets(target_id);
            CREATE TABLE IF NOT EXISTS last_state_ids(cycle_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS frozen_components(
                component_id TEXT PRIMARY KEY, evidence_sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS frozen_orders(
                order_id TEXT PRIMARY KEY, component_id TEXT NOT NULL,
                FOREIGN KEY(component_id) REFERENCES frozen_components(component_id));
            CREATE TABLE IF NOT EXISTS frozen_cycles(
                cycle_id TEXT PRIMARY KEY, component_id TEXT NOT NULL,
                FOREIGN KEY(component_id) REFERENCES frozen_components(component_id));
            CREATE TABLE IF NOT EXISTS totals(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recent_events(
                component_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
                close_ms INTEGER NOT NULL, net TEXT NOT NULL,
                PRIMARY KEY(component_id, event_seq),
                FOREIGN KEY(component_id) REFERENCES frozen_components(component_id));
            CREATE INDEX IF NOT EXISTS ix_recent_events_time ON recent_events(close_ms);
            CREATE TABLE IF NOT EXISTS rolling_24h(
                id INTEGER PRIMARY KEY CHECK(id=1),
                as_of_ms INTEGER NOT NULL, total TEXT NOT NULL);
            ''')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior = _metarow(self.db, 'schema')
                if prior is None:
                    _setmeta(self.db, 'schema', SIDECAR_SCHEMA)
                    _setmeta(self.db, 'instrument', instrument)
                    _setmeta(self.db, 'uuid', uuid.uuid4())
                    _setmeta(self.db, 'generation', 0)
                    self.db.execute('INSERT INTO totals VALUES (1,?)',
                                    (json_compact(FrozenTotals(now_ms=0).safe_snapshot()).decode(),))
                    self.db.execute('INSERT INTO rolling_24h VALUES (1,0,?)',('0',))
                elif (prior != str(SIDECAR_SCHEMA)
                      or _metarow(self.db, 'instrument') != instrument):
                    raise Drift('Foreign or incompatible sidecar')
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            self.path.chmod(0o600)
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def atomic_freeze_batch(self):
        """OFFLINE bootstrap: all component freezes in one durable transaction.

        Every nested freeze uses a SAVEPOINT; a NotReady component rolls back
        just itself, while a crash/error of the batch rolls back ALL freezes.
        No intermediate checkpoint is published. Do not call ingest inside.
        """
        if getattr(self, '_freeze_batch_open', False) or self.db.in_transaction:
            raise Drift('Nested or overlapping stats freeze batch')
        self.db.execute('BEGIN IMMEDIATE')
        self._freeze_batch_open = True
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        finally:
            self._freeze_batch_open = False

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _cursor(self, kind):
        row = self.db.execute('SELECT payload FROM cursors WHERE kind=?', (kind,)).fetchone()
        return json.loads(row[0]) if row else None

    def _add_cycle(self, rowid: int, cycle: dict):
        cid = str(cycle.get('cycleId') or '')
        if not cid:
            raise Drift('Archive cycle lacks cycleId')
        if self.db.execute('SELECT 1 FROM frozen_cycles WHERE cycle_id=?', (cid,)).fetchone():
            raise Drift('Archive appended already frozen cycle')
        if self.db.execute('SELECT 1 FROM pending_cycles WHERE cycle_id=?', (cid,)).fetchone():
            raise Drift('Duplicate archive cycle in staging')
        self.db.execute('INSERT INTO pending_cycles VALUES (?,?,?)',
                        (cid, rowid, json_compact(cycle).decode()))
        for order in component_orders(cycle):
            if self.db.execute('SELECT 1 FROM frozen_orders WHERE order_id=?', (order,)).fetchone():
                raise Drift('New archived cycle connects to a frozen order')
            self.db.execute('INSERT INTO pending_orders VALUES (?,?)', (order, cid))
        for leg in cycle.get('tpOrders', []):
            if leg.get('source') == 'aggregate_exit':
                plan = leg.get('aggregatePlan')
                if not isinstance(plan, list) or not plan:
                    raise Drift('Invalid aggregate plan')
                for item in plan:
                    target = str(item.get('cycleId') or '')
                    if not target:
                        raise Drift('Empty aggregate plan target')
                    self.db.execute('INSERT OR IGNORE INTO pending_targets VALUES (?,?)',
                                    (cid, target))

    def _check_new_fill(self, rowid: int, fill: dict):
        oid = str(fill.get('ordId') or '')
        bill = str(fill.get('billId') or '')
        if not oid or not bill:
            raise Drift('Invalid execution identity')
        if self.db.execute('SELECT 1 FROM frozen_orders WHERE order_id=?', (oid,)).fetchone():
            raise Drift('LATE_FILL_FROZEN_ORDER: FULL rebuild necessary')

    def _stable_sources(self, root: Path, last_state_identity, state):
        after, new_identity = stable_state(root / 'state/bot_state.json')
        if new_identity != last_state_identity or after != state:
            raise NotReady('State changed during ingestion')

    def ingest(self, root: Path, *, inject_crash_after_cycles=False):
        """No all-history rows in Python. Source files are only opened mode=ro.

        Order follows actual write sequence in reverse: state -> archive ->
        accounted -> fills. Each source uses its own WAL snapshot. A concurrent
        state change causes the entire sidecar transaction to be rolled back.
        """
        root = Path(root)
        state, sid = stable_state(root / 'state/bot_state.json')
        if state.get('instrument') != self.instrument:
            raise Drift('State/sidecar instrument mismatch')
        ids = state_cycles(state)
        added = {}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for kind in ('cycles', 'accounted', 'fills'):
                with TableSnapshot(root / 'state' / SOURCES[kind], kind) as source:
                    previous = self._cursor(kind)
                    consumer = (self._add_cycle if kind == 'cycles' else
                                self._check_new_fill if kind == 'fills' else None)
                    current = source.incremental_cursor(previous, consumer=consumer)
                    added[kind] = current['new']
                    current.pop('new')
                    self.db.execute('INSERT INTO cursors VALUES (?,?) '
                                    'ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload',
                                    (kind, json_compact(current).decode()))
                if inject_crash_after_cycles and kind == 'cycles':
                    raise RuntimeError('Simulated crash before cursor transaction commit')
            # Every cycle absent from the PREVIOUS pinned state must be either
            # staged in archive or have been frozen. No inferred disappearance.
            for (cid,) in self.db.execute('SELECT cycle_id FROM last_state_ids'):
                if cid not in ids:
                    exists = (self.db.execute('SELECT 1 FROM pending_cycles WHERE cycle_id=?',
                                              (cid,)).fetchone()
                              or self.db.execute('SELECT 1 FROM frozen_cycles WHERE cycle_id=?',
                                                 (cid,)).fetchone())
                    if not exists:
                        raise Drift('Cycle disappeared from state without archive')
            self._stable_sources(root, sid, state)
            self.db.execute('DELETE FROM last_state_ids')
            self.db.executemany('INSERT INTO last_state_ids VALUES (?)',
                                ((cid,) for cid in ids))
            _setmeta(self.db, 'state_identity', json_compact(sid).decode())
            _setmeta(self.db, 'generation', int(_metarow(self.db, 'generation')) + 1)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return {'new': added, 'pending': self.pending_count(),
                'frozen': self.frozen_count(), 'generation': self.generation()}

    def generation(self):
        return int(_metarow(self.db, 'generation'))

    def pending_count(self):
        return self.db.execute('SELECT COUNT(*) FROM pending_cycles').fetchone()[0]

    def frozen_count(self):
        return self.db.execute('SELECT COUNT(*) FROM frozen_components').fetchone()[0]

    def _component(self, cycle_id, state_ids, limit):
        """Bounded BFS over indexed disk edges, including shared MARKET plan IDs."""
        pending = deque([str(cycle_id)])
        seen = set()
        cycles = []
        while pending:
            cid = pending.popleft()
            if cid in seen:
                continue
            if len(seen) >= limit:
                raise NotReady('Component exceeds bounded memory limit')
            row = self.db.execute('SELECT data FROM pending_cycles WHERE cycle_id=?',
                                  (cid,)).fetchone()
            if not row:
                raise NotReady('Shared MARKET component not fully archived')
            if cid in state_ids:
                raise NotReady('Component still present in state')
            cycle = json.loads(row[0])
            seen.add(cid)
            cycles.append(cycle)
            for oid in component_orders(cycle):
                if self.db.execute('SELECT 1 FROM frozen_orders WHERE order_id=?',
                                   (oid,)).fetchone():
                    raise Drift('Pending order already frozen elsewhere')
                for (sibling,) in self.db.execute(
                    'SELECT cycle_id FROM pending_orders WHERE order_id=?', (oid,)
                ):
                    if sibling not in seen:
                        pending.append(sibling)
            for (target,) in self.db.execute(
                'SELECT target_id FROM pending_targets WHERE src_id=?', (cid,)
            ):
                if target not in seen:
                    pending.append(target)
            for (parent,) in self.db.execute(
                'SELECT src_id FROM pending_targets WHERE target_id=?', (cid,)
            ):
                if parent not in seen:
                    pending.append(parent)
        return cycles

    @contextmanager
    def finance_snapshots(self, root):
        """Hold TWO pinned read-only WAL snapshots for one bounded bootstrap batch.

        Source files are never written. Watermark identity is verified on open;
        finance calls also refuse a changed sidecar source cursor. Snapshots are
        closed before the outer bootstrap checkpoint is committed.
        """
        root = Path(root)
        with ExitStack() as stack:
            views = {}
            for kind, filename in (('accounted', 'pnl.sqlite'), ('fills', 'fills.sqlite')):
                source = stack.enter_context(TableSnapshot(root / 'state' / filename, kind))
                pinned = self._cursor(kind)
                if pinned is None:
                    raise Drift('Missing pinned finance cursor: '+kind)
                source.validate_prior(pinned)
                views[kind] = (source, pinned)
            yield views

    def _scoped_finance(self, root, cycles, *, pinned_views=None):
        """Bounded exact fills and PnL rows. Missing data simply defers freeze."""
        root = Path(root)
        orders = sorted(set().union(*(component_orders(c) for c in cycles)))
        raw_keys = {pnl_identity(c) for c in cycles if c.get('state') != 'CANCELED'}
        if None in raw_keys:
            raise NotReady('Missing PnL identity')
        keys = sorted(raw_keys)
        fills = []
        accounted = {}
        with ExitStack() as stack:
            def view(kind, filename):
                if pinned_views is None:
                    source = stack.enter_context(TableSnapshot(root / 'state' / filename, kind))
                    pinned = self._cursor(kind)
                    if pinned is None:
                        raise Drift('Missing ingested finance cursor: '+kind)
                    source.validate_prior(pinned)
                else:
                    source, pinned = pinned_views[kind]
                    if source.db is None or pinned != self._cursor(kind):
                        raise Drift('Stale pooled finance snapshot: '+kind)
                return source, pinned

            db, pinned = view('accounted', 'pnl.sqlite')
            for key in keys:
                row = db.db.execute('SELECT data FROM accounted WHERE id=? AND rowid<=?',
                                    (key, pinned['rowid'])).fetchone()
                if row is None:
                    raise NotReady('Missing ledger row')
                accounted[key] = json.loads(row[0])
            db, pinned = view('fills', 'fills.sqlite')
            for i in range(0, len(orders), 200):
                batch = orders[i:i+200]
                statement = ('SELECT data FROM fills WHERE rowid<=? AND order_id IN ('
                             + ','.join('?' for _ in batch) + ')')
                for row in db.db.execute(statement, [pinned['rowid']] + batch):
                    fills.append(json.loads(row[0]))
                    if len(fills) > 20000:
                        raise NotReady('Component fill count exceeds memory cap')
        return fills, accounted

    @staticmethod
    def _restore_totals(snapshot):
        """Return bounded totals; check Decimal fields and checkpoint shape."""
        from okx_stats_v2_money import MONEY_FIELDS, COUNT_FIELDS
        from okx_stats_v2_contract import money
        from decimal import Decimal
        t = FrozenTotals(now_ms=int(snapshot['as_of_ms']))
        t.money = {k: money(snapshot['money'][k]) for k in MONEY_FIELDS}
        t.counts = {k: int(snapshot['counts'][k]) for k in COUNT_FIELDS}
        t.closed_buy_nominal = money(snapshot['closed_buy_nominal'])
        t.ledger_total = money(snapshot['ledger_total'])
        t.days = {k: money(v) for k,v in snapshot['days_utc'].items()}
        if snapshot['last_25h_events']:
            raise Drift('Phase3 stores rolling close events only in SQLite')
        t.recent = []
        return t

    def totals_snapshot(self):
        row = self.db.execute('SELECT payload FROM totals WHERE id=1').fetchone()
        if not row:
            raise Drift('Missing frozen totals')
        obj = json.loads(row[0])
        self._restore_totals(obj)  # refuse malformed financial data
        return obj

    def freeze_one(self, root: Path, cid: str, *, full_oracle, allocate,
                   contract_value, bid, now_ms, component_cap=DEFAULT_COMPONENT_CAP,
                   inject_crash_before_commit=False, pinned_views=None,
                   pinned_state=None):
        """Freeze one complete component or return NOT_READY (not a partial result).

        Source integrity is rechecked before money commit; a late fill detected
        by next ingest triggers FULL. OFFLINE ONLY; no financial publication.
        """
        root = Path(root)
        if pinned_state is None:
            state, sid = stable_state(root / 'state/bot_state.json')
        else:
            state, sid = pinned_state
        if state.get('instrument') != self.instrument:
            raise Drift('State instrument mismatch')
        live_ids = state_cycles(state)
        expected_identity = _metarow(self.db, 'state_identity')
        if expected_identity is None or json_compact(sid).decode() != expected_identity:
            raise NotReady('State changed after last source ingestion')
        cycles = self._component(cid, live_ids, component_cap)
        # An archive-only BFS cannot discover *unarchived* siblings sharing a
        # MARKET order or aggregatePlan. Defer rather than freezing half of a
        # component that currently lives in state.
        if len(live_ids) > component_cap * 16:
            raise NotReady('HOT state component check exceeds safe bound')
        component_ids = {str(c['cycleId']) for c in cycles}
        component_order_ids = set().union(*(component_orders(c) for c in cycles))
        for state_id, live in live_ids.items():
            if state_id in component_ids:
                raise NotReady('Component still present in state')
            if component_order_ids & component_orders(live):
                raise NotReady('Unarchived sibling shares component order')
            for leg in live.get('tpOrders', []):
                if leg.get('source') == 'aggregate_exit':
                    if any(str(item.get('cycleId') or '') in component_ids
                           for item in leg.get('aggregatePlan', [])):
                        raise NotReady('Unarchived aggregate plan shares component')
        if any(c.get('state') not in ('CLOSED', 'EXITED', 'CANCELED') for c in cycles):
            raise NotReady('Nonterminal archived component cannot be frozen')
        fills, ledger = self._scoped_finance(root, cycles, pinned_views=pinned_views)
        try:
            chunk = verified_frozen_chunk(cycles, fills, ledger, contract_value,
                                          full_oracle=full_oracle, allocate=allocate, bid=bid)
        except Drift as exc:
            # Financial incompleteness remains pending; invalid fills/ledger
            # should not be silently counted as a zero-valued frozen component.
            if ('fills_incomplete' in str(exc) or 'accounted_missing' in str(exc)):
                raise NotReady(str(exc)) from exc
            raise
        if pinned_state is None:
            self._stable_sources(root, sid, state)
        # The caller must revalidate the pinned state ONCE before batch commit.
        # Do NOT mix the two-stage raw-source capture with the sidecar staging
        # rows from a DIFFERENT generation. Caller must ingest before freezing.
        in_batch = getattr(self, '_freeze_batch_open', False)
        self.db.execute('SAVEPOINT stats_freeze_component' if in_batch else 'BEGIN IMMEDIATE')
        try:
            if any(self.db.execute('SELECT 1 FROM frozen_cycles WHERE cycle_id=?',
                                   (c['cycleId'],)).fetchone() for c in cycles):
                raise Drift('Component was already frozen')
            identifiers = sorted(str(c['cycleId']) for c in cycles)
            component_id = hashlib.sha256(json_compact(identifiers)).hexdigest()
            if self.db.execute('SELECT 1 FROM frozen_components WHERE component_id=?',
                               (component_id,)).fetchone():
                raise Drift('Component already frozen')
            row = self.db.execute('SELECT payload FROM totals WHERE id=1').fetchone()
            totals = self._restore_totals(json.loads(row[0]))
            totals.roll_forward(int(now_ms))
            totals.add(chunk)
            # 25-hour events are indexed in SQLite, NOT rewritten in a growing
            # JSON array at every single component freeze. One component only
            # is ever held in Python memory.
            totals.recent = []
            evidence = hashlib.sha256(json_compact(chunk.monetary) + json_compact(chunk.counts)).hexdigest()
            self.db.execute('INSERT INTO frozen_components VALUES (?,?)', (component_id, evidence))
            # The rolling 24h sum and raw close events are committed with the
            # same frozen-money/index transaction. Never recalculate a full day
            # of events on every StatsWorker tick.
            prior_24h,_=self._rolling_advance(now_ms)
            from okx_stats_v2_contract import money
            for ts,net in chunk.close_events:
                if int(ts) >= int(now_ms)-24*3600*1000:
                    prior_24h += money(net)
            self.db.execute('UPDATE rolling_24h SET as_of_ms=?,total=? WHERE id=1',
                            (int(now_ms),str(prior_24h)))
            self.db.executemany('INSERT INTO recent_events VALUES (?,?,?,?)',
                                ((component_id, i, int(ts), str(net))
                                 for i, (ts, net) in enumerate(chunk.close_events)))
            self.db.execute('DELETE FROM recent_events WHERE close_ms < ?',
                            (int(now_ms) - 25*3600*1000,))
            self.db.executemany('INSERT INTO frozen_cycles VALUES (?,?)',
                                ((cycle['cycleId'], component_id) for cycle in cycles))
            self.db.executemany('INSERT INTO frozen_orders VALUES (?,?)',
                                ((oid, component_id) for oid in chunk.orders))
            self.db.executemany('DELETE FROM pending_cycles WHERE cycle_id=?',
                                ((cycle['cycleId'],) for cycle in cycles))
            self.db.executemany('DELETE FROM pending_orders WHERE cycle_id=?',
                                ((cycle['cycleId'],) for cycle in cycles))
            self.db.executemany('DELETE FROM pending_targets WHERE src_id=?',
                                ((cycle['cycleId'],) for cycle in cycles))
            self.db.execute('UPDATE totals SET payload=? WHERE id=1',
                            (json_compact(totals.safe_snapshot()).decode(),))
            _setmeta(self.db, 'generation', int(_metarow(self.db, 'generation')) + 1)
            if inject_crash_before_commit:
                raise RuntimeError('Simulated crash before atomic money/index commit')
            self.db.execute('RELEASE SAVEPOINT stats_freeze_component' if in_batch else 'COMMIT')
        except BaseException:
            if in_batch:
                self.db.execute('ROLLBACK TO SAVEPOINT stats_freeze_component')
                self.db.execute('RELEASE SAVEPOINT stats_freeze_component')
            else:
                self.db.execute('ROLLBACK')
            raise
        return {'component_id': component_id, 'cycles': len(cycles), 'orders': len(chunk.orders),
                'grid_gain': chunk.monetary['grid_gain']}

    def _rolling_advance(self, now_ms):
        """Exact Decimal expiry delta; invoked ONLY within a sidecar transaction.

        Last close older than the old cutoff was already excluded. Expire only
        events crossing the moving 24h boundary, not every event in the day.
        Retention deletes MUST occur strictly after this advance.
        """
        from decimal import Decimal
        from okx_stats_v2_contract import money
        row=self.db.execute('SELECT as_of_ms,total FROM rolling_24h WHERE id=1').fetchone()
        if not row: raise Drift('Missing rolling 24h checkpoint')
        previous,raw=row
        previous=int(previous)
        now_ms=int(now_ms)
        if now_ms < previous:
            raise Drift('Rolling 24h clock moved backwards')
        total=money(raw)
        if now_ms == previous:
            return total, False
        old_cutoff=previous-24*3600*1000
        new_cutoff=now_ms-24*3600*1000
        expired=False
        for (raw_net,) in self.db.execute(
            'SELECT net FROM recent_events WHERE close_ms>=? AND close_ms<? ORDER BY close_ms',
            (old_cutoff,new_cutoff)
        ):
            total -= money(raw_net)
            expired=True
        return total, expired

    def last_24h(self, now_ms):
        """O(expiring events), O(1) when no new close/expiry; exact decimals.

        Sidecar only; no source writes, no trading/report publication. A changed
        24h sum is durably committed before return, so crash/restart remains safe.
        """
        total,expired=self._rolling_advance(now_ms)
        if expired:
            if self.db.in_transaction: raise Drift('Rolling expiry inside active transaction')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                # Re-evaluate inside transaction, never use a stale checkpoint.
                total,_=self._rolling_advance(now_ms)
                self.db.execute('UPDATE rolling_24h SET as_of_ms=?,total=? WHERE id=1',
                                (int(now_ms),str(total)))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return total

    def pending_batch(self, limit):
        """One fair bounded sweep across the queue, persistent after crash.

        The cursor records the last *attempted* source rowid, including deferred
        components. Repeated NotReady at the head cannot starve newer cycles.
        It is stored in the sidecar transaction together with frozen money.
        """
        limit=int(limit)
        if not 1<=limit<=256:
            raise ValueError('Invalid freeze attempt budget')
        raw=_metarow(self.db,'freeze_after_rowid','0')
        try:
            after=int(raw)
        except (TypeError,ValueError) as exc:
            raise Drift('Invalid durable pending scan cursor') from exc
        if after<0: raise Drift('Negative durable pending scan cursor')
        after_rows=self.db.execute(
            'SELECT cycle_id,rowid_source FROM pending_cycles WHERE rowid_source>? '
            'ORDER BY rowid_source LIMIT ?', (after,limit)).fetchall()
        if len(after_rows)<limit:
            before_rows=self.db.execute(
                'SELECT cycle_id,rowid_source FROM pending_cycles WHERE rowid_source<=? '
                'ORDER BY rowid_source LIMIT ?',
                (after,limit-len(after_rows))).fetchall()
            after_rows.extend(before_rows)
        return after_rows

    def pending_ids(self, limit=1000):
        return [row[0] for row in self.db.execute(
            'SELECT cycle_id FROM pending_cycles ORDER BY rowid_source LIMIT ?', (int(limit),)
        )]

    def export_summary(self):
        """No JSON mirroring/writing. An external export is NON-authoritative."""
        return {'schema': SIDECAR_SCHEMA, 'instrument': self.instrument,
                'sidecar_generation': self.generation(),
                'cursors': {kind: self._cursor(kind) for kind in SOURCES},
                'pending_count': self.pending_count(), 'frozen_components': self.frozen_count(),
                'recent_events_on_disk': self.db.execute('SELECT COUNT(*) FROM recent_events').fetchone()[0],
                'totals': self.totals_snapshot()}
