#!/usr/bin/env python3
"""Read-only Telegram control-room for OKX bot instances.

One process serves both instruments; it never loads exchange API keys, never
sends/cancels an exchange order, and never writes trading state/control files.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in a mode-0600 env file.
"""
import json
import os
from pathlib import Path
import sys
# Telegram lives in /root/okx; shared, versionless modules live separately.
_SHARED_CODE = Path('/opt/okx-grid-live')
if not (_SHARED_CODE / 'okx_paths.py').is_file():
    raise RuntimeError('OKX shared runtime missing: ' + str(_SHARED_CODE))
sys.path.insert(0, str(_SHARED_CODE))
from okx_paths import state_file, events_file
sys.path.insert(0, str(Path(__file__).resolve().parent))
import re
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from urllib.request import Request, urlopen, getproxies
import http.client
import ssl
import html
from okx_telegram_views import status_one, latency_report, read_json, LINE, verified_snapshot_bid, number
from okx_telegram_events import (one, event_alert, market_event, market_process, zero_buy_scan, observer_fault_scan, gate_alert_scan)
from okx_telegram_followups import partial_scan, panic_scan
from okx_telegram_delivery import persist as durable_save, drain as alert_drain
from okx_telegram_health import begin_systemd_snapshot, systemd_active, service_check, engine_heartbeat_check, observers_check, panic_ack_check, position_reader_health_check, reader_bypass_check

BASE = Path(os.environ.get('OKXBOT_BASE_DIR','/root/okx')).resolve()
REGISTRY = Path(os.environ.get('OKXBOT_REGISTRY',str(BASE/'.okxbot_registry.json')))
SYSTEMD_DIR = Path(os.environ.get('OKXBOT_SYSTEMD_DIR','/etc/systemd/system'))
BOT_DIRS = {}
SERVICES = {}
_DISCOVERY_LAST = 0.0


def _inside_base(path):
    try:
        p=Path(path).resolve()
        return p != BASE and BASE in p.parents and (p/'config.json').is_file()
    except (OSError,ValueError):return False


def discover_bots():
    """All registry bots + unregistered grid systemd units. Never reads .env."""
    found={}
    try:
        records=json.loads(REGISTRY.read_text()) if REGISTRY.is_file() else {}
        if isinstance(records,dict):
            for display,entry in records.items():
                if not isinstance(entry,dict):continue
                root=entry.get('path','')
                unit=str(entry.get('service',''))
                if (_inside_base(root) and re.fullmatch(r'okx-grid-[a-z0-9-]+',unit)):
                    key=Path(root).name
                    found[key]=(str(Path(root).resolve()),unit)
    except (OSError,ValueError,TypeError):pass
    for file in SYSTEMD_DIR.glob('okx-grid-*.service'):
        try:
            unit=file.stem
            if not re.fullmatch(r'okx-grid-[a-z0-9-]+',unit):continue
            text=file.read_text(encoding='utf-8')
            m=re.search(r'(?m)^WorkingDirectory=(.+)$',text)
            if not m or not _inside_base(m.group(1).strip()):continue
            root=Path(m.group(1).strip()).resolve()
            found.setdefault(root.name,(str(root),unit))
        except (OSError,ValueError):continue
    return dict(sorted(found.items()))


def _confirmed_archived(name, root):
    # Only exclude verified completed terminal accounting; never a stopped LIVE bot.

    # Malformed/incomplete archival data => keep monitoring.
    # Do not use systemd inactive to decide lifecycle.
    try:
        p = Path(root) / 'state/final_stats.json'
        if not p.is_file() or time.time() - p.stat().st_mtime < 3600:
            return False
        f = json.loads(p.read_text())
        cfg = json.loads((Path(root) / 'config.json').read_text())
        return (isinstance(f, dict) and f.get('schema_version') == 1 and
                f.get('complete') is True and
                f.get('mode') in ('PANIC_DONE', 'RETIRED') and
                f.get('instrument') == cfg.get('instrument') and
                (Path(root) / 'state/control_ack.json').is_file())
    except (OSError, ValueError, TypeError):
        return False


def refresh_bots(*,force=False):
    global _DISCOVERY_LAST
    now=time.monotonic()
    if not force and now-_DISCOVERY_LAST<15:return
    _DISCOVERY_LAST=now
    discovered=discover_bots()
    BOT_DIRS.clear();SERVICES.clear()
    for key,(root,unit) in discovered.items():
        if _confirmed_archived(key, root):
            continue
        BOT_DIRS[key]=root;SERVICES[key]=unit


def _active_service(name):
    try:
        return systemd_active(SERVICES[name])
    except KeyError:
        return False

def _terminal_report(name):
    root=Path(BOT_DIRS[name]);path=root/'state/final_stats.json'
    if not path.is_file():return None
    data=json.loads(path.read_text())
    instrument=json.loads((root/'config.json').read_text()).get('instrument')
    if (data.get('schema_version')!=1 or data.get('complete') is not True
        or data.get('instrument')!=instrument
        or data.get('mode') not in ('PANIC_DONE','RETIRED')):
        raise ValueError('Invalid terminal report')
    return f'🔴 {name} · {data["mode"]}\n'+data['report']

SHARED_SOFT = Path('/var/lib/okxbot/account_soft_stop.json')
STATE_PATH = Path('/var/lib/okxbot/telegram_monitor_state.json')
ALERT_EVENTS = {
    'ACCOUNT_AUTO_SOFT_STOP':'🚨 AUTO SOFT STOP',
    'AUTO_SOFT_MODE_LATCHED':'🛑 SOFT STOP confirmed',
    'RECONCILIATION_LONG_WAIT':'⚠️ Reconciliation stalled',
    'TRADING_LOOP_LONG_PASS':'⚠️ Long trading pass',
    'BUY_POST_UNRESOLVED':'⚠️ BUY POST unresolved',
    'TP_POST_UNRESOLVED':'⚠️ TP POST unresolved',
    'KNOWN_TP_UNRESOLVED':'⚠️ Known TP unavailable',
    'TP_RESTORE_REJECTED':'🔴 TP restore rejected',
    'ATTACHED_TP_REJECTED':'🚨 Attached TP failed on OKX',
    'TP_PRICE_LIMIT_UNAVAILABLE':'⚠️ TP price limit unavailable',
    'TP_FAILURE_BUY_HOLD':'🚨 BUY held: attached TP failure',
    'PANIC_UNRESOLVED':'🚨 PANIC outcome unresolved',
    'BUY_POST_EXPIRED_ABSENT':'✅ Expired BUY confirmed absent',
    'TP_POST_EXPIRED_ABSENT':'✅ Expired TP confirmed absent',
    'RECONCILIATION_RECOVERED':'✅ Reconciliation recovered',
    'STARTUP_SYNC_WAIT':'⚠️ Startup syncing',
    'FEE_CHECK_PAUSED_BUYS':'⚠️ BUY paused: fee check',
    'ACCOUNT_POSITIONS_GATE_RECOVERED':'⚠️ Position limiter recovered',
    'ACCOUNT_POSITIONS_GATE_DEGRADED':'🚨 Position limiter fallback active',
    'AGG_MARKET_INTENT':'⚠️ Market SELL: intent saved',
    'AGG_MARKET_ACK':'📤 Market SELL: accepted by OKX',
    'AGG_MARKET_REJECTED':'🚨 Market SELL: rejected',
    'AGG_MARKET_TERMINAL':'💰 Market SELL: final result',
    'BOT_TERMINAL_FINALIZED':'🏁 Final bot PnL verified',
    'BOT_TERMINAL_STATS_PENDING':'⚠️ Final bot accounting pending',
    'EMERGENCY_POSITIONS_GATE_BLOCKED':'🚨 Emergency positions gate blocked',
    'EMERGENCY_POSITIONS_READ':'🚨 Emergency positions read',
    'COVERAGE_RECOVERY_WAIT':'⚠️ TP coverage hold',
}
RATE_LIMIT_SECONDS = 600
MARKET_ALERTS = frozenset({'AGG_MARKET_INTENT','AGG_MARKET_ACK',
                            'AGG_MARKET_REJECTED','AGG_MARKET_TERMINAL',
                            'BOT_TERMINAL_FINALIZED','BOT_TERMINAL_STATS_PENDING'})
POLL_SECONDS = 5
TZ = ZoneInfo('America/Santiago')


def read_env(path):
    env={}
    if path.stat().st_mode & 0o077:
        raise RuntimeError('Telegram config must have permissions 0600')
    for line in path.read_text().splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            key,val=line.split('=',1)
            env[key.strip()]=val.strip().strip('"').strip("'")
    token=env.get('TELEGRAM_BOT_TOKEN','')
    chat=env.get('TELEGRAM_CHAT_ID','')
    if not token or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{10,}',token):
        raise RuntimeError('Missing/invalid TELEGRAM_BOT_TOKEN')
    if not re.fullmatch(r'-?[0-9]+',chat):
        raise RuntimeError('Missing/invalid TELEGRAM_CHAT_ID')
    return token,chat


class TelegramPermanentError(RuntimeError):
    telegram_permanent=True


class Telegram:
    def __init__(self,token,chat):
        self.base='https://api.telegram.org/bot'+token+'/'
        self.path='/bot'+token+'/'
        self.chat=str(chat)
        self._http=None
        self._http_last_used=0.0
        # urllib respects HTTP[S]_PROXY. Preserve that behavior on VPSes
        # configured for a proxy; otherwise reuse a verified TLS connection.
        self._proxy=bool(getproxies().get('https'))

    def _close_http(self):
        conn=self._http
        self._http=None
        self._http_last_used=0.0
        if conn is not None:
            try: conn.close()
            except OSError: pass

    def call(self,method,params):
        payload=json.dumps(params).encode()
        # getUpdates is read-only and polled frequently. Give it an isolated,
        # one-request TLS connection so a stale keep-alive cannot delay
        # critical alert delivery. Bounded timeout prevents 18s monitor stalls.
        is_poll = method == 'getUpdates'
        timeout = 5 if is_poll else 18
        if self._proxy:
            # Respect the configured proxy; never bypass it silently.
            from urllib.error import HTTPError
            req=Request(self.base+method,data=payload,headers={'Content-Type':'application/json'})
            try:
                with urlopen(req,timeout=timeout) as response:
                    result=json.loads(response.read().decode())
            except HTTPError as exc:
                if exc.code in (400,403):
                    raise TelegramPermanentError(f'Telegram permanent HTTP {exc.code}') from exc
                raise
        else:
            if is_poll:
                conn=http.client.HTTPSConnection(
                    'api.telegram.org',timeout=timeout,context=ssl.create_default_context())
            else:
                # Alert sendMessage is never automatically resent on uncertain
                # outcome. Refresh idle connections before the *first* send.
                if (self._http is not None and self._http_last_used
                        and time.monotonic()-self._http_last_used>15):
                    self._close_http()
                if self._http is None:
                    self._http=http.client.HTTPSConnection(
                        'api.telegram.org',timeout=timeout,context=ssl.create_default_context())
                conn=self._http
            try:
                conn.request('POST',self.path+method,body=payload,
                             headers={'Content-Type':'application/json'})
                response=conn.getresponse()
                status=response.status
                raw=response.read()
                if status in (400,403):
                    raise TelegramPermanentError(f'Telegram permanent HTTP {status}')
                if status != 200:
                    raise RuntimeError('Telegram transient HTTP '+str(status))
                result=json.loads(raw.decode())
                if not is_poll:
                    if response.will_close:
                        self._close_http()
                    else:
                        self._http_last_used=time.monotonic()
            except BaseException:
                if not is_poll:
                    self._close_http()
                raise
            finally:
                if is_poll:
                    try:conn.close()
                    except OSError:pass
        if not isinstance(result,dict) or not result.get('ok'):
            raise RuntimeError('Telegram '+method+' returned not-ok')
        return result.get('result')
    def send(self,text,parse_mode=None):
        payload={'chat_id':self.chat,'text':text[:3900]}
        if parse_mode: payload['parse_mode']=parse_mode
        return self.call('sendMessage',payload)


def load_state():
    try:
        st=json.loads(STATE_PATH.read_text())
        if not isinstance(st,dict) or st.get('schema')!=1:raise ValueError('Invalid persisted Telegram monitor schema; manual review required')
        return st
    except FileNotFoundError:
        return {'schema':1,'offsets':{},'last_alerts':{},'service_status':{},
                'updates_offset':0,'daily_date':''}


def save_state(st):
    durable_save(st)


def _report_values(report):
    """Read verified fields from the engine's complete stats snapshot.

    Do not recalculate fees, grid PnL, 24h PnL, percentages or drawdown.
    """
    values={}
    for line in str(report).splitlines():
        m=re.match(r'^\s*([^:]+?):\s*(.*?)\s*$',line)
        if m:
            values[m.group(1).strip().casefold()]=m.group(2).strip()
    return values


def _value(values,*keys):
    for key in keys:
        v=values.get(key.casefold())
        if v and v!='—':return v
    return 'н/д'


def _launch_epoch(root,values):
    """Bot's original first launch, not current process/systemd start."""
    possible=[]
    source=_value(values,'Запущен')
    if source!='н/д':
        try:
            dt=datetime.strptime(source,'%d.%m.%Y %H:%M:%S UTC')
            possible.append(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:pass
    for file,key in ((state_file(root),'started_at'),):
        try:
            val=float(json.loads(file.read_text())[key])
            if 0<val<=time.time():possible.append(val)
        except (OSError,ValueError,KeyError,TypeError):pass
    return min(possible) if possible else None


def _age_label(start):
    if start is None or start>time.time():return 'н/д'
    minutes=max(0,int((time.time()-start)//60))
    days,remaining=divmod(minutes,24*60)
    hours,mins=divmod(remaining,60)
    return f'{days}d {hours}h {mins}min'


def _service_status(name,control_mode,stats_age):
    """Exchange-trading status comes from systemd and control file."""
    try:
        result=subprocess.run(['systemctl','is-active',SERVICES[name]],
                              capture_output=True,text=True,timeout=3)
        active=result.stdout.strip()=='active'
    except (OSError,subprocess.TimeoutExpired):
        return '🟡','unknown'
    if not active:return '🔴','stopped'
    if SHARED_SOFT.exists():return '🟡','auto soft stop'
    control_mode=str(control_mode).upper()
    if control_mode in ('RUN','RUNNING'):
        if stats_age>1200:return '🟡','stats stale'
        return '🟢','running'
    if control_mode in ('STOP','STOPPED','PANIC','ERROR'):
        return '🔴',control_mode.lower()
    return '🟡','paused' if control_mode in ('PAUSE','PAUSED') else control_mode.lower()


def _historical_max_position(root, instrument):
    """Reuse the existing manager's historical position result, never recompute it.

    okxbot.py uses `okx_position_peak.format_peak(root, instrument)`.
    Its output has a dedicated `МАКС. ПОЗИЦИЯ В ИСТОРИИ:` line.
    `max position` in the normal grid report is NOT this value.
    """
    try:
        from okx_position_peak import format_peak
    except ImportError:
        # The script and manager may live in different directories on VPS.
        import sys
        format_peak=None
        for module_dir in ('/opt/okx-grid-live', '/root/okx'):
            if not (Path(module_dir)/'okx_position_peak.py').is_file():
                continue
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
            try:
                from okx_position_peak import format_peak
                break
            except ImportError:
                continue
        if format_peak is None:
            return 'н/д'
    try:
        lines=format_peak(root, instrument) or []
        if isinstance(lines,str):
            lines=lines.splitlines()
        for line in lines:
            label,sep,value=str(line).partition(':')
            if not sep:continue
            normalized=label.casefold().replace('.', '').strip()
            if 'позиц' in normalized and 'истори' in normalized and ('макс' in normalized or 'наибольш' in normalized):
                return value.strip() or 'н/д'
        return 'н/д'
    except (OSError, ValueError, TypeError, KeyError):
        return 'н/д'


def mode_and_stats(name):
    root=Path(BOT_DIRS[name])
    stats=json.loads((root/'state/stats.json').read_text()) if (root/'state/stats.json').is_file() else {}
    values=_report_values(stats.get('report',''))
    age=_age_label(_launch_epoch(root,values))
    return status_one(name,root,age)

# Account summary is a read-only sum of verified bot snapshots.

def _account_number(value):
    # Self-contained converter: cannot depend on another monitor's imports.
    from decimal import Decimal, InvalidOperation
    if value is None or isinstance(value, bool):
        return None
    try:
        number=Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    return number if number.is_finite() else None


def _account_total_pnl(active):
    from decimal import Decimal
    now=time.time()
    total=Decimal(0)
    for _name,root in active:
        cfg=read_json(root/'config.json')
        stats=read_json(root/'state/stats.json')
        perf=(stats.get('performance') or stats.get('performance_v0724')) if isinstance(stats,dict) else None
        if (not isinstance(cfg,dict) or not isinstance(stats,dict)
            or stats.get('complete') is not True or
                stats.get('instrument')!=cfg.get('instrument') or
                not isinstance(perf,dict) or perf.get('schema')!=1):
            return None
        try:
            generated=float(stats['generated_at_unix'])
        except (TypeError,ValueError,KeyError,OverflowError):
            return None
        if not (0<=now-generated<=900):
            return None
        values={k: _account_number(perf.get(k)) for k in (
            'total_pnl','grid_profit','limit_tp_net','market_tp_net',
            'partial_exit_net','floating_pnl','realized_open_components')}
        if any(v is None for v in values.values()):
            return None
        if (abs(values['limit_tp_net']+values['market_tp_net']-values['grid_profit'])>Decimal('0.00000001')
            or abs(values['grid_profit']+values['partial_exit_net']+
                   values['floating_pnl']+values['realized_open_components']-
                   values['total_pnl'])>Decimal('0.00000001')):
            return None
        anchor=read_json(root/'state/stats_financial_anchor.json')
        if not isinstance(anchor,dict):
            return None
        anchor_at=_account_number(anchor.get('at'))
        anchor_total=_account_number(anchor.get('total_pnl_usdt'))
        if (anchor.get('schema')!=1 or anchor.get('instrument')!=cfg.get('instrument')
            or anchor_at is None or abs(anchor_at-_account_number(generated))>Decimal('0.5')
            or anchor_total!=values['total_pnl']):
            return None
        total+=values['total_pnl']
    return total


_ACCOUNT_READER = Path('/root/okx/okx_telegram_account_balance_getonly.py')
_ACCOUNT_READER_SHA = '1556838774bcaa0be9473cb67b2a9c374fe99dbcb46a488c739de55172606754'
_ACCOUNT_READ_CACHE = {'mono':0.0,'value':None}


def _verified_account_balances():
    """One user-triggered, GET-only balance call via isolated child process.

    Telegram Monitor never loads OKX API credentials. No continuous polling.
    Readiness checks bypass this call, and no failed results are cached.
    """
    if os.environ.get('OKX_TELEGRAM_PREFLIGHT_NO_API')=='1':
        return None
    now=time.monotonic()
    if _ACCOUNT_READ_CACHE['value'] is not None and now-_ACCOUNT_READ_CACHE['mono']<12:
        return _ACCOUNT_READ_CACHE['value']
    try:
        if _ACCOUNT_READER.is_symlink() or not _ACCOUNT_READER.is_file():
            return None
        import hashlib
        if hashlib.sha256(_ACCOUNT_READER.read_bytes()).hexdigest()!=_ACCOUNT_READER_SHA:
            return None
        p=subprocess.run([sys.executable,'-B',str(_ACCOUNT_READER),'--fetch'],
                         stdin=subprocess.DEVNULL,capture_output=True,text=True,
                         timeout=5.5,check=False)
        if p.returncode or len(p.stdout)>2048:
            return None
        obj=json.loads(p.stdout)
        if not isinstance(obj,dict) or obj.get('schema')!=1 or obj.get('unit') not in ('USDT','USD'):
            return None
        values={k:_account_number(obj.get(k)) for k in ('equity','balance','floating')}
        if any(x is None for x in values.values()):
            return None
        if (values['equity']<0 or
            abs(values['equity']-values['balance']-values['floating'])>Decimal('0.00000001')):
            return None
        asof=datetime.fromisoformat(str(obj.get('asof')).replace('Z','+00:00'))
        if asof.tzinfo is None or abs((datetime.now(timezone.utc)-asof).total_seconds())>30:
            return None
        _ACCOUNT_READ_CACHE.update(mono=now,value=values)
        return values
    except (OSError,ValueError,TypeError,ArithmeticError,subprocess.TimeoutExpired,OverflowError):
        return None


_CENT = Decimal('0.01')
_TERMINAL_GRID_STATES = frozenset(('CLOSED', 'CANCELED', 'EXITED'))


def _display_money(value, *, signed=False, symbol=True):
    """Display-only rounding; never recompute persisted financial records."""
    if value is None:
        return 'н/д'
    q=value.quantize(_CENT, rounding=ROUND_HALF_UP)
    prefix='+' if q>0 and signed else '−' if q<0 else ''
    return prefix+('$' if symbol else '')+f'{abs(q):,.2f}'


def _grid_formula(net, grid, floating):
    """Keep the equation true at displayed cent precision.

    Other realized components (open-cycle fees, partial MARKET exits) are
    not silently called floating. Add an explicit third term when needed.
    """
    n=net.quantize(_CENT, rounding=ROUND_HALF_UP)
    g=grid.quantize(_CENT, rounding=ROUND_HALF_UP)
    f=floating.quantize(_CENT, rounding=ROUND_HALF_UP)
    extra=n-g-f
    first=('−' if g<0 else '')+f'{abs(g):,.2f}'
    second=('−' if f<0 else '+')+f'{abs(f):,.2f}'
    third=''
    if extra:
        third=('−' if extra<0 else '+')+f'{abs(extra):,.2f}'
    return _display_money(n,signed=True), first+second+third


def _precise_quantity(value, cfg):
    """Exact base quantity at contract*lot granularity; no invented rounding."""
    cv=number(cfg.get('contract_value'))
    lot=number(cfg.get('contract_lot'))
    quantum=cv*lot if cv is not None and lot is not None else None
    if quantum is not None and quantum>0 and value%quantum==0:
        digits=max(0,-quantum.normalize().as_tuple().exponent)
        raw=f'{value:,.{digits}f}'
    else:
        raw=f'{value:,f}'
    if '.' in raw:
        raw=raw.rstrip('0').rstrip('.')
    return raw


def _precise_bid(value, cfg):
    tick=number(cfg.get('tick_size'))
    if tick is None or tick<=0:
        return 'н/д'
    # Use original exchange precision, including trailing zeros in tickSz.
    raw=str(cfg.get('tick_size')).lower()
    digits=max(0,-Decimal(raw).as_tuple().exponent)
    if digits>18:
        return 'н/д'
    return f'{value:,.{digits}f}'


def _compact_bot_row(name,root,now):
    """Only verified, co-timestamped stats and existing durable grid state.

    Never calls the exchange, imports a trading engine, or writes state.
    """
    cfg=read_json(root/'config.json')
    stats=read_json(root/'state/stats.json')
    perf=(stats.get('performance') or stats.get('performance_v0724')) if isinstance(stats,dict) else None
    if (not isinstance(cfg,dict) or not isinstance(perf,dict)
        or stats.get('complete') is not True
        or stats.get('instrument')!=cfg.get('instrument')
        or perf.get('schema')!=1):
        raise ValueError('stats not confirmed')
    generated=_account_number(stats.get('generated_at_unix'))
    if generated is None or not 0<=Decimal(str(now))-generated<=Decimal('900'):
        raise ValueError('stats stale')
    bid=verified_snapshot_bid(root,cfg,stats)
    if bid is None:
        raise ValueError('bid/financial anchor not confirmed')
    values={k:_account_number(perf.get(k)) for k in (
        'grid_profit','total_pnl','floating_pnl','open_base','bid',
        'limit_tp_net','market_tp_net','partial_exit_net','realized_open_components')}
    if any(v is None for v in values.values()):
        raise ValueError('financial field missing')
    if (values['bid']!=bid or values['open_base']<0
        or abs(values['grid_profit']-values['limit_tp_net']-values['market_tp_net'])>Decimal('0.00000001')
        or abs(values['total_pnl']-values['grid_profit']-values['partial_exit_net']-
               values['floating_pnl']-values['realized_open_components'])>Decimal('0.00000001')):
        raise ValueError('financial identity mismatch')
    anchor=read_json(root/'state/stats_financial_anchor.json')
    if (anchor.get('schema')!=1 or anchor.get('instrument')!=cfg.get('instrument')
        or abs((_account_number(anchor.get('at')) or Decimal('-999'))-generated)>Decimal('0.5')
        or _account_number(anchor.get('total_pnl_usdt'))!=values['total_pnl']):
        raise ValueError('financial anchor mismatch')
    cap=cfg.get('max_grids')
    state=read_json(state_file(root))
    if (type(cap)!=int or cap<=0 or
        not isinstance(state.get('cycles'),dict) or
        (state.get('instrument') not in (None,cfg.get('instrument')))):
        raise ValueError('grid state unavailable')
    cycles=state['cycles']
    if any(not isinstance(c,dict) or not isinstance(c.get('state'),str) for c in cycles.values()):
        raise ValueError('invalid grid cycles')
    occupied=sum(c['state'] not in _TERMINAL_GRID_STATES for c in cycles.values())
    if occupied>cap:
        raise ValueError('grid over capacity')
    pct=(Decimal(occupied)*100/Decimal(cap)).quantize(Decimal('1'),rounding=ROUND_HALF_UP)
    coin=str(cfg.get('instrument') or '').split('-')[0]
    if not re.fullmatch(r'[A-Z0-9]{2,20}',coin):
        raise ValueError('invalid coin')
    net=values['total_pnl'];grid=values['grid_profit'];floating=values['floating_pnl']
    left,expr=_grid_formula(net,grid,floating)
    return {'name':name,'marker':'🟢' if net>=0 else '🔴',
        'left':left,'expr':expr,'sort':grid,'stamp':float(generated),
        'position':_precise_quantity(values['open_base'],cfg)+' '+coin,
        'grid':f'{occupied}/{cap} ({pct}%)',
        'bid':_precise_bid(bid,cfg)}


def compact_status_report():
    """One physical message, one logical row per active bot, no unverified $0."""
    refresh_bots(force=True)
    entries=[];failed=[];active=[]
    now=time.time()
    for name in BOT_DIRS:
        if not _active_service(name):continue
        root=Path(BOT_DIRS[name])
        if (root/'state/final_stats.json').exists():continue
        active.append((name,root))
        try:
            entries.append(_compact_bot_row(name,root,now))
        except (OSError,ValueError,KeyError,TypeError,ArithmeticError):
            failed.append(name)
    total=len(BOT_DIRS)
    if not active:
        return f'<b>Активных ботов: 0/{total} · н/д UTC</b>'
    stamps=[entry['stamp'] for entry in entries]
    stamp=(datetime.fromtimestamp(min(stamps),timezone.utc).strftime('%H:%M')
           if entries and not failed else 'н/д')
    header=f'<b>Активных ботов: {len(active)}/{total} · {stamp} UTC</b>'
    entries.sort(key=lambda e:(-e['sort'],e['name']))
    leftwidth=max((len(e['left']) for e in entries),default=0)
    exprwidth=max((len(e['expr']) for e in entries),default=0)
    poswidth=max((len(e['position']) for e in entries),default=0)
    gridwidth=max((len(e['grid']) for e in entries),default=0)
    lines=[header]
    for entry in entries:
        # The '=' and all subsequent column starts align in monospace.
        row=(f"{entry['left']:>{leftwidth}}={entry['expr']:<{exprwidth}}  "
             f"{entry['position']:<{poswidth}}  {entry['grid']:<{gridwidth}}  {entry['bid']}")
        lines.append(entry['marker']+' <code>'+html.escape(row)+'</code>')
    for name in failed:
        lines.append('⚪ <code>'+html.escape(name+' — н/д (снимок не подтверждён)')+'</code>')
    lines.append('')
    # Total PnL is verified bot sum; NOT account equity or OKX balance.
    acc=_account_total_pnl(active) if not failed else None
    if acc is None:
        lines.append('<b>⬜ TOTAL PnL: н/д</b>')
    else:
        lines.append('<b>'+('🟩' if acc>=0 else '🟥')+' TOTAL PnL: '+
                     html.escape(_display_money(acc,signed=True))+'</b>')
    account=_verified_account_balances()
    if account is None:
        lines.extend(('', '<b>💰 Account Equity: н/д</b>',
                      'Balance н/д','Floating PnL н/д'))
    else:
        lines.extend(('', '<b>💰 Account Equity: '+_display_money(account['equity'])+'</b>',
                      'Balance '+_display_money(account['balance']),
                      'Floating PnL '+_display_money(account['floating'],signed=True)))
    text='\n'.join(lines)
    if len(text)>3700:
        raise ValueError('Compact status cannot fit one Telegram message')
    return text


def _account_total_line(active):
    # An account aggregation fault must not suppress the complete /status.
    try:
        total=_account_total_pnl(active)
    except Exception:
        total=None
    if total is None:
        return '<b>⬜ ACCOUNT TOTAL PnL: н/д (нет полной свежей сверки)</b>'
    label=('+' if total>0 else '−' if total<0 else '')+'$'+f'{abs(total):,.2f}'
    marker='🟩' if total>0 else '🟥' if total<0 else '⬜'
    return '<b>'+marker+' ACCOUNT TOTAL PnL: '+html.escape(label)+' '+marker+'</b>'

def status_report(requested=None, *, include_account=False):
    refresh_bots(force=True)
    if requested:
        for name in BOT_DIRS:
            if name.casefold()==requested.casefold():
                return mode_and_stats(name)
        return 'Бот '+html.escape(str(requested))+' не найден.'
    out=[]
    active=[]
    for name in BOT_DIRS:
        if not _active_service(name): continue
        root=Path(BOT_DIRS[name])
        if (root/'state/final_stats.json').exists(): continue
        active.append((name,root))
        try: out.append(mode_and_stats(name))
        except (OSError,ValueError,KeyError,TypeError):
            out.append('<b>'+html.escape(name)+'</b>\n⚠️ Статистика недоступна')
    if not out:
        return 'Нет активных ботов.'
    if include_account:
        out[-1] = out[-1]+'\n\n'+_account_total_line(active)
    return ('\n'+LINE+'\n').join(out)

def send_report(api,report):
    # HTML messages are split ONLY on bot separators; never break HTML tags.
    parts=report.split('\n'+LINE+'\n')
    batch=''
    for part in parts:
        combined=(batch+'\n'+LINE+'\n' if batch else '')+part
        if batch and len(combined)>3700:
            api.send(batch,parse_mode='HTML');batch=part
        else:
            batch=combined
        if len(batch)>3800:
            raise ValueError('Single Telegram bot report exceeds 3800 bytes')
    if batch: api.send(batch,parse_mode='HTML')

def format_alert(name,obj):
    typ=obj['event']
    parts=[f'{ALERT_EVENTS[typ]} | OKX {name}']
    for key in ('severity','code','price','cycleId','cycles','qty','requested',
                'grid_pnl','final_pnl','remaining','mode',
                'filled','avgPx','state','ordId','clOrdId','seconds','reason','error','message'):
        if key in obj and obj[key] not in ('',None):
            safe=re.sub(r'[^\w\d .:,()=+\-_/]', '',str(obj[key]))
            parts.append(f'{key}: {safe[:200]}')
    return '\n'.join(parts)


def ingest_events(api,st):
    for name,root_s in BOT_DIRS.items():
        root=Path(root_s)
        path=events_file(root)
        if not path.is_file(): continue
        key=name
        start=st.setdefault('offsets',{}).get(key)
        size=path.stat().st_size
        if start is None:
            # New monitor starts from tail; don't replay old live incidents.
            st['offsets'][key]=size
            continue
        if start>size: start=0
        with path.open('rb') as f:
            f.seek(start)
            while True:
                row=f.readline()
                if not row or not row.endswith(b'\n'):break
                try: obj=json.loads(row)
                except ValueError:
                    st['offsets'][key]=f.tell();continue
                kind=obj.get('event')
                if kind in ('AGG_MARKET_INTENT','AGG_MARKET_ACK','AGG_MARKET_TERMINAL',
                            'AGG_MARKET_REJECTED'):
                    market_event(st,name,obj)
                else:
                    event_alert(api,st,name,obj)
                st['offsets'][key]=f.tell()
                # One normal cursor commit per poll in main().
                # Any alert was already durably committed by enqueue().
    market_process(api,st,{k:Path(v) for k,v in BOT_DIRS.items()})
    names={k:Path(v) for k,v in BOT_DIRS.items() if _active_service(k)}
    observer_fault_scan(api,st,names)
    panic_scan(api,st,{k:Path(v) for k,v in BOT_DIRS.items()})
    partial_scan(api,st,names,{k:Path(v) for k,v in BOT_DIRS.items()})
    zero_buy_scan(api,st,names)
    gate_alert_scan(api,st)


# Console latency bridge: source-controlled, versionless read-only sibling CLI.
# No API calls or trading mutations here. Main thread owns Telegram send and state.
import html as _latency_html
import queue as _latency_queue
import re as _latency_re
import subprocess as _latency_subprocess
import threading as _latency_threading
import time as _latency_time

_LATENCY_CLI_PATH = str(Path(__file__).resolve().with_name('okx_per_bot_speed_readonly.py'))
_LATENCY_CLI_RESULTS = _latency_queue.Queue(maxsize=1)
_LATENCY_CLI_THREAD = None
_LATENCY_CLI_TIMEOUT = 25


def _latency_console_worker(minutes):
    start = _latency_time.monotonic()
    try:
        proc = _latency_subprocess.run(
            ['nice', '-n', '19', 'ionice', '-c3', '/usr/bin/python3', '-B',
             _LATENCY_CLI_PATH, '--minutes', str(minutes)],
            stdout=_latency_subprocess.PIPE, stderr=_latency_subprocess.PIPE,
            text=True, errors='replace', timeout=_LATENCY_CLI_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError('код возврата ' + str(proc.returncode) +
                               '; stderr: ' + proc.stderr.strip()[-400:])
        if 'OKX LIVE PER-BOT SPEED' not in proc.stdout or 'NO API' not in proc.stdout:
            raise RuntimeError('консольный анализатор вернул неожиданный формат')
        if len(proc.stdout) > 30000:
            raise RuntimeError('консольный отчёт слишком большой для Telegram')
        _LATENCY_CLI_RESULTS.put(('ok', minutes, proc.stdout,
            round(_latency_time.monotonic() - start, 2)))
    except _latency_subprocess.TimeoutExpired:
        _LATENCY_CLI_RESULTS.put(('error', minutes,
            'консольный анализатор превысил лимит 25 секунд',
            round(_latency_time.monotonic() - start, 2)))
    except Exception as exc:
        _LATENCY_CLI_RESULTS.put(('error', minutes, str(exc),
            round(_latency_time.monotonic() - start, 2)))


def _latency_console_send_chunks(api, raw, minutes, duration):
    # Formatting only; no recalculation, filtering or truncation of statistics.
    clean = _latency_re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', raw).strip()
    # Use inline <code> per row instead of <pre>: no Telegram Copy header.
    # Keep line boundaries and escape HTML. Conservative 2200-char chunks
    # leave room for tags and Telegram UTF-16 accounting on all clients.
    rows=[]
    for line in clean.splitlines():
        line=line.rstrip()
        if line.startswith('===') and line.endswith('==='):
            rows.append('<b>'+_latency_html.escape(line)+'</b>')
        elif line:
            if len(line)>1500:
                # Never cut inside an entity: split raw text, escape afterwards.
                rows.extend('<code>'+_latency_html.escape(line[i:i+1200])+'</code>'
                            for i in range(0,len(line),1200))
            else:
                rows.append('<code>'+_latency_html.escape(line)+'</code>')
        else:
            rows.append('')
    chunks=[];block=''
    for row in rows:
        candidate=(block+'\n' if block else '')+row
        if block and len(candidate)>2200:
            chunks.append(block)
            block=row
        else:
            block=candidate
    if block:chunks.append(block)
    if not chunks:
        raise RuntimeError('консольный отчёт пустой')
    for idx, chunk in enumerate(chunks,1):
        title=('<b>⚡ OKX LATENCY ('+str(minutes)+' мин) '+
               str(idx)+'/'+str(len(chunks))+', '+str(duration)+' с</b>')
        api.send(title+'\n'+chunk,parse_mode='HTML')


def _latency_console_flush(api):
    global _LATENCY_CLI_THREAD
    try:
        kind, minutes, payload, duration = _LATENCY_CLI_RESULTS.get_nowait()
    except _latency_queue.Empty:
        return
    _LATENCY_CLI_THREAD = None
    if kind == 'ok':
        print('TG_LATENCY_CLI_DONE minutes=' + str(minutes) +
              ' seconds=' + str(duration) + ' chars=' +
              str(len(payload)), flush=True)
        _latency_console_send_chunks(api, payload, minutes, duration)
    else:
        print('TG_LATENCY_CLI_FAILED minutes=' + str(minutes) +
              ' reason=' + str(payload)[:200], flush=True)
        api.send('⚠️ /latency: консольный отчёт не получен (' +
                 _latency_html.escape(str(payload)[:350]) + '). ' +
                 'Торговля не затронута.')


def _latency_console_start(api, minutes):
    global _LATENCY_CLI_THREAD
    if _LATENCY_CLI_THREAD is not None:
        api.send('⏳ Предыдущий отчёт latency ещё формируется.')
        return
    _LATENCY_CLI_THREAD = _latency_threading.Thread(
        target=_latency_console_worker, args=(minutes,),
        name='tg-console-latency-readonly', daemon=True)
    _LATENCY_CLI_THREAD.start()
    print('TG_LATENCY_CLI_STARTED minutes=' + str(minutes), flush=True)
    api.send('⏳ Запустил консольный расчёт latency за ' + str(minutes) +
             ' минут. /status продолжает работать.')


_TELEGRAM_COMMANDS = (
    {'command':'status','description':'Кратко: PnL, позиция, сетка, BID'},
    {'command':'full','description':'Подробная статистика и общий PnL'},
    {'command':'latency','description':'Скорость торговли за 60 минут'},
    {'command':'help','description':'Список команд'},
)


def register_command_menu(api):
    """Register only in the authorized chat; no global bot command changes."""
    scope={'type':'chat','chat_id':int(api.chat)}
    api.call('setMyCommands',{'commands':list(_TELEGRAM_COMMANDS),'scope':scope})
    api.call('setChatMenuButton',{'chat_id':int(api.chat),
                                 'menu_button':{'type':'commands'}})


def poll_commands(api,st):
    _latency_console_flush(api)
    updates=api.call('getUpdates',{'offset':st['updates_offset'],'timeout':0,'limit':20})
    for msg in updates:
        st['updates_offset']=msg['update_id']+1
        data=msg.get('message',{})
        if str(data.get('chat',{}).get('id',''))==api.chat:
            parts=str(data.get('text','')).strip().split()
            cmd=parts[0].split('@')[0] if parts else ''
            if cmd=='/status':
                if len(parts)!=1:
                    api.send('Допустимо: /status')
                else:
                    api.send(compact_status_report(),parse_mode='HTML')
            elif cmd=='/full':
                if len(parts)>2:
                    api.send('Допустимо: /full или /full <бот>')
                else:
                    send_report(api,status_report(parts[1] if len(parts)==2 else None,
                                                  include_account=True))
            elif cmd=='/help':
                api.send('📊 /status — деньги и позиции одной строкой на бот'
                         '\n📋 /full — полный отчёт и TOTAL PnL'
                         '\n⚡ /latency — последние 60 минут'
                         '\n/help — список команд')
            elif cmd=='/latency':
                if len(parts)!=1:
                    api.send('Допустимо: /latency')
                else:
                    _latency_console_start(api, 60)
        save_state(st)

def services_check(api,st):
    service_check(api,st,SERVICES,BOT_DIRS)


def _mon_phase(name, fn, *args, **kwargs):
    from okx_telegram_phases_v4 import measure
    return measure(name, fn, *args, **kwargs)


def safety_check(api,st):
    now=time.time()
    is_soft=SHARED_SOFT.exists()
    was_soft=st.get('shared_soft')
    if is_soft and was_soft is not True:
        one(api,st,'shared_soft/'+str(int(now)),
            '🚨 OKX ACCOUNT: shared AUTO SOFT STOP. Manual reset required.')
    elif not is_soft and was_soft is True:
        one(api,st,'shared_soft/clear/'+str(int(now)),
            '✅ OKX ACCOUNT: общая SOFT-блокировка снята. Состояние каждого бота проверяйте отдельно.')
    st['shared_soft']=is_soft
    _mon_phase("safety.heartbeat",engine_heartbeat_check,api,st,SERVICES,BOT_DIRS,now)
    _mon_phase("safety.observers",observers_check,api,st,SERVICES,BOT_DIRS,now)
    _mon_phase("safety.panic_ack",panic_ack_check,api,st,SERVICES,BOT_DIRS)
    _mon_phase("safety.reader_health",position_reader_health_check,api,st)
    _mon_phase("safety.reader_bypass",reader_bypass_check,api, st, SERVICES, BOT_DIRS)


def daily_check(api,st,now=None):
    now=now or datetime.now(TZ)
    day=now.strftime('%Y-%m-%d')
    if now.hour>=9 and st.get('daily_date')!=day:
        send_report(api,status_report())
        st['daily_date']=day


def main():
    from okx_telegram_io_probe import install as _mon_io_install, tick as _mon_io_tick
    _mon_io_install()
    token,chat=read_env(Path(os.environ.get('OKX_TELEGRAM_CONFIG','/etc/okxbot/telegram.env')))
    api=Telegram(token,chat)
    original_call = api.call
    def measured_telegram_call(method, params):
        label = 'telegram.http.' + (method if method in ('getUpdates','sendMessage') else 'other')
        return _mon_phase(label, original_call, method, params)
    api.call = measured_telegram_call
    st=load_state()
    # Telegram menu registration is isolated from the monitor's live polling
    # connection and main loop; a slow Bot API call cannot delay alerts.
    def _menu_setup():
        menu_api=Telegram(token,chat)
        try:
            register_command_menu(menu_api)
            print('TG_COMMAND_MENU=PASS',flush=True)
        except Exception as exc:
            print('TG_COMMAND_MENU=UNAVAILABLE '+type(exc).__name__,flush=True)
        finally:
            menu_api._close_http()
    _latency_threading.Thread(target=_menu_setup,name='tg-command-menu',daemon=True).start()
    print('OKX Telegram monitor running',flush=True)
    from okx_telegram_phases_v4 import loop_end
    while True:
        cycle_started = time.monotonic()
        try:
            _mon_phase('refresh', refresh_bots)
            _mon_phase('snapshot', begin_systemd_snapshot, SERVICES)
            _mon_phase('ingest_events', ingest_events, api, st)
            _mon_phase('services_check', services_check, api, st)
            _mon_phase('safety_check', safety_check, api, st)
            _mon_phase('poll_commands', poll_commands, api, st)
            # Scheduled 09:00 report disabled; /full remains manual.
            _mon_phase('save_state', save_state, st)
            _mon_phase('alert_drain', alert_drain, api, st)
        except Exception as exc:
            # No credentials or outbound URL in diagnostics, ever.
            print('Telegram monitor retry:',type(exc).__name__,flush=True)
            try: alert_drain(api,st)
            except Exception: pass
        loop_end(time.monotonic() - cycle_started)
        _mon_io_tick()
        time.sleep(POLL_SECONDS)


if __name__=='__main__':main()
