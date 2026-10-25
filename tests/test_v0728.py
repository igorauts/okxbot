"""v0.7.28 regression — strictly local/no exchange/services/trading writes."""
import ast
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_latency_rollup import (MinuteWriter, add_row, read_rollups,
                               distribution, segment_dir, SECONDS)
from okx_version import ENGINE_VERSION, RELEASE_VERSION, STATE_SCHEMA
from historical import single_transition_upgrader as upgrader
import okx_telegram_health as health

assert __import__("okx_release_contract").check()
assert (upgrader.OLD_VERSION,upgrader.NEW_VERSION)==('0.7.27','0.7.28')
assert upgrader.OLD_ENGINE_SHA256=='e0d66fc153a2a20d2b9c5df6e79915b73deb1cbd694eae1d153f305284b02717'
assert upgrader.OLD_ENGINE_SHA256 != __import__('hashlib').sha256((ROOT/'okx_bot.py').read_bytes()).hexdigest()
print('PASS 0728 version and pinned v0.7.27 published-engine SHA256')

# Regression: do not lose an ongoing member when a grouped alert contracts
# from 2/3 bots to one bot. Resuming monitor from serialized persisted state
# must not create a second incident or false common recovery.
with tempfile.TemporaryDirectory() as td:
    bases={};services={}
    for bot in ('near','btc','dood'):
        root=Path(td)/bot;(root/'state').mkdir(parents=True)
        (root/'state/runtime_stats.json').write_text('{"last":1}')
        bases[bot]=root;services[bot]='okx-grid-'+bot+'.service'
    sent=[];st={};props={name:('active','process-1') for name in services}
    def fake_show(unit):
        name=next(key for key,val in services.items() if val==unit)
        active,inv=props[name]
        return {'ActiveState':active,'InvocationID':inv}
    def tick(now, fresh=()):
        for name in fresh:
            (bases[name]/'state/runtime_stats.json').write_text(json.dumps({'last':now}))
        health.engine_heartbeat_check(None,st,services,bases,now=now)
    with patch.object(health,'_show',side_effect=fake_show), \
         patch.object(health,'one',side_effect=lambda api,state,key,msg:sent.append((key,msg))):
        # Dood initially healthy; Near and BTC become confirmed fault.
        for now in (100,110,120,130): tick(now,('dood',))
        assert st['common_engine_stall_v0727']['active']
        assert set(st['common_engine_stall_v0727']['members'])=={'btc','near'}
        assert sum(k.startswith('heartbeat/common/') and '/update/' not in k and '/recovered/' not in k for k,_ in sent)==1
        # Third bot fails and joins the open incident.
        for now in (180,190,200,205): tick(now)
        assert set(st['common_engine_stall_v0727']['members'])=={'btc','dood','near'}
        assert any('Присоединились: dood' in msg for _,msg in sent)
        # Simulate persisted Telegram monitor restart.
        st=json.loads(json.dumps(st))
        # Near and BTC recover but dood stays stalled.
        for now in (220,225,235,240): tick(now,('near','btc'))
        assert st['common_engine_stall_v0727']['active']
        assert st['common_engine_stall_v0727']['members']==['dood']
        assert any('Всё ещё без heartbeat: dood' in msg for _,msg in sent)
        assert not any('/recovered/' in k for k,_ in sent)
        # Inactive systemd is NOT a fresh heartbeat and cannot close common.
        props['dood']=('inactive','process-1')
        tick(245,('near','btc'))
        assert st['common_engine_stall_v0727']['active']
        # An omitted dynamic service record also cannot signal recovery.
        removed=services.pop('dood')
        tick(247,('near','btc'))
        assert st['common_engine_stall_v0727']['members']==['dood']
        services['dood']=removed
        # Restarted, no new heartbeat yet — still not recovered.
        props['dood']=('active','process-2')
        tick(250,('near','btc'))
        assert st['common_engine_stall_v0727']['members']==['dood']
        # At least 15s and 3 healthy samples: valid final recovery.
        for now in (255,260,270,275):tick(now,('near','btc','dood'))
        assert st['common_engine_stall_v0727']['active'] is False
        assert sum('/recovered/' in k for k,_ in sent)==1
        count=len(sent);tick(280,('near','btc','dood'))
        assert len(sent)==count
print('PASS 0728 common heartbeat: joins, partial recovery, persisted restart, systemd stop, final recovery')

# Single-member faults continue to send their own notifications and restore.
with tempfile.TemporaryDirectory() as td:
    root=Path(td);(root/'state').mkdir()
    hb=root/'state/runtime_stats.json';hb.write_text('{"last":1}')
    got=[];st={}
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'one'}), \
         patch.object(health,'one',side_effect=lambda api,state,key,msg:got.append((key,msg))):
        for now in (100,110,120,130):health.engine_heartbeat_check(None,st,{'x':'u'},{'x':root},now=now)
        assert sum(k.startswith('heartbeat/down/') for k,_ in got)==1
        for now in (150,155,165,170):
            hb.write_text(json.dumps({'last':now}))
            health.engine_heartbeat_check(None,st,{'x':'u'},{'x':root},now=now)
        assert sum(k.startswith('heartbeat/recovery/') for k,_ in got)==1
print('PASS 0728 singleton heartbeat incidents remain independently actionable')

# Append-only minute segments: no rewriting of previous line/files, bounded 24h
# API view, clean legacy migration, tail repair, and hourly cleanup.
with tempfile.TemporaryDirectory() as td:
    path=Path(td)/'latency_24h_rollups.jsonl'
    now=int(time.time()//3600)*3600 + 25*60;minute=int(now//60)  # deterministic intra-hour fixture
    row={'phase':'http','ms':23,'method':'GET','route':'/api/v5/account/positions'}
    legacy={'schema':1,'minute':minute-4,'series':{}}
    add_row({'phase':'http','ms':44,'method':'GET','route':'/api/v5/account/positions'},legacy['series'])
    path.write_text(json.dumps(legacy)+'\n')
    legacy_bytes=path.read_bytes()
    w=MinuteWriter(path)
    w.minute=minute
    for _ in range(10):add_row(row,w.buckets)
    w.flush()
    directory=segment_dir(path);hourfile=directory/(str(minute//60)+'.jsonl')
    assert hourfile.exists() and path.read_bytes()==legacy_bytes
    old=hourfile.read_bytes()
    w.minute=minute+1
    for _ in range(5):add_row(row,w.buckets)
    w.flush()
    assert hourfile.read_bytes().startswith(old)
    assert len(hourfile.read_bytes())>len(old)
    assert path.read_bytes()==legacy_bytes
    records=read_rollups(path,now)
    assert len(records)==3
    assert distribution(records,'http',now)['n']==16
    # Interrupted write: strip ONLY last torn record before next append.
    with hourfile.open('ab') as file:file.write(b'{"incomplete":')
    w2=MinuteWriter(path);w2.minute=minute+2;add_row(row,w2.buckets);w2.flush()
    assert b'incomplete' not in hourfile.read_bytes()
    assert distribution(read_rollups(path,(minute+2)*60),'http',(minute+2)*60)['n']==17
    assert len(read_rollups(path,now+SECONDS+3600))==0
    # Old hourly segment is evicted, but current hour remains available.
    stale=directory/(str(int((now-SECONDS-7200)//3600))+'.jsonl')
    stale.write_text(json.dumps(legacy)+'\n')
    assert stale.exists()
    w2._cleanup(now+3600)
    assert not stale.exists()
    assert len(list(directory.glob('*.jsonl')))<=2
print('PASS 0728 latency: append-only, legacy migration, 24h bounds, torn tail repair, hourly cleanup')

# Only diagnostic events gain semantic fields; retain legacy price field for
# existing readers. No strategy/price/money calculation changes permitted.
engine=ast.parse((ROOT/'okx_bot.py').read_text())
assert 'price' in (ROOT/'okx_bot.py').read_text()
for event in ('ATTACHED_TP_DISCOVERED','TP_VIRTUAL','TP_BUFFER_CLEANUP','TP_FILLED'):
    calls=[node for node in ast.walk(engine) if isinstance(node,ast.Call)
           and isinstance(node.func,ast.Name) and node.func.id=='log'
           and node.args and isinstance(node.args[0],ast.Constant) and node.args[0].value==event]
    if event=='ATTACHED_TP_DISCOVERED':
        assert "else 'ATTACHED_TP_DISCOVERED'" in (ROOT/'okx_bot.py').read_text()
        continue
    assert calls,event
    assert {'price','buyPx','tpPx','cycleId'}<=set(k.arg for k in calls[0].keywords),event
print('PASS 0728 TP diagnostics: legacy price plus explicit buyPx / tpPx / cycleId')
print('OFFLINE v0.7.28 FOCUSED PASS — no exchange, VPS, services or LIVE writes')
