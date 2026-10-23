"""Read-only systemd / engine / observer health for Telegram v0.7.22.

Never restarts a service, writes bot state, or accesses the OKX exchange.
Systemd journal cursors let a fast restart be observed between 5s polls.
"""
import json
import subprocess
import time
from pathlib import Path
from okx_paths import observer_dir
from okx_telegram_events import one


def _run(cmd, timeout=5):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          check=False)


def _show(unit):
    try:
        p = _run(['systemctl', 'show', unit, '--no-pager',
                  '-p', 'ActiveState', '-p', 'SubState', '-p', 'InvocationID',
                  '-p', 'MainPID', '-p', 'ActiveEnterTimestampMonotonic'], timeout=4)
        if p.returncode:
            return None
        return dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _journal(unit, cursor=None):
    # Journal is the source of truth for individual state transitions.
    cmd = ['journalctl', '-u', unit, '-o', 'json', '--no-pager', '-n', '1000']
    if cursor:
        cmd += ['--after-cursor', cursor]
    try:
        p = _run(cmd, timeout=8)
        if p.returncode:
            return None
        rows = []
        for line in p.stdout.splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except ValueError:
                continue
        return rows
    except (OSError, subprocess.TimeoutExpired):
        return None


def _action(row):
    # Do not mistake the bot's own journal messages for systemd lifecycle.
    if str(row.get('_PID', '')) != '1' and row.get('SYSLOG_IDENTIFIER') != 'systemd':
        return None
    msg = str(row.get('MESSAGE') or '')
    if msg.startswith(('Stopped ', 'Stopping ')):
        return 'stop' if msg.startswith('Stopped ') else None
    if msg.startswith('Started '):
        return 'start'
    return None


def service_check(api, st, services, roots):
    recs = st.setdefault('systemd_watch_v0722', {})
    for name, unit in services.items():
        props = _show(unit)
        if props is None:
            continue  # Unknown status must not be reported as STOPPED.
        active = props.get('ActiveState') == 'active'
        inv = props.get('InvocationID') or ''
        previous = recs.get(name)
        if previous is None:
            history = _journal(unit)
            recs[name] = {'active': active, 'invocation': inv,
                          'cursor': history[-1].get('__CURSOR') if history else None}
            continue  # Initial install: no historical startup/stop flood.
        old_active = bool(previous.get('active'))
        old_inv = str(previous.get('invocation') or '')
        cursor = previous.get('cursor')
        history = _journal(unit, cursor) if cursor else _journal(unit)
        transitions = []
        if history is not None:
            if not cursor:
                # Journal unavailable on prior loop: don't replay history.
                previous['cursor'] = history[-1].get('__CURSOR') if history else None
            else:
                for row in history:
                    marker = row.get('__CURSOR')
                    action = _action(row)
                    if action:
                        transitions.append((action, marker or str(row.get('__REALTIME_TIMESTAMP'))))
                if history:
                    previous['cursor'] = history[-1].get('__CURSOR')
        # One systemd restart can happen entirely between monitor polls.
        if active and old_active and inv and old_inv and inv != old_inv:
            if not any(x[0] == 'stop' for x in transitions):
                transitions.insert(0, ('stop', 'invocation/' + old_inv))
            if not any(x[0] == 'start' for x in transitions):
                transitions.append(('start', 'invocation/' + inv))
        elif active and not old_active and not any(x[0] == 'start' for x in transitions):
            transitions.append(('start', 'invocation/' + (inv or str(time.time_ns()))))
        elif old_active and not active and not any(x[0] == 'stop' for x in transitions):
            transitions.append(('stop', 'invocation/' + (old_inv or str(time.time_ns()))))
        root = Path(roots[name])
        # Read independently: an absent control.json means RUN to the engine,
        # while control_ack.json may validly exist and say RUNNING.  Malformed
        # or unreadable existing control files must NEVER imply RUN.
        try:
            ack = json.loads((root / 'state/control_ack.json').read_text())
            if not isinstance(ack, dict):
                ack = {}
        except (OSError, ValueError):
            ack = {}
        try:
            control = json.loads((root / 'state/control.json').read_text())
            if (not isinstance(control, dict) or
                    control.get('mode') not in ('RUN', 'PAUSE', 'SOFT', 'HARD', 'PANIC')):
                control = {}
        except FileNotFoundError:
            # Match okx_bot.control_mode() exactly for the missing-file case.
            control = {'mode': 'RUN'}
        except (OSError, ValueError):
            control = {}
        expected_terminal = (ack.get('phase') in ('PANIC_DONE', 'RETIRED') or
                             control.get('mode') in ('HARD', 'PANIC'))
        now = time.time()
        pending = previous.setdefault('pending_starts', [])
        for kind, identity in transitions:
            if kind == 'stop' and expected_terminal:
                continue
            if kind == 'stop':
                one(api, st, f'systemd/stop/{name}/{identity}',
                    f'⛔ {name}: STOPPED (systemd)')
            else:
                pending.append({'id': identity, 'since': now})
        # Only claim RUN after a fresh post-start heartbeat AND ack. Do not
        # conflate an active systemd process with a running grid strategy.
        for item in list(pending):
            since = float(item['since'])
            hb = root / 'state/runtime_stats.json'
            try:
                stats = json.loads(hb.read_text())
                last = float(stats['last'])
                fresh = (since <= last <= now+5 and now-last <= 35 and
                         hb.stat().st_mtime >= since)
            except (OSError, ValueError, TypeError, KeyError):
                fresh = False
            if active and fresh and ack.get('phase') == 'RUNNING' and control.get('mode') == 'RUN':
                one(api, st, f'systemd/start/{name}/{item["id"]}',
                    f'✅ {name}: STARTED — RUN (systemd + fresh engine heartbeat)')
                pending.remove(item)
            elif active and fresh and ack.get('phase') in ('PAUSED', 'SOFT_STOPPED'):
                one(api, st, f'systemd/start/{name}/{item["id"]}',
                    f'✅ {name}: STARTED — {ack["phase"]} (systemd + engine heartbeat)')
                pending.remove(item)
            elif now-since > 60:
                one(api, st, f'systemd/start/unconfirmed/{name}/{item["id"]}',
                    f'⚠️ {name}: STARTED в systemd, но RUN не подтверждён за 60 с')
                pending.remove(item)
        previous['active'], previous['invocation'] = active, inv


# Debounced health alerts. A single late/missing telemetry sample is not a fault.
# The trading engine and observer subprocesses remain fully independent of this
# read-only reporter. Stable timestamps are persisted by the Telegram monitor.
HEARTBEAT_STALE_SEC = 45
HEARTBEAT_CONFIRM_SEC = 15
OBSERVER_STALE_SEC = 90
OBSERVER_CONFIRM_SEC = 60
OBSERVER_INACTIVE_CONFIRM_SEC = 10
RECOVERY_CONFIRM_SEC = 15
RECOVERY_MIN_SAMPLES = 3
_HEALTH_RULE = 'debounced-v0722-1'


def _health_record(watches, key, now, invocation=None):
    rec = watches.get(key)
    if not isinstance(rec, dict) or rec.get('health_rule') != _HEALTH_RULE:
        # Old v0.7.22 records (including a spurious active alert) cannot trigger
        # an immediate new alarm/recovery on upgrading this monitor.
        rec = {'health_rule': _HEALTH_RULE, 'fault': None, 'bad_since': None,
               'bad_count': 0, 'good_since': None, 'good_count': 0}
        watches[key] = rec
    if invocation is not None and rec.get('invocation') != invocation:
        # New process: its previous heartbeat is not evidence of a hang.
        rec.update({'invocation': invocation, 'fault': None,
                    'bad_since': None, 'bad_count': 0,
                    'good_since': None, 'good_count': 0})
    return rec


def _observe_fault(rec, reason, now):
    """Return duration of uninterrupted bad readings, or None for healthy."""
    if reason is None:
        rec['bad_since'] = None
        rec['bad_count'] = 0
        return None
    since = rec.get('bad_since')
    if (since is None or now < float(since) or
            reason != rec.get('bad_reason')):
        rec['bad_since'] = now
        rec['bad_count'] = 1
        rec['bad_reason'] = reason
        rec['good_since'] = None
        rec['good_count'] = 0
        return 0.0
    rec['bad_count'] = int(rec.get('bad_count', 0)) + 1
    rec['good_since'] = None
    rec['good_count'] = 0
    return now - float(since)


def _confirmed_recovery(rec, now):
    if rec.get('fault') is None:
        rec['good_since'] = None
        rec['good_count'] = 0
        return False
    since = rec.get('good_since')
    if since is None or now < float(since):
        rec['good_since'] = now
        rec['good_count'] = 1
        return False
    rec['good_count'] = int(rec.get('good_count', 0)) + 1
    return (rec['good_count'] >= RECOVERY_MIN_SAMPLES and
            now - float(since) >= RECOVERY_CONFIRM_SEC)


def engine_heartbeat_check(api, st, services, roots, now=None):
    now = time.time() if now is None else now
    watches = st.setdefault('engine_heartbeat_v0722', {})
    just_failed=[]
    just_recovered=[]
    for name, unit in services.items():
        info = _show(unit)
        if info is None:  # systemd itself could not be queried: no speculation.
            continue
        active = info.get('ActiveState') == 'active'
        invocation = info.get('InvocationID') or ''
        rec = _health_record(watches, name, now, invocation)
        if not active:
            # Unexpected trade STOP/START has its own systemd journal alert.
            rec.update({'fault': None, 'bad_since': None, 'bad_count': 0,
                        'good_since': None, 'good_count': 0})
            continue
        path = Path(roots[name]) / 'state/runtime_stats.json'
        try:
            data = json.loads(path.read_text())
            stamp = float(data['last'])
            fresh = 0 <= now - stamp <= HEARTBEAT_STALE_SEC
        except (OSError, ValueError, TypeError, KeyError):
            stamp = None
            fresh = False
        reason = None if fresh else 'heartbeat stale'
        elapsed_bad = _observe_fault(rec, reason, now)
        if reason:
            if (rec.get('fault') is None and elapsed_bad is not None and
                    elapsed_bad >= HEARTBEAT_CONFIRM_SEC and
                    rec.get('bad_count', 0) >= 3):
                age = (f'{now-stamp:.0f} с' if stamp is not None and
                       0 <= now-stamp < 1e9 else 'нет свежего файла')
                just_failed.append((name,age))
                rec['fault'] = reason
        elif _confirmed_recovery(rec, now):
            just_recovered.append(name)
            rec.update({'fault': None, 'good_since': None, 'good_count': 0})

    # Read-only cross-bot correlation of confirmed stale heartbeats.
    # Send once per common episode, not once per poll. Never infer OKX outage.
    affected=tuple(sorted(name for name in services
                          if isinstance(watches.get(name),dict)
                          and watches[name].get('fault') == 'heartbeat stale'))
    correlated=st.setdefault('common_engine_stall_v0727',{})
    was_common=bool(correlated.get('active'))
    is_common=len(affected)>=2
    if is_common and not was_common:
        one(api, st, 'heartbeat/common/'+str(int(now)),
            '🚨 COMMON ENGINE STALL | '+', '.join(affected)+
            '\nНесколько ботов без свежего heartbeat. REST / сеть / VPS — причина не установлена.')
        correlated.update({'active':True,'members':list(affected),'since':now})
    elif not is_common and was_common:
        one(api, st, 'heartbeat/common/recovered/'+str(int(now)),
            '✅ COMMON ENGINE STALL: общая задержка heartbeat больше не подтверждается')
        correlated.update({'active':False,'members':[]})
    if not is_common and not was_common:
        # A genuinely isolated stall remains actionable; do not suppress it.
        for name,age in just_failed:
            one(api, st, f'heartbeat/down/{name}/{int(now)}',
                f'🚨 {name}: нет heartbeat движка ({age}), служба active')
        for name in just_recovered:
            one(api, st, f'heartbeat/recovery/{name}/{int(now)}',
                f'✅ BACK TO NORMAL | {name}\nHeartbeat торгового движка восстановлен')
    # When a common episode is/was active, one grouped alert and one grouped
    # recovery replace redundant per-bot notifications from the same episode.


def observers_check(api, st, services, roots, now=None):
    now = time.time() if now is None else now
    watches = st.setdefault('observers_health_v0722', {})
    for name, trade_unit in services.items():
        trade = _show(trade_unit)
        if not trade or trade.get('ActiveState') != 'active':
            continue
        for kind in ('burst', 'execution'):
            unit = f'observer-{trade_unit.removesuffix(".service")}-{kind}.service'
            token = f'{name}/{kind}'
            try:
                enabled = _run(['systemctl', 'is-enabled', unit], timeout=3).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                continue
            if enabled not in ('enabled', 'enabled-runtime'):
                watches.pop(token, None)  # Intentionally disabled: no alarm.
                continue
            info = _show(unit)
            if info is None:
                continue
            running = info.get('ActiveState') == 'active'
            summary = observer_dir(roots[name], kind) / 'summary.json'
            try:
                ts = summary.stat().st_mtime
                fresh = 0 <= now - ts <= OBSERVER_STALE_SEC
            except OSError:
                fresh = False
            reason = (None if running and fresh else
                      ('service inactive' if not running else 'measurements stale >90s'))
            # Do not reset a persistent stale-data fault when the observer crash-loops.
            rec = _health_record(watches, token, now)
            elapsed_bad = _observe_fault(rec, reason, now)
            if reason:
                minimum = (OBSERVER_INACTIVE_CONFIRM_SEC if not running else
                           OBSERVER_CONFIRM_SEC)
                if (elapsed_bad is not None and elapsed_bad >= minimum and
                        rec.get('bad_count', 0) >= 3 and
                        rec.get('fault') is None):
                    one(api, st, f'observer/fault/{token}/{int(now)}',
                        f'🚨 OBSERVER {kind.upper()} | {name}\n{reason}')
                    rec['fault'] = reason
                elif (rec.get('fault') == 'measurements stale >90s' and
                      reason == 'service inactive' and elapsed_bad is not None and
                      elapsed_bad >= OBSERVER_INACTIVE_CONFIRM_SEC and
                      rec.get('bad_count', 0) >= 3):
                    # True escalation: summary stale AND systemd has now failed.
                    one(api, st, f'observer/fault/escalated/{token}/{int(now)}',
                        f'🚨 OBSERVER {kind.upper()} | {name}\nservice inactive')
                    rec['fault'] = reason
            elif _confirmed_recovery(rec, now):
                one(api, st, f'observer/normal/{token}/{int(now)}',
                    f'✅ BACK TO NORMAL | {name}\nObserver {kind} работает, данные свежие')
                rec.update({'fault': None, 'good_since': None, 'good_count': 0})


def panic_ack_check(api, st, services, roots):
    """One critical alert per uninterrupted PANIC manual-review episode.

    Engine can rewrite the same ACK with fresh timestamps every loop;
    updated_at/remaining_position must NOT become a new alert identity.
    """
    notified=st.setdefault('panic_ack_alarms_v0725',{})
    for name in services:
        if name not in roots:continue
        path=Path(roots[name])/'state/control_ack.json'
        try:ack=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue  # No fake healthy indication.
        if not isinstance(ack,dict):continue
        phase=str(ack.get('phase') or '')
        if ack.get('mode')!='PANIC' or phase not in (
                'PANIC_MANUAL_REVIEW','PANIC_RETRY_EXHAUSTED'):
            notified.pop(name,None)  # New event can create a NEW episode.
            continue
        previous=notified.get(name)
        if isinstance(previous,dict) and previous.get('phase')==phase:
            continue
        stamp=str(ack.get('updated_at') or 'unknown')[:64]
        remaining=str(ack.get('remaining_position') or 'н/д')[:60]
        one(api,st,f'panic/ack/{name}/{phase}/{stamp}',
            f'🚨 PANIC {phase} | {name}\n'
            f'Остаток позиции: {remaining}\n'
            'Автоматическое закрытие НЕ подтверждено. Нужен ручной разбор.')
        notified[name]={'phase':phase,'stamp':stamp}
