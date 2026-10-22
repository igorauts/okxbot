"""No network, no trading, no service writes. Run with python3 -B tests/test_offline.py."""
from pathlib import Path
import copy
import sys, json, ast, tempfile, importlib, types, hashlib
from decimal import Decimal
ROOT=Path(__file__).resolve().parents[1]
SH=ROOT
sys.path.insert(0,str(SH))

from okx_contracts import base_coin, validate_spec
from okx_state import empty, validate, IncompatibleState
from okx_version import VERSION, STATE_SCHEMA
from okx_paths import state_file as current_state, fills_db as current_fills
from okx_archive_reader import state_file as archive_state, fills_db as archive_fills

assert VERSION=='0.7.22.1' and STATE_SCHEMA==1
assert base_coin('BTC-USDT-SWAP')=='BTC'
assert base_coin('1000PEPE-USDT-SWAP')=='1000PEPE'
for invalid in ['BTC-USD-SWAP','BTC-USDC-SWAP','BTC-USDT-241225','BTC-USDT-SWAP-XXX','']:
    try: base_coin(invalid)
    except ValueError: pass
    else: raise AssertionError('Unexpected accepted market: '+invalid)

valid={'instId':'1000PEPE-USDT-SWAP','instType':'SWAP','ctType':'linear',
       'settleCcy':'USDT','state':'live','ctValCcy':'1000PEPE',
       'tickSz':'0.00001','ctVal':'100','lotSz':'1','minSz':'1'}
assert validate_spec('1000PEPE-USDT-SWAP',valid)=='1000PEPE'
for key,value in [('ctType','inverse'),('settleCcy','USD'),('instType','FUTURES'),
                  ('state','suspend'),('instId','BTC-USDT-SWAP'),('ctValCcy','USDT'),
                  ('tickSz','0'),('lotSz','NaN'),('minSz','')]:
    bad={**valid,key:value}
    try:validate_spec('1000PEPE-USDT-SWAP',bad)
    except ValueError:pass
    else:raise AssertionError('Accepted invalid '+key+'='+value)

new=empty(VERSION,'BTC-USDT-SWAP')
assert validate(new,'BTC-USDT-SWAP') is new
assert 'archived_cycles' in new
for broken in [{**new,'schema_version':0},{**new,'instrument':'ETH-USDT-SWAP'},
                {**new,'cycles':{'1':{'state':'BUY_LIVE'}}},{**new,'archived_cycles':{}}]:
    try:validate(broken,'BTC-USDT-SWAP')
    except IncompatibleState:pass
    else:raise AssertionError('Accepted invalid state')

with tempfile.TemporaryDirectory() as d:
    d=Path(d)
    assert current_state(d)==d/'state/bot_state.json'
    assert current_fills(d)==d/'state/fills.sqlite'
    (d/'config.json').write_text('{"version":"0.7.17"}')
    assert archive_state(d)==d/'state/bot04_state.json'
    assert archive_fills(d)==d/'state/fills_v068.sqlite'
    (d/'config.json').write_text('{"version":"0.7.19"}')
    assert archive_state(d)==current_state(d)
    assert archive_fills(d)==current_fills(d)
    (d/'config.json').write_text('{"version":"bad"}')
    try:archive_state(d)
    except ValueError:pass
    else:raise AssertionError('Invalid archive version accepted')

manifest=json.loads((SH/'engine_manifest.json').read_text())
assert manifest['current_version']==VERSION
assert list(manifest['engines'])==[VERSION]
for dependency in manifest['engines'][VERSION]['dependencies']:
    assert (SH/dependency).is_file(),dependency

# Main safety-critical functions are pinned to reviewed AST (v0.7.21 scope only).
candidate=ast.parse((SH/'okx_bot.py').read_text())
references=json.loads((ROOT/'tests/reference_safety_ast.json').read_text())
new_ast={n.name:n for n in candidate.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
strategy_tree=ast.parse((SH/'okx_strategy.py').read_text())
strategy_factory=next(n for n in strategy_tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_grid_rules')
new_ast.update({n.name:n for n in strategy_factory.body if isinstance(n,ast.FunctionDef)})
unchanged=['RestGovernor','ExchangeUnresolved','ExchangeTemporary','place','cancel_tp',
    'place_virtual_tp','virtual_escape','reconcile_position','reconcile_panic_orders',
    'place_panic_sell','controlled_shutdown','market_exit_partial','partial_cleanup_action',
    'TPBufferScheduler','coverage_audit','WSWatcher','PublicBidWatcher',
    'grid_top','desired_buy_levels','missing_buy_levels_for_addition',
    'plan_buy_buffer_cleanup','tp_buffer_candidates','level_free',
    'format_okx_report','BotDrawdownSampler']
def operational_ast(node):
    node=copy.deepcopy(node)
    if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant) and isinstance(node.body[0].value.value,str):
        node.body.pop(0)  # Docstring is presentation, not trading behavior.
    return ast.dump(node,include_attributes=False)

for name in unchanged:
    assert name in new_ast and name in references,name
    actual=hashlib.sha256(operational_ast(new_ast[name]).encode()).hexdigest()
    assert actual==references[name],('TRADING BEHAVIOR CHANGED',name)
assert 'migrate_cycle_records' not in new_ast
assert 'run_tests' not in new_ast
assert '_legacy_exchange_fills' not in new_ast
assert 'bot04_state_before' not in (SH/'okx_bot.py').read_text()
assert 'ALLOWED =' not in (SH/'okx_bot.py').read_text()
assert 'OKX_BOT04' not in (SH/'okx_bot.py').read_text()
assert 'OKX_BOT05' not in (SH/'okx_bot.py').read_text()
assert not any(SH.glob('okx_monitor[0-9]*.py'))
assert not any(SH.glob('okx_monitor_compat*.py'))
assert not (SH/'okx_runtime_paths.py').exists()

# Manager and monitoring modules import without contacting the exchange.
for name in ['okx_monitor','okxbot','okx_ui','okx_setup','okx_observers','okx_archive_reader']:
    importlib.import_module(name)

# All distributed Python sources compile, including Telegram read-only components.
for p in ROOT.rglob('*.py'):
    compile(p.read_bytes(),str(p),'exec')
print('PASS: contracts, state, archive, stable paths, import smoke')
print('PASS: critical trading AST guarded ('+str(len(unchanged))+' symbols); v0.7.22 shutdown/WS unchanged; display-only report updated')
print('PASS: legacy migrations absent; all Python syntax valid')
print('OFFLINE ONLY — no OKX connection or orders')

# Current error in 0.7.18 was monitor compatibility import ordering. The new
# module has no compat chain and must import cleanly.
assert not any(p.name.startswith('okx_monitor_compat') for p in SH.glob('*.py'))

# Critical last-LIVE Telegram mismatch hotfix stays in candidate unchanged
# except for the path import.
observer=(SH/'okx_execution_observer.py').read_text()
events=(ROOT/'okx_telegram_events.py').read_text()
assert 'paired_samples' in observer and 'paired_samples' in events

# Archive report puts verified terminal net PnL BEFORE the stale pre-PANIC snapshot.
import okxbot
with tempfile.TemporaryDirectory() as d:
    p=Path(d);(p/'state').mkdir()
    (p/'config.json').write_text('{"version":"0.7.18","instrument":"ZEC-USDT-SWAP"}')
    (p/'state/final_stats.json').write_text(json.dumps({
        'schema_version':1,'complete':True,'instrument':'ZEC-USDT-SWAP',
        'mode':'PANIC_DONE','report':'FINAL CONFIRMED +39.02614715'}))
    orig=(okxbot.find_entry,okxbot.bot_path,okxbot.describe,okxbot.unit_name,
          okxbot.read_stats_snapshot,okxbot.with_latest_bot_dd)
    okxbot.find_entry=lambda name:(name,{'path':str(p),'service':'okx-grid-zec0710'})
    okxbot.bot_path=lambda entry:p
    okxbot.describe=lambda entry:'ZEC-USDT-SWAP'
    okxbot.unit_name=lambda entry:'okx-grid-zec0710.service'
    okxbot.read_stats_snapshot=lambda path,instrument:('HISTORICAL SNAPSHOT',0)
    okxbot.with_latest_bot_dd=lambda report,path,instrument:report
    try:
        text=okxbot.stats_text('zec0710')
        assert text.index('FINAL CONFIRMED')<text.index('HISTORICAL SNAPSHOT')
    finally:
        (okxbot.find_entry,okxbot.bot_path,okxbot.describe,okxbot.unit_name,
         okxbot.read_stats_snapshot,okxbot.with_latest_bot_dd)=orig
print('PASS: archive final report displayed before historical snapshot')

# API extraction checks: DRY RUN forbids writes, GET is bounded, POST unknown
# outcome never retries. All fake responses are in-memory, no socket access.
from okx_api import build_api
import threading
class DummyRest:
    def __init__(self): self.begins=0;self.ends=0
    def begin(self,*args,**kwargs):self.begins+=1;return False
    def end(self,*args):self.ends+=1
    def throttle(self,*args):pass
class DummyGate:
    def take(self):pass
class Unknown(RuntimeError):pass
class Temporary(RuntimeError):pass
class Limited(Temporary):pass
class Expired(RuntimeError):pass
class Singleflight:
    def run(self,identity,fn):return fn()

COUNT={'flush':0}
def flush():COUNT['flush']+=1

def api_factory(live, gate):
    return build_api(ENV={'OKX_API_KEY':'TEST','OKX_API_SECRET':'TEST',
        'OKX_API_PASSPHRASE':'TEST'},CFG={'fee_group_id':'','buy_base_qty':'1'},
        COIN='BTC',INST='BTC-USDT-SWAP',LIVE=live,VER=VERSION,
        D=lambda x:Decimal(str(x or '0')),die=lambda text: (_ for _ in ()).throw(RuntimeError(text)),
        CONTRACT_LOT=Decimal('1'),CONTRACT_MIN=Decimal('1'),CONTRACT_VALUE=Decimal('1'),
        PRICE_STEP=Decimal('0.1'),QTY=Decimal('1'),STEP=Decimal('1'),TP=Decimal('2'),
        ExchangeTemporary=Temporary,ExchangeUnresolved=Unknown,
        OrderExpiredBeforeSend=Expired,RateLimitedError=Limited,
        GateUnavailable=RuntimeError,REST_GATE=gate,FILL_HISTORY_GATE=DummyGate(),
        ACCOUNT_POSITIONS_GATE=DummyGate(),FILL_FETCH_FLIGHT=Singleflight(),
        _PERF_LOCAL=threading.local(),_PERF_RECON_ENABLED=False,
        _flush_confirmed_facts=flush,latency_ms=lambda *a,**kw:None,
        fill_store=lambda:None)

gate=DummyRest();api=api_factory(False,gate)()
try:api.req('POST','/api/v5/trade/order',body={'test':'no'})
except RuntimeError as exc:assert 'Write operations disabled' in str(exc)
else:raise AssertionError('AUDIT allowed POST')
assert gate.begins==0
requests=[]
api._http=lambda *args:(requests.append(args) or (200,{'code':'0','data':[{'ok':1}]}))
assert api.req('GET','/api/v5/public/instruments') == [{'ok':1}]
assert gate.begins==gate.ends==1 and len(requests)==1

live_gate=DummyRest();live_api=api_factory(True,live_gate)()
called=[]
def uncertain(*args):
    called.append(args)
    raise OSError('synthetic network loss')
live_api._http=uncertain
try:live_api.req('POST','/api/v5/trade/order',body={'clOrdId':'synthetic'})
except Unknown as exc:assert 'UNKNOWN POST OUTCOME' in str(exc)
else:raise AssertionError('Uncertain POST accepted')
assert len(called)==1 and live_gate.begins==live_gate.ends==1
assert COUNT['flush']==1
print('PASS: API dry-run guard, GET response, uncertain POST single attempt')

# Accounting extraction: a NEW empty ledger starts at zero and never consumes
# old JSON. All SQLite activity is confined to a temporary test directory.
from okx_accounting import build_pnl_ledger
with tempfile.TemporaryDirectory() as d:
    database=Path(d)/'pnl.sqlite'
    ledger_type=build_pnl_ledger(D=lambda x:Decimal(str(x or '0')),
        STEP=Decimal('0.1'),A=None,RateLimitedError=Limited,
        allocated_market_fills=None,fmt_accounted_price=str,
        money=lambda x,places=6:str(x),money_usdt=str,bold=str,
        die=lambda s:(_ for _ in ()).throw(RuntimeError(s)),PNL_DB=database)
    ledger=ledger_type(Decimal('0.01'))
    assert ledger.total()==0 and ledger.total_all()==0
    assert database.exists()
print('PASS: new PnL ledger empty, no historic JSON migration')

# The extraction leaves API/PnL implementation methods identical except the
# explicitly reviewed API instrumentation/late FillStore lookup sites.
method_ref=json.loads((ROOT/'tests/reference_method_ast.json').read_text())
api_tree=ast.parse((SH/'okx_api.py').read_text())
acct_tree=ast.parse((SH/'okx_accounting.py').read_text())
def extracted_methods(tree, factory_name, class_name):
    factory=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==factory_name)
    cls=next(n for n in factory.body if isinstance(n,ast.ClassDef) and n.name==class_name)
    return {n.name:n for n in cls.body if isinstance(n,ast.FunctionDef)}

for name,methods,allow in (
    ('API',extracted_methods(api_tree,'build_api','API'),{'req','fills_by_order','instrument'}),
    ('PnLLedger',extracted_methods(acct_tree,'build_pnl_ledger','PnLLedger'),{'_initialize'})):
    assert set(methods)==set(method_ref[name])
    for method,node in methods.items():
        if method in allow:continue
        actual=hashlib.sha256(operational_ast(node).encode()).hexdigest()
        assert actual==method_ref[name][method],(name,method)
print('PASS: API/PnL extracted method bodies retained (except explicit late binding)')
