"""Read-only local latency view for the terminal manager."""
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _percentile(xs,p):
    if not xs:return 'н/д'
    s=sorted(xs)
    idx=max(0,math.ceil(len(s)*p/100)-1)
    return f'{s[idx]:.2f} ms'


def _records(path, bytes_limit=32*1024*1024):
    if not path.is_file() or path.is_symlink():return []
    # Tail only: cap the scanned history at 32 MiB, not an unbounded journal of latency logs in the manager.
    with path.open('rb') as f:
        end=f.seek(0,2)
        f.seek(max(0,end-bytes_limit))
        if end>bytes_limit:f.readline()
        raw=f.read(bytes_limit)
    rows=[]
    for line in raw.splitlines():
        try:
            obj=json.loads(line)
            if isinstance(obj,dict):rows.append(obj)
        except (UnicodeDecodeError,ValueError):continue
    return rows


def report(root):
    window_minutes=60
    root=Path(root)
    log=root/'logs/latency.jsonl'
    health=root/'logs/latency_health.json'
    now=datetime.now(timezone.utc)
    after=now-timedelta(minutes=window_minutes)
    summary={p:[] for p in ('loop_pass','http','positions_gate','rest_gate','ws_drain')}
    samples=[]
    for rec in _records(log):
        try:
            ts=datetime.fromisoformat(rec['ts'].replace('Z','+00:00'))
            ms=float(rec['ms'])
            if not math.isfinite(ms) or ms<0 or ts.tzinfo is None:continue
            if after<=ts<=now+timedelta(seconds=10):
                samples.append(ts)
                if rec.get('phase') in summary:summary[rec['phase']].append(ms)
        except (ValueError,TypeError,KeyError):continue
    latest=max(samples) if samples else None
    first=min(samples) if samples else None
    coverage=min(60.0,max(0.0,(latest-first).total_seconds()/60.0)) if first else 0.0
    # Span is not complete sample coverage when there are telemetry gaps.
    ordered=sorted(samples)
    gaps=sum((b-a).total_seconds()>120 for a,b in zip(ordered,ordered[1:]))
    active=bool(latest and (now-latest).total_seconds()<90)
    lines=[f'LATENCY DIAGNOSTICS — {root.name}',
           f'Статус: {"ACTIVE" if active else "NO FRESH DATA"}',
           f'Окно: {window_minutes} мин | временной охват: {coverage:.1f}/60 мин (хвост до 32 MiB)',
           f'Разрывы данных >120 с: {gaps}',
           f'Измерений в окне: {len(samples)}',
           'Последний замер: '+(latest.isoformat(timespec='seconds') if latest else 'н/д'),
           '', 'Путь                 N       P50          P95          P99          MAX']
    for label,values in summary.items():
        lines.append(f'{label:<20} {len(values):<6} {_percentile(values,50):<12} {_percentile(values,95):<12} {_percentile(values,99):<12} {_percentile(values,100)}')
    try:
        if health.is_file() and not health.is_symlink():
            data=json.loads(health.read_text())
            if data.get('schema')==1:
                lines.extend(['',f'Dropped (всего): {data.get("dropped","н/д")}',
                     f'Ошибок записи (всего): {data.get("write_errors","н/д")}'])
            else:lines.append('Dropped: н/д (неизвестный формат)')
        else:lines.append('Dropped: н/д (старый движок, нет sidecar)')
    except (OSError,ValueError):lines.append('Dropped: н/д (ошибка sidecar)')
    lines.extend(['','Локальный журнал; дополнительных запросов OKX нет.'])
    return '\n'.join(lines)
