"""Focused v0.7.25 offline P0/P1 fault injection; NEVER accesses live APIs."""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_version import RELEASE_VERSION,ENGINE_VERSION
assert (RELEASE_VERSION,ENGINE_VERSION)==('0.7.26','0.7.26')
import okx_panic_all as panic
import okx_telegram_delivery as delivery
import okx_telegram_events as events
import okx_telegram_health as health
import okx_panic_cleanup as cleanup
import okx_observers as observers
import okxbot

class Permanent(Exception):telegram_permanent=True
class DummyTelegram:
    def __init__(self):self.sent=[]
    def send(self,text,parse_mode=None):
        if text=='PERMANENT':raise Permanent('HTTP 400, proven malformed')
        if text=='TRANSIENT':raise TimeoutError('network ambiguity')
        self.sent.append(text)

with tempfile.TemporaryDirectory() as td:
    base=Path(td);path=base/'outbox.json';st={'schema':1}
    api=DummyTelegram()
    for key,txt in [('x','PERMANENT'),('y','critical NEXT')]:
        assert delivery.enqueue(st,key,txt,path=path)
    assert delivery.drain(api,st,path=path,now=10**10)==1
    assert api.sent==['critical NEXT']
    assert st['alert_dead_letter_total_v0725']==1
    assert len(st['alert_outbox_v0722'])==0
    assert delivery.drain(api,st,path=path,now=10**10)==0
    assert not delivery.enqueue(st,'x','PERMANENT',path=path)
    delivery.enqueue(st,'z','TRANSIENT',path=path)
    delivery.enqueue(st,'q','not sent before transient',path=path)
    assert delivery.drain(api,st,path=path,now=10**10)==0
    assert st['alert_outbox_v0722'][0]['key']=='z'
    assert api.sent==['critical NEXT']
    # State size bound: no queued dedupe may be pruned.
    from time import time
    st={'incident_sent':{f'old/{i}':0.0 for i in range(20001)}}
    delivery.prune_sent(st,time())
    assert len(st['incident_sent'])<=delivery.MAX_SENT_KEYS
    assert all(evt in events.EVENT_ALARMS for evt in (
        'PANIC_MARKET_REJECTED','PANIC_RETRY_EXHAUSTED',
        'EMERGENCY_POSITION_UNRESOLVED','EMERGENCY_BUY_IDENTITY_CONFLICT'))
print('PASS Telegram: permanent dead-letter unblocks critical alerts; transient retains order, dedupe bounded')

with tempfile.TemporaryDirectory() as td:
    root=Path(td);(root/'state').mkdir()
    (root/'state/control_ack.json').write_text(json.dumps({
        'mode':'PANIC','phase':'PANIC_MANUAL_REVIEW',
        'updated_at':'2026-10-22T20:00:00Z','remaining_position':'11'}))
    got=[];st={}
    with patch.object(health,'one',side_effect=lambda api,st,key,text:got.append(text)):
        health.panic_ack_check(None,st,{'bot':'service'},{'bot':root})
        health.panic_ack_check(None,st,{'bot':'service'},{'bot':root})
        (root/'state/control_ack.json').write_text(json.dumps({
            'mode':'PANIC','phase':'PANIC_MANUAL_REVIEW',
            'updated_at':'2026-10-22T20:00:01Z','remaining_position':'10'}))
        health.panic_ack_check(None,st,{'bot':'service'},{'bot':root})
    assert len(got)==1 and '11' in got[0]
    (root/'state/control_ack.json').write_text(json.dumps({
        'mode':'PANIC','phase':'PANIC_DONE','updated_at':'2026-10-22T20:01:00Z'}))
    with patch.object(health,'one',side_effect=lambda api,st,key,text:got.append(text)):
        health.panic_ack_check(None,st,{'bot':'service'},{'bot':root})
    assert len(got)==1
print('PASS critical PANIC ACK: independently observable manual review; no duplicate spam')

with tempfile.TemporaryDirectory() as td:
    root=Path(td)/'x';(root/'state').mkdir(parents=True)
    cfg={'version':'0.7.23.1','instrument':'BTC-USDT-SWAP'}
    st={'version':'0.7.23.1','instrument':'BTC-USDT-SWAP',
        'terminal_mode':'PANIC_DONE','terminal_snapshot':{'position_contracts':'0','mode':'PANIC_DONE'}}
    ack={'mode':'PANIC','phase':'PANIC_DONE','remaining_position':'0'}
    (root/'config.json').write_text(json.dumps(cfg))
    (root/'state/bot_state.json').write_text(json.dumps(st))
    (root/'state/control_ack.json').write_text(json.dumps(ack))
    (root/'state/control.json').write_text('{"mode":"PANIC"}')
    assert cleanup.confirmed_panic(root)
    cfg['version']='0.7.99.1';st['version']='0.7.99.1'
    (root/'config.json').write_text(json.dumps(cfg))
    (root/'state/bot_state.json').write_text(json.dumps(st))
    assert not cleanup.confirmed_panic(root)
print('PASS PANIC cleanup: exact historical 0.7.23.1 only; arbitrary 4-part versions fail closed')

with tempfile.TemporaryDirectory() as td:
    base=Path(td);service='okx-grid-solo';root=base/'solo';root.mkdir();(root/'state').mkdir()
    cfg={'version':'0.7.22','instrument':'NEAR-USDT-SWAP'}
    st={'version':'0.7.22','instrument':'NEAR-USDT-SWAP','schema_version':1}
    (root/'config.json').write_text(json.dumps(cfg))
    (root/'state/bot_state.json').write_text(json.dumps(st))
    (root/'okx_bot.py').write_text('')
    assert observers.supports_versionless(root,service,base)
    st['version']='0.7.21';(root/'state/bot_state.json').write_text(json.dumps(st))
    assert not observers.supports_versionless(root,service,base)
    st['version']='0.7.22';cfg['version']='0.7.25.1'
    (root/'state/bot_state.json').write_text(json.dumps(st))
    (root/'config.json').write_text(json.dumps(cfg))
    assert not observers.supports_versionless(root,service,base)
print('PASS observer ABI: 0.7.22 clean schema eligible; inconsistent/fake maintenance version rejected')

with tempfile.TemporaryDirectory() as td:
    base=Path(td);registry=base/'.okxbot_registry.json';units=base/'systemd';units.mkdir()
    names=['alpha','bravo','charlie']
    entries={}
    for name,pair in zip(names,['BTC','NEAR','DOOD']):
        root=base/name;root.mkdir();(root/'state').mkdir();(root/'.venv').mkdir()
        (root/'config.json').write_text(json.dumps({'version':'0.7.25','instrument':pair+'-USDT-SWAP'}))
        (root/'state/bot_state.json').write_text(json.dumps({
            'version':'0.7.25','instrument':pair+'-USDT-SWAP','schema_version':1}))
        (root/'okx_bot.py').write_text('')
        if name!='charlie':(root/'.env').write_text('OKX_API_KEY=fake_fixture_not_real\n')
        entries[name]={'service':'okx-grid-'+name,'path':str(root)}
    entries['broken']=None  # inactive corrupt registry record should not veto.
    registry.write_text(json.dumps(entries))
    def sysctl(cmd,**kwargs):
        if cmd[1]=='list-units':
            return SimpleNamespace(stdout=''.join(
                f'okx-grid-{n}.service loaded active running fixture\n' for n in names))
        if cmd[1]=='show':
            name=cmd[2].removeprefix('okx-grid-').removesuffix('.service')
            root=base/name
            return SimpleNamespace(stdout=f'ActiveState=active\nMainPID={names.index(name)+100}\n'
                f'WorkingDirectory={root}\nExecStart={root}/.venv/bin/python -u {root}/okx_bot.py\n')
        raise AssertionError(cmd)
    with patch.object(panic.subprocess,'run',side_effect=sysctl):
        ready,skips,identity=panic.survey(base,registry,units,lambda entry:True)
        assert {x[0] for x in ready}=={'alpha','bravo'}
        assert len(identity)==3 and any('charlie' in x[0] for x in skips)
        assert any('broken' in x[0] for x in skips)
        # One bot already in PANIC: independent eligible bots still proceed.
        (base/'bravo/state/control.json').write_text('{"mode":"PANIC"}')
        ready,skips,_=panic.survey(base,registry,units,lambda entry:True)
        assert [x[0] for x in ready]==['alpha']
        (base/'bravo/state/control.json').unlink()
        # Duplicate NET instrument: strict global refusal.
        cfg={'version':'0.7.25','instrument':'BTC-USDT-SWAP'}
        (base/'bravo/config.json').write_text(json.dumps(cfg))
        try:panic.survey(base,registry,units,lambda entry:True)
        except RuntimeError as exc:assert 'Duplicate NET' in str(exc)
        else:raise AssertionError('Duplicate NET pair accepted')
        # Unprovable instrument on ACTIVE service still globally rejects.
        (base/'bravo/config.json').write_text('{corrupt')
        try:panic.survey(base,registry,units,lambda entry:True)
        except RuntimeError as exc:assert 'Cannot prove instrument' in str(exc)
        else:raise AssertionError('Unknown active pair accepted')
print('PASS PANIC ALL: independent active candidates, skip failed/malformed, block duplicate/unknown NET')

with tempfile.TemporaryDirectory() as td:
    base=Path(td);registry=base/'.okxbot_registry.json';units=base/'units';units.mkdir()
    root=base/'good';root.mkdir();(root/'config.json').write_text('{"instrument":"BTC-USDT-SWAP"}')
    (units/'okx-grid-good.service').write_text('')
    registry.write_text(json.dumps({'good':{'path':str(root),'service':'okx-grid-good'},'bad':42}))
    with patch.object(okxbot,'BASE',base),patch.object(okxbot,'REGISTRY',registry),patch.object(okxbot,'SYSTEMD_DIR',units):
        rows,warnings=okxbot.display_registry()
        assert list(rows)==['good'] and len(warnings)==1
        assert len(json.loads(registry.read_text()))==2  # never drop bad entry on disk
print('PASS read-only registry: malformed record isolated, source registry unchanged')

# End-to-end manager dispatch: a skipped independent third bot must not veto
# two valid durable PANIC intents. Simulate all read-only systemd snapshots.
plan=(('alpha','BTC-USDT-SWAP','okx-grid-alpha.service',101,'RUN'),
      ('bravo','NEAR-USDT-SWAP','okx-grid-bravo.service',102,'SOFT'))
skips=(('okx-grid-charlie.service','missing credentials'),)
identity=(('okx-grid-alpha.service','active','101','BTC-USDT-SWAP'),
          ('okx-grid-bravo.service','active','102','NEAR-USDT-SWAP'),
          ('okx-grid-charlie.service','active','103','DOOD-USDT-SWAP'))
sent=[]
def current_mode(entry):return 'RUN' if entry=='alpha' else 'SOFT'
def fake_show(args,**kw):
    if args[0]=='systemctl' and args[1]=='show':
        return SimpleNamespace(stdout=('101\n' if 'alpha' in args[2] else '102\n'))
    raise AssertionError('Unexpected OS command: '+str(args))
with patch.object(okxbot,'_panic_all_preflight',wraps=okxbot._panic_all_preflight) as wrapped, \
     patch.object(panic,'survey',return_value=(plan,skips,identity)), \
     patch.object(okxbot,'find_entry',side_effect=lambda name:(name,name)), \
     patch.object(okxbot,'service_running',return_value=True), \
     patch.object(okxbot,'read_control',side_effect=lambda entry:{'mode':current_mode(entry)}), \
     patch.object(okxbot,'write_control',side_effect=lambda entry,mode:sent.append((entry,mode))), \
     patch.object(okxbot.subprocess,'run',side_effect=fake_show), \
     patch.object(okxbot.sys,'stdin',SimpleNamespace(isatty=lambda:True)), \
     patch.object(okxbot.sys,'stdout',SimpleNamespace(isatty=lambda:True,write=lambda x:None,flush=lambda:None)), \
     patch.object(okxbot.sys,'stderr',SimpleNamespace(write=lambda x:None,flush=lambda:None)), \
     patch('builtins.input',return_value='PANIC ALL'):
    # The wrapper's attributes are not used in this fixture, but real
    # preflight recomputes both plan and skip/universe before any write.
    try:okxbot.cmd_panic_all()
    except RuntimeError as exc:assert 'частично' in str(exc)
    else:raise AssertionError('Partial PANIC ALL was returned as fully successful')
assert sent==[('alpha','PANIC'),('bravo','PANIC')]
print('PASS PANIC ALL dispatch: independent durable intents sent; skipped bot still reports partial failure')

# In-repo updater must not depend on hardcoded live bot names, refuse stop by default.
# Regression for LIVE RC1 blocker: the v0.7.25 engine's SHA256 must
# come from the PUBLISHED v0.7.25 release asset, not the candidate v0.7.26.
# Independently computed from okx_grid_v0725_final.zip/okx_bot.py.
import hashlib
import okx_live_upgrade as upgrader
assert upgrader.OLD_ENGINE_SHA256 == '0c1530e1a8b56cda2f27f3427680da522b8f1682de244dcc62faa637fb1f2f68'
assert upgrader.OLD_ENGINE_SHA256 != hashlib.sha256((ROOT/'okx_bot.py').read_bytes()).hexdigest()
assert 'instance_check(bot,cfg[' in (ROOT/'okx_live_upgrade.py').read_text()
print('PASS updater: pinned original v0.7.25 published engine hash; preflight verifies actual code before any stop')

upgrade=ast.parse((ROOT/'okx_live_upgrade.py').read_text())
assert any(isinstance(n,ast.FunctionDef) and n.name=='upgrade_bot' for n in upgrade.body)
assert '--confirm-live-stop' in (ROOT/'okx_live_upgrade.py').read_text()
assert '--sha256' in (ROOT/'okx_live_upgrade.py').read_text()
print('PASS updater: in-repo controlled single-instance installer, pinned package and explicit stop consent')
# Exercise actual installer stage checksum and Telegram-only transactional install.
# No systemctl or live filesystem access is permitted in this test.
import zipfile,hashlib,stat
import okx_live_upgrade as upgrader
with tempfile.TemporaryDirectory() as td:
    temp=Path(td);pkg=temp/'candidate.zip';tg=temp/'telegram';tg.mkdir()
    backups=temp/'backups';lock=temp/'upgrade.lock'
    with zipfile.ZipFile(pkg,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for f in sorted(ROOT.rglob('*')):
            # Only files allowed in the release archive. A Git checkout has
            # .git/ and may have test caches: neither belongs in the fixture.
            rel=f.relative_to(ROOT)
            if not f.is_file() or f.is_symlink():continue
            if not (len(rel.parts)==1 or
                    (len(rel.parts)==2 and rel.parts[0]=='tests')):continue
            if '__pycache__' in rel.parts or f.suffix=='.pyc':continue
            n=rel.as_posix()
            info=zipfile.ZipInfo(n)
            info.create_system=3
            info.external_attr=(stat.S_IFREG|0o644)<<16
            z.writestr(info,f.read_bytes())
    digest=hashlib.sha256(pkg.read_bytes()).hexdigest()
    files=sorted(p.name for p in ROOT.glob('okx_telegram_*.py'))
    assert len(files)>=5
    for name in files:(tg/name).write_text('PREVIOUS MODULE\n')
    ops=[]
    def fake_operation(*cmd,**kw):
        ops.append(cmd)
        if cmd[:2]==('systemctl','show'):
            return SimpleNamespace(stdout='active\n')
        if cmd[:2]==('systemctl','restart'):
            return SimpleNamespace(stdout='')
        raise AssertionError(cmd)
    with patch.object(upgrader,'PACKAGE',pkg), \
         patch.object(upgrader,'STAGE',ROOT), \
         patch.object(upgrader,'PACKAGE_SHA256',digest), \
         patch.object(upgrader,'TELEGRAM_ROOT',tg), \
         patch.object(upgrader,'BACKUPS',backups), \
         patch.object(upgrader,'run',side_effect=fake_operation):
        assert upgrader.stage_check()
        upgrader.install_telegram()
        assert (tg/files[0]).read_bytes()==(ROOT/files[0]).read_bytes()
        assert len(ops)==2 and ops[0][:2]==('systemctl','restart')
        assert all('okx-grid-' not in ' '.join(cmd) for cmd in ops)
    # No authorization to stop a trading service without explicit flag.
    argv=['okx_live_upgrade.py','bot','--bot','near0721','--package',str(pkg),
          '--stage',str(ROOT),'--sha256',digest]
    with patch.object(upgrader.os,'geteuid',return_value=0), \
         patch.object(upgrader,'LOCK',lock),patch.object(upgrader.sys,'argv',argv), \
         patch.object(upgrader,'upgrade_bot',side_effect=AssertionError('called')):
        try:upgrader.main()
        except RuntimeError as exc:assert 'without --confirm-live-stop' in str(exc)
        else:raise AssertionError('LIVE stop authorized without operator flag')
print('PASS updater: pinned ZIP, real Telegram file-copy transaction, trading-service isolation, explicit LIVE stop guard')

print('OFFLINE v0.7.25 FOCUSED PASS — no OKX/API/systemd calls outside mocks')
