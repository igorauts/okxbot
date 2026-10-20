#!/usr/bin/env python3
"""v0.7.12 independent, local-only execution/burst observer.

Reads ONLY bot state, bot event log, and trading service journald.
NEVER imports a trading module, reads .env, calls OKX, or writes bot state.
"""
import argparse
import json
import os
import re
import selectors
import subprocess
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from okx_runtime_paths import state_file, events_file, observer_dir

OBSERVER_VERSION = '0.7.14'
BID_PATTERN = re.compile(r'РЕЖИМ=[A-Z_]+\s*\|\s*BID=([0-9]+(?:\.[0-9]+)?)')
ENGINE_COUNTS = re.compile(r'\bBUY\s+(\d+)/(\d+)\s*\|\s*TP\s+(\d+)\s*\(реальных\s+(\d+)\)')
JOURNAL_TIME = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z))')
EVENTS = {'BUY_FILLED','BUY_FILLED_DURING_CANCEL','BUY_PLACED','BUY_CANCEL_CONFIRMED',
          'BUY_CANCEL_REQUESTED','BUY_BUFFER_CLEANUP','TP_FILLED','TP_BUFFER_CLEANUP',
          'TP_BUFFER_OVERFLOW','TP_HARD_CAP_REACHED','AGG_MARKET_INTENT','AGG_MARKET_ACK','AGG_MARKET_TERMINAL',
          'POSITION_SYNC_WAIT','POSITION_SYNC_OK','SYNC_WAIT', 'RECONCILIATION_LONG_WAIT',
          'ATTACHED_TP_DISCOVERED','PARTIAL_ATTACHED_TP_DISCOVERED'}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def num(v):
    try:
        n = Decimal(str(v))
        return n if n.is_finite() else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def parse_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def append(path, rec):
    # Diagnostic writes are intentionally buffered by the OS. No fsync or bot lock.
    with path.open('a', encoding='utf-8') as out:
        out.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':'), default=str) + '\n')


def read_json(path):
    try:
        x = json.loads(path.read_text(encoding='utf-8'))
        return x if isinstance(x, dict) else None
    except (OSError, ValueError):
        return None


def sample_state(st):
    cycles = st.get('cycles')
    if not isinstance(cycles, dict):
        raise ValueError('No valid cycles in bot state')
    buys = []
    real_tp = 0
    pending_tp = 0
    virtual = 0
    for c in cycles.values():
        if not isinstance(c, dict):
            continue
        typ = c.get('state')
        if typ in ('BUY_LIVE', 'BUY_PARTIAL'):
            px = num(c.get('buyPx'))
            if px is not None:
                buys.append(px)
        if typ == 'VIRTUAL_TP':
            virtual += 1
        for tp in c.get('tpOrders', []) or []:
            if not isinstance(tp, dict):
                continue
            if tp.get('phase') == 'ACTIVE':
                real_tp += 1
            elif tp.get('phase') in ('PENDING','PLACING','CANCELING'):
                pending_tp += 1
    return dict(buy=len(buys), tp=real_tp, tp_other=pending_tp, virtual_tp=virtual,
                top_buy=str(max(buys)) if buys else None,
                bottom_buy=str(min(buys)) if buys else None)


class Tail:
    def __init__(self, path):
        self.path = path
        self.file = None
        self.inode = None
        self.fragment = ''

    def read(self):
        try:
            if not self.path.is_file():
                return []
            stat = self.path.stat()
            if self.file is None or stat.st_ino != self.inode or stat.st_size < self.file.tell():
                if self.file:
                    self.file.close()
                self.file = self.path.open(encoding='utf-8', errors='replace')
                self.inode = stat.st_ino
                self.file.seek(0, 2)  # Only new events: no replay of old fills.
                self.fragment = ''
                return []
            rows = []
            for chunk in self.file:
                line = self.fragment + chunk
                if not line.endswith('\n'):
                    self.fragment = line
                    break
                self.fragment = ''
                try:
                    e = json.loads(line)
                    if isinstance(e, dict):
                        rows.append(e)
                except ValueError:
                    continue
            return rows
        except OSError:
            return []


def _observer_supported_version(cfg, st):
    """Allow same-version state/config >= 0.7.7, without hardcoded latest release."""
    a=cfg.get('version');b=st.get('version')
    if not isinstance(a,str) or a!=b or not re.fullmatch(r'\d+\.\d+\.\d+',a):
        return False
    return tuple(int(p) for p in a.split('.')) >= (0,7,7)

class Observer:
    def __init__(self, root, service, mode):
        self.root, self.service, self.mode = root, service, mode
        cfg = read_json(root / 'config.json')
        st = read_json(state_file(root))
        if cfg is None or st is None or not _observer_supported_version(cfg, st):
            raise RuntimeError('Observer requires consistent state/config version >= 0.7.7')
        if cfg.get('instrument') != st.get('instrument'):
            raise RuntimeError('Instrument does not match')
        self.target_buy = int(cfg['active_buy_orders'])
        self.target_tp = int(cfg.get('real_tp_target', cfg.get('active_tp_orders', 5)))
        self.grid_step = num(cfg.get('grid_step'))
        if self.grid_step is None or self.grid_step <= 0 or not 1 <= self.target_buy <= 25 or not 1 <= self.target_tp <= 25:
            raise RuntimeError('Invalid grid config')
        self.out = observer_dir(root,mode)
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tail = Tail(events_file(root))
        self.counters = Counter()
        self.bid = None
        self.bid_at = 0.
        self.last_state = None
        self.paired_run = f"{int(time.time()*1000)}-{os.getpid()}"
        self.paired_seq = 0
        self.paired_samples = deque(maxlen=64)
        self.last_sample = 0.
        self.last_summary = 0.
        self.below_since = None
        self.below_count = 0
        self.below_total_sec = 0.
        self.below_max_sec = 0.
        self.recent_fills = deque(maxlen=100)
        self.burst = None
        self.canceled = defaultdict(deque)
        self.same_price_250ms = 0
        self.same_price_1s = 0
        self.last_activity = now_iso()
        self.last_error_notice = 0.
        self.last_journal_error = None
        self.engine_sample = None
        self.engine_sample_at = 0.  # when the diagnostic process observed the journal
        self.engine_episode_first_ts = None  # actual journald timestamp, NOT cached state
        self.engine_below_periods = 0
        self.engine_below_total_sec = 0.
        self.engine_below_max_sec = 0.
        self.engine_stale_gaps = 0
        self.state_engine_disagreements = 0
        self.last_disagreement_notice = 0.
        self.mismatch_since = None
        self.mismatch_polls = 0
        self.mismatch_confirmed = 0
        self.mismatch_transient = 0
        self.mismatch_longest_sec = 0.
        self.timeline_buffer = []
        self.last_flush = time.monotonic()
        self.dropped_timeline = 0

    def message(self, kind, data):
        append(self.out / 'alerts.jsonl', {'ts':now_iso(),'kind':kind, **data})
        print('MONITOR', kind, json.dumps(data, ensure_ascii=False, default=str), flush=True)

    def event(self, e):
        kind = str(e.get('event') or '')
        t = parse_ts(e.get('ts'))
        if t is None:
            return
        if kind == 'PARTIAL_ATTACHED_TP_DISCOVERED' and 'execution_class' not in e:
            # v077 used this name even for full BUY fills. Do not claim partial.
            self.counters['ATTACHED_TP_LEGACY_UNVERIFIED'] += 1
        else:
            self.counters[kind] += 1
        if kind in EVENTS:
            # No complete payloads, credentials, or headers in diagnostic timeline.
            safe = {k:e[k] for k in ('price','cancelled','bid','ordId','cycleId','tpId','count','age_ms')
                    if k in e and isinstance(e[k], (int,float,str)) and len(str(e[k])) < 90}
            if len(self.timeline_buffer) < 1024:
                self.timeline_buffer.append({'ts':e['ts'],'event':kind, **safe})
            else:
                self.dropped_timeline += 1
        if kind in ('BUY_FILLED','BUY_FILLED_DURING_CANCEL'):
            self.recent_fills.append(t)
            if self.mode == 'burst':
                self.observe_burst(t)
        if kind == 'BUY_CANCEL_CONFIRMED':
            px = str(e.get('price') or '')
            if px:
                self.canceled[px].append((t, str(e.get('ordId') or '')))
        if kind == 'BUY_PLACED':
            px = str(e.get('price') or '')
            queue = self.canceled[px]
            while queue and t - queue[0][0] > 120:
                queue.popleft()
            if queue and t >= queue[-1][0]:
                old_ts, old_id = queue.pop()
                if old_id != str(e.get('ordId') or ''):
                    dt = t - old_ts
                    if dt <= 1:
                        self.same_price_1s += 1
                    if dt <= .25:
                        self.same_price_250ms += 1
                        self.message('SAME_PRICE_BUY_REPLACED_250MS', {'price':px,'delay_ms':round(dt*1000,1)})
        if kind in ('TP_BUFFER_OVERFLOW','TP_HARD_CAP_REACHED','RECONCILIATION_LONG_WAIT'):
            self.message(kind, {'event_ts':e['ts']})
        if self.burst is not None:
            self.burst['counts'][kind] += 1
            if kind in ('BUY_FILLED','BUY_FILLED_DURING_CANCEL'):
                self.burst['last_fill'] = t

    def observe_burst(self, t):
        while self.recent_fills and t - self.recent_fills[0] > 10:
            self.recent_fills.popleft()
        # For five-slot experiments, 3 fills in <=10s qualify as a burst.
        threshold = min(3, self.target_buy)
        if self.burst is None and len(self.recent_fills) >= threshold:
            self.burst = {'start':self.recent_fills[0], 'last_fill':t,
                          'counts':Counter({'BUY_FILLED':len(self.recent_fills)-1}), 'min_buy':None, 'max_tp':0,
                          'max_bid_gap_steps':0., 'under_buy_seconds':0.}
            print('BURST START', now_iso(), 'threshold', threshold, flush=True)

    def read_bid(self, line):
        """In-memory engine journal snapshots are the source for BUY recovery.

        state.json is a delayed persistence snapshot; never use it to time
        an apparently missing BUY. Journal processing runs OUTSIDE engine.
        """
        m=BID_PATTERN.search(line)
        if m:
            self.bid=num(m.group(1))
            self.bid_at=time.monotonic()
        counts=ENGINE_COUNTS.search(line)
        if not counts or m is None:
            return
        journal_time=JOURNAL_TIME.match(line)
        ts=parse_ts(journal_time.group(1)) if journal_time else time.time()
        if ts is None:
            ts=time.time()
        # Journal time must be credible. Never let a stale future stamp
        # inflate/erase a recovery duration.
        if abs(ts-time.time())>60:
            return
        buy,target,tp_total,tp_real=map(int,counts.groups())
        if target!=self.target_buy:
            return
        self.engine_sample={'ts':datetime.fromtimestamp(ts,timezone.utc).isoformat(),
                            'buy':buy,'target':target,'tp':tp_real,
                            'bid':str(self.bid),'source':'engine_journal'}
        self.engine_sample_at=time.monotonic()
        if buy<target:
            if self.engine_episode_first_ts is None:
                self.engine_episode_first_ts=ts
                self.engine_below_periods+=1
                self.message('ENGINE_BUY_BELOW_TARGET',{'buy':buy,'target':target,
                    'source':'engine_journal'})
        elif self.engine_episode_first_ts is not None:
            duration=max(0.,ts-self.engine_episode_first_ts)
            self.engine_below_total_sec+=duration
            self.engine_below_max_sec=max(self.engine_below_max_sec,duration)
            self.message('ENGINE_BUY_RECOVERED',{'seconds':round(duration,3),
                'target':target,'source':'engine_journal',
                'is_exchange_confirmation':False})
            self.engine_episode_first_ts=None
        if self.burst is not None:
            b=self.burst
            b['min_buy']=buy if b['min_buy'] is None else min(b['min_buy'],buy)
            b['max_tp']=max(b['max_tp'],tp_real)
        # Journal events are recorded but not written once per heartbeat.

    def snapshot(self):
        st = read_json(state_file(self.root))
        if st is None:
            if time.monotonic() - self.last_error_notice > 30:
                self.last_error_notice = time.monotonic()
                self.message('STATE_READ_FAILED', {})
            return
        try:
            s = sample_state(st)
        except ValueError:
            return
        self.last_state = s
        fresh_bid = self.bid if time.monotonic() - self.bid_at < 15 else None
        gap = None
        if fresh_bid is not None and s['top_buy'] is not None:
            gap = float((fresh_bid - Decimal(s['top_buy'])) / self.grid_step)
        s.update(ts=now_iso(), bid=str(fresh_bid) if fresh_bid is not None else None,
                 bid_above_top_steps=round(gap,3) if gap is not None else None)
        append(self.out / 'snapshots.jsonl', s)
        self.counters['samples'] += 1
        # Delayed persistent state is informative, not proof that the live bot
        # spent seconds with missing BUY. Compare to fresh engine journal only.
        if self.engine_sample and time.monotonic()-self.engine_sample_at < 5:
            s['engine_buy']=self.engine_sample['buy']
            s['engine_tp']=self.engine_sample['tp']
            s['engine_observed_ts']=self.engine_sample['ts']
            if (not self.paired_samples or
                self.paired_samples[-1]['engine_observed_ts'] != s['engine_observed_ts']):
                self.paired_seq += 1
                self.paired_samples.append({
                    'seq':self.paired_seq, 'ts':s['ts'],
                    'state_buy':s['buy'], 'engine_buy':s['engine_buy'],
                    'engine_observed_ts':s['engine_observed_ts']})
            if s['buy'] != self.engine_sample['buy']:
                self.state_engine_disagreements += 1
                now = time.monotonic()
                if self.mismatch_since is None:
                    self.mismatch_since = now
                    self.mismatch_polls = 0
                self.mismatch_polls += 1
                duration = now - self.mismatch_since
                self.mismatch_longest_sec = max(self.mismatch_longest_sec, duration)
                if (self.mode == 'execution' and self.mismatch_polls >= 3
                        and duration >= 10 and now-self.last_disagreement_notice>=30):
                    if self.mismatch_polls == 3:
                        self.mismatch_confirmed += 1
                    self.message('PERSISTED_STATE_DIFFERS_FROM_ENGINE',{
                        'state_buy':s['buy'],'engine_buy':self.engine_sample['buy'],
                        'duration_sec':round(duration,1),
                        'warning':'Not an exchange BUY deficit'})
                    self.last_disagreement_notice=now
            elif self.mismatch_since is not None:
                if self.mismatch_polls < 3:
                    self.mismatch_transient += 1
                self.mismatch_since = None
                self.mismatch_polls = 0
        else:
            s['engine_buy']=None
            # Engine reference is stale. Do not manufacture a mismatch alert.
            self.mismatch_since = None
            self.mismatch_polls = 0
        # Zero inferred BUY-recovery timers from the delayed JSON snapshot.
        if s['buy'] > self.target_buy + 5:
            self.message('BUY_EXCEEDED_BUFFER', {'buy':s['buy'], 'max':self.target_buy+5})
        if s['tp'] > 150:
            self.message('TP_EXCEEDED_HARD_CAP', {'tp':s['tp'], 'max':150})
        if self.burst is not None and gap is not None:
            self.burst['max_bid_gap_steps'] = max(self.burst['max_bid_gap_steps'],gap)
        # gap is ESTIMATED using a persisted top BUY price, not exchange book.


    def finalize_burst(self):
        b = self.burst
        if b is None:
            return
        rec = {'ts':now_iso(),'first_buy_fill_ts':datetime.fromtimestamp(b['start'],timezone.utc).isoformat(),
               'last_buy_fill_ts':datetime.fromtimestamp(b['last_fill'],timezone.utc).isoformat(),
               'counts':dict(b['counts']),'min_buy':b['min_buy'],'max_tp':b['max_tp'],
               'max_bid_above_top_steps':round(b['max_bid_gap_steps'],2),
               'sampled_seconds_below_buy_target':None,
               'buy_count_source':'engine_journal',
               'warning':'BUY shortage durations are from journal statuses, not state polling'}
        append(self.out / 'bursts.jsonl',rec)
        print('BURST COMPLETED', json.dumps(rec,ensure_ascii=False),flush=True)
        self.counters['completed_bursts'] += 1
        self.burst = None

    def summary(self):
        rec = {'ts':now_iso(),'version':OBSERVER_VERSION,'mode':self.mode,
               'service':self.service, 'instrument':self.root.name,
               'target_buy':self.target_buy,'target_tp':self.target_tp,
               'last_state':self.last_state,'events':dict(self.counters),
               'below_buy_target_periods':self.engine_below_periods,
               'below_buy_target_total_seconds':round(self.engine_below_total_sec,2),
               'below_buy_target_longest_seconds':round(self.engine_below_max_sec,2),
               'engine_latest':self.engine_sample,
               'engine_count_source':'engine_journal',
               'state_engine_disagreements':self.state_engine_disagreements,
                'state_engine_confirmed':self.mismatch_confirmed,
                'state_engine_transient':self.mismatch_transient,
                'state_engine_longest_sec':round(self.mismatch_longest_sec,1),
               'timeline_events_dropped':self.dropped_timeline,
               'cancel_then_replace_same_price_250ms':self.same_price_250ms,
               'cancel_then_replace_same_price_1s':self.same_price_1s,
               'fresh_bid':str(self.bid) if self.bid is not None and time.monotonic()-self.bid_at<15 else None,
               'note':'Read-only. BUY duration based on engine journal statuses, persisted JSON is separately sampled at 5s. Not independent OKX confirmation.'}
        rec['paired_run'] = self.paired_run
        rec['paired_samples'] = list(self.paired_samples)
        p = self.out/'summary.json'
        tmp = self.out/'.summary.tmp'
        tmp.write_text(json.dumps(rec,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(tmp,p)

    def flush_timeline(self):
        if not self.timeline_buffer:
            return
        pending=self.timeline_buffer
        self.timeline_buffer=[]
        p=self.out/'timeline.jsonl'
        try:
            with p.open('a',encoding='utf-8') as stream:
                stream.write(''.join(json.dumps(x,ensure_ascii=False,separators=(',',':'))+'\n'
                                     for x in pending))
        except OSError:
            self.dropped_timeline+=len(pending)

    def step(self):
        for e in self.tail.read():
            self.event(e)
        now = time.monotonic()
        if now - self.last_sample >= 5.:
            self.snapshot()
            self.last_sample = now
        if now-self.last_flush>=2. or len(self.timeline_buffer)>=200:
            self.flush_timeline()
            self.last_flush=now
        if self.mode == 'burst' and self.burst is not None and time.time()-self.burst['last_fill'] > 20:
            self.finalize_burst()
        if now - self.last_summary >= 20.:
            self.summary()
            self.last_summary = now


def run(root,service,mode):
    observer = Observer(root,service,mode)
    proc = None
    selector = selectors.DefaultSelector()
    fragment = b''
    print('OKX v'+OBSERVER_VERSION,mode,'observer ACTIVE / LOCAL READ ONLY / NO OKX API',flush=True)
    try:
        while True:
            if proc is None or proc.poll() is not None:
                if proc:
                    try:
                        selector.unregister(proc.stdout)
                    except Exception:
                        pass
                proc = subprocess.Popen(['journalctl','-u',service+'.service','-f','-n','0','-o','short-iso-precise', '--no-pager'],
                                        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
                os.set_blocking(proc.stdout.fileno(),False)
                selector.register(proc.stdout,selectors.EVENT_READ)
                fragment = b''
            for key,_ in selector.select(timeout=.25):
                data = os.read(key.fd,262144)
                if not data:
                    continue
                fragment += data
                while b'\n' in fragment:
                    line,fragment = fragment.split(b'\n',1)
                    observer.read_bid(line.decode('utf-8',errors='replace'))
                if len(fragment)>1048576:
                    fragment=b''
            observer.step()
    finally:
        if proc:
            proc.terminate()
        selector.close()


def report(root,mode):
    p=observer_dir(root,mode)/'summary.json'
    data=read_json(p)
    print(json.dumps(data,ensure_ascii=False,indent=2) if data else 'No observer data yet')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--service')
    p.add_argument('--mode',choices=['burst','execution'],required=True)
    p.add_argument('--report',action='store_true')
    a=p.parse_args()
    if a.report:
        report(a.root,a.mode)
    else:
        if not re.fullmatch(r'okx-grid-[a-z0-9-]+',a.service or ''):
            raise SystemExit('Invalid service')
        run(a.root.resolve(),a.service,a.mode)


if __name__=='__main__':
    main()
