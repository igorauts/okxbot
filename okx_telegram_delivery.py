"""Durable, local-only Telegram alert queue (v0.7.22).

At-least-once delivery: an ambiguous Telegram network ACK can still duplicate a
message. Telegram Bot API has no sendMessage idempotency key. Trading files,
credentials, and exchange APIs are never read or written by this module.
"""
import json
import os
import time
from pathlib import Path

STATE_PATH = Path('/var/lib/okxbot/telegram_monitor_state.json')
MAX_PENDING = 5000


def persist(st, path=None):
    target = Path(path) if path is not None else STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(target.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(st, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
        dfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp.exists():
            tmp.unlink()


def enqueue(st, key, message, html_mode=False, path=None):
    seen = st.setdefault('incident_sent', {})
    if key in seen:
        return False
    queue = st.setdefault('alert_outbox_v0722', [])
    if len(queue) >= MAX_PENDING:
        raise RuntimeError('Telegram alert outbox full; do not drop critical alerts')
    now = time.time()
    queue.append({'key': key, 'text': message[:3900], 'html': bool(html_mode),
                  'created': now, 'tries': 0, 'retry_at': now})
    seen[key] = now
    try:
        persist(st, path)
    except BaseException:
        queue.pop()
        seen.pop(key, None)
        raise
    return True


def drain(api, st, path=None, limit=30, now=None):
    now = time.time() if now is None else now
    queue = st.setdefault('alert_outbox_v0722', [])
    sent = 0
    for _ in range(min(limit, len(queue))):
        msg = queue[0]
        if now < float(msg.get('retry_at', 0)):
            break  # Preserve ordering and don't send later alerts ahead of faults.
        try:
            api.send(msg['text'], parse_mode='HTML' if msg.get('html') else None)
        except Exception:
            msg['tries'] = min(16, int(msg.get('tries', 0)) + 1)
            msg['retry_at'] = now + min(300, 5 * 2 ** (msg['tries'] - 1))
            persist(st, path)
            return sent
        queue.pop(0)
        persist(st, path)
        sent += 1
    return sent
