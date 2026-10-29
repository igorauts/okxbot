from okx_telegram_wire import state_key
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
from okx_paths import state_file, fills_db, observer_dir
from okx_telegram_delivery import enqueue

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
    'EMERGENCY_POSITIONS_GATE_BLOCKED', 'EMERGENCY_POSITIONS_READ',
    'COVERAGE_RECOVERY_WAIT','ATTACHED_TP_REJECTED',
    'TP_PRICE_LIMIT_UNAVAILABLE','TP_FAILURE_BUY_HOLD',
    'PANIC_MARKET_REJECTED','PANIC_RETRY_EXHAUSTED',
    'EMERGENCY_POSITION_UNRESOLVED','EMERGENCY_BUY_IDENTITY_CONFLICT',
}
OBSERVER_ALARMS = {'TP_HARD_CAP_REACHED','TP_BUFFER_OVERFLOW','STATE_READ_FAILED',
                   'RECONCILIATION_LONG_WAIT'}


def one(api, st, key, msg, *, html_mode=False):
    """Persist-before-send; delivered asynchronously by the main monitor loop."""
    return enqueue(st, key, msg, html_mode=html_mode)


def event_key(name, obj):
    typ = str(obj.get('event', ''))
    identity = (obj.get('ordId') or obj.get('clOrdId') or obj.get('code') or
                obj.get('reason') or obj.get('phase') or typ)
    return f'event/{name}/{typ}/{identity}'


# A brief unavailable /positions FIFO gate normally resolves on the next REST
# reconciliation. Hold ONLY this warning for a bounded interval so Telegram
# does not deliver a stale danger signal after the engine has recovered.
# Other CRITICAL events and systemd faults keep their existing urgency.
GATE_ALERT_GRACE_SEC = 15.0
GATE_EVENT = 'ACCOUNT_POSITIONS_GATE_DEGRADED'


def gate_alert_scan(api, st, now=None):
    """Flush matured unresolved gate faults; durable state is saved by caller.

    The user is notified if the problem lasts beyond the short grace period.
    An explicit reconciliation recovery clears it even after an interruption
    of the Telegram monitor. No trading state or FIFO file is touched.
    """
    now = time.time() if now is None else float(now)
    pending = st.setdefault(state_key('positions_gate_pending'), {})
    counters = st.setdefault(state_key('positions_gate_episode'), {})
    for name, record in list(pending.items()):
        if record.get('sent') or now - float(record['received_at']) < GATE_ALERT_GRACE_SEC:
            continue
        number = int(counters.get(name, 0)) + 1
        key = f'event/{name}/{GATE_EVENT}/{number}'
        error = str(record.get('error') or 'GateUnavailable')[:120]
        # The queue is durable; do not mark the record sent before enqueueing.
        one(api, st, key, f'🚨 {GATE_EVENT} | {name}\nerror: {error}\n'
            'Шлюз /positions недоступен более 15 с; проверка позиции ожидается')
        counters[name] = number
        record['sent'] = True
        record['episode'] = number


def _gate_recovered(api, st, name, obj):
    """Close a pending gate episode only on a subsequent explicit recovery."""
    pending = st.setdefault(state_key('positions_gate_pending'), {})
    record = pending.get(name)
    if not record:
        return
    # Events are processed in journal order, but a recovery can carry an
    # exchange-generated timestamp earlier than the fault. Do not let that
    # stale recovery silently cancel a newer gate failure.
    start = record.get('event_ts')
    ended = date_epoch(obj.get('ts'))
    if start and ended and ended + 0.5 < start:
        return
    if record.get('sent'):
        number = record.get('episode')
        alarm_key = f'event/{name}/{GATE_EVENT}/{number}'
        outbox = st.setdefault(state_key('alert_outbox'), [])
        # Delivery is handled in this very same single-threaded monitor loop.
        # If Telegram had not actually received the alarm, do not deliver a
        # stale alarm *after* reconciliation has already recovered.
        if any(x.get('key') == alarm_key for x in outbox):
            outbox[:] = [x for x in outbox if x.get('key') != alarm_key]
            st.setdefault('incident_sent', {}).pop(alarm_key, None)
        else:
            one(api, st, f'recovery/{name}/{GATE_EVENT}/{number}',
                f'✅ ACCOUNT_POSITIONS_GATE_RECOVERED | {name}\n'
                'Свежая REST-сверка восстановлена')
    del pending[name]


RECOVERY_EVENTS = {
    'RECONCILIATION_RECOVERED': ('RECONCILIATION_LONG_WAIT',),
    'ACCOUNT_POSITIONS_GATE_RECOVERED': ('ACCOUNT_POSITIONS_GATE_DEGRADED',
                                         'EMERGENCY_POSITIONS_GATE_BLOCKED',
                                         'EMERGENCY_POSITIONS_READ'),
}


def event_alert(api, st, name, obj):
    typ = str(obj.get('event') or '')
    episodes = st.setdefault(state_key('event_incidents'), {})
    counters = st.setdefault(state_key('event_incident_counters'), {})
    now = time.time()
    if typ == GATE_EVENT:
        pending = st.setdefault(state_key('positions_gate_pending'), {})
        if name not in pending:
            pending[name] = {'received_at': now, 'event_ts': date_epoch(obj.get('ts')),
                             'error': str(obj.get('error') or 'GateUnavailable'),
                             'sent': False}
        return
    if typ in ('RECONCILIATION_RECOVERED', 'ACCOUNT_POSITIONS_GATE_RECOVERED'):
        _gate_recovered(api, st, name, obj)
    if typ in RECOVERY_EVENTS:
        cleared = []
        for key in list(episodes):
            if any(key.startswith('event/' + name + '/' + fault + '/')
                   for fault in RECOVERY_EVENTS[typ]):
                cleared.append(key)
                del episodes[key]
        if cleared:
            one(api, st, 'recovery/' + name + '/' + typ + '/' + str(int(now)),
                '✅ ' + typ + ' | ' + name + '\nПодтверждено движком')
        return
    if typ not in EVENT_ALARMS:
        return
    base = event_key(name, obj)
    item = episodes.get(base)
    # The same fault continuously emitted by an engine is ONE incident.
    # A new occurrence after 30 minutes without that event is a new incident.
    if item and 0 <= now - float(item.get('last', 0)) < 1800:
        item['last'] = now
        return
    episode = int(counters.get(base, 0)) + 1
    old_item = episodes.get(base)
    counters[base] = episode
    episodes[base] = {'episode': episode, 'last': now}
    extras = []
    for k in ('code', 'ordId', 'reason', 'message', 'remaining', 'remaining_position'):
        val = obj.get(k)
        if val is not None:
            extras.append(f'{k}: {str(val)[:120]}')
    try:
        one(api, st, base + '/incident/' + str(episode),
            f'🚨 {typ} | {name}\n' + '\n'.join(extras))
    except BaseException:
        counters[base] = episode - 1
        if old_item is None:
            episodes.pop(base, None)
        else:
            episodes[base] = old_item
        raise


def market_event(st,name,obj):
    typ=obj.get('event')
    cid=str(obj.get('clOrdId') or '')
    if not cid and typ!='AGG_MARKET_REJECTED':
        return
    records=st.setdefault('market_events',{})
    key=f'{name}/{cid}'
    if typ=='AGG_MARKET_REJECTED' and not cid:
        # Rejection can carry no clOrdId. Only one unresolved aggregate
        # market intent can exist per bot; link to that record, not a phantom.
        pending=[k for k,v in records.items() if k.startswith(name+'/')
                 and v.get('intent_at') and not v.get('terminal') and not v.get('rejected')]
        key=max(pending,key=lambda k:records[k]['intent_at']) if pending else name+'/rejected/'+str(obj.get('ts'))
    item=records.setdefault(key,{})
    ts=date_epoch(obj.get('ts'))
    if typ=='AGG_MARKET_INTENT':
        item['intent_at']=ts
        item['cycles']=obj.get('cycles')
        for field in ('tp_to_intent_ms','cross_to_collect_ms',
                      'collect_to_intent_ms','additional_cycles','cross_source',
                      'added_during_window','added_after_window','added_unclassified'):
            if field in obj:
                item[field]=obj[field]
    elif typ=='AGG_MARKET_ACK':
        item['ack_at']=ts
        item['order_id']=obj.get('ordId')
        if 'intent_to_submit_ms' in obj:
            item['intent_to_submit_ms']=obj['intent_to_submit_ms']
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
        value=item.get('tp_to_intent_ms')
        if isinstance(value,(float,int)) and 0<=value<=3600000:
            times['tp_to_intent_ms']=value
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
            db=fills_db(root)
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
        statepath=state_file(root)
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


def zero_buy_scan(api, st, names):
    """One bounded journalctl for all active units; preserve original 0/N policy."""
    now = time.time()
    incidents = st.setdefault('buy_zero', {})
    if not names:
        return
    units = {'okx-grid-' + name + '.service': name for name in names}
    cmd = ['journalctl', '--no-pager', '-o', 'json',
           '--since', '25 seconds ago', '-g', 'РЕЖИМ=']
    for unit in units:
        cmd.extend(['-u', unit])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return
    if proc.returncode:
        return
    latest = {}
    for line in proc.stdout.splitlines():
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            unit = str(record.get('_SYSTEMD_UNIT') or '')
            name = units.get(unit)
            if name is None:
                continue
            message = str(record.get('MESSAGE') or '')
            match = COUNTS.search(message)
            mode = MODE.search(message)
            if not (match and mode):
                continue
            sample_ts = int(record['__REALTIME_TIMESTAMP']) / 1_000_000
            if not now - 15 <= sample_ts <= now + 5:
                continue
            buy, target, _, _ = map(int, match.groups())
            eligible = (mode.group(1) == 'RUN' and
                        'WAITING' not in message and
                        'VIP fee unavailable' not in message)
            if name not in latest or sample_ts >= latest[name][0]:
                latest[name] = (sample_ts, buy, target, eligible)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    for name, root in names.items():
        last = latest.get(name)
        if last is None:
            continue
        control = root / 'state/control.json'
        if control.exists() and read_json(control).get('mode') in ('PAUSE','SOFT','HARD','PANIC'):
            continue
        sample_ts, buy, target, eligible = last
        if not eligible or Path('/var/lib/okxbot/account_soft_stop.json').exists():
            incidents.pop(name, None)
            continue
        prior = incidents.get(name)
        if buy == 0 and not prior:
            one(api, st, f'zero/{name}/{sample_ts}',
                f'🔴 BUY 0/{target} | {name}\nСетка без BUY LIMIT!')
            incidents[name] = {'since': sample_ts, 'target': target}
        elif buy >= target and prior:
            duration = max(0, sample_ts - float(prior['since']))
            one(api, st, f'zero_restored/{name}/{prior["since"]}',
                f'🟢 BUY {target}/{target} | {name}\nВосстановление ≈{duration:.2f} с')
            del incidents[name]


def _discard_nonactionable_mismatch_notifications(st):
    """Remove only legacy diagnostic mismatch notifications (including queued).

    Local observer alerts.jsonl/summary.json are never modified. Real trading
    incidents, zero BUY, stale observers and PANIC alarms remain untouched.
    Called before durable alert delivery by the main Telegram monitor loop.
    """
    changed=bool(st.pop('state_mismatch_incidents',None))
    queue=st.get(state_key('alert_outbox'))
    if isinstance(queue,list):
        keep=[row for row in queue if not (isinstance(row,dict) and
              str(row.get('key','')).startswith(('state_mismatch/open/',
                                                 'state_mismatch/normal/')))]
        if len(keep)!=len(queue):
            queue[:]=keep
            changed=True
    return changed


def observer_fault_scan(api,st,names):
    _discard_nonactionable_mismatch_notifications(st)
    offsets=st.setdefault('observer_offsets',{})
    for name,root in names.items():
        for path in ([observer_dir(root,'execution')/'alerts.jsonl'] if (observer_dir(root,'execution')/'alerts.jsonl').is_file() else list(root.glob('diagnostics/execution_*/alerts.jsonl'))):
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
                        if kind in OBSERVER_ALARMS:
                            one(api,st,'observer/'+name+'/'+str(kind)+'/'+str(obj.get('event_ts') or obj.get('ts')),
                                f'🚨 {kind} | {name}')
                        offsets[token]=f.tell()
            except (OSError,ValueError,KeyError):
                continue
