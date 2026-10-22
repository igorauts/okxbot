"""Offline deterministic v0.7.22 Telegram/systemd and UI tests; no exchange access."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import okx_telegram_delivery as delivery
import okx_telegram_events as events
import okx_telegram_health as health
import okx_telegram_followups as followups
import okx_telegram_views as views

class APIFake:
    def __init__(self,fail=0): self.sent=[];self.fail=fail
    def send(self,msg,parse_mode=None):
        if self.fail:
            self.fail-=1
            raise OSError('Simulated Telegram failure')
        self.sent.append((msg,parse_mode))

with tempfile.TemporaryDirectory() as directory:
    base=Path(directory)
    state=base/'telegram.json'
    api=APIFake(fail=1)
    st={'schema':1}
    assert delivery.enqueue(st,'incident/x','🚨 failure',path=state)
    assert not delivery.enqueue(st,'incident/x','🚨 failure',path=state)
    assert len(json.loads(state.read_text())['alert_outbox_v0722'])==1
    assert delivery.drain(api,st,path=state,now=time.time())==0
    assert len(json.loads(state.read_text())['alert_outbox_v0722'])==1
    assert delivery.drain(api,st,path=state,now=time.time()+10)==1
    assert len(json.loads(state.read_text())['alert_outbox_v0722'])==0
    assert len(api.sent)==1
    # Simulate a monitor restart before a network recovery.
    st2={'schema':1}
    delivery.enqueue(st2,'incident/y','🚨 persisted',path=state)
    recovered=json.loads(state.read_text())
    assert delivery.drain(api,recovered,path=state,now=time.time()+11)==1
    assert len(api.sent)==2

    # A new episode after explicit recovery must never collide with the old key.
    st={'schema':1}
    enqueued=[]
    with patch.object(events,'enqueue',side_effect=lambda st,key,msg,html_mode=False: enqueued.append(key) or True):
        events.event_alert(api,st,'near0721',{'event':'RECONCILIATION_LONG_WAIT','reason':'SAME'})
        events.event_alert(api,st,'near0721',{'event':'RECONCILIATION_LONG_WAIT','reason':'SAME'})
        assert len(enqueued)==1
        events.event_alert(api,st,'near0721',{'event':'RECONCILIATION_RECOVERED'})
        events.event_alert(api,st,'near0721',{'event':'RECONCILIATION_LONG_WAIT','reason':'SAME'})
        assert len(enqueued)==3
        assert enqueued[0]!=enqueued[2]

    # 2 actual lifecycle events survive a rapid restart between 5s snapshots.
    st={'schema':1,'systemd_watch_v0722':{'near0721':{'active':True,'invocation':'old','cursor':'cur0'}}}
    calls=[]
    rows=[{'_PID':'1','MESSAGE':'Stopped OKX Grid Bot', '__CURSOR':'cur1'},
          {'_PID':'1','MESSAGE':'Started OKX Grid Bot', '__CURSOR':'cur2'}]
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'new'}), \
         patch.object(health,'_journal',return_value=rows), \
         patch.object(health,'one',side_effect=lambda api,st,key,msg: calls.append((key,msg))):
        health.service_check(api,st,{'near0721':'okx-grid-near0721.service'},
                             {'near0721':base})
    assert len(calls)==1 and 'STOPPED' in calls[0][1],calls
    assert st['systemd_watch_v0722']['near0721']['cursor']=='cur2'
    # The START message must wait for an engine heartbeat from the new invocation.
    (base/'state').mkdir(exist_ok=True)
    (base/'state'/'control_ack.json').write_text('{"phase":"RUNNING"}')
    (base/'state'/'control.json').write_text('{"mode":"RUN"}')
    (base/'state'/'runtime_stats.json').write_text(json.dumps({'last': time.time()}))
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'new'}), \
         patch.object(health,'_journal',return_value=[]), \
         patch.object(health,'one',side_effect=lambda api,st,key,msg: calls.append((key,msg))):
        health.service_check(api,st,{'near0721':'okx-grid-near0721.service'},
                             {'near0721':base})
    assert len(calls)==2 and 'STARTED — RUN' in calls[1][1]
    # No journal? The invocation ID still proves STOP, then verified new START.
    calls=[]
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'third'}), \
         patch.object(health,'_journal',return_value=None), \
         patch.object(health,'one',side_effect=lambda api,st,key,msg:calls.append(msg)):
        health.service_check(api,st,{'near0721':'okx-grid-near0721.service'},
                             {'near0721':base})
    assert len(calls)==1 and 'STOPPED' in calls[0]
    (base/'state'/'runtime_stats.json').write_text(json.dumps({'last': time.time()}))
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'third'}), \
         patch.object(health,'_journal',return_value=None), \
         patch.object(health,'one',side_effect=lambda api,st,key,msg:calls.append(msg)):
        health.service_check(api,st,{'near0721':'okx-grid-near0721.service'},
                             {'near0721':base})
    assert len(calls)==2 and 'STARTED — RUN' in calls[1]

    # PANIC: a ready final snapshot must NOT enqueue the old transient milestone.
    root=base/'panic_bot'
    (root/'state').mkdir(parents=True)
    (root/'state'/'control_ack.json').write_text('{"updated_at": 1900000000}')
    st={'v0720_observing_since':1.,'v0720_panic_seen':{'near0721':'observed_running'}}
    outgoing=[]
    with patch.object(followups,'state_file',return_value=root/'state'/'bot_state.json'), \
         patch.object(followups,'read_json',side_effect=lambda path: {
             'terminal_mode':'PANIC_DONE','terminal_snapshot':{'at_utc':'2026-10-21T12:00:00Z'}} if str(path).endswith('bot_state.json') else {'updated_at':'2026-10-21T12:00:00Z'}), \
         patch.object(followups,'_confirmed_panic',return_value=True), \
         patch.object(followups,'_final_stats',return_value='verified'), \
         patch.object(followups,'_panic_message',return_value='🏁 verified'), \
         patch.object(followups,'one',side_effect=lambda api,st,key,msg,**kw: outgoing.append((key,msg))):
        followups.panic_scan(api,st,{'near0721':root})
    assert len(outgoing)==1 and '/final/' in outgoing[0][0],outgoing

    # Missing final over 15 seconds => progress; >60 => warning too.
    st={'v0720_observing_since':1.,'v0720_panic_seen':{'near0721':'observed_running'}}
    outgoing=[]
    now=time.time()
    with patch.object(followups,'state_file',return_value=root/'state'/'bot_state.json'), \
         patch.object(followups,'read_json',side_effect=lambda path: {
             'terminal_mode':'PANIC_DONE','terminal_snapshot':{'at_utc':'2026-10-21T12:00:00Z'}} if str(path).endswith('bot_state.json') else {'updated_at':now-20}), \
         patch.object(followups,'_confirmed_panic',return_value=True), \
         patch.object(followups,'_final_stats',return_value=None), \
         patch.object(followups,'one',side_effect=lambda api,st,key,msg,**kw: outgoing.append((key,msg))):
        followups.panic_scan(api,st,{'near0721':root})
    assert len(outgoing)==1 and '/confirmed/' in outgoing[0][0]

    # No stale-file heartbeat silence; runtime_stats.json drives the alert.
    (base/'state').mkdir(exist_ok=True)
    (base/'state'/'runtime_stats.json').write_text('{"last": 1}')
    st={'engine_heartbeat_v0722':{'near0721':{'since':0.,'alarm':False,'invocation':'same'}}}
    calls=[]
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'same'}), \
         patch.object(health,'one',side_effect=lambda api,st,key,msg:calls.append(msg)):
        health.engine_heartbeat_check(api,st,{'near0721':'okx-grid-near0721.service'},
                                      {'near0721':base},now=200.)
        (base/'state'/'runtime_stats.json').write_text('{"last": 201}')
        health.engine_heartbeat_check(api,st,{'near0721':'okx-grid-near0721.service'},
                                      {'near0721':base},now=202.)
    # One stale reading must not immediately alarm or produce a recovery pair.
    assert calls==[],calls
    # Persistent stale readings do cause an alert, and recovery requires confirmation.
    st={'engine_heartbeat_v0722':{'near0721':{'since':1.,'alarm':False,'invocation':'same'}}}
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'same'}), \
         patch.object(health,'one',side_effect=lambda api,st,key,msg:calls.append(msg)):
        (base/'state'/'runtime_stats.json').write_text('{"last": 1}')
        for stamp in (300.,320.,340.):
            health.engine_heartbeat_check(api,st,{'near0721':'okx-grid-near0721.service'},
                                          {'near0721':base},now=stamp)
        assert len(calls)==1 and 'нет heartbeat' in calls[0]
        (base/'state'/'runtime_stats.json').write_text('{"last": 341}')
        for stamp in (345.,355.,365.):
            health.engine_heartbeat_check(api,st,{'near0721':'okx-grid-near0721.service'},
                                          {'near0721':base},now=stamp)
        assert len(calls)==2 and 'BACK TO NORMAL' in calls[1]

    assert views.price_digits({'tick_size':'0.000001'})==6
    assert views.price_digits({'tick_size':'0.1'})==2
    assert views.price_digits({'tick_size':'0.00000001'})==8
    # Telegram MUST NOT re-parse the v0.7.21 engine's rounded $0.00 BID.
    micro=base/'micro_bot'
    (micro/'state').mkdir(parents=True)
    micro_cfg={'instrument':'DOOD-USDT-SWAP','version':'0.7.21','tick_size':'0.000001'}
    (micro/'config.json').write_text(json.dumps(micro_cfg))
    (micro/'state/bot_state.json').write_text(json.dumps({'instrument':'DOOD-USDT-SWAP',
        'cycles':{'1':{'state':'TP_WAIT','filledBuy':'1000','filledTP':'0','buyPx':'0.002580'}}}))
    sample=1999999900.0
    micro_stats={'complete':True,'instrument':'DOOD-USDT-SWAP',
        'generated_at_unix':sample, 'report':"""Текущий BID: $0.00
ИТОГОВЫЙ PnL: -$1.04
Открытая позиция: 27000 DOOD"""}
    (micro/'state/stats.json').write_text(json.dumps(micro_stats))
    anchor={'schema':1,'instrument':'DOOD-USDT-SWAP','at':sample,'bid':'0.002304'}
    (micro/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
    text=views.status_one('micro_bot',micro,'1h 45min')
    assert '$0.002304' in text and '−10.70%' in text, text
    assert '$0.000000' not in text and '−100.00%' not in text, text
    anchor['at']=sample-600
    (micro/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
    text=views.status_one('micro_bot',micro,'1h 45min')
    assert 'Текущий bid: н/д' in text and '−100.00%' not in text, text
    anchor['at']=sample;anchor['bid']='0'
    (micro/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
    text=views.status_one('micro_bot',micro,'1h 45min')
    assert 'Текущий bid: н/д' in text and '−100.00%' not in text, text
    anchor['bid']='0.002304';anchor['instrument']='NEAR-USDT-SWAP'
    (micro/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
    text=views.status_one('micro_bot',micro,'1h 45min')
    assert 'Текущий bid: н/д' in text and '−100.00%' not in text, text
    anchor['instrument']='DOOD-USDT-SWAP';anchor['bid']='0.002304'
    (micro/'state/stats_financial_anchor.json').write_text(json.dumps(anchor))
    assert views.verified_snapshot_bid(micro,micro_cfg,micro_stats)==views.number('0.002304')

    from okx_setup_presets import ASSETS
    assert ASSETS['Doodles']=='DOOD' and 'APR' not in ASSETS.values()

print('PASS: Telegram durable retry / restart / repeated incident / PANIC single message / heartbeat / micro-price / DOOD only')
