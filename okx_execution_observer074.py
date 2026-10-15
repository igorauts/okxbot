#!/usr/bin/env python3
"""Read-only multi-asset BTC/ETH/SOL v0.7.4 execution/burst observer.

No OKX requests. No order writes. Never imports the trading engine or .env.
Reads bot events, TP journal, atomic state snapshots and systemd journal.
Evidence-based alerts are separated from observations requiring exchange confirmation.
"""
from __future__ import annotations
import argparse
from collections import Counter, deque
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

TARGET_BUY = 25
MAX_BUY = 30
TARGET_TP = 25
MAX_TP = 55
HARD_TP = 150
BUFFER_COOLDOWN = 1.5
TP_CANCEL_GAP = 0.1
BUY_PROTECT_STEPS = 3
ISO = '%Y-%m-%dT%H:%M:%S.%f%z'
BID_RE = re.compile(r'РЕЖИМ=[A-Z_]+\s*\|\s*BID=([0-9]+(?:\.[0-9]+)?)')
SAFE_EVENTS = ('BUY_', 'TP_', 'AGG_', 'PARTIAL_', 'POSITION_', 'SYNC_', 'FEE_', 'RECONCILIATION_')
SECURE_FIELDS = ('event', 'price', 'buyPx', 'tp', 'cancelled', 'bid', 'qty', 'sz',
                 'filled', 'total', 'remaining', 'tpId', 'tpPx', 'real', 'maximum', 'active_before',
                 'buffer_before', 'count', 'age_ms', 'cycleId', 'ordId', 'clOrdId',
                 'side', 'state', 'reason', 'interval_ms', 'cycles', 'above_max',
                 'severity', 'seconds', 'code')

def num(x, default=None):
    try:
        if x is None: return default
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return default

def stamp(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z','+00:00')).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def ztime():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def clean_event(raw):
    return {k:raw[k] for k in SECURE_FIELDS if k in raw and isinstance(raw[k], (int,float,str,bool)) and len(str(raw[k]))<180}

def summarize_state(st, bid=None, step=Decimal('.5'), stale=False,
                    target_buy=25, protect_steps=5):
    """Pure, non-mutating view. Only ACTIVE SELLs count as real live TP.

    CANCELING is separately accounted for: still on exchange until final status.
    Desired BUY levels mirror ETH v073 desired_buy_levels, excluding occupied TP.
    """
    if st.get('instrument') not in ('BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP'):
        raise ValueError('Unsupported OKX swap instrument')
    cycles=st.get('cycles')
    if not isinstance(cycles,dict): raise ValueError('Invalid bot state: cycles')
    buys=[]; active_tp=[]; canceling_tp=[]; tp_pending=[]; virtual=[]
    invalid=[]; covered=Decimal('0'); aggregate=[]
    occupied=set()
    for level,c in cycles.items():
        if not isinstance(c,dict): continue
        s=c.get('state')
        px=num(c.get('buyPx'))
        if s in ('BUY_LIVE','BUY_PARTIAL','PLACING') and px is not None:
            buys.append({'price':px,'state':s,'ordId':str(c.get('ordId','')),'level':str(level)})
        if s in ('TP_WAIT','TP_PARTIAL','VIRTUAL_TP'):
            occupied.add(num(level))
        bought=num(c.get('filledBuy'),Decimal('0'))
        sells=Decimal('0')
        records=c.get('tpOrders') or []
        for r in records:
            if not isinstance(r,dict): continue
            sold=num(r.get('filled'),Decimal('0'))
            sells+=sold
            rem=num(r.get('sz'),Decimal('0'))-sold
            phase=r.get('phase')
            # Aggregate shared legs still live in records: count individual TP
            # only for concrete ACTIVE/CANCELING exchange SELL orders.
            item={'px':num(c.get('tpPx')),'buyPx':px,'ordId':str(r.get('ordId','')),
                  'clOrdId':str(r.get('clOrdId','')),'cycleId':str(c.get('cycleId','')),
                  'qty':rem,'phase':phase,'cancelAt':r.get('cancelAt',0)}
            if phase=='ACTIVE': active_tp.append(item)
            elif phase=='CANCELING':canceling_tp.append(item)
            elif phase in ('PENDING','PLACING'):tp_pending.append(item)
            if rem<0:invalid.append('negative_tp_remainder:'+str(level))
            if phase in ('ACTIVE','CANCELING','PENDING','PLACING') and (not item['clOrdId'] and not item['ordId']):
                invalid.append('tp_missing_identity_or_bad_qty:'+str(level))
        if sells>bought:invalid.append('sell_exceeds_buy:'+str(level))
        if s in ('TP_WAIT','TP_PARTIAL','VIRTUAL_TP') and bought<=0:
            invalid.append('tp_cycle_with_no_confirmed_buy:'+str(level))
        if s=='VIRTUAL_TP' and bought>sells:virtual.append({'price':num(c.get('tpPx')),'qty':bought-sells,'cycleId':str(c.get('cycleId'))})
        if s in ('TP_WAIT','TP_PARTIAL','VIRTUAL_TP'):
            # Exactly one coverage bucket must account for remaining exposure;
            # exchange position itself needs independent API and is not inferred.
            covered+=max(Decimal('0'), bought-sells)
    agg=st.get('aggregate_exits',[]) or []
    aggregate=[g for g in agg if isinstance(g,dict) and g.get('phase') not in ('FILLED','CANCELED','REJECTED')]
    missing=[]; near_missing=[]; desired=[]
    if bid is not None and not stale and step>0:
        top=(bid/step).to_integral_value(rounding=ROUND_FLOOR)*step
        if top>=bid:top-=step
        price=top
        while len(desired)<target_buy and price>0 and len(desired)<500:
            if price not in occupied: desired.append(price)
            price-=step
        exists={x['price'] for x in buys}
        missing=[p for p in desired if p not in exists]
        near_missing=[p for p in missing if abs(p-bid)<=protect_steps*step]
    return {'version':st.get('version'),'cycles':len(cycles), 'buy':len(buys),
            'real_tp':len(active_tp), 'canceling_tp':len(canceling_tp),
            'pending_tp':len(tp_pending), 'virtual_tp':len(virtual),
            'unresolved_market':len(aggregate), 'open_covered_contracts':str(covered),
            'missing_buy':len(missing), 'near_missing_buy':len(near_missing),
            'missing_prices':[str(p) for p in missing[:15]],
            'near_missing_prices':[str(p) for p in near_missing[:8]],
            'bad_cycles':invalid[:12], 'buy_rows':buys, 'tp_rows':active_tp,
            'canceling_tp_rows':canceling_tp,'pending_tp_rows':tp_pending}

def ranked_tps(rows,bid):
    return sorted(rows, key=lambda r:(abs(r['px']-bid),r['px'],r['ordId']))

class EventTail:
    def __init__(self, path, future_only=True):
        self.path=Path(path)
        self.handle=None
        self.ino=None
        self.future_only=future_only
        self.partial=''
        self.initial=True
    def read(self):
        if not self.path.is_file():return []
        inode=self.path.stat().st_ino
        if self.handle is None or self.ino!=inode or self.path.stat().st_size<self.handle.tell():
            if self.handle:self.handle.close()
            self.handle=self.path.open(encoding='utf8',errors='replace')
            self.ino=inode
            if self.initial and self.future_only: self.handle.seek(0,2)
            self.initial=False;self.partial=''
        lines=self.handle.readlines()
        result=[]
        for line in lines:
            # Bot writes one line per append. Avoid treating a partial read as evidence.
            s=self.partial+line
            if not s.endswith('\n'):
                self.partial=s;continue
            self.partial=''
            try:
                result.append(json.loads(s))
            except (ValueError, TypeError):
                continue
        return result
    def close(self):
        if self.handle:
            self.handle.close();self.handle=None

class Monitor:
    def __init__(self, root:Path, output:Path, event_file=None, journal=False, fake_clock=None,
                 mode='both'):
        self.root=root;self.mode=mode
        if mode not in ('both','burst','execution'):
            raise ValueError('Invalid monitoring mode')
        self.output=output;self.output.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(self.output,0o700)
        self.events=EventTail(event_file or root/'logs/bot04_events.jsonl')
        self.tp_journal=EventTail(root/'logs/tp_transitions_v057.jsonl')
        self.last_bid=None;self.bid_time=0.0
        self.last_state=None;self.last_state_time=0.0
        self.step=Decimal('.5')
        self.target_buy=25;self.max_buy=30;self.target_tp=25;self.max_tp=55
        self.buy_protect_steps=5
        self.latest_state_mtime=0.0
        self.last_sample=0.0
        self.recent=deque(maxlen=1200)
        self.recent_fills=deque(maxlen=500)
        self.recent_buy_cancels=deque(maxlen=50)
        self.first_burst=None
        self.last_buy_fill_time=0.0
        self.last_tp_buffer_cancel=None
        self.last_tp_set=set();self.last_tp_growth=0.0
        self.last_tp_growth_wall=0.0
        self.unresolved_since=None
        self.last_snapshot_log=0.0
        self.last_snapshot_signature=None
        self.underbuy_since=None;self.missing_since=None
        self.counters=Counter();self.alert_keys={}
        self.start_time=ztime();self.journal=journal
        self.last_buy_cancel_group_time=-1e12
        self.all_seen_buy_fills=set()
        self.all_seen_tp_fills=set()
        self.all_seen_agg_intents=set()
        self.now=fake_clock or time.monotonic
        cfg=root/'config.json'
        if cfg.exists():
            try:
                c=json.loads(cfg.read_text())
                self.step=num(c.get('grid_step'),Decimal('.5'))
                assert c.get('instrument') in ('BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP')
                self.coin=c['instrument'].split('-')[0]
                self.target_buy=int(c.get('active_buy_orders',25))
                self.max_buy=self.target_buy+5
                self.target_tp=int(c.get('real_tp_target',min(25,int(c.get('active_tp_orders',25)))))
                self.max_tp=self.target_tp+self.target_buy+5
                self.buy_protect_steps=5
                assert 1<=self.target_buy<=25 and self.target_buy<=self.max_buy<=30
                assert 1<=self.target_tp<=25 and self.target_tp<=self.max_tp<=55
                assert self.buy_protect_steps==5
                assert self.step>0
            except Exception as e: raise ValueError('Invalid bot config') from e
    def write(self,name,record):
        p=self.output/name
        fd=os.open(p,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        with os.fdopen(fd,'a') as f:f.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
    def alert(self,kind,severity,details,once=15):
        if self.mode=='burst': return
        now=self.now();key=kind+':'+str(details.get('price',''))
        if now-self.alert_keys.get(key,-1e9)<once:return
        self.alert_keys[key]=now;self.counters['alerts_'+severity]+=1
        rec={'ts':ztime(),'kind':kind,'severity':severity,**details}
        self.write('alerts.jsonl',rec)
        print(f"🚨 [{severity}] {kind}: {json.dumps(details,ensure_ascii=False,default=str)[:230]}",flush=True)
    def feed_bid_line(self,line):
        m=BID_RE.search(line)
        if m:
            px=num(m.group(1))
            if px is not None:
                self.last_bid=px;self.bid_time=self.now()
    def feed_tp_journal(self,rec):
        event=rec.get('event','')
        if event in ('CANCEL_INTENT','CANCEL_ACCEPTED_PENDING_FINAL', 'TP_CANCELED_CONFIRMED',
                     'TP_FILLED_CONFIRMED', 'RESTORE_INTENT','RESTORE_ACCEPTED_PENDING_CONFIRMATION',
                     'RESTORE_REJECTED','ATTACHED_TP_FAIL_51165'):
            self.counters['tp_journal_'+event]+=1
            self.write('tp_journal_v074.jsonl',{'ts':rec.get('time'),
                       'event':event,'cycleId':rec.get('cycleId'),
                       'buyPx':rec.get('buyPx'),'ordId':rec.get('ordId')})
    def feed_event(self,e):
        name=str(e.get('event',''))
        burst_was_active=self.first_burst is not None
        ts=stamp(e.get('ts'))
        if not ts:return
        t=ts.timestamp()
        self.counters['event_'+name]+=1
        # Avoid verbose WS payloads in output, but keep counters/signals.
        if name=='WS_ORDER':
            if e.get('side')=='sell' and e.get('state') in ('filled','partially_filled'):
                self.counters['ws_tp_fills']+=1
            elif e.get('side')=='buy' and e.get('state') in ('filled','partially_filled'):
                self.counters['ws_buy_fills']+=1
        d={'ts':e['ts'],'event':name,'data':clean_event(e)}
        if name.startswith(SAFE_EVENTS) and name!='WS_ORDER':
            self.write('timeline.jsonl',d)
        self.recent.append((t,name,e))
        if name in ('BUY_FILLED','BUY_FILLED_DURING_CANCEL'):
            oid=str(e.get('ordId') or '')
            duplicate=bool(oid and oid in self.all_seen_buy_fills)
            if duplicate:
                self.alert('DUPLICATE_BUY_FILLED','WARNING',{'ordId':oid})
            else:
                if oid:self.all_seen_buy_fills.add(oid)
                self.recent_fills.append((t,e))
                self.last_buy_fill_time=t
                if self.mode!='execution':self._maybe_start_burst(t)
        if name=='TP_FILLED':
            oid=str(e.get('tpId') or '')
            if oid:
                if oid in self.all_seen_tp_fills:
                    self.alert('DUPLICATE_TP_FILLED','WARNING',{'tpId':oid})
                self.all_seen_tp_fills.add(oid)
        if name=='BUY_CANCEL_REQUESTED':
            self.recent_buy_cancels.append((t,num(e.get('cancelled')),e.get('ordId')))
        if name=='BUY_BUFFER_CLEANUP':
            bid=num(e.get('bid'))
            if bid is None:return
            related=[(at,px,oid) for at,px,oid in self.recent_buy_cancels
                     if self.last_buy_cancel_group_time<at<=t and t-at<=1.5]
            related=related[-int(e.get('count',1) or 1):]
            self.last_buy_cancel_group_time=t
            for at,px,oid in related:
                if px is not None and abs(px-bid)<=self.buy_protect_steps*self.step:
                    self.alert('NEAR_BID_BUY_CANCEL','CRITICAL',
                               {'price':str(px),'bid':str(bid),'ordId':oid,'distance':str(abs(px-bid))},once=0)
            self.counters['buy_cleanup_batches']+=1
        if name=='TP_BUFFER_OVERFLOW':
            self.counters['tp_overflow_events']+=1
        if name=='TP_BUFFER_CLEANUP':
            self._tp_cancel_event(e,ts)
        if name in ('AGG_MARKET_INTENT','AGG_MARKET_ACK','AGG_MARKET_TERMINAL'):
            self.counters['agg_'+name]+=1
            if name=='AGG_MARKET_INTENT':
                cid=str(e.get('clOrdId') or '')
                if cid and cid in self.all_seen_agg_intents:
                    self.alert('DUPLICATE_AGG_MARKET_INTENT','CRITICAL',{'clOrdId':cid},once=0)
                if cid:self.all_seen_agg_intents.add(cid)
        if name in ('SYNC_WAIT','RECONCILIATION_LONG_WAIT','KNOWN_TP_UNRESOLVED',
                    'BUY_CANCEL_PENDING','TP_CANCEL_PENDING','AGG_MARKET_REJECTED'):
            self.alert(name,'WARNING',clean_event(e),once=60)
        if self.first_burst is not None and (burst_was_active or name not in
                ('BUY_FILLED','BUY_FILLED_DURING_CANCEL')):
            # A new burst's historic 10s events have already been counted.
            self.first_burst['counts'][name]+=1
    def _tp_cancel_event(self,e,ts):
        t=ts.timestamp();before=int(e.get('active_before',0) or 0)
        bid=num(e.get('bid'));px=num(e.get('price'))
        if before<=self.target_tp:
            self.alert('TP_BUFFER_CANCEL_AT_OR_BELOW_25','CRITICAL',
                       {'active_before':before,'price':str(px)},once=0)
        if self.last_tp_buffer_cancel is not None:
            gap=(t-self.last_tp_buffer_cancel)*1000
            if gap<99.5:
                self.alert('TP_BUFFER_CANCEL_INTERVAL','CRITICAL',
                           {'gap_ms':round(gap,2),'min_ms':100},once=0)
        self.last_tp_buffer_cancel=t
        self.counters['tp_buffer_cancels']+=1
        # v0.7.4: verify engine's monotonic scheduler timing. Never compare
        # poll-detected TP growth with a prior exchange event: false positives.
        quiet=e.get('quiet_ms')
        if quiet is not None and before<=self.max_tp and float(quiet)<1499.5:
            self.alert('TP_CANCEL_BEFORE_COOLDOWN','CRITICAL',
                       {'quiet_ms':quiet,'minimum_ms':1500,'active_before':before},once=0)
        if bid is not None and px is not None:
            # The event is written AFTER cancel intent is durably saved;
            # read a fresh state snapshot rather than a potentially stale 1s sample.
            try:
                st=json.loads((self.root/'state/bot04_state.json').read_text())
                fresh=summarize_state(st,bid,self.step)
            except (OSError,ValueError,json.JSONDecodeError):
                fresh=None
            rows=(fresh['tp_rows']+fresh['canceling_tp_rows']) if fresh else []
            if len(rows)==before and len(rows)>=26:
                rank=ranked_tps(rows,bid)
                # Price alone is ambiguous when two TP leg orders share a px;
                # only raise verified alerts when a unique candidate exists.
                candidates=[r for r in rows if r['px']==px and r['phase']=='CANCELING']
                if len(candidates)==1:
                    base={r['ordId'] for r in rank[:self.target_tp]}
                    if candidates[0]['ordId'] in base:
                        self.alert('BASE_TP_WAS_CANCELED','CRITICAL',
                                   {'price':str(px),'bid':str(bid),'real_before':before},once=0)
                    elif candidates[0]['ordId']!=rank[-1]['ordId']:
                        self.alert('TP_CLEANUP_NOT_FARTHEST','WARNING',
                                   {'price':str(px),'bid':str(bid),'real_before':before},once=0)
                else:self.counters['tp_cleanup_order_unverified']+=1
            else:self.counters['tp_cleanup_order_unverified']+=1
    def _maybe_start_burst(self,t):
        recent=[(tt,e) for tt,e in self.recent_fills if t-tt<=10.0]
        if self.first_burst is None and len(recent)>=5:
            start=recent[0][0]
            b={'start':start,'last':t,'fills':set(), 'counts':Counter(),
               'placed':0,'first_placed':None,'min_buy':999,
               'max_tp':0,'max_missing':0,'max_near_missing':0,
               'seconds_under_25':0.0,'last_sample':None,
               'max_unresolved_market':0}
            for tt,e in self.recent_fills:
                if tt>=start and tt<=t:
                    b['fills'].add(e.get('ordId') or (e.get('price'),tt))
            for tt,name,e in self.recent:
                if start<=tt<=t:
                    b['counts'][name]+=1
                    if name=='BUY_PLACED':
                        b['placed']+=1
                        if b['first_placed'] is None:b['first_placed']=tt
            self.first_burst=b
            print(f'⚡ {getattr(self,"coin","OKX")} BURST: {len(b["fills"])} BUY / 10s',flush=True)
        elif self.first_burst is not None:
            self.first_burst['last']=t
            self.first_burst['fills'].add(self.recent_fills[-1][1].get('ordId') or
                                          (self.recent_fills[-1][1].get('price'),t))
    def _finalize_burst(self):
        b=self.first_burst
        if b is None:return
        results={
            'finished_at':ztime(), 'first_fill_utc':datetime.fromtimestamp(b['start'],timezone.utc).isoformat(),
            'last_fill_utc':datetime.fromtimestamp(b['last'],timezone.utc).isoformat(),
            'fills':len(b['fills']), 'new_buy_placements':b['counts'].get('BUY_PLACED',0),
            'min_active_buy_observed':None if b['min_buy']==999 else b['min_buy'],
            'max_real_tp_observed':b['max_tp'],'max_missing_buy':b['max_missing'],
            'max_near_missing_buy':b['max_near_missing'],
            'observed_seconds_under_target':round(b['seconds_under_25'],2),
            'agg_market_intents':b['counts'].get('AGG_MARKET_INTENT',0),
            'tp_buffer_cancels':b['counts'].get('TP_BUFFER_CLEANUP',0),
            'new_buy_placement_after_first_fill_ms':None if b['first_placed'] is None else round(1000*(b['first_placed']-b['start']),1),
            'note':'Observations from asynchronous state/BID/event stream, not independent exchange certification.'}
        self.write('bursts_v074.jsonl',results)
        print('⚡ BURST SAVED: '+json.dumps({k:results[k] for k in ('fills','new_buy_placements','min_active_buy_observed','max_real_tp_observed','observed_seconds_under_target')},ensure_ascii=False),flush=True)
        self.counters['completed_bursts']+=1
        self.first_burst=None
    def _sample(self):
        p=self.root/'state/bot04_state.json'
        try:
            st=json.loads(p.read_text(encoding='utf8'))
            if st.get('version')!='0.7.4':
                self.alert('WRONG_ENGINE_VERSION','WARNING',{'state_version':st.get('version')},once=60)
            age=self.now()-self.bid_time
            trusted_bid=self.last_bid if self.last_bid is not None and age<12 else None
            curr=summarize_state(st,trusted_bid,self.step,stale=(trusted_bid is None),
                                  target_buy=self.target_buy,protect_steps=self.buy_protect_steps)
        except (OSError,json.JSONDecodeError,ValueError) as ex:
            self.alert('STATE_READ_FAILED','WARNING',{'error':str(ex)[:90]},once=60)
            return
        self.last_state=curr;self.last_state_time=self.now()
        self.counters['samples']+=1
        active=curr['buy'];real=curr['real_tp'];now=self.now()
        if active>self.max_buy:
            self.alert('BUY_EXCEEDS_BUFFER','CRITICAL',{'buy':active},once=5)
        if real>HARD_TP:
            self.alert('TP_EXCEEDS_EXCHANGE_CAP','CRITICAL',{'real_tp':real},once=5)
        elif real>self.max_tp:
            self.alert('TP_BUFFER_OVER_MAX','WARNING',{'real_tp':real},once=15)
        if curr['bad_cycles']:
            self.alert('CYCLE_VOLUME_INCONSISTENT','CRITICAL',{'examples':curr['bad_cycles'][:3]},once=20)
        if trusted_bid is not None and curr['missing_buy']>0:
            if self.missing_since is None:self.missing_since=now
            if curr['near_missing_buy'] and now-self.missing_since>4:
                self.alert('NEAR_BID_GRID_GAP','WARNING',{'bid':str(trusted_bid),
                    'missing':curr['near_missing_prices'],'age_s':round(now-self.missing_since,1)},once=20)
        else:self.missing_since=None
        if active<self.target_buy:
            if self.underbuy_since is None:self.underbuy_since=now
            if now-self.underbuy_since>=7:
                self.alert('BUY_COUNT_UNDER_TARGET','WARNING',{'buy':active,
                    'age_s':round(now-self.underbuy_since,1),'bid':str(trusted_bid)},once=20)
        else:self.underbuy_since=None
        ids={r['ordId'] or r['clOrdId'] for r in curr['tp_rows']}
        if len(ids-self.last_tp_set)>0 and self.last_tp_set and real>self.target_tp:
            self.last_tp_growth=now
            self.last_tp_growth_wall=time.time()
        self.last_tp_set=ids
        if curr['unresolved_market']:
            if self.unresolved_since is None:self.unresolved_since=now
            if now-self.unresolved_since>30:
                self.alert('AGG_MARKET_UNRESOLVED_OVER_30S','WARNING',
                           {'count':curr['unresolved_market'],'duration_s':round(now-self.unresolved_since,1)},once=60)
        else:self.unresolved_since=None
        b=self.first_burst
        if b:
            b['min_buy']=min(b['min_buy'],active)
            b['max_tp']=max(b['max_tp'],real)
            b['max_missing']=max(b['max_missing'],curr['missing_buy'])
            b['max_near_missing']=max(b['max_near_missing'],curr['near_missing_buy'])
            b['max_unresolved_market']=max(b['max_unresolved_market'],curr['unresolved_market'])
            if b['last_sample'] is not None and active<self.target_buy:
                b['seconds_under_25']+=min(3,now-b['last_sample'])
            b['last_sample']=now
        snapshot={'ts':ztime(),'bid':str(trusted_bid) if trusted_bid is not None else None,
                   **{k:v for k,v in curr.items() if k not in ('buy_rows','tp_rows','canceling_tp_rows','pending_tp_rows')}}
        signature=(active,real,curr['virtual_tp'],curr['missing_buy'],
                   curr['near_missing_buy'],curr['unresolved_market'])
        if b or signature!=self.last_snapshot_signature or now-self.last_snapshot_log>=10:
            self.write('snapshots_v074.jsonl',snapshot)
            self.last_snapshot_log=now;self.last_snapshot_signature=signature
    def step_once(self):
        for e in self.events.read():self.feed_event(e)
        for e in self.tp_journal.read():self.feed_tp_journal(e)
        now=self.now()
        if now-self.last_sample>=1:
            self._sample();self.last_sample=now
        if not hasattr(self,'last_summary_at') or now-self.last_summary_at>=5:
            self.last_summary_at=now
            summary={'ts':ztime(),'started_at':self.start_time,'event_counts':dict(self.counters),
                     'bid':str(self.last_bid) if self.last_bid else None,
                     'state':{k:v for k,v in (self.last_state or {}).items() if k not in
                            ('buy_rows','tp_rows','canceling_tp_rows','pending_tp_rows')},
                     'limitations':'No OKX REST/WS connection. Local state and bot event evidence only.'}
            self._atomic_summary(summary)
        if self.first_burst and (time.time()-self.first_burst['last']>20):
            self._finalize_burst()
    def _atomic_summary(self,d):
        dst=self.output/'summary_v074.json';tmp=self.output/'.summary_v074.tmp'
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump(d,f,ensure_ascii=False,indent=2,default=str)
        os.replace(tmp,dst)

def journal_proc(service):
    if not re.fullmatch(r'okx-grid-[a-z0-9-]+', service):
        raise ValueError('Invalid systemd service name')
    return subprocess.Popen(['journalctl','-f','-n','0','-u',service,
                '-o','cat','--no-pager'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                bufsize=0)

def serve(m,service):
    print(ztime(),m.coin, 'v0.7.4',m.mode,'observer ready; READ ONLY; NO OKX API, NO ORDERS',flush=True)
    p=None;sel=selectors.DefaultSelector();buffer=b''
    try:
        while True:
            if p is None or p.poll() is not None:
                if p is not None:
                    try:sel.unregister(p.stdout)
                    except Exception:pass
                p=journal_proc(service)
                os.set_blocking(p.stdout.fileno(),False)
                sel.register(p.stdout,selectors.EVENT_READ)
                buffer=b''
            for key,_ in sel.select(timeout=.25):
                # Drain *all* available journald lines. A burst can produce
                # dozens of log messages per second; processing only one per
                # tick would lag behind the market and corrupt BID correlation.
                while True:
                    try:chunk=os.read(key.fileobj.fileno(),65536)
                    except BlockingIOError:break
                    if not chunk:break
                    buffer+=chunk
                    if len(buffer)>1_000_000:
                        buffer=buffer[-200000:]
                    parts=buffer.split(b'\n')
                    buffer=parts.pop()
                    for raw in parts:
                        if b'BID=' in raw:
                            m.feed_bid_line(raw.decode('utf8',errors='replace'))
            m.step_once()
    except KeyboardInterrupt:pass
    finally:
        m.events.close();m.tp_journal.close()
        if p is not None:
            p.terminate()
            try:p.wait(timeout=3)
            except subprocess.TimeoutExpired:p.kill()
        sel.close()

def report(out:Path):
    f=out/'summary_v074.json'
    if not f.exists():
        print('No observations yet. Run service and wait for events.');return
    v=json.loads(f.read_text())
    s=v.get('state',{});c=v.get('event_counts',{})
    print('=== OKX v0.7.4 EXECUTION/BURST MONITOR (READ ONLY) ===')
    print('Last sample:',v['ts'],'Bid:',v.get('bid'))
    for k in ('buy','real_tp','canceling_tp','pending_tp','virtual_tp','missing_buy',
              'near_missing_buy','unresolved_market','open_covered_contracts'):
        print(f'{k:27s}',s.get(k,'?'))
    print('TP buffer cancels:',c.get('tp_buffer_cancels',0),
          'aggregated MARKET intents:',c.get('agg_AGG_MARKET_INTENT',0),
          'completed bursts:',c.get('completed_bursts',0))
    print('Warnings:',c.get('alerts_WARNING',0),'Critical:',c.get('alerts_CRITICAL',0))
    for name in ('alerts.jsonl','bursts_v074.jsonl'):
        path=out/name
        if path.exists():
            print(f'\n=== {name} (last 8) ===')
            for line in path.read_text().splitlines()[-8:]:
                try:print(json.dumps(json.loads(line),ensure_ascii=False)[:360])
                except ValueError:pass
    print('\nNOTE: exchange fills, fees and net position are NOT independently verified without read-only OKX API.')

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--root',type=Path,required=True)
    a.add_argument('--out',type=Path)
    a.add_argument('--once',action='store_true')
    a.add_argument('--service',help='Trading systemd service, e.g. okx-grid-eth064')
    a.add_argument('--mode',choices=('both','burst','execution'),default='both')
    a.add_argument('--report',action='store_true')
    opt=a.parse_args()
    out=opt.out or opt.root/('diagnostics/'+opt.mode+'_v074')
    if opt.report:report(out);return
    m=Monitor(opt.root,out,mode=opt.mode)
    if opt.once:
        # One-time snapshot must never parse old logs as new burst episodes.
        m.step_once();report(out);return
    if not opt.service: a.error('--service required unless --once or --report')
    serve(m,opt.service)
if __name__=='__main__':main()
