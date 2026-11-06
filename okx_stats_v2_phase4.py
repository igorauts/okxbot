"""Stats v2 phase 4: OFFLINE bounded shadow orchestrator and FULL parity oracle.

NO LIVE PATHS. NO STATS PUBLICATION. Does not import the executable engine.
When drift/mismatch occurs, it raises and requires an ORIGINAL FULL rebuild;
there is deliberately no silent recovery or auto-switching to incremental output.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from okx_stats_v2_contract import json_compact
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from okx_stats_v2_contract import Drift, NotReady, TableSnapshot, component_orders, money, stable_state
from okx_stats_v2_money import FrozenTotals, compose_with_hot, DAY_MS
from okx_stats_v2_phase3 import DurableStager, SOURCES, state_cycles

ENGINE_SHA = '9c6cf1b46ba8f0b045a80ea97b3178657fd296c2e6d564fb8579839e66057cbd'
ORACLE_FUNCTIONS = frozenset({'all_stat_cycles','accumulate_expected_sell','allocated_market_fills',
                              'stats_pct','full_stats_snapshot','_last_24h_profit'})


def load_pinned_oracle(path: Path):
    """Only compile pure AST function nodes from byte-pinned historical engine."""
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ENGINE_SHA:
        raise Drift('Pinned FULL oracle hash mismatch')
    tree = ast.parse(raw)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ORACLE_FUNCTIONS]
    if len(nodes) != len(ORACLE_FUNCTIONS):
        raise Drift('Pinned FULL oracle function set mismatch')
    env = {'Decimal': Decimal, 'D': lambda v: Decimal(str(v or '0'))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'PINNED_FULL_AST_ONLY', 'exec'), env)
    return {n:env[n] for n in ORACLE_FUNCTIONS}


def audit_due(instrument: str, now_ms: int, last_audit_ms: int | None,
              period_ms: int = 6*3600*1000):
    """Stable per-instrument 6h audit time. No randomness or coordinated burst."""
    if period_ms <= 0: raise ValueError('Invalid audit period')
    stagger = int.from_bytes(hashlib.sha256(instrument.encode()).digest()[:8], 'big') % period_ms
    slot = (int(now_ms) - stagger) // period_ms
    boundary = slot * period_ms + stagger
    return last_audit_ms is None or int(last_audit_ms) < boundary



def verify_full_prefix_chains(root: Path, cursors: dict):
    """O(history) audit ONLY; detect middle-row edits hidden from O(1) anchor.

    Recomputes EXACT phase-1 hash chain for the ingested prefix. It is never
    invoked during normal incremental ticks. Return no source row caches.
    """
    for kind, filename in SOURCES.items():
        saved=cursors.get(kind)
        if not saved:
            raise Drift('Missing source cursor for audit: '+kind)
        with TableSnapshot(Path(root)/'state'/filename,kind) as src:
            src.validate_prior(saved)
            chain=b'\0'*32
            n=0
            for rowid, raw in src.db.execute(
                f'SELECT rowid,data FROM {kind} WHERE rowid<=? ORDER BY rowid',
                (saved['rowid'],)
            ):
                chain=hashlib.sha256(chain+str(rowid).encode()+b'\0'+raw.encode()).digest()
                n+=1
            if n!=saved['count'] or chain.hex()!=saved['chain_sha256']:
                raise Drift('SOURCE_FULL_CHAIN_MISMATCH: '+kind)

def _unique_cycles(state, archive_rows):
    """Exact all_stat_cycles precedence: archive -> state.archived -> state.cycles."""
    return {str(c.get('cycleId') or c.get('ordId') or ''):c for c in
            list(archive_rows)+list(state.get('archived_cycles',[]))+list(state['cycles'].values())}


def _bounded_fills(root: Path, orders: set[str], pinned: dict, cap: int):
    """Look up only HOT orders, never keep all historic execution IDs in Python."""
    if len(orders) > cap: raise NotReady('HOT order cap reached')
    fills=[]
    with TableSnapshot(root/'state/fills.sqlite', 'fills') as s:
        s.validate_prior(pinned)
        ordered=sorted(orders)
        for i in range(0,len(ordered),200):
            chunk=ordered[i:i+200]
            if not chunk: continue
            query='SELECT data FROM fills WHERE rowid<=? AND order_id IN (' + ','.join('?'*len(chunk)) + ') ORDER BY rowid'
            for (raw,) in s.db.execute(query,[pinned['rowid']]+chunk):
                fills.append(json.loads(raw))
                if len(fills)>cap*40: raise NotReady('HOT fills cap reached')
    return fills


def _source_rows_for_full(root:Path, state_identity, caps:tuple[int,int]):
    """EXPLICIT OFFLINE FULL audit; O(history) memory and CPU by design.

    Uses one pinned read snapshot per SQLite; never touches source DB metadata.
    """
    max_cycles,max_fills=caps
    state, sid = stable_state(root/'state/bot_state.json')
    if sid != state_identity: raise NotReady('State changed since shadow snapshot')
    with TableSnapshot(root/'state/cycles.sqlite','cycles') as s:
        if s.count>max_cycles: raise NotReady('FULL audit size cap exceeded for cycles')
        archive=[json.loads(raw) for (raw,) in s.db.execute('SELECT data FROM cycles ORDER BY rowid')]
    with TableSnapshot(root/'state/fills.sqlite','fills') as s:
        if s.count>max_fills: raise NotReady('FULL audit size cap exceeded for fills')
        fills=[json.loads(raw) for (raw,) in s.db.execute('SELECT data FROM fills ORDER BY rowid')]
    if stable_state(root/'state/bot_state.json')[1] != sid:
        raise NotReady('State changed across FULL audit')
    return state,archive,fills


@dataclass(frozen=True)
class ShadowResult:
    """Data NEVER automatically published to trading system."""
    incremental: dict | None
    authoritative_full: dict | None
    comparison: str
    frozen: int
    pending: int
    hot_cycles: int
    ingested_rows: dict
    times_ms: dict
    last_24h_incremental: str | None = None
    last_24h_full: str | None = None
    mismatch_fields: tuple[str, ...] = ()
    authoritative_source: str = 'NONE'


class OfflineShadowRunner:
    """A bounded shadow step, no production integration or external API.

    Carries no historic fill/order caches across runs. Indexed SQLite sidecar is
    used for frozen membership and sums, ORIGINAL FULL remains authoritative.
    """
    def __init__(self, stager: DurableStager, oracle: dict, *, hot_cycle_cap=1024,
                 hot_order_cap=4096, freeze_attempt_cap=64, audit_caps=(100000,200000)):
        self.s=stager
        self.o=oracle
        self.hot_cycle_cap=hot_cycle_cap
        self.hot_order_cap=hot_order_cap
        self.freeze_attempt_cap=freeze_attempt_cap
        self.audit_caps=audit_caps
        # These diagnostics are only stored in the OFFLINE stats sidecar.
        self.s.db.execute('CREATE TABLE IF NOT EXISTS shadow_audit_events('
                          'seq INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, '
                          'event TEXT NOT NULL, details TEXT NOT NULL)')

    def _hot(self, root, state, sid, bid, cv):
        state_ids=state_cycles(state)
        pending=self.s.db.execute('SELECT data FROM pending_cycles ORDER BY rowid_source LIMIT ?',
                                  (self.hot_cycle_cap+1,)).fetchall()
        if len(state_ids)+len(pending) > self.hot_cycle_cap:
            raise NotReady('HOT cycle cap reached')
        cycles={str(c['cycleId']):c for c in (json.loads(row[0]) for row in pending)}
        cycles.update(state_ids)  # state overrides archived record in crash window
        # A hot cycle sharing order with an already frozen component cannot be
        # calculated separately without risking duplicate volume and PnL.
        orders=set()
        for c in cycles.values():
            orders.update(component_orders(c))
        sorted_orders=sorted(orders)
        for i in range(0,len(sorted_orders),200):
            chunk=sorted_orders[i:i+200]
            if not chunk: continue
            sql='SELECT order_id FROM frozen_orders WHERE order_id IN (' + ','.join('?'*len(chunk)) + ') LIMIT 1'
            if self.s.db.execute(sql,chunk).fetchone():
                raise Drift('HOT/FROZEN shared order')
        curs=self.s._cursor('fills')
        if curs is None: raise Drift('Missing pinned fills cursor')
        fills=_bounded_fills(root, orders, curs, self.hot_order_cap)
        # State value map is keyed by grid PRICE, not cycle ID.
        active_ids={str(x['cycleId']) for x in state['cycles'].values()}
        shadow_state={'cycles':{cid:cycles[cid] for cid in active_ids},
                      'archived_cycles':[c for cid,c in cycles.items() if cid not in active_ids],
                      'completed':state.get('completed',0)}
        shadow_state['archived_cycles']=[c for cid,c in cycles.items()
                                           if cid not in shadow_state['cycles']]
        hot=self.o['full_stats_snapshot'](shadow_state,fills,money(bid),money(cv),
                                           include_durable_archive=False,snapshot_archive=[])
        if not hot.get('complete'):
            raise NotReady('HOT fills not complete')
        if stable_state(root/'state/bot_state.json')[1] != sid:
            raise NotReady('State changed during HOT calculation')
        return hot,cycles,fills

    def bootstrap_batch(self, root: Path, *, bid, contract_value, now_ms,
                        freeze_budget=128):
        """Initial backlog migration, strictly offline: no report is produced.

        Needed when history is much larger than HOT capacity. Caller repeats
        batches until ready or progress stops; no full-history Python cache.
        A stalled pending component requires investigation and FULL fallback.
        """
        root=Path(root)
        resolved=root.resolve()
        if resolved == Path('/root/okx') or Path('/root/okx') in resolved.parents:
            raise Drift('OFFLINE PROTOTYPE REFUSES LIVE BOT ROOT')
        if not (1 <= freeze_budget <= 1000):
            raise ValueError('Invalid bootstrap budget')
        cv=money(contract_value)
        bid=money(bid)
        ingested=self.s.ingest(root)
        done=0
        not_ready=0
        with self.s.finance_snapshots(root) as views, self.s.atomic_freeze_batch():
            for cid in self.s.pending_ids(limit=freeze_budget):
                if self.s.db.execute('SELECT 1 FROM pending_cycles WHERE cycle_id=?',
                                     (cid,)).fetchone() is None:
                    continue
                try:
                    self.s.freeze_one(root,cid,full_oracle=self.o['full_stats_snapshot'],
                                      allocate=self.o['allocated_market_fills'],
                                      contract_value=cv,bid=bid,now_ms=int(now_ms),
                                      pinned_views=views)
                    done+=1
                except NotReady:
                    not_ready+=1
        return {'frozen_new_components':done,'not_ready_attempts':not_ready,
                'pending_remaining':self.s.pending_count(),
                'new_source_rows':ingested['new'],
                'report_published':False}

    def step(self, root: Path, *, bid, contract_value, now_ms,
             compare_full=False, freeze_budget=None, audit_policy='manual'):
        root=Path(root)
        # A deliberate guard: a development prototype must never be pointed
        # directly at actual funded-bot directories by accident.
        resolved=root.resolve()
        if resolved == Path('/root/okx') or Path('/root/okx') in resolved.parents:
            raise Drift('OFFLINE PROTOTYPE REFUSES LIVE BOT ROOT')
        if audit_policy not in ('manual','due'):
            raise ValueError('Invalid audit policy')
        bid=money(bid); cv=money(contract_value)
        t0=perf_counter()
        recorded=self.s.ingest(root)
        t1=perf_counter()
        budget=self.freeze_attempt_cap if freeze_budget is None else int(freeze_budget)
        if budget<0 or budget>10000: raise ValueError('Invalid freeze budget')
        frozen=0
        skipped=0
        # Fixed snapshot of pending IDs avoids repeatedly trying one unready
        # component in the same iteration; one component can consume >1 ID.
        for cid in self.s.pending_ids(limit=budget):
            if self.s.db.execute('SELECT 1 FROM pending_cycles WHERE cycle_id=?',(cid,)).fetchone() is None:
                continue
            try:
                self.s.freeze_one(root,cid, full_oracle=self.o['full_stats_snapshot'],
                                  allocate=self.o['allocated_market_fills'],
                                  contract_value=cv,bid=bid,now_ms=int(now_ms))
                frozen += 1
            except NotReady:
                skipped += 1
        t2=perf_counter()
        state,sid=stable_state(root/'state/bot_state.json')
        pinned=self.s.db.execute('SELECT val FROM meta WHERE key="state_identity"').fetchone()
        from okx_stats_v2_contract import json_compact
        if not pinned or pinned[0]!=json_compact(sid).decode():
            raise NotReady('State moved since ingestion')
        hot,hot_cycles,hot_fills=self._hot(root,state,sid,bid,cv)
        totals=self.s._restore_totals(self.s.totals_snapshot())
        # Periodic 24h close events are authoritative in sidecar recent_events.
        # NOTE: full_stats_snapshot does not expose BUY nominals. Need exact
        # quantities from bounded HOT BUY fills for percent denominator.
        hot_closed_nominal=Decimal(0)
        hot_open_nominal=Decimal(0)
        rows_by_order={}
        for f in hot_fills:
            rows_by_order.setdefault(str(f['ordId']),[]).append(f)
        for c in hot_cycles.values():
            if c.get('state') not in ('CLOSED','EXITED','CANCELED'):
                if money(c.get('filledBuy',0))>0:
                    for f in rows_by_order.get(str(c.get('ordId') or ''),[]):
                        hot_open_nominal+=money(f['fillPx'])*money(f['fillSz'])*cv
            elif c.get('state')=='CLOSED':
                for f in rows_by_order.get(str(c.get('ordId') or ''),[]):
                    hot_closed_nominal+=money(f['fillPx'])*money(f['fillSz'])*cv
        composed=compose_with_hot(totals,hot,hot_closed_nominal,hot_open_nominal,
                                  state,bid,cv,stats_pct=self.o['stats_pct'])
        recent=self.s.last_24h(int(now_ms))
        hot_recent=self.o['_last_24h_profit'](state,hot_fills,cv,int(now_ms),
                                              snapshot_archive=list(c for cid,c in hot_cycles.items()
                                                                    if cid not in state_cycles(state)))
        recent += hot_recent
        t3=perf_counter()
        full=None
        full24=None
        comparison='NOT_REQUESTED'
        last=self.s.db.execute('SELECT val FROM meta WHERE key=?',
                               ('last_full_audit_ms',)).fetchone()
        last_ms=int(last[0]) if last else None
        do_full=(compare_full or (audit_policy=='due' and
                                   audit_due(self.s.instrument, now_ms, last_ms)))
        mismatch_keys=()
        if do_full:
            verify_full_prefix_chains(root,{k:self.s._cursor(k) for k in SOURCES})
            full_state,archive,fills=_source_rows_for_full(root,sid,self.audit_caps)
            full=self.o['full_stats_snapshot'](full_state,fills,bid,cv,
                                               snapshot_archive=archive)
            if not full.get('complete'):
                raise NotReady('ORIGINAL FULL incomplete')
            full24=self.o['_last_24h_profit'](full_state,fills,cv,int(now_ms),
                                               snapshot_archive=archive)
            mismatch_keys=tuple(sorted(k for k in set(composed) | set(full)
                                       if composed.get(k)!=full.get(k)))
            if recent!=full24:
                mismatch_keys=tuple(sorted(set(mismatch_keys)|{'last_24h'}))
            comparison='PASS' if not mismatch_keys else 'MISMATCH'
            self.s.db.execute('BEGIN IMMEDIATE')
            try:
                if comparison=='PASS':
                    self.s.db.execute('INSERT INTO meta(key,val) VALUES (?,?) '
                                      'ON CONFLICT(key) DO UPDATE SET val=excluded.val',
                                      ('last_full_audit_ms',str(int(now_ms))))
                else:
                    self.s.db.execute('INSERT INTO shadow_audit_events(at_ms,event,details) '
                                      'VALUES (?,?,?)',
                                      (int(now_ms),'STATS_INCREMENTAL_MISMATCH',
                                       json_compact({'keys':mismatch_keys}).decode()))
                self.s.db.execute('COMMIT')
            except BaseException:
                self.s.db.execute('ROLLBACK')
                raise
        t4=perf_counter()
        if stable_state(root/'state/bot_state.json')[1]!=sid:
            raise NotReady('State changed after report comparison')
        return ShadowResult(
            incremental=composed,authoritative_full=full,comparison=comparison,
            frozen=frozen,pending=self.s.pending_count(),hot_cycles=len(hot_cycles),
            ingested_rows=recorded['new'],
            times_ms={'ingest':round((t1-t0)*1000,3),
                      'freeze':round((t2-t1)*1000,3),
                      'compose':round((t3-t2)*1000,3),
                      'full_audit':round((t4-t3)*1000,3)},
            last_24h_incremental=str(recent),
            last_24h_full=(str(full24) if full24 is not None else None),
            mismatch_fields=mismatch_keys,
            # On any mismatch, the FULL result is the only valid answer;
            # this offline prototype NEVER publishes either result.
            authoritative_source=('FULL' if full is not None else 'NONE'),
        )
