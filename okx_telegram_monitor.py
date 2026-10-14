#!/usr/bin/env python3
"""Read-only Telegram control-room for OKX bot instances (v0.6.10).

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
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

BOT_DIRS = {'ETH':'/root/okx/eth064', 'SOL':'/root/okx/sol065'}
SERVICES = {'ETH':'okx-grid-eth064', 'SOL':'okx-grid-sol065'}
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
}
RATE_LIMIT_SECONDS = 600
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


def mode_and_stats(name):
    root=Path(BOT_DIRS[name]);control=root/'state/control.json'
    mode='RUN'
    if control.exists():
        mode=json.loads(control.read_text()).get('mode','UNKNOWN')
    if SHARED_SOFT.exists():mode='AUTO SOFT'
    path=root/'state/stats.json'
    if not path.exists():return f'{name}: {mode} | statistics unavailable'
    st=json.loads(path.read_text())
    age=time.time()-float(st.get('generated_at_unix',0))
    text=str(st.get('report',''))
    picks=['Текущий BID:','Закрытых циклов:','BUY LIMIT:','ждут TP:',
           'виртуальных TP:','GRID PROFIT:','Прибыль за последние 24ч:',
           'Открытая позиция:','Нереализованный PnL:','ИТОГОВЫЙ PnL:']
    rows=[line.strip() for line in text.splitlines() if any(item in line for item in picks)]
    stale=f' | статистика {int(age//60)} мин назад' if age>1200 else ''
    return '\n'.join([f'{name}: {mode}{stale}',*rows])


def status_report():
    output=['📊 OKX BOTS | v0.6.10']
    for name in BOT_DIRS:
        try:output.extend(['',mode_and_stats(name)])
        except Exception:output.extend(['',f'{name}: statistics temporarily unavailable'])
    return '\n'.join(output)


def format_alert(name,obj):
    typ=obj['event']
    parts=[f'{ALERT_EVENTS[typ]} | OKX {name}']
    for key in ('severity','code','price','cycleId','seconds','reason','message'):
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
                    important=typ in ('ACCOUNT_AUTO_SOFT_STOP','AUTO_SOFT_MODE_LATCHED')
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
        if source==api.chat and str(data.get('text','')).split('@')[0].startswith('/status'):
            api.send(status_report())
        save_state(st)


def services_check(api,st):
    for name,service in SERVICES.items():
        result=subprocess.run(['systemctl','is-active',service],capture_output=True,
                              text=True,timeout=5).stdout.strip()
        previous=st['service_status'].get(name)
        if result!='active' and previous is None:
            api.send(f'🚨 OKX {name}: service {result} at monitor start')
        elif result!=previous and previous is not None:
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
        api.send(status_report())
        st['daily_date']=day


def main():
    token,chat=read_env(Path(os.environ.get('OKX_TELEGRAM_CONFIG','/etc/okxbot/telegram.env')))
    api=Telegram(token,chat)
    st=load_state()
    print('OKX Telegram monitor running',flush=True)
    while True:
        try:
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
