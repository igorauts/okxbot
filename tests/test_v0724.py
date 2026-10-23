"""v0.7.25 offline P0 and display regressions; no exchange, no live files."""
import ast
import json
import sys
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import okxbot
from okx_version import RELEASE_VERSION,ENGINE_VERSION,STATE_SCHEMA,validate_version_ids
from okx_latency_ui import report as latency_report
from okx_diagnostics_ui import fifo_report,execution_report
from okx_stats_ui import format_stats
from okx_positions_fifo import AccountPositionsFIFO

D=lambda n:Decimal(str(n if n is not None and n!='' else '0'))
assert (RELEASE_VERSION,ENGINE_VERSION,STATE_SCHEMA)==('0.7.25','0.7.25',1)
assert okxbot.latest_engine()==('okx_bot.py','0.7.25')
assert validate_version_ids('0.7.25.1','0.7.25')
for invalid in [('0.7.25.1','0.7.25.1'),('0.7.25.1','0.7.25.1'),('0.7.25','0.7.24')]:
    try:validate_version_ids(*invalid)
    except RuntimeError:pass
    else:raise AssertionError('Version mismatch allowed: '+str(invalid))
# Maintenance tag simulation, with a REAL provisional instance package.
with tempfile.TemporaryDirectory() as d:
    folder=Path(d);(folder/'config.json').write_text('{"version":"0.7.25.1","instrument":"BTC-USDT-SWAP"}')
    with patch.object(okxbot,'RELEASE_VERSION','0.7.25.1'):
        engine,version=okxbot.provision_latest_engine(folder)
        assert engine=='okx_bot.py' and version=='0.7.25'
        assert json.loads((folder/'config.json').read_text())['version']=='0.7.25'
        assert (folder/'okx_version.py').is_file()
        from okx_state import empty
        created=empty(version,'BTC-USDT-SWAP')
        assert created['version']=='0.7.25'
    forged=folder/'wrong_source';forged.mkdir()
    shutil.copy2(ROOT/'okx_bot.py',forged/'okx_bot.py')
    bad=json.loads((ROOT/'engine_manifest.json').read_text());bad['current_version']='0.7.25.1'
    (forged/'engine_manifest.json').write_text(json.dumps(bad))
    try:okxbot.latest_engine(forged)
    except RuntimeError:pass
    else:raise AssertionError('Bad release/engine mismatch not rejected')
    # Reject missing engine dependency, not just mismatched versions.
    bad['current_version']='0.7.25';(forged/'engine_manifest.json').write_text(json.dumps(bad))
    try:okxbot.latest_dependencies('okx_bot.py','0.7.25',forged)
    except RuntimeError:pass
    else:raise AssertionError('Missing engine dependency allowed')
print('PASS P0: new bot engine v0.7.25 despite release v0.7.25.1, malformed sources fail closed')

# Extract a pure stats function without importing the LIVE engine module.
tree=ast.parse((ROOT/'okx_bot.py').read_text())
funcs={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
import copy
snap=copy.deepcopy(funcs['full_stats_snapshot']);scope={
    'Decimal':Decimal,'D':D,'all_stat_cycles':lambda state:list(state['cycles'].values()),
    'allocated_market_fills':lambda fills,plan,cid:fills,
    'accumulate_expected_sell':lambda expected,leg:expected.__setitem__(leg['ordId'],D(leg['filled'])),
    'stats_pct':lambda n,d:'—' if d<=0 else str(n/d*100)+'%',
}
exec(compile(ast.Module(body=[snap],type_ignores=[]),'stats_offline','exec'),scope)
def fill(oid,trade,side,px,qty,fee,kind='M'):
    return {'ordId':oid,'tradeId':str(trade),'billId':str(trade),'fillPx':str(px),
            'fillSz':str(qty),'fee':str(fee),'feeCcy':'USDT','execType':kind,
            'side':side,'ts':'1792670000000'}
def cycle(cid,state,bsz,ssz,legs):
    return {'cycleId':str(cid),'state':state,'ordId':'b'+str(cid),'buyPx':'100',
            'filledBuy':str(bsz),'filledTP':str(ssz),'tpOrders':legs}
def leg(oid,qty,source=None):
    data={'ordId':oid,'filled':str(qty)}
    if source:data.update(source=source,aggregatePlan={'test':1})
    return data
cycles={
    '1':cycle('1','CLOSED',2,2,[leg('s1',2)]),
    '2':cycle('2','CLOSED',1,1,[leg('s2',1,'aggregate_exit')]),
    '3':cycle('3','TP_PARTIAL',3,1,[leg('s3',1)]),
    '4':cycle('4','EXITED',1,1,[leg('s4',1)]),
}
fills=[fill('b1',1,'buy',100,2,'-.2'),fill('s1',2,'sell',105,2,'-.21'),
       fill('b2',3,'buy',100,1,'-.1'),fill('s2',4,'sell',110,1,'-.11','T'),
       fill('b3',5,'buy',100,3,'-.3'),fill('s3',6,'sell',106,1,'-.106'),
       fill('b4',7,'buy',100,1,'-.1'),fill('s4',8,'sell',98,1,'-.098')]
stat=scope['full_stats_snapshot']({'cycles':cycles,'completed':2},fills,D(103),D(1))
assert stat['complete'] and stat['closed_count']==2
assert stat['grid_gain']==D('19.59')-D('0.21') # 9.59+9.79 =19.38
assert stat['limit_tp_net']==D('9.59') and stat['market_tp_net']==D('9.79')
assert stat['floating_pnl']==D(6) and stat['open_base']==D(2)
assert stat['realized_open_components']==D('5.594')
assert stat['partial_exit_net']==D('-2.198')
assert stat['total_gain']==D('28.776')
assert stat['grid_gain']+stat['partial_exit_net']+stat['realized_open_components']+stat['floating_pnl']==stat['total_gain']
print('PASS stats: exact actual-fee LIMIT/MARKET split; Floating excludes realized partial portions')

# Display must not call OKX or write state; small-price math uses Decimal.
with tempfile.TemporaryDirectory() as d:
    root=Path(d);(root/'state').mkdir();(root/'logs').mkdir()
    (root/'config.json').write_text(json.dumps({'instrument':'DOOD-USDT-SWAP','tick_size':'0.000001','contract_value':'1000'}))
    payload=dict(schema=1,bid='0.002000',top_open_buy='0.002500',open_base='2000',
        open_nominal='4.000000',floating_pnl='-1.00000000',grid_profit='3.500',
        closed_count=23,limit_tp_net='2.5',market_tp_net='1.0',partial_exit_count=1,
        partial_exit_net='-.5',realized_open_components='.3',total_pnl='2.3',tp_waiting=2,
        last_24h='1',per_day='2',per_30d='60',per_year='730',capital='200',
        max_base='200000',grid_reference_top='.003',grid_bottom='.001',grid_average='.002',
        launch_at=1792660000,uptime='99.82%',volume='200',maker_volume='180',
        taker_volume='20',paid_fees='-.2')
    now=1792680000
    out=format_stats(root,'DOOD-USDT-SWAP',payload,bot_active=True,heartbeat_fresh=True,
                     snapshot_at=now-100,now=now)
    assert '🟢 Бот работает' in out
    assert 'Верх сетки: $0.002500' in out and 'ниже на 20.00%' in out
    assert 'Открытая позиция: 2000.00000000 DOOD  |  $4.00000000' in out
    assert 'Floating PnL: −$1.00000000' in out and 'ИТОГОВЫЙ PnL: +$2.30000000' in out
    assert 'Виртуальных TP' not in out and 'Последний замер DD' not in out
    assert 'Макс. позиция в истории' in out
    bad=dict(payload,total_pnl='2.4')
    try:format_stats(root,'DOOD-USDT-SWAP',bad,now=now)
    except ValueError:pass
    else:raise AssertionError('Inconsistent accounting displayed')
    t=datetime.now(timezone.utc)
    points=[{'phase':'loop_pass','ts':(t-timedelta(minutes=m)).isoformat(),'ms':x}
            for m,x in [(48,5),(10,10),(1,100),(0,250)]]
    points+=[{'phase':'positions_gate_error','ts':t.isoformat(),'ms':0,
              'reason':'lock_timeout','outcome':'unavailable'}]
    (root/'logs'/'latency.jsonl').write_text('\n'.join(map(json.dumps,points))+'\n')
    text=latency_report(root)
    assert 'P99' in text and 'MAX' in text and '60 мин' in text and 'N' in text
    assert '250.00 ms' in text and '48.0/60 мин' in text
    assert 'lock_timeout=1' in fifo_report(root)
    events=[{'ts':t.isoformat(),'event':n} for n in ('BUY_FILLED','TP_FILLED','SYNC_WAIT')]
    (root/'logs'/'bot_events.jsonl').write_text('\n'.join(map(json.dumps,events))+'\n')
    ex=execution_report(root)
    assert 'BUY FILLED events: 1' in ex and 'TP FILLED events: 1' in ex
    assert 'Partial fill ratio: н/д' in ex
print('PASS UI: floating/nominal exact, 60m/P99/MAX, FIFO reason, execution evidence-only')

# Real quota path in a temp dir; safety algorithm outcome unchanged.
with tempfile.TemporaryDirectory() as d:
    rec=[]
    gate=AccountPositionsFIFO(Path(d)/'positions.json',reporter=lambda *a,**k:rec.append((a,k)))
    gate.take()
    assert any(a[0]=='positions_gate_lock_wait' for a,_ in rec)
    assert any(a[0]=='positions_gate_fifo_wait' for a,_ in rec)
print('PASS FIFO: local telemetry emitted, quota reservation still safe')
print('OFFLINE ONLY — no OKX, VPS, services or LIVE files touched')
