#!/usr/bin/env python3
"""Numerical Reader HTTP error metrics for last 30 min (read-only)."""
import json
import re
import subprocess

p = subprocess.run(['journalctl', '-u', 'okx-position-reader.service',
                    '--since', '30 minutes ago', '--no-pager', '-o', 'json'],
                   text=True, capture_output=True, timeout=30, check=False)
if p.returncode:
    raise SystemExit('HTTP_AUDIT_FAILED: journalctl error')
metrics = {'http_429': 0, 'okx_50011': 0, 'http_non200': 0, 'http_attempts': 0}
reports = 0
raw429 = 0
for raw in p.stdout.splitlines():
    try:
        msg = json.loads(raw).get('MESSAGE', '')
    except (TypeError, ValueError):
        continue
    if not isinstance(msg, str):
        continue
    found = []
    for match in re.finditer(r'"positions_http_delta"\s*:\s*(\{[^{}]*\})', msg):
        try:
            obj = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if any(type(obj.get(k, 0)) is not int or obj.get(k, 0) < 0 for k in metrics):
            raise SystemExit('HTTP_AUDIT_FAILED: invalid numeric counters')
        for k in metrics:
            metrics[k] += obj.get(k, 0)
        reports += 1
        found.append((match.start(), match.end()))
    # Check separately for explicit 429 *outside* the JSON counters.
    if found:
        masked = list(msg)
        for a, b in found:
            masked[a:b] = [' '] * (b-a)
        remainder = ''.join(masked)
    else:
        remainder = msg
    if re.search(r'(?i)(?:HTTP\s*[/ :]\s*429|status\s*[=:]\s*429|too many requests)', remainder):
        raw429 += 1
print('===== READER HTTP COUNTERS LAST 30 MIN =====')
print('METRIC_REPORTS=', reports)
for k, v in metrics.items():
    print(k.upper() + '=', v)
print('EXPLICIT_429_LOG_LINES=', raw429)
print('NO_OKX_CALLS=YES READ_ONLY=YES')
