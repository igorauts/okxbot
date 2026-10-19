"""Read-only Telegram notifications for OKX Grid Bot.

Consumes existing local events, journal and persisted accounting. Never contacts OKX,
reads exchange secrets, or writes bot state. Only Telegram monitor own cursor changes.
"""
import html
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from okx_telegram_views import (COUNTS, MODE, date_epoch, read_json, number,
                                market_message, market_ledger, journal)

# Event names represent actionable faults, not ordinary recovery or startup progress.
EVENT_ALARMS = {
    'ACCOUNT_AUTO_SOFT_STOP','BUY_POST_UNRESOLVED','TP_POST_UNRESOLVED',
    'KNOWN_TP_UNRESOLVED','TP_RESTORE_REJECTED','PANIC_UNRESOLVED',
    'RECONCILIATION_LONG_WAIT','FEE_CHECK_PAUSED_BUYS',
    'ACCOUNT_POSITIONS_GATE_DEGRADED','TP_HARD_CAP_REACHED','TP_BUFFER_OVERFLOW',
    'TP_CANCEL_REJECTED','PARTIAL_EXIT_MARKET_REJECTED',
    'BUY_PAUSED_UNPLACEABLE_TP', 'BOT_TERMINAL_STATS_PENDING',
}
OBSERVER_ALARMS = {'TP_HARD_CAP_REACHED','TP_BUFFER_OVERFLOW','STATE_READ_FAILED',
                   'PERSISTED_STATE_DIFFERS_FROM_ENGINE','RECONCILIATION_LONG_WAIT'}


def one(api, st, key, msg, *, html_mode=False):
    fired=st.setdefault('incident_sent',{})
    if key in fired:
        return False
    api.send(msg,parse_mode='HTML' if html_mode else None)
    fired[key]=time.time()
    # State retention is bounded; a historical incident never repeats if logs
    # cursors are stable. Never remove incident keys for current active faults.
    if len(fired)>20000:
        cutoff=time.time()-30*86400
        for k in list(fired):
            if fired[k]<cutoff: del fired[k]
    return True


def event_key(name,obj):
    typ=str(obj.get('event',''))
    # Avoid repeating a persistent fault every engine loop. For per-order
    # incidents use actual immutable order identity when available.
    identity=(obj.get('ordId') or obj.get('clOrdId') or obj.get('code') or
              obj.get('reason') or obj.get('phase') or typ)
    return f'event/{name}/{typ}/{identity}'


def event_alert(api,st,name,obj):
    typ=obj.get('event')
    if typ not in EVENT_ALARMS:
        return
    extras=[]
    for k in ('code','ordId','reason','message'):
        val=obj.get(k)
        if val is not None:
            extras.append(f'{k}: {str(val)[:120]}')
    one(api,st,event_key(name,obj),f'🚨 {typ} | {name}\n'+ '\n'.join(extras))


def market_event(st,name,obj):
    typ=obj.get('event')
    cid=str(obj.get('clOrdId') or '')
    if not cid and typ!='AGG_MARKET_REJECTED':
        return
    records=st.setdefault('market_events',{})
    key=f'{name}/{cid}'
    if typ=='AGG_MARKET_REJECTED' and not cid:
        # v0.7.14 rejection emits no clOrdId. Only one unresolved aggregate
        # market intent can exist per bot; link to that record, not a phantom.
        pending=[k for k,v in records.items() if k.startswith(name+'/')
                 and v.get('intent_at') and not v.get('terminal') and not v.get('rejected')]
        key=max(pending,key=lambda k:records[k]['intent_at']) if pending else name+'/rejected/'+str(obj.get('ts'))
    item=records.setdefault(key,{})
    ts=date_epoch(obj.get('ts'))
    if typ=='AGG_MARKET_INTENT':
        item['intent_at']=ts
        item['cycles']=obj.get('cycles')
    elif typ=='AGG_MARKET_ACK':
        item['ack_at']=ts
        item['order_id']=obj.get('ordId')
    elif typ=='AGG_MARKET_TERMINAL':
        item['terminal_at']=ts
        item['terminal']=obj
        item['order_id']=obj.get('ordId')
    elif typ=='AGG_MARKET_REJECTED':
        item['rejected']=obj


def market_process(api,st,names):
    items=st.setdefault('market_events',{})
    for key,item in list(items.items()):
        name=key.split('/',1)[0]
        if name not in names:
            continue
        if item.get('rejected'):
            one(api,st,'market/rejected/'+key,'🚨 Market SELL REJECTED | '+name)
            del items[key]
            continue
        terminal=item.get('terminal')
        if not terminal:
            intent=item.get('intent_at')
            if intent and time.time()-intent>30:
                one(api,st,'market/wait/'+key,
                    '🚨 Market SELL | '+name+' | ожидание подтверждённого завершения >30 с')
            continue
        if terminal.get('state')!='FILLED':
            one(api,st,'market/incomplete/'+key,
                '🚨 Market SELL не завершён | '+name+' | '+str(terminal.get('state')))
            del items[key]
            continue
        root=names[name]
        times={}
        intent=item.get('intent_at')
        ack=item.get('ack_at')
        done=item.get('terminal_at')
        if intent and ack and ack>=intent:
            times['ack']=(ack-intent)*1000
        if intent and done and done>=intent:
            times['full']=(done-intent)*1000
        oid=str(terminal.get('ordId') or '')
        # All SELL fills are local, already written by engine. Fill first timestamp
        # may use OKX clock: therefore displayed with approximation sign.
        try:
            import sqlite3
            db=root/'state/fills_v068.sqlite'
            with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as con:
                rows=[json.loads(x[0]) for x in con.execute('SELECT data FROM fills WHERE order_id=?',(oid,))]
            if intent and rows:
                first=min((float(f.get('fillTime') or f.get('ts')) / 1000.0 for f in rows))
                if 0<=first-intent<60:
                    times['fill']=(first-intent)*1000
        except (OSError,ValueError,TypeError,KeyError,Exception):
            pass
        text=market_message(name,root,terminal,times)
        if text:
            one(api,st,'market/filled/'+key,text,html_mode=True)
            del items[key]
        elif done and time.time()-done>30:
            one(api,st,'market/accounting_wait/'+key,
                '⚠️ Market SELL | '+name+' | FILLED, ожидаем подтверждённый net PnL')
        if done and time.time()-done>86400:
            # Preserve an alert if reliable accounting never materializes.
            one(api,st,'market/accounting_stale/'+key,
                '🚨 Market SELL | '+name+' | net PnL не подтверждён за 24ч')
            del items[key]


def partial_scan(api,st,names):
    """Persist observed partials across monitor polls; no transient fill spam."""
    now=time.time()
    active=st.setdefault('partial_tracking',{})
    current=set()
    for name,root in names.items():
        cfg=read_json(root/'config.json')
        statepath=root/'state/bot04_state.json'
        try:
            # A stale state is not an authoritative live partial fill.
            if now-statepath.stat().st_mtime>30:
                continue
        except OSError:
            continue
        state=read_json(statepath)
        for cycle in state.get('cycles',{}).values():
            buyid=str(cycle.get('ordId') or '')
            filled_buy=number(cycle.get('filledBuy'))
            buy_size=number(cycle.get('sz'))
            if (cycle.get('state')=='BUY_PARTIAL' and buyid and
                filled_buy is not None and buy_size is not None and
                0<filled_buy<buy_size):
                key=f'{name}/BUY/{buyid}'
                current.add(key)
                since=float(cycle.get('partial_since') or 0)
                active.setdefault(key, since if 0<since<=now else now)
                if now-active[key]>=2:
                    one(api,st,'partial/'+key,
                        f'⚠️ PARTIAL BUY | {name}\n'+
                        f'{cycle.get("filledBuy")}/{cycle.get("sz")} контр. | {cycle.get("buyPx")}')
            for rec in cycle.get('tpOrders',[]) or []:
                oid=str(rec.get('ordId') or rec.get('clOrdId') or '')
                filled=number(rec.get('filled'))
                sz=number(rec.get('sz'))
                if (oid and filled is not None and sz and
                    0<filled<sz and rec.get('phase')=='ACTIVE'):
                    key=f'{name}/TP/{oid}'
                    current.add(key)
                    active.setdefault(key,now)
                    if now-active[key]>=2:
                        one(api,st,'partial/'+key,
                            f'⚠️ PARTIAL SELL TP | {name}\n{filled}/{sz} контр.')
    for key in list(active):
        if key not in current:
            del active[key]


def zero_buy_scan(api,st,names):
    """Detect 0/N from FRESH engine journal, never delayed state.json.
    Uses latest sample per bot in last 15s. Normal pauses/WAITING excluded.
    """
    now=time.time()
    incidents=st.setdefault('buy_zero',{})
    for name,root in names.items():
        # Unlike latency diagnostics, only pull last 25 seconds.
        last=None
        for line in journal(name,25/3600):
            match=COUNTS.search(line)
            mode=MODE.search(line)
            if match and mode:
                timestamp=date_epoch(line.split(' ',1)[0])
                if timestamp and timestamp>=now-15:
                    buy,target,_,_=map(int,match.groups())
                    eligible=(mode.group(1)=='RUN' and 'WAITING' not in line
                              and 'VIP fee unavailable' not in line)
                    last=(timestamp,buy,target,eligible)
        if last is None or (root/'state/control.json').exists() and read_json(root/'state/control.json').get('mode') in ('PAUSE','SOFT','HARD','PANIC'):
            continue
        ts,buy,target,eligible=last
        if not eligible or Path('/var/lib/okxbot/account_soft_stop.json').exists():
            incidents.pop(name,None)  # expected mode, not an unresolved 0/N incident
            continue
        prior=incidents.get(name)
        if buy==0 and not prior:
            inc={'since':ts,'target':target}
            # Store incident identity, so restarts don't resend 0/N.
            one(api,st,f'zero/{name}/{ts}',f'🔴 BUY 0/{target} | {name}\nСетка без BUY LIMIT!')
            incidents[name]=inc
        elif buy>=target and prior:
            duration=max(0,ts-float(prior['since']))
            one(api,st,f'zero_restored/{name}/{prior["since"]}',
                f'🟢 BUY {target}/{target} | {name}\nВосстановление ≈{duration:.2f} с')
            del incidents[name]


def _state_mismatch_alert(api,st,name,obj):
    """Open one incident per unresolved mismatch, ignoring observer re-alerts."""
    incidents=st.setdefault('state_mismatch_incidents',{})
    ts=date_epoch(obj.get('ts')) or time.time()
    rec=incidents.get(name)
    if rec is not None:
        # One persistent mismatch. Invalidate recovery streak if another alert occurs.
        rec['last_alert_at']=max(float(rec.get('last_alert_at',ts)),ts)
        rec['matches']=0
        rec['last_match_ts']=None
        return
    try:
        duration=max(0.,min(3600.,float(obj.get('duration_sec') or 0.)))
    except (ValueError,TypeError):
        duration=0.
    state_buy=obj.get('state_buy')
    engine_buy=obj.get('engine_buy')
    rec={'since':ts-duration,'last_alert_at':ts,'matches':0,
         'last_match_ts':None,'state_buy':state_buy,'engine_buy':engine_buy}
    # Don't store an active incident unless Telegram accepted its alert.
    one(api,st,f'state_mismatch/open/{name}/{ts}',
        f'⚠️ STATE MISMATCH | {name}\n'
        f'State: {state_buy} BUY | Engine: {engine_buy} BUY\n'
        f'Расхождение: {duration:.1f} с')
    incidents[name]=rec


def _state_mismatch_check_recovery(api,st,names):
    """Check actual paired observer measurements, never silence as recovery.

    Observer v0.7.14 writes summary.json every ~20s. Its `last_state`
    is enriched IN MEMORY with engine_buy/engine_observed_ts; unlike
    snapshots.jsonl, which is written before adding engine data.
    Three distinct fresh, consecutive matching summary samples required.
    """
    incidents=st.setdefault('state_mismatch_incidents',{})
    now=time.time()
    for name in list(incidents):
        if name not in names:
            # Do not claim recovery from an inactive process or missing observer.
            continue
        rec=incidents[name]
        root=names[name]
        paths=list(root.glob('diagnostics/execution_*/summary.json'))
        if not paths:
            rec['matches']=0
            continue
        try:
            newest=max(paths,key=lambda p:p.stat().st_mtime)
            summary=read_json(newest)
            if summary.get('mode')!='execution':
                rec['matches']=0; continue
            sample=summary.get('last_state')
            if not isinstance(sample,dict):
                rec['matches']=0; continue
            summary_ts=date_epoch(summary.get('ts'))
            sample_ts=date_epoch(sample.get('ts'))
            engine_ts=date_epoch(sample.get('engine_observed_ts'))
            if (summary_ts is None or sample_ts is None or engine_ts is None
                or not (-2<=now-summary_ts<=45)
                or not (-2<=now-sample_ts<=35)
                or abs(sample_ts-engine_ts)>6
                or sample_ts<=float(rec.get('last_alert_at',0))):
                rec['matches']=0;continue
            # Refuse unknown/stale engine reports; require numeric equality.
            state_buy=sample.get('buy')
            engine_buy=sample.get('engine_buy')
            if (not isinstance(state_buy,int) or isinstance(state_buy,bool)
                or not isinstance(engine_buy,int) or isinstance(engine_buy,bool)
                or state_buy<0 or engine_buy<0 or state_buy!=engine_buy):
                rec['matches']=0
                rec['last_match_ts']=None
                continue
            last=rec.get('last_match_ts')
            if last is not None and sample_ts<=float(last):
                continue  # same observer sample on the next Telegram poll
            if last is not None and sample_ts-float(last)>45:
                rec['matches']=0  # missed too many summary intervals
            rec['last_match_ts']=sample_ts
            rec['matches']=int(rec.get('matches',0))+1
            if rec['matches']>=3:
                duration=max(0.,sample_ts-float(rec['since']))
                one(api,st,f'state_mismatch/normal/{name}/{rec["since"]}',
                    f'✅ BACK TO NORMAL | {name}\n'
                    f'State и Engine снова совпадают\n'
                    f'Длительность инцидента: {duration:.1f} с')
                del incidents[name]
        except (OSError,TypeError,ValueError,OverflowError):
            rec['matches']=0
            # Next monitoring iteration can retry reading a new summary.
            continue


def observer_fault_scan(api,st,names):
    offsets=st.setdefault('observer_offsets',{})
    for name,root in names.items():
        for path in root.glob('diagnostics/execution_*/alerts.jsonl'):
            token=str(path)
            try:
                size=path.stat().st_size
                start=offsets.get(token)
                if start is None:
                    offsets[token]=size
                    continue
                if start>size:start=0
                with path.open('rb') as f:
                    f.seek(start)
                    while True:
                        row=f.readline()
                        if not row or not row.endswith(b'\n'):break
                        obj=json.loads(row)
                        kind=obj.get('kind')
                        if kind=='PERSISTED_STATE_DIFFERS_FROM_ENGINE':
                            _state_mismatch_alert(api,st,name,obj)
                        elif kind in OBSERVER_ALARMS:
                            one(api,st,'observer/'+name+'/'+str(kind)+'/'+str(obj.get('event_ts') or obj.get('ts')),
                                f'🚨 {kind} | {name}')
                        offsets[token]=f.tell()
            except (OSError,ValueError,KeyError):
                continue
    _state_mismatch_check_recovery(api,st,names)
