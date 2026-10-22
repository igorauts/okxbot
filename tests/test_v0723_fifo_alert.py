"""Deterministic local-only regression: gate recovered before queue delivery."""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import okx_telegram_events as events
import okx_telegram_delivery as delivery
import okx_latency_ui as latency

class API:
    def __init__(self): self.sent=[]
    def send(self,msg,parse_mode=None): self.sent.append(msg)

now=time.time()
base={'event':'ACCOUNT_POSITIONS_GATE_DEGRADED','error':'GateUnavailable',
      'ts':'2026-10-22T15:17:28+00:00'}
recovered={'event':'RECONCILIATION_RECOVERED','duration_sec':3,
           'ts':'2026-10-22T15:17:31+00:00'}
with tempfile.TemporaryDirectory() as t:
    path=Path(t)/'telegram.json'
    api=API(); st={'schema':1}
    with patch.object(delivery,'STATE_PATH',path):
        # Exact reported failure sequence: 3 second gate outage. No stale alarm.
        with patch.object(events.time,'time',return_value=now):
            events.event_alert(api,st,'near0721',base)
        events.event_alert(api,st,'near0721',recovered)
        events.gate_alert_scan(api,st,now+50)
        assert st.get('alert_outbox_v0722',[])==[]
        assert st['positions_gate_pending_v0723']=={}
        # A genuinely sustained failure MUST alert after 15s, not before.
        with patch.object(events.time,'time',return_value=now+60):
            events.event_alert(api,st,'near0721',base)
        events.gate_alert_scan(api,st,now+74.9)
        assert len(st.get('alert_outbox_v0722',[]))==0
        events.gate_alert_scan(api,st,now+75.1)
        assert len(st['alert_outbox_v0722'])==1
        events.gate_alert_scan(api,st,now+95)
        assert len(st['alert_outbox_v0722'])==1
        # If the alarm is still waiting for Telegram delivery, eliminate
        # the stale alarm rather than sending it with an instant recovery.
        events.event_alert(api,st,'near0721',recovered)
        assert len(st['alert_outbox_v0722'])==0
        events.event_alert(api,st,'near0721',recovered)
        assert len(st['alert_outbox_v0722'])==0
        # If the alarm really WAS delivered, notify on explicit recovery.
        with patch.object(events.time,'time',return_value=now+100):
            events.event_alert(api,st,'near0721',base)
        events.gate_alert_scan(api,st,now+116)
        assert delivery.drain(api,st,path=path,now=now+116)==1
        events.event_alert(api,st,'near0721',recovered)
        assert len(st['alert_outbox_v0722'])==1
        assert 'ACCOUNT_POSITIONS_GATE_RECOVERED' in st['alert_outbox_v0722'][-1]['text']
        # A new episode after confirmed recovery is eligible independently.
        with patch.object(events.time,'time',return_value=now+120):
            events.event_alert(api,st,'near0721',base)
        events.gate_alert_scan(api,st,now+136)
        assert len(st['alert_outbox_v0722'])==2
        assert delivery.drain(api,st,path=path,now=now+200)==2
        assert len(api.sent)==3  # one earlier alert + recovery + fresh incident
        # No loss after monitor reboot: deferred status survives persisted state.
        st={'schema':1}
        with patch.object(events.time,'time',return_value=now+200):
            events.event_alert(api,st,'btc0721',base)
        delivery.persist(st,path)
        st2=json.loads(path.read_text())
        events.gate_alert_scan(api,st2,now+217)
        assert len(st2['alert_outbox_v0722'])==1
        # Even at the 32MiB tail boundary the last valid record is parsed.
        logfile=Path(t)/'latency.jsonl'
        logfile.write_bytes(b'0'*9*1024*1024 + b'\n'+json.dumps({'ts':'2026-10-22T15:17:31+00:00', 'phase':'http','ms':3.3}).encode()+b'\n')
        assert len(latency._records(logfile))==1
        assert latency._records.__defaults__[0]==32*1024*1024
        assert '32 MiB' in latency.report(Path(t))
print('PASS: 3s gate outage suppressed; sustained alarm, recovery, durable restart, 32 MiB latency tail')
