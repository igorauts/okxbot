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
from urllib.request import Request, urlopen
import html
from okx_telegram_views import status_one, latency_report, read_json, LINE
from okx_telegram_events import (one, event_alert, market_event, market_process, zero_buy_scan, observer_fault_scan, gate_alert_scan)
from okx_telegram_followups import partial_scan, panic_scan
from okx_telegram_delivery import persist as durable_save, drain as alert_drain
from okx_telegram_health import service_check, engine_heartbeat_check, observers_check, panic_ack_check, position_reader_health_check

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


def refresh_bots(*,force=False):
    global _DISCOVERY_LAST
    now=time.monotonic()
    if not force and now-_DISCOVERY_LAST<15:return
    _DISCOVERY_LAST=now
    discovered=discover_bots()
    BOT_DIRS.clear();SERVICES.clear()
    for key,(root,unit) in discovered.items():
        BOT_DIRS[key]=root;SERVICES[key]=unit


def _active_service(name):
    try:
        p=subprocess.run(['systemctl','is-active',SERVICES[name]],
            capture_output=True,text=True,timeout=3)
        return p.stdout.strip()=='active'
    except (OSError,subprocess.TimeoutExpired,KeyError):return False


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
        self.chat=str(chat)
    def call(self,method,params):
        payload=json.dumps(params).encode()
        req=Request(self.base+method,data=payload,headers={'Content-Type':'application/json'})
        from urllib.error import HTTPError
        try:
            with urlopen(req,timeout=18) as response:
                result=json.loads(response.read().decode())
        except HTTPError as exc:
            # Permanent, independently established HTTP errors only.
            # 403 may mean bot blocked; 400 indicates a malformed message.
            if exc.code in (400,403):
                raise TelegramPermanentError(f'Telegram permanent HTTP {exc.code}') from exc
            raise
        if not result.get('ok'):
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


def _account_total_line(active):
    # An account aggregation fault must not suppress the complete /status.
    try:
        total=_account_total_pnl(active)
    except Exception:
        total=None
    if total is None:
        return '<b>ACCOUNT TOTAL PnL: н/д (нет полной свежей сверки)</b>'
    label=('+' if total>0 else '−' if total<0 else '')+'$'+f'{abs(total):,.2f}'
    marker='🟢' if total>0 else '🔴' if total<0 else '⚪'
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
        out.append(_account_total_line(active))
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
                save_state(st)
    market_process(api,st,{k:Path(v) for k,v in BOT_DIRS.items()})
    names={k:Path(v) for k,v in BOT_DIRS.items() if _active_service(k)}
    observer_fault_scan(api,st,names)
    panic_scan(api,st,{k:Path(v) for k,v in BOT_DIRS.items()})
    partial_scan(api,st,names,{k:Path(v) for k,v in BOT_DIRS.items()})
    zero_buy_scan(api,st,names)
    gate_alert_scan(api,st)

def poll_commands(api,st):
    updates=api.call('getUpdates',{'offset':st['updates_offset'],'timeout':0,'limit':20})
    for msg in updates:
        st['updates_offset']=msg['update_id']+1
        data=msg.get('message',{})
        if str(data.get('chat',{}).get('id',''))==api.chat:
            parts=str(data.get('text','')).strip().split()
            cmd=parts[0].split('@')[0] if parts else ''
            if cmd=='/status':
                send_report(api,status_report(parts[1] if len(parts)>1 else None, include_account=True))
            elif cmd=='/help':
                api.send('🤖 OKX Grid Bot\n/status — статистика активных ботов'
                         '\n/latency — последние 60 минут'
                         '\n/latency8h — последние 8 часов\n/help — команды')
            elif cmd in ('/latency', '/latency8h'):
                hours=8 if cmd=='/latency8h' or (len(parts)==2 and parts[1].casefold()=='8h') else 1
                if ((cmd=='/latency8h' and len(parts)>1) or (cmd=='/latency' and len(parts)>1 and (len(parts)!=2 or parts[1].casefold()!='8h'))):
                    api.send('Допустимо: /latency или /latency8h')
                else:
                    refresh_bots(force=True)
                    active=[(name,Path(root)) for name,root in BOT_DIRS.items()
                            if _active_service(name) and not (Path(root)/'state/final_stats.json').exists()]
                    send_report(api,latency_report(active,hours))
        save_state(st)

def services_check(api,st):
    service_check(api,st,SERVICES,BOT_DIRS)


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
    engine_heartbeat_check(api,st,SERVICES,BOT_DIRS,now)
    observers_check(api,st,SERVICES,BOT_DIRS,now)
    panic_ack_check(api,st,SERVICES,BOT_DIRS)
    position_reader_health_check(api,st)


def daily_check(api,st,now=None):
    now=now or datetime.now(TZ)
    day=now.strftime('%Y-%m-%d')
    if now.hour>=9 and st.get('daily_date')!=day:
        send_report(api,status_report())
        st['daily_date']=day


def main():
    token,chat=read_env(Path(os.environ.get('OKX_TELEGRAM_CONFIG','/etc/okxbot/telegram.env')))
    api=Telegram(token,chat)
    st=load_state()
    print('OKX Telegram monitor running',flush=True)
    while True:
        try:
            refresh_bots()
            ingest_events(api,st)
            services_check(api,st)
            safety_check(api,st)
            poll_commands(api,st)
            daily_check(api,st)
            save_state(st)
            alert_drain(api,st)
        except Exception as exc:
            # No credentials or outbound URL in diagnostics, ever.
            print('Telegram monitor retry:',type(exc).__name__,flush=True)
            try: alert_drain(api,st)
            except Exception: pass
        time.sleep(POLL_SECONDS)


if __name__=='__main__':main()
