"""StatsWorker v2 PHASE-1, OFFLINE ONLY: append-snapshot and freeze-readiness contracts.

No engine imports this module. Read-only source scans are not financial authority.
Do not publish incremental statistics from it. Python standard library only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from decimal import Decimal
from typing import Iterator


class Drift(RuntimeError):
    """Source cannot be proven compatible with a saved watermark; use FULL."""


class NotReady(RuntimeError):
    """Candidate component cannot be frozen; remain HOT/on-disk deferred."""


SCHEMAS = {
    'cycles': ('id', 'data'),
    'accounted': ('id', 'kind', 'net', 'data'),
    'fills': ('order_id', 'bill_id', 'qty', 'data'),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_compact(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise Drift('Nonfinite monetary/quantity field')
    return result


def stable_state(path: Path) -> tuple[dict, tuple]:
    """Read JSON once without following a replaced/symlinked file."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Drift('Missing or symlinked state')
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    identity = lambda x: (x.st_dev, x.st_ino, x.st_mtime_ns, x.st_size)
    if identity(before) != identity(after) or len(raw) != after.st_size:
        raise NotReady('State replaced while being captured')
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise Drift('Invalid state JSON') from exc
    if not isinstance(state, dict) or not isinstance(state.get('cycles'), dict) or not isinstance(state.get('archived_cycles'), list):
        raise Drift('Unsupported state schema')
    return state, identity(after)


class TableSnapshot:
    """One read transaction of ONE SQLite database. A separate snapshot per DB.

    The persisted cursors are deliberately *not* mutated by this scanner; caller
    can atomically save all of them ONLY after complete financial verification.
    """
    def __init__(self, path: Path, table: str):
        if table not in SCHEMAS:
            raise ValueError('Unknown source table')
        self.path = Path(path)
        self.table = table
        self.db = None

    def __enter__(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise Drift(f'Missing/symlinked {self.table} database')
        self.st = self.path.stat()
        uri = self.path.resolve().as_uri() + '?mode=ro'
        self.db = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('BEGIN')
            cols = tuple(row[1] for row in self.db.execute(f'PRAGMA table_info({self.table})'))
            if cols != SCHEMAS[self.table]:
                raise Drift(f'Unexpected {self.table} schema: {cols}')
            self.count, self.max_rowid = self.db.execute(
                f'SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM {self.table}'
            ).fetchone()
            self.identity = (self.st.st_dev, self.st.st_ino)
            return self
        except BaseException:
            self.db.close()
            self.db = None
            raise

    def __exit__(self, *_):
        if self.db is not None:
            try:
                self.db.rollback()
            finally:
                self.db.close()
                self.db = None

    def row(self, rowid: int):
        return self.db.execute(
            f'SELECT data FROM {self.table} WHERE rowid=?', (rowid,)
        ).fetchone()

    def validate_prior(self, previous: dict | None):
        if previous is None:
            return 0, 0, b'\0' * 32
        if tuple(previous.get('file_identity', ())) != self.identity:
            raise Drift(f'{self.table} file identity changed')
        wm = previous.get('rowid')
        count = previous.get('count')
        if type(wm) is not int or type(count) is not int or wm < 0 or count < 0:
            raise Drift('Invalid prior cursor')
        if wm > self.max_rowid or count > self.count:
            raise Drift('Source truncated or reset')
        if wm:
            found = self.row(wm)
            if not found or digest(found[0].encode()) != previous.get('anchor_sha256'):
                raise Drift('Watermark anchor modified or renumbered')
        if not wm and count:
            raise Drift('Zero watermark with nonzero count')
        chain = previous.get('chain_sha256', '00' * 32)
        try:
            chain_bytes = bytes.fromhex(chain)
        except (TypeError, ValueError) as exc:
            raise Drift('Bad chain checksum') from exc
        if len(chain_bytes) != 32:
            raise Drift('Bad chain checksum length')
        return wm, count, chain_bytes

    def iter_new(self, after: int) -> Iterator[tuple[int, str]]:
        cursor = self.db.execute(
            f'SELECT rowid,data FROM {self.table} WHERE rowid>? AND rowid<=? ORDER BY rowid',
            (after, self.max_rowid),
        )
        yield from cursor

    def incremental_cursor(self, previous: dict | None, consumer=None) -> dict:
        """O(new rows) hashing; consumer must not retain the entire row stream."""
        old_wm, old_count, chain = self.validate_prior(previous)
        cursor = old_wm
        n = 0
        anchor = previous.get('anchor_sha256') if previous else None
        for rowid, raw in self.iter_new(old_wm):
            if rowid <= cursor:
                raise Drift('Non-monotonic rowid')
            if consumer is not None:
                consumer(rowid, json.loads(raw))
            content = raw.encode('utf-8')
            chain = hashlib.sha256(chain + str(rowid).encode() + b'\0' + content).digest()
            anchor = digest(content)
            n += 1
            cursor = rowid
        if old_count + n != self.count or cursor != self.max_rowid:
            raise Drift('Row count/append invariant failed')
        return {
            'file_identity': list(self.identity), 'count': self.count,
            'rowid': cursor, 'anchor_sha256': anchor,
            'chain_sha256': chain.hex(), 'new': n,
        }


def source_snapshots(root: Path, prior: dict | None = None, consumers=None) -> dict:
    """Order: state -> cycles -> accounted -> fills. All source reads read-only.

    Sources do NOT share an atomic transaction; state must be revalidated by
    the caller before it authorizes checkpoint/publishing.
    """
    root = Path(root)
    state, identity = stable_state(root / 'state' / 'bot_state.json')
    observed = {}
    paths = {
        'cycles': root / 'state' / 'cycles.sqlite',
        'accounted': root / 'state' / 'pnl.sqlite',
        'fills': root / 'state' / 'fills.sqlite',
    }
    for kind in ('cycles', 'accounted', 'fills'):
        with TableSnapshot(paths[kind], kind) as snapshot:
            observed[kind] = snapshot.incremental_cursor(
                (prior or {}).get(kind), (consumers or {}).get(kind)
            )
    return {'state': state, 'state_identity': identity, 'cursors': observed}


def pnl_identity(cycle: dict) -> str | None:
    """Matches PnLLedger.identity in published v0.7.61."""
    buy = cycle.get('ordId')
    if not buy:
        return None
    tp_orders = cycle.get('tpOrders', [])
    legs = [leg for leg in tp_orders if money(leg.get('filled', 0)) > 0]
    if len(tp_orders) > 1:
        return 'chain:' + str(buy)
    if not legs and not cycle.get('tpOrdId'):
        return None
    sell = legs[0]['ordId'] if legs else cycle['tpOrdId']
    return str(buy) + ':' + str(sell)


def component_orders(cycle: dict) -> set[str]:
    ids = {str(cycle.get('ordId') or ''), str(cycle.get('tpOrdId') or '')}
    ids.update(str(leg.get('ordId') or '') for leg in cycle.get('tpOrders', []))
    return ids - {''}


def connected_components(cycles: list[dict]) -> list[list[dict]]:
    """Input is ONLY the new/deferred/hot bounded workset, never all history."""
    owner = {}
    parent = list(range(len(cycles)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i, j):
        pi, pj = root(i), root(j)
        if pi != pj:
            parent[pi] = pj

    by_id = {}
    for i, cycle in enumerate(cycles):
        cid = str(cycle.get('cycleId') or '')
        if not cid or cid in by_id:
            raise Drift('Duplicate/missing cycleId in component workset')
        by_id[cid] = i
        for oid in component_orders(cycle):
            if oid in owner:
                join(i, owner[oid])
            else:
                owner[oid] = i
    for i, cycle in enumerate(cycles):
        for leg in cycle.get('tpOrders', []):
            if leg.get('source') != 'aggregate_exit':
                continue
            plan = leg.get('aggregatePlan')
            if not isinstance(plan, list) or not plan:
                raise Drift('Missing aggregate MARKET plan')
            for item in plan:
                target = str(item.get('cycleId') or '')
                if target in by_id:
                    join(i, by_id[target])
    groups = {}
    for i, cycle in enumerate(cycles):
        groups.setdefault(root(i), []).append(cycle)
    return list(groups.values())


def freeze_readiness(component: list[dict], archive_ids: set[str], state_ids: set[str],
                     fills_by_order: dict, accounted_by_id: dict) -> tuple[bool, str]:
    """Only checks eligibility for EXACT component FULL oracle. Never freezes money.

    On huge deferred populations the caller must page from a SQLite sidecar;
    it must NEVER build a full-history Python map of order IDs.
    """
    ids = {str(c.get('cycleId') or '') for c in component}
    if len(ids) != len(component) or not ids:
        return False, 'invalid_cycle_identity'
    if ids & state_ids:
        return False, 'state_overrides_archive'
    if not ids <= archive_ids:
        return False, 'archive_row_missing'
    expected = {}
    for c in component:
        state = c.get('state')
        if state == 'CANCELED' and money(c.get('filledBuy', 0)) == 0:
            continue
        if state not in ('CLOSED', 'EXITED'):
            return False, 'not_terminal'
        key = pnl_identity(c)
        row = accounted_by_id.get(key)
        if not row or row.get('kind') != state:
            return False, 'accounted_missing_or_wrong_kind'
        buy = str(c.get('ordId') or '')
        if not buy:
            return False, 'buy_order_missing'
        expected[buy] = money(c.get('filledBuy'))
        legs = [leg for leg in c.get('tpOrders', []) if money(leg.get('filled', 0)) > 0]
        if not legs and c.get('tpOrdId'):
            legs = [{'ordId': c['tpOrdId'], 'filled': c.get('filledTP', 0)}]
        if sum((money(leg.get('filled', 0)) for leg in legs), Decimal(0)) != money(c.get('filledTP', 0)):
            return False, 'tp_quantity_mismatch'
        for leg in legs:
            oid = str(leg.get('ordId') or '')
            if not oid:
                return False, 'sell_order_missing'
            expected[oid] = expected.get(oid, Decimal(0)) + money(leg['filled'])
            if leg.get('source') == 'aggregate_exit':
                plan = leg.get('aggregatePlan', [])
                plan_ids = {str(x.get('cycleId') or '') for x in plan}
                if not plan_ids <= ids or len(plan_ids) != len(plan):
                    return False, 'aggregate_plan_incomplete'
    for oid, required in expected.items():
        fills = fills_by_order.get(oid, [])
        actual = Decimal(0)
        seen = set()
        for row in fills:
            ident = (str(row.get('ordId') or ''), str(row.get('billId') or ''))
            if ident[0] != oid or not ident[1] or ident in seen:
                return False, 'invalid_fill_identity'
            seen.add(ident)
            if row.get('feeCcy') != 'USDT' or row.get('fee') in (None, ''):
                return False, 'invalid_fill_fee'
            actual += money(row.get('fillSz', 0))
        if actual != required:
            return False, 'fills_incomplete_or_overfilled'
    return True, 'ready_for_full_component_oracle'
