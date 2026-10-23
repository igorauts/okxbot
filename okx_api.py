"""One-instance OKX REST client. No exchange calls at import time.

Dependencies are explicit and injected by the engine. HTTP safety, FIFO,
GET retries and uncertain POST/DELETE outcomes retain their existing behavior.
"""
import base64
import hashlib
import hmac
import http.client
import json
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from okx_contracts import validate_spec


def build_api(*, ENV, CFG, COIN, INST, LIVE, VER, D, die,
              CONTRACT_LOT, CONTRACT_MIN, CONTRACT_VALUE, PRICE_STEP,
              QTY, STEP, TP, ExchangeTemporary, ExchangeUnresolved,
              OrderExpiredBeforeSend, RateLimitedError, GateUnavailable,
              REST_GATE, FILL_HISTORY_GATE, ACCOUNT_POSITIONS_GATE,
              FILL_FETCH_FLIGHT, _PERF_LOCAL, _PERF_RECON_ENABLED,
              _flush_confirmed_facts, latency_ms, fill_store):
    class API:
        """One verified TLS keep-alive connection PER THREAD, never shared across threads.

        Network errors on GET are safe to retry. POST/DELETE always have ONE
        network attempt, with durable intent and next-pass clOrdId reconciliation.
        """
        _local=threading.local()

        def _connect(self):
            conn=getattr(self._local,'conn',None)
            if conn is None:
                conn=http.client.HTTPSConnection('www.okx.com',timeout=12,
                        context=ssl.create_default_context())
                self._local.conn=conn
            return conn

        def _close(self):
            conn=getattr(self._local,'conn',None)
            self._local.conn=None
            if conn is not None:
                try:conn.close()
                except Exception:pass

        def _http(self, method,path,payload,headers):
            conn=self._connect()
            try:
                conn.request(method,path,body=payload.encode() if payload else None,
                             headers=headers)
                response=conn.getresponse()
                raw=response.read()
                status=response.status
                # Do not reuse a connection the server decided to close.
                if response.will_close:self._close()
                try:result=json.loads(raw.decode('utf-8'))
                except (ValueError,UnicodeError):
                    if status>=500:raise ExchangeTemporary(f'HTTP {status} invalid JSON')
                    raise ExchangeUnresolved(f'Unexpected OKX response HTTP {status}')
                return status,result
            except (OSError,TimeoutError,ConnectionError,http.client.HTTPException,
                    ssl.SSLError):
                self._close()
                raise

        def req(self, method, path, params=None, body=None, *, background=False, exp_time_ms=None):
            if method != 'GET' and not LIVE:
                die('Write operations disabled in DRY RUN')
            if method != 'GET':
                # Trading intents must already be durable; flush any older facts too.
                _flush_confirmed_facts()
            if params:path += '?' + urllib.parse.urlencode(params)
            route=path.split('?',1)[0]
            if exp_time_ms is not None:
                if method!='POST' or route!='/api/v5/trade/order' or not isinstance(exp_time_ms,int):
                    raise ValueError('expTime allowed only for an order placement')
                if int(time.time()*1000)>=exp_time_ms:
                    raise OrderExpiredBeforeSend('Order deadline expired locally before HTTP send')
            payload=json.dumps(body,separators=(',',':')) if body is not None else ''
            attempts=4 if method=='GET' else 1
            for attempt in range(attempts):
                p=getattr(_PERF_LOCAL,'current',None) if _PERF_RECON_ENABLED else None
                gate_start=time.perf_counter_ns()
                urgent=(p.run('rest_governor',REST_GATE.begin,method,route,background=background)
                        if p is not None else REST_GATE.begin(method,route,background=background))
                latency_ms('rest_gate',gate_start,method=method,route=route)
                limited=False
                try:
                    if method=='GET' and route=='/api/v5/trade/fills-history':
                        history_gate_start=time.perf_counter_ns()
                        try:
                            FILL_HISTORY_GATE.take()
                        except GateUnavailable as exc:
                            raise ExchangeTemporary('Account fills history gate unavailable') from exc
                        finally:
                            latency_ms('fills_history_shared_wait',
                                       history_gate_start,method=method,route=route)
                    if method=='GET' and route=='/api/v5/account/positions':
                        position_gate_start=time.perf_counter_ns()
                        try:
                            ACCOUNT_POSITIONS_GATE.take()
                        finally:
                            latency_ms('positions_gate',position_gate_start,method=method,route=route)
                    ts=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
                    sig=base64.b64encode(hmac.new(ENV['OKX_API_SECRET'].encode(),
                        (ts+method+path+payload).encode(),hashlib.sha256).digest()).decode()
                    headers={'User-Agent':'okxbot/'+VER,'Content-Type':'application/json',
                        'OK-ACCESS-KEY':ENV['OKX_API_KEY'],'OK-ACCESS-SIGN':sig,
                        'OK-ACCESS-TIMESTAMP':ts,'OK-ACCESS-PASSPHRASE':ENV['OKX_API_PASSPHRASE']}
                    if exp_time_ms is not None:headers['expTime']=str(exp_time_ms)
                    try:
                        p=getattr(_PERF_LOCAL,'current',None) if _PERF_RECON_ENABLED else None
                        http_start=time.perf_counter_ns()
                        http_status=None
                        try:
                            if p is not None:
                                status,result=p.run('rest_http',self._http,method,path,payload,headers)
                            else:
                                status,result=self._http(method,path,payload,headers)
                            http_status=status
                        finally:
                            latency_ms('http',http_start,method=method,route=route,
                                       status=http_status,attempt=attempt+1)
                    except ExchangeTemporary:
                        if method!='GET':raise ExchangeUnresolved('Uncertain POST response; saved intent must reconcile')
                        if attempt==attempts-1:raise
                        REST_GATE.throttle(route,min(12,2*(2**attempt)))
                        continue
                    except (OSError,TimeoutError,ConnectionError,http.client.HTTPException,
                            ssl.SSLError) as exc:
                        if method!='GET':
                            raise ExchangeUnresolved(
                                f'UNKNOWN {method} OUTCOME: {type(exc).__name__}; reconcile stored intent') from exc
                        if attempt==attempts-1:
                            raise ExchangeTemporary(f'GET {route} transport unavailable: {type(exc).__name__}') from exc
                        self._close()
                        REST_GATE.throttle(route,min(12,2*(2**attempt)))
                        continue
                    if status==429 or status>=500 or result.get('code')=='50011':
                        limited=True
                    elif status>=400:
                        raise ExchangeUnresolved(f'HTTP {status} {str(result.get("msg",""))[:120]}')
                    elif result.get('code')=='51603' and method=='GET' and route=='/api/v5/trade/order':
                        return []
                    elif (method=='POST' and route=='/api/v5/trade/order'
                          and isinstance(result.get('data'),list)
                          and len(result['data'])==1
                          and isinstance(result['data'][0],dict)
                          and str(result['data'][0].get('sCode','')) not in ('','0')):
                        # Definitive, explicit per-order rejection. Never classify a
                        # 5xx/429/transport timeout as a rejection. Check identity.
                        row=result['data'][0]
                        if (not body or not body.get('clOrdId') or
                            row.get('clOrdId')!=str(body['clOrdId']) or
                            row.get('ordId') not in (None, '')):
                            raise ExchangeUnresolved('Contradictory order rejection identity/ordId')
                        return result['data']
                    elif (method=='POST' and route=='/api/v5/trade/cancel-order'
                          and isinstance(result.get('data'),list)
                          and len(result['data'])==1
                          and isinstance(result['data'][0],dict)
                          and str(result['data'][0].get('sCode','')) not in ('','0')):
                        row=result['data'][0]
                        if (not body or not body.get('ordId') or
                            str(row.get('ordId'))!=str(body['ordId'])):
                            raise ExchangeUnresolved('Cancel rejection order ID mismatch')
                        return result['data']
                    elif result.get('code')!='0':
                        raise ExchangeUnresolved(f'OKX API {result.get("code")}: {str(result.get("msg"))[:120]}')
                    else:
                        return result.get('data',[])
                finally:
                    REST_GATE.end(urgent)
                if limited:
                    REST_GATE.throttle(route,min(20,2*(2**attempt)))
                    if method!='GET':
                        raise ExchangeUnresolved(f'Uncertain {method} response HTTP 5xx/429; durable intent remains')
                    if attempt==attempts-1:
                        raise RateLimitedError(f'GET {route} unavailable HTTP 5xx/429 after retries')
            raise ExchangeTemporary(f'GET {route} retry exhausted')

        def server_time_ms(self):
            rows=self.req('GET', '/api/v5/public/time')
            if len(rows)!=1 or not str(rows[0].get('ts') or '').isdigit():
                raise ExchangeTemporary('OKX server time unavailable')
            return int(rows[0]['ts'])

        def pending(self):
            # Return ALL relevant orders; fail closed if pagination looks strange.
            result=[]; after=None
            for _ in range(8):
                p={'instType':'SWAP','instId':INST,'limit':'100'}
                if after: p['after']=after
                page=self.req('GET','/api/v5/trade/orders-pending',p)
                result.extend(page)
                if len(page)<100: return result
                nxt=page[-1].get('ordId')
                if not nxt or nxt==after: die('Cannot paginate open orders')
                after=nxt
            die('Pending-order pagination overflow')

        def order(self, oid='', cid=''):
            p={'instId':INST}
            if oid: p['ordId']=oid
            else: p['clOrdId']=cid
            data=self.req('GET','/api/v5/trade/order',p)
            return data[0] if data else None

        def positions(self):
            return self.req('GET','/api/v5/account/positions',{'instId':INST})

        def ticker(self):
            data=self.req('GET','/api/v5/market/ticker',{'instId':INST})
            if not data: die('Ticker missing')
            return D(data[0]['bidPx'])

        def maker_fee(self):
            # OKX uses NEGATIVE values for commissions; POSITIVE for rebates.
            # For SWAP contracts fees are scoped by instFamily, not instId.
            params={'instType':'SWAP','instFamily':f'{COIN}-USDT'}
            rows=self.req('GET','/api/v5/account/trade-fee',params)
            if len(rows)!=1:die('Cannot read account VIP fees')
            groups=rows[0].get('feeGroup') or []
            group_id=str(CFG.get('fee_group_id') or '')
            if group_id and groups:
                matches=[g for g in groups if str(g.get('groupId'))==group_id]
                if len(matches)!=1:die('Account fee group has changed')
                tier=matches[0]
            elif len(groups)==1:
                tier=groups[0]
            elif not groups:
                tier=rows[0]
            else:die('Ambiguous OKX VIP fee group')
            raw=tier.get('maker')
            if raw in (None,''):die('Missing maker commission')
            rate=-D(raw)
            if rate>=1:die('Invalid maker commission')
            return max(Decimal(0),rate)

        def instrument(self):
            data=self.req('GET','/api/v5/public/instruments',{'instType':'SWAP','instId':INST})
            if len(data)!=1: die('Instrument missing')
            v=data[0]
            try:validate_spec(INST, v)
            except ValueError as exc:die(str(exc))
            if (D(v.get('ctVal'))!=CONTRACT_VALUE
                or D(v.get('tickSz'))!=PRICE_STEP or STEP%PRICE_STEP or TP%PRICE_STEP
                or QTY<D(v['minSz']) or QTY%D(v['lotSz'])
                or D(v['lotSz'])!=CONTRACT_LOT or D(v['minSz'])!=CONTRACT_MIN
                or QTY*CONTRACT_VALUE!=D(CFG['buy_base_qty'])):
                die('Instrument specs changed/incompatible; STOP before writes')
            return v

        def history(self):
            # Only use this for strong, exact TP ID correlation, never price matching.
            return self.req('GET','/api/v5/trade/orders-history',{'instType':'SWAP','instId':INST,'limit':'100'})

        def fills_by_order(self, order_id, expected_qty=None):
            """Actual fills by exact ordId; use durable verified records if complete.

            A BUY may remain open more than 90d and its fill disappear from OKX
            history before the TP closes. The locally persisted row stays valid.
            """
            if expected_qty is not None and fill_store() is not None and fill_store().initialized():
                saved=fill_store().get_rows([order_id])
                if sum((D(f['fillSz']) for f in saved),Decimal(0)) == D(expected_qty):
                    return saved
            def fetch_one():
                result=self.req('GET','/api/v5/trade/fills-history',{
                    'instType':'SWAP','instId':INST,'ordId':order_id,'limit':'100'
                },background=True)
                if len(result)>=100:
                    raise RuntimeError('100+ fills for one order; detailed pagination required')
                if any(str(f.get('ordId'))!=str(order_id) or f.get('instId')!=INST
                       for f in result):
                    raise RuntimeError('Order fill filter mismatch; financial record unsafe')
                if fill_store() is not None:fill_store().insert(result)
                return result
            result=FILL_FETCH_FLIGHT.run(order_id, fetch_one)
            # A waiting worker uses durable rows rather than another HTTP GET.
            if fill_store() is not None:
                stored=fill_store().get_rows([order_id])
                if (expected_qty is None or
                        sum((D(f['fillSz']) for f in stored),Decimal(0))==D(expected_qty)):
                    return stored
            return result

        def algo(self,cid):
            # The attached algo is only registered after parent BUY fully fills.
            data=self.req('GET','/api/v5/trade/order-algo',{'algoClOrdId':cid})
            return data[0] if data else None

    return API
