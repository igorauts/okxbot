#!/usr/bin/env python3
"""Read-only Telegram control-room for OKX bot instances (status v0.7.7).

One process serves both instruments; it never loads exchange API keys, never
sends/cancels an exchange order, and never writes trading state/control files.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in a mode-0600 env file.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

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
    'BUY_POST_UNRESOLVED':'⚠️ BUY POST unresolved',
    'TP_POST_UNRESOLVED':'⚠️ TP POST unresolved',
    'KNOWN_TP_UNRESOLVED':'⚠️ Known TP unavailable',
    'TP_RESTORE_REJECTED':'🔴 TP restore rejected',
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


class Telegram:
    def __init__(self,token,chat):
        self.base='https://api.telegram.org/bot'+token+'/'
        self.chat=str(chat)
    def call(self,method,params):
        payload=json.dumps(params).encode()
        req=Request(self.base+method,data=payload,headers={'Content-Type':'application/json'})
        with urlopen(req,timeout=18) as response:
            result=json.loads(response.read().decode())
        if not result.get('ok'):
            raise RuntimeError('Telegram '+method+' returned not-ok')
        return result.get('result')
    def send(self,text):
        return self.call('sendMessage',{'chat_id':self.chat,'text':text[:3900]})


def load_state():
    try:
        st=json.loads(STATE_PATH.read_text())
        if st.get('schema')!=1:raise ValueError('wrong schema')
        return st
    except FileNotFoundError:
        return {'schema':1,'offsets':{},'last_alerts':{},'service_status':{},
                'updates_offset':0,'daily_date':''}


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=STATE_PATH.with_suffix('.tmp')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(st,handle,ensure_ascii=False)
        handle.flush();os.fsync(handle.fileno())
    os.replace(target,STATE_PATH)


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
    for file,key in (
        (root/'state/bot04_state.json','started_at'),
        (root/'state/okx_stats_grid_reference_v059.json','started_at'),
    ):
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
        for module_dir in ('/root/okx', '/opt/okx-grid-v068'):
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
    final=_terminal_report(name)
    if final is not None:
        root=Path(BOT_DIRS[name])
        path=root/'state/stats.json'
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
            hist=data.get('report') if (data.get('complete') is True and
                    data.get('instrument') == cfg.get('instrument') and
                    isinstance(data.get('report'),str) and data.get('report').strip()) else None
        except (OSError,ValueError,TypeError):
            hist=None
        return (f'📜 {name}: ИСТОРИЧЕСКИЙ СНИМОК ДО ЗАВЕРШЕНИЯ\n\n'
                + (hist+'\n\n' if hist else 'Историческая статистика недоступна.\n\n')
                + '━'*24+'\n'+final)
    root=Path(BOT_DIRS[name]);control=root/'state/control.json'
    mode='RUN'
    if control.exists():
        mode=json.loads(control.read_text()).get('mode','UNKNOWN')
    path=root/'state/stats.json'
    state_file=root/'state/bot04_state.json'
    if state_file.is_file():
        st=json.loads(state_file.read_text())
        if st.get('terminal_mode') in ('PANIC_DONE','RETIRED'):
            return f'⏳ {name}: бот завершён, финальный расчёт ожидает подтверждения'
    if not path.exists():return f'🟡 {name.lower()}: статистика недоступна'
    stats=json.loads(path.read_text())
    age=max(0,time.time()-float(stats.get('generated_at_unix',0)))
    values=_report_values(stats.get('report',''))
    version=stats.get('bot_version') or 'н/д'
    try:
        state=json.loads((root/'state/bot04_state.json').read_text())
        if state.get('instrument')!=stats.get('instrument'):
            raise ValueError('Statistics instrument mismatch')
        version=state.get('version') or version
    except (OSError,ValueError,KeyError,TypeError):
        pass
    icon,status=_service_status(name,mode,age)
    age_label=_age_label(_launch_epoch(root,values))
    uptime_suffix=(' '+age_label if age_label!='н/д' else '')
    bot_name=root.name
    rows=[f'{icon} {bot_name} · v{str(version).lstrip("v")} · {status}{uptime_suffix}']
    if age>1200:rows.append(f'⚠️ Статистика обновлялась {int(age//60)} мин назад')
    labels=(
        ('Текущий BID','Текущий BID'),
        ('Закрытых циклов','Закрытых циклов'),
        ('Грид профит','GRID PROFIT'),
        ('Торговый объём','Общий объём'),
        ('Уплаченная комиссия','Уплачено комиссий'),
        ('Прибыль за последние 24ч','Прибыль за последние 24ч'),
        ('Открытая позиция','Открытая позиция'),
        ('Нереализованный PnL','Нереализованный PnL'),
        ('Итоговый PnL','ИТОГОВЫЙ PnL'),
    )
    for label,key in labels:
        rows.append(f'{label}: {_value(values,key)}')
    rows.extend(['','Средний темп:',
                 f'  сутки: {_value(values,"сутки")}',
                 f'  30 дней: {_value(values,"30 дней")}',
                 f'  год: {_value(values,"год")}',
                 ''])
    drawdown=_value(values,'МАКС. ПРОСАДКА БОТА')
    if drawdown!='н/д' and '%' in drawdown:
        drawdown=drawdown.replace('%)','% от 1x)')
    rows.append(f'Максимальная просадка бота: {drawdown}')
    rows.append(f'  Зафиксирована: {_value(values,"Зафиксирована")}')
    rows.append('Максимальная позиция в истории: '+
                _historical_max_position(root, stats.get("instrument", "")))
    return '\n'.join(rows)


def status_report(requested=None):
    refresh_bots()
    if requested:
        for name in BOT_DIRS:
            if requested.casefold() in (name.casefold(),Path(BOT_DIRS[name]).name.casefold()):
                return '📊 OKX Grid Monitor\n\n'+mode_and_stats(name)
        return f'Бот {requested} не найден. /status — все активные боты.'
    output=['📊 OKX Grid Monitor — активные боты']
    active=[name for name in BOT_DIRS if _active_service(name)]
    if not active:output.append('Нет активных ботов. Для завершённого: /status имя_бота')
    for idx,name in enumerate(active):
        if idx:output.extend(['','━━━━━━━━━━━━━━━━━━'])
        output.append('')
        try:output.append(mode_and_stats(name))
        except Exception:output.append(f'🟡 {name}: статистика временно недоступна')
    return '\n'.join(output)


def send_report(api,report):
    """Telegram truncates at 3900 in send(); split instead of dropping bots."""
    lines=report.splitlines(keepends=True)
    block=''
    for line in lines:
        if len(block)+len(line)>3700 and block:
            api.send(block.rstrip());block=''
        while len(line)>3700:
            api.send(line[:3700]);line=line[3700:]
        block+=line
    if block.strip():api.send(block.rstrip())


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
    for name,root in BOT_DIRS.items():
        path=Path(root)/'logs/bot04_events.jsonl'
        if not path.is_file():continue
        signature=f'{name}'
        known=st['offsets'].get(signature)
        legacy={'eth064':'ETH','sol065':'SOL'}.get(name)
        if known is None and legacy in st['offsets']:
            known=st['offsets'][legacy]
        size=path.stat().st_size
        if known is None:
            # Initial launch does not spam the entire history.
            st['offsets'][signature]=size
            continue
        if known>size:known=0  # log rotated/truncated
        with path.open('rb') as file:
            file.seek(known)
            while True:
                point=file.tell()
                raw=file.readline()
                if not raw:break
                if not raw.endswith(b'\n'):
                    break
                obj=json.loads(raw)
                typ=obj.get('event')
                if typ in ALERT_EVENTS:
                    key=f'{name}/{typ}/{obj.get("severity","")}'
                    now=time.time()
                    last=float(st['last_alerts'].get(key,0))
                    # Every MARKET event belongs to its own order; never
                    # suppress another market exit for 10 minutes.
                    important=typ in MARKET_ALERTS or typ in (
                        'ACCOUNT_AUTO_SOFT_STOP','AUTO_SOFT_MODE_LATCHED')
                    if important or now-last>=RATE_LIMIT_SECONDS:
                        api.send(format_alert(name,obj))
                        st['last_alerts'][key]=now
                st['offsets'][signature]=file.tell()
                # Write cursor only after successful send. On API failure, replay.
                save_state(st)


def poll_commands(api,st):
    updates=api.call('getUpdates',{'offset':st['updates_offset'],'timeout':0,'limit':20})
    for msg in updates:
        st['updates_offset']=msg['update_id']+1
        data=msg.get('message',{})
        source=str(data.get('chat',{}).get('id',''))
        if source==api.chat:
            raw=str(data.get('text','')).strip().split()
            if raw and raw[0].split('@')[0]=='/status':
                send_report(api,status_report(raw[1] if len(raw)>1 else None))
        save_state(st)


def services_check(api,st):
    # Keep last statuses for stopped bots so active -> stopped is notified.
    for name,service in SERVICES.items():
        result=subprocess.run(['systemctl','is-active',service],capture_output=True,
                              text=True,timeout=5).stdout.strip()
        previous=st['service_status'].get(name)
        if result=='active' and previous is None:
            st['service_status'][name]=result;continue
        if result!=previous and previous is not None:
            api.send(f'{"✅" if result=="active" else "🚨"} OKX {name}: service {previous} → {result}')
        st['service_status'][name]=result


def safety_check(api,st):
    is_soft=SHARED_SOFT.exists()
    previous=st.get('shared_soft')
    if is_soft and previous is not True:
        api.send('🚨 OKX ACCOUNT: shared AUTO SOFT STOP active. Manual reset required.')
    elif not is_soft and previous is True:
        api.send('✅ OKX ACCOUNT: shared SOFT latch cleared manually.')
    st['shared_soft']=is_soft
    for name,root in BOT_DIRS.items():
        if not _active_service(name):continue
        heartbeat=Path(root)/'state/okx_stats_runtime_v059.json'
        if not heartbeat.is_file():continue
        try:
            value=json.loads(heartbeat.read_text())
            age=time.time()-float(value.get('last',0))
            if age>120 and time.time()-float(st['last_alerts'].get('HEARTBEAT/'+name,0))>600:
                api.send(f'🚨 OKX {name}: no trading heartbeat for {int(age)} seconds')
                st['last_alerts']['HEARTBEAT/'+name]=time.time()
        except (OSError,ValueError,KeyError):pass


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
        except Exception as exc:
            # No credentials or outbound URL in diagnostics, ever.
            print('Telegram monitor retry:',type(exc).__name__,flush=True)
        time.sleep(POLL_SECONDS)


if __name__=='__main__':main()
