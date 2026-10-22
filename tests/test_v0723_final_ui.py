"""Pure offline regressions for v0.7.23 final UI patch: no OKX or LIVE state."""
import sys
from pathlib import Path
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import okx_setup,okxbot
from okx_account_ui import format_account, _age_and_utc
from datetime import datetime,timezone,timedelta

# All four interactive preview screens show numeric context before first keypress.
ctx={'coin':'ZEC','instrument':'ZEC-USDT-SWAP','price':D('1253.06'),
     'tick':D('.01'),'min_tp':D('.61'),'maker':D('.0002'),
     'min_qty':D('.01'),'ct':D(1),'lot':D('.01'),
     'step':D(2),'grids':D(100),'qty':D('.01')}
for key,expected in [('step','GRID STEP: 0.0000%'),
                     ('grids','Покрытие вниз: 0.0000%'),
                     ('qty','Номинал одного ордера при текущем BID:'),
                     ('tp','Профит с одной сделки:')]:
    assert expected in '\n'.join(okx_setup.lines_for(key,ctx,''))
rows=okx_setup.lines_for('tp',ctx,'2')
assert len([x for x in rows if '→' in x])==5
assert 'План net' not in '\n'.join(rows)
assert okx_setup.signed_usd(D('0.014984'))=='+$0.014984'
assert 'от BID' in '\n'.join(rows)
assert 'Оценка без комиссий' not in '\n'.join(okx_setup.lines_for('qty',ctx,'.01'))

# Walk the actual wizard twice: second pass must use existing inputs unchanged;
# contract gets fetched EXACTLY ONCE; no exchange calls from keystrokes.
contract=dict(ctx)
contract.update(spec={'groupId':'4'},min_contracts=D('.01'),taker=D('.0005'),
                fee_level='4',break_even=D('.51'))
values={'step':'2','grids':'100','qty':'.01','tp':'2','buys':'2',
        'real_tp':'2','upper':'0'}
menus=iter([0,0,1,0])
inputs=[]
def fake_number(scr,title,body,validate,initial=''):
    key=next(k for k in values if k in title.lower()) if False else None
    # Discriminate by the first line produced by the screen callback.
    first=body(initial)[0]
    if 'GRID-уровнями' in first: key='step'
    elif 'количество GRID-уровней' in first:key='grids'
    elif 'размер одного BUY' in first:key='qty'
    elif 'TAKE PROFIT' in first:key='tp'
    elif 'следящих BUY' in first:key='buys'
    elif 'реальных TP' in first:key='real_tp'
    else:key='upper'
    inputs.append((key,initial))
    return validate(initial or values[key])
with patch.object(okx_setup.curses,'curs_set'), \
     patch.object(okx_setup,'setup_colors'), \
     patch.object(okx_setup,'menu',side_effect=lambda *a,**kw:next(menus)), \
     patch.object(okx_setup,'load_contract',return_value=dict(contract)) as load, \
     patch.object(okx_setup,'numeric_input',side_effect=fake_number), \
     patch.object(okx_setup,'name_input',side_effect=lambda *a,**kw:'zectest'):
    result=okx_setup.wizard(object(),{},())
assert result['name']=='zectest' and result['tp']==D(2)
assert load.call_count==1
assert len(inputs)==14 and all(initial for _,initial in inputs[7:])
assert set(k for k,_ in inputs[:7])==set(values)
print('PASS wizard: edit preserves all settings, instant previews, no repeat API contract fetch')

# Margin: use ordFrozen only when provided by OKX, never infer from difference.
now=datetime(2026,10,22,13,29,36,tzinfo=timezone.utc)
assert _age_and_utc('2026-10-22T13:29:26+00:00',now)==\
       '22.10.2026 13:29:26 UTC (10 сек назад)'
base={'asof':'2026-10-22T13:29:26+00:00','equity':'1114.23',
      'equity_unit':'USDT','floating_pnl':'-43.79','margin_in_use':'59.24',
      'free_margin':'1054.60','free_margin_label':'Доступная маржа',
      'orders_margin':'8.89','positions':[]}
lines=format_account(base,[],now=now)
assert any('Маржа открытых ордеров:' in x and '$8.89' in x for x in lines)
assert any('Другое / расхождение:' in x for x in lines)
base2=dict(base,orders_margin=None)
lines2=format_account(base2,[],now=now)
assert any('Маржа открытых ордеров:' in x and 'н/д' in x for x in lines2)
assert not any('Другое / расхождение:' in x for x in lines2)
print('PASS account: ordFrozen authoritative, timestamp and missing-data fallback')

# GET happens only on explicit "Refresh" (not once per 1-second redraw).
init=(base,[],None)
state={'fetches':0}
def fetch():
    state['fetches']+=1
    return (base,[],None)
class Screen:
    def timeout(self,ms):pass
    def getmaxyx(self):return (35,100)
    def erase(self):pass
    def addnstr(self,*args):pass
    def refresh(self):pass
    def get_wch(self):
        # Select refresh and then return to previous menu using Down + Enter.
        return next(keys)
keys=iter(['\n',okxbot.curses.KEY_DOWN,'\n'])
with patch.object(okxbot,'_account_overview_data',side_effect=fetch):
    okxbot._account_detail_menu(Screen(),init)
assert state['fetches']==1
print('PASS account: refresh only once on explicit input; no background GET')
print('OFFLINE FINAL UI PASS')
