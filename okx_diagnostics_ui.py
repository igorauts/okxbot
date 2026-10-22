"""Bounded, read-only FIFO and execution diagnostics for a single bot.

No trading imports, API calls, file writes, or guesses about missing telemetry.
"""
import json
import math
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from okx_latency_ui import _records, _percentile


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
    lines=[f'FIFO DIAGNOSTICS — {root.name}',
           'Последние 60 минут; локальная телеметрия, хвост до 32 MiB',
           'Quota delay — рекомендованный backoff, НЕ фактическое ожидание.',
           '', 'Фаза                      N       P50          P95          MAX']
    for phase,label in phases:
        values=[v for r in recs if r.get('phase')==phase if (v:=_ms(r)) is not None]
        lines.append(f'{label:<26} {len(values):<6} {_percentile(values,50):<12} '
                     f'{_percentile(values,95):<12} {_percentile(values,100)}')
    errors=[r for r in recs if r.get('phase')=='positions_gate_error']
    recovered=[r for r in recs if r.get('phase')=='positions_gate_recovery']
    reasons=Counter(str(r.get('reason') or 'неизвестно') for r in errors)
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
    return '\n'.join(lines)
