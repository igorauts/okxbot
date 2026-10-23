"""Bounded, read-only FIFO and execution diagnostics for a single bot.

No trading imports, API calls, file writes, or guesses about missing telemetry.
"""
import json
import math
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from okx_latency_ui import _records, _percentile
from okx_latency_rollup import distribution, fresh_rows
from okx_pnl_diagnostics import inspect as pnl_inspect


def _time(record):
    try:
        ts=datetime.fromisoformat(str(record['ts']).replace('Z','+00:00'))
        return ts if ts.tzinfo else None
    except (ValueError,TypeError,KeyError):return None


def _window(path, now, limit=60):
    floor=now-timedelta(minutes=limit)
    return [r for r in _records(path) if (t:=_time(r)) and floor<=t<=now+timedelta(seconds=10)]


def _ms(record):
    try:
        value=float(record['ms'])
        return value if math.isfinite(value) and value>=0 else None
    except (KeyError,ValueError,TypeError):return None


def fifo_report(root, *, now=None):
    root=Path(root);now=now or datetime.now(timezone.utc)
    recs=_window(root/'logs'/'latency.jsonl',now)
    phases=[('positions_gate_lock_wait','Lock wait (факт)'),
            ('positions_gate_quota_wait','Quota delay (предложено)'),
            ('positions_gate_fifo_wait','FIFO wait (факт)'),
            ('positions_gate','Positions gate'),
            ('http','HTTP общие')]
    rollups=[]
    try:
        for line in (root/'logs/latency_24h_rollups.jsonl').read_text().splitlines():
            try:rollups.append(json.loads(line))
            except ValueError:continue
        rollups=fresh_rows(rollups,now.timestamp())
    except OSError:pass
    lines=[f'FIFO DIAGNOSTICS — {root.name}',
           'Последние 60 минут; raw до 32 MiB, быстрые lock/quota из минутных гистограмм',
           'Quota delay — рекомендованный backoff, НЕ фактическое ожидание.',
           '', 'Фаза                      N       P50          P95          MAX']
    for phase,label in phases:
        values=[v for r in recs if r.get('phase')==phase if (v:=_ms(r)) is not None]
        if phase in ('positions_gate_lock_wait','positions_gate_quota_wait') and rollups:
            d=distribution(rollups,phase,now.timestamp(),window_seconds=3600)
            fmt=lambda ms: f'{ms:.2f} ms<=' if ms is not None else 'н/д'
            lines.append(f'{label:<26} {d["n"]:<6} {fmt(d["p50_upper_ms"]):<12} '
                         f'{fmt(d["p95_upper_ms"]):<12} '
                         f'{d["max_ms"]:.2f} ms (hist)')
        else:
            lines.append(f'{label:<26} {len(values):<6} {_percentile(values,50):<12} '
                         f'{_percentile(values,95):<12} {_percentile(values,100)}')
    errors=[r for r in recs if r.get('phase')=='positions_gate_error']
    recovered=[r for r in recs if r.get('phase')=='positions_gate_recovery']
    reasons=Counter(str(r.get('reason') or 'неизвестно') for r in errors)
    lines.extend(['','FIFO 24 часа (гистограмма; оценки округлены вверх):'])
    for phase,label in phases:
        d=distribution(rollups,phase,now.timestamp())
        lines.append(f'{label}: N={d["n"]} P95<={d["p95_upper_ms"]}ms MAX={d["max_ms"]:.2f}ms')
    lines.extend(['',f'GateUnavailable: {len(errors)} подтверждённых диагностикой',
                  f'Recovery storage: {len(recovered)} событий',
                  'Причины: '+(', '.join(f'{k}={v}' for k,v in sorted(reasons.items())) if reasons else 'нет событий'),
                  'Нет метрик ≠ задержка 0. Только наблюдение, без OKX GET.'])
    return '\n'.join(lines)


def execution_report(root, *, now=None):
    root=Path(root);now=now or datetime.now(timezone.utc)
    recs=_window(root/'logs'/'bot_events.jsonl',now)
    counter=Counter(str(r.get('event') or '') for r in recs)
    buys=counter['BUY_FILLED']+counter['BUY_FILLED_DURING_CANCEL']
    sells=counter['TP_FILLED']
    attached=counter['PARTIAL_ATTACHED_TP_DISCOVERED']
    market=(counter['AGG_MARKET_TERMINAL'])
    sync=counter['SYNC_WAIT']; recoveries=counter['RECONCILIATION_RECOVERED']
    dur=[]
    for r in recs:
        if r.get('event')=='RECONCILIATION_RECOVERED':
            try:
                value=float(r['duration_sec'])
                if math.isfinite(value) and value>=0:dur.append(value)
            except (KeyError,ValueError,TypeError):pass
    lines=[f'EXECUTION DIAGNOSTICS — {root.name}',
           'Последние 60 минут; подтверждённые локальные события, 32 MiB хвост','',
           f'BUY FILLED events: {buys}',
           f'TP FILLED events: {sells}',
           f'Attached partial TP discoveries: {attached}',
           f'Aggregate MARKET terminal events: {market}',
           f'SYNC_WAIT events: {sync}',
           f'RECONCILIATION_RECOVERED events: {recoveries}',
           f'Срок восстановления (заявлен движком), N={len(dur)}: '
              + (f'P50 {_percentile([x*1000 for x in dur],50)}, '
                 f'P95 {_percentile([x*1000 for x in dur],95)}, MAX {_percentile([x*1000 for x in dur],100)}'
                 if dur else 'н/д'),
           '', 'Partial fill ratio: н/д (события не дают полной количественной базы)',
           'TP detection latency: н/д (нет гарантированной пары correlation IDs)',
           'HOLD duration: н/д (нет однозначных границ для всей истории)',
           'Это события, не число уникальных биржевых исполнений или циклов.',
           'Диагностика не взаимодействует с биржей и не изменяет state.']
    try:
        p=pnl_inspect(root)
        lines.extend(['', 'PNL LEDGER vs STATS (read-only):',
                      'Ledger: '+str(p.get('ledger_grid_profit','н/д')),
                      'Report: '+str(p.get('report_grid_profit','н/д')),
                      'Delta: '+str(p.get('delta','н/д')),
                      'Предупреждение: асинхронные снимки могут временно отличаться'])
    except (OSError,ValueError,KeyError,ArithmeticError) as exc:
        lines.append('PnL diagnostic: unavailable ('+type(exc).__name__+')')
    return '\n'.join(lines)
