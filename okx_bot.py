#!/usr/bin/env python3
"""OKX USDT linear SWAP LONG grid: verified fills and fail-closed orders."""
import base64
import hashlib
import hmac
import json
import sqlite3
import http.client
import ssl
import socket
import os
import copy
import contextlib
import secrets
import sys
import fcntl
import shutil
import threading
import queue
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path

import websocket
from okx_latency import LatencyJournal
from okx_version import ENGINE_VERSION as VER, STATE_SCHEMA
from okx_state import validate as validate_state, empty as empty_state
from okx_contracts import base_coin, validate_spec

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text())
DATA = ROOT / 'state' / 'bot_state.json'
EVENTS = ROOT / 'logs' / 'bot_events.jsonl'
INST = str(CFG.get('instrument',''))
COIN = base_coin(INST)
STEP, TP, QTY = (Decimal(str(CFG.get(k,0))) for k in
                 ('grid_step','take_profit','order_size'))
TARGET, CAP, EX_CAP = (int(CFG.get(k,0)) for k in
                       ('active_buy_orders','max_grids','max_exchange_orders'))
INTERVAL = 1.0  # main safety loop; BID updates independently via public WS
# REST orders-pending is paginated. Exchange-attached SELLs may temporarily
# exceed the baseline real TP count during multiple simultaneous BUY fills.
# Exchange-attached TP can grow during a burst. Surplus is trimmed only
# after quiet time, one confirmed cancellation per REST reconciliation.
REAL_TP_CONFIG = int(CFG.get('active_tp_orders',0))
# BUY remains capped at 30. Real SELL TP may grow up to the 150-order safety cap.
# Legacy active_tp_orders=75 remains accepted for state migration, but no longer
# controls live real TP count. New config uses 1..25 real TP baseline.
REAL_TP_TARGET = int(CFG.get('real_tp_target', min(25, REAL_TP_CONFIG)))
# independent user settings are base BUY (1..25) and base TP (1..25).
# All safety margins are FIXED constants, not user-editable config settings.
# Ignore legacy buffer/protection keys; never inherit unsafe overrides.
BUY_BUFFER = 5
BUY_MAX = TARGET + BUY_BUFFER
BUY_CANCEL_PROTECT_STEPS = 5
REAL_TP_RESERVE = TARGET + BUY_BUFFER
REAL_TP_MAX = 150  # hard TP safety limit, NOT a cleanup threshold
TP_BUFFER_COOLDOWN = 1.5
TP_BUFFER_CANCEL_INTERVAL = 0.1
BUY_BUFFER_COOLDOWN = 1.5
PRICE_WAKE_COOLDOWN = 0.5
ESCAPE_COALESCE_SEC = 0.15

REDUCE_ONLY_HARD_CAP = 150
TP_ACTIONS_PER_PASS = 5  # normal TP recovery; burst cleanup uses 1
TP_JOURNAL = ROOT / 'logs' / 'tp_transitions.jsonl'
PNL_DB = ROOT / 'state' / 'pnl.sqlite'
CONTRACT_VALUE = Decimal(str(CFG.get('contract_value',0)))
PRICE_STEP = Decimal(str(CFG.get('tick_size',0)))
CONTRACT_LOT = Decimal(str(CFG.get('contract_lot',0)))
CONTRACT_MIN = Decimal(str(CFG.get('contract_min',0)))
MAKER_COST = Decimal(str(CFG.get('maker_fee_at_setup',0)))
UPPER = Decimal(str(CFG.get('upper_price',0)))
CAPITAL_REFERENCE = Decimal(str(CFG.get('capital_reference_price',0)))
USDT_QUANT = Decimal('0.000001')
ORDER_REQUEST_TTL_MS = 8000  # OKX expTime; not the same as a live LIMIT order expiry
ABSENCE_GRACE_MS = 120000  # wait for OKX history to index any accepted order
ABSENCE_CHECK_GAP_SEC = 20
ABSENCE_REQUIRED_CHECKS = 3
ABSENCE_MAX_AGE_SEC = 6 * 24 * 3600  # within OKX 7-day history retention
# These are operator-configurable only for new bots; old instances preserve
# their existing trading parameters with these defaults during migration.
PARTIAL_AGE = int(CFG.get('partial_buy_timeout_sec',60))
PARTIAL_STEPS = Decimal(str(CFG.get('partial_buy_distance_steps',5)))
BOT_DD_DATA = ROOT / 'state' / 'bot_drawdown.json'
STATS_FINANCIAL_ANCHOR = ROOT / 'state' / 'stats_financial_anchor.json'
BOT_DD_SAMPLE_INTERVAL = 5  # seconds; refreshed via live public WS BID, no account API
BOT_DD_LOCK = threading.Lock()
if not (30 <= PARTIAL_AGE <= 3600 and 1 <= PARTIAL_STEPS <= 100):
    raise RuntimeError('Invalid partial BUY cleanup thresholds')


# dev1 instrumentation: opt-in, no trading-control changes.
# Enable with OKX_RECON_PROFILE=1, recording to logs/reconcile_profile.jsonl.
# Samples are diagnostic only, intentionally not fsynced to avoid slowing trading.
_PERF_RECON_ENABLED = False  # Set after LIVE guard evaluation, below.
_PERF_LOCAL = threading.local()
_PERF_PATH = ROOT / 'logs' / 'reconcile_profile.jsonl'

_RECON_PROFILE_MAX_BYTES = 16 * 1024 * 1024
_RECON_PROFILE_ROTATION_LOCK = threading.Lock()

def _append_recon_profile(result):
    """Bound profiling to <=16 MiB active + one archived file, best effort."""
    payload = (json.dumps(result, separators=(',', ':')) + '\n').encode('utf-8')
    with _RECON_PROFILE_ROTATION_LOCK:
        # One writer in each bot root. This is only diagnostic data; it must
        # never block or change trading decisions on log I/O failure.
        if _PERF_PATH.exists() and _PERF_PATH.stat().st_size + len(payload) > _RECON_PROFILE_MAX_BYTES:
            rotated = _PERF_PATH.with_name(_PERF_PATH.name + '.1')
            os.replace(_PERF_PATH, rotated)
        fd = os.open(_PERF_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError('Short recon profile write')
        finally:
            os.close(fd)

class _ReconProfiler:
    def __init__(self):
        self.started_ns=time.perf_counter_ns()
        self.timings_ms={}
        self.counts={}
        self.passes=0
        self.mismatches=0

    def run(self, phase, fun, *args, **kwargs):
        start=time.perf_counter_ns()
        try:
            return fun(*args, **kwargs)
        finally:
            milliseconds=(time.perf_counter_ns()-start)/1_000_000
            self.timings_ms[phase]=self.timings_ms.get(phase,0.0)+milliseconds
            self.counts[phase]=self.counts.get(phase,0)+1

    def finish(self, error_type=None):
        elapsed=(time.perf_counter_ns()-self.started_ns)/1_000_000
        if elapsed<350 and self.passes<=1 and self.counts.get('state_save',0)<2:
            return
        result={'ts':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'instrument':INST, 'duration_ms':round(elapsed,2),
            'passes':self.passes, 'mismatches':self.mismatches,
            'timings_ms':{k:round(v,2) for k,v in self.timings_ms.items()},
            'counts':self.counts}
        if error_type: result['exception']=error_type
        try:
            _append_recon_profile(result)
        except (OSError,ValueError,TypeError):
            pass  # Profiling must never affect trading decisions or error propagation.

def fmt_accounted_price(v):
    """Human-readable confirmed average execution; NEVER used for orders/PnL."""
    value = D(v)
    digits = max(2, -PRICE_STEP.as_tuple().exponent,
                 min(8, max(0, -value.as_tuple().exponent)))
    return f'{value:.{digits}f}'

def die(s):
    raise RuntimeError(s)

def fmt(x):
    price = Decimal(str(x))
    if not price.is_finite() or price % PRICE_STEP:
        die('Refusing off-tick trading price: ' + str(x))
    digits = max(2, -PRICE_STEP.as_tuple().exponent)
    return f'{price:.{digits}f}'

def D(v):
    return Decimal(str(v or '0'))

if (INST != f'{COIN}-USDT-SWAP'
    or CFG.get('direction') != 'long' or CFG.get('td_mode') != 'cross'
    or CFG.get('buy_type') != 'post_only' or CFG.get('tp_type') != 'attached_limit'
    or STEP<=0 or TP<=0 or QTY<=0 or CONTRACT_VALUE<=0 or PRICE_STEP<=0
    or CONTRACT_LOT<=0 or CONTRACT_MIN<=0 or QTY%CONTRACT_LOT
    or QTY<CONTRACT_MIN or MAKER_COST>=1
    or STEP%PRICE_STEP or TP%PRICE_STEP or UPPER<0
    or (UPPER and UPPER%PRICE_STEP)
    or CAPITAL_REFERENCE<=0 or CAPITAL_REFERENCE-STEP*(CAP-1)<=0
    or not (1<=CAP<=500 and 1<=TARGET<=min(CAP,25)
            and 1<=REAL_TP_CONFIG<=75 and 1<=REAL_TP_TARGET<=25
            and BUY_BUFFER==5 and BUY_MAX<=30
            and REAL_TP_RESERVE==TARGET+5 and REAL_TP_MAX==REDUCE_ONLY_HARD_CAP
            and BUY_CANCEL_PROTECT_STEPS==5) or EX_CAP != 500
    or D(CFG.get('buy_base_qty'))!=QTY*CONTRACT_VALUE
    or (UPPER and UPPER!=CAPITAL_REFERENCE)):
    die('Invalid USDT linear SWAP configuration. Refusing to start.')

ENV = {}
for line in (ROOT / '.env').read_text().splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); ENV[k.strip()]=v.strip().strip('"').strip("'")
if not all(ENV.get(k) for k in ('OKX_API_KEY','OKX_API_SECRET','OKX_API_PASSPHRASE')):
    die('Missing API secrets')
if ENV.get('OKX_DEMO','0') != '0':
    die('Unexpected demo mode')

# Intentional separate live activation guard. Setting config alone is NOT enough.
# Audit mode may be used even when live_trading=true in the config.
# It cannot submit or cancel orders: API.req prevents non-GET methods.
STATS_ONLY = os.getenv('OKX_GRID_STATS') == 'ONCE'
TERMINAL_ONLY = '--terminal-finalize' in sys.argv
AUDIT = os.getenv('OKX_GRID_AUDIT') == 'READ_ONLY' or STATS_ONLY or TERMINAL_ONLY
LIVE = (not AUDIT and CFG.get('live_trading') is True
        and os.getenv('OKX_GRID_LIVE') == 'CONFIRM')
FULL = os.getenv('OKX_GRID_PROBE') == 'FULL_25'
if CFG.get('live_trading') is True and not LIVE and not AUDIT:
    die('Live config requires explicit OKX_GRID_LIVE=CONFIRM')
if LIVE and not FULL:
    die('LIVE requires OKX_GRID_PROBE=FULL_25 confirmation.')



# local asynchronous latency journal, enabled for LIVE by default.
# Disable with OKX_LATENCY_PROFILE=0. Never writes to trading state/SQLite.
_PERF_RECON_ENABLED = LIVE and os.getenv('OKX_RECON_PROFILE', '1') != '0'
LATENCY = LatencyJournal(ROOT / 'logs' / 'latency.jsonl',
                         enabled=LIVE and os.getenv('OKX_LATENCY_PROFILE', '1') != '0')

def latency_ms(phase, started_ns, **fields):
    LATENCY.record(phase, (time.perf_counter_ns() - started_ns) / 1_000_000, **fields)



def new_request_deadline_ms():
    """Persist before POST; OKX will refuse late arrivals after expTime."""
    return int(time.time() * 1000) + ORDER_REQUEST_TTL_MS


def post_absence_confirmed(rec, side, st=None):
    """Conservative multi-pass negative verification; fail closed on incomplete pages.

    Only applies to expTime-protected *standalone* order placements.  A full
    instrument-scoped seven-day order history must cover intent creation time;
    an absent order is checked on 3 separate successful snapshots after grace.
    Absence is operational evidence, not a formal exchange guarantee.
    """
    expiry = int(rec.get('expTimeMs') or 0)
    placed = int(float(rec.get('placedAt') or 0) * 1000)
    if not expiry or not placed or side not in ('buy', 'sell'):
        return False  # legacy v0.6.9 intents are never auto-cleared
    now = time.time()
    age = now - float(rec['placedAt'])
    if (age < 0 or age > ABSENCE_MAX_AGE_SEC
            or int(now * 1000) < expiry + ABSENCE_GRACE_MS):
        return False
    if now - float(rec.get('absenceCheckAt') or 0) < ABSENCE_CHECK_GAP_SEC:
        return False
    # Recheck actual OKX clock.  Reject local-clock divergence and data failures.
    server_time = A.server_time_ms()
    if abs(server_time - int(time.time()*1000)) > 30000 or server_time < expiry + ABSENCE_GRACE_MS:
        raise ExchangeTemporary('OKX/server clock disagreement; POST outcome stays unknown')
    cursor = None
    seen = set()
    found = False
    covered = False
    for _ in range(20):
        params = {'instType':'SWAP', 'instId':INST, 'limit':'100'}
        if cursor:params['after'] = cursor
        page = A.req('GET', '/api/v5/trade/orders-history', params)
        for order in page:
            if order.get('instId') != INST or not str(order.get('ordId') or ''):
                raise ExchangeUnresolved('Cannot trust order-history identity for absence proof')
            if order.get('clOrdId') == rec.get('clOrdId'):
                found = True
            ctime = order.get('cTime')
            if not str(ctime or '').isdigit():
                raise ExchangeUnresolved('Order-history cTime absent; cannot prove absence')
        if found:return False
        if len(page)<100:
            covered = True
            break
        if min(int(o['cTime']) for o in page) < placed-10000:
            covered = True
            break
        following = str(page[-1]['ordId'])
        if not following or following == cursor or following in seen:
            raise ExchangeUnresolved('Order history pagination incomplete')
        cursor = following
        seen.add(cursor)
    if not covered:
        return False
    # An explicit order query and fresh pending snapshot also ran in track().
    # No repeats within one call; confirmations span at least 40 seconds.
    rec['absenceChecks'] = int(rec.get('absenceChecks') or 0) + 1
    rec['absenceCheckAt'] = now
    log('POST_ABSENCE_PROBE', side=side, clOrdId=rec['clOrdId'],
        checks=rec['absenceChecks'], required=ABSENCE_REQUIRED_CHECKS)
    if st is not None:save(st)
    return rec['absenceChecks'] >= ABSENCE_REQUIRED_CHECKS


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value, f, separators=(',', ':'), ensure_ascii=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
    dirfd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)

def log(event, **details):
    record={'ts':datetime.now(timezone.utc).isoformat(),'event':event,**details}
    fd=os.open(EVENTS,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a') as f: f.write(json.dumps(record,ensure_ascii=False)+'\n')
    t = record['ts'][11:19]
    # Full structured events remain in logs/bot_events.jsonl, even when
    # we suppress redundant terminal messages from the WebSocket stream.
    if event == 'WS_ORDER':
        return
    if event == 'BUY_PLACED':
        print(f"📌 BUY {details['price']} → TP {details['tp']} | LIMIT", flush=True)
    elif event == 'BUY_FILLED':
        print(f"💥 Вход {details['price']} исполнен → ждём TP {details['tp']}",flush=True)
    elif event == 'TP_FILLED':
        # Money lines follow when individual exchange fills can be verified.
        pass
    elif event == 'TP_LINKED':
        print(f"🔗 TP {details['price']} привязан | SELL {details['tpOrdId']}",flush=True)
    elif event == 'BUY_MOVED_UP':
        print(f"↗️ Сетка вверх | отменён BUY {details['cancelled']}",flush=True)
    elif event == 'POSITION_SYNC_WAIT':
        print(f"⏳ Сверка позиции: биржа {details['exchange']} / бот {details['local']}",flush=True)
    elif event == 'POSITION_SYNC_OK':
        print(f"✅ Позиция синхронизирована: {details['exchange']}",flush=True)
    else:
        print(f"[{t}] {event}",details,flush=True)

def financial_signature(st):
    """Fingerprint accounting-relevant cycle changes; unfilled BUY moves are PnL-neutral.

    A cached fill-ledger PnL may be marked to BID only while this fingerprint
    is unchanged. After a fill or TP settlement, wait for authoritative stats.
    """
    rows=[]
    cycles=list(st.get('cycles',{}).values())+list(st.get('archived_cycles',[]))
    for c in cycles:
        buy=D(c.get('filledBuy')); sell=D(c.get('filledTP'))
        if not buy and not sell and c.get('state') not in ('CLOSED','EXITED'):
            continue
        rows.append([str(c.get('cycleId') or c.get('ordId') or c.get('clOrdId') or ''),
            str(c.get('ordId') or ''),c.get('state'),str(buy),str(sell),
            [[str(r.get('ordId') or ''),str(D(r.get('filled')))]
             for r in c.get('tpOrders',[])]])
    raw=json.dumps(sorted(rows,key=lambda r:tuple(str(x) for x in r[:2])),
                   separators=(',',':'),ensure_ascii=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def record_bot_drawdown(total_pnl, now=None, source='fills', bid=None):
    """Most negative *total bot PnL* since monitoring began, not account equity.

    Intentionally measures max loss versus zero (user's definition), NOT
    conventional peak-to-trough drawdown. No exchange writes or account reads.
    """
    now=time.time() if now is None else float(now)
    total_pnl=D(total_pnl)
    if not total_pnl.is_finite():raise ValueError('Non-finite bot PnL sample')
    with BOT_DD_LOCK:
        old=json.loads(BOT_DD_DATA.read_text()) if BOT_DD_DATA.exists() else {}
        if old and (old.get('schema')!=1 or old.get('instrument')!=INST):
            raise ValueError('Bot drawdown file instrument/schema mismatch')
        worst=min(Decimal(0),D(old.get('worst_total_pnl_usdt','0')),total_pnl)
        prior=D(old.get('worst_total_pnl_usdt','0'))
        when=(now if not old or worst<prior else old.get('max_drawdown_at',now))
        latest=isinstance(old,dict) and now>=float(old.get('last_at',-1))
        latest_pnl=total_pnl if latest or not old else D(old.get('latest_total_pnl_usdt','0'))
        payload={'schema':1,'instrument':INST,'bot':CFG.get('name',''),
                 'first_at':old.get('first_at',now),'last_at':max(now,float(old.get('last_at',now))),
                 'samples':int(old.get('samples',0))+1,
                 'latest_total_pnl_usdt':str(latest_pnl),
                 'current_drawdown_usdt':str(max(Decimal(0),-latest_pnl)),
                 'worst_total_pnl_usdt':str(worst),
                 'max_drawdown_usdt':str(-worst),'max_drawdown_at':when,
                 'last_source':source,'last_bid':str(bid) if bid is not None else None}
        atomic_json(BOT_DD_DATA,payload)
        return payload


def bot_dd_mark(anchor, st, bid):
    """Fast exact BID adjustment to a previously verified fill-based baseline.

    Do not extrapolate after a trade until history fetches all actual fills.
    """
    if (anchor.get('schema')!=1 or anchor.get('instrument')!=INST
        or anchor.get('signature')!=financial_signature(st)):
        return None
    bid=D(bid)
    if bid<=0 or not bid.is_finite():return None
    return (D(anchor['total_pnl_usdt']) + D(anchor['open_contracts']) *
            CONTRACT_VALUE * (bid-D(anchor['bid'])))


class ExchangeTemporary(RuntimeError):
    """Safe GET/transient API failure: halt new writes but keep process alive."""



class OrderExpiredBeforeSend(RuntimeError):
    """Locally refused before any network request; not an ambiguous outcome."""

class ExchangeUnresolved(RuntimeError):
    """Durably uncertain order or position: no write until exact reconciliation."""


class RateLimitedError(ExchangeTemporary):
    """OKX 429/50011 persisted after safe GET-only retries."""


class RestGovernor:
    """One shared in-process gate for trading, PnL and full statistics.

    The 3-month fills-history endpoint permits 10 requests / 2 seconds per
    OKX user. Use 0.55s between requests across BOTH analytics threads,
    including other instances of API in this process. Trading queries and
    order writes do not wait for an analytics network request to complete.
    NOTE: another process using the same OKX user is not governed here.
    """
    def __init__(self):
        self.cv=threading.Condition()
        self.next_endpoint={}
        self.cooldown={}
        self.last_any_get=0.0
        self.urgent_inflight=0
        self.last_urgent=0.0

    def begin(self, method, route, background=False):
        urgent=not background
        if urgent:
            with self.cv:
                self.urgent_inflight+=1
                self.last_urgent=time.monotonic()
                self.cv.notify_all()
        background_wait_since=time.monotonic()
        with self.cv:
            while True:
                now=time.monotonic()
                delay=max(0.0, self.cooldown.get(route,0)-now,
                          self.next_endpoint.get(route,0)-now)
                if method=='GET':
                    delay=max(delay, self.last_any_get+0.045-now)
                if background:
                    # Yield REST capacity to order/position reconciliation.
                    if self.urgent_inflight and now-background_wait_since<2.0:
                        delay=max(delay,0.15)
                    elif (not self.urgent_inflight and now-self.last_urgent<0.15
                          and now-background_wait_since<2.0):
                        delay=max(delay,0.15-(now-self.last_urgent))
                if delay>0:
                    self.cv.wait(timeout=delay)
                    continue
                if method=='GET':self.last_any_get=now
                if route=='/api/v5/trade/fills-history':
                    self.next_endpoint[route]=now+0.55
                break
        return urgent

    def end(self, urgent):
        if urgent:
            with self.cv:
                self.urgent_inflight-=1
                self.last_urgent=time.monotonic()
                self.cv.notify_all()

    def throttle(self, route, seconds):
        with self.cv:
            self.cooldown[route]=max(self.cooldown.get(route,0),
                                    time.monotonic()+seconds)
            self.cv.notify_all()

REST_GATE=RestGovernor()

from okx_positions_fifo import AccountPositionsFIFO, GateUnavailable
from okx_emergency_gate import (EmergencyPositionsGate, EmergencyGateUnavailable,
                                eligible_emergency_failure)
EMERGENCY_POSITIONS_GATE = EmergencyPositionsGate()
class _AccountPositionsGate(AccountPositionsFIFO):
    def __init__(self):
        shared=Path(ENV.get('OKX_ACCOUNT_POSITIONS_GATE_PATH',
                            '/root/okx/.account_positions_gate_v075.json'))
        super().__init__(shared, reporter=lambda phase,ms,**fields:
                         LATENCY.record(phase,ms,**fields))
        self._last_warning=0.0
        self._last_emergency_notice=float('-inf')
    def take(self):
        try:
            return super().take()
        except (GateUnavailable, OSError) as exc:
            # Only a genuine STORAGE failure can enable emergency GET.
            # Saturated or timed-out FIFO must NEVER become extra capacity.
            # control_mode() re-reads persisted control to verify HARD/PANIC.
            if eligible_emergency_failure(exc) and control_mode() in ('HARD','PANIC'):
                try:
                    EMERGENCY_POSITIONS_GATE.take()
                    # Do not consume an emergency permit for a RUN GET if an
                    # operator changed control mode during the bounded wait.
                    if control_mode() not in ('HARD','PANIC'):
                        raise ExchangeTemporary('Emergency mode exited before position GET')
                except EmergencyGateUnavailable as fail:
                    try:log('EMERGENCY_POSITIONS_GATE_BLOCKED',severity='CRITICAL',
                            reason=type(fail).__name__)
                    except Exception:pass
                    raise ExchangeTemporary('Both position gates unavailable in emergency') from fail
                now=time.monotonic()
                if now-self._last_emergency_notice>=30:
                    self._last_emergency_notice=now
                    try:log('EMERGENCY_POSITIONS_READ',severity='CRITICAL',
                            mode=control_mode(),reason=type(exc).__name__,
                            note='Emergency read only; no relaxation of position/order checks')
                    except Exception:pass
                return
            # No local fallback for RUN/PAUSE/SOFT, and none for FIFO congestion.
            now=time.monotonic()
            if now-self._last_warning>=60:
                self._last_warning=now
                try:log('ACCOUNT_POSITIONS_GATE_DEGRADED',error=type(exc).__name__)
                except Exception:pass
            raise ExchangeTemporary('Shared positions gate unavailable; no trade without fresh REST') from exc

ACCOUNT_POSITIONS_GATE=_AccountPositionsGate()
from okx_positions_fifo import FillsHistoryFIFO
FILL_HISTORY_GATE=FillsHistoryFIFO(Path(ENV.get('OKX_FILL_HISTORY_GATE_PATH',
                                  '/root/okx/.account_fills_history_gate_v0712.json')))

from okx_fill_singleflight import FillSingleFlight
from okx_api import build_api
FILL_FETCH_FLIGHT=FillSingleFlight()
API=build_api(
    ENV=ENV, CFG=CFG, COIN=COIN, INST=INST, LIVE=LIVE, VER=VER, D=D, die=die,
    CONTRACT_LOT=CONTRACT_LOT, CONTRACT_MIN=CONTRACT_MIN,
    CONTRACT_VALUE=CONTRACT_VALUE, PRICE_STEP=PRICE_STEP,
    QTY=QTY, STEP=STEP, TP=TP, ExchangeTemporary=ExchangeTemporary,
    ExchangeUnresolved=ExchangeUnresolved, OrderExpiredBeforeSend=OrderExpiredBeforeSend,
    RateLimitedError=RateLimitedError, GateUnavailable=GateUnavailable,
    REST_GATE=REST_GATE, FILL_HISTORY_GATE=FILL_HISTORY_GATE,
    ACCOUNT_POSITIONS_GATE=ACCOUNT_POSITIONS_GATE,
    FILL_FETCH_FLIGHT=FILL_FETCH_FLIGHT, _PERF_LOCAL=_PERF_LOCAL,
    _PERF_RECON_ENABLED=_PERF_RECON_ENABLED,
    _flush_confirmed_facts=lambda: _flush_confirmed_facts(),
    latency_ms=latency_ms, fill_store=lambda: globals().get('FILL_STORE'))
A=API()

# account-wide safety latch. Each bot trading the SAME margin account
# MUST use the SAME persistent absolute path. Default assumes a shared VPS.
ACCOUNT_SOFT_STOP = Path(ENV.get('OKX_ACCOUNT_SOFT_STOP_PATH',
                                   '/var/lib/okxbot/account_soft_stop.json'))
if not ACCOUNT_SOFT_STOP.is_absolute():
    die('OKX_ACCOUNT_SOFT_STOP_PATH must be absolute')
ACCOUNT_SOFT_LOCK = ACCOUNT_SOFT_STOP.with_name(ACCOUNT_SOFT_STOP.name + '.lock')

def prepare_account_safety():
    """Called before LIVE trading; unavailable shared storage is fail-closed."""
    ACCOUNT_SOFT_STOP.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(ACCOUNT_SOFT_LOCK,
              os.O_CREAT|os.O_RDWR, 0o600),'r+'):
        pass
    # Test durable write permission without changing an existing latch.
    probe=ACCOUNT_SOFT_STOP.with_name(ACCOUNT_SOFT_STOP.name+'.probe.'+secrets.token_hex(6))
    try:
        fd=os.open(probe,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:
            out.write('ok');out.flush();os.fsync(out.fileno())
    finally:
        probe.unlink(missing_ok=True)
    if ACCOUNT_SOFT_STOP.exists():
        with ACCOUNT_SOFT_STOP.open('rb') as f:
            if json.load(f).get('mode')!='SOFT':
                die('Invalid shared SOFT latch; manual inspection required')

def account_soft_active():
    try:
        if not ACCOUNT_SOFT_STOP.exists():return False
        data=json.loads(ACCOUNT_SOFT_STOP.read_text(encoding='utf-8'))
        if data.get('mode')!='SOFT' or data.get('schema')!=1:
            die('Invalid shared SOFT safety latch. Manual inspection required')
        return True
    except OSError as exc:
        raise RuntimeError('Cannot read account-wide safety latch; refusing BUY') from exc

def margin_rejection(scode):
    # OKX 51008 / 51008_xxxx: insufficient balance or margin.
    return str(scode or '').split('_',1)[0]=='51008'


def exchange_cancel_margin(order):
    # OKX documented cancellation reasons: 2=position margin,
    # 3=margin-risk cancel, 6=ADL risk cancel, 9=balance after funding.
    # Only positive *exchange* reason codes, not operator cancels, trigger stop.
    return (order.get('state') in ('canceled', 'mmp_canceled') and
            str(order.get('cancelSource') or '') in ('2', '3', '6', '9'))


def trip_if_margin_cancel(order, action):
    if exchange_cancel_margin(order):
        trip_account_soft_stop('cancelSource:'+str(order['cancelSource']),
            str(order.get('cancelSourceReason') or '')[:160],action)

def trip_account_soft_stop(scode, message, action):
    """Durable account-wide latch BEFORE any further request to open exposure."""
    prepare_account_safety()
    fd=os.open(ACCOUNT_SOFT_LOCK,os.O_CREAT|os.O_RDWR,0o600)
    new_stop=False
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        if not account_soft_active():
            new_stop=True
            atomic_json(ACCOUNT_SOFT_STOP, {
                'schema':1, 'mode':'SOFT', 'reason':'MARGIN_REJECTION',
                'trigger_instrument':INST, 'trigger_bot':CFG.get('name',''),
                'code':str(scode), 'action':str(action),
                'message':str(message)[:180],
                'at':datetime.now(timezone.utc).isoformat(),
                'manual_reset_required':True})
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)
    if new_stop:
        log('ACCOUNT_AUTO_SOFT_STOP',instrument=INST,code=str(scode),
            action=action,reason=str(message)[:140],
            message='ALL account bots must stop BUY and cancel known BUY limits; manual re-arm only')
    # Also persist this bot's SOFT mode; peer bots persist theirs upon observing
    # the shared latch. Removing the latch alone NEVER restarts trading.
    control_mode()


# Local append-only record of exchange executions. Fills are unique by ordId+billId
# (NOT by grid price or client-side order lifetime). SQLite transactions are
# committed BEFORE a completed financial snapshot can use them.
FILL_DB = ROOT / 'state' / 'fills.sqlite'
ARCHIVE_DB = ROOT / 'state' / 'cycles.sqlite'

class FillStore:
    def __init__(self):
        self.lock=threading.RLock()

    def _conn(self):
        FILL_DB.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(str(FILL_DB),timeout=20)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS fills ('
                   'order_id TEXT NOT NULL, bill_id TEXT NOT NULL, qty TEXT NOT NULL, '
                   'data TEXT NOT NULL, PRIMARY KEY(order_id,bill_id))')
        db.execute('CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        os.chmod(FILL_DB,0o600)
        return db

    @contextlib.contextmanager
    def _managed_conn(self):
        db=self._conn()
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def _validate(f):
        oid=str(f.get('ordId') or '')
        bill=str(f.get('billId') or '')
        if (not oid or not bill or f.get('instId',INST)!=INST or
            f.get('side') not in ('buy','sell') or
            f.get('execType') not in ('M','T') or
            f.get('feeCcy')!='USDT' or f.get('fee') in (None,'') or
            D(f.get('fillPx'))<=0 or D(f.get('fillSz'))<=0):
            raise ValueError('Incomplete/foreign fill; refusing durable financial archive')
        return oid,bill

    def insert(self,rows):
        if not rows:return 0
        with self.lock,self._managed_conn() as db:
            before=db.total_changes
            for f in rows:
                oid,bill=self._validate(f)
                raw=json.dumps(f,ensure_ascii=False,separators=(',',':'))
                prior=db.execute('SELECT data FROM fills WHERE order_id=? AND bill_id=?',
                                 (oid,bill)).fetchone()
                if prior:
                    # OKX may add non-financial fields later. Recorded execution
                    # must keep SAME size/fee/price/side/execType forever.
                    old=json.loads(prior[0])
                    fields=('fillPx','fillSz','fee','feeCcy','side','execType','tradeId')
                    if any(str(old.get(k))!=str(f.get(k)) for k in fields):
                        raise ValueError(f'Conflicting exchange fill {oid}/{bill}')
                else:
                    db.execute('INSERT INTO fills VALUES (?,?,?,?)',
                               (oid,bill,str(f['fillSz']),raw))
            return db.total_changes-before

    def initialized(self):
        with self.lock,self._managed_conn() as db:
            row=db.execute('SELECT value FROM meta WHERE name="complete_bootstrap"').fetchone()
            return bool(row and row[0]=='1')

    def bootstrap(self,st):
        # Fresh engine: an uninitialized fill archive must not accompany filled cycles.
        if self.initialized():return
        if expected_filled_orders(st):
            raise ExchangeUnresolved('Fill archive missing for populated state')
        with self.lock,self._managed_conn() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('complete_bootstrap','1'))

    def get_rows(self,order_ids):
        order_ids=[str(x) for x in order_ids if x]
        if not order_ids:return []
        with self.lock,self._managed_conn() as db:
            rows=[]
            for i in range(0,len(order_ids),250):
                batch=order_ids[i:i+250]
                sql='SELECT data FROM fills WHERE order_id IN ('+','.join('?' for _ in batch)+')'
                rows.extend(json.loads(item[0]) for item in db.execute(sql,batch))
            return rows

    def ensure_expected(self,expected):
        if not expected:return []
        rows=self.get_rows(expected.keys())
        volumes={}
        for f in rows:
            oid=str(f['ordId'])
            volumes[oid]=volumes.get(oid,Decimal(0))+D(f['fillSz'])
        missing=[oid for oid,qty in expected.items() if volumes.get(oid,Decimal(0))<qty]
        for oid in missing:
            # After bootstrap ONLY exact missing order IDs are downloaded,
            # never a full historic scan per report. Safe, bounded GET retries.
            A.fills_by_order(oid)
        if missing:rows=self.get_rows(expected.keys())
        return rows

FILL_STORE=FillStore()



# Shared exchange MARKET fills belong to one order. Allocate them deterministically
# to separate parent cycles without duplicating a tradeId, size, price or fee.
# A FIFO allocation of fills is used for partially executed MARKET orders.
def allocated_market_fills(order_fills, plan, cycle_id):
    allocations={str(row['cycleId']):D(row['sz']) for row in plan}
    if str(cycle_id) not in allocations:raise ValueError('Missing aggregate cycle allocation')
    if len(allocations)!=len(plan) or any(q<=0 for q in allocations.values()):
        raise ValueError('Invalid aggregate allocation plan')
    remaining=allocations.copy()
    mine=[]
    seen=set()
    for f in sorted(order_fills,key=lambda r:(int(r.get('fillTime') or r.get('ts') or 0), str(r.get('tradeId')))):
        oid=str(f.get('ordId') or '')
        tid=str(f.get('tradeId') or '')
        if not oid or not tid or (oid,tid) in seen:raise ValueError('Duplicate/invalid aggregate fill')
        seen.add((oid,tid))
        qty=D(f['fillSz'])
        if qty<=0:raise ValueError('Invalid aggregate fill size')
        leftover=qty
        leftover_fee=D(f['fee'])
        chunks=[]
        for key in allocations:
            chunk=min(remaining[key],leftover)
            if chunk<=0:continue
            chunks.append((key,chunk))
            remaining[key]-=chunk
            leftover-=chunk
            if leftover==0:break
        if leftover:raise ValueError('Aggregate MARKET overfilled its reserved quantity')
        for i,(key,chunk) in enumerate(chunks):
            fee=(leftover_fee if i==len(chunks)-1
                 else D(f['fee'])*chunk/qty)
            leftover_fee-=fee
            if key==str(cycle_id):
                item=dict(f)
                item['fillSz']=str(chunk)
                item['fee']=str(fee)
                mine.append(item)
    return mine


def expected_cycle_sells(c, indexed):
    """Verified per-cycle fills: actual exchange order with deterministic partition."""
    result=[]
    for r in c.get('tpOrders',[]):
        if D(r.get('filled'))<=0:continue
        rows=indexed.get(str(r['ordId']),[])
        if r.get('source')=='aggregate_exit':
            rows=allocated_market_fills(rows,r['aggregatePlan'],c['cycleId'])
        result.extend(rows)
    return result


def accumulate_expected_sell(expected, leg):
    oid=str(leg.get('ordId') or '')
    if not oid:raise ValueError('Filled SELL missing order ID')
    qty=D(leg['filled'])
    # Legacy individual SELL has a globally unique order ID. Aggregate SELL
    # references are shared, so contributions MUST be summed, not overwritten.
    if leg.get('source')=='aggregate_exit':
        expected[oid]=expected.get(oid,Decimal(0))+qty
    elif oid in expected:
        raise ValueError('Nonaggregate SELL order reused across cycles')
    else:
        expected[oid]=qty


def archived_rows():
    if not ARCHIVE_DB.exists():return []
    with contextlib.closing(sqlite3.connect(str(ARCHIVE_DB),timeout=20)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        return [json.loads(row[0]) for row in db.execute('SELECT data FROM cycles')]


def all_stat_cycles(st):
    present={}
    for c in (archived_rows()+list(st.get('archived_cycles',[]))+
              list(st.get('cycles',{}).values())):
        ident=str(c.get('cycleId') or c.get('ordId') or '')
        if not ident:raise ValueError('Archived cycle missing identity')
        present[ident]=c
    return list(present.values())


def compact_archived(st,pnl):
    old=st.get('archived_cycles',[])
    eligible=[];retained=[]
    for c in old:
        if c.get('state')=='CANCELED' and not D(c.get('filledBuy')):
            eligible.append(c)
        elif c.get('state') in ('CLOSED','EXITED') and pnl.recorded(c):
            eligible.append(c)
        else:retained.append(c)
    if not eligible:return 0
    ARCHIVE_DB.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with contextlib.closing(sqlite3.connect(str(ARCHIVE_DB),timeout=20)) as db:
        with db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            for c in eligible:
                db.execute('INSERT OR IGNORE INTO cycles VALUES (?,?)',
                           (str(c['cycleId']),json.dumps(c,ensure_ascii=False)))
    os.chmod(ARCHIVE_DB,0o600)
    # Persist durable archive first. On crash duplicate is harmless and
    # removed on the next compaction; a closed cycle is NEVER lost.
    st['archived_cycles']=retained
    save(st)
    return len(eligible)


def newid(prefix):
    return 'OX4'+prefix+str(int(time.time()*1000))+secrets.token_hex(4)

def _durable_save(state):
    # Keep fsync(file), atomic os.replace and fsync(directory) unchanged.
    p=getattr(_PERF_LOCAL,'current',None) if globals().get('_PERF_RECON_ENABLED',False) else None
    if p is not None:
        return p.run('state_save',atomic_json,DATA,state)
    return atomic_json(DATA,state)

def _flush_confirmed_facts():
    state=getattr(_PERF_LOCAL,'deferred_state',None)
    if state is not None and getattr(_PERF_LOCAL,'deferred_dirty',False):
        # On I/O failure, do not silently mark dirty state as persisted.
        _durable_save(state)
        _PERF_LOCAL.deferred_dirty=False

def save(state):
    # Only this thread, only while reconciling known exchange facts.
    if state is getattr(_PERF_LOCAL,'deferred_state',None):
        _PERF_LOCAL.deferred_dirty=True
        return
    return _durable_save(state)

@contextlib.contextmanager
def _batch_confirmed_facts(state):
    if getattr(_PERF_LOCAL,'deferred_state',None) is not None:
        raise RuntimeError('Nested exchange fact batching forbidden')
    _PERF_LOCAL.deferred_state=state
    _PERF_LOCAL.deferred_dirty=False
    try:
        yield
    finally:
        try:
            # On GET errors as well: leave no unsaved in-memory transitions.
            # If disk fails, propagate the exception and fail closed.
            _flush_confirmed_facts()
        finally:
            _PERF_LOCAL.deferred_state=None
            _PERF_LOCAL.deferred_dirty=False

# A separate persistent control plane; never edited by the analytics worker.
# The manager submits a request by atomic rename. Only the trading process
# sends/cancels orders and writes the acknowledgement, under bot.lock.
CONTROL_FILE = ROOT / 'state' / 'control.json'
CONTROL_ACK = ROOT / 'state' / 'control_ack.json'
TERMINAL_STATS = ROOT / 'state' / 'final_stats.json'
CONTROL_MODES = {'RUN', 'PAUSE', 'SOFT', 'HARD', 'PANIC'}
TERMINAL_ACKS = {'RETIRED', 'PANIC_DONE'}


def control_mode():
    raw=(json.loads(CONTROL_FILE.read_text(encoding='utf-8'))
         if CONTROL_FILE.exists() else {'mode':'RUN'})
    if not isinstance(raw, dict) or raw.get('mode') not in CONTROL_MODES:
        die('Invalid control.json; refusing new writes')
    if account_soft_active() and raw['mode'] not in ('SOFT','HARD','PANIC'):
        # Persist locally: a manual shared-latch reset cannot automatically
        # resume either instance; the operator must separately command RUN.
        raw=dict(raw)
        raw['mode']='SOFT'
        raw['auto_soft_stop']=True
        raw['auto_soft_reason']='ACCOUNT_MARGIN_REJECTION'
        atomic_json(CONTROL_FILE,raw)
        log('AUTO_SOFT_MODE_LATCHED',instrument=INST)
    return raw['mode']


def control_ack(mode, phase, **details):
    record = {'schema_version':1, 'mode':mode, 'phase':phase,
              'updated_at':datetime.now(timezone.utc).isoformat(), **details}
    # No unnecessary fsync on every one-second reconciliation.
    old = None
    if CONTROL_ACK.exists():
        try: old = json.loads(CONTROL_ACK.read_text())
        except (ValueError, OSError): pass
    signature = (record['mode'],record['phase'],details)
    if old and (old.get('mode'), old.get('phase'),
                {k:v for k,v in old.items() if k not in ('schema_version','mode','phase','updated_at')}) == signature:
        if mode=='SOFT' and phase=='SOFT_STOPPED' and account_soft_active():
            # A new account latch can trip AFTER a bot was already SOFT.
            # Acknowledge it only after another successful SOFT safety pass,
            # preserving the manager's strict post-latch timestamp check.
            latch=json.loads(ACCOUNT_SOFT_STOP.read_text(encoding='utf-8'))
            at=datetime.fromisoformat(str(latch['at']).replace('Z','+00:00'))
            previous=datetime.fromisoformat(str(old['updated_at']).replace('Z','+00:00'))
            if at.tzinfo is None or previous.tzinfo is None:
                raise RuntimeError('Invalid SOFT acknowledgment/latch timestamp')
            if previous>=at:
                return
        else:
            return
    atomic_json(CONTROL_ACK,record)
    log('CONTROL_STATE',mode=mode,phase=phase,**details)


def controller_owns_orders(st, pending):
    """Fail closed if any pending order on this instrument isn't ours."""
    verify_existing(st,pending)
    # A panic order is also tracked using a durable client ID.
    known_oids={e.get('ordId') for e in st.get('panic_orders',[])}
    known_cids={e.get('clOrdId') for e in st.get('panic_orders',[])}
    for order in pending:
        if order.get('ordId') in known_oids or order.get('clOrdId') in known_cids:
            continue
        # verify_existing already checked tracked cycles.


def panic_executions(st):
    return sum((D(e.get('filled')) for e in st.get('panic_orders',[])),Decimal(0))


def reconcile_panic_orders(st, pending=None):
    """Resolve market SELLs by durable order ID, never by amount or price.

    Unknown POSTs can only be cleared after the expTime-aware, three-pass
    historical absence proof AND a fresh pending-order snapshot. Absent proof
    is never inferred from one missing GET or from a transport error.
    """
    all_resolved=True
    for e in st.get('panic_orders',[]):
        if e.get('state') in ('filled','canceled','mmp_canceled',
                              'rejected','EXPIRED_ABSENT'):
            continue
        order=A.order(oid=e['ordId']) if e.get('ordId') else A.order(cid=e['clOrdId'])
        if order is None:
            # A complete fresh /orders-pending is required for negative proof.
            if (pending is not None and not e.get('ordId') and not any(
                    o.get('clOrdId')==e['clOrdId'] or
                    (e.get('ordId') and o.get('ordId')==e['ordId'])
                    for o in pending) and post_absence_confirmed(e,'sell',st)):
                e['state']='EXPIRED_ABSENT';e['resolvedAt']=time.time();save(st)
                log('PANIC_MARKET_ABSENT_VERIFIED',clOrdId=e['clOrdId'],
                    severity='WARNING',message='Three independent history probes; old client ID is never reused')
                continue
            all_resolved=False
            if time.time()-float(e.get('placedAt',0))>30 and not e.get('warned'):
                e['warned']=True;save(st)
                log('PANIC_UNRESOLVED',clOrdId=e['clOrdId'],
                    severity='CRITICAL',message='Market outcome unknown; no duplicate POST')
            continue
        if (order.get('instId')!=INST or order.get('clOrdId')!=e['clOrdId']
            or order.get('side')!='sell' or order.get('ordType')!='market'
            or order.get('reduceOnly') not in ('true',True)):
            die('PANIC order identity/side/reduceOnly mismatch: no further writes')
        if e.get('ordId') and e['ordId']!=order.get('ordId'):
            die('PANIC order ID changed unexpectedly')
        if D(order.get('sz'))!=D(e['sz']):
            die('PANIC order size changed unexpectedly')
        filled=D(order.get('accFillSz'))
        if filled<D(e.get('filled')) or filled>D(e['sz']):
            die('Nonmonotonic PANIC fill; no further writes')
        state=order.get('state')
        if state not in ('live','partially_filled','filled','canceled','mmp_canceled'):
            die('Unknown PANIC order state: '+str(state))
        change=(not e.get('ordId') or filled!=D(e.get('filled')) or e.get('state')!=state)
        e.update(ordId=order['ordId'],filled=str(filled),state=state)
        if change:
            save(st);log('PANIC_ORDER_UPDATE',ordId=e['ordId'],filled=str(filled),state=state)
        if state not in ('filled','canceled','mmp_canceled'):
            all_resolved=False
    return all_resolved


PANIC_MAX_POST_ATTEMPTS = 3
PANIC_RETRY_BACKOFF_SEC = 30


def panic_retry_delay(st):
    """Every prior MARKET must be terminal before a distinct clOrdId is used.

    Rejected/expired requests count toward a persistent per-PANIC cap; an
    unknown intent does NOT count as proof that it is safe to retry.
    """
    orders=st.get('panic_orders',[])
    if not orders:
        return 0.0
    if any(e.get('state') not in ('filled','canceled','mmp_canceled',
                                  'rejected','EXPIRED_ABSENT') for e in orders):
        raise ExchangeUnresolved('PANIC order still uncertain: no further POST')
    if sum(e.get('state') in ('rejected','EXPIRED_ABSENT')
           for e in orders)>=PANIC_MAX_POST_ATTEMPTS:
        return None
    last=orders[-1]
    if last.get('state') not in ('rejected','EXPIRED_ABSENT'):
        return 0.0  # confirmed partial/canceled MARKET, remaining position verified
    return max(0.0, PANIC_RETRY_BACKOFF_SEC*(2**(sum(e.get('state') in ('rejected','EXPIRED_ABSENT') for e in orders)-1)) -
               (time.time()-float(last.get('resolvedAt',last.get('placedAt',0)))))


def place_panic_sell(st, remaining):
    if remaining<=0 or remaining<CONTRACT_MIN or remaining%CONTRACT_LOT:
        die('PANIC remaining size not valid for market sell: '+str(remaining))
    if sum(e.get('state') in ('rejected','EXPIRED_ABSENT')
           for e in st.get('panic_orders',[]))>=PANIC_MAX_POST_ATTEMPTS:
        raise ExchangeUnresolved('PANIC rejections/absences exhausted; manual audit required')
    cid=newid('P')
    deadline=new_request_deadline_ms()
    # Persist unique ID + expiry BEFORE any network I/O; timeout stays unknown.
    rec={'clOrdId':cid,'ordId':'','sz':str(remaining), 'filled':'0',
         'state':'SUBMITTED_UNKNOWN','placedAt':time.time(),
         'expTimeMs':deadline}
    st.setdefault('panic_orders',[]).append(rec);save(st)
    log('PANIC_INTENT',clOrdId=cid,qty=str(remaining),expTimeMs=deadline)
    try:
        result=A.req('POST','/api/v5/trade/order',body={
            'instId':INST,'tdMode':'cross','side':'sell','posSide':'net',
            'reduceOnly':True,'ordType':'market','sz':str(remaining),'clOrdId':cid},
            exp_time_ms=deadline)
    except OrderExpiredBeforeSend:
        # A local expiry is positive proof that NO network POST happened.
        rec['state']='EXPIRED_ABSENT';rec['resolvedAt']=time.time();save(st)
        log('PANIC_LOCAL_EXPIRY',clOrdId=cid,severity='WARNING')
        return
    if (len(result)!=1 or not isinstance(result[0],dict)
        or result[0].get('clOrdId') not in (None,'',cid)):
        raise ExchangeUnresolved('PANIC ACK identity or length ambiguous; intent reserved')
    if result[0].get('sCode')!='0':
        # API.req only returns explicit per-order rejection with matching cid
        # and no ordId. A 5xx/429/transport error raises unknown instead.
        if result[0].get('ordId') not in ('',None) or not result[0].get('sCode'):
            raise ExchangeUnresolved('PANIC rejection contradicts order identity')
        rec['state']='rejected';rec['rejectCode']=str(result[0]['sCode'])
        rec['resolvedAt']=time.time();save(st)
        log('PANIC_MARKET_REJECTED',severity='CRITICAL',clOrdId=cid,
            code=rec['rejectCode'],message=str(result[0].get('sMsg',''))[:160])
        return
    if not result[0].get('ordId'):
        raise ExchangeUnresolved('PANIC ACK missing ordId; recovery by clOrdId only')
    rec['ordId']=result[0]['ordId'];save(st)
    log('PANIC_ACCEPTED',clOrdId=cid,ordId=rec['ordId'])


def controlled_shutdown(st, pending, actual, resolved, mode, terminal_bid=None):
    """Persistent, idempotent, multi-pass liquidation state machine.

    HARD/PANIC: cease BUYs, cancel all our BUYs and TP orders, wait for
    confirmed final states, then retire; PANIC alone additionally market
    closes the reconciled bot net position. Never call normal TP rearming.
    """
    # Early owner-verified BUY cancels are handled before position reconciliation
    # in live_loop(). Never bypass the fresh full pass for TP cancellation or SELL.
    if not resolved:
        control_ack(mode,'WAIT_RECONCILIATION')
        return False
    active=active_buy(st)
    if active:
        # Cancel only individually confirmed live BUYs; no bulk cancellations
        # that could accidentally cancel other bots' orders.
        for c in active[:5]:
            if c['state'] not in ('BUY_LIVE','BUY_PARTIAL'):
                die('Cannot stop safely with unsettled BUY '+str(c.get('buyPx')))
            cancel_lowest_specific(st,c)
        control_ack(mode,'CANCEL_BUYS')
        return False
    sell_orders=real_tp_active(st)
    if sell_orders:
        for _,c,rec in sell_orders[:5]:cancel_tp(st,c,rec)
        control_ack(mode,'CANCEL_TPS')
        return False
    if pending:
        # At this point no orders should remain in the exchange pending list.
        die('Unexpected pending OKX order during shutdown; refuse to finalize')
    if mode=='HARD':
        st['terminal_snapshot']={'mode':'RETIRED','at_unix':time.time(),
            'at_utc':datetime.now(timezone.utc).isoformat(),
            'bid':str(terminal_bid) if terminal_bid is not None else None,
            'position_contracts':str(actual)}
        st['terminal_mode']='RETIRED';save(st)
        control_ack(mode,'RETIRED',remaining_position=str(actual),
                    warning='OPEN POSITION HAS NO TP PROTECTION')
        log('BOT_RETIRED',remaining_position=str(actual))
        return True
    # Panic is only reachable once BUY and TP cancels have FINAL outcomes.
    # No other pending orders. Prevent selling against anyone else's account
    # position by demanding exact local/exchange reconciliation every pass.
    if not reconcile_panic_orders(st,pending):
        control_ack(mode,'WAIT_PANIC_ORDER')
        return False
    # The caller read actual from the SAME round of reconcile_position.
    if actual == 0:
        st['terminal_snapshot']={'mode':'PANIC_DONE','at_unix':time.time(),
            'at_utc':datetime.now(timezone.utc).isoformat(),
            'bid':str(terminal_bid) if terminal_bid is not None else None,
            'position_contracts':'0'}
        st['terminal_mode']='PANIC_DONE';save(st)
        control_ack(mode,'PANIC_DONE',remaining_position='0')
        log('PANIC_DONE',closed_contracts=str(panic_executions(st)))
        return True
    if actual < 0:
        die('PANIC refused: net position is short')
    # Resend only after a definitive rejection/expiry or confirmed final fill,
    # bounded by a durable retry count and with a fresh reconciled position.
    delay=panic_retry_delay(st)
    if delay is None:
        control_ack(mode,'PANIC_MANUAL_REVIEW',remaining_position=str(actual))
        if not st.get('panic_retry_exhausted_warned'):
            st['panic_retry_exhausted_warned']=True;save(st)
            log('PANIC_RETRY_EXHAUSTED',severity='CRITICAL',remaining=str(actual))
        return False
    if delay>0:
        control_ack(mode,'WAIT_PANIC_RETRY',retry_after_sec=round(delay,1))
        return False
    place_panic_sell(st,actual)
    control_ack(mode,'WAIT_PANIC_ORDER')
    return False


def emergency_precancel_own_buys(st, mode):
    """Reduce exposure before the /positions gate or unrelated TP can stall.

    Exact currently pending BUY identity is proven independently of position
    reconciliation. Never cancel other orders, PLACING intents, or a BUY that
    has become terminal; CANCELING intents are never re-POSTed.
    """
    eligible=[c for c in active_buy(st)
              if c.get('state') in ('BUY_LIVE','BUY_PARTIAL') and c.get('ordId')]
    if not eligible:
        return False
    pending=A.pending()  # fresh complete pagination, no /positions quota
    by_id={}
    for o in pending:
        oid=str(o.get('ordId') or '')
        if not oid or oid in by_id:
            raise ExchangeUnresolved('Emergency pending snapshot has duplicate/missing order IDs')
        by_id[oid]=o
    matches=[]
    conflicts=[]
    for c in eligible:
        o=by_id.get(str(c['ordId']))
        if o is None:
            continue  # filled/canceled race; full reconciliation must resolve
        try:
            identity_ok=(o.get('instId')==INST and o.get('side')=='buy'
                and o.get('clOrdId')==c.get('clOrdId')
                and o.get('state') in ('live','partially_filled')
                and D(o.get('sz'))==D(c['sz'])
                and D(o.get('px'))==D(c['buyPx']))
        except (ValueError,TypeError,ArithmeticError):
            identity_ok=False
        if not identity_ok:
            conflicts.append(str(c['ordId']))
            continue  # NEVER cancel an unverified or manually edited order.
        matches.append(c)
    if control_mode()!=mode:
        raise ExchangeTemporary('Emergency command changed during BUY snapshot')
    cancelled=0
    for c in matches[:5]:
        if control_mode()!=mode:
            raise ExchangeTemporary('Emergency command changed during BUY cancellation batch')
        cancel_lowest_specific(st,c)  # durable CANCELING before any cancel POST
        cancelled+=1
    if cancelled:
        control_ack(mode,'CANCEL_BUYS')
        log('EMERGENCY_EARLY_BUY_CANCEL',mode=mode,count=cancelled,
            message='Independently confirmed exact-ID BUYs only; SELL gate unchanged')
    if conflicts:
        # Cancel independently PROVEN own BUYs, then keep emergency unresolved.
        # Do NOT allow PANIC MARKET to proceed on an unresolved NET position.
        log('EMERGENCY_BUY_IDENTITY_CONFLICT',severity='CRITICAL',
            mode=mode,count=len(conflicts),ordIds=conflicts[:8])
        raise ExchangeUnresolved('Emergency BUY identity conflict; verified BUYs canceled, '
                                 'unknown BUYs untouched; position requires reconciliation')
    return bool(cancelled)


def cancel_buys_for_soft_stop(st, resolved, pending=None):
    # An uncertain position mismatch must not obstruct the SOFT request.
    # Cancel only BUYs positively present in this *fresh pending* snapshot,
    # with exact ordId/clOrdId/side and an already tracked local identity.
    live_oids=None
    if pending is not None:
        live_oids={str(o.get('ordId')):o for o in pending
                   if o.get('side')=='buy' and o.get('state') in ('live','partially_filled')}
    confirmed=[]
    for c in active_buy(st):
        if c['state'] not in ('BUY_LIVE','BUY_PARTIAL') or not c.get('ordId'):
            continue
        if live_oids is not None:
            exchange=live_oids.get(str(c['ordId']))
            if exchange is None:continue
            if (exchange.get('clOrdId')!=c.get('clOrdId') or
                exchange.get('instId')!=INST):
                die('SOFT cancellation order identity mismatch')
        confirmed.append(c)
    if confirmed:
        for c in confirmed[:5]:
            cancel_lowest_specific(st,c)
        return 'CANCEL_BUYS'
    if not resolved or any(c['state'] in ('PLACING','CANCELING')
                           for c in st['cycles'].values()):
        return 'WAIT_RECONCILIATION'
    return 'SOFT_STOPPED'


def count(state):
    return sum(x['state'] not in ('CLOSED','CANCELED','EXITED') for x in state['cycles'].values())

def active_buy(state):
    return [x for x in state['cycles'].values() if x['state'] in ('BUY_LIVE','BUY_PARTIAL','PLACING')]


def validate_buy_buffer(active):
    """Up to five temporary BUYs beyond the normal 25 are allowed."""
    if len(active)>BUY_MAX:
        die('More active BUYs than buffer maximum: '+str(len(active))+'>'+str(BUY_MAX))



def money(value, places=6):
    x=Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return ('+' if x>=0 else '−') + f'{abs(x):.{places}f}'


def money_usdt(value, places=6):
    signed=money(value,places)
    return signed[0]+'$'+signed[1:]


def bold(value):
    return ('\033[1m'+value+'\033[0m') if sys.stdout.isatty() else value


from okx_strategy import build_grid_rules
(level_free, tp_quantity_placeable, required_safe_tp, grid_top,
 desired_buy_levels, missing_buy_levels_for_addition,
 plan_buy_buffer_cleanup, tp_buffer_candidates) = build_grid_rules(
    STEP=STEP, TARGET=TARGET, PRICE_STEP=PRICE_STEP,
    BUY_CANCEL_PROTECT_STEPS=BUY_CANCEL_PROTECT_STEPS,
    REAL_TP_TARGET=REAL_TP_TARGET, CONTRACT_MIN=CONTRACT_MIN,
    CONTRACT_LOT=CONTRACT_LOT, D=D, fmt=fmt, die=die,
    ExchangeUnresolved=ExchangeUnresolved)

from okx_accounting import build_pnl_ledger
PnLLedger = build_pnl_ledger(
    D=D, STEP=STEP, A=A, RateLimitedError=RateLimitedError,
    allocated_market_fills=allocated_market_fills,
    fmt_accounted_price=fmt_accounted_price, money=money,
    money_usdt=money_usdt, bold=bold, die=die, PNL_DB=PNL_DB)


def initial_state():
    pos=A.positions(); pending=A.pending()
    if any(D(p.get('pos'))!=0 for p in pos) or pending:
        die('Untracked instrument position/orders: refusing initialization. '             'Use dedicated instrument per bot; net mode position is shared.')
    st=empty_state(VER, INST)
    st.update(bootstrapped=True, started_at=time.time())
    save(st); return st

# Every historical SELL remains associated with its original BUY via cycleId.
# The exchange order ID changes on TP restoration, never the cycle identity.

def journal_tp(event, cycle, **extra):
    item={'time':datetime.now(timezone.utc).isoformat(),
          'event':event,'cycleId':cycle.get('cycleId'),
          'buyOrdId':cycle.get('ordId'),'buyPx':cycle.get('buyPx'), **extra}
    fd=os.open(TP_JOURNAL,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a') as f:
        f.write(json.dumps(item,ensure_ascii=False,sort_keys=True)+'\n')
        f.flush();os.fsync(f.fileno())

def tp_records(c):
    return c.setdefault('tpOrders',[])

def cycle_sell_fill(c):
    return sum((D(y.get('filled')) for y in tp_records(c)),Decimal(0))

def remaining_tp(c):
    return D(c.get('filledBuy'))-cycle_sell_fill(c)

def active_tp_records(c):
    return [r for r in tp_records(c) if r['phase'] in
            ('PENDING','PLACING','ACTIVE','CANCELING')]

def virtual_cycles(st):
    return [(k,c) for k,c in st['cycles'].items() if
            c.get('state')=='VIRTUAL_TP' and remaining_tp(c)>0]


def unprotected_partial_buy(st):
    """Exchange-confirmed BUY fills with parent order not yet fully filled.

    The attached TP does not exist until the parent BUY is FILLED. Reserve
    these fills separately from exchange/virtual SELL coverage; never pretend
    that they have an attached or virtual TP.
    """
    return sum((D(c.get('filledBuy'))-D(c.get('filledTP'))
                for c in st['cycles'].values()
                if c.get('state') in ('BUY_LIVE','BUY_PARTIAL','CANCELING') and not tp_records(c)),
               Decimal('0'))




def unplaceable_virtual_tp(st):
    return [(k,c) for k,c in virtual_cycles(st)
            if not tp_quantity_placeable(remaining_tp(c))]

def paged_order(cid, pages=12):
    """Exact client ID match; NEVER use price/quantity coincidence."""
    seen=set();after=None
    for _ in range(pages):
        params={'instType':'SWAP','instId':INST,'limit':'100'}
        if after:params['after']=after
        batch=A.req('GET','/api/v5/trade/orders-history',params)
        matches=[o for o in batch if o.get('side')=='sell' and o.get('clOrdId')==cid]
        if len(matches)>1:die('Ambiguous TP client ID: '+cid)
        if matches:return matches[0]
        if len(batch)<100:return None
        after=batch[-1].get('ordId')
        if not after or after in seen:die('History paging stalled')
        seen.add(after)
    return None  # Not enough information: do NOT assume cancellation.

def register_cancel_attached(st,pending):
    """OKX since Jul 2026: canceling a partial BUY creates attached TP.

    Bind by the original, unique attachAlgoClOrdId BEFORE the generic
    unknown-order guard. Never associate by price or approximate size.
    """
    for c in st['cycles'].values():
        if c.get('state') not in ('CANCELING','BUY_PARTIAL','BUY_LIVE','VIRTUAL_TP'):
            continue
        cid=c.get('tpClOrdId')
        if not cid or tp_records(c):continue
        for o in pending:
            if o.get('clOrdId')!=cid:continue
            if (o.get('instId')!=INST or o.get('side')!='sell'
                or D(o.get('px'))!=D(c['tpPx'])
                or o.get('reduceOnly') not in (None,'','true',True)
                or D(o.get('sz'))<=0 or D(o.get('sz'))>D(c['sz'])):
                die('Attached partial TP identity/size/side/price mismatch')
            if c.get('state')=='VIRTUAL_TP':
                # Migration of a canceled-partial BUY: old engine
                # wrongly assumed the attached TP would never materialize.
                # Exact clOrdId proves this live SELL belongs to that cycle.
                c['state']='TP_WAIT'
                log('MIGRATED_PARTIAL_ATTACHED_TP',price=c['buyPx'],
                    ordId=o.get('ordId',''))
            tp_records(c).append({'clOrdId':cid,'ordId':o.get('ordId',''),
                'sz':str(D(o['sz'])),'filled':'0','phase':'PENDING',
                'source':'attached','placedAt':time.time()})
            save(st)
            # Attached TP can arrive BEFORE track() records a fully filled BUY.
            # Do not call it a partial execution unless the partial fill is
            # actually confirmed by the cycle's accumulated filledBuy.
            observed_buy=D(c.get('filledBuy', '0'))
            is_confirmed_partial=(D('0') < observed_buy < D(c['sz']))
            log('PARTIAL_ATTACHED_TP_DISCOVERED' if is_confirmed_partial
                else 'ATTACHED_TP_DISCOVERED',price=c['buyPx'],
                ordId=o.get('ordId'),sz=o['sz'],
                execution_class=('confirmed_partial' if is_confirmed_partial
                                 else 'unconfirmed_at_discovery'))
            break


def resolve_partial_cancel(st,c,filled):
    """Don't guess that the attached TP vanished on a partial cancellation.

    A 2026 OKX change generates the TP for the executed part. If it is
    delayed, reserve the full position and keep waiting (no duplicate SELL).
    """
    if filled<=0:
        c['state']='CANCELED';save(st)
        return
    c['filledBuy']=str(filled)
    if not tp_records(c):
        tp_records(c).append({'clOrdId':c['tpClOrdId'],'ordId':'',
            'sz':str(filled),'filled':'0','phase':'PENDING',
            'source':'attached','placedAt':time.time()})
    else:
        for rec in tp_records(c):
            if rec['source']=='attached' and D(rec['sz'])!=filled:
                die('Attached partial TP size mismatches canceled BUY')
    c['state']='TP_WAIT';save(st)
    log('BUY_PARTIAL_CANCELED_TP_PENDING',price=c['buyPx'],
        qty=str(filled),cleanup=bool(c.get('partial_cleanup')))


def verify_existing(st,pending):
    known={c.get('ordId') for c in st['cycles'].values()}
    known_cls={c.get('clOrdId') for c in st['cycles'].values()}
    for c in st['cycles'].values():
        for rec in tp_records(c):
            known.add(rec.get('ordId'))
            known_cls.add(rec.get('clOrdId'))
    for g in st.get('aggregate_exits',[]):
        known.add(g.get('ordId'))
        known_cls.add(g.get('clOrdId'))
    for e in st.get('panic_orders',[]):
        known.add(e.get('ordId'))
        known_cls.add(e.get('clOrdId'))
    for o in pending:
        if o.get('ordId') not in known and o.get('clOrdId') not in known_cls:
            die('Unrecognized pending OKX order: '+str(o.get('ordId')))

def parent_tp_failure(c):
    parent=A.order(oid=c['ordId'])
    if not parent:die('Parent BUY not queryable: '+str(c['ordId']))
    if parent.get('state')!='filled':return ''
    for a in parent.get('attachAlgoOrds',[]):
        if a.get('attachAlgoClOrdId')==c.get('tpClOrdId'):
            return str(a.get('failCode') or '')
    return ''

def tp_record_update(st, c, rec, order):
    if order.get('instId')!=INST or order.get('side')!='sell':
        die('Wrong TP instrument/side '+c['cycleId'])
    if order.get('clOrdId')!=rec['clOrdId']:
        die('TP client identity mismatch '+c['cycleId'])
    if rec.get('ordId') and order.get('ordId')!=rec['ordId']:
        die('TP order ID mismatch '+c['cycleId'])
    if not order.get('ordId'):die('TP lacks ordId '+c['cycleId'])
    if D(order.get('sz'))!=D(rec['sz']):die('TP size changed externally '+c['cycleId'])
    if rec.get('source')=='partial_exit':
        if order.get('ordType')!='market':
            die('Partial exit order not MARKET '+c['cycleId'])
        if order.get('reduceOnly') not in ('true',True):
            die('Partial exit order not reduceOnly '+c['cycleId'])
    elif D(order.get('px'))!=D(c['tpPx']):
        die('TP price changed externally '+c['cycleId'])
    if order.get('reduceOnly') not in (None,'','true',True):
        die('TP not reduce-only: '+c['cycleId'])
    filled=D(order.get('accFillSz'))
    if filled<D(rec.get('filled')) or filled>D(rec['sz']):
        die('Nonmonotonic TP fill '+c['cycleId'])
    changed=False
    if rec.get('ordId')!=order['ordId']:
        rec['ordId']=order['ordId'];changed=True
        log('TP_LINKED',price=c['buyPx'],tpOrdId=order['ordId'],clOrdId=rec['clOrdId'])
    if filled!=D(rec.get('filled')):
        rec['filled']=str(filled);changed=True
    state=order.get('state')
    trip_if_margin_cancel(order, 'TP_CANCELED_BY_OKX')
    phases={'live':'ACTIVE','partially_filled':'ACTIVE',
            'filled':'FILLED','canceled':'CANCELED','mmp_canceled':'CANCELED'}
    if state not in phases:die('Unknown TP state '+str(state))
    new=phases[state]
    # A cancel ACK is asynchronous: until terminal canceled/filled, keep
    # CANCELING instead of treating an old 'live' snapshot as permission.
    if rec.get('phase')=='CANCELING' and new=='ACTIVE':
        if (rec.get('cancelRejectAt') and
            time.time()-float(rec['cancelRejectAt'])>=10):
            # Rejected cancellation followed by a fresh confirmed live SELL.
            # It can be safely considered ACTIVE again; a new cancel will
            # require a new explicit operator/strategy decision.
            rec.pop('cancelRejectAt',None)
            rec['phase']='ACTIVE'
            save(st)
            return 'ACTIVE'
        # ACK can precede final exchange status by seconds during volatility.
        # Do not confuse an in-flight SELL cancel with an absent TP or retry
        # the same POST; keep cycle and remaining quantity reserved.
        elapsed=time.time()-float(rec.get('cancelAt') or time.time())
        if elapsed>=30 and time.time()-float(rec.get('cancelWarnAt',0))>=30:
            rec['cancelWarnAt']=time.time()
            changed=True
            log('TP_CANCEL_PENDING',price=c['buyPx'],ordId=rec['ordId'],seconds=int(elapsed))
        new='CANCELING'
    if rec.get('phase')!=new:
        rec['phase']=new;changed=True
        if new=='CANCELED':journal_tp('TP_CANCELED_CONFIRMED',c,ordId=rec['ordId'],filled=rec['filled'])
        if new=='FILLED':journal_tp('TP_FILLED_CONFIRMED',c,ordId=rec['ordId'],filled=rec['filled'])
    if changed:save(st)
    return new


def unresolved_aggregate(st):
    return any(g.get('phase') not in ('FILLED','CANCELED','REJECTED')
               for g in st.get('aggregate_exits',[]))


def reconcile_aggregate(st):
    """Shared MARKET order authoritative cumulative fill, never assumed full."""
    resolved=True
    for g in st.get('aggregate_exits',[]):
        if g.get('phase') in ('FILLED','CANCELED','REJECTED'):
            continue
        o=A.order(oid=g['ordId']) if g.get('ordId') else A.order(cid=g['clOrdId'])
        if o is None:
            resolved=False  # uncertain network outcome: never blind retry
            continue
        if (o.get('instId')!=INST or o.get('side')!='sell' or
            o.get('clOrdId')!=g['clOrdId'] or o.get('ordType')!='market' or
            o.get('reduceOnly') not in (True,'true')):
            die('Aggregate MARKET identity/side/type mismatch')
        if D(o.get('sz'))!=D(g['sz']):die('Aggregate MARKET order size mismatch')
        if g.get('ordId') and str(o['ordId'])!=str(g['ordId']):
            die('Aggregate MARKET ordId mismatch')
        state=o.get('state')
        if state not in ('live','partially_filled','filled','canceled','mmp_canceled'):
            die('Unknown aggregate MARKET state')
        filled=D(o.get('accFillSz'))
        if filled<D(g.get('filled','0')) or filled>D(g['sz']):
            die('Nonmonotonic aggregate MARKET quantity')
        g['ordId']=str(o['ordId'])
        g['filled']=str(filled)
        g['phase']=('FILLED' if state=='filled' else 'CANCELED'
                    if state in ('canceled','mmp_canceled') else 'ACTIVE')
        remaining=filled
        for part in g['plan']:
            c=next((x for x in st['cycles'].values() if x['cycleId']==part['cycleId']),None)
            if c is None:die('Aggregate parent cycle missing')
            legs=[r for r in tp_records(c) if r.get('source')=='aggregate_exit'
                  and r.get('clOrdId')==g['clOrdId']]
            if len(legs)!=1:die('Aggregate sell leg missing/ambiguous')
            rec=legs[0]
            alloc=min(D(part['sz']),remaining)
            if alloc<D(rec.get('filled','0')):die('Aggregate allocation nonmonotonic')
            remaining-=alloc
            rec['ordId']=g['ordId']
            rec['filled']=str(alloc)
            rec['aggregateFilledTotal']=str(filled)
            rec['phase']=('FILLED' if alloc==D(part['sz']) and g['phase']=='FILLED'
                           else 'CANCELED' if g['phase'] in ('FILLED','CANCELED')
                           else 'ACTIVE')
            # Existing normal tracker recomputes cycle state and filledTP.
        if remaining:die('Aggregate fill allocation overflow')
        save(st)
        if g['phase']=='ACTIVE':resolved=False
        else:log('AGG_MARKET_TERMINAL',clOrdId=g['clOrdId'],ordId=g['ordId'],
                 filled=str(filled),requested=g['sz'],state=g['phase'],
                 avgPx=str(o.get('avgPx') or ''))
    return resolved


def persist_crossed_virtual_tps(st,market):
    """Persist crossing signals before any early return / TP cancellation.

    A ticker peak may be gone by the time REST reconciliation completes.
    Detecting the peak is not trading permission: the normal coverage and
    exchange-side SELL reconciliation still gate a MARKET order.
    """
    seen=market.take_crossed_virtual_ids()
    changed=False
    for c in st['cycles'].values():
        if c.get('state') == 'VIRTUAL_TP':
            if (c['cycleId'] in seen and not c.get('virtualTpCrossed')
                    and not c.get('partial_cleanup')):
                c['virtualTpCrossed']=True
                c['virtualTpCrossedAt']=time.time()
                changed=True
        else:
            # New periods of virtual exposure must not inherit old signals
            # from a cycle which has since been covered by a real SELL.
            if c.pop('virtualTpCrossed',None) is not None:
                c.pop('virtualTpCrossedAt',None)
                changed=True
    if changed:save(st)
    market.set_virtual_targets({c['cycleId']:D(c['tpPx']) for _,c in virtual_cycles(st)
                                if not c.get('partial_cleanup')})


def virtual_escape(st,bid,actual,pending,mode):
    """Called ONLY after a full resolved REST position/order reconciliation."""
    if mode!='RUN':return 'NONE'
    if unresolved_aggregate(st):return 'WAIT'
    # Never race with known exchange real SELL / attached TP.
    if any(o.get('side')=='sell' for o in pending):
        if st.pop('escape_collection',None) is not None:save(st)
        return 'NONE'
    candidates=[c for _,c in virtual_cycles(st)
                if not c.get('partial_cleanup')
                and (D(c['tpPx'])<=bid or c.get('virtualTpCrossed'))
                and tp_quantity_placeable(remaining_tp(c))]
    now=time.time()
    collect=st.get('escape_collection')
    if not collect:
        if not candidates:return 'NONE'
        st['escape_collection']={'startedAt':now,'cycles':[c['cycleId'] for c in candidates]}
        save(st)
        log('AGG_ESCAPE_COLLECT',cycles=len(candidates),bid=str(bid))
        return 'WAIT'
    ids=set(collect['cycles']) | {c['cycleId'] for c in candidates}
    if now-float(collect['startedAt'])<ESCAPE_COALESCE_SEC:
        if ids!=set(collect['cycles']):
            collect['cycles']=sorted(ids);save(st)
        return 'WAIT'
    wanted=sorted((c for _,c in virtual_cycles(st) if c['cycleId'] in ids
                   and not c.get('partial_cleanup') and tp_quantity_placeable(remaining_tp(c))),
                  key=lambda c:(D(c['tpPx']),c['cycleId']))
    st.pop('escape_collection',None)
    if not wanted:save(st);return 'NONE'
    # Conservative full balance check: zero pending real SELL, no unknown
    # aggregate, only confirmed virtual exposure may be submitted.
    qty=sum((remaining_tp(c) for c in wanted),Decimal(0))
    if qty<=0 or qty>actual:die('Aggregate exit exceeds reconciled position')
    cid=newid('E');exp=new_request_deadline_ms()
    plan=[{'cycleId':c['cycleId'],'sz':str(remaining_tp(c))} for c in wanted]
    group={'clOrdId':cid,'ordId':'','sz':str(qty),'filled':'0',
           'phase':'PLACING','plan':plan,'placedAt':now,'expTimeMs':exp}
    st.setdefault('aggregate_exits',[]).append(group)
    for c,part in zip(wanted,plan):
        tp_records(c).append({'clOrdId':cid,'ordId':'','sz':part['sz'],
            'filled':'0','phase':'PLACING','source':'aggregate_exit',
            'aggregatePlan':plan,'aggregateFilledTotal':'0','placedAt':now})
        c['state']='TP_WAIT'
    save(st)  # fsync all parent links + intent BEFORE the network POST
    log('AGG_MARKET_INTENT',clOrdId=cid,cycles=len(plan),qty=str(qty))
    body={'instId':INST,'tdMode':'cross','side':'sell','posSide':'net',
          'reduceOnly':True,'ordType':'market','sz':str(qty),'clOrdId':cid}
    try:
        rows=A.req('POST','/api/v5/trade/order',body=body,exp_time_ms=exp)
    except OrderExpiredBeforeSend:
        rows=[{'sCode':'LOCAL_EXPIRED','clOrdId':cid,'ordId':''}]
    if len(rows)!=1:raise ExchangeUnresolved('Ambiguous aggregate MARKET response')
    result=rows[0]
    if result.get('sCode')!='0':
        if result.get('ordId') or result.get('clOrdId',cid)!=cid:
            raise ExchangeUnresolved('Contradictory aggregate MARKET rejection')
        group['phase']='REJECTED'
        group['rejectCode']=str(result.get('sCode'))
        for c in wanted:
            rec=tp_records(c)[-1]
            rec['phase']='FAILED';c['state']='VIRTUAL_TP'
            c['tpRestoreRetryAt']=now+5
        save(st)
        log('AGG_MARKET_REJECTED',code=group['rejectCode'],cycles=len(plan))
        if margin_rejection(group['rejectCode']):
            trip_account_soft_stop(group['rejectCode'],result.get('sMsg',''),'AGG_MARKET')
        return 'WAIT'
    if not result.get('ordId'):
        raise ExchangeUnresolved('Aggregate MARKET ACK without ordId; intent remains')
    group['ordId']=str(result['ordId'])
    for c in wanted:tp_records(c)[-1]['ordId']=group['ordId']
    save(st)
    log('AGG_MARKET_ACK',ordId=group['ordId'],clOrdId=cid,qty=str(qty))
    return 'ACTION'


def track(st,pending):
    """All TP replacements + fills are tracked under the original CycleID."""
    all_resolved=reconcile_aggregate(st)
    byid={o.get('ordId'):o for o in pending}
    bycid={o.get('clOrdId'):o for o in pending}
    for key,c in list(st['cycles'].items()):
        status=c['state']
        if status in ('CLOSED','CANCELED','EXITED'):continue
        if status=='PLACING':
            o=bycid.get(c['clOrdId']) or A.order(cid=c['clOrdId'])
            if not o and post_absence_confirmed(c,'buy',st):
                c['state']='CANCELED';c['rejectCode']='EXPIRED_ABSENT'
                save(st)
                log('BUY_POST_EXPIRED_ABSENT',price=key,clOrdId=c['clOrdId'])
                continue
            if not o:
                # No negative proof of non-placement: retain the durable
                # intent indefinitely, block only new exposure, and continue
                # reconciling other cycles/TPs. NEVER blindly repeat this BUY.
                all_resolved=False
                if time.time()-float(c.get('placingWarnAt',0))>=60:
                    c['placingWarnAt']=time.time();save(st)
                    log('BUY_POST_UNRESOLVED',price=key,clOrdId=c['clOrdId'],
                        message='Manual exchange audit may be required; no duplicate BUY')
                continue
            if (o.get('instId')!=INST or o.get('clOrdId')!=c['clOrdId']
                or o.get('side')!='buy' or not o.get('ordId')):
                die('BUY PLACING identity mismatch '+key)
            c['ordId']=o['ordId'];c['state']='BUY_LIVE';save(st)
            status='BUY_LIVE'
        if status=='CANCELING':
            # POST cancel is only an ACK: exchange may still show live, or
            # this order may FILL while its cancel is travelling to OKX.
            # No new BUY submissions are allowed until the exact order ID
            # returns a final exchange state. This survives restarts.
            o=byid.get(c.get('ordId')) or A.order(oid=c['ordId'])
            if o is None:
                all_resolved=False
                continue
            if o.get('instId')!=INST or o.get('side')!='buy' or o.get('ordId')!=c.get('ordId'):
                die('BUY cancel identity mismatch '+key)
            state=o.get('state')
            trip_if_margin_cancel(o, 'BUY_CANCELED_BY_OKX')
            filled=D(o.get('accFillSz'))
            if filled < D(c.get('filledBuy')) or filled > D(c['sz']):
                die('Nonmonotonic BUY cancel fill '+key)
            if state in ('canceled','mmp_canceled'):
                resolve_partial_cancel(st,c,filled)
                if not filled:
                    log('BUY_CANCEL_CONFIRMED',price=key,ordId=c['ordId'])
                    continue
                status='TP_WAIT'
            elif state=='filled':
                # Cancel lost the race. Keep SAME cycle ID, linked attached TP.
                if c.pop('partial_cleanup',None):
                    log('PARTIAL_EXIT_ABORTED_FULL_BUY',price=key)
                c['state']='BUY_LIVE';save(st)
                log('BUY_FILLED_DURING_CANCEL',price=key,ordId=c['ordId'])
                status='BUY_LIVE'
            elif state in ('live','partially_filled'):
                if filled != D(c.get('filledBuy')):
                    if filled and not c.get('partial_since'):
                        c['partial_since']=time.time()
                    c['filledBuy']=str(filled);save(st)
                # A definitive cancel rejection means no cancellation was
                # accepted. Wait 10s, confirm the order STILL live, then
                # re-arm cancellation. Never repeat an ambiguous cancel.
                if c.get('cancelRejectAt') and time.time()-float(c['cancelRejectAt'])>=10:
                    c['state']='BUY_PARTIAL' if filled else 'BUY_LIVE'
                    c.pop('cancelRejectAt',None)
                    save(st)
                    all_resolved=False
                    continue
                all_resolved=False
                started=D(c.get('cancelAt'))
                elapsed=time.time()-float(started) if started else 0
                # Never blindly resubmit a cancellation or place replacement
                # buys. Log a warning periodically, but keep reconciling.
                if elapsed>=30 and time.time()-float(c.get('cancelWarnAt',0))>=30:
                    c['cancelWarnAt']=time.time();save(st)
                    log('BUY_CANCEL_PENDING',price=key,ordId=c['ordId'],
                        state=state,seconds=int(elapsed))
                continue
            else:
                die('Unknown BUY cancel state '+str(state)+' at '+key)
        if status in ('BUY_LIVE','BUY_PARTIAL'):
            o=byid.get(c.get('ordId')) or A.order(oid=c['ordId'])
            if not o:
                # Do not strand unrelated TP on a temporarily unavailable BUY.
                all_resolved=False
                continue
            trip_if_margin_cancel(o, 'BUY_CANCELED_BY_OKX')
            state=o.get('state');filled=D(o.get('accFillSz'))
            if state=='filled':
                c['state']='TP_WAIT';c['filledBuy']=str(filled)
                if not tp_records(c):
                    tp_records(c).append({'clOrdId':c['tpClOrdId'],
                        'ordId':c.get('tpOrdId',''),'sz':str(filled),'filled':'0',
                        'phase':'PENDING','source':'attached','placedAt':time.time()})
                save(st)
                log('BUY_FILLED',price=key,ordId=c['ordId'],tp=c['tpPx'])
            elif state in ('partially_filled','live'):
                # Update the filled amount even if OKX reports 'live' with a
                # nonzero accFillSz. Avoid an unnecessary disk fsync per pass.
                target='BUY_PARTIAL' if filled else 'BUY_LIVE'
                if filled<D(c.get('filledBuy')) or filled>D(c['sz']):
                    die('Nonmonotonic BUY partial fill '+key)
                if c['state']!=target or D(c.get('filledBuy'))!=filled:
                    if filled and not D(c.get('filledBuy')):
                        c.setdefault('partial_since',time.time())
                    elif filled and not c.get('partial_since'):
                        # Migration of an already partially-filled BUY.
                        c['partial_since']=time.time()
                    c['state']=target;c['filledBuy']=str(filled);save(st)
                    if filled:
                        log('BUY_PARTIAL',price=key,filled=str(filled),
                            total=c['sz'],remaining=str(D(c['sz'])-filled))
            elif state in ('canceled','mmp_canceled'):
                resolve_partial_cancel(st,c,filled)
                if not filled:continue
            else:die('Unknown BUY status '+str(state))
        if c['state'] not in ('TP_WAIT','TP_PARTIAL','VIRTUAL_TP'):
            continue
        for rec in tp_records(c):
            if rec.get('source')=='aggregate_exit':continue  # handled once per shared ordId
            if rec['phase'] in ('FILLED','CANCELED','FAILED'):continue
            o=(byid.get(rec.get('ordId')) if rec.get('ordId') else None)
            if o is None:o=bycid.get(rec['clOrdId'])
            if o is None:
                o=A.order(oid=rec['ordId']) if rec.get('ordId') else A.order(cid=rec['clOrdId'])
            if o is None and rec['phase'] in ('PENDING','PLACING'):
                # Old 51165 TP failures are authoritative proof that no TP was placed.
                if rec.get('source')=='attached' and parent_tp_failure(c)=='51165':
                    rec['phase']='FAILED';rec['failCode']='51165';save(st)
                    journal_tp('ATTACHED_TP_FAIL_51165',c,clOrdId=rec['clOrdId'])
                    continue
            if o is None and rec['phase'] in ('PENDING','PLACING','CANCELING'):
                o=paged_order(rec['clOrdId'])
            if (o is None and rec.get('source')=='recovery' and rec['phase']=='PLACING'
                and post_absence_confirmed(rec,'sell',st)):
                rec['phase']='FAILED';rec['failCode']='EXPIRED_ABSENT'
                c['tpRestoreRetryAt']=time.time()+10
                save(st)
                log('TP_POST_EXPIRED_ABSENT',cycleId=c['cycleId'],clOrdId=rec['clOrdId'])
                continue
            if o is None:
                if rec['phase'] in ('ACTIVE','CANCELING'):
                    all_resolved=False
                    if time.time()-float(rec.get('lastWaitNotice',0))>=60:
                        rec['lastWaitNotice']=time.time();save(st)
                        log('KNOWN_TP_UNRESOLVED',cycleId=c['cycleId'],
                            clOrdId=rec['clOrdId'],message='Missing terminal status; SELL still reserved')
                    continue
                if rec.get('source')=='attached' and parent_tp_failure(c)=='51165':
                    rec['phase']='FAILED';rec['failCode']='51165';save(st)
                    continue
                if rec['phase']=='PLACING' and time.time()-rec.get('placedAt',0)>60:
                    if time.time()-float(rec.get('lastWaitNotice',0))>=60:
                        rec['lastWaitNotice']=time.time();save(st)
                        log('TP_POST_UNRESOLVED',cycleId=c['cycleId'],
                            clOrdId=rec['clOrdId'],
                            message='Do not repeat TP; its placement is still unknown')
                    # Never mark FAILED on mere absence; keep the SELL reserved.
                if (rec.get('source')=='attached' and c.get('partial_cleanup')
                    and time.time()-rec.get('placedAt',time.time())>30
                    and time.time()-rec.get('lastWaitNotice',0)>60):
                    rec['lastWaitNotice']=time.time();save(st)
                    log('PARTIAL_ATTACHED_TP_DELAYED',price=key,
                        message='No MARKET until attached TP is positively resolved')
                all_resolved=False
                continue
            tp_record_update(st,c,rec,o)
        filled=cycle_sell_fill(c)
        bought=D(c.get('filledBuy'))
        if filled>bought:die('More TP fills than BUY '+c['cycleId'])
        if c.get('filledTP')!=str(filled):
            c['filledTP']=str(filled);save(st)
        active=active_tp_records(c)
        if filled==bought and bought>0:
            # Every TP leg must have terminated: no dangling reduce-only SELL.
            if active: die('Filled cycle still has live SELL '+c['cycleId'])
            if c['state'] not in ('CLOSED','EXITED'):
                if c.get('partial_cleanup'):
                    c['state']='EXITED';st['partial_exits']=st.get('partial_exits',0)+1
                    save(st)
                    log('PARTIAL_EXIT_DONE',price=key,
                        qty=str(bought),lastSell=tp_records(c)[-1].get('ordId',''))
                else:
                    c['state']='CLOSED';st['completed']+=1;save(st)
                    log('TP_FILLED',price=key,tpId=tp_records(c)[-1].get('ordId',''))
        elif active:
            if any(r['phase'] in ('PENDING','PLACING','CANCELING') for r in active):
                all_resolved=False
            target='TP_PARTIAL' if filled else 'TP_WAIT'
            if c['state']!=target:c['state']=target;save(st)
        else:
            if c['state']!='VIRTUAL_TP':
                c['state']='VIRTUAL_TP';save(st)
                log('TP_VIRTUAL',price=key,qty=str(bought-filled),cycleId=c['cycleId'])
    return all_resolved

def real_tp_active(st):
    return [(k,c,r) for k,c in st['cycles'].items()
            for r in tp_records(c) if r['phase']=='ACTIVE']

class CoverageMismatch(ExchangeTemporary):
    """Coverage mismatch: block BUY; continue reconciling outstanding SELL."""

def coverage_audit(st,pending,position):
    active_sells=[o for o in pending if o.get('side')=='sell']
    total=sum((D(o.get('sz'))-D(o.get('accFillSz')) for o in active_sells),Decimal(0))
    virtual=sum((remaining_tp(c) for _,c in virtual_cycles(st)),Decimal(0))
    partial=unprotected_partial_buy(st)
    # Partial BUYs are account exposure, but cannot yet have attached TP.
    # Without this reservation a 0.01/0.03 fill raised false mismatch and
    # crashed the bot despite the BUY still resting correctly on OKX.
    if min(total,virtual,partial)<0:
        die('Negative TP/partial BUY coverage')
    if len(active_sells)>REDUCE_ONLY_HARD_CAP:
        die('Exchange TP count exceeds 150')
    if total>position:raise CoverageMismatch(f'Active TP quantity > position: {total}>{position}')
    if total+virtual+partial!=position:
        raise CoverageMismatch(f'Coverage mismatch: active {total}, virtual {virtual}, '
            f'partial BUY {partial}, position {position}')
    if len(active_sells)!=len(real_tp_active(st)):
        raise CoverageMismatch('Unmapped active TP count; no writes allowed')
    return len(active_sells),total,virtual


def cancel_tp(st,c,rec):
    """Durable TP cancellation intent. Only reconciliation may mark TP virtual."""
    if rec['phase']!='ACTIVE':die('Cancel requires known live TP')
    oid=rec['ordId']
    rec['phase']='CANCELING';rec['cancelAt']=time.time();save(st)
    journal_tp('CANCEL_INTENT',c,ordId=oid,clOrdId=rec['clOrdId'])
    res=A.req('POST','/api/v5/trade/cancel-order',body={'instId':INST,'ordId':oid})
    if len(res)!=1:
        raise ExchangeUnresolved('TP cancel response ambiguous; intent preserved')
    if res[0].get('sCode')!='0':
        rec['cancelRejectCode']=res[0].get('sCode')
        rec['cancelRejectAt']=time.time()
        save(st)
        log('TP_CANCEL_REJECTED',cycleId=c['cycleId'],ordId=oid,
            code=rec['cancelRejectCode'],message=res[0].get('sMsg',''))
        return 'CANCELING'
    journal_tp('CANCEL_ACCEPTED_PENDING_FINAL',c,ordId=oid)
    # The next reconciliation can observe live, canceled, partially filled,
    # or filled. No second POST, no premature release of this cycle.
    return 'CANCELING'


def place_virtual_tp(st,c):
    if c['state']!='VIRTUAL_TP' or active_tp_records(c):
        die('TP restore requires virtual-only cycle')
    remaining=remaining_tp(c)
    if remaining<=0:die('Cannot restore zero remaining TP')
    # Fractional size after a partial fill may be below OKX's lot step:
    # never round up and accidentally sell more than the open cycle.
    if not tp_quantity_placeable(remaining):
        # Keep durable virtual exposure, but don't crash every pass or
        # round a SELL upward. Fresh BUY submissions remain paused.
        return False
    # Never exceed position coverage or exchange cap, even during surge.
    if len(real_tp_active(st))>=REAL_TP_TARGET:
        return False
    cid=newid('T')
    stamp=time.time();expiry=new_request_deadline_ms()
    rec={'clOrdId':cid,'ordId':'','sz':str(remaining),'filled':'0',
         'phase':'PLACING','source':'recovery','placedAt':stamp,'expTimeMs':expiry}
    tp_records(c).append(rec);c['state']='TP_WAIT';save(st)
    journal_tp('RESTORE_INTENT',c,clOrdId=cid,qty=str(remaining),px=c['tpPx'])
    body={'instId':INST,'tdMode':'cross','side':'sell','posSide':'net',
          'reduceOnly':True,'ordType':'limit','px':c['tpPx'],
          'sz':str(remaining),'clOrdId':cid}
    try:
        res=A.req('POST','/api/v5/trade/order',body=body,exp_time_ms=expiry)
    except OrderExpiredBeforeSend:
        rec['phase']='FAILED';rec['failCode']='LOCAL_EXPIRED'
        c['state']='VIRTUAL_TP';c['tpRestoreRetryAt']=time.time()+10
        save(st)
        log('TP_POST_LOCAL_EXPIRY',cycleId=c['cycleId'],clOrdId=cid)
        return False
    if len(res)!=1:die('Unknown TP restoration response '+c['cycleId'])
    if res[0].get('sCode')!='0':
        # A definitive OKX rejection means the submitted SELL does NOT exist.
        # Preserve actual exposure as virtual TP; retry with backoff only.
        rec['phase']='FAILED';rec['failCode']=res[0].get('sCode')
        c['state']='VIRTUAL_TP'
        failures=int(c.get('tpRestoreFailures',0))+1
        c['tpRestoreFailures']=failures
        c['tpRestoreRetryAt']=time.time()+min(120,10*failures)
        save(st)
        journal_tp('RESTORE_REJECTED',c,code=rec['failCode'],message=res[0].get('sMsg',''))
        log('TP_RESTORE_REJECTED',cycleId=c['cycleId'],code=rec['failCode'],
            retry_seconds=min(120,10*failures))
        if margin_rejection(rec['failCode']):
            trip_account_soft_stop(rec['failCode'],res[0].get('sMsg',''),'TP_RESTORE')
        return False
    if not res[0].get('ordId'):die('TP restore ACK missing ordId')
    rec['ordId']=res[0]['ordId']
    c['tpRestoreFailures']=0;c.pop('tpRestoreRetryAt',None)
    save(st)
    journal_tp('RESTORE_ACCEPTED_PENDING_CONFIRMATION',c,ordId=rec['ordId'])
    # Query on the next reconcile, never assume acceptance equals live/fill.
    return True


def partial_cleanup_due(c,bid,now=None):
    """Only upward moves; don't market dump partially-filled BUYs in a fall."""
    now=time.time() if now is None else float(now)
    if c.get('state')!='BUY_PARTIAL' or not D(c.get('filledBuy')):
        return False
    first=float(c.get('partial_since') or now)
    return now-first>=PARTIAL_AGE and bid-D(c['buyPx'])>=PARTIAL_STEPS*STEP


def market_exit_partial(st,c,qty):
    """One durable, reduce-only MARKET SELL; recovery by clOrdId, no blind POST retry."""
    if c.get('state')!='VIRTUAL_TP' or not c.get('partial_cleanup'):
        die('Partial exit requires a canceled and reconciled attached TP')
    if time.time()<float(c.get('partialExitRetryAt',0)):
        return False
    if active_tp_records(c):
        die('Cannot market exit while SELL TP is active or unresolved')
    if not tp_quantity_placeable(qty):
        log('PARTIAL_EXIT_DUST',price=c['buyPx'],qty=str(qty),
            message='Exact market SELL below OKX lot/min; no rounding')
        return False
    cid=newid('E')
    rec={'clOrdId':cid,'ordId':'','sz':str(qty),'filled':'0',
         'phase':'PLACING','source':'partial_exit','placedAt':time.time()}
    tp_records(c).append(rec);c['state']='TP_WAIT';save(st)
    log('PARTIAL_EXIT_INTENT',price=c['buyPx'],qty=str(qty),clOrdId=cid)
    data=A.req('POST','/api/v5/trade/order',body={
        'instId':INST,'tdMode':'cross','side':'sell','posSide':'net',
        'reduceOnly':True,'ordType':'market','sz':str(qty),'clOrdId':cid})
    if len(data)!=1:
        raise ExchangeUnresolved('Partial-exit MARKET response unknown; client ID reserved')
    if data[0].get('sCode')!='0':
        rec['phase']='FAILED';rec['failCode']=data[0].get('sCode')
        c['state']='VIRTUAL_TP'
        c['partialExitRetryAt']=time.time()+60
        save(st)
        log('PARTIAL_EXIT_MARKET_REJECTED',cycleId=c['cycleId'],
            code=rec['failCode'],clOrdId=cid)
        if margin_rejection(rec['failCode']):
            trip_account_soft_stop(rec['failCode'],data[0].get('sMsg',''),'PARTIAL_EXIT')
        return False
    if not data[0].get('ordId'):
        die('Partial exit MARKET missing order ID; reconcile client ID')
    rec['ordId']=data[0]['ordId'];save(st)
    log('PARTIAL_EXIT_MARKET_ACK',price=c['buyPx'],qty=str(qty),ordId=rec['ordId'])
    return True


def partial_cleanup_action(st,bid,actual,mode):
    """Invoked only AFTER resolved order/position reconciliation.

    Actions deliberately run before normal virtual TP rearming. One exchange
    mutation per pass, then caller must reconcile from scratch.
    """
    if mode!='RUN':return 'NONE'
    # Prefer completing already requested cleanups over initiating new ones.
    for c in st['cycles'].values():
        if not c.get('partial_cleanup'):continue
        if c['state'] in ('BUY_PARTIAL','CANCELING','BUY_LIVE'):
            return 'WAIT'
        if c['state'] in ('CLOSED','EXITED','CANCELED'):
            continue
        for rec in active_tp_records(c):
            if rec['phase']=='ACTIVE':
                cancel_tp(st,c,rec)
                log('PARTIAL_EXIT_TP_CANCEL_REQUESTED',price=c['buyPx'],ordId=rec['ordId'])
                return 'ACTION'
            return 'WAIT'
        if c['state']=='VIRTUAL_TP':
            qty=remaining_tp(c)
            if qty>actual:die('Partial cleanup volume exceeds reconciled net position')
            if market_exit_partial(st,c,qty):return 'ACTION'
            return 'WAIT'
        return 'WAIT'
    for c in st['cycles'].values():
        if partial_cleanup_due(c,bid):
            c['partial_cleanup']=True
            c['partial_cleanup_at']=time.time()
            save(st)
            log('PARTIAL_EXIT_TRIGGER',price=c['buyPx'],
                filled=c['filledBuy'],bid=str(bid),
                age=int(time.time()-float(c.get('partial_since') or time.time())),
                threshold_steps=str(PARTIAL_STEPS))
            cancel_lowest_specific(st,c)
            return 'ACTION'
    return 'NONE'




class TPBufferScheduler:
    """Runtime timing only; exchange order states/cancel intent remain durable.

    Each newly observed attached SELL restarts the 1500ms quiet period
    while above the TP target. A process restart starts a fresh quiet
    period. Never sleep here or send exchange requests.
    """
    def __init__(self):
        self.seen=set()
        self.quiet_since=None
        self.last_cancel_at=None
        self.overflow_notified=False

    def observe(self,active,now):
        ids={(str(c.get('cycleId')),str(rec.get('clOrdId') or rec.get('ordId')))
             for _,c,rec in active}
        extra=len(active)>REAL_TP_TARGET
        if extra and (self.quiet_since is None or bool(ids-self.seen)):
            self.quiet_since=now
        if not extra:
            self.quiet_since=None
            self.overflow_notified=False
        self.seen=ids

    def cancel_due(self,count,now):
        if count<=REAL_TP_TARGET:return False
        if self.last_cancel_at is not None and now-self.last_cancel_at<TP_BUFFER_CANCEL_INTERVAL:
            return False
        # Normal 15/25/55 TP bursts never force immediate cancellation.
        # At the HARD limit, trim one at a time as soon as possible.
        if count>=REAL_TP_MAX:return True
        return (self.quiet_since is not None
                and now-self.quiet_since>=TP_BUFFER_COOLDOWN)

    def canceled(self,now):
        self.last_cancel_at=now


def maintain_virtual_tp(st,pending,position,bid=None,tp_buffer=None):
    real,_,_=coverage_audit(st,pending,position)
    active=real_tp_active(st)
    if tp_buffer is None and real>REAL_TP_TARGET:
        # Offline callers may omit controller.  LIVE loop always passes one
        # persistent scheduler; an implicit one never bypasses 1.5s grace.
        tp_buffer=TPBufferScheduler()
    now=time.monotonic()
    if tp_buffer is not None:tp_buffer.observe(active,now)
    if tp_buffer is not None and real>=REAL_TP_MAX and not tp_buffer.overflow_notified:
        log('TP_HARD_CAP_REACHED',real=real,maximum=REAL_TP_MAX,
            reason='No additional BUY capacity; trim surplus one by one')
        tp_buffer.overflow_notified=True
    elif tp_buffer is not None and real<REAL_TP_MAX:
        tp_buffer.overflow_notified=False
    if real>REAL_TP_TARGET:
        # Cancel surplus only after a quiet interval (or at the 150 cap).
        # During active BUY refill, the live loop defers this cleanup.
        if not tp_buffer.cancel_due(real,now):return 'NONE'
        # Exactly ONE cancel per reconciled REST snapshot, and no faster than
        # 100ms between submissions. Never operate on the nearest 25 real SELL.
        candidates=tp_buffer_candidates(active,bid)
        if not candidates:die('TP buffer has no removable surplus')
        _,c,rec=candidates[0]
        gap_ms=(round((now-tp_buffer.last_cancel_at)*1000,1)
                if tp_buffer and tp_buffer.last_cancel_at is not None else None)
        quiet_ms=(round((now-tp_buffer.quiet_since)*1000,1)
                  if tp_buffer and tp_buffer.quiet_since is not None else None)
        result=cancel_tp(st,c,rec)
        tp_buffer.canceled(time.monotonic())
        log('TP_BUFFER_CLEANUP',price=c['tpPx'],bid=str(bid),
            active_before=real,buffer_before=real-REAL_TP_TARGET,
            above_max=real>=REAL_TP_MAX,interval_ms=int(1000*TP_BUFFER_CANCEL_INTERVAL),
            quiet_ms=quiet_ms,cancel_gap_ms=gap_ms,
            reason='order_cap' if real>=REAL_TP_MAX else 'cooldown')
        return 'CANCEL'
    # At the target choose the nearest virtual TP only if it is closer
    # than the farthest currently live SELL. This avoids needless churn.
    virtual=sorted((item for item in virtual_cycles(st)
                    if not item[1].get('partial_cleanup')
                    and tp_quantity_placeable(remaining_tp(item[1]))
                    and (bid is None or D(item[1]['tpPx'])>bid)),
                   key=lambda y:D(y[1]['tpPx']))
    # Do not cancel a live protective TP to make room for a rejected
    # virtual SELL that is still in its cooldown window.
    virtual=[(key,c) for key,c in virtual
             if time.time()>=float(c.get('tpRestoreRetryAt',0))]
    if not virtual:return 'NONE'
    active=real_tp_active(st)
    if active and real>=REAL_TP_TARGET:
        farthest=max(active,key=lambda y:D(y[1]['tpPx']))
        if D(virtual[0][1]['tpPx'])<D(farthest[1]['tpPx']):
            cancel_tp(st,farthest[1],farthest[2]);return 'CANCEL'
        return 'NONE'
    if real<REAL_TP_TARGET:
        # A rejected SELL can be retried only after its cooldown; an
        # ambiguous submission never enters the virtual-only list.
        n=min(TP_ACTIONS_PER_PASS,REAL_TP_TARGET-real)
        attempted=False
        for _,c in virtual[:n]:
            if len(real_tp_active(st))>=REAL_TP_TARGET:break
            attempted=True
            place_virtual_tp(st,c)
        return 'RESTORE' if attempted else 'NONE'
    return 'NONE'

def place(st,px):
    order_start=time.perf_counter_ns()
    k=fmt(px); tp=fmt(px+TP)
    previous=st['cycles'].get(k)
    if previous:
        if previous['state'] not in ('CLOSED','CANCELED','EXITED'):
            die('Level already occupied: '+k)
        st.setdefault('archived_cycles',[]).append({'level':k,**previous})
        # Archive old level with the next PLACING intent in one durable save.
    cl=newid('B'); tcl=newid('T')
    stamp=time.time()
    expiry=new_request_deadline_ms()
    x={'state':'PLACING','buyPx':k,'tpPx':tp,'sz':str(QTY),
       'placedAt':stamp,'expTimeMs':expiry,
       'cycleId':COIN.lower()+'-'+cl,'tpOrders':[],
       'clOrdId':cl,'tpClOrdId':tcl,'ordId':'','tpOrdId':''}
    st['cycles'][k]=x; save(st)  # durable intent BEFORE network request
    req={'instId':INST,'tdMode':'cross','side':'buy','posSide':'net',
         'ordType':'post_only','px':k,'sz':str(QTY),'clOrdId':cl,
         'attachAlgoOrds':[{'attachAlgoClOrdId':tcl,'tpOrdKind':'limit','tpOrdPx':tp}]}
    try:
        data=A.req('POST','/api/v5/trade/order',body=req,exp_time_ms=expiry)
    except OrderExpiredBeforeSend:
        latency_ms('buy_post',order_start,outcome='local_expired')
        x['state']='CANCELED';x['rejectCode']='LOCAL_EXPIRED';save(st)
        log('BUY_POST_LOCAL_EXPIRY',price=k,clOrdId=cl)
        return False
    if len(data)!=1:
        die('Unexpected place-order response: '+str(data))
    if data[0].get('sCode') != '0':
        latency_ms('buy_post',order_start,outcome='rejected')
        # Positive OKX rejection acknowledgement: no order was placed.
        x['state']='CANCELED'; x['rejectCode']=data[0].get('sCode'); save(st)
        log('BUY_REJECTED',price=k,code=x['rejectCode'],msg=data[0].get('sMsg',''))
        if margin_rejection(x['rejectCode']):
            trip_account_soft_stop(x['rejectCode'],data[0].get('sMsg',''),'BUY')
        # Recalculate on next market snapshot; don't blindly resend here.
        return False
    if not data[0].get('ordId'):
        die('Order accepted without ID: manual check required')
    x['ordId']=data[0]['ordId']; x['state']='BUY_LIVE'
    # Crash after ACK replays the durable PLACING intent by exact clOrdId.
    log('BUY_PLACED',price=k,tp=tp,ordId=x['ordId'])
    latency_ms('buy_post',order_start,outcome='accepted')
    return True

def cancel_lowest_specific(st,lowest):
    if lowest.get('state') not in ('BUY_LIVE','BUY_PARTIAL') or not lowest.get('ordId'):
        die('BUY cancellation requires confirmed live/partial order')
    oid=lowest['ordId']
    # Write intent before network I/O. One POST only: on uncertain outcome,
    # CANCELING remains durable and reconciliation continues at next startup.
    lowest['state']='CANCELING'
    lowest['cancelAt']=time.time()
    save(st)
    data=A.req('POST','/api/v5/trade/cancel-order',body={'instId':INST,'ordId':oid})
    if len(data)!=1:
        raise ExchangeUnresolved('BUY cancel response ambiguous; intent preserved')
    if data[0].get('sCode')!='0':
        lowest['cancelRejectCode']=data[0].get('sCode')
        lowest['cancelRejectAt']=time.time()
        save(st)
        log('BUY_CANCEL_REJECTED',price=lowest['buyPx'],ordId=oid,
            code=lowest['cancelRejectCode'],message=data[0].get('sMsg',''))
        return False
    log('BUY_CANCEL_REQUESTED',cancelled=lowest['buyPx'],ordId=oid)
    # ACK is not a final state. No immediate GET or premature CANCELED here.
    # Next reconcile must observe canceled OR filled. In-between live means
    # WAIT, not STOP. On filled, attached TP remains bound to same CycleID.
    return True

class WSWatcher(threading.Thread):
    """Private order event stream; REST remains the authority for reconciliation."""
    def __init__(self):
        super().__init__(daemon=True)
        self.events=queue.Queue(maxsize=1000)
        self.wake=threading.Event()
        self.private_wake=threading.Event()
        self.connected=False
    def run(self):
        while True:
            ws=None
            try:
                ws=websocket.create_connection('wss://ws.okx.com/ws/v5/private',
                    timeout=12,header=['User-Agent: curl/8.5.0'])
                ts=str(int(time.time()))
                msg=ts+'GET/users/self/verify'
                sig=base64.b64encode(hmac.new(ENV['OKX_API_SECRET'].encode(),
                    msg.encode(),hashlib.sha256).digest()).decode()
                ws.send(json.dumps({'op':'login','args':[{'apiKey':ENV['OKX_API_KEY'],
                    'passphrase':ENV['OKX_API_PASSPHRASE'],'timestamp':ts,'sign':sig}]}))
                ans=json.loads(ws.recv())
                if ans.get('event')!='login' or ans.get('code')!='0': die('WS login '+str(ans))
                ws.send(json.dumps({'op':'subscribe','args':[{'channel':'orders',
                    'instType':'SWAP','instId':INST}]}))
                ans=json.loads(ws.recv())
                if ans.get('event')!='subscribe': die('WS subscribe '+str(ans))
                self.connected=True
                ws.settimeout(5); last_ping=time.monotonic()
                while True:
                    if time.monotonic()-last_ping>=15:
                        ws.send('ping');last_ping=time.monotonic()
                    try: raw=ws.recv()
                    except websocket.WebSocketTimeoutException: continue
                    if raw=='ping': ws.send('pong');continue
                    if raw=='pong':continue
                    parsed=json.loads(raw)
                    if parsed.get('event')=='error': die('WS event error '+str(parsed))
                    if parsed.get('arg',{}).get('channel')=='orders':
                        for item in parsed.get('data',[]):
                            if item.get('instId')==INST:
                                safe={k:item.get(k) for k in ('ordId','clOrdId','algoClOrdId',
                                    'side','state','accFillSz','fillSz','tradeId')}
                                self.events.put_nowait(safe)
                                self.private_wake.set()
                                self.wake.set()
            except Exception as e:
                self.connected=False
                # Never log authentication messages containing credentials.
                print('[WS] reconnect in 5s:',type(e).__name__,flush=True)
                time.sleep(5)
            finally:
                self.connected=False
                if ws:
                    try: ws.close()
                    except Exception: pass


class PublicBidWatcher(threading.Thread):
    """Realtime OKX BID feed. Never sends private credentials.

    The ticker channel pushes a snapshot on price/best-bid changes (fastest
    published update period 100ms). REST is used if WS is disconnected or
    has not received an update for a while. A stale cached quote is NEVER used
    for placing/canceling live orders.
    """
    def __init__(self, shared_wake=None):
        super().__init__(daemon=True)
        self._shared_wake=shared_wake
        self._grid_ref=None
        self._last_price_wake=0.0
        # WS remembers every crossed virtual TP even if BID retraces before
        # the main REST loop becomes runnable. Only the main thread persists it.
        self._virtual_targets={}
        self._virtual_crossed=set()
        self._lock=threading.Lock()
        self._bid=None
        self._updated=0.0
        self.connected=False
        self.wake=threading.Event()
        self.fallback_logged=False

    def run(self):
        while True:
            ws=None
            try:
                ws=websocket.create_connection('wss://ws.okx.com/ws/v5/public',
                    timeout=12,header=['User-Agent: curl/8.5.0'])
                ws.send(json.dumps({'op':'subscribe','args':[
                    {'channel':'tickers','instId':INST}]}))
                reply=json.loads(ws.recv())
                if reply.get('event') != 'subscribe':
                    die('Public BID subscription failed: '+str(reply))
                self.connected=True
                print('✅ Public BID WebSocket connected',flush=True)
                ws.settimeout(3)
                last_ping=time.monotonic()
                while True:
                    if time.monotonic()-last_ping >= 15:
                        ws.send('ping')
                        last_ping=time.monotonic()
                    try:
                        raw=ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if raw=='ping': ws.send('pong'); continue
                    if raw=='pong': continue
                    msg=json.loads(raw)
                    if msg.get('event')=='error': die('Public BID WS error '+str(msg))
                    if msg.get('arg',{}).get('channel')!='tickers':
                        continue
                    for data in msg.get('data',[]):
                        if data.get('instId')!=INST: continue
                        bid=D(data.get('bidPx'))
                        if bid<=0: continue
                        with self._lock:
                            self._bid=bid
                            self._updated=time.monotonic()
                            self._capture_crossings(bid)
                            new_top=grid_top(bid)
                            trigger=(self._grid_ref is not None
                                and new_top>self._grid_ref
                                and time.monotonic()-self._last_price_wake>=PRICE_WAKE_COOLDOWN)
                            if trigger:self._last_price_wake=time.monotonic()
                        self.wake.set()
                        if trigger and self._shared_wake is not None:
                            self._shared_wake.set()
            except Exception as e:
                print('[BID WS] reconnect in 5s:',type(e).__name__,flush=True)
                time.sleep(5)
            finally:
                self.connected=False
                with self._lock:
                    self._updated=0.0
                if ws:
                    try: ws.close()
                    except Exception: pass

    def _capture_crossings(self,bid):
        # Caller holds _lock. A fast peak must not be lost when BID retreats.
        for cycle_id,tp in self._virtual_targets.items():
            if bid >= tp:
                self._virtual_crossed.add(cycle_id)

    def set_virtual_targets(self,targets):
        # Published by main thread only from reconciled virtual cycles.
        with self._lock:
            self._virtual_targets=dict(targets)
            if (self.connected and self._bid is not None
                and time.monotonic()-self._updated<=3):
                self._capture_crossings(self._bid)

    def take_crossed_virtual_ids(self):
        with self._lock:
            found=set(self._virtual_crossed)
            self._virtual_crossed.clear()
            return found

    def set_grid_reference(self,top):
        with self._lock:self._grid_ref=top

    def cached_bid(self):
        """Fresh WS quote ONLY. Never initiate a REST call for monitoring."""
        with self._lock:
            if self.connected and self._bid is not None and time.monotonic()-self._updated<=3.0:
                return self._bid
        return None

    def get_bid(self):
        with self._lock:
            cached=self._bid
            age=time.monotonic()-self._updated
        if self.connected and cached is not None and age<=3.0:
            self.fallback_logged=False
            return cached
        # When an instrument stays unchanged the channel need not publish;
        # verify the quote with REST instead of assuming the old WS bid is fresh.
        if not self.fallback_logged:
            log('BID_REST_FALLBACK',reason='WS disconnected or quote >3s old')
            self.fallback_logged=True
        return A.ticker()












class PnLWorker(threading.Thread):
    """Calculate real fill fees in background, NEVER delay trading on PnL I/O."""
    def __init__(self,pnl):
        super().__init__(daemon=True)
        self.pnl=pnl
        self.jobs=queue.Queue(maxsize=1)

    def submit(self,st):
        # Main thread alone owns/mutates trading state; the copy is immutable
        # to the worker and skips a file write if there are no closed cycles.
        if not any(x.get('state') in ('CLOSED','EXITED')
                   for x in list(st['cycles'].values())+st.get('archived_cycles',[])):
            return
        # Only pending closed cycles are relevant; no enormous historical state copy.
        relevant=[c for c in self.pnl.all_cycles(st)
                  if self.pnl.identity(c) and not self.pnl.recorded(c)]
        if not relevant:return
        slim={'cycles':{str(i):c for i,c in enumerate(relevant)},'archived_cycles':[]}
        try: self.jobs.put_nowait(copy.deepcopy(slim))
        except queue.Full: pass  # Next pass will submit a newer snapshot.

    def run(self):
        while True:
            state=self.jobs.get()
            try:
                self.pnl.work(state,max_cycles=2,historical=False)
            except Exception as e:
                print('⚠️ PnL background error:',type(e).__name__,str(e)[:140],flush=True)


def acquire_lock():
    (ROOT/'state').mkdir(mode=0o700,exist_ok=True)
    (ROOT/'logs').mkdir(mode=0o700,exist_ok=True)
    path=ROOT/'state'/'bot.lock'
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        die('Another instance is already running in this directory. STOP it first.')
    return fd


def ensure_state():
    if DATA.exists():
        st=json.loads(DATA.read_text(encoding='utf-8'))
        try:return validate_state(st, INST)
        except ValueError as exc:die(str(exc))
    if not LIVE:
        return empty_state(VER, INST)
    return initial_state()


def position_retry_delay(retry, pause=0.5):
    """Bounded first recheck; later retries retain 0.5s backoff.

    Only used after a COMPLETE fresh REST pass detected a mismatch. It never
    skips /orders-pending, order history, fill tracking or /positions.
    """
    return min(float(pause), 0.15 if retry==1 else (0.30 if retry==2 else 0.50))


def reconcile_position(st, timeout=30, pause=0.5, *, allow_mismatch=False):
    """Read *fresh* order states and net position until they agree.

    Under rapid multiple fills a sequential GET /trade/order pass is not an
    atomic snapshot: later orders can fill AFTER we queried them, but BEFORE
    /account/positions. An immediate mismatch isn't proof of missing trades.
    NEVER submit new orders until a complete fresh pass reconciles. If the
    mismatch persists beyond the timeout, fail closed with existing orders
    still on the exchange.
    """
    p=_ReconProfiler() if globals().get('_PERF_RECON_ENABLED',False) else None
    if p is not None:_PERF_LOCAL.current=p
    try:
        started=time.monotonic()
        previous=None
        while True:
            if p is not None:p.passes+=1
            pending=(p.run('pending',A.pending) if p is not None else A.pending())
            # Defer only re-observable exchange facts; preserve emergency PANIC
            # durability and validate identity before the net-position check.
            with _batch_confirmed_facts(st):
                (p.run('register_attached',register_cancel_attached,st,pending) if p is not None else register_cancel_attached(st,pending))
                resolved=(p.run('track',track,st,pending) if p is not None else track(st,pending))
                (p.run('verify_existing',verify_existing,st,pending) if p is not None else verify_existing(st,pending))
            # The position GET happens as soon as the confirmed fact batch is
            # reconciled/durable; NEVER fetch positions before inspecting fills.
            if st.get('panic_orders'):
                resolved=reconcile_panic_orders(st,pending) and resolved
            positions=(p.run('positions',A.positions) if p is not None else A.positions())
            expected=sum((D(x.get('filledBuy'))-D(x.get('filledTP'))
                          for x in st['cycles'].values()),Decimal('0')) - panic_executions(st)
            actual=sum((D(p.get('pos')) for p in positions),Decimal('0'))
            if actual==expected:
                if previous is not None:
                    log('POSITION_SYNC_OK',exchange=str(actual),local=str(expected),
                        retries=previous)
                return pending,resolved,actual
            if p is not None:p.mismatches+=1
            remaining=timeout-(time.monotonic()-started)
            if remaining <= 0:
                if allow_mismatch:
                    # We still have a fresh list of individual known pending BUYs.
                    # Permit their exact-ID cancellations in SOFT, but block any
                    # new exposure or TP placement that needs position coverage.
                    log('EMERGENCY_POSITION_UNRESOLVED' if control_mode() in ('HARD','PANIC')
                        else 'SOFT_POSITION_UNRESOLVED',exchange=str(actual),local=str(expected),
                        message='Only independently confirmed BUY cancels are allowed')
                    return pending,False,actual
                raise ExchangeUnresolved('Position mismatch persisted after full REST rechecks: '
                    f'exchange={actual} local={expected}; no new BUY. '
                    'Inspect pending orders and position before restarting.')
            # Expose the mismatch, rather than making a trading decision with
            # data from different moments in time.
            if previous is None:
                previous=0
            previous+=1
            if previous==1 or previous%5==0:
                log('POSITION_SYNC_WAIT',exchange=str(actual),local=str(expected),
                    retry=previous,timeout_seconds=timeout)
            delay=min(position_retry_delay(previous,pause),max(0.1,remaining))
            (p.run('retry_sleep',time.sleep,delay) if p is not None else time.sleep(delay))

    finally:
        if p is not None:
            _PERF_LOCAL.current=None
            exc_type=sys.exc_info()[0]
            p.finish(error_type=exc_type.__name__ if exc_type is not None else None)
            LATENCY.record('position_reconcile',
                           (time.perf_counter_ns()-p.started_ns)/1_000_000,
                           passes=p.passes, mismatches=p.mismatches,
                           outcome='exception' if exc_type is not None else 'returned')


def audit_once(st):
    pending,resolved,actual=reconcile_position(st,timeout=30)
    real,covered,virtual=coverage_audit(st,pending,actual)
    states={}
    for c in st['cycles'].values():
        states[c['state']]=states.get(c['state'],0)+1
    buys=sum(o.get('side')=='buy' for o in pending)
    print(f'=== {COIN} TP VIRTUAL AUDIT v{VER} ===',flush=True)
    print('Position contracts:',actual,'| active TP contracts:',covered,
          '| VIRTUAL contracts:',virtual,
          '| PARTIAL BUY contracts:',unprotected_partial_buy(st),flush=True)
    print('Open BUY:',buys,'| Real TP:',real,'| Virtual cycles:',len(virtual_cycles(st)),flush=True)
    print('Tracked states:',json.dumps(states,sort_keys=True),flush=True)
    print('CycleID unique:',len({c['cycleId'] for c in st['cycles'].values()})==len(st['cycles']))
    print('TP links resolved:',resolved,flush=True)
    if not resolved:
        die('Some TP identities unknown. No trading until fully reconciled.')
    print('✅ AUDIT CONSISTENT — NO ORDERS SUBMITTED OR CANCELED',flush=True)
    print('⚠️ Virtual TP positions are UNPROTECTED while bot is offline.',flush=True)


def safe_tp_during_buy_wait(st,pending,actual):
    """Recover other virtual TP while a separate order remains unresolved.

    Unknown SELL quantities are always reserved. We never cancel/move a real
    SELL during incomplete reconciliation, and submit only ONE new SELL before
    the next fresh full REST snapshot. No new BUY can pass this function.
    """
    if unresolved_aggregate(st):return 'AGG_MARKET_PENDING'
    pending_sells=[o for o in pending if o.get('side')=='sell']
    pending_ids={str(o.get('ordId')) for o in pending_sells}
    pending_cids={str(o.get('clOrdId')) for o in pending_sells}
    known_pending=sum((D(o['sz'])-D(o.get('accFillSz')) for o in pending_sells),Decimal(0))
    reserved=Decimal(0)
    uncertain_count=0
    for c in st['cycles'].values():
        for rec in tp_records(c):
            if rec['phase'] not in ('PENDING','PLACING','ACTIVE','CANCELING'):
                continue
            if (str(rec.get('ordId') or '') in pending_ids and rec.get('ordId') or
                str(rec.get('clOrdId') or '') in pending_cids and rec.get('clOrdId')):
                continue
            remaining=D(rec['sz'])-D(rec.get('filled'))
            if remaining<0:
                raise ExchangeUnresolved('Negative uncertain SELL reservation')
            reserved+=remaining
            uncertain_count+=1
    virtual=sum((remaining_tp(c) for _,c in virtual_cycles(st)),Decimal(0))
    partial=unprotected_partial_buy(st)
    if min(known_pending,reserved,virtual,partial)<0:
        return 'NEGATIVE_COVERAGE'
    if known_pending+reserved+virtual+partial!=actual:
        # Do not turn an unknown order into a duplicate SELL.
        return 'COVERAGE_UNRESOLVED'
    if len(pending_sells)+uncertain_count >= REAL_TP_TARGET:
        return 'TP_LIMIT_RESERVED'
    virtuals=sorted(((key,c) for key,c in virtual_cycles(st)
                     if not c.get('partial_cleanup')
                     and tp_quantity_placeable(remaining_tp(c))
                     and time.time()>=float(c.get('tpRestoreRetryAt',0))),
                    key=lambda item:D(item[1]['tpPx']))
    if not virtuals:return 'NONE'
    # Account exposure already balanced, and every unknown SELL volume is reserved.
    place_virtual_tp(st,virtuals[0][1])
    return 'RESTORE'


def coverage_hold_delay(attempts):
    return (0.5,1.0,2.0)[min(max(0,attempts),2)]


def wait_coverage_hold(watcher,delay):
    """Bounded HOLD backoff; private order events or emergency stop wake it.

    Public BID changes never reset this timer. A wake merely requests a fresh
    REST reconciliation and cannot approve new BUY on its own.
    """
    deadline=time.monotonic()+delay
    while True:
        if control_mode()!='RUN':
            return 'CONTROL'
        if not watcher.events.empty() or watcher.private_wake.is_set():
            return 'PRIVATE'
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return 'TIMEOUT'
        watcher.private_wake.wait(min(0.2,remaining))


def live_loop(st,watcher,market,pnl,pnl_worker,dd_worker=None):
    last_status_key=None
    last_print=0
    last_sync_notice=0.0
    last_sync_kind=None
    last_heartbeat=0.0
    last_fee_check=0.0
    last_archive_compact=0.0
    sync_since=None
    sync_warn_level=0
    maker_cost=None
    tp_buffer=TPBufferScheduler()
    coverage_hold_reason=None
    coverage_hold_attempts=0
    while True:
        try:
            started=time.monotonic()
            loop_start_ns=time.perf_counter_ns()
            mode=control_mode()
            if st.get('terminal_mode'):
                control_ack(mode,st['terminal_mode'])
                return
            if started-last_heartbeat>=15:
                runtime_heartbeat()
                last_heartbeat=started
            # Private WS acts as a wake signal; only REST-synchronized states may
            # cause order placement or rearming. Drop duplicate messages safely.
            ws_start=time.perf_counter_ns()
            ws_n=0
            while not watcher.events.empty():
                log('WS_ORDER',**watcher.events.get_nowait())
                ws_n+=1
            if ws_n:
                latency_ms('ws_drain',ws_start,count=ws_n)
            watcher.wake.clear()
            watcher.private_wake.clear()

            if mode in ('HARD','PANIC') and emergency_precancel_own_buys(st,mode):
                # Resume with a FRESH snapshot next pass. Never let an unknown
                # TP, foreign order or saturated /positions delay safe BUY cancel.
                continue
            pending,all_resolved,actual=reconcile_position(
                st,timeout=30,allow_mismatch=(mode in ('SOFT','HARD','PANIC')))
            # A manager command may change during a 30-second REST pass.
            # Never continue to BUY placement under a stale RUN/PAUSE mode.
            if control_mode()!=mode:
                continue
            # Publish reconciled virtual targets to the ticker listener and
            # durably latch crossings observed during the previous REST pass.
            # Never send orders here; exchange position/order gates remain.
            persist_crossed_virtual_tps(st,market)
            if all_resolved:
                if sync_since is not None:
                    log('RECONCILIATION_RECOVERED',duration_sec=int(time.monotonic()-sync_since))
                    sync_since=None;sync_warn_level=0
            else:
                if sync_since is None:sync_since=time.monotonic()
                elapsed=time.monotonic()-sync_since
                level=2 if elapsed>=120 else (1 if elapsed>=60 else 0)
                if level>sync_warn_level:
                    log('RECONCILIATION_LONG_WAIT',seconds=int(elapsed),
                        severity='CRITICAL' if level==2 else 'WARNING',
                        mode=mode)
                    sync_warn_level=level
            if all_resolved:
                pnl_submit_start=time.perf_counter_ns()
                try:
                    pnl_worker.submit(st)
                finally:
                    latency_ms('pnl_submit',pnl_submit_start)
                if dd_worker is not None:
                    dd_submit_start=time.perf_counter_ns()
                    try:
                        dd_worker.submit_reconciled(st)
                    finally:
                        latency_ms('drawdown_submit',dd_submit_start)
                if started-last_archive_compact>=30:
                    last_archive_compact=started
                    if st.get('archived_cycles'):
                        compact_archived(st,pnl)
            if mode in ('HARD','PANIC'):
                terminal_bid=market.cached_bid()
                # Only at completed HARD cancellation: a fresh public BID
                # is required for a truthful stop-time unrealized PnL.
                if (mode=='HARD' and terminal_bid is None and all_resolved
                        and not pending and not active_buy(st) and not real_tp_active(st)):
                    try:terminal_bid=market.get_bid()  # public quote REST fallback
                    except (ExchangeTemporary,ExchangeUnresolved):
                        log('TERMINAL_BID_UNAVAILABLE',mode='HARD')
                if controlled_shutdown(st,pending,actual,all_resolved,mode,terminal_bid):
                    return
                time.sleep(0.15)
                continue
            # Re-read latch AFTER REST I/O. A different bot may have tripped
            # account SOFT while our process was reconciling.
            mode=control_mode()
            if mode=='SOFT':
                phase=cancel_buys_for_soft_stop(st,all_resolved,pending)
                control_ack(mode,phase)
                if phase!='SOFT_STOPPED':
                    # While a BUY is unresolved, continue maintaining
                    # independently verified protective SELLs when safe.
                    # A cancellation we just submitted must first reconcile.
                    if phase=='WAIT_RECONCILIATION':
                        safe_tp_during_buy_wait(st,pending,actual)
                    time.sleep(0.15)
                    continue
            elif mode=='PAUSE':
                control_ack(mode,'PAUSED')
            else:
                control_ack(mode,'RUNNING')
            if not all_resolved:
                buy_cancel_count=sum(c['state']=='CANCELING' for c in st['cycles'].values())
                tp_cancel_count=sum(r.get('phase')=='CANCELING'
                    for c in st['cycles'].values() for r in tp_records(c))
                reason=(buy_cancel_count,tp_cancel_count)
                now=time.monotonic()
                if reason!=last_sync_kind or now-last_sync_notice>=10:
                    if buy_cancel_count or tp_cancel_count:
                        print(f'⏳ Отмены на OKX ещё обрабатываются: BUY {buy_cancel_count}, '
                              f'TP {tp_cancel_count} | новые заявки на паузе',flush=True)
                    else:
                        print('⏳ Проверяем привязку TP | новые BUY на паузе',flush=True)
                    last_sync_notice=now;last_sync_kind=reason
                safe_tp_during_buy_wait(st,pending,actual)
                watcher.wake.wait(0.15)
                watcher.wake.clear()
                continue

            # surplus TP no longer enters maintain_virtual_tp()
            # before BUY refill. Preserve its independent coverage check.
            # Do not send BUY if SELL/virtual exposure is inconsistent.
            if mode=='RUN':
                try:
                    coverage_audit(st,pending,actual)
                except CoverageMismatch as exc:
                    # Existing LIMIT TP at OKX remain live even during HOLD.
                    # A separate protected virtual TP may be placed ONLY when
                    # safe_tp_during_buy_wait independently proves coverage.
                    now=time.monotonic()
                    if last_sync_kind!='COVERAGE_HOLD' or now-last_sync_notice>=10:
                        log('COVERAGE_RECOVERY_WAIT',reason=str(exc)[:180],
                            mode=mode,message='BUY blocked; existing TP retained')
                        last_sync_kind='COVERAGE_HOLD'
                        last_sync_notice=now
                    reason=str(exc)
                    if reason==coverage_hold_reason:
                        coverage_hold_attempts+=1
                    else:
                        coverage_hold_reason=reason
                        coverage_hold_attempts=0
                    delay=coverage_hold_delay(coverage_hold_attempts)
                    if control_mode()!=mode:
                        continue
                    outcome=safe_tp_during_buy_wait(st,pending,actual)
                    if outcome=='RESTORE':
                        # A protective TP was submitted; reconcile promptly.
                        coverage_hold_attempts=0
                        continue
                    wait_coverage_hold(watcher,delay)
                    continue
                else:
                    coverage_hold_reason=None
                    coverage_hold_attempts=0
            # TP lifecycle takes precedence over all new BUY placements.
            # Any cancel/restore requires a full new REST reconciliation.
            bid=market.get_bid()
            partial_action=partial_cleanup_action(st,bid,actual,mode)
            if partial_action!='NONE':
                watcher.wake.wait(0.15);watcher.wake.clear()
                continue
            escape_action=virtual_escape(st,bid,actual,pending,mode)
            if escape_action!='NONE':
                watcher.wake.wait(0.10 if escape_action=='WAIT' else 0.15)
                watcher.wake.clear()
                continue
            # rearm under-target real protective TP *before* BUY.
            # At target, the farthest-to-nearest TP rotation is elective and
            # now waits until after the BUY refill. Surplus already waits.
            real_tp_now=len(real_tp_active(st))
            tp_buffer.observe(real_tp_active(st),time.monotonic())
            if real_tp_now>=REAL_TP_TARGET:
                tp_action='NONE'
            else:
                tp_action=maintain_virtual_tp(st,pending,actual,bid,tp_buffer)
            if tp_action!='NONE':
                watcher.wake.wait(0.10 if tp_action=='CANCEL' else 0.15)
                watcher.wake.clear()
                continue
            waiting=bool(UPPER and bid>UPPER)
            if waiting and last_sync_kind!='UPPER':
                log('WAIT_UPPER',bid=str(bid),upper=str(UPPER),
                    message='No new BUY while bid exceeds upper bound')
                last_sync_kind='UPPER'
            if not waiting and last_sync_kind=='UPPER':
                log('UPPER_RESUMED',bid=str(bid),upper=str(UPPER))
                last_sync_kind=None
            # Fee-tier changes should never cause fresh BUYs with unprofitable TP.
            # When API is unavailable, pause new BUYs but keep TP maintenance alive.
            if time.monotonic()-last_fee_check>(30 if maker_cost is None else 600):
                last_fee_check=time.monotonic()
                try:
                    maker_cost=A.maker_fee()
                except Exception as exc:
                    maker_cost=None
                    log('FEE_CHECK_PAUSED_BUYS',error=f'{type(exc).__name__}: {str(exc)[:100]}')
            if maker_cost is None:
                waiting=True
            # When a partially canceled BUY left sub-lot exposure, preserving
            # it is safer than a synthetic round-up or another BUY. Continue
            # servicing every other TP, but block new BUY until resolved.
            dust=unplaceable_virtual_tp(st)
            if dust:
                waiting=True
                if last_sync_kind!='UNPLACEABLE_TP':
                    log('BUY_PAUSED_UNPLACEABLE_TP',cycles=len(dust),
                        qty=str(sum((remaining_tp(c) for _,c in dust),Decimal('0'))))
                    last_sync_kind='UNPLACEABLE_TP'
            elif last_sync_kind=='UNPLACEABLE_TP':
                log('BUY_RESUMED_LOT_COVERAGE');last_sync_kind=None
            top=grid_top(bid)
            active=active_buy(st)
            validate_buy_buffer(active)

            # protect near-market BUYs during BOTH directions of shift.
            # extra BUYs are a temporary buffer, never a steady target.
            # All order writes are preceded by the same full REST reconciliation.
            market.set_grid_reference(top)
            if not hasattr(live_loop,'_shift_top'):
                live_loop._shift_top=top
                live_loop._shift_at=time.monotonic()
            if top!=live_loop._shift_top:
                live_loop._shift_top=top
                live_loop._shift_at=time.monotonic()
            active_prices={D(x['buyPx']) for x in active}
            occupied_tp={D(k) for k,x in st['cycles'].items()
                         if x['state'] in ('TP_WAIT','TP_PARTIAL','VIRTUAL_TP')}
            desired=desired_buy_levels(top,occupied_tp)
            # r2: DO NOT add a BUY below the lowest of the 25
            # existing highest-price BUYs while 25+ orders are already live.
            # Otherwise bottom-up cleanup would immediately cancel this order
            # and the two algorithms could churn endlessly at a STATIC BID.
            missing=missing_buy_levels_for_addition(desired,active)
            outdated=[x for x in active if D(x['buyPx']) not in desired]
            reserve=bool(missing and outdated and len(active)>=TARGET)
            if len(active)>=BUY_MAX:
                reserve=False
            slots=(BUY_MAX if reserve else TARGET)-len(active)
            real_tp_count=len(real_tp_active(st))
            # All outstanding SELLs count toward the 150-instrument cap.
            # Keep the existing 5-slot headroom for asynchronously attached
            # TP; never borrow capacity from unresolved exchange orders.
            allowed_buys=max(0,min(BUY_MAX,REDUCE_ONLY_HARD_CAP-real_tp_count-5))
            cap=(0 if waiting or mode!='RUN' else max(0,min(slots,allowed_buys-len(active),
                 CAP-count(st),EX_CAP-len(pending))))
            candidates=[]
            prices=missing
            for px in prices:
                if len(candidates)>=cap:break
                if (not UPPER or px<=UPPER) and level_free(st,px):candidates.append(px)
            if not LIVE:
                print('DRY RUN', 'BID',bid,'current BUY',len(active),
                      'occupied',count(st),'would place',
                      [(fmt(p),fmt(p+TP)) for p in candidates],flush=True)
                break
            placed=0
            buy_batch_start=time.perf_counter_ns()
            for price in candidates:
                if control_mode()!='RUN':break
                current_bid=market.get_bid()
                if (UPPER and (current_bid>UPPER or price>UPPER)) or price>=current_bid:
                    log('BUY_BATCH_PAUSED',reason='Fresh BID no longer above LIMIT',level=fmt(price))
                    break
                if not level_free(st,price):continue
                if maker_cost is None or TP<required_safe_tp(price,maker_cost):
                    log('BUY_BATCH_PAUSED',reason='Maker fee safety minimum')
                    break
                if len(active_buy(st))>=allowed_buys or count(st)>=CAP:break
                if place(st,price):placed+=1
                else:break
                time.sleep(0.05)  # BUY pacing; keep order rate limit
            if candidates:
                latency_ms('buy_batch',buy_batch_start,
                           attempted=len(candidates),placed=placed,
                           active_buys=len(active_buy(st)),target_buys=TARGET)
            # After 1.5s without ANY grid shift, trim surplus BUYs BOTTOM-UP.
            # At the 30-BUY ceiling, a missing level may release the LOWEST
            # confirmed safe BUY earlier. Never choose cancellation candidates
            # based on the computed desired grid; it is only for additions.
            moved=0
            active=active_buy(st)
            # Avoid an extra REST ticker request on every idle second when
            # no cleanup is due (the public WS quote can be >3s old).
            cleanup_due=(time.monotonic()-live_loop._shift_at>=BUY_BUFFER_COOLDOWN
                         and len(active)>=TARGET+1)
            capacity_due=(len(active)>=BUY_MAX and bool(missing))
            if (mode=='RUN' and not waiting and len(active)>=TARGET
                and (cleanup_due or capacity_due)):
                fresh_bid=market.get_bid()
                fresh_top=grid_top(fresh_bid)
                if fresh_top!=live_loop._shift_top:
                    live_loop._shift_top=fresh_top
                    live_loop._shift_at=time.monotonic()
                fresh_desired=desired_buy_levels(fresh_top,occupied_tp)
                still_missing=bool(fresh_desired-{D(c['buyPx']) for c in active})
                needs_room=len(active)>=BUY_MAX and still_missing
                cooling=time.monotonic()-live_loop._shift_at>=BUY_BUFFER_COOLDOWN
                max_remove=(max(0,len(active)-TARGET) if cooling
                            else (1 if needs_room else 0))
                selected,protected=plan_buy_buffer_cleanup(
                    active,fresh_bid,min(max_remove,5))
                for c in selected:
                    # A fresh quote immediately before each cancel protects
                    # against mid-batch reversal and REST/WS scheduling delays.
                    cancel_bid=market.get_bid()
                    cancel_top=grid_top(cancel_bid)
                    cancel_desired=desired_buy_levels(cancel_top,occupied_tp)
                    if cancel_top!=live_loop._shift_top:
                        live_loop._shift_top=cancel_top
                        live_loop._shift_at=time.monotonic()
                        # New grid shift: allow cancellation ONLY to free
                        # capacity at the hard 30-BUY ceiling. Otherwise wait
                        # the complete 1.5s cooldown again.
                        latest_missing=bool(cancel_desired-
                            {D(x['buyPx']) for x in active_buy(st)})
                        if not (len(active_buy(st))>=BUY_MAX and latest_missing):
                            log('BUY_CANCEL_DEFERRED',bid=str(cancel_bid),
                                reason='Grid shifted while cleanup was pending')
                            break
                    # Re-evaluate ALL currently open BUYs under the latest BID.
                    # The originally selected order must STILL be the lowest
                    # cancellable one: never cancel a higher price by mistake.
                    fresh_lowest,_=plan_buy_buffer_cleanup(
                        active_buy(st),cancel_bid,1)
                    if not fresh_lowest or fresh_lowest[0].get('ordId')!=c.get('ordId'):
                        log('BUY_CANCEL_DEFERRED',price=c['buyPx'],
                            bid=str(cancel_bid),reason='Lowest safe BUY changed before cancel')
                        break
                    if cancel_lowest_specific(st,c):
                        moved+=1
                if moved:
                    log('BUY_BUFFER_CLEANUP',count=moved,
                        bid=str(fresh_bid),age_ms=int(
                            1000*(time.monotonic()-live_loop._shift_at)))
                elif max_remove and protected:
                    log('BUY_CANCEL_PROTECTED',
                        levels=[c['buyPx'] for c in protected[:5]],
                        bid=str(fresh_bid),reason='Near BID; retained inside 30 BUY limit')
            # surplus TP cleanup is opportunistic and one-at-a-time,
            # after a pass with no new BUY/BUY-cancel request. This preserves
            # the existing rule: every TP cancellation must reconcile fully
            # before any subsequent exchange write.
            if (not placed and not moved and
                    len(real_tp_active(st))>REAL_TP_TARGET):
                trim_action=maintain_virtual_tp(st,pending,actual,
                    market.get_bid(),tp_buffer)
                if trim_action!='NONE':
                    watcher.wake.wait(0.10 if trim_action=='CANCEL' else 0.15)
                    watcher.wake.clear()
                    continue
            # Rotation at the baseline is not a hard safety rearm: only do it
            # on an idle pass, never ahead of restoring missing BUY slots.
            if (not placed and not moved
                    and len(real_tp_active(st))==REAL_TP_TARGET
                    and virtual_cycles(st)):
                rotate_action=maintain_virtual_tp(st,pending,actual,
                    market.get_bid(),tp_buffer)
                if rotate_action!='NONE':
                    watcher.wake.wait(0.15)
                    watcher.wake.clear()
                    continue
            states={}
            for c in st['cycles'].values():
                states[c['state']]=states.get(c['state'],0)+1
            tps=states.get('TP_WAIT',0)+states.get('TP_PARTIAL',0)
            virtual=states.get('VIRTUAL_TP',0)
            status=(f'РЕЖИМ={mode} | BID={bid} | '+(f'WAITING ≤{UPPER} | ' if UPPER and bid>UPPER else '')
                    +('PAUSE: VIP fee unavailable | ' if maker_cost is None else '')
                    +f'BUY {len(active_buy(st))}/{TARGET} '
                    f'| TP {tps} (реальных {len(real_tp_active(st))}) | виртуальных {virtual} | гриды {count(st)}/{CAP} '
                    f'| закрыто {st["completed"]} | {pnl.status(st)}')
            now=time.monotonic()
            status_key=(waiting,len(active_buy(st)),tps,virtual,count(st),st['completed'],pnl.status(st))
            if status_key!=last_status_key or placed or moved or now-last_print>=60:
                print('📊 '+status,flush=True)
                last_status_key=status_key; last_print=now
            # Per-pass timing includes full REST reconciliation and all writes.
            # It is only a diagnostic, not a deadline or a trading gate.
            latency_ms('loop_pass',loop_start_ns,
                placed=placed,active_buys=len(active_buy(st)),
                target_buys=TARGET,dropped=LATENCY.dropped)
            # Keep the safety loop at ~1s maximum idle delay. During REST work,
            # processing can still be slower; never promise a fixed 1s reaction.
            delay=max(0.0, INTERVAL-(time.monotonic()-started))
            if delay:
                watcher.wake.wait(delay)
                watcher.wake.clear()

        except (ExchangeTemporary, ExchangeUnresolved) as exc:
            # Safe recovery: maintain exchange orders unchanged, retry READ reconciliation.
            # No duplicate POST and NO systemd restart storm during connectivity failures.
            message=f'{type(exc).__name__}: {str(exc)[:200]}'
            if sync_since is None:sync_since=time.monotonic()
            elapsed=time.monotonic()-sync_since
            level=2 if elapsed>=120 else (1 if elapsed>=60 else 0)
            if level>sync_warn_level:
                log('RECONCILIATION_LONG_WAIT',seconds=int(elapsed),
                    severity='CRITICAL' if level==2 else 'WARNING',
                    mode=control_mode(),reason=message)
                sync_warn_level=level
            if message!=last_sync_kind or time.monotonic()-last_sync_notice>=30:
                log('SYNC_WAIT',reason=message,mode=control_mode())
                last_sync_kind=message
                last_sync_notice=time.monotonic()
            # HARD/PANIC requests also wait for confirmed exchange state; they are not lost.
            watcher.wake.wait(3)
            watcher.wake.clear()
            continue



# OKX fill-based statistics run outside the trading thread.
# All numbers are derived from OKX's actual fills (not LIMIT prices).
STATS_INTERVAL = 600  # calculate at start, then every ten minutes
STATS_FILE = ROOT / 'state' / 'stats.json'  # same schema across all new OKX bots
STATS_HISTORY_PAGES_LIMIT = 200  # cap REST work and disclose if coverage fails
STATS_INTERVAL_BETWEEN_PAGES = 0.0  # Shared RestGovernor schedules fills-history calls


def stats_pct(num, denominator):
    if denominator <= 0:
        return '—'
    pct=num / denominator * Decimal(100)
    return f'{pct:.6f}%' if pct and abs(pct)<Decimal('0.01') else f'{pct:.4f}%'


def stats_amount(x, digits=6):
    return f'${x:,.{digits}f}'


def full_stats_snapshot(state, fills, bid, contract_value, *, include_durable_archive=True):
    """Pure calculation over authoritative fill rows.

    Financial statistics are complete only if *every* expected BUY/TP fill
    for every tracked cycle is present. Partial history is NEVER represented
    as full lifetime accounting.
    """
    cycles=(all_stat_cycles(state) if include_durable_archive else
            list(state.get('cycles',{}).values())+list(state.get('archived_cycles',[])))
    grouped={}
    # idempotent even if API pagination boundaries overlap
    seen=set()
    unique_fills=[]
    for f in fills:
        oid=str(f.get('ordId') or '')
        tid=str(f.get('tradeId') or '')
        bill=str(f.get('billId') or '')
        if not oid or not tid or not bill: raise ValueError('Fill missing identifying fields')
        identity=(oid,bill)
        if identity in seen: continue
        seen.add(identity)
        unique_fills.append(f)
        if f.get('fee') in (None,''):
            raise ValueError('Missing actual fee; no estimate allowed')
        if f.get('feeCcy')!='USDT':
            raise ValueError('Non-USDT fee currency; cannot report exact USDT fees')
        if f.get('execType') not in ('M','T'):
            raise ValueError('Unknown Maker/Taker execution type')
        px=D(f['fillPx']); quantity=D(f['fillSz'])
        if px<=0 or quantity<=0: raise ValueError('Invalid fill size/price')
        g=grouped.setdefault(oid,{'qty':Decimal(0),'notional':Decimal(0),
                                   'fee':Decimal(0),'side':f.get('side')})
        if g['side'] != f.get('side'):
            raise ValueError('Mixed BUY/SELL under same ordId')
        g['qty']+=quantity
        g['notional']+=quantity*contract_value*px
        g['fee']+=D(f['fee'])

    closed_count=0
    closed_gain=Decimal(0)
    closed_buy_nominal=Decimal(0)
    open_gain=Decimal(0)
    open_buy_nominal=Decimal(0)
    exit_gain=Decimal(0)
    exit_count=0
    # Presentation-only attribution. Trading accounting totals remain unchanged.
    limit_tp_net=market_tp_net=Decimal(0)
    floating_pnl=Decimal(0)
    expected={}
    known_orders=set()
    for c in cycles:
        buy_id=str(c.get('ordId') or '')
        sell_id=str(c.get('tpOrdId') or '')
        buy_sz=D(c.get('filledBuy'))
        sell_sz=D(c.get('filledTP'))
        if buy_sz:
            if not buy_id: raise ValueError('Missing BUY order ID for filled quantity')
            expected[buy_id]=buy_sz
            known_orders.add(buy_id)
        if sell_sz:
            legs=[r for r in c.get('tpOrders',[]) if D(r.get('filled'))>0]
            if not legs:legs=[{'ordId':sell_id,'filled':str(sell_sz)}]
            if sum((D(r['filled']) for r in legs),Decimal(0))!=sell_sz:
                raise ValueError('Cumulative TP quantity mismatch')
            for r in legs:
                oid=str(r.get('ordId') or '')
                if not oid:raise ValueError('TP leg without ordId')
                accumulate_expected_sell(expected,r)
                known_orders.add(oid)

    missing=[(oid,qty,grouped.get(oid,{}).get('qty',Decimal(0)))
             for oid,qty in expected.items()
             if grouped.get(oid,{}).get('qty',Decimal(0)) != qty]
    if missing:
        return {'complete':False,'missing':missing[:4],
                'fill_orders_expected':len(expected),'fill_orders_seen':len(grouped)}

    maker_volume=taker_volume=maker_fee=taker_fee=Decimal(0)
    for f in unique_fills:
        oid=str(f.get('ordId') or '')
        if oid not in known_orders: continue
        px=D(f['fillPx']); qty=D(f['fillSz']); fee=D(f['fee'])
        if f['execType']=='M': maker_volume+=qty*contract_value*px;maker_fee+=fee
        elif f['execType']=='T': taker_volume+=qty*contract_value*px;taker_fee+=fee

    for c in cycles:
        b=grouped.get(str(c.get('ordId') or ''),None)
        legs=[r for r in c.get('tpOrders',[]) if D(r.get('filled'))>0]
        if not legs and c.get('tpOrdId'):
            legs=[{'ordId':c['tpOrdId'],'filled':c.get('filledTP','0')}]
        sell_parts=[]
        fill_by_id={}
        for f in unique_fills:fill_by_id.setdefault(str(f['ordId']),[]).append(f)
        for r in legs:
            oid=str(r['ordId'])
            if oid not in grouped:continue
            if r.get('source')=='aggregate_exit':
                split=allocated_market_fills(fill_by_id[oid],r['aggregatePlan'],c['cycleId'])
                q=sum((D(f['fillSz']) for f in split),Decimal(0))
                n=sum((D(f['fillPx'])*D(f['fillSz'])*contract_value for f in split),Decimal(0))
                fee=sum((D(f['fee']) for f in split),Decimal(0))
                sell_parts.append({'qty':q,'notional':n,'fee':fee,'source':'aggregate_exit'})
            else:
                sell_parts.append({**grouped[oid], 'source':'limit_tp'})
        if not b or b['qty']<=0: continue
        buy_avg=b['notional']/(b['qty']*contract_value)
        sell_qty=sum((v['qty'] for v in sell_parts),Decimal(0))
        if sell_qty>b['qty']: raise ValueError('TP exceeds BUY')
        sell_nominal=sum((v['notional'] for v in sell_parts),Decimal(0))
        sell_fee=sum((v['fee'] for v in sell_parts),Decimal(0))
        remaining=b['qty']-sell_qty
        if c.get('state')=='CLOSED':
            if remaining: raise ValueError('CLOSED cycle has unclosed position')
            closed_count+=1
            closed_gain += sell_nominal-b['notional']+b['fee']+sell_fee
            # Allocate actual BUY notional/fees pro rata among independently
            # confirmed TP legs. This is a reporting partition, not PnL rewrite.
            for part in sell_parts:
                share=part['qty']/b['qty']
                leg_net=part['notional'] + part['fee'] - (b['notional']-b['fee'])*share
                if part['source']=='aggregate_exit':market_tp_net+=leg_net
                else:limit_tp_net+=leg_net
            closed_buy_nominal+=b['notional']
        elif c.get('state')=='EXITED':
            if remaining:raise ValueError('EXITED partial cycle still has position')
            exit_count+=1
            exit_gain+=sell_nominal-b['notional']+b['fee']+sell_fee
        else:
            # Open-cycle mark-to-market, including realized portion of PARTIAL
            # TP and commissions already paid. Funding excluded.
            floating_pnl += remaining*contract_value*(bid-buy_avg)
            open_gain += (sell_nominal - sell_qty*contract_value*buy_avg +
                          remaining*contract_value*(bid-buy_avg) +
                          b['fee'] + sell_fee)
            open_buy_nominal+=b['notional']

    volume=maker_volume+taker_volume
    total_fees=maker_fee+taker_fee
    if abs(limit_tp_net+market_tp_net-closed_gain)>Decimal('0.00000001'):
        raise ValueError('Closed TP attribution does not reconcile')
    total_gain=closed_gain+open_gain+exit_gain
    total_buy=closed_buy_nominal+open_buy_nominal
    active_buys=sum(c.get('state') in ('BUY_LIVE','BUY_PARTIAL','PLACING') for c in state.get('cycles',{}).values())
    tps=sum(c.get('state') in ('TP_WAIT','TP_PARTIAL') for c in state.get('cycles',{}).values())
    open_contracts=sum((D(c.get('filledBuy'))-D(c.get('filledTP'))
                        for c in state.get('cycles',{}).values()),Decimal(0))
    return dict(complete=True,bid=bid,closed_count=closed_count,
                partial_exit_count=exit_count,partial_exit_net=exit_gain,
                active_buys=active_buys,tps=tps,grid_gain=closed_gain,
                grid_pct=stats_pct(closed_gain,closed_buy_nominal),
                open_gain=open_gain,open_pct=stats_pct(open_gain,open_buy_nominal),
                floating_pnl=floating_pnl,realized_open_components=open_gain-floating_pnl,
                limit_tp_net=limit_tp_net,market_tp_net=market_tp_net,
                total_gain=total_gain,total_pct=stats_pct(total_gain,total_buy),
                total_fees=total_fees,volume=volume,maker_volume=maker_volume,
                taker_volume=taker_volume,maker_fee=maker_fee,taker_fee=taker_fee,
                maker_percent=stats_pct(maker_volume,volume),
                taker_percent=stats_pct(taker_volume,volume),
                maker_fee_pct=stats_pct(-maker_fee,maker_volume),
                taker_fee_pct=stats_pct(-taker_fee,taker_volume),
                open_contracts=open_contracts,open_base=open_contracts*contract_value,
                account_closed=state.get('completed',0),
                expected_orders=len(expected))


def expected_filled_orders(state, *, include_archive=True):
    cycles=all_stat_cycles(state) if include_archive else list(state.get('cycles',{}).values()) + list(state.get('archived_cycles',[]))
    expected={}
    for c in cycles:
        oid=str(c.get('ordId') or '')
        if D(c.get('filledBuy'))>0 and oid:
            expected[oid]=D(c['filledBuy'])
        if D(c.get('filledTP'))>0:
            legs=[r for r in c.get('tpOrders',[]) if D(r.get('filled'))>0]
            if not legs:legs=[{'ordId':c.get('tpOrdId'),'filled':c.get('filledTP')}]
            for r in legs:
                if r.get('ordId'):accumulate_expected_sell(expected,r)
    return expected


def stats_exchange_fills(state):
    # Mandatory archive bootstrap only once, then fetch missing order IDs.
    return FILL_STORE.ensure_expected(expected_filled_orders(state))


# OKX performance report: fixed capital basis from initial upper bound or creation price.
# Revenue/fees/execution remain actual OKX fills, not LIMIT-price estimates.
RUNTIME_STATS = ROOT / 'state' / 'runtime_stats.json'
GRID_REFERENCE = ROOT / 'state' / 'grid_reference.json'

def _first_anchor(fills):
    # Capital is frozen at bot creation: upper bound if set, otherwise
    # the market price captured at setup. Never use a later moving BID.
    if GRID_REFERENCE.exists():
        r=json.loads(GRID_REFERENCE.read_text())
        if D(r['top_buy']) != CAPITAL_REFERENCE:
            raise ValueError('Frozen capital reference conflicts with config')
        return float(r['started_at']),D(r['top_buy'])
    if CFG.get('created_at'):
        state_created=json.loads(DATA.read_text()).get('started_at') if DATA.exists() else None
        anchor=float(state_created or CFG['created_at'])
        atomic_json(GRID_REFERENCE,{'started_at':anchor,
                    'top_buy':str(CAPITAL_REFERENCE),'grid_step':str(STEP),
                    'max_grids':CAP,'upper':str(UPPER)})
        return anchor,CAPITAL_REFERENCE
    # Prefer persistent order-placement events, which predate fill data.
    oldest_start = None
    oldest_buy = None
    if EVENTS.exists():
        with EVENTS.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    ev=json.loads(line)
                    dt=datetime.fromisoformat(ev['ts'].replace('Z','+00:00'))
                    if dt.tzinfo is None: continue
                    stamp=dt.astimezone(timezone.utc).timestamp()
                    if ev.get('event')=='START' and ev.get('live') is True:
                        oldest_start=min(oldest_start,stamp) if oldest_start else stamp
                    if ev.get('event')=='BUY_PLACED' and ev.get('price'):
                        x=(stamp,D(ev['price']))
                        if oldest_buy is None or x[0]<oldest_buy[0]: oldest_buy=x
                except (ValueError,KeyError,TypeError,json.JSONDecodeError):
                    continue
    first_fill=None
    for f in fills:
        ts=f.get('ts') or f.get('fillTime')
        if ts and str(ts).isdigit():
            t=int(ts)/1000
            first_fill=min(first_fill,t) if first_fill is not None else t
    launched=oldest_start or (oldest_buy[0] if oldest_buy else first_fill)
    top=oldest_buy[1] if oldest_buy else None
    if launched is not None and top is not None:
        if top<=0 or top%STEP:
            raise ValueError('Invalid initial grid price; cannot fix 1x reference')
        atomic_json(GRID_REFERENCE,{'started_at':launched,'top_buy':str(top),
                                    'grid_step':str(STEP),'max_grids':CAP})
    return launched,top


def _clock(seconds):
    seconds=max(0,int(seconds))
    days,rem=divmod(seconds,86400)
    hours,rem=divmod(rem,3600)
    minutes,secs=divmod(rem,60)
    return (f'{days}д {hours}ч {minutes}м {secs}с' if days else
            f'{hours}ч {minutes}м {secs}с')


def runtime_heartbeat():
    # Accurate uptime tracking begins ONLY with .
    # Gaps >60s are downtime, not falsely counted as running time.
    now=time.time()
    if RUNTIME_STATS.exists():
        d=json.loads(RUNTIME_STATS.read_text())
    else:
        d={'since':now,'last':now,'seconds':0.0}
    delta=now-float(d.get('last',now))
    if 0 <= delta <= 60:
        d['seconds']=float(d.get('seconds',0))+delta
    d['last']=now
    atomic_json(RUNTIME_STATS,d)


def runtime_pct(now):
    if not RUNTIME_STATS.exists(): return '—'
    d=json.loads(RUNTIME_STATS.read_text())
    elapsed=max(0,now-float(d['since']))
    if elapsed<60: return '—'
    running=max(0,float(d.get('seconds',0)))
    return f'{min(100.,100.*running/elapsed):.2f}%'


def _last_24h_profit(st,fills, contract_value, now_ms):
    """Completed-cycle realized net, using per-cycle shares of aggregate SELL fills.

    A single reduce-only MARKET can close several parent BUY cycles. Its full
    exchange fills must NEVER be counted once per parent in the 24h report.
    """
    grouped={}
    seen=set()
    for f in fills:
        oid=str(f.get('ordId') or '')
        bill=str(f.get('billId') or '')
        if not oid or not bill: raise ValueError('Missing fill identity')
        k=(oid,bill)
        if k in seen:continue
        seen.add(k)
        ts=f.get('ts') or f.get('fillTime')
        if not ts or not str(ts).isdigit():
            raise ValueError('OKX fills missing timestamp for 24h profit')
        grouped.setdefault(oid,[]).append(f)
    cutoff=now_ms-86400000
    total=Decimal(0)
    for c in all_stat_cycles(st):
        if c.get('state')!='CLOSED': continue
        bidid=str(c.get('ordId') or '')
        buys=grouped.get(bidid,[])
        legs=[r for r in c.get('tpOrders',[]) if D(r.get('filled'))>0]
        if not legs and c.get('tpOrdId'):
            legs=[{'ordId':c['tpOrdId'],'filled':c.get('filledTP','0')}]
        sells=[]
        for leg in legs:
            rows=grouped.get(str(leg.get('ordId') or ''),[])
            if leg.get('source')=='aggregate_exit':
                # Partition the shared MARKET fills exactly as the PnL ledger
                # does: by trade ID, fill quantity, fill price and fee.
                rows=allocated_market_fills(rows,leg['aggregatePlan'],c['cycleId'])
            sells.extend(rows)
        if not buys or not sells:raise ValueError('Closed-cycle fills missing')
        if max(int(f.get('ts') or f.get('fillTime')) for f in sells)>=cutoff:
            sell_net=sum((D(f['fillPx'])*D(f['fillSz'])*contract_value+D(f['fee'])
                          for f in sells),Decimal(0))
            buy_cost=sum((D(f['fillPx'])*D(f['fillSz'])*contract_value-D(f['fee'])
                          for f in buys),Decimal(0))
            total+=sell_net-buy_cost
    return total


def format_okx_report(st,fills,stats,now=None):
    now = time.time() if now is None else float(now)
    launch,top=_first_anchor(fills)
    elapsed=now-launch if launch is not None else None
    # The grid is the original reference range, NOT the current count of
    # virtual/real TP. The 1x capital reference is capacity times average grid price.
    if top is not None:
        bottom=top-STEP*(CAP-1)
        if bottom<=0: raise ValueError('Invalid reference-grid lower bound')
        average=(top+bottom)/2
        max_base=CAP*QTY*CONTRACT_VALUE
        capital=max_base*average
    else:
        bottom=average=max_base=capital=None
    if capital is not None:
        past24=_last_24h_profit(st,fills,CONTRACT_VALUE,int(now*1000))
    else:
        past24=None
    days=Decimal(str(elapsed))/Decimal('86400') if elapsed and elapsed>0 else None
    perday=stats['grid_gain']/days if days else None
    per30=perday*30 if perday is not None else None
    peryear=perday*365 if perday is not None else None
    rate=lambda v: stats_pct(v,capital) if v is not None and capital else '—'
    raw=lambda v,p=4: (f'${D(v).quantize(Decimal(1).scaleb(-p),rounding=ROUND_HALF_UP):,.{p}f}'
                        if v is not None else '—')
    price_digits=max(2, -PRICE_STEP.normalize().as_tuple().exponent)
    plus=lambda v,p=4: money_usdt(v,p) if v is not None else '—'
    datestr=(datetime.fromtimestamp(launch,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
             if launch is not None else '—')
    nowstr=datetime.fromtimestamp(now,timezone.utc).strftime('%H:%M:%S UTC')
    real=len(real_tp_active(st))
    virtual=len(virtual_cycles(st))
    items=[
        f'[{nowstr}] 📊 OKX {COIN} GRID STATISTICS',
        '============================================',
        f'Запущен:                   {datestr}',
        f'Работает:                  {_clock(elapsed) if elapsed is not None else "—"}',
        f'Uptime:                    {runtime_pct(now)}',
        f'Текущий BID:               {raw(stats["bid"],price_digits)}',
        f'Закрытых циклов:           {stats["closed_count"]}',
        f'Открыто ордеров:           {stats["active_buys"]+real}',
        f'  BUY LIMIT:               {stats["active_buys"]}',
        f'  ждут TP:                 {real}',
        f'  виртуальных TP:          {virtual}',
        f'GRID PROFIT:               {plus(stats["grid_gain"],8)}',
        f'Частичных выходов MARKET:  {stats["partial_exit_count"]}',
        f'PnL частичных выходов:     {plus(stats["partial_exit_net"],8)}',
        f'Прибыль за последние 24ч: {plus(past24)}   ({rate(past24)})',
        'Средний темп:',
        f'  сутки:                   {plus(perday)}   ({rate(perday)})',
        f'  30 дней:                 {plus(per30,2)}   ({rate(per30)})',
        f'  год:                     {plus(peryear,2)}   ({rate(peryear)})',
        f'Расчётный капитал для 1x: {raw(capital,2) if capital is not None else "—"}',
        f'  max position:            {max_base:.8f} {COIN}' if max_base is not None else '  max position:            —',
        f'  сетка:                   {raw(top,price_digits)} → {raw(bottom,price_digits)}' if top is not None else '  сетка:                   —',
        f'  средняя цена:            {raw(average,price_digits)}' if average is not None else '  средняя цена:            —',
        f'Уплачено комиссий:        {raw(-stats["total_fees"],8)}',
        f'Общий объём:              {raw(stats["volume"],4)}',
        f'  Maker:                   {raw(stats["maker_volume"],4)}',
        f'  Taker:                   {raw(stats["taker_volume"],4)}',
        f'Открытая позиция:         {stats["open_base"]:.8f} {COIN}',
        f'Нереализованный PnL:      {plus(stats["open_gain"],8)}',
        '--------------------------------------------',
        f'ИТОГОВЫЙ PnL:             {plus(stats["total_gain"],8)} ({rate(stats["total_gain"])})',
        '--------------------------------------------',
        '✅ Все закрытые циклы сопоставлены.',
    ]
    if BOT_DD_DATA.exists():
        try:
            dd=json.loads(BOT_DD_DATA.read_text())
            if dd.get('schema')==1 and dd.get('instrument')==INST:
                worst=-D(dd['max_drawdown_usdt'])
                at=datetime.fromtimestamp(float(dd['max_drawdown_at']),timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
                items[-1:-1] = [
                    f'МАКС. ПРОСАДКА БОТА:      {plus(worst,8)} ({rate(worst)})',
                    f'  Зафиксирована:          {at}',
                ]
        except (OSError,ValueError,KeyError,TypeError):
            pass
    return '\n'.join(items)


def performance_snapshot_v0724(st, fills, stats, now):
    """Read-only presentation payload from exactly the audited fill snapshot.

    This does not change PnL and deliberately does not mark unverified
    partially completed cycles as realized GRID PROFIT.
    """
    launched,ref=_first_anchor(fills)
    bottom=average=max_base=capital=None
    if ref is not None:
        bottom=ref-STEP*(CAP-1)
        if bottom<=0:raise ValueError('Invalid reference grid lower bound')
        average=(ref+bottom)/2
        max_base=CAP*QTY*CONTRACT_VALUE
        capital=max_base*average
    elapsed=now-launched if launched is not None else None
    days=Decimal(str(elapsed))/Decimal(86400) if elapsed and elapsed>0 else None
    daily=stats['grid_gain']/days if days else None
    past24=_last_24h_profit(st,fills,CONTRACT_VALUE,int(now*1000)) if capital is not None else None
    open_top=[]
    for c in st.get('cycles',{}).values():
        if c.get('state') not in ('TP_WAIT','TP_PARTIAL','VIRTUAL_TP'):continue
        if D(c.get('filledBuy'))>D(c.get('filledTP')):
            px=D(c.get('buyPx'))
            if px>0:open_top.append(px)
    payload={
        'schema':1,'bid':str(stats['bid']),
        'top_open_buy':str(max(open_top)) if open_top else None,
        'open_base':str(stats['open_base']),
        'open_nominal':str(stats['open_base']*stats['bid']),
        'floating_pnl':str(stats['floating_pnl']),
        'grid_profit':str(stats['grid_gain']),
        'closed_count':stats['closed_count'],
        'limit_tp_net':str(stats['limit_tp_net']),
        'market_tp_net':str(stats['market_tp_net']),
        'partial_exit_count':stats['partial_exit_count'],
        'partial_exit_net':str(stats['partial_exit_net']),
        'realized_open_components':str(stats['realized_open_components']),
        'total_pnl':str(stats['total_gain']),
        'tp_waiting':len(real_tp_active(st)),  # legacy: real only
        'tp_waiting_all':len(real_tp_active(st))+len(virtual_cycles(st)),
        'last_24h':str(past24) if past24 is not None else None,
        'per_day':str(daily) if daily is not None else None,
        'per_30d':str(daily*30) if daily is not None else None,
        'per_year':str(daily*365) if daily is not None else None,
        'capital':str(capital) if capital is not None else None,
        'max_base':str(max_base) if max_base is not None else None,
        'grid_reference_top':str(ref) if ref is not None else None,
        'grid_bottom':str(bottom) if bottom is not None else None,
        'grid_average':str(average) if average is not None else None,
        'launch_at':launched,'uptime':runtime_pct(now),
        'volume':str(stats['volume']),
        'maker_volume':str(stats['maker_volume']),
        'taker_volume':str(stats['taker_volume']),
        'paid_fees':str(-stats['total_fees']),
    }
    return payload


def print_full_stats(pnl=None, *, require_complete=False):
    """One-shot report / 10-minute background print; NEVER touches orders."""
    if not DATA.exists():
        print('📊 GRID STATISTICS: торговое состояние ещё не создано',flush=True)
        return
    st=json.loads(DATA.read_text())
    if st.get('instrument')!=INST:
        raise ValueError('Statistics file is for another instrument')
    bid=A.ticker()
    fills=stats_exchange_fills(st)
    d=full_stats_snapshot(st,fills,bid,CONTRACT_VALUE)
    if d.get('complete') and pnl is not None:
        recovered=pnl.ingest_history(st,fills)
        if recovered:
            print(f'✅ Финансовый журнал дополнен: {recovered} закрытых TP',flush=True)
        ledger_total=pnl.total()
        if abs(ledger_total-d['grid_gain'])>Decimal('0.00000001'):
            if require_complete:
                raise ExchangeUnresolved(f'PnL ledger discrepancy: {ledger_total} != {d["grid_gain"]}')
            print(f'⚠️ Расхождение PnL журнала и отчёта: {ledger_total} / {d["grid_gain"]}',flush=True)
    if not d['complete']:
        print('⚠️ Статистика неполная: часть исполнений отсутствует',flush=True)
        print('Исполнения для ордеров:',d['fill_orders_seen'],'/',d['fill_orders_expected'],flush=True)
        print('Примеры расхождений:',d['missing'],flush=True)
        print('Финансовые суммы не выводятся.',flush=True)
        if require_complete:
            raise ExchangeUnresolved('Incomplete statistics in mandatory pre-LIVE audit')
        return
    # A fill may have arrived while background history was paginating.
    # Never persist a false drawdown from an outdated open-position inventory.
    if LIVE and financial_signature(json.loads(DATA.read_text()))!=financial_signature(st):
        print('⏳ Статистика: исполнение изменилось во время REST-сверки; ждём нового полного снимка',flush=True)
        return
    # Publish a complete, immutable-format report for the external manager.
    # Crucially, only the active trading process publishes the snapshot.
    # AUDIT / STATS_ONLY never overwrite a live bot's snapshot.
    now=time.time()
    if LIVE:
        # Existing complete PnL uses authoritative BUY/SELL fills and fees.
        # Only the running engine changes the monitoring baseline.
        # DD sampled independently from in-memory state every 5s.
        atomic_json(STATS_FINANCIAL_ANCHOR, {'schema':1,'instrument':INST,
            'signature':financial_signature(st),'total_pnl_usdt':str(d['total_gain']),
            'open_contracts':str(d['open_contracts']),'bid':str(bid),'at':now})
    report=format_okx_report(st,fills,d,now=now)
    detail=performance_snapshot_v0724(st,fills,d,now)
    if LIVE:
        atomic_json(STATS_FILE, {
            'schema_version': 1,
            'complete': True,
            'instrument': INST,
            'bot_version': VER,
            'generated_at': datetime.fromtimestamp(now,timezone.utc).isoformat(),
            'generated_at_unix': now,
            'performance_v0724': detail,
            'report': report,
        })
    print('\n'+report+'\n',flush=True)


def trim_verified_fills_for_snapshot(rows, expected):
    """Include exact fills existing up to confirmed order quantities at snapshot.

    A later fill on the same BUY may reach SQLite while an earlier PnL sample
    is being processed. Preserve the earlier sample by taking complete real
    individual executions up to the recorded quantity, never rounding.
    """
    grouped={}
    for f in rows:grouped.setdefault(str(f['ordId']),[]).append(f)
    kept=[]
    for oid,need in expected.items():
        total=Decimal(0)
        for f in sorted(grouped.get(str(oid),[]),
                        key=lambda r:(int(r.get('ts') or 0),str(r.get('billId') or ''))):
            q=D(f['fillSz'])
            if total+q>need:break
            kept.append(f)
            total+=q
            if total==need:break
        if total!=need:
            return None
    return kept


class BotDrawdownSampler:
    """Every ~5s: estimated realized net + open-position mark to cached BID.

    Not an independent accounting system. Reads the in-memory PnL ledger and
    cycle state that the trading loop already has. A BUY's limit price is an
    approximate execution price for DD purposes; momentary partial fills can
    introduce small errors, as deliberately accepted by the operator.

    No REST, no extra GET, no fills history, no anchor/fingerprint gating,
    Bounded async writer persists the sample; errors never affect orders.
    """
    def __init__(self, market, pnl):
        self.market = market
        self.pnl = pnl
        self.last_sample = 0.0
        self.last_error_notice = 0.0
        self.samples = queue.Queue(maxsize=120)  # bounded, latest seconds of diagnostics
        self.dropped = 0
        self._thread = threading.Thread(target=self._write_loop,
                                         daemon=True,name='okx-drawdown-writer')
        self._thread.start()

    def _write_loop(self):
        while True:
            total, at, bid = self.samples.get()
            try:
                record_bot_drawdown(total, now=at,
                                    source='local_realized_plus_bid', bid=bid)
            except Exception as exc:
                clock=time.monotonic()
                if clock-self.last_error_notice>=60:
                    self.last_error_notice=clock
                    try:
                        log('BOT_DRAWDOWN_SAMPLE_FAILED',
                            error=f'{type(exc).__name__}: {str(exc)[:120]}')
                    except Exception:
                        pass
            finally:
                self.samples.task_done()

    def submit_reconciled(self, st):
        clock = time.monotonic()
        if clock - self.last_sample < BOT_DD_SAMPLE_INTERVAL:
            return
        self.last_sample = clock
        try:
            bid_value = self.market.cached_bid()  # Existing public WS cache ONLY
            if bid_value is None:
                return
            bid = D(bid_value)
            if bid <= 0 or not bid.is_finite():
                return
            realized = self.pnl.total_all()  # In-memory, no SQLite or API call
            floating = Decimal(0)
            for cycle in st.get('cycles', {}).values():
                # Ignore closed/cancelled cycles; realized net is already in pnl.
                if cycle.get('state') in ('CLOSED', 'CANCELED', 'EXITED'):
                    continue
                open_contracts = D(cycle.get('filledBuy')) - D(cycle.get('filledTP'))
                if open_contracts < 0:
                    return  # incoherent transient state: don't overwrite prior DD
                if not open_contracts:
                    continue
                # Cycle BUY LIMIT price is a good enough entry approximation.
                # It is already present in the local trading state.
                entry = D(cycle.get('buyPx'))
                if entry <= 0 or not entry.is_finite():
                    return
                floating += open_contracts * CONTRACT_VALUE * (bid - entry)
            total = D(realized) + floating
            if not total.is_finite():
                return
            try:
                self.samples.put_nowait((total, time.time(), bid))
            except queue.Full:
                # Diagnostic data may drop; trading must NEVER wait for fsync.
                self.dropped += 1
        except Exception as exc:
            # A diagnostic error must never interrupt trading.
            if clock - self.last_error_notice >= 60:
                self.last_error_notice = clock
                try:
                    log('BOT_DRAWDOWN_SAMPLE_FAILED',
                        error=f'{type(exc).__name__}: {str(exc)[:120]}')
                except Exception:
                    pass

class StatsWorker(threading.Thread):
    """Background verified fills; refresh promptly after executions, not every tick."""
    def __init__(self,pnl):
        super().__init__(daemon=True)
        self.pnl=pnl

    def run(self):
        last=0.0
        while True:
            delay=5
            now=time.monotonic()
            try:
                due=(not last or now-last>=STATS_INTERVAL)
                if not due and now-last>=60 and DATA.exists() and STATS_FINANCIAL_ANCHOR.exists():
                    current=json.loads(DATA.read_text())
                    anchor=json.loads(STATS_FINANCIAL_ANCHOR.read_text())
                    due=financial_signature(current)!=anchor.get('signature')
                if due:
                    last=now
                    print_full_stats(self.pnl)
            except RateLimitedError:
                delay=60
                print('⏳ OKX ограничил историю сделок; статистика повторится через 60с',flush=True)
            except Exception as e:
                print('⚠️ Статистика недоступна:',type(e).__name__,str(e)[:180],flush=True)
            time.sleep(delay)


def main():
    if TERMINAL_ONLY:
        # Explicit historical/backfill mode: never acquires the trading lock,
        # never imports LIVE order entrypoints, and NEVER submits POSTs.
        from okx_terminal_accounting import finalize
        result=finalize(sys.modules[__name__])
        print(result['report'],flush=True)
        return
    if STATS_ONLY:
        print_full_stats()  # read-only, no lock, safe while LIVE bot runs
        return
    lock=acquire_lock()
    try:
        print(f'=== OKX GRID v{VER} ===',
              f'LIVE {TARGET} BUY' if LIVE else ('READ ONLY AUDIT' if AUDIT else 'READ ONLY'),flush=True)
        # At startup (including systemd restart after network outage), safe
        # GET/API rate failures must NOT exhaust systemd StartLimitBurst.
        # Read-only upgrade audits deliberately fail fast and roll back.
        startup_warning=0.0
        if LIVE:prepare_account_safety()
        while True:
            try:
                a=A.req('GET','/api/v5/account/config')
                if not a or a[0].get('posMode')!='net_mode': die('Only net_mode supported')
                info=A.instrument()
                value=D(info.get('ctVal'))
                if info.get('ctValCcy')!=COIN or value!=CONTRACT_VALUE:
                    die('Unexpected contract value. STOP for safe PnL calculation.')
                break
            except (ExchangeTemporary,ExchangeUnresolved) as exc:
                if AUDIT:raise
                if time.monotonic()-startup_warning>=30:
                    log('STARTUP_SYNC_WAIT',error=f'{type(exc).__name__}: {str(exc)[:160]}')
                    startup_warning=time.monotonic()
                time.sleep(5)
        st=ensure_state()
        if st.get('terminal_mode') and not AUDIT:
            die('Bot retired: never restart this instance, create a clone')
        pnl=PnLLedger(value)
        # The fill archive refuses BOTH read-only upgrade audit and LIVE start until the
        # complete historic fill archive has been durably imported/verified.
        # Never continue trading with a known-incomplete lifetime ledger.
        FILL_STORE.bootstrap(st)
        pnl.initial_ids={pnl.identity(c) for c in pnl.all_cycles(st)}
        log('START',live=LIVE,audit=AUDIT,tracked=count(st),
            realized_usdt=str(pnl.total()))
        if AUDIT:
            audit_once(st)
            pnl.work(st,max_cycles=1,historical=True)
            print('💰',pnl.status(st),flush=True)
            print_full_stats(pnl,require_complete=True)
            return
        watcher=WSWatcher();watcher.start()
        market=PublicBidWatcher(shared_wake=watcher.wake);market.start()
        pnl_worker=PnLWorker(pnl);pnl_worker.start()
        stats_worker=StatsWorker(pnl);stats_worker.start()
        dd_worker=BotDrawdownSampler(market,pnl)
        live_loop(st,watcher,market,pnl,pnl_worker,dd_worker)
        if st.get('terminal_mode') in ('PANIC_DONE','RETIRED'):
            # Trading is ALREADY terminal. Only GETs / fill archive writes follow.
            # Never block cancellation or market close for financial reporting.
            from okx_terminal_accounting import finalize
            try:
                result=finalize(sys.modules[__name__],max_wait_seconds=60)
                print('✅ TERMINAL ACCOUNTING VERIFIED\n'+result['report'],flush=True)
            except Exception as exc:
                log('BOT_TERMINAL_STATS_PENDING',error=f'{type(exc).__name__}: {str(exc)[:150]}')
                print('⚠️ Final accounting pending: GET-only retry required',flush=True)
    finally:
        os.close(lock)

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt: print('\nStopped (open orders remain on OKX).',flush=True)
    except Exception as exc:
        print('⛔ STOP:',str(exc),file=sys.stderr,flush=True)
        sys.exit(1)
