"""Account-wide Shared Position Reader diagnostics (strictly local, GET-free).

This module does not import trading APIs, open credentials, write state, or
manage systemd services. Individual bot rows are presented on ONE account page.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SOCKET = '/run/okx-position-reader/positions.sock'
POSITIONS_ROUTE = '/api/v5/account/positions'
WINDOW = 3600
TAIL_BYTES = 32 * 1024 * 1024
_VALID_INST = re.compile(r'[A-Z0-9]+-USDT-SWAP\Z')


def reader_health(path=SOCKET, *, timeout=2.0):
    """Bounded IPC health only; never requests account positions or quota."""
    if path != SOCKET and not Path(path).is_absolute():
        return None
    try:
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(b'{"schema":1,"op":"health"}\n')
            buf = bytearray()
            while b'\n' not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk or len(buf) + len(chunk) > 8192:
                    return None
                buf.extend(chunk)
        reply = json.loads(buf.split(b'\n', 1)[0])
        if not isinstance(reply, dict) or reply.get('ok') is not True:
            return None
        data = reply.get('data')
        if not isinstance(data, dict) or data.get('reader_running') is not True:
            return None
        for k in ('fetch_failed', 'quota_failed', 'pending'):
            if type(data.get(k)) is not int or data[k] < 0:
                return None
        instruments = data.get('instruments')
        if not isinstance(instruments, list) or len(instruments) > 10 or any(
                not isinstance(i, str) or not _VALID_INST.fullmatch(i)
                for i in instruments) or len(set(instruments)) != len(instruments):
            return None  # never assume an instrument is registered
        if data.get('scheduling', 'unknown') not in ('mixed-fifo','priority','unknown'):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError, TimeoutError, UnicodeError):
        return None


def _reader_env(path):
    """Read ONLY the two Reader settings; do not return or print credentials."""
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        return {}
    keys = {'OKX_POSITION_READER_SOCKET', 'OKX_POSITION_READER_QUOTA_SCHEDULING'}
    result = {}
    try:
        # Bound malformed accidental files; secrets never leave this scope.
        if p.stat().st_size > 128 * 1024:
            return {}
        for line in p.read_text(encoding='utf-8').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            k, v = line.split('=', 1)
            if k.strip() in keys:
                if k.strip() in result:
                    return {}  # ambiguous duplicate setting
                result[k.strip()] = v.strip().strip('"').strip("'")
    except (OSError, UnicodeError, ValueError):
        return {}
    return result


def _latency_summary(path, *, now=None, window=WINDOW, limit=TAIL_BYTES):
    now = time.time() if now is None else now
    out = {'ipc_ok': 0, 'ipc_fail': 0, 'direct': 0,
           'last_ok': None, 'last_fail': None, 'last_direct': None,
           'recent_direct': 0, 'recent_fail': 0, 'truncated': False}
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        return out
    try:
        with p.open('rb') as f:
            size = f.seek(0, 2)
            out['truncated'] = size > limit
            f.seek(max(0, size-limit))
            if size > limit:
                f.readline()
            raw = f.read(limit)
    except OSError:
        return out
    for line in raw.splitlines():
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                continue
            ts = datetime.fromisoformat(str(r.get('ts', '')).replace('Z', '+00:00'))
            if ts.tzinfo is None:
                continue
            at = ts.timestamp()
            if not (now-window <= at <= now+10):
                continue
            phase = r.get('phase')
            if phase == 'position_reader_ipc':
                if r.get('outcome') == 'ok':
                    out['ipc_ok'] += 1
                    out['last_ok'] = max(out['last_ok'] or at, at)
                else:
                    out['ipc_fail'] += 1
                    out['last_fail'] = max(out['last_fail'] or at, at)
                    if at >= now-120:
                        out['recent_fail'] += 1
            elif phase == 'positions_gate' and r.get('route') == POSITIONS_ROUTE:
                out['direct'] += 1
                out['last_direct'] = max(out['last_direct'] or at, at)
                if at >= now-120:
                    out['recent_direct'] += 1
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            continue
    return out


def _active(unit):
    if not isinstance(unit, str) or not re.fullmatch(r'okx-grid-[a-z0-9-]+(?:\.service)?', unit):
        return False
    try:
        cmd = subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                             timeout=3, capture_output=True, check=False)
        return cmd.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def collect(roots, services, *, now=None, health=None, active=None, tail_bytes=TAIL_BYTES):
    """One account snapshot. No OKX GET; no state, service, or registry mutations."""
    now = time.time() if now is None else now
    info = reader_health() if health is None else health
    registered = set(info.get('instruments', ())) if info else set()
    entries = []
    for name, raw_root in sorted(roots.items()):
        unit = services.get(name)
        if not isinstance(name, str) or not unit or (active or _active)(unit) is not True:
            continue
        root = Path(raw_root)
        if not root.is_dir() or root.is_symlink() or (root/'state/final_stats.json').exists():
            continue
        try:
            cfg = json.loads((root/'config.json').read_text())
            instrument = cfg.get('instrument', '')
            if not isinstance(instrument, str) or not _VALID_INST.fullmatch(instrument):
                raise ValueError('invalid instrument')
        except (OSError, ValueError, TypeError, AttributeError):
            entries.append({'name': name, 'state': 'UNKNOWN CONFIG', 'metrics': {}})
            continue
        env = _reader_env(root/'.env')
        socket_ok = env.get('OKX_POSITION_READER_SOCKET') == SOCKET
        fifo_ok = env.get('OKX_POSITION_READER_QUOTA_SCHEDULING') == 'mixed-fifo'
        registered_ok = instrument in registered if info else None
        metrics = _latency_summary(root/'logs/latency.jsonl', now=now, limit=tail_bytes)
        if not socket_ok or not fifo_ok:
            state = 'CONFIG BYPASS'
        elif info is None:
            state = 'READER DOWN / UNKNOWN'
        elif not registered_ok:
            state = 'UNREGISTERED'
        elif (metrics['recent_fail'] >= 2 and metrics['recent_direct'] >= 3
              and (metrics['last_ok'] is None or metrics['last_ok'] < now-120)):
            state = 'SUSTAINED FALLBACK'
        elif metrics['last_ok'] is not None and metrics['last_ok'] >= now-120:
            state = 'READER OK'
        else:
            state = 'CONFIGURED / NO RECENT IPC'
        entries.append({'name': name, 'instrument': instrument,
                        'state': state, 'socket_ok': socket_ok,
                        'fifo_ok': fifo_ok, 'registered': registered_ok,
                        'metrics': metrics})
    return {'health': info, 'bots': entries, 'window': WINDOW}


def report(snapshot):
    info = snapshot['health']
    rows = snapshot['bots']
    lines = ['SHARED POSITION READER — ДИАГНОСТИКА АККАУНТА',
             'Только локальные данные · GET OKX: 0 · управление: нет', '']
    if info:
        lines += ['Reader: ACTIVE / IPC OK',
                  'Режим: ' + str(info.get('scheduling', 'unknown')) +
                  (' (Priority FIFO OFF)' if info.get('scheduling') == 'mixed-fifo' else ''),
                  'Инструменты: '+(', '.join(info['instruments']) or 'нет'),
                  'GET failed: '+str(info['fetch_failed'])+' | quota failed: '+str(info['quota_failed']),
                  'Очередь: '+str(info['pending'])+' | текущий GET: '+str(info.get('inflight_age_ms',0))+' мс']
    else:
        lines += ['Reader: DOWN / неизвестен (IPC health недоступен)',
                  'Регистрация инструментов не подтверждена']
    lines += ['', f'Активных ботов: {len(rows)} | окно телеметрии: 60 минут', '']
    for row in rows:
        m = row['metrics']
        lines.append(f'{row["name"]} | {row["state"]}')
        if 'instrument' not in row:
            continue
        lines.append(f'  {row["instrument"]} | registered: '+(
            'YES' if row['registered'] is True else 'NO' if row['registered'] is False else 'UNKNOWN'))
        lines.append(f'  Reader IPC OK/ошибки: {m["ipc_ok"]}/{m["ipc_fail"]} | прямые GET: {m["direct"]}')
        if m.get('last_fail'):
            age = max(0, int(time.time()-m['last_fail']))
            lines.append(f'  Последний сбой IPC: {age} с назад; причина: недоступно в телеметрии')
        if m.get('truncated'):
            lines.append('  ⚠️ Журнал ограничен хвостом 32 MiB; окно может быть неполным')
    lines += ['', 'Прямой GET после отказа Reader использует исходный общий лимитер.',
              'Показатели — локальные наблюдения, не подтверждение биржевого fill.']
    return '\n'.join(lines)
