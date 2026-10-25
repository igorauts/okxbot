"""v0.7.27 reliability/observability offline regressions: NO EXCHANGE, NO LIVE."""
import ast
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_version import ENGINE_VERSION,RELEASE_VERSION,STATE_SCHEMA
from okx_latency_rollup import MinuteWriter,add_row,distribution,fresh_rows,read_rollups
from okx_execution_observer import sample_state,Observer
from okx_pnl_diagnostics import inspect
import okx_telegram_health as health

assert __import__("okx_release_contract").check()
# The upgrader MUST recognize LIVE v0.7.27, not only the new candidate.
from historical import single_transition_upgrader as upgrader
assert upgrader.OLD_VERSION == '0.7.27' and upgrader.NEW_VERSION == '0.7.28'
assert upgrader.OLD_VERSION != upgrader.NEW_VERSION
assert upgrader.OLD_ENGINE_SHA256 == 'e0d66fc153a2a20d2b9c5df6e79915b73deb1cbd694eae1d153f305284b02717'
assert health.HEARTBEAT_STALE_SEC==45 and health.HEARTBEAT_CONFIRM_SEC==15
print('PASS 0727 version+confirmed stale heartbeat timing')

# Uncertain PLACING must be tracked but never called exchange-confirmed BUY.
st={'cycles':{'a':{'state':'BUY_LIVE','buyPx':'1200','tpOrders':[]},
              'b':{'state':'PLACING','buyPx':'1201','tpOrders':[]},
              'c':{'state':'VIRTUAL_TP','tpOrders':[]}}}
assert sample_state(st)['buy']==1 and sample_state(st)['placing']==1
print('PASS 0727 PLACING is durable uncertain intent, not acknowledged BUY')

# Exactness of state machine unchanged, except explicit post-batch save.
source=(ROOT/'okx_bot.py').read_text();tree=ast.parse(source)
assert 'if placed:' in source and 'placement batch' in source and 'save(st)' in source
assert "if place(st,price):placed+=1" in source
assert 'protected_signature!=last_protected_signature' in source
assert "log('TRADING_LOOP_LONG_PASS'" in source
assert "BUY_CANCEL_PROTECT_STEPS = 5" in source
api=(ROOT/'okx_api.py').read_text()
assert 'attempts=4 if method==\'GET\' else 1' in api
assert "HTTPSConnection('www.okx.com',timeout=12" in api
assert "status=http_status,attempt=attempt+1" in api
print('PASS 0727 GET/POST safety and trade parameters unchanged; positive BUY ACK saved')

# Rolling 24h histogram: merge route, ensure old rows evicted, P99 upper bound honest.
now=time.time();minute=int(now//60)
with tempfile.TemporaryDirectory() as td:
    file=Path(td)/'latency_24h_rollups.jsonl'
    writer=MinuteWriter(file)
    rec={'phase':'http','ms':22,'method':'GET','route':'/api/v5/account/positions'}
    writer.minute=minute
    for _ in range(20):writer.observe(rec)
    writer.flush()
    loaded=read_rollups(file,now)
    d=distribution(loaded,'http',now,route='/api/v5/account/positions')
    assert d['n']==20 and d['p95_upper_ms']==50 and d['max_ms']==22
    assert distribution(loaded,'http',now,route='/api/v5/trade/order')['n']==0
    old={'schema':1,'minute':minute-1500,'series':loaded[0]['series']}
    assert len(fresh_rows([old,*loaded],now))==1
    assert distribution([old],'http',now)['n']==0
    assert distribution(loaded,'http',now,window_seconds=3600)['n']==20
    assert distribution(loaded,'http',now,window_seconds=1)['n'] in (0,20)
print('PASS 0727 route-level histogram, bounded 24h retention, percentiles approximate')

# Temporary filesystem observer: only execution writes detailed timeline/snapshots.
with tempfile.TemporaryDirectory() as td:
    root=Path(td)/'zec0724';(root/'state').mkdir(parents=True)
    (root/'config.json').write_text(json.dumps({'instrument':'ZEC-USDT-SWAP',
        'active_buy_orders':1,'real_tp_target':1,'grid_step':'1'}))
    (root/'state/bot_state.json').write_text(json.dumps({
        'schema_version':1,'instrument':'ZEC-USDT-SWAP','cycles':{}}))
    o=Observer(root,'okx-grid-zec0724','execution')
    assert o.counters=={}
    o.counters['BUY_FILLED']=17
    o.summary()
    assert (root/'diagnostics/execution/daily_counters.json').exists()
    newer=Observer(root,'okx-grid-zec0724','execution')
    assert newer.counters['BUY_FILLED']==17
    new_br=Observer(root,'okx-grid-zec0724','burst')
    new_br.snapshot()
    assert not (root/'diagnostics/burst/snapshots.jsonl').exists()
    newer.snapshot()
    assert (root/'diagnostics/execution/snapshots.jsonl').exists()
    newest=Observer(root,'okx-grid-zec0724','execution')
    assert newest.counters['BUY_FILLED']==17
print('PASS 0727 observer UTC daily persistence, deduplicated burst snapshots')

# PnL diagnostics: no mutations of report/ledger; mismatch disclosed as such.
with tempfile.TemporaryDirectory() as td:
    root=Path(td);(root/'state').mkdir()
    p=root/'state/pnl.sqlite';db=sqlite3.connect(p)
    db.execute('CREATE TABLE accounted (id TEXT PRIMARY KEY,kind TEXT,net TEXT,data TEXT)')
    db.execute('INSERT INTO accounted VALUES (?,?,?,?)',('1','CLOSED','0.010084','{}'))
    db.commit();db.close()
    rpt=root/'state/stats.json';rpt.write_text(json.dumps({
        'complete':True,'generated_at_unix':now,'performance':{'grid_profit':'0'}}))
    before=[p.stat().st_mtime_ns,rpt.stat().st_mtime_ns]
    out=inspect(root,now=now+5)
    assert out['delta']=='0.010084' and out['closed_ledger_rows']==1
    assert before==[p.stat().st_mtime_ns,rpt.stat().st_mtime_ns]
print('PASS 0727 PnL ledger/report drift surfaced read-only, never auto-corrected')

# Cross-bot incident grouping is one event per continuing multi-bot episode.
with tempfile.TemporaryDirectory() as td:
    bases={};services={}
    for bot in ('near0721','btc0721'):
        root=Path(td)/bot;(root/'state').mkdir(parents=True)
        (root/'state/runtime_stats.json').write_text('{"last":1}')
        bases[bot]=root;services[bot]='okx-grid-'+bot+'.service'
    st={};sent=[]
    with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'test'}), \
         patch.object(health,'one',side_effect=lambda api,state,key,msg:sent.append((key,msg))):
        for stamp in (100,110,120,130):
            health.engine_heartbeat_check(None,st,services,bases,now=stamp)
        assert sum('heartbeat/common/' in k and 'recovered' not in k for k,_ in sent)==1
        assert st['common_engine_stall_v0727']['active']
        for root in bases.values():
            (root/'state/runtime_stats.json').write_text('{"last":150}')
        for stamp in (150,155,165,170):
            health.engine_heartbeat_check(None,st,services,bases,now=stamp)
        assert sum('heartbeat/common/recovered/' in k for k,_ in sent)==1
print('PASS 0727 correlated missing-heartbeat incident is debounced')

# System modules import without the exchange or root permissions.
for name in ('okx_latency_ui','okx_diagnostics_ui','okx_pnl_diagnostics',
             'collect_okx_diagnostics_24h','okx_observers'):
    __import__(name)
print('OFFLINE v0.7.27 FOCUSED PASS — no API calls, trading writes or systemd')
