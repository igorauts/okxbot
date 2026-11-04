"""Conservative incremental verified finance for v0.7.51, offline candidate.

Frozen terminal *archived* cycle groups are computed once from real fills.
Mutable cycles and connected MARKET groups remain hot. Archive append can be
processed incrementally only when its order IDs are disjoint from the frozen
and mutable population; otherwise a FULL reset/rebuild is mandatory.

Every boot and each periodic audit compares *all* fields with the original
FULL oracle; mismatch refuses publication. No persistent monetary authority.
This cache is not a trader and performs no requests/writes.
"""
from __future__ import annotations
from decimal import Decimal
import heapq
import time

WINDOW_MS = 86400000
INTEGRITY_AUDIT_SEC = 1800

class FinanceDrift(RuntimeError): pass

def D(x):
    n = Decimal(str(x or '0'))
    if not n.is_finite(): raise FinanceDrift('Nonfinite money or quantity')
    return n

def cycle_id(c):
    v = str(c.get('cycleId') or c.get('ordId') or '')
    if not v: raise FinanceDrift('Cycle has no identity')
    return v

def orders(c):
    result={str(x) for x in (c.get('ordId'),c.get('tpOrdId')) if x}
    result.update(str(r['ordId']) for r in c.get('tpOrders',()) if r.get('ordId'))
    return result

def connected_components(cycles):
    """Union cycles connected by a shared BUY/SELL ID (incl. aggregate EXIT)."""
    parent=list(range(len(cycles)))
    def leader(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]]
            i=parent[i]
        return i
    owners={}
    for i,c in enumerate(cycles):
        for oid in orders(c):
            if oid in owners:
                a=leader(i); b=leader(owners[oid]);parent[a]=b
            else: owners[oid]=i
    groups={}
    for i in range(len(cycles)):
        groups.setdefault(leader(i),[]).append(cycles[i])
    return list(groups.values())

def select_fills(cycles,byorder):
    ids=set()
    for c in cycles: ids.update(orders(c))
    indexed=[f for oid in ids for f in byorder.get(oid,())]
    indexed.sort(key=lambda f:f[0])
    return [f[1] for f in indexed]

def reduced(cycles):
    return {'cycles':{cycle_id(c):c for c in cycles},'archived_cycles':[],
            'completed':0}

def chunk(cycles,byorder,bid,cv,full):
    fs=select_fills(cycles,byorder)
    d=full(reduced(cycles),fs,bid,cv,include_durable_archive=False)
    if not d.get('complete'): raise FinanceDrift('Incomplete chunk: missing required verified fills')
    closed_buy=open_buy=Decimal(0)
    for c in cycles:
        oid=str(c.get('ordId') or '')
        if not oid or D(c.get('filledBuy'))<=0: continue
        nominal=sum((D(f['fillPx'])*D(f['fillSz'])*cv
                     for _,f in byorder.get(oid,())),Decimal(0))
        if c.get('state')=='CLOSED':closed_buy+=nominal
        elif c.get('state')!='EXITED':open_buy+=nominal
    return d,closed_buy,open_buy

ADDITIVE_MONEY=('grid_gain','partial_exit_net','open_gain','floating_pnl',
                'limit_tp_net','market_tp_net','maker_volume','taker_volume',
                'maker_fee','taker_fee')
ADDITIVE_INT=('closed_count','partial_exit_count','expected_orders')

def add_chunks(a,b):
    if a is None:return b
    d=dict(a[0]);other=b[0]
    for name in ADDITIVE_MONEY:d[name]+=other[name]
    for name in ADDITIVE_INT:d[name]+=other[name]
    return d,a[1]+b[1],a[2]+b[2]

def compose(frozen,hot,state,bid,cv,pct):
    a,b=frozen[0],hot[0]
    plus=lambda k:a[k]+b[k]
    count=lambda k:int(a[k])+int(b[k])
    maker=plus('maker_volume');taker=plus('taker_volume')
    fees=plus('maker_fee')+plus('taker_fee')
    closed=plus('grid_gain');open_net=plus('open_gain');exit_net=plus('partial_exit_net')
    total=closed+open_net+exit_net
    cb=frozen[1]+hot[1];ob=frozen[2]+hot[2]
    active=list(state.get('cycles',{}).values())
    open_qty=sum((D(c.get('filledBuy'))-D(c.get('filledTP')) for c in active),Decimal(0))
    return {'complete':True,'bid':bid,'closed_count':count('closed_count'),
        'partial_exit_count':count('partial_exit_count'),'partial_exit_net':exit_net,
        'active_buys':sum(c.get('state') in ('BUY_LIVE','BUY_PARTIAL','PLACING') for c in active),
        'tps':sum(c.get('state') in ('TP_WAIT','TP_PARTIAL') for c in active),
        'grid_gain':closed,'grid_pct':pct(closed,cb),'open_gain':open_net,
        'open_pct':pct(open_net,ob),'floating_pnl':plus('floating_pnl'),
        'realized_open_components':open_net-plus('floating_pnl'),
        'limit_tp_net':plus('limit_tp_net'),'market_tp_net':plus('market_tp_net'),
        'total_gain':total,'total_pct':pct(total,cb+ob),
        'total_fees':fees,'volume':maker+taker,'maker_volume':maker,
        'taker_volume':taker,'maker_fee':plus('maker_fee'),'taker_fee':plus('taker_fee'),
        'maker_percent':pct(maker,maker+taker),'taker_percent':pct(taker,maker+taker),
        'maker_fee_pct':pct(-plus('maker_fee'),maker),
        'taker_fee_pct':pct(-plus('taker_fee'),taker),
        'open_contracts':open_qty,'open_base':open_qty*cv,
        'account_closed':state.get('completed',0),
        'expected_orders':count('expected_orders')}

def closed_events(cycles,byorder,cv,allocate):
    result={}
    for c in cycles:
        if c.get('state')!='CLOSED':continue
        cid=cycle_id(c);bidid=str(c.get('ordId') or '')
        buys=[row for _,row in byorder.get(bidid,())]
        legs=[r for r in c.get('tpOrders',()) if D(r.get('filled'))>0]
        if not legs and c.get('tpOrdId'):
            legs=[{'ordId':c['tpOrdId'],'filled':c.get('filledTP','0')}]
        sells=[]
        for leg in legs:
            rows=[r for _,r in byorder.get(str(leg.get('ordId') or ''),())]
            if leg.get('source')=='aggregate_exit':
                rows=allocate(rows,leg['aggregatePlan'],cid)
            sells.extend(rows)
        if not buys or not sells:
            raise FinanceDrift('Archived CLOSED lacks exact fills '+cid)
        ts=max(int(r.get('ts') or r.get('fillTime')) for r in sells)
        net=sum((D(r['fillPx'])*D(r['fillSz'])*cv+D(r['fee']) for r in sells),Decimal(0))
        net-=sum((D(r['fillPx'])*D(r['fillSz'])*cv-D(r['fee']) for r in buys),Decimal(0))
        result[cid]=(ts,net)
    return result

class FrozenWindow:
    def __init__(self):
        self.events={};self.heap=[];self.total=Decimal(0);self.now=None
    def add(self, events):
        for key, (stamp,net) in events.items():
            value=(int(stamp),D(net))
            if key in self.events:
                if self.events[key]!=value:raise FinanceDrift('Historical CLOSED event changed')
                continue
            self.events[key]=value
            if self.now is None or value[0]>=self.now-WINDOW_MS:
                heapq.heappush(self.heap,(value[0],key));self.total+=value[1]
    def advance(self,now):
        now=int(now)
        if self.now is not None and now<self.now:
            raise FinanceDrift('Wall clock moved backwards')
        self.now=now
        cutoff=now-WINDOW_MS
        while self.heap and self.heap[0][0]<cutoff:
            stamp,key=heapq.heappop(self.heap)
            if self.events[key][0]!=stamp:raise FinanceDrift('Window inconsistency')
            self.total-=self.events[key][1]
        return self.total

class IncrementalFinance:
    """Only LIVE StatsWorker uses this in-process cache; no restore from disk."""
    def __init__(self):
        self.ready=False;self.audit_monotonic=0.0
        self.last_archive_len=0;self.last_archive_id=None
        self.frozen_ids=set();self.frozen_orders=set();self.frozen=None
        self.hot_archive={};self.window=FrozenWindow()
        self.last_mode='uninitialized';self.full_audits=0;self.incremental_runs=0

    def reset(self):
        self.__init__()

    def _snapshot_state(self,state):
        items=list(state.get('archived_cycles',()))+list(state.get('cycles',{}).values())
        mapping={cycle_id(c):c for c in items}
        return mapping

    def _bootstrap(self,state,archive,byorder,bid,cv,full,allocate):
        live=self._snapshot_state(state)
        visible={cycle_id(c):c for c in archive}
        visible.update(live)  # same as original all_stat_cycles override order
        archive_ids={cycle_id(c) for c in archive}
        frozen=[];hot_archive={}
        for group in connected_components(list(visible.values())):
            can_freeze=all(cycle_id(c) in archive_ids and cycle_id(c) not in live and
                (c.get('state') in ('CLOSED','EXITED') or
                 (c.get('state')=='CANCELED' and D(c.get('filledBuy'))==0 and
                  D(c.get('filledTP'))==0)) for c in group)
            if can_freeze:frozen.extend(group)
            else:
                for c in group:
                    if cycle_id(c) in archive_ids and cycle_id(c) not in live:
                        hot_archive[cycle_id(c)]=c
        self.frozen_ids={cycle_id(c) for c in frozen}
        self.frozen_orders=set().union(*(orders(c) for c in frozen)) if frozen else set()
        self.hot_archive=hot_archive
        self.frozen=chunk(frozen,byorder,bid,cv,full)
        self.window=FrozenWindow()
        self.window.add(closed_events(frozen,byorder,cv,allocate))
        self.last_archive_len=len(archive)
        self.last_archive_id=cycle_id(archive[-1]) if archive else None
        self.ready=True

    def _increment_archive(self,state,archive,byorder,bid,cv,full,allocate):
        if len(archive)<self.last_archive_len:
            raise FinanceDrift('Archived SQLite history shrank')
        if self.last_archive_len and cycle_id(archive[self.last_archive_len-1])!=self.last_archive_id:
            raise FinanceDrift('Archived SQLite prefix changed')
        fresh=archive[self.last_archive_len:]
        if not fresh:return
        live=self._snapshot_state(state)
        # Evaluate ALL newly archived overrides before freezing any companion
        # cycle. A new terminal cycle can share an aggregate SELL with a new
        # archive entry still present in LIVE state; iteration order must not
        # split that connected group across frozen and mutable partitions.
        overriding=[c for c in fresh if cycle_id(c) in live]
        hot_population=list(live.values())+list(self.hot_archive.values())+overriding
        hot_orders=set().union(*(orders(c) for c in hot_population)) if hot_population else set()
        new_ids=set();new_group=[]
        for c in fresh:
            cid=cycle_id(c)
            if cid in new_ids or cid in self.frozen_ids or cid in self.hot_archive:
                raise FinanceDrift('Archive duplicate cycle appended')
            new_ids.add(cid)
            if cid in live:
                self.hot_archive[cid]=c
                continue
            terminal=(c.get('state') in ('CLOSED','EXITED') or
                      (c.get('state')=='CANCELED' and D(c.get('filledBuy'))==0 and
                       D(c.get('filledTP'))==0))
            if not terminal or orders(c)&(self.frozen_orders|hot_orders):
                # Linked or unfinished history must be fully regrouped, not
                # silently double-counted with immutable cold totals.
                raise FinanceDrift('New archive overlaps frozen/hot group')
            new_group.append(c)
        if new_group:
            add=chunk(new_group,byorder,bid,cv,full)
            self.frozen=add_chunks(self.frozen,add)
            self.frozen_ids.update(cycle_id(c) for c in new_group)
            self.frozen_orders.update(set().union(*(orders(c) for c in new_group)))
            self.window.add(closed_events(new_group,byorder,cv,allocate))
        self.last_archive_len=len(archive)
        self.last_archive_id=cycle_id(archive[-1])

    def calculate(self,state,archive,fills,bid,cv,now_ms,*,
                  full,pct,rolling,allocate,source_cache=None):
        """Return (verified financial dict, 24h Decimal, mode).

        Completeness of pinned fills and independent PnL ledger is checked in
        print_full_stats before publication. Every warm result is audited
        against original FULL at least once per INTEGRITY_AUDIT_SEC.
        """
        cv=D(cv);bid=D(bid)
        if source_cache is None:
            raise FinanceDrift('No verified SQLite source for incremental finance')
        byorder={}
        for index,f in enumerate(fills):
            byorder.setdefault(str(f['ordId']),[]).append((index,f))
        now_mono=time.monotonic()
        audit_due=not self.ready or now_mono-self.audit_monotonic>=INTEGRITY_AUDIT_SEC
        if self.ready:
            try:
                dirty=getattr(source_cache,'last_added_order_ids')
                if not isinstance(dirty,set):raise FinanceDrift('No exact delta order IDs')
                if dirty&self.frozen_orders:
                    raise FinanceDrift('Late fill in immutable financial group')
                self._increment_archive(state,archive,byorder,bid,cv,full,allocate)
                current=self._snapshot_state(state)
                for cid,c in current.items():
                    if cid in self.frozen_ids or orders(c)&self.frozen_orders:
                        raise FinanceDrift('Mutable cycle overrides/links immutable group')
            except FinanceDrift:
                self.reset()
                audit_due=True
        if not self.ready:
            self._bootstrap(state,archive,byorder,bid,cv,full,allocate)
            audit_due=True
        hot_map=dict(self.hot_archive)
        hot_map.update(self._snapshot_state(state))
        hot=list(hot_map.values())
        hot_data=chunk(hot,byorder,bid,cv,full)
        calculated=compose(self.frozen,hot_data,state,bid,cv,pct)
        hot_win=rolling(reduced(hot),select_fills(hot,byorder),cv,now_ms,snapshot_archive=[])
        frozen_win=self.window.advance(now_ms)
        past24=frozen_win+hot_win
        if audit_due:
            reference=full(state,fills,bid,cv,snapshot_archive=archive)
            if not reference.get('complete') or reference!=calculated:
                # A financial inconsistency must NEVER publish a cached number.
                self.reset()
                keys=sorted(k for k in reference if reference.get(k)!=calculated.get(k))
                raise FinanceDrift('Incremental vs FULL money mismatch: '+','.join(keys[:12]))
            reference24=rolling(state,fills,cv,now_ms,snapshot_archive=archive)
            if reference24!=past24:
                self.reset()
                raise FinanceDrift('Incremental vs FULL rolling24h mismatch')
            self.audit_monotonic=now_mono
            self.full_audits+=1
            self.last_mode='full_audit'
        else:
            self.incremental_runs+=1
            self.last_mode='incremental'
        return calculated,past24,self.last_mode
