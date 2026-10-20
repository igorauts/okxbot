"""SQLite PnL ledger, isolated from order placement.

Built with explicit dependencies from one bot instance. No exchange calls at import.
"""
import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal


def build_pnl_ledger(*, D, STEP, A, RateLimitedError, allocated_market_fills,
                     fmt_accounted_price, money, money_usdt, bold, die, PNL_DB):
    class PnLLedger:
        """Non-trading cycle PnL ledger. All entries sourced from real OKX fills.

        Separate state file prevents a stats bug from mutating trading positions.
        Stored cycle keys are unique BUY+TP IDs; rearming a grid never double-counts.
        """
        def __init__(self, contract_value):
            self.contract_value=contract_value
            self.lock=threading.RLock()
            if self.contract_value<=0: die('Invalid contract size for profit accounting')
            self.tried={}
            self.retry_after={}
            self.initial_ids=set()
            self._initialize()
            self.recent_total=self.total()

        def _conn(self):
            PNL_DB.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            db=sqlite3.connect(str(PNL_DB),timeout=20)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS accounted ('
                       'id TEXT PRIMARY KEY, kind TEXT NOT NULL, net TEXT NOT NULL, '
                       'data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            os.chmod(PNL_DB,0o600)
            return db

        @contextlib.contextmanager
        def _managed_conn(self):
            db=self._conn()
            try:
                with db:yield db
            finally:db.close()

        def _initialize(self):
            with self.lock,self._managed_conn() as db:
                self.closed_sum=Decimal(0)
                self.all_sum=Decimal(0)
                self.known_ids=set()
                for key,kind,net in db.execute('SELECT id,kind,net FROM accounted'):
                    self.known_ids.add(key)
                    amount=D(net)
                    self.all_sum+=amount
                    if kind=='CLOSED':self.closed_sum+=amount

        def total(self):
            with self.lock:return self.closed_sum

        def total_all(self):
            with self.lock:return self.all_sum

        def recorded(self, cycle):
            key=self.identity(cycle)
            if not key:return False
            with self.lock:return key in self.known_ids

        @staticmethod
        def identity(cycle):
            if not cycle.get('ordId'):return None
            legs=[r for r in cycle.get('tpOrders',[]) if D(r.get('filled'))>0]
            if len(cycle.get('tpOrders',[]))>1:
                # Stable across TP replacement; distinct from pre-ledger keys.
                return 'chain:'+str(cycle['ordId'])
            if not legs and not cycle.get('tpOrdId'):return None
            oid=(legs[0]['ordId'] if legs else cycle.get('tpOrdId'))
            return str(cycle['ordId'])+':'+str(oid)

        def all_cycles(self, st):
            # Includes previously replaced grid levels: completed cycles are
            # archived by place() so their realized PnL must never disappear.
            previous=list(st.get('archived_cycles',[]))
            current=list(st['cycles'].values())
            return [x for x in previous+current if x.get('state') in ('CLOSED','EXITED')]

        @staticmethod
        def aggregate(fills, oid, size, side):
            if not fills: raise ValueError('Fills not yet available')
            q=Decimal('0'); amt=Decimal('0'); fee=Decimal('0'); liquidity=set(); ids=set()
            for f in fills:
                if str(f.get('ordId'))!=str(oid): raise ValueError('Unexpected fill order ID')
                if f.get('side') != side: raise ValueError('Wrong side for order fills')
                tid=f.get('tradeId')
                if not tid or tid in ids: raise ValueError('Missing/duplicate tradeId')
                ids.add(tid)
                fs=D(f.get('fillSz')); fp=D(f.get('fillPx'))
                if fs<=0 or fp<=0: raise ValueError('Invalid fill price or size')
                if f.get('feeCcy')!='USDT':
                    raise ValueError('Commission currency not USDT; cannot calculate USDT net')
                if f.get('fee') in (None,''): raise ValueError('Missing actual exchange fee')
                if f.get('execType') not in ('M','T'):
                    raise ValueError('Missing Maker/Taker execution type')
                q+=fs; amt+=fs*fp; fee+=D(f['fee']);liquidity.add(f['execType'])
            if q!=size: raise ValueError(f'Fill volume differs: {q} != {size}')
            return {'qty':q,'avg_px':amt/q,'fee':fee,'liquidity':'/'.join(sorted(liquidity))}

        def compute(self, cycle, buy_fills, sell_fills):
            amount=D(cycle.get('filledBuy'))
            if amount<=0 or amount!=D(cycle.get('filledTP')):
                raise ValueError('Incomplete cycle cannot have realized profit')
            b=self.aggregate(buy_fills,cycle['ordId'],amount,'buy')
            grouped={}
            for f in sell_fills:
                grouped.setdefault(str(f.get('ordId')),[]).append(f)
            legs=[r for r in cycle.get('tpOrders',[])
                  if D(r.get('filled'))>0]
            if not legs:
                legs=[{'ordId':cycle['tpOrdId'],'filled':str(amount)}]
            sold_qty=Decimal(0); sold_amt=Decimal(0); sell_fee=Decimal(0); liq=set()
            for leg in legs:
                oid=leg['ordId']; qty=D(leg['filled'])
                rows=grouped.get(str(oid),[])
                if leg.get('source')=='aggregate_exit':
                    rows=allocated_market_fills(rows,leg['aggregatePlan'],cycle['cycleId'])
                x=self.aggregate(rows,oid,qty,'sell')
                sold_qty+=qty;sold_amt+=x['avg_px']*qty
                sell_fee+=x['fee'];liq.update(x['liquidity'].split('/'))
            if sold_qty!=amount:
                raise ValueError('SELL chain volume mismatch')
            sell_avg=sold_amt/sold_qty
            base_qty=amount*self.contract_value
            gross=(sell_avg-b['avg_px'])*base_qty
            plan=(D(cycle['tpPx'])-D(cycle['buyPx']))*base_qty
            execution=gross-plan
            fees=b['fee']+sell_fee
            net=gross+fees
            return {'buy_id':cycle['ordId'],'sell_id':legs[-1]['ordId'],
                    'sell_ids':[x['ordId'] for x in legs],
                    'buy_px':str(b['avg_px']),'sell_px':str(sell_avg),
                    'qty_contracts':str(amount),'qty_base':str(base_qty),
                    'gross':str(gross),'planned_gross':str(plan),
                    'execution':str(execution),'fees':str(fees),'net':str(net),
                    'buy_liquidity':b['liquidity'],'sell_liquidity':'/'.join(sorted(liq)),
                    'kind':cycle.get('state','CLOSED'),
                    'accounted_at':datetime.now(timezone.utc).isoformat()}

        def print_closed(self, result, historical=False):
            buy=D(result['buy_px']); sell=D(result['sell_px'])
            steps=(sell-buy)/STEP
            total=self.total()
            earned=bold(money_usdt(result['net']))
            overall=bold(money_usdt(total))
            note=' | история' if historical else ''
            buy_s=fmt_accounted_price(buy); sell_s=fmt_accounted_price(sell)
            if result.get('kind')=='EXITED':
                print(f"🧹 PARTIAL MARKET EXIT {buy_s} → {sell_s} | "
                      f"realized {money_usdt(result['net'])} | not a GRID TP",flush=True)
                return
            print(f"💰 TP {buy_s} → {sell_s} | {money(steps,2)} шага "
                  f"| в копилку {earned} | всего {overall}{note}",flush=True)
            print(f"   факт {money(result['gross'])} "
                  f"(план {money(result['planned_gross'])}, "
                  f"execution {money(result['execution'])}, "
                  f"комиссия {money(result['fees'])}) "
                  f"| {result['buy_liquidity']}/{result['sell_liquidity']}",flush=True)

        def commit_completed(self, cycle, result, historical=False):
            """Durable idempotent INSERT of verified exchange fills and exact net."""
            key=self.identity(cycle)
            if not key:raise ValueError('Cannot account a closed cycle without order identity')
            with self.lock:
                if key in self.known_ids:return False
                kind=result.get('kind','CLOSED')
                with self._managed_conn() as db:
                    db.execute('INSERT INTO accounted VALUES (?,?,?,?)',
                        (key,kind,str(result['net']),json.dumps(result,ensure_ascii=False)))
                # Caches are changed ONLY after the SQL transaction is committed.
                self.known_ids.add(key)
                self.all_sum+=D(result['net'])
                if kind=='CLOSED':self.closed_sum+=D(result['net'])
                self.tried.pop(key,None)
                self.retry_after.pop(key,None)
                self.print_closed(result,historical=historical)
                return True

        def ingest_history(self, st, fills):
            """Re-use fills already fetched by the full statistics report.

            No extra REST traffic. Only CLOSE cycles with complete matched BUY/TP
            fills are committed; never infer a missing commission or quantity.
            """
            indexed={}
            for f in fills:
                indexed.setdefault(str(f.get('ordId')),[]).append(f)
            recovered=0
            for c in self.all_cycles(st):
                if not self.identity(c) or self.recorded(c):continue
                try:
                    sell_ids=[r['ordId'] for r in c.get('tpOrders',[]) if D(r.get('filled'))>0]
                    if not sell_ids:sell_ids=[c['tpOrdId']]
                    all_sell=[f for oid in sell_ids for f in indexed.get(str(oid),[])]
                    result=self.compute(c,indexed.get(str(c['ordId']),[]),all_sell)
                except ValueError:
                    continue  # Late fills: leave for normal worker retry.
                if self.commit_completed(c,result,historical=True):
                    recovered+=1
            return recovered

        def work(self, st, max_cycles=1, historical=True):
            # Read-only fill requests. All other threads may keep trading; only
            # the small file commit is serialized with full-stats ingestion.
            pending=[c for c in self.all_cycles(st) if self.identity(c) and not self.recorded(c)]
            if not pending:return
            pending.sort(key=lambda x: str(x.get('tpOrdId','')),reverse=True)
            checked=0
            for c in pending:
                key=self.identity(c)
                now=time.monotonic()
                with self.lock:
                    if now<self.retry_after.get(key,0): continue
                try:
                    buy=A.fills_by_order(c['ordId'],D(c['filledBuy']))
                    sell_legs=[(r['ordId'],D(r.get('aggregateFilledTotal',r['filled'])))
                               for r in c.get('tpOrders',[]) if D(r.get('filled'))>0]
                    if not sell_legs:sell_legs=[(c['tpOrdId'],D(c['filledTP']))]
                    sell=[f for oid,qty in sell_legs for f in A.fills_by_order(oid,qty)]
                    r=self.compute(c,buy,sell)
                    self.commit_completed(c,r,historical=key in self.initial_ids)
                except Exception as e:
                    with self.lock:
                        count=self.tried.get(key,0)+1
                        self.tried[key]=count
                        wait=min(120,5*count)
                        if isinstance(e,RateLimitedError):wait=max(wait,30)
                        self.retry_after[key]=time.monotonic()+wait
                    bp=c.get('buyPx','?');tp=c.get('tpPx','?')
                    transient=(isinstance(e,RateLimitedError) or
                        (isinstance(e,ValueError) and
                         ('Fills not yet available' in str(e) or
                          'Fill volume differs' in str(e))))
                    if count==1:
                        print(f'⏳ TP {bp} → {tp} закрыт | ждём комиссии',flush=True)
                    if (not transient and (count==1 or count%10==0)):
                        print(f'⚠️ PnL {bp} → {tp}: данные требуют проверки: '
                              f'{type(e).__name__} {str(e)[:120]}',flush=True)
                    elif isinstance(e,RateLimitedError) and count==1:
                        print('⏳ OKX ограничил запросы истории; PnL продолжит расчёт позже',flush=True)
                checked+=1
                if checked>=max_cycles:break

        def status(self, st):
            unaccounted=sum(bool(self.identity(c)) and not self.recorded(c)
                            for c in self.all_cycles(st))
            return f'всего {money_usdt(self.total())} | PnL не рассчитан {unaccounted}'

    return PnLLedger
