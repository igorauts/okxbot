"""Minimal five-second audit wait: no exchange operations or trading decisions.

The trading loop retains existing short safety waits and hard financial checks.
Only QUIET idle passes use this local wait, without a REST call during polling.
"""
import hashlib
import time


def audit_jitter_seconds(bot_name, instrument):
    """Deterministic 0..0.8 second per-bot jitter, reproducible in tests."""
    digest=hashlib.sha256((str(bot_name)+':'+str(instrument)).encode()).digest()
    return int.from_bytes(digest[:4],'big')/2**32*0.8


def aligned_idle_audit_wait(wall_now, period=5.0):
    """Bounded wall-clock phase alignment for QUIET idle audit only.

    Never caches positions, never changes wake/controls/urgent deadlines.
    Returns 0.1..period seconds; if wall clock is invalid fail to periodic
    audit without aligning. Used only by the opt-in OFFLINE experiment.
    """
    import math
    if not (isinstance(period,(int,float)) and math.isfinite(period)
            and 0.1 <= period <= 60.0):
        raise ValueError('invalid independent audit period')
    if not (isinstance(wall_now,(int,float)) and math.isfinite(wall_now)
            and wall_now >= 0):
        return float(period)
    remaining = period - (float(wall_now) % period)
    # Avoid busy-poll bursts just ahead of the next shared boundary.
    if remaining < .1:
        remaining = .1
    return min(float(period), remaining)

def idle_deadline(*, now, buy_over_target, buy_shift_at, buy_cooldown,
                  tp_over_target, tp_quiet_since, tp_cooldown,
                  tp_last_cancel_at, tp_cancel_gap, partial_expiries, wall_now):
    """Earliest EXISTING financial housekeeping deadline, never overwrite it.

    Returns monotonic deadline or None. Wall-time partial expiry is converted
    to monotonic once at selection, so this loop never sleeps through a due
    partial cleanup. No additional recheck cadence when WS unhealthy.
    """
    candidates=[]
    if buy_over_target:
        candidates.append(buy_shift_at+buy_cooldown)
    if tp_over_target and tp_quiet_since is not None:
        candidates.append(tp_quiet_since+tp_cooldown)
        if tp_last_cancel_at is not None:
            candidates.append(tp_last_cancel_at+tp_cancel_gap)
    for expiry in partial_expiries:
        candidates.append(now+max(0.,expiry-wall_now))
    # Elapsed deadlines do NOT force endless immediate GETs when cleanup is
    # blocked by safety conditions. Next idle audit/WS event may try again.
    future=[deadline for deadline in candidates if deadline>now+0.001]
    return min(future) if future else None


def wait_for_idle_reconcile(*, wake, control_read, original_control,
                            ws_health, initial_ws_health, extra_due,
                            max_wait_seconds, local_poll_seconds=.2,
                            monotonic=time.monotonic):
    if not .05<=local_poll_seconds<=.5:
        raise ValueError('invalid local control poll')
    if not .1<=max_wait_seconds<=60:
        raise ValueError('invalid audit interval')
    due=monotonic()+max_wait_seconds
    if extra_due is not None: due=min(due,extra_due)
    while True:
        if control_read()!=original_control: return 'control'
        if ws_health()!=initial_ws_health: return 'ws_health'
        if wake.is_set(): return 'wake'
        remaining=due-monotonic()
        if remaining<=0: return 'timer'
        wake.wait(min(local_poll_seconds,remaining))


def wait_error_backoff(*, control_read, original_control, seconds=3.0,
                       local_poll_seconds=.2, monotonic=time.monotonic):
    """Preserve the existing recovery REST cadence, but never delay PANIC.

    No WS wake can trigger an extra GET during exchange errors. Only a
    control-mode change interrupts backoff; no REST is performed here.
    """
    if not .05<=local_poll_seconds<=.5 or not .1<=seconds<=60:
        raise ValueError('invalid backoff')
    deadline=monotonic()+seconds
    while True:
        if control_read()!=original_control:
            return 'control'
        remaining=deadline-monotonic()
        if remaining<=0:
            return 'backoff'
        time.sleep(min(local_poll_seconds,remaining))


def classify_background_candidate_after_orders(*, trigger_source, mode,
        orders_complete, facts_changed, unresolved, pending_intents,
        protective_deadline_due, private_or_virtual_or_grid_wake,
        ws_healthy, control_changed, external_uncertainty):
    """OFFLINE B1 shadow policy: never changes a Reader priority or trading result.

    Must be evaluated AFTER orders-pending and the complete order/fill proof.
    An incomplete or unobservable condition is NOT eligible for BACKGROUND.
    All defaults are deny-by-default: callers must supply every positive proof.
    This candidate is not yet wired into the LIVE engine.
    """
    # No truthiness inference: None/0/1/string are NOT verified booleans.
    proofs=(orders_complete,facts_changed,unresolved,pending_intents,
            protective_deadline_due,private_or_virtual_or_grid_wake,
            ws_healthy,control_changed,external_uncertainty)
    if any(type(value) is not bool for value in proofs):
        return False, 'unverified_fact_flag'
    if trigger_source != 'timer':return False, 'non_timer'
    if mode != 'RUN':return False, 'non_run'
    if not orders_complete:return False, 'incomplete_orders'
    if facts_changed:return False, 'new_exchange_fact'
    if unresolved:return False, 'unresolved_exchange_state'
    if pending_intents:return False, 'durable_intent_pending'
    if protective_deadline_due:return False, 'protective_deadline'
    if private_or_virtual_or_grid_wake:return False, 'event_wake'
    if not ws_healthy:return False, 'ws_degraded'
    if control_changed:return False, 'control_changed'
    if external_uncertainty:return False, 'external_uncertainty'
    return True, 'quiet_after_verified_orders'


def b1_shadow_pending_intents(st):
    """Conservative shadow-only uncertainty detection; never authorizes trading.

    Iterates historical cycles only on a sampled timer (<= once / 30s per bot).
    Unknown state or missing evidence is an intent blocker, never a false proof.
    """
    if not isinstance(st,dict) or not isinstance(st.get('cycles'),dict):return True
    if st.get('panic_orders'):return True
    groups=st.get('aggregate_exits',[])
    if not isinstance(groups,list):return True
    for group in groups:
        if not isinstance(group,dict):return True
        if group.get('phase') not in ('TERMINAL','DONE','REJECTED','FILLED','COMPLETED'):
            return True
    for c in st['cycles'].values():
        if not isinstance(c,dict):return True
        if c.get('state') not in ('CLOSED','CANCELED','EXITED','BUY_LIVE',
                                  'VIRTUAL_TP','TP_WAIT','TP_PARTIAL'):
            return True
        recs=c.get('tpOrders')
        if not isinstance(recs,list):return True
        for r in recs:
            if not isinstance(r,dict):return True
            if r.get('phase') in ('PLACING','CANCELING','PENDING'):
                return True
    return False

def b1_shadow_probe(*, source, last_at, state, mode, changed, resolved,
                    deadline, watcher, market, ws_epoch, control_read, reporter,
                    monotonic=time.monotonic, perf_counter_ns=time.perf_counter_ns):
    """Best-effort sampled observation; no control over trading or GETs.

    Any failure is reported as diagnostic, never treated as a quiet proof.
    All source-dependent branches are outside the trading engine.
    """
    now=monotonic()
    if source!='timer' or now-last_at<30.0:return last_at
    try:
        start=perf_counter_ns()
        intents=b1_shadow_pending_intents(state)
        waiting=(watcher.wake.is_set() or watcher.private_wake.is_set()
                 or not watcher.events.empty())
        ws_ok=(watcher.healthy() and market.healthy() and
               (watcher.connection_epoch,market.connection_epoch)==ws_epoch)
        current_control=control_read()
        eligible,reason=classify_background_candidate_after_orders(
            trigger_source=source,mode=mode,orders_complete=True,
            facts_changed=bool(changed),unresolved=not resolved,
            pending_intents=intents,
            protective_deadline_due=(deadline is not None and deadline<=monotonic()+1.0),
            private_or_virtual_or_grid_wake=bool(waiting),ws_healthy=bool(ws_ok),
            control_changed=(current_control!=mode),external_uncertainty=not resolved)
        reporter('b1_shadow',(perf_counter_ns()-start)/1e6,
                 source='timer',reason=reason,outcome='eligible' if eligible else 'denied')
    except Exception as exc:
        try:reporter('b1_shadow',0,source='timer',reason='observation_error',
                     outcome=type(exc).__name__)
        except Exception:pass
    return now

