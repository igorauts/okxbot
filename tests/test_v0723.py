"""No OKX network, no trading processes, no live files; v0.7.23 regression tests."""
import ast
import copy
import json
import queue
import sys
import tempfile
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import okx_setup
from okx_account_ui import get_account_snapshot,format_account
from okx_latency_ui import _percentile, report as latency_report

D=Decimal
ctx={'price':D('0.002304'),'tick':D('0.000001'),'coin':'DOOD',
     'step':D('0.000010'),'grids':D(100),'qty':D(1000),
     'maker':D('0.0002'),'min_qty':D(1000),'lot':D(1),'ct':D(1000),
     'min_tp':D('0.000002')}
project=okx_setup.grid_projection(ctx['price'],ctx['step'],100,D(1000))
assert project['bottom']==D('0.001304')
assert project['units']==D(100000)
assert project['spent']==D('179.900000')
assert project['nominal']==D('130.400000')
assert project['floating']==D('-49.500000')
assert f'{project["floating"]/project["spent"]*100:.2f}'=='-27.52'
assert okx_setup.percentage(ctx['step'],ctx['price'])=='0.4340%'
assert [(str(v),p) for _,v,p in okx_setup.tp_reference_rows(ctx)]==[
 ('0.000002','0.0868%'),('0.000003','0.1302%'),('0.000005','0.2170%'),
 ('0.000007','0.3038%'),('0.000012','0.5208%')]
assert okx_setup.tp_estimated_net(ctx,D('0.000010'),D(1000))==D('0.0090764000')
assert '20%' not in '\n'.join(okx_setup.lines_for('tp',ctx,'0.000010'))
assert '0.4340%' in '\n'.join(okx_setup.lines_for('tp',ctx,'0.000010'))
for name in ('step','grids','qty','tp'):
    preview='\n'.join(okx_setup.lines_for(name,ctx,''))
    assert preview and 'Текущий BID' in preview
    assert (name!='step' or 'GRID STEP: 0.0000%' in preview)
    assert (name!='grids' or 'Покрытие вниз: 0.0000%' in preview)
    assert (name!='qty' or 'Номинал одного ордера при текущем BID' in preview)
    assert (name!='tp' or 'Профит с одной сделки' in preview)
assert okx_setup.signed_usd(D('0.014984'))=='+$0.014984'
assert okx_setup.signed_usd(D('-99.00'),2)=='−$99.00'
assert 'Оценка без комиссий' not in '\n'.join(okx_setup.lines_for('qty',ctx,'1000'))
assert 'План net' not in '\n'.join(okx_setup.lines_for('tp',ctx,'.000010'))
ctx_summary=dict(ctx,upper=D(0),tp=D('.000010'),buys=D(10),real_tp=D(10),name='doodtest',instrument='DOOD-USDT-SWAP')
sumtxt='\n'.join(okx_setup.report(ctx_summary))
assert 'GRID STEP:' in sumtxt and '0.4340%' in sumtxt
assert 'TAKE PROFIT:' in sumtxt and 'Размер BUY:' in sumtxt
assert 'Профит с одной сделки: ' in sumtxt and '+$0.009076' in sumtxt
assert not any(x in sumtxt for x in ('BUY буфер:','Защита BUY','SELL TP буфер:',
                                     'РЕЖИМ:','Плечо: устанавливается','Стоимость BUY:'))
assert 'Плавающий убыток на дне сетки' in '\n'.join(okx_setup.lines_for('qty',ctx,'1000'))
assert 'Покрытие вниз' in '\n'.join(okx_setup.lines_for('grids',ctx,'100'))
assert '0.4340%' in '\n'.join(okx_setup.lines_for('step',ctx,'0.000010'))
try:okx_setup.grid_projection(D('0.002'),D('0.001'),3,D(1))
except ValueError:pass
else:raise AssertionError('Invalid bottom accepted')
# No price queries for live numeric edits.
with patch.object(okx_setup,'okx_req',side_effect=AssertionError('extra OKX REST')):
    okx_setup.lines_for('qty',ctx,'1000');okx_setup.lines_for('grids',ctx,'100')
print('PASS wizard: Decimal, percentage, tick examples, full-grid risk and input previews')

# Futures/USDT-Cross: balance details contain fields that are blank at root.
requests=[]
def fake_req(url,env=None,params=None):
    requests.append((url,params))
    if url.endswith('/balance'):
        return [{'totalEq':'1105.92','adjEq':'','availEq':'','upl':'',
                 'imr':'','mmr':'','mgnRatio':'',
                 'details':[{'ccy':'USDT','eq':'1105.92','upl':'-54.92',
                             'availEq':'1046.66','imr':'59.26','ordFrozen':'6.91','mmr':'23.85',
                             'mgnRatio':'41.65'}]}]
    return [
        {'instId':'BTC-USDT-SWAP','pos':'5.7','lever':'100',
         'notionalUsd':'4649.22','imr':'46.54','upl':'-53.43'},
        {'instId':'NEAR-USDT-SWAP','pos':'1.8','lever':'50',
         'notionalUsd':'89.68','imr':'1.79','upl':'-0.88'},
        {'instId':'DOOD-USDT-SWAP','pos':'36','lever':'20',
         'notionalUsd':'79.98','imr':'4.02','upl':'-0.34'}]
with patch.object(okx_setup,'read_env',return_value=({'OKX_API_KEY':'FAKE'},'')), \
     patch.object(okx_setup,'okx_req',side_effect=fake_req), \
     patch('okx_positions_fifo.AccountPositionsFIFO.take') as gate_take:
    data=get_account_snapshot('/tmp/does-not-exist')
assert len(requests)==2 and all(x[0].startswith('/api/v5/account/') for x in requests)
assert gate_take.call_count==1
assert data['equity']=='1105.92' and len(data['positions'])==3
assert data['floating_pnl']=='-54.92'
assert data['margin_in_use']=='59.26' and data['orders_margin']=='6.91'
assert data['free_margin']=='1046.66'
from types import SimpleNamespace
with tempfile.TemporaryDirectory() as d:
    root=Path(d)
    bots=[]
    for name,contract in [('BTC','0.01'),('NEAR','10'),('DOOD','1000')]:
        botdir=root/name;botdir.mkdir()
        (botdir/'config.json').write_text(json.dumps(
            {'instrument':name+'-USDT-SWAP','contract_value':contract}))
        bots.append(SimpleNamespace(instrument=name+'-USDT-SWAP',
                                    root=botdir,systemd_state='active'))
    lines=format_account(data,bots)
joined='\n'.join(lines)
assert 'Equity:' in joined and '$1,105.92 USDT' in joined
assert 'Floating PnL:' in joined and '-$54.92' in joined
assert 'In use:' in joined and '$59.26' in joined
assert 'Доступная маржа:' in joined and '$1,046.66' in joined
assert '0.057 BTC ($4,649.22)' in joined
assert '18 NEAR ($89.68)' in joined
assert '36,000 DOOD ($79.98)' in joined
assert '100x' in joined and '50x' in joined and '20x' in joined
assert '$46.54' in joined and '$1.79' in joined and '$4.02' in joined
assert 'Маржа позиций:' in joined and '$52.35' in joined
assert 'Маржа открытых ордеров:' in joined and '$6.91' in joined
assert 'Другое / расхождение:' not in joined # matching 59.26 = 52.35 + 6.91
assert 'Всего In use:' in joined and '$59.26' in joined
assert 'UTC (' in joined and 'сек назад' in joined
assert all(len(line)<=80 for line in lines), [x for x in lines if len(x)>80]
assert 'Adjusted Equity' not in joined and 'Margin ratio' not in joined
assert 'ПАРА LIVE-БОТА' not in joined and 'pos ' not in joined
assert 'GET-only' not in joined
assert 'Пара' not in joined and 'IMR' not in joined and 'UPL' not in joined
assert not any('4000%' in line for line in lines)
# Never derive a per-order margin allocation from In use minus position IMR.
test_data=data.copy();test_data['orders_margin']=None
unknown_margin='\n'.join(format_account(test_data,bots))
assert 'Маржа открытых ордеров:' in unknown_margin and 'н/д' in unknown_margin
assert 'Другое / расхождение:' not in unknown_margin
# Flag nonreconciling values without misattributing them to order margin.
test_data['orders_margin']='1.00'
assert 'Другое / расхождение:' in '\n'.join(format_account(test_data,bots))
# Without an authoritative contract size never pass contract count off as coins.
unknown=format_account(data,[SimpleNamespace(instrument='BTC-USDT-SWAP',systemd_state='active')])
assert '5.7 контр.' in '\n'.join(unknown)
# A blank Futures top-level field is never mistaken for USDT value 0.
assert data['margin_in_use']!='0'
print('PASS account Futures/USDT balance and compact margin/coin-quantity table')

# Deliberately isolate exact new manager command AST; do not import LIVE manager runtime.
mtree=ast.parse((ROOT/'okxbot.py').read_text())
names={'cmd_panic_all','_panic_all_preflight'}
subset=[copy.deepcopy(node) for node in mtree.body
        if isinstance(node,ast.FunctionDef) and node.name in names]
assert len(subset)==len(names)
import types
from unittest.mock import Mock
nsp={'sys':SimpleNamespace(stdin=SimpleNamespace(isatty=lambda:True),
                            stdout=SimpleNamespace(isatty=lambda:True),stderr=SimpleNamespace(write=lambda x:None)),
     'print':lambda *a,**kw:None,
     'BASE':Path('/unused'), 'REGISTRY':Path('/unused'),
     'SYSTEMD_DIR':Path('/unused'), 'control_supported':lambda e:True,
     'input':lambda p='':'PANIC ALL',
     '_panic_all_preflight':None}
exec(compile(ast.fix_missing_locations(ast.Module(body=[n for n in subset if n.name=='cmd_panic_all'],type_ignores=[])),
       '<panic-all>','exec'),nsp)
plan=(('a','BTC-USDT-SWAP','okx-grid-a.service',123,'RUN'),
      ('b','DOOD-USDT-SWAP','okx-grid-b.service',456,'SOFT'))
sent=[]
nsp.update({'_panic_all_preflight':lambda:plan,
            'find_entry':lambda name:(name,name),
            'service_running':lambda x:True,
            'read_control':lambda x:{'mode':'RUN' if x=='a' else 'SOFT'},
            'write_control':lambda entry,mode:sent.append((entry,mode)),
            '_current_account_bots':lambda:[],
            'subprocess':SimpleNamespace(run=lambda *a,**kw:
                         SimpleNamespace(stdout='123\n' if 'okx-grid-a.service' in a[0] else '456\n'))})
with patch('okx_panic_all.survey',return_value=(plan,(),())):
    nsp['cmd_panic_all']()
assert sent==[('a','PANIC'),('b','PANIC')]
sent.clear();nsp['input']=lambda x:''
with patch('okx_panic_all.survey',return_value=(plan,(),())):
    nsp['cmd_panic_all']()
assert not sent
nsp['input']=lambda x:'PANIC ALL'
nsp['_panic_all_preflight']=iter([plan,plan[:1]]).__next__
try:nsp['cmd_panic_all']()
except RuntimeError as exc:assert 'изменился' in str(exc)
else:raise AssertionError('Changing bot list did not fail closed')
assert not sent
print('PASS PANIC ALL: one phrase, all durable control intents, changed-plan fail-closed')

# Shared SOFT latch re-acknowledges a NEW incident, not bypassing the timestamp.
btree=ast.parse((ROOT/'okx_bot.py').read_text())
ack=next(copy.deepcopy(n) for n in btree.body
         if isinstance(n,ast.FunctionDef) and n.name=='control_ack')
with tempfile.TemporaryDirectory() as d:
    root=Path(d)
    ack_file=root/'ack.json';latch_file=root/'latch.json'
    past=datetime(2026,10,20,0,tzinfo=timezone.utc)
    newer=past+timedelta(hours=1)
    ack_file.write_text(json.dumps({'mode':'SOFT','phase':'SOFT_STOPPED','updated_at':past.isoformat(),
                                    'schema_version':1}))
    latch_file.write_text(json.dumps({'schema':1,'mode':'SOFT','at':newer.isoformat()}))
    writes=[]
    def atomic(path,value):
        writes.append(value)
        path.write_text(json.dumps(value))
    ns={'CONTROL_ACK':ack_file,'ACCOUNT_SOFT_STOP':latch_file,
        'account_soft_active':lambda:True,'atomic_json':atomic,'json':json,
        'datetime':datetime,'timezone':timezone,'log':lambda *a,**kw:None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ack],type_ignores=[])),'<ack>','exec'),ns)
    ns['control_ack']('SOFT','SOFT_STOPPED')
    assert len(writes)==1
    ns['control_ack']('SOFT','SOFT_STOPPED')
    assert len(writes)==1
print('PASS SOFT latch: renewed SOFT_STOPPED acknowledgment only for newer latch')

# Diagnostic disk write lives ONLY in a background method.
cls=next(n for n in btree.body if isinstance(n,ast.ClassDef) and n.name=='BotDrawdownSampler')
submit=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='submit_reconciled')
writer=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_write_loop')
assert 'record_bot_drawdown' not in ast.dump(submit)
assert 'put_nowait' in ast.dump(submit) and 'record_bot_drawdown' in ast.dump(writer)
assert '_thread.start' not in ast.dump(submit)
print('PASS drawdown: bounded queue from trading loop; disk I/O only in async worker')

assert _percentile([1,2,3,4,100],95)=='100.00 ms'
with tempfile.TemporaryDirectory() as d:
    root=Path(d);(root/'logs').mkdir()
    now=datetime.now(timezone.utc)
    ts=now.isoformat()
    (root/'logs/latency.jsonl').write_text('\n'.join(json.dumps(
        {'phase':'loop_pass','ms':x,'ts':ts}) for x in (1,2,3,4,100))+'\n')
    out=latency_report(root)
    assert 'ACTIVE' in out and '3.00 ms' in out and '100.00 ms' in out
    assert 'Dropped: н/д' in out
print('PASS latency: read-only local P50/P95/P99/MAX, unknown dropped clearly unavailable')
print('OFFLINE V0723 PASS: no live processes, no exchange connections or trade writes')
