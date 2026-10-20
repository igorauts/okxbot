"""Offline critical scenarios of v0.7.21; loads functions only, never imports live engine."""
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
import ast, copy, time, threading, json
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
tree=ast.parse((ROOT/'okx_bot.py').read_text())
names={'panic_retry_delay','place_panic_sell','reconcile_panic_orders',
       'emergency_precancel_own_buys','coverage_hold_delay','wait_coverage_hold',
       'controlled_shutdown','post_absence_confirmed'}
subset=[copy.deepcopy(n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
assert len(subset)==len(names)

class Unresolved(Exception): pass
class Expired(Exception): pass
class Temporary(Exception): pass
class StubAPI:
    def __init__(self):
        self.orders=[];self.pending_orders=[];self.responses=[];self.posts=[]
    def pending(self):return self.pending_orders
    def order(self,**kwargs):
        cid=kwargs.get('cid');oid=kwargs.get('oid')
        return next((o for o in self.orders if o.get('clOrdId')==cid or o.get('ordId')==oid),None)
    def req(self,method,path,body=None,exp_time_ms=None):
        assert method=='POST' and path=='/api/v5/trade/order'
        assert body['ordType']=='market' and body['reduceOnly'] is True
        assert exp_time_ms>time.time()*1000
        self.posts.append(dict(body))
        response=self.responses.pop(0)
        if isinstance(response,Exception):raise response
        return response

api=StubAPI();loglines=[];saved=[];acks=[];canceled=[]
ns={'Decimal':Decimal,'D':lambda x:Decimal(str(x or '0')),
    'time':time,'A':api,'INST':'ZEC-USDT-SWAP',
    'CONTRACT_MIN':Decimal('1'),'CONTRACT_LOT':Decimal('1'),
    'datetime':datetime, 'timezone':timezone,
    'ExchangeUnresolved':Unresolved,'ExchangeTemporary':Temporary,
    'OrderExpiredBeforeSend':Expired,
    'newid':lambda prefix:'P-TEST-'+str(len(api.posts)+len(saved)),
    'new_request_deadline_ms':lambda:int(time.time()*1000)+8000,
    'save':lambda st:saved.append(copy.deepcopy(st)),
    'log':lambda event,**kw:loglines.append((event,kw)),
    'control_ack':lambda *args,**kw:acks.append((args,kw)),
    'die':lambda msg: (_ for _ in ()).throw(Unresolved(msg)),
    'active_buy':lambda st:[c for c in st['cycles'].values() if c['state'] in ('BUY_LIVE','BUY_PARTIAL','PLACING')],
    'cancel_lowest_specific':lambda st,c:(c.update(state='CANCELING'),canceled.append(c['ordId'])),
    'real_tp_active':lambda st:[], 'panic_executions':lambda st:sum((Decimal(e.get('filled','0')) for e in st.get('panic_orders',[])),Decimal('0')),
    'control_mode':lambda:'RUN',
    'PANIC_MAX_POST_ATTEMPTS':3, 'PANIC_RETRY_BACKOFF_SEC':30,
}
exec(compile(ast.fix_missing_locations(ast.Module(body=subset,type_ignores=[])),str(ROOT/'okx_bot.py'),'exec'),ns)
real_absence_function=ns['post_absence_confirmed']
ns['post_absence_confirmed']=lambda e,side,st:False

def reset():
    api.pending_orders=[];api.orders=[];api.responses=[];api.posts=[]
    saved.clear();logs=loglines;logs.clear();acks.clear();canceled.clear()

def newst():return {'cycles':{},'panic_orders':[]}

def buy(st,oid='owned-1',cid='cid-1',state='BUY_LIVE'):
    st['cycles'][oid]={'ordId':oid,'clOrdId':cid,'sz':'5','buyPx':'100','state':state}
    return {'ordId':oid,'clOrdId':cid,'sz':'5','px':'100','state':'live',
            'instId':'ZEC-USDT-SWAP','side':'buy'}

# A fresh pending snapshot can cancel ONLY owned BUY, even if foreign order exists.
reset();st=newst();owned=buy(st)
foreign={'ordId':'foreign-1','clOrdId':'manual-1','sz':'2','px':'98',
         'state':'live','side':'buy','instId':'ZEC-USDT-SWAP'}
api.pending_orders=[foreign,owned]
ns['control_mode']=lambda:'PANIC'
assert ns['emergency_precancel_own_buys'](st,'PANIC') is True
assert canceled==['owned-1'] and st['cycles']['owned-1']['state']=='CANCELING'
assert ns['emergency_precancel_own_buys'](st,'PANIC') is False # no duplicate cancel
assert len(canceled)==1

# A withdrawn or changed emergency command never cancels on a stale snapshot.
reset();st=newst();order=buy(st);api.pending_orders=[order]
ns['control_mode']=lambda:'RUN'
try:ns['emergency_precancel_own_buys'](st,'HARD')
except Temporary:pass
else:raise AssertionError('Canceled BUY after HARD was withdrawn')
assert not canceled
ns['control_mode']=lambda:'HARD'

# Wrong side/identity/price/size: zero POSTs, fail closed.
for field,val in [('side','sell'),('sz','6'),('px','101'),('clOrdId','wrong'),('instId','ETH-USDT-SWAP')]:
    reset();st=newst();order=buy(st);order[field]=val;api.pending_orders=[order]
    try:ns['emergency_precancel_own_buys'](st,'HARD')
    except Unresolved:pass
    else:raise AssertionError('Unsafe BUY accepted: '+field)
    assert canceled==[]

# Unresolved PLACING must NOT be canceled blindly.
reset();st=newst();owned=buy(st,state='PLACING');api.pending_orders=[owned]
assert ns['emergency_precancel_own_buys'](st,'PANIC') is False and not canceled

# Partial BUY may be safely canceled by exact IDs; its attached TP is untouched.
reset();st=newst();owned=buy(st,state='BUY_PARTIAL');api.pending_orders=[owned]
ns['control_mode']=lambda:'HARD'
assert ns['emergency_precancel_own_buys'](st,'HARD') is True
assert canceled==['owned-1']

# Unknown MARKET never triggers re-POST until fully proven absent.
reset();st=newst();api.responses=[Unresolved('network timeout')]
try:ns['place_panic_sell'](st,Decimal('5'))
except Unresolved:pass
else:raise AssertionError('Expected unknown POST')
assert len(api.posts)==1 and len(st['panic_orders'])==1
rec=st['panic_orders'][0]
assert rec['expTimeMs'] and rec['state']=='SUBMITTED_UNKNOWN'
assert ns['reconcile_panic_orders'](st,[]) is False
# No emergency MARKET while order identity/position is unresolved.
assert ns['controlled_shutdown'](st,[],Decimal('5'),False,'PANIC') is False
assert not any(event=='PANIC_DONE' for event,kw in loglines)
try:ns['panic_retry_delay'](st)
except Unresolved:pass
else:raise AssertionError('Unknown MARKET permitted retry')
assert len(api.posts)==1
# Negative proof demands fresh pending snapshot and history coverage.
ns['post_absence_confirmed']=lambda e,side,st:True
assert ns['reconcile_panic_orders'](st,None) is False
assert ns['reconcile_panic_orders'](st,[{'clOrdId':rec['clOrdId']}]) is False
assert ns['reconcile_panic_orders'](st,[]) is True
assert rec['state']=='EXPIRED_ABSENT'
assert ns['panic_retry_delay'](st)>0
# A positive accepted ordId must NEVER be cleared by negative history probes.
reset();st=newst();st['panic_orders']=[{
    'clOrdId':'P-positive','ordId':'confirmed-123','sz':'5','filled':'0',
    'state':'SUBMITTED_UNKNOWN','placedAt':time.time()-300,'expTimeMs':int((time.time()-290)*1000)}]
ns['post_absence_confirmed']=lambda e,side,st:True
assert ns['reconcile_panic_orders'](st,[]) is False
assert st['panic_orders'][0]['state']=='SUBMITTED_UNKNOWN'

# Real three-pass absence proof requires aged expTime and complete order history.
class HistoricalAPI:
    def __init__(self,clock):self.clock=clock;self.gets=0
    def server_time_ms(self):return int(self.clock.time()*1000)
    def req(self,method,path,params=None):
        assert method=='GET' and path=='/api/v5/trade/orders-history'
        self.gets+=1
        return [{'ordId':'other-order','instId':'ZEC-USDT-SWAP',
                 'clOrdId':'other-client','cTime':str(int(self.clock.time()*1000))}]
class FakeClock:
    def __init__(self):self.value=time.time()
    def time(self):return self.value
clock=FakeClock();clock.value+=200
hist=HistoricalAPI(clock)
proof=ns.copy();proof['A']=hist;proof['time']=clock
exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(n) for n in subset
    if n.name=='post_absence_confirmed'],type_ignores=[])),str(ROOT/'okx_bot.py'),'exec'),proof)
proof['ABSENCE_GRACE_MS']=120000
proof['ABSENCE_CHECK_GAP_SEC']=20
proof['ABSENCE_REQUIRED_CHECKS']=3
proof['ABSENCE_MAX_AGE_SEC']=6*24*3600
rec={'clOrdId':'P-unknown','placedAt':clock.time()-200,
     'expTimeMs':int((clock.time()-192)*1000)}
assert proof['post_absence_confirmed'](rec,'sell',newst()) is False
assert proof['post_absence_confirmed'](rec,'sell',newst()) is False # same timestamp
clock.value+=21
assert proof['post_absence_confirmed'](rec,'sell',newst()) is False
clock.value+=21
assert proof['post_absence_confirmed'](rec,'sell',newst()) is True
assert hist.gets==3 and rec['absenceChecks']==3

# Definite rejection: persistent code, bounded backoff, eventual safe retry.
reset();st=newst();api.responses=[{'not':'a response'}]  # replaced next
api.responses=[[{'sCode':'51008','clOrdId':'P-TEST-0','ordId':'','sMsg':'margin'}]]
ns['newid']=lambda prefix:'P-TEST-0'
ns['place_panic_sell'](st,Decimal('5'))
assert st['panic_orders'][0]['state']=='rejected' and api.posts[0]['clOrdId']=='P-TEST-0'
assert any(name=='PANIC_MARKET_REJECTED' for name,kw in loglines)
assert ns['reconcile_panic_orders'](st,[]) is True
assert ns['panic_retry_delay'](st)>0
st['panic_orders'][0]['resolvedAt']=time.time()-40
assert ns['panic_retry_delay'](st)==0

# Any positive fill of a confirmed partially canceled MARKET can be followed
# by a fresh position check; it does not count against rejection cap.
reset();st=newst();st['panic_orders']=[{'state':'canceled','filled':'3','sz':'5','placedAt':time.time()}]*5
assert ns['panic_retry_delay'](st)==0
assert ns['coverage_hold_delay'](0)==0.5
assert ns['coverage_hold_delay'](1)==1.0
assert ns['coverage_hold_delay'](2)==2.0
assert ns['coverage_hold_delay'](20)==2.0

# HOLD responds to the private stream, ignores public price wakes, and
# never turns a WS event into permission to trade.
class FakeWatcher:
    def __init__(self):
        import queue
        self.events=queue.Queue()
        self.private_wake=threading.Event()
        self.wake=threading.Event()
w=FakeWatcher();w.wake.set()  # public BID only
ns['control_mode']=lambda:'RUN'
assert ns['wait_coverage_hold'](w,.01)=='TIMEOUT'
w.private_wake.set()
assert ns['wait_coverage_hold'](w,2)=='PRIVATE'
ns['control_mode']=lambda:'PANIC'
assert ns['wait_coverage_hold'](w,2)=='CONTROL'

print('PASS: emergency exact-ID BUY cancels, foreign order isolation, no duplicate cancels')
print('PASS: unknown PANIC POST idempotence, expiry proof, definitive reject, bounded retries')
print('PASS: COVERAGE HOLD backoff/private wake/urgent control, price-wake ignored')

# Combined PANIC close path: one accepted MARKET, unknown until exact
# terminal order status; do not re-POST while waiting, then PANIC_DONE at 0.
reset();st=newst();ns['control_mode']=lambda:'PANIC';ns['newid']=lambda prefix:'P-OK'
api.responses=[[{'sCode':'0','clOrdId':'P-OK','ordId':'order-1'}]]
assert ns['controlled_shutdown'](st,[],Decimal('5'),True,'PANIC') is False
assert len(api.posts)==1 and st['panic_orders'][0]['ordId']=='order-1'
assert ns['controlled_shutdown'](st,[],Decimal('5'),True,'PANIC') is False
assert len(api.posts)==1
api.orders=[{'instId':'ZEC-USDT-SWAP','ordId':'order-1','clOrdId':'P-OK',
    'side':'sell','ordType':'market','reduceOnly':'true','sz':'5',
    'accFillSz':'5','state':'filled'}]
assert ns['reconcile_panic_orders'](st,[]) is True
assert st['panic_orders'][0]['filled']=='5'
assert ns['controlled_shutdown'](st,[],Decimal('0'),True,'PANIC') is True
assert st['terminal_mode']=='PANIC_DONE' and len(api.posts)==1

# Terminal PnL: zero-fill rejected/absent intents are auditable attempts,
# not phantom SELL fills; unknown status/contradictory identity stays blocked.
import sys
sys.path.insert(0,str(ROOT))
import okx_terminal_accounting as terminal
class Engine:
    CONTRACT_VALUE=Decimal('0.01')
    def expected_filled_orders(self,st):return {}
    def full_stats_snapshot(self,st,fills,bid,cv):
        return {'complete':True,'open_contracts':Decimal(0),
                'grid_gain':Decimal('1.25'),'partial_exit_net':Decimal(0),
                'closed_count':5}
    def all_stat_cycles(self,st):return []
for state in ('rejected','EXPIRED_ABSENT'):
    st={'terminal_mode':'PANIC_DONE','terminal_snapshot':{'position_contracts':'0'},
        'panic_orders':[{'state':state,'filled':'0','ordId':'','clOrdId':'P-TEST'}]}
    assert terminal.compute(Engine(),st,[],bid=Decimal('100'))['final_trade_pnl']==Decimal('1.25')
    st['panic_orders'][0]['ordId']='unexpected-ack'
    try: terminal.compute(Engine(),st,[],bid=Decimal('100'))
    except ValueError:pass
    else:raise AssertionError('Accepted absent PANIC with positive ordId')
st['panic_orders'][0].update(state='SUBMITTED_UNKNOWN',ordId='')
try: terminal.compute(Engine(),st,[],bid=Decimal('100'))
except ValueError:pass
else:raise AssertionError('Accepted ambiguous PANIC as final PnL')

# Static integration guard for the emergency ordering and HOLD separation.
live=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop')
source=ast.unparse(live)
assert source.index('emergency_precancel_own_buys(st, mode)') < source.index('reconcile_position(')
assert 'if control_mode() != mode:' in source
assert 'wait_coverage_hold(watcher, delay)' in source
assert "watcher.private_wake.clear()" in source
assert 'watcher.wake.wait(0.15)' in source  # normal reconciliation backoff intact
print('PASS: terminal accounting rejects phantom fills; emergency precedes positions gate')
