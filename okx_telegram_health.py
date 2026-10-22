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
        try:
            ack = json.loads((root / 'state/control_ack.json').read_text())
            control = json.loads((root / 'state/control.json').read_text())
        except (OSError, ValueError):
            ack, control = {}, {}
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


def engine_heartbeat_check(api, st, services, roots, now=None):
    now = time.time() if now is None else now
    watches = st.setdefault('engine_heartbeat_v0722', {})
    for name, unit in services.items():
        info = _show(unit)
        if info is None:
            continue
        active = info.get('ActiveState') == 'active'
        rec = watches.setdefault(name, {'since': now, 'alarm': False})
        invocation = info.get('InvocationID') or ''
        if rec.get('invocation') != invocation:
            rec.update({'invocation': invocation, 'since': now, 'alarm': False})
        if not active:
            rec['since'] = now
            rec['alarm'] = False
            continue
        path = Path(roots[name]) / 'state/runtime_stats.json'
        try:
            data = json.loads(path.read_text())
            stamp = float(data['last'])
            good = 0 <= now - stamp <= 120
        except (OSError, ValueError, TypeError, KeyError):
            stamp = None
            good = False
        if not good and now - float(rec.get('since', now)) >= 120 and not rec.get('alarm'):
            age = f'{now-stamp:.0f} с' if stamp and stamp <= now else 'нет свежего файла'
            one(api, st, f'heartbeat/down/{name}/{int(now)}',
                f'🚨 {name}: нет heartbeat движка ({age}), служба active')
            rec['alarm'] = True
        if good and rec.get('alarm'):
            one(api, st, f'heartbeat/recovery/{name}/{int(now)}',
                f'✅ BACK TO NORMAL | {name}\nHeartbeat торгового движка восстановлен')
            rec['alarm'] = False


def observers_check(api, st, services, roots, now=None):
    now = time.time() if now is None else now
    watches = st.setdefault('observers_health_v0722', {})
    for name, trade_unit in services.items():
        trade = _show(trade_unit)
        if not trade or trade.get('ActiveState') != 'active':
            continue
        for kind in ('burst', 'execution'):
            unit = f'observer-{trade_unit.removesuffix(".service")}-{kind}.service'
            try:
                enabled = _run(['systemctl', 'is-enabled', unit], timeout=3).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                continue
            if enabled not in ('enabled', 'enabled-runtime'):
                watches.pop(f'{name}/{kind}', None)  # Intentionally switched off.
                continue
            info = _show(unit)
            if info is None:
                continue
            running = info.get('ActiveState') == 'active'
            summary = observer_dir(roots[name], kind) / 'summary.json'
            try:
                ts = summary.stat().st_mtime
                fresh = 0 <= now - ts <= 90
            except OSError:
                fresh = False
            reason = None if running and fresh else ('service inactive' if not running else 'measurements stale >90s')
            token = f'{name}/{kind}'
            rec = watches.setdefault(token, {'since': now, 'fault': None})
            if reason and rec['fault'] != reason and (not running or now - float(rec['since']) > 90):
                one(api, st, f'observer/fault/{token}/{int(now)}',
                    f'🚨 OBSERVER {kind.upper()} | {name}\n{reason}')
                rec['fault'] = reason
            if not reason and rec.get('fault'):
                one(api, st, f'observer/normal/{token}/{int(now)}',
                    f'✅ BACK TO NORMAL | {name}\nObserver {kind} работает, данные свежие')
                rec['fault'] = None
                rec['since'] = now
