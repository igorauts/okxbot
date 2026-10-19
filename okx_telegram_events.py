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
                        if obj.get('kind') in OBSERVER_ALARMS:
                            one(api,st,'observer/'+name+'/'+str(obj.get('kind'))+'/'+str(obj.get('event_ts') or obj.get('ts')),
                                f'🚨 {obj["kind"]} | {name}')
                        offsets[token]=f.tell()
            except (OSError,ValueError,KeyError):
                continue
