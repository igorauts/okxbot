from okx_telegram_wire import state_key
"""Read-only systemd / engine / observer health for Telegram.

Never restarts a service, writes bot state, or accesses the OKX exchange.
Systemd journal cursors let a fast restart be observed between 5s polls.
"""
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from okx_paths import observer_dir
from okx_telegram_events import one


def _run(cmd, timeout=5):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          check=False)


_SHOW_CACHE = {}
_SHOW_KEYS = ('Id', 'ActiveState', 'SubState', 'InvocationID', 'MainPID',
              'ActiveEnterTimestampMonotonic', 'Result', 'NRestarts',
              'UnitFileState', 'LoadState')

def begin_systemd_snapshot(services):
    """One systemctl show for engines, observers and Reader per poll.
    Unknown/malformed units fall back to individual show (no false healthy).
    """
    _SHOW_CACHE.clear()
    unit_names = list(dict.fromkeys(
        [str(u).removesuffix('.service') + '.service' for u in services.values()] +
        [f'observer-{str(u).removesuffix(".service")}-{kind}.service'
         for u in services.values() for kind in ('burst', 'execution')] +
        ['okx-position-reader.service']))
    cmd = ['systemctl', 'show', '--no-pager']
    for key in _SHOW_KEYS:
        cmd += ['-p', key]
    cmd += unit_names
    try:
        result = _run(cmd, timeout=7)
        # A missing archived unit can make systemctl exit nonzero; preserve
        # the valid records it did return instead of falling back for all.
        allowed = set(unit_names)
        for block in result.stdout.split('\n\n'):
            props = dict(line.split('=', 1) for line in block.splitlines()
                         if '=' in line)
            unit = props.get('Id', '')
            if unit in allowed:
                value = (props if props.get('ActiveState') and
                         props.get('LoadState') != 'not-found' else None)
                _SHOW_CACHE[unit] = value
                _SHOW_CACHE[unit.removesuffix('.service')] = value
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

def _show(unit):
    if unit in _SHOW_CACHE:
        return _SHOW_CACHE[unit]
    try:
        cmd = ['systemctl', 'show', unit, '--no-pager']
        for key in _SHOW_KEYS:
            cmd += ['-p', key]
        result = _run(cmd, timeout=4)
        props = (dict(line.split('=', 1) for line in result.stdout.splitlines()
                      if '=' in line) if result.returncode == 0 else None)
        if props and (not props.get('ActiveState') or props.get('LoadState') == 'not-found'):
            props = None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        props = None
    _SHOW_CACHE[unit] = props
    return props

def systemd_active(unit):
    props = _show(unit)
    return props is not None and props.get('ActiveState') == 'active'

def _journal(unit, cursor=None):
    # Journal is the source of truth for individual state transitions.
    cmd = ['journalctl', '-u', unit, '-o', 'json', '--no-pager', '-n', '1000' if cursor else '1']
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


def _reader_health_ipc(path):
    """GET-free, bounded local health check. No trading credentials or OKX REST."""
    if not os.path.isabs(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            deadline = time.monotonic() + 2.0
            client.settimeout(max(0.05, deadline-time.monotonic()))
            client.connect(path)
            client.settimeout(max(0.05, deadline-time.monotonic()))
            client.sendall(b'{"schema":1,"op":"health"}\n')
            result=bytearray()
            while b'\n' not in result and len(result)<=4096:
                left=deadline-time.monotonic()
                if left<=0:
                    raise TimeoutError('Reader health deadline expired')
                client.settimeout(left)
                chunk=client.recv(4096)
                if not chunk:
                    raise OSError('Reader health socket closed')
                result.extend(chunk)
                if len(result)>4096:
                    raise ValueError('Reader health oversized response')
        payload=json.loads(bytes(result).split(b'\n',1)[0])
        info=payload.get('data') if isinstance(payload,dict) else None
        if (isinstance(payload,dict) and payload.get('ok') is True and isinstance(info,dict)
                and info.get('reader_running') is True
                and type(info.get('fetch_failed')) is int
                and type(info.get('quota_failed')) is int
                and info['fetch_failed']>=0 and info['quota_failed']>=0):
            return info
    except (OSError,ValueError,TypeError,KeyError,TimeoutError,UnicodeError):
        pass
    return None


def position_reader_health_check(api, st):
    """Bounded, durable, GET-free Reader incident monitoring.

    Systemd FAILED always alerts once until a verified recovery. The other
    alerts are limited per rolling episode; a crash-loop is explicitly FLAPPING.
    No trading commands, service restarts, or exchange API calls.
    """
    path=os.environ.get('OKX_POSITION_READER_SOCKET', '').strip()
    if not path:
        return
    rec=st.setdefault('position_reader_health', {})
    now=time.time()
    info=_reader_health_ipc(path)
    props=_show('okx-position-reader.service')
    active=props.get('ActiveState') if props else None
    online=(info is not None and (props is None or active=='active'))
    failed=(props is not None and active=='failed')
    prev_down=rec.get('down') is True
    ep=int(rec.get('episode',0))
    n_restarts=None
    if props is not None:
        val=str(props.get('NRestarts',''))
        if val.isdecimal():n_restarts=int(val)
    prior=rec.get('systemd_n_restarts')
    missed_crash=(online and not prev_down and type(prior) is int
                  and n_restarts is not None and n_restarts>prior)
    if n_restarts is not None:
        rec['systemd_n_restarts']=n_restarts
    if online and not rec.get('monitor_armed'):
        one(api, st, 'reader/watchdog/armed',
            'ℹ️ Shared Position Reader: Telegram-контроль включён. '
            'DOWN / RECOVERED / FAILED; без торговых команд.')
        rec['monitor_armed']=True

    def alert(key, text):
        one(api,st,key,text)

    # A time-bounded window persists across monitor restarts. Guard against
    # system clock adjustments and reboot; never replay a stale crash burst.
    times=[float(x) for x in rec.get('down_times',[]) if
           isinstance(x,(int,float)) and 0 <= now-float(x) < 600]
    new_down=(not online and not prev_down) or missed_crash
    if new_down:
        ep+=1
        rec['episode']=ep
        rec['stable_since']=0  # missed in-between crash resets stability too
        times.append(now)
    rec['down_times']=times[-64:]
    if new_down and len(times)>=3 and not rec.get('flapping'):
        rec['flapping']=True
        alert('reader/flapping/'+str(ep),
              '🚨 Shared Position Reader FLAPPING: ≥3 DOWN за 10 минут. '
              'Повторные сообщения подавлены до устойчивого восстановления.')

    if new_down and not rec.get('flapping'):
        alert('reader/unavailable/'+str(ep),
              '🚨 Shared Position Reader DOWN: health IPC/systemd не подтвердили '
              'работоспособность; боты используют резервный GET через общую квоту.')
    if failed and not rec.get('failed_alerted'):
        reason=str(props.get('Result') or 'unknown')[:80]
        alert('reader/failed/'+str(ep),
              '🔴 Shared Position Reader FAILED: systemd, Result='+reason)
        rec['failed_alerted']=True
    if not online:
        rec['stable_since']=0
    else:
        if not isinstance(rec.get('stable_since'),(int,float)) or rec['stable_since']<=0:
            rec['stable_since']=now
        # Do not send 120 recoveries in crash loops; require 120s stability
        # to exit FLAPPING and clear the notification suppression episode.
        if rec.get('flapping'):
            if now-float(rec['stable_since']) >= 120:
                rec['flapping']=False
                rec['down_times']=[]
                rec['failed_alerted']=False
                alert('reader/stable/'+str(ep),
                      '✅ Shared Position Reader STABLE: 120с устойчивой работы после FLAPPING.')
        elif prev_down or missed_crash:
            alert('reader/recovered/'+str(ep),
                  '✅ Shared Position Reader RECOVERED: active + health IPC PASS.')
        if not rec.get('flapping'):
            rec['failed_alerted']=False
    # Aggregate actual Reader failures into one actionable incident, not a
    # 10-minute stream of misleading GET/quota warnings. Reader DOWN and FAILED
    # remain separate immediate critical notifications above.
    if info is not None:
        counters = (info['fetch_failed'], info['quota_failed'])
        if rec.get('counters_initialized'):
            previous = (int(rec.get('fetch_failed', 0)), int(rec.get('quota_failed', 0)))
            fetch_delta = max(0, counters[0] - previous[0])
            quota_delta = max(0, counters[1] - previous[1])
            if fetch_delta or quota_delta:
                rec['failure_last_at'] = now
                window = rec.setdefault('failure_window', [])
                window.append([now, fetch_delta, quota_delta])
        window = rec.get('failure_window', [])
        if not isinstance(window, list):
            window = []
        window = [item for item in window if
                  isinstance(item, list) and len(item) == 3 and
                  isinstance(item[0], (float, int)) and
                  0 <= now - float(item[0]) <= 600]
        rec['failure_window'] = window[-150:]
        fetch_10m = sum(int(item[1]) for item in rec['failure_window'])
        quota_10m = sum(int(item[2]) for item in rec['failure_window'])
        if not rec.get('failure_incident_active') and (fetch_10m >= 5 or quota_10m >= 1):
            number = int(rec.get('failure_incident_number', 0)) + 1
            # Persist-before-send via one()/enqueue(); if enqueue fails, do
            # not silently mark the episode alerted.
            one(api, st, f'reader/fetch_episode/{number}',
                f'⚠️ Shared Reader: повышенная частота ошибок за 10 мин. '
                f'GET={fetch_10m}, quota={quota_10m}. '
                'Reader DOWN/FAILED отслеживается отдельно.')
            rec['failure_incident_number'] = number
            rec['failure_incident_active'] = True
        elif rec.get('failure_incident_active') and (
                now - float(rec.get('failure_last_at') or now) >= 600):
            number = int(rec.get('failure_incident_number', 0))
            one(api, st, f'reader/fetch_recovery/{number}',
                '✅ Shared Reader: 10 минут без новых ошибок GET/quota; '
                'инцидент закрыт. Reader IPC/systemd подтверждены.')
            rec['failure_incident_active'] = False
            rec['failure_window'] = []
        rec['fetch_failed'], rec['quota_failed'] = counters
        rec['counters_initialized'] = True
    rec['down']=not online


def service_check(api, st, services, roots):
    recs = st.setdefault(state_key('systemd_watch'), {})
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
                          'cursor': history[-1].get('__CURSOR') if history else None,
                           'n_restarts': props.get('NRestarts')}
            continue  # Initial install: no historical startup/stop flood.
        old_active = bool(previous.get('active'))
        old_inv = str(previous.get('invocation') or '')
        cursor = previous.get('cursor')
        old_restarts = previous.get('n_restarts')
        changed = (active != old_active or inv != old_inv or
                   (old_restarts is not None and
                    props.get('NRestarts') != old_restarts))
        history = (_journal(unit, cursor) if changed else []) if cursor else (_journal(unit) if changed else [])
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
        # A terminal command or confirmed terminal ACK deliberately cannot
        # produce RUNNING. In particular a PANIC-only restart must not be
        # remembered as an unconfirmed RUN startup. Drop any start watches
        # created before the PANIC/HARD command was observed as well.
        if expected_terminal:
            pending.clear()
        for kind, identity in transitions:
            if expected_terminal:
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
        previous['n_restarts'] = props.get('NRestarts')


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
from okx_telegram_wire import HEALTH_RULE_WIRE_ID
_HEALTH_RULE = HEALTH_RULE_WIRE_ID


def _health_record(watches, key, now, invocation=None):
    rec = watches.get(key)
    if not isinstance(rec, dict) or rec.get('health_rule') != _HEALTH_RULE:
        # Earlier health records (including a spurious active alert) cannot trigger
        # an immediate new alarm/recovery on upgrading this monitor.
        rec = {'health_rule': _HEALTH_RULE, 'fault': None, 'bad_since': None,
               'bad_count': 0, 'good_since': None, 'good_count': 0}
        watches[key] = rec
    if invocation is not None and rec.get('invocation') != invocation:
        # New process: its previous heartbeat is not evidence of a hang.
        # An unconfirmed restart is NOT a recovery of an already-reported
        # incident. Keep it unresolved until the new process writes a fresh
        # heartbeat and recovery is independently confirmed.
        rec.update({'invocation': invocation,
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
    watches = st.setdefault(state_key('engine_heartbeat'), {})
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
            # A STOP is covered by systemd notifications. Crucially an
            # outstanding HEARTBEAT alarm is NOT a confirmed recovery.
            # Retain that alarm until a fresh heartbeat on an active process.
            rec.update({'bad_since': None, 'bad_count': 0,
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

    # Each fault remains independent in engine_heartbeat. Grouping is
    # presentation only: a common incident stays open while ANY member is
    # unresolved, including one remaining after partial recovery. Never
    # interpret systemd stop / transient monitoring failures as recovery.
    affected = tuple(sorted(name for name in services
                            if isinstance(watches.get(name), dict)
                            and watches[name].get('fault') == 'heartbeat stale'))
    correlated = st.setdefault(state_key('common_engine_stall'), {})
    old_members = set(correlated.get('members', [])) if correlated.get('active') else set()
    new_members = set(affected)
    # Bot discovery/registry loss is not evidence of recovery. Retain any
    # previously alarmed member until it becomes observable and confirms a
    # fresh heartbeat, even if it vanishes from the current services mapping.
    new_members.update(old_members - set(services))
    was_common = bool(correlated.get('active'))
    is_common = len(affected) >= 2
    if not was_common and is_common:
        number = int(correlated.get('episode', 0)) + 1
        one(api, st, f'heartbeat/common/{number}',
            '🚨 COMMON ENGINE STALL | ' + ', '.join(affected) +
            '\nНесколько ботов без свежего heartbeat. REST / сеть / VPS — причина не установлена.')
        correlated.update({'active': True, 'members': list(affected),
                           'since': now, 'episode': number, 'change': 0})
    elif was_common:
        number = int(correlated.get('episode', 1))
        if not new_members:
            one(api, st, f'heartbeat/common/recovered/{number}',
                '✅ ALL RECOVERED | ' + ', '.join(sorted(old_members)) +
                '\nУ всех участников общего инцидента подтверждён свежий heartbeat')
            correlated.update({'active': False, 'members': [], 'ended': now})
        elif new_members != old_members:
            index = int(correlated.get('change', 0)) + 1
            joined = sorted(new_members - old_members)
            recovered = sorted(old_members - new_members)
            details = []
            if joined: details.append('🔴 Присоединились: ' + ', '.join(joined))
            if recovered: details.append('✅ Восстановились: ' + ', '.join(recovered))
            details.append('🔴 Всё ещё без heartbeat: ' + ', '.join(sorted(new_members)))
            one(api, st, f'heartbeat/common/update/{number}/{index}',
                '⚠️ COMMON STALL UPDATE\n' + '\n'.join(details))
            correlated.update({'members': sorted(new_members), 'change': index})
    else:
        # Isolated incidents retain their own notification and recovery.
        for name, age in just_failed:
            one(api, st, f'heartbeat/down/{name}/{int(now)}',
                f'🚨 {name}: нет heartbeat движка ({age}), служба active')
        for name in just_recovered:
            one(api, st, f'heartbeat/recovery/{name}/{int(now)}',
                f'✅ BACK TO NORMAL | {name}\nHeartbeat торгового движка восстановлен')


def observers_check(api, st, services, roots, now=None):
    now = time.time() if now is None else now
    watches = st.setdefault(state_key('observers_health'), {})
    for name, trade_unit in services.items():
        trade = _show(trade_unit)
        if not trade or trade.get('ActiveState') != 'active':
            continue
        for kind in ('burst', 'execution'):
            unit = f'observer-{trade_unit.removesuffix(".service")}-{kind}.service'
            token = f'{name}/{kind}'
            info = _show(unit)
            if info is None:
                continue
            enabled = str(info.get('UnitFileState') or '')
            if not enabled:
                try:
                    enabled = _run(['systemctl', 'is-enabled', unit], timeout=3).stdout.strip()
                except (OSError, subprocess.TimeoutExpired):
                    continue
            if enabled not in ('enabled', 'enabled-runtime'):
                watches.pop(token, None)  # Intentionally disabled: no alarm.
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
    notified=st.setdefault(state_key('panic_ack_alarms'),{})
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


def reader_bypass_check(api, st, services, roots, *, now=None):
    """One incident per sustained misconfiguration/unknown instrument.

    A temporarily unavailable Reader is already reported by the separate
    DOWN/RECOVERED watcher. Do not call OKX or treat an empty-position
    direct fallback as evidence of a broken Reader; its IPC looks identical.
    """
    from okx_reader_diagnostics import collect
    now = time.time() if now is None else now
    # Bounded, infrequent local reads; no restart / exchange access.
    records = st.setdefault('reader_route_episodes', {})
    if not isinstance(records, dict):
        records = {}
        st['reader_route_episodes'] = records
    if now - float(st.get('reader_route_scan_at') or 0) < 30:
        return
    st['reader_route_scan_at'] = now
    def is_run(root):
        p = Path(root) / 'state/control.json'
        try:
            if not p.exists():
                return True  # engine default, no control request
            obj = json.loads(p.read_text())
            return isinstance(obj, dict) and obj.get('mode') == 'RUN'
        except (OSError, ValueError, TypeError):
            return False  # unknown mode: never invent a RUN bypass
    run_roots = {n: root for n, root in roots.items() if is_run(root)}
    run_services = {n: services[n] for n in run_roots if n in services}
    if not run_services:
        return
    snap = collect(run_roots, run_services, now=now, tail_bytes=1024*1024)
    if snap['health'] is None:
        return  # Reader DOWN has its own incident, do not double-alert
    active = set()
    for row in snap['bots']:
        name = row['name']
        active.add(name)
        state = row['state']
        # Only provable configuration bypasses: no guessing on ReaderEmpty.
        fault = state if state in ('CONFIG BYPASS', 'UNREGISTERED') else None
        rec = records.setdefault(name, {})
        if not isinstance(rec, dict):
            rec = {}
            records[name] = rec
        if fault:
            if rec.get('fault') != fault:
                rec.update(fault=fault, since=now, good_since=None)
            if now - float(rec.get('since') or now) >= 60 and not rec.get('alerted'):
                episode = int(rec.get('episode', 0)) + 1
                one(api, st, f'reader/bypass/{name}/{episode}',
                    f'⚠️ Shared Reader BYPASS: {name} / {row.get("instrument", "?")} — '
                    + ('нет корректной настройки Reader' if fault == 'CONFIG BYPASS'
                       else 'пара не зарегистрирована Reader')
                    + '. Бот использует резервный прямой GET через общую квоту.')
                rec.update(alerted=True, episode=episode)
        elif rec.get('alerted'):
            # Recovery requires demonstrated Reader IPC, not just a socket env.
            m = row.get('metrics', {})
            good = state == 'READER OK' and m.get('last_ok') and now-m['last_ok'] < 90
            if good:
                if not rec.get('good_since'):
                    rec['good_since'] = now
                if now - float(rec['good_since']) >= 60:
                    one(api, st, f'reader/bypass-recovered/{name}/{rec["episode"]}',
                        f'✅ Shared Reader RECOVERED: {name} снова использует IPC Reader.')
                    rec.update(fault=None, since=None, good_since=None, alerted=False)
            else:
                rec['good_since'] = None
        else:
            rec.update(fault=None, since=None, good_since=None)
    # Drop inactive stale non-alerted state; preserve active alerts for later review.
    for name in list(records):
        if name not in active and not records[name].get('alerted'):
            records.pop(name, None)
