#!/usr/bin/env python3
"""Terminal-only GET/SQLite accounting; NEVER submits or cancels orders.

Uses the exact trade and commission fills already verified by the engine.
Fail-closed: an incomplete/foreign fill or mismatched position is not a final PnL.
"""
import json
import os
import fcntl
import hashlib
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def dec(x):
    d=Decimal(str(x))
    if not d.is_finite():raise ValueError('Nonfinite amount')
    return d


def _fmt(x):
    d=dec(x)
    return f'{"+" if d>=0 else "−"}${abs(d):,.8f}'


def _group_fills(rows, cv):
    seen=set()
    grouped={}
    for f in rows:
        oid=str(f.get('ordId') or '')
        bill=str(f.get('billId') or '')
        if not oid or not bill:raise ValueError('Fill without order or bill ID')
        if (oid,bill) in seen:continue
        seen.add((oid,bill))
        if f.get('feeCcy')!='USDT' or f.get('execType') not in ('M','T'):
            raise ValueError('Unverifiable fill fee/liquidity')
        if f.get('fee') in (None,'') or f.get('side') not in ('buy','sell'):
            raise ValueError('Incomplete fill')
        q=dec(f['fillSz']); p=dec(f['fillPx']); fee=dec(f['fee'])
        if min(q,p)<=0:raise ValueError('Nonpositive fill')
        g=grouped.setdefault(oid,{'qty':Decimal(0),'notional':Decimal(0),
                                   'fee':Decimal(0),'side':f['side'],'rows':[]})
        if g['side']!=f['side']:raise ValueError('Mixed fill sides')
        g['qty']+=q;g['notional']+=q*p*cv;g['fee']+=fee;g['rows'].append(f)
    return grouped


def _open_cycle_components(engine, cycles, grouped, cv, bid):
    """Split pre-terminal partial realization from remaining BUY acquisition cost."""
    partial=Decimal(0); remaining_cost=Decimal(0)
    unrealized=Decimal(0); open_fees=Decimal(0); open_qty=Decimal(0)
    for c in cycles:
        if c.get('state') in ('CLOSED','EXITED','CANCELED'):
            continue
        bought=dec(c.get('filledBuy') or 0)
        if not bought:continue
        b=grouped.get(str(c.get('ordId') or ''))
        if not b or b['side']!='buy' or b['qty']!=bought:
            raise ValueError('Missing/mismatched open BUY fills')
        legs=[r for r in c.get('tpOrders',[]) if dec(r.get('filled') or 0)>0]
        if not legs and dec(c.get('filledTP') or 0):
            legs=[{'ordId':c.get('tpOrdId'),'filled':c['filledTP']}]
        sold_qty=sold_nominal=sold_fee=Decimal(0)
        for leg in legs:
            g=grouped.get(str(leg.get('ordId') or ''))
            if not g or g['side']!='sell':raise ValueError('Missing partial TP fills')
            if leg.get('source')=='aggregate_exit':
                alloc=engine.allocated_market_fills(g['rows'],leg['aggregatePlan'],c['cycleId'])
                q=sum((dec(f['fillSz']) for f in alloc),Decimal(0))
                n=sum((dec(f['fillSz'])*dec(f['fillPx'])*cv for f in alloc),Decimal(0))
                fees=sum((dec(f['fee']) for f in alloc),Decimal(0))
            else:
                q,n,fees=g['qty'],g['notional'],g['fee']
            if q!=dec(leg['filled']):raise ValueError('Mismatched partial TP allocation')
            sold_qty+=q;sold_nominal+=n;sold_fee+=fees
        if sold_qty!=dec(c.get('filledTP') or 0):
            raise ValueError('Open cycle TP quantity mismatch')
        remaining=bought-sold_qty
        if remaining<0:raise ValueError('TP larger than BUY')
        price=b['notional']/(bought*cv)
        fraction=sold_qty/bought
        # BUY fee is allocated exactly once between realized and remaining lots.
        partial+=sold_nominal-sold_qty*price*cv+b['fee']*fraction+sold_fee
        rem_fee=b['fee']*(remaining/bought)
        remaining_cost+=remaining*price*cv-rem_fee
        unrealized+=remaining*(bid-price)*cv
        open_fees+=rem_fee
        open_qty+=remaining
    return dict(partial=partial,remaining_cost=remaining_cost,
                mark_unrealized=unrealized,remaining_buy_fees=open_fees,
                open_qty=open_qty)


def compute(engine, state, fills, *, bid, snapshot=None):
    """Pure deterministic calculation. No APIs, writes, or position mutation."""
    snap=snapshot or state.get('terminal_snapshot') or {}
    mode=state.get('terminal_mode')
    if mode not in ('PANIC_DONE','RETIRED'):
        raise ValueError('Bot has not finalized HARD/PANIC')
    cv=dec(engine.CONTRACT_VALUE)
    if cv<=0:raise ValueError('Invalid contract value')
    bid=dec(bid)
    if bid<=0:raise ValueError('Missing terminal BID; no approximate snapshot')
    regular_expected=engine.expected_filled_orders(state)
    stats=engine.full_stats_snapshot(state,fills,bid,cv)
    if not stats['complete']:raise ValueError('Incomplete ordinary fills')
    grouped=_group_fills(fills,cv)
    cycles=engine.all_stat_cycles(state)
    pieces=_open_cycle_components(engine,cycles,grouped,cv,bid)
    if stats['open_contracts']!=pieces['open_qty']:
        raise ValueError('Open cycle position mismatch')
    if snap.get('position_contracts') is not None and dec(snap['position_contracts']) != pieces['open_qty'] and mode=='RETIRED':
        raise ValueError('Hard terminal position differs from recorded cycles')
    panic_net=None
    panic_gross=panic_sell_fee=panic_qty=Decimal(0)
    if mode=='PANIC_DONE':
        if snap.get('position_contracts') is not None and dec(snap['position_contracts'])!=0:
            raise ValueError('PANIC terminal position is not zero')
        seen_panic=set()
        for rec in state.get('panic_orders',[]):
            oid=str(rec.get('ordId') or '')
            qty=dec(rec.get('filled') or 0)
            if not oid or oid in seen_panic or rec.get('state') not in ('filled','canceled','mmp_canceled'):
                raise ValueError('PANIC order identity/outcome unconfirmed')
            seen_panic.add(oid)
            g=grouped.get(oid)
            if qty>0:
                if not g or g['side']!='sell' or g['qty']!=qty or oid in regular_expected:
                    raise ValueError('PANIC fill not exactly confirmed')
                panic_qty+=qty;panic_gross+=g['notional'];panic_sell_fee+=g['fee']
        if panic_qty!=pieces['open_qty']:
            raise ValueError(f'PANIC executed {panic_qty}, residual BUY {pieces["open_qty"]}')
        panic_net=panic_gross+panic_sell_fee-pieces['remaining_cost']
    total=(stats['grid_gain']+stats['partial_exit_net']+pieces['partial']+
           (panic_net if panic_net is not None else Decimal(0))) if mode=='PANIC_DONE' else None
    return dict(mode=mode,grid_profit=stats['grid_gain'],
                other_closed_profit=stats['partial_exit_net']+pieces['partial'],
                panic_pnl=panic_net,final_trade_pnl=total,
                panic_sell_qty=panic_qty,panic_sell_notional=panic_gross,
                panic_sell_fee=panic_sell_fee,
                position_contracts=(Decimal(0) if mode=='PANIC_DONE' else pieces['open_qty']),
                position_base=(Decimal(0) if mode=='PANIC_DONE' else pieces['open_qty']*cv),
                frozen_unrealized=(pieces['mark_unrealized'] if mode=='RETIRED' else None),
                remaining_buy_fee=(pieces['remaining_buy_fees'] if mode=='RETIRED' else None),
                bid=bid,closed_cycles=stats['closed_count'])


def format_report(result,coin,closed_at):
    mode=result['mode']
    rows=[f'БОТ ЗАВЕРШЁН — {"PANIC SELL" if mode=="PANIC_DONE" else "HARD STOP"}',
          f'Остановлен:              {closed_at}',
          f'GRID PROFIT:             {_fmt(result["grid_profit"])}',
          f'Другие закрытые выходы:  {_fmt(result["other_closed_profit"])}']
    if mode=='PANIC_DONE':
        rows += [f'PANIC SELL PnL:          {_fmt(result["panic_pnl"])}',
                 f'PANIC SELL объём:       {result["panic_sell_qty"]} контр.',
                 f'PANIC SELL комиссии:    {_fmt(result["panic_sell_fee"])}',
                 f'Открытая позиция:       0 {coin}',
                 '--------------------------------------------',
                 f'ИТОГОВЫЙ PnL:           {_fmt(result["final_trade_pnl"])}',
                 '✅ Финальный торговый результат подтверждён исполнениями OKX.']
    else:
        rows += [f'Позиция при остановке:  {result["position_base"]} {coin}',
                 f'BID при остановке:      ${result["bid"]}',
                 f'Нереализованный PnL:    {_fmt(result["frozen_unrealized"])}',
                 f'Комиссии открытых BUY: {_fmt(result["remaining_buy_fee"])}',
                 '⚠️ Позиция оставлена открытой. ИТОГОВЫЙ PnL НЕ ОПРЕДЕЛЁН.']
    rows.append('Фандинг не включён. Все суммы — USDT, после торговых комиссий, кроме отдельного нереализованного PnL.')
    return '\n'.join(rows)


def _finalize_locked(engine, *, max_wait_seconds=0):
    """GET-only finalizer; safe on a retired bot, never touches trading orders."""
    target=engine.ROOT/'state'/'final_stats.json'
    deadline=time.monotonic()+max(0,max_wait_seconds)
    last_exc=None
    while True:
        try:
            state_bytes=engine.DATA.read_bytes()
            state=json.loads(state_bytes)
            state_sha=hashlib.sha256(state_bytes).hexdigest()
            mode=state.get('terminal_mode')
            if mode not in ('PANIC_DONE','RETIRED'):
                raise ValueError('Bot is not terminal')
            if target.exists():
                previous=json.loads(target.read_text())
                if (previous.get('complete') is True and previous.get('mode')==mode
                    and previous.get('instrument')==engine.INST
                    and previous.get('terminal_state_sha256')==state_sha):
                    return previous
                if previous.get('complete') is True:
                    raise ValueError('Existing verified final result differs from terminal state; manual audit')
            snap=state.get('terminal_snapshot') or {}
            if mode=='RETIRED' and not snap.get('bid'):
                raise ValueError('No frozen HARD BID; refuse current market substitution')
            # For historical v0.7.6 PANIC we do NOT invent a shutdown BID: it
            # cancels in the formula for realized closed positions. A positive
            # synthetic marker is used only to satisfy legacy stats calculation.
            bid=dec(snap['bid']) if snap.get('bid') else Decimal('1')
            expected=engine.expected_filled_orders(state)
            for rec in state.get('panic_orders',[]):
                qty=dec(rec.get('filled') or 0)
                if qty:
                    oid=str(rec.get('ordId') or '')
                    if not oid or oid in expected:raise ValueError('PANIC order identity collision')
                    expected[oid]=qty
            engine.FILL_STORE.bootstrap(state)
            rows=engine.FILL_STORE.ensure_expected(expected)
            result=compute(engine,state,rows,bid=bid,snapshot=snap)
            ack_file=engine.ROOT/'state/control_ack.json'
            ack=json.loads(ack_file.read_text()) if ack_file.is_file() else {}
            closed_at=(snap.get('at_utc') or ack.get('updated_at') or
                       datetime.now(timezone.utc).isoformat())
            report=format_report(result,engine.COIN,closed_at)
            payload={'schema_version':1,'complete':True,'instrument':engine.INST,
                     'mode':mode,'source_bot_version':str(state.get('version','')),
                     'calculated_by':'0.7.7','closed_at':closed_at,
                     'terminal_state_sha256':state_sha,
                     'generated_at_unix':time.time(),'report':report,
                     'values':{key:(str(val) if isinstance(val,Decimal) else val)
                               for key,val in result.items()}}
            # A late worker must not make the just-proven report stale.
            if hashlib.sha256(engine.DATA.read_bytes()).hexdigest()!=state_sha:
                raise ValueError('Terminal state changed during reconciliation')
            engine.atomic_json(target,payload)
            engine.log('BOT_TERMINAL_FINALIZED',mode=mode,grid_pnl=str(result['grid_profit']),
                       final_pnl=(str(result['final_trade_pnl']) if result['final_trade_pnl'] is not None else ''),
                       remaining=str(result['position_contracts']))
            return payload
        except Exception as exc:
            last_exc=exc
            if time.monotonic()>=deadline:break
            time.sleep(min(5,max(0,deadline-time.monotonic())))
    raise RuntimeError('Terminal PnL NOT VERIFIED (retry GET-only later): '+type(last_exc).__name__+': '+str(last_exc)[:160])


def finalize(engine, *, max_wait_seconds=0):
    """Serialize manual and periodic GET-only finalizers per retired instance."""
    lockfile=engine.ROOT/'state'/'terminal_accounting.lock'
    fd=os.open(lockfile,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        return _finalize_locked(engine,max_wait_seconds=max_wait_seconds)
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)
