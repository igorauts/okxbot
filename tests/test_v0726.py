"""v0.7.26: mismatch suppression + exact compact statistics. Local-only tests."""
import ast
import copy
import json
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import okx_telegram_events as telegram
import okx_position_peak
from okx_stats_ui import base_quantity, format_stats
from okx_version import ENGINE_VERSION,RELEASE_VERSION,STATE_SCHEMA

assert (RELEASE_VERSION,ENGINE_VERSION,STATE_SCHEMA)==('0.7.26','0.7.26',1)

# Fixed semantic: trading-engine snapshot must add exchange-active TP records
# and positive-remaining virtual TP cycles, from the SAME state snapshot.
tree=ast.parse((ROOT/'okx_bot.py').read_text(encoding='utf-8'))
func=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
          and n.name=='performance_snapshot_v0724')
ns=dict(Decimal=Decimal,_first_anchor=lambda fills:(100000.,Decimal('1300')),
        STEP=Decimal('1'), CAP=200,QTY=Decimal('0.01'),CONTRACT_VALUE=Decimal('1'),
        _last_24h_profit=lambda *args:Decimal('3'),runtime_pct=lambda now:'99.99%',
        real_tp_active=lambda st:st['active_real'],
        virtual_cycles=lambda st:st['active_virtual'])
exec(compile(ast.Module(body=[copy.deepcopy(func)],type_ignores=[]),'<snapshot-test>','exec'),ns)
state={'cycles':{},'active_real':[1,2],'active_virtual':[3,4,5]}
stat={'bid':Decimal('1200'),'open_base':Decimal('0.62'),
      'floating_pnl':Decimal('-10.19'),'grid_gain':Decimal('13.3362564'),
      'closed_count':1319,'limit_tp_net':Decimal('13.0569163'),
      'market_tp_net':Decimal('0.2793401'),'partial_exit_count':0,
      'partial_exit_net':Decimal('0'),'realized_open_components':Decimal('-.154'),
      'total_gain':Decimal('2.9922564'),'volume':Decimal('10'),
      'maker_volume':Decimal('8'),'taker_volume':Decimal('2'),'total_fees':Decimal('-.3')}
res=ns['performance_snapshot_v0724'](state,[],stat,100100.)
assert res['tp_waiting']==2 and res['tp_waiting_all']==5
assert res['open_nominal']==str(stat['open_base']*stat['bid'])
print('PASS 0726 TP: 2 real + 3 virtual = 5 pending, from one engine snapshot')

# Exchange contract_value × contract_lot determines base quantum; never pad.
for v,cfg,want in [
    ('0.62000000',{'contract_value':'0.01','contract_lot':'1'},'0.62'),
    ('0.83000000',{'contract_value':'0.01','contract_lot':'1'},'0.83'),
    ('0.06300000',{'contract_value':'0.001','contract_lot':'1'},'0.063'),
    ('52000.00000000',{'contract_value':'1000','contract_lot':'1'},'52000'),
    ('0.625',{'contract_value':'0.01','contract_lot':'1'},'0.625'),
]:
    assert base_quantity(v,cfg)==want,(v,base_quantity(v,cfg),want)

with tempfile.TemporaryDirectory() as td:
    root=Path(td)
    (root/'state').mkdir()
    cfg={'instrument':'ZEC-USDT-SWAP','tick_size':'0.01',
         'contract_value':'0.01','contract_lot':'1'}
    (root/'config.json').write_text(json.dumps(cfg))
    p=dict(schema=1,bid='1225.50',top_open_buy='1258.00',
           open_base='0.62',open_nominal='759.810',floating_pnl='-10.19',
           grid_profit='13.3362564',closed_count=1319,
           limit_tp_net='13.0569163',market_tp_net='0.2793401',
           partial_exit_net='0',realized_open_components='-.154',
           total_pnl='2.9922564',tp_waiting=1,tp_waiting_all=28,
           last_24h='3.0',per_day='4',per_30d='120',per_year='1460',
           capital='4802',max_base='4.00000000',grid_reference_top='1300',
           grid_bottom='1101',grid_average='1200.5',launch_at=1792700000,
           uptime='99.99%',volume='65482.6112',maker_volume='64374.54',
           taker_volume='1108.0712',paid_fees='13.4289436')
    with patch.object(okx_position_peak,'_ordered_rows',return_value=[]),\
         patch.object(okx_position_peak,'reconstruct_peak',
                      return_value=({'base_qty':'0.83000000','nominal_usdt':'996.00'},None)):
        out=format_stats(root,'ZEC-USDT-SWAP',p,bot_active=True,
                         heartbeat_fresh=True,now=1792750000,snapshot_at=1792749995)
        lines=out.splitlines()
        assert lines[0].startswith('🟢 Работает:')
        assert lines[1].startswith('Запущен:')
        assert 'Бот работает' not in out
        assert 'Верх сетки: $1,258.00' in out
        assert 'Текущий BID: $1,225.50 (-2.58%)' in out
        assert 'ниже на' not in out
        assert 'Ждут TP: 28' in out
        assert 'Открытая позиция: 0.62 ZEC ($759.81)' in out
        assert '  max position: 4 ZEC' in out
        assert 'Макс. позиция в истории: 0.83 ZEC ($996.00)' in out
        assert 'ИТОГОВЫЙ PnL: +$2.99225640' in out
        assert '✅ Все закрытые циклы сопоставлены.' in out
        assert lines[-1].startswith('✅ ')  # no lost output indentation
        old=dict(p);old.pop('tp_waiting_all')
        old_text=format_stats(root,'ZEC-USDT-SWAP',old,bot_active=True,
                              heartbeat_fresh=True,now=1792750000,snapshot_at=1792749995)
        assert 'Ждут TP: н/д' in old_text and 'Ждут TP: 1' not in old_text
print('PASS 0726 stats: compact status, signed BID, virtual+real TP, precise base and USD cents')

# Telegram must never send or recover mismatch incidents, even when the old
# monitor has left their messages queued across restart. The same input must
# still alert for TP coverage and other actionable faults.
with tempfile.TemporaryDirectory() as td:
    root=Path(td)/'zec0724';out=root/'diagnostics'/'execution';out.mkdir(parents=True)
    alert=out/'alerts.jsonl'
    alert.write_text('\n'.join(json.dumps({'kind':kind,'ts':'2026-10-23T15:00:00Z'})
          for kind in ('PERSISTED_STATE_DIFFERS_FROM_ENGINE','TP_HARD_CAP_REACHED',
                       'STATE_READ_FAILED'))+'\n')
    queued=[{'key':'state_mismatch/open/zec0724/1','text':'old mismatch'},
            {'key':'state_mismatch/normal/zec0724/1','text':'old recovery'},
            {'key':'panic/zec0724/x','text':'critical PANIC'},
            {'key':'zero/zec0724/1','text':'BUY 0/1'}]
    st={'observer_offsets':{str(alert):0},'alert_outbox_v0722':queued,
        'state_mismatch_incidents':{'zec0724':{'matches':2}}}
    called=[]
    with patch.object(telegram,'one',side_effect=lambda api,st,key,msg:called.append((key,msg))):
        telegram.observer_fault_scan(None,st,{'zec0724':root})
    assert 'state_mismatch_incidents' not in st
    assert [x['key'] for x in st['alert_outbox_v0722']]==['panic/zec0724/x','zero/zec0724/1']
    assert len(called)==2
    assert any('TP_HARD_CAP_REACHED' in x[1] for x in called)
    assert any('STATE_READ_FAILED' in x[1] for x in called)
    assert not any('MISMATCH' in x[1] or 'BACK TO NORMAL' in x[1] for x in called)
    assert st['observer_offsets'][str(alert)]==alert.stat().st_size
    assert 'PERSISTED_STATE_DIFFERS_FROM_ENGINE' in alert.read_text() # diagnostic stays
    telegram.observer_fault_scan(None,st,{'zec0724':root})
    assert len(called)==2  # no replay; cursor advanced
print('PASS 0726 Telegram: legacy mismatch outbox purged; observer retained; critical alarms preserved')

manager=(ROOT/'okxbot.py').read_text(encoding='utf-8')
start=manager.index("if curses.has_colors() and line.startswith((")
fragment=manager[start:start+230]
assert 'GRID PROFIT:' in fragment and 'ИТОГОВЫЙ PnL:' in fragment
assert 'Floating PnL:' in fragment and 'Макс. просадка:' in fragment
assert 'LIMIT TP:' not in fragment and 'MARKET TP:' not in fragment
assert 'Partial MARKET Exit:' not in fragment
print('PASS 0726 terminal colors: TP split neutral; GRID/total/floating/DD retained')
print('OFFLINE v0.7.26 FOCUSED PASS — no OKX, services or LIVE files accessed')
