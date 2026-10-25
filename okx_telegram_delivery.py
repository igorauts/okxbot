from okx_telegram_wire import state_key
"""Durable, local-only Telegram alert queue.

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
MAX_DEAD_LETTERS = 256
MAX_SENT_KEYS = 20000
SENT_KEY_TTL = 90 * 86400


def prune_sent(st, now):
    """Bound persistent dedupe keys without releasing keys still queued."""
    seen=st.setdefault('incident_sent',{})
    if len(seen)<=MAX_SENT_KEYS:return
    pending={item.get('key') for item in st.get(state_key('alert_outbox'),[])}
    old=sorted(((float(v),k) for k,v in seen.items()
                if k not in pending and isinstance(v,(float,int))),
               key=lambda pair:pair[0])
    for ts,k in old:
        if len(seen)<=MAX_SENT_KEYS or ts>now-SENT_KEY_TTL:break
        seen.pop(k,None)
    # If volume still exceeds the bound, expire oldest delivered keys.
    for _,k in old:
        if len(seen)<=MAX_SENT_KEYS:break
        seen.pop(k,None)



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
    queue = st.setdefault(state_key('alert_outbox'), [])
    if len(queue) >= MAX_PENDING:
        raise RuntimeError('Telegram alert outbox full; do not drop critical alerts')
    now = time.time()
    prune_sent(st, now)
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
    queue = st.setdefault(state_key('alert_outbox'), [])
    sent = 0
    for _ in range(min(limit, len(queue))):
        msg = queue[0]
        if now < float(msg.get('retry_at', 0)):
            break  # Temporary API/network failures remain retryable in order.
        try:
            api.send(msg['text'], parse_mode='HTML' if msg.get('html') else None)
        except Exception as exc:
            # Only a structurally confirmed permanent error may be bypassed.
            # 429 and all network/5xx errors must remain retryable.
            if getattr(exc,'telegram_permanent',False):
                dead=st.setdefault(state_key('alert_dead_letters'),[])
                dead.append({'key':msg.get('key'),'error':type(exc).__name__,
                             'at':now,'tries':int(msg.get('tries',0))+1})
                if len(dead)>MAX_DEAD_LETTERS:
                    del dead[:-MAX_DEAD_LETTERS]
                st[state_key('alert_dead_letter_total')]=int(
                    st.get(state_key('alert_dead_letter_total'),0))+1
                queue.pop(0)
                persist(st,path)
                continue  # Release the next independent CRITICAL message.
            msg['tries'] = min(16, int(msg.get('tries', 0)) + 1)
            msg['retry_at'] = now + min(300, 5 * 2 ** (msg['tries'] - 1))
            persist(st, path)
            return sent
        queue.pop(0)
        persist(st, path)
        sent += 1
    return sent
