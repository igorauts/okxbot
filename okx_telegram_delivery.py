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
    """Apply TTL independently of count; never expire queued alert keys."""
    seen=st.setdefault('incident_sent',{})
    pending={item.get('key') for item in st.get(state_key('alert_outbox'),[])
             if isinstance(item,dict)}
    old=sorted(((float(v),k) for k,v in seen.items()
                if k not in pending and isinstance(v,(float,int))),
               key=lambda pair:pair[0])
    for ts,key in old:
        if ts<=now-SENT_KEY_TTL:
            seen.pop(key,None)
    # Cap only delivered keys; no delete of an outstanding outbox item.
    for ts,key in old:
        if len(seen)<=MAX_SENT_KEYS:break
        seen.pop(key,None)



_PERSIST_CACHE={}
_STREAM_CHECKPOINT_SECONDS=30
_STREAM_CURSOR_KEYS=('buy_zero_stream_cursor_v4', 'buy_zero_stream_cursor_at_v4')
_EVENT_OFFSETS_CHECKPOINT_SECONDS=30
_EVENT_OFFSETS_CHECKPOINT_KEY='event_offsets_checkpoint_at_v1'


def _safe_advance(prev, current):
    """Only defer ordinary forward seeks in already-durable event files.

    A new/removed bot cursor, rewind after rotation, or malformed offset must
    be checkpointed immediately. Never suppress the first cursor for a bot.
    """
    if not isinstance(prev,dict) or not isinstance(current,dict):
        return False
    if prev.keys()!=current.keys():
        return False
    for name,old in prev.items():
        new=current[name]
        if (type(old) is not int or type(new) is not int or
                old < 0 or new < old):
            return False
    return True


def _persistent_view(st, baseline, now):
    """Defer only independently replayable cursors and scan heartbeat noise.

    A bot event is read from an append-only local journal. Alerts/outbox,
    command update ACKs, incidents and market tracking remain durable on any
    change. Replaying <=30 s of event rows after a crash is preferable to
    repeated fsync of unchanged alert state. New/rewound event cursors are
    always persisted immediately. This function never mutates caller state.
    """
    if not isinstance(baseline,dict):
        return st
    out=st.copy()

    # BUY 0/N journal cursor: existing 30s bounded replay checkpoint.
    then=baseline.get('buy_zero_stream_cursor_at_v4')
    recent=isinstance(then,(int,float)) and 0<=now-float(then)<_STREAM_CHECKPOINT_SECONDS
    if recent:
        for key in _STREAM_CURSOR_KEYS:
            if key in baseline:
                out[key]=baseline[key]
            else:
                out.pop(key,None)

    # A scan timestamp is only a scheduling hint, never a safety ACK.
    # Recheck on startup instead of fsyncing the hint every 30 seconds.
    key='reader_route_scan_at'
    if key in baseline:
        out[key]=baseline[key]
    else:
        out.pop(key,None)

    # Bot event offsets are local file positions, NOT Telegram getUpdates ACK.
    # Durable checkpoint epoch stays fixed across unrelated alert/command
    # writes, so frequent critical writes cannot starve the 30s checkpoint.
    anchor=baseline.get(_EVENT_OFFSETS_CHECKPOINT_KEY)
    if _EVENT_OFFSETS_CHECKPOINT_KEY in baseline:
        out[_EVENT_OFFSETS_CHECKPOINT_KEY]=anchor
    else:
        out.pop(_EVENT_OFFSETS_CHECKPOINT_KEY,None)
    old=baseline.get('offsets')
    new=st.get('offsets')
    if old!=new:
        if (_safe_advance(old,new) and isinstance(anchor,(int,float)) and
                0<=now-float(anchor)<_EVENT_OFFSETS_CHECKPOINT_SECONDS):
            out['offsets']=old
        else:
            # First cursor/new bot/rotation/stale checkpoint: fsync now.
            out[_EVENT_OFFSETS_CHECKPOINT_KEY]=now

    live_health=st.get('buy_zero_stream_health_v4')
    prev_health=baseline.get('buy_zero_stream_health_v4')
    if isinstance(live_health,dict) and isinstance(prev_health,dict):
        health=live_health.copy()
        if 'observed_at' in prev_health:
            health['observed_at']=prev_health['observed_at']
        else:
            health.pop('observed_at',None)
        out['buy_zero_stream_health_v4']=health
    return out


def persist(st,path=None):
    target=Path(path) if path is not None else STATE_PATH
    cache_key=str(target.resolve())
    try:
        stat=target.stat()
        identity=(stat.st_ino,stat.st_size,stat.st_mtime_ns)
    except FileNotFoundError:
        identity=None
    cached=_PERSIST_CACHE.get(cache_key)
    if cached is None or cached['identity']!=identity:
        if identity is None:
            baseline=None
        else:
            # Fail closed on unexpected/corrupt state. Never silently replace.
            baseline=json.loads(target.read_text(encoding='utf-8'))
            if not isinstance(baseline,dict):
                raise ValueError('Unexpected Telegram persistent state schema')
        cached={'identity':identity,'baseline':baseline,'raw':None}
        _PERSIST_CACHE[cache_key]=cached
    now=time.time()
    durable=_persistent_view(st,cached['baseline'],now)
    raw=json.dumps(durable,ensure_ascii=False).encode('utf-8')
    if identity is not None and cached['raw']==raw:
        return False
    # On first observation after restart, compare with current disk bytes.
    if identity is not None and cached['raw'] is None:
        old_raw=target.read_bytes()
        if old_raw==raw:
            cached['raw']=old_raw
            return False
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=target.with_name(target.name+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp,target)
        dfd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if tmp.exists(): tmp.unlink()
    stat=target.stat()
    _PERSIST_CACHE[cache_key]={'raw':raw,'baseline':json.loads(raw),
        'identity':(stat.st_ino,stat.st_size,stat.st_mtime_ns)}
    return True


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
