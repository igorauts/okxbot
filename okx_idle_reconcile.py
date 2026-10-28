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
