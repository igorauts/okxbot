"""Candidate single-authority incremental financial calculator for OKX grids.

Only the StatsWorker thread may call this module. No trade/market entrypoints,
no order POSTs, no complete historical FULL scan during ordinary operation.
The original FULL function is injected ONLY to evaluate bounded components and
HOT state. Source ledgers are authoritative; this SQLite sidecar is derived.

FAIL CLOSED: invalid/missing finance yields no report, never invented money.
This file alone does not enable or modify a LIVE engine.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from okx_stats_v2_contract import Drift, NotReady, TableSnapshot, component_orders, json_compact, money, pnl_identity, stable_state
from okx_stats_v2_money import compose_with_hot
from okx_stats_v2_phase3 import DurableStager, state_cycles, _metarow


@dataclass(frozen=True)
class IncrementalResult:
    stats: dict
    last_24h: Decimal
    state: dict
    fills_hot: list
    captured_at: float
    bid: Decimal
    frozen_new: int
    pending: int
    hot_cycles: int
    source_rows: dict
    sidecar_generation: int
    deferred: int
    deferred_reasons: dict


class SingleAuthorityStats:
    """One derived sidecar, bounded active history, fail-closed results.

    All methods must run ONLY on StatsWorker, never from the trading loop.
    ``oracle`` is a dict of live engine's original pure finance functions.
    ``fill_store`` is the existing validated FillStore. No new API method.
    """
    def __init__(self, root: Path, instrument: str, sidecar: Path, *, oracle,
                 fill_store, expected_filled_orders, contract_value,
                 hot_cycle_cap=2048, hot_order_cap=4096, freeze_budget=64,
                 pnl_ledger=None):
        self.root=Path(root)
        self.instrument=instrument
        self.cv=money(contract_value)
        self.oracle=oracle
        self.fill_store=fill_store
        self.pnl_ledger=pnl_ledger
        self.expected_filled_orders=expected_filled_orders
        self.hot_cycle_cap=hot_cycle_cap
        self.hot_order_cap=hot_order_cap
        self.freeze_budget=freeze_budget
        if self.cv<=0 or not (1<=freeze_budget<=256) or hot_cycle_cap<=0 or hot_order_cap<=0:
            raise ValueError('Invalid Stats-v2 resource bounds')
        if not self.root.is_dir():
            raise Drift('Missing bot root')
        self.stager=DurableStager(sidecar,instrument)

    def close(self):
        self.stager.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _state_and_pending(self):
        state,sid=stable_state(self.root/'state/bot_state.json')
        if state.get('instrument')!=self.instrument:
            raise Drift('Foreign state instrument')
        pinned=_metarow(self.stager.db,'state_identity')
        if pinned!=json_compact(sid).decode():
            raise NotReady('State moved since indexed source capture')
        active=state_cycles(state)
        pending=self.stager.db.execute(
            'SELECT data FROM pending_cycles ORDER BY rowid_source LIMIT ?',
            (self.hot_cycle_cap+1,)).fetchall()
        if len(pending)+len(active)>self.hot_cycle_cap:
            raise NotReady('HOT cycle bound reached')
        cycles={str(c['cycleId']):c for c in (json.loads(row[0]) for row in pending)}
        cycles.update(active)
        return state,sid,cycles

    def _hot_scope(self, state, cycles):
        active_ids={str(c['cycleId']) for c in state['cycles'].values()}
        extra=[c for cid,c in cycles.items() if cid not in active_ids]
        hot_state={'cycles':dict(state['cycles']), 'archived_cycles':extra,
                   'completed':state.get('completed',0)}
        return hot_state,extra

    def _hot_fills(self, cycles):
        orders=set()
        for cycle in cycles.values():
            orders.update(component_orders(cycle))
        if len(orders)>self.hot_order_cap:
            raise NotReady('HOT order count bound reached')
        # No hot cycle may share a previously frozen MARKET/BUY order.
        sorted_orders=sorted(orders)
        for start in range(0,len(sorted_orders),200):
            subset=sorted_orders[start:start+200]
            if subset:
                sql=('SELECT order_id FROM frozen_orders WHERE order_id IN ('
                     +','.join('?'*len(subset))+') LIMIT 1')
                if self.stager.db.execute(sql,subset).fetchone():
                    raise Drift('HOT/FROZEN shared order')
        cursor=self.stager._cursor('fills')
        if cursor is None:
            raise Drift('Missing indexed fills cursor')
        data=[]
        with TableSnapshot(self.root/'state/fills.sqlite','fills') as s:
            s.validate_prior(cursor)
            for i in range(0,len(sorted_orders),200):
                subset=sorted_orders[i:i+200]
                if not subset:continue
                sql=('SELECT data FROM fills WHERE rowid<=? AND order_id IN ('
                     +','.join('?'*len(subset))+') ORDER BY rowid')
                for (raw,) in s.db.execute(sql,[cursor['rowid']]+subset):
                    data.append(json.loads(raw))
                    if len(data)>20000:
                        raise NotReady('HOT fill row bound reached')
        return data

    def _trace_hot_gap(self, hot, cycles):
        """Bounded 5-minute diagnostic; never discloses order IDs or PnL.

        The oracle returns only the first four mismatches, not the full count.
        No writes to financial source, no API, no extra read transaction.
        """
        now=time.monotonic()
        last=getattr(self,'_hot_gap_log_at',0.0)
        if last and now-last<300:
            return
        self._hot_gap_log_at=now
        cursor=self.stager._cursor('fills') or {}
        sample=hot.get('missing') or ()
        report={
            'hot_cycles':len(cycles),
            'missing_sample':min(len(sample),4),
            'sample_is_truncated':len(sample)>=4,
            'fill_orders_expected':hot.get('fill_orders_expected'),
            'fill_orders_seen':hot.get('fill_orders_seen'),
            'pinned_fill_rowid':cursor.get('rowid'),
            'pending_cycles':self.stager.pending_count(),
        }
        print('STATS_V2_HOT_GAP',json.dumps(report,sort_keys=True,
            separators=(',',':')),flush=True)

    def _recover_hot_fills(self, hot_state, *, missing_sample=()):
        """Repair incomplete HOT using authoritative fills, never estimate money.

        The financial oracle returns up to four mismatches from the PINNED
        snapshot.  If all four are already exact in a NEW read-only local WAL
        snapshot, prioritize a bounded re-pin rather than an unnecessary OKX
        recovery request.  A two-attempt ceiling guarantees that a persistent
        missing order cannot be starved by continuous new fills.  We never
        publish the newer rows against the older pinned source identity.

        For a real gap, retain the ORIGINAL FillStore/API recovery governor.
        """
        expected=self.expected_filled_orders(hot_state,include_archive=False)
        if len(expected)>self.hot_order_cap:
            raise NotReady('HOT recovery order bound reached')
        # The oracle reports only the first four mismatches.  Do not treat the
        # sample as proof that the complete HOT set is present or valid.
        sample=list(missing_sample)
        if sample and len(sample)<=4 and all(
                isinstance(item,(tuple,list)) and len(item)==3
                and str(item[0]) in expected for item in sample):
            reader=getattr(self.fill_store,'get_rows',None)
            if callable(reader) and getattr(self,'_local_hot_repin_streak',0)<2:
                ids={str(item[0]) for item in sample}
                rows=reader(sorted(ids))  # read-only NEW WAL snapshot, no API
                have={oid:Decimal(0) for oid in ids}
                seen=set()
                for row in rows:
                    oid=str(row.get('ordId') or '')
                    bill=str(row.get('billId') or '')
                    if oid not in ids or not bill:
                        raise Drift('Invalid local HOT fill identity')
                    if (oid,bill) in seen:
                        raise Drift('Duplicate local HOT fill identity')
                    seen.add((oid,bill))
                    have[oid]+=money(row['fillSz'])
                # Fresh WAL can legitimately move beyond an older pinned
                # state.  Only EXACT equality authorizes the fast re-pin;
                # overfill is left to the original financial reconciliation.
                if all(have[oid]==money(expected[oid]) for oid in ids):
                    self._local_hot_repin_streak=getattr(
                        self,'_local_hot_repin_streak',0)+1
                    raise NotReady('HOT local fills arrived; immediate re-pin required')
        self._local_hot_repin_streak=0
        self.fill_store.ensure_expected(expected)

    def _check_hot_ledger(self, cycles, hot, cursor):
        """Exact hot CLOSED/EXITED journal parity, O(hot), not O(history)."""
        keys=[]
        for c in cycles.values():
            if c.get('state') in ('CLOSED','EXITED'):
                key=pnl_identity(c)
                if not key:raise NotReady('HOT terminal cycle missing PnL key')
                keys.append(key)
        total=Decimal(0)
        if keys:
            with TableSnapshot(self.root/'state/pnl.sqlite','accounted') as db:
                db.validate_prior(cursor)
                # One bounded SQL batch per 200 terminal HOT cycles instead of
                # one SQLite round-trip per key.  Exact pinned rowid and the
                # original fail-closed per-key presence check are preserved.
                unique_keys=sorted(set(keys))
                for start in range(0,len(unique_keys),200):
                    subset=unique_keys[start:start+200]
                    sql=('SELECT id,data FROM accounted WHERE id IN ('+
                         ','.join('?'*len(subset))+') AND rowid<=?')
                    rows={str(k):json.loads(raw) for k,raw in
                          db.db.execute(sql,subset+[cursor['rowid']])}
                    for key in subset:
                        if key not in rows:
                            raise NotReady('HOT PnL ledger row not committed')
                        total+=money(rows[key]['net'])
        expect=money(hot['grid_gain'])+money(hot['partial_exit_net'])
        if abs(total-expect)>Decimal('0.00000001'):
            raise Drift('HOT ledger does not reconcile with HOT finance')

    @staticmethod
    def _defer_reason(message):
        for name,needle in (
            ('fills_incomplete','fills_incomplete'),
            ('accounted_missing','accounted_missing'),
            ('accounted_missing','Missing ledger row'),
            ('component_present','Component still present'),
            ('unarchived_sibling','Unarchived'),
            ('nonterminal','Nonterminal'),
            ('shared_component','Shared MARKET component'),
            ('missing_identity','Missing PnL identity'),
        ):
            if needle in message:return name
        return 'other_not_ready'

    def _freeze_bounded(self, bid, now_ms):
        completed=0
        deferred=0
        reasons={}
        # One pinned state and one source read snapshot for the entire budget.
        # Invalidated state rolls back all frozen money AND the durable cursor.
        with self.stager.finance_snapshots(self.root) as views, self.stager.atomic_freeze_batch():
            state,sid=stable_state(self.root/'state/bot_state.json')
            if state.get('instrument')!=self.instrument:
                raise Drift('State instrument mismatch during freeze')
            pinned=(state,sid)
            for cid,rowid in self.stager.pending_batch(self.freeze_budget):
                if self.stager.db.execute(
                    'SELECT 1 FROM pending_cycles WHERE cycle_id=?',(cid,)).fetchone() is None:
                    continue
                try:
                    self.stager.freeze_one(self.root,cid,
                        full_oracle=self.oracle['full_stats_snapshot'],
                        allocate=self.oracle['allocated_market_fills'],
                        contract_value=self.cv,bid=bid,now_ms=now_ms,
                        pinned_views=views,pinned_state=pinned)
                    completed+=1
                except NotReady as exc:
                    deferred+=1
                    reason=self._defer_reason(str(exc))
                    reasons[reason]=reasons.get(reason,0)+1
                # Advance only within the same atomic checkpoint as money.
                _metaval=str(rowid)
                self.stager.db.execute(
                    'INSERT INTO meta VALUES (?,?) '
                    'ON CONFLICT(key) DO UPDATE SET val=excluded.val',
                    ('freeze_after_rowid',_metaval))
            self.stager._stable_sources(self.root,sid,state)
            # Terminal cycles legitimately waiting for their linked archive
            # membership are NOT failed financial progress. Alarm only for
            # actionable NotReady categories; retain durable count otherwise.
            # No change to monetary freezing or source-of-truth ledgers.
            harmless={'shared_component','component_present',
                      'unarchived_sibling','nonterminal'}
            actionable={name:n for name,n in reasons.items()
                        if name not in harmless and n}
            old=int(_metarow(self.stager.db,'freeze_no_progress','0'))
            stalled=(min(old+1,1000000) if actionable and completed==0 else 0)
            self.stager.db.execute(
                'INSERT INTO meta VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET val=excluded.val',
                ('freeze_no_progress',str(stalled)))
        self.freeze_stalled=stalled
        self.last_freeze_deferred=deferred
        self.last_freeze_reasons=reasons
        return completed,deferred,reasons

    def compute(self, bid, *, now=None, allow_fill_recovery=True,
                _local_repin_used=False):
        """One finance attempt. On failure DO NOT publish a report.

        Missing HOT fills may be recovered by the original FillStore before
        raising NotReady, but an extra snapshot is always required to publish.
        """
        captured=time.time() if now is None else float(now)
        if not 0<captured<10**11:
            raise ValueError('Invalid financial capture timestamp')
        now_ms=int(captured*1000)
        bid=money(bid)
        if bid<=0:raise Drift('Invalid financial BID')
        ingested=self.stager.ingest(self.root)
        frozen,deferred,deferred_reasons=self._freeze_bounded(bid,now_ms)
        state,sid,cycles=self._state_and_pending()
        hot_state,extra=self._hot_scope(state,cycles)
        fills=self._hot_fills(cycles)
        hot=self.oracle['full_stats_snapshot'](hot_state,fills,bid,self.cv,
                    include_durable_archive=False,snapshot_archive=[])
        if not hot.get('complete'):
            self._trace_hot_gap(hot,cycles)
            if allow_fill_recovery:
                try:
                    self._recover_hot_fills(
                        hot_state,missing_sample=hot.get('missing',()))
                except NotReady as exc:
                    if (str(exc)=='HOT local fills arrived; immediate re-pin required'
                            and not _local_repin_used):
                        # Exactly ONE additional source-pinned computation on
                        # this StatsWorker call. No publication from an older
                        # cursor and no recursive retry loop under heavy churn.
                        return self.compute(bid,now=captured,
                            allow_fill_recovery=True,_local_repin_used=True)
                    raise
            raise NotReady('HOT fills not complete; next pin required')
        self._local_hot_repin_streak=0
        # Only on a proven missing HOT journal row: invoke the original,
        # idempotent ledger writer over bounded HOT. Ordinary publications do
        # NOT rescan or rewrite any ledger records.
        try:
            self._check_hot_ledger(cycles,hot,self.stager._cursor('accounted'))
        except NotReady as exc:
            if (str(exc)!='HOT PnL ledger row not committed'
                    or self.pnl_ledger is None):
                raise
            self.pnl_ledger.ingest_history(hot_state,fills)
            # The persisted accounted cursor predates any new ledger rows.
            # Even when another PnL worker won the race, re-pin next time.
            raise NotReady('HOT PnL ledger repair attempted; next pin required') from exc
        byorder={}
        for f in fills:
            byorder.setdefault(str(f['ordId']),[]).append(f)
        hot_closed_nom=Decimal(0)
        hot_open_nom=Decimal(0)
        for c in cycles.values():
            status=c.get('state')
            oid=str(c.get('ordId') or '')
            for f in byorder.get(oid,()):
                val=money(f['fillPx'])*money(f['fillSz'])*self.cv
                if status=='CLOSED':hot_closed_nom+=val
                elif status not in ('CANCELED','EXITED'):hot_open_nom+=val
        frozen_totals=self.stager._restore_totals(self.stager.totals_snapshot())
        composed=compose_with_hot(frozen_totals,hot,hot_closed_nom,hot_open_nom,
                     state,bid,self.cv,stats_pct=self.oracle['stats_pct'])
        hot_24=self.oracle['_last_24h_profit'](hot_state,fills,self.cv,now_ms,
                                               snapshot_archive=[])
        last_24=self.stager.last_24h(now_ms)+hot_24
        if abs(frozen_totals.ledger_total+money(hot['grid_gain'])+
                money(hot['partial_exit_net'])-
                money(composed['grid_gain'])-money(composed['partial_exit_net']))>Decimal('0.00000001'):
            raise Drift('FROZEN+HOT ledger does not reconcile')
        # In particular, a newly archived sibling may appear between source
        # scan and publication: only a stable state and pinned cursors pass.
        _,sid2=stable_state(self.root/'state/bot_state.json')
        if sid2!=sid:
            raise NotReady('State changed during financial calculation')
        return IncrementalResult(
            stats=composed,last_24h=last_24,state=state,fills_hot=fills,
            captured_at=captured,bid=bid,frozen_new=frozen,
            pending=self.stager.pending_count(),hot_cycles=len(cycles),
            source_rows=ingested['new'],sidecar_generation=self.stager.generation(),
            deferred=deferred,deferred_reasons=deferred_reasons)
