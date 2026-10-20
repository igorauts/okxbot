"""Pure grid/TP calculations. Trading I/O stays in the engine.

The functions retain the original behavior with explicit instance parameters.
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING


def build_grid_rules(*, STEP, TARGET, PRICE_STEP, BUY_CANCEL_PROTECT_STEPS,
                     REAL_TP_TARGET, CONTRACT_MIN, CONTRACT_LOT,
                     D, fmt, die, ExchangeUnresolved):
    def level_free(state,price):
        key=fmt(price)
        return key not in state['cycles'] or state['cycles'][key]['state'] in ('CLOSED','CANCELED','EXITED')

    def tp_quantity_placeable(quantity):
        return (quantity >= CONTRACT_MIN and quantity % CONTRACT_LOT == 0)

    def required_safe_tp(buy_price, maker_cost):
        if maker_cost<0 or maker_cost>=1:die('Invalid maker fee')
        exact=buy_price*(maker_cost*2)/(1-maker_cost)*Decimal('1.20')
        return max(PRICE_STEP,(exact/PRICE_STEP).to_integral_value(rounding=ROUND_CEILING)*PRICE_STEP)

    def grid_top(bid):
        top=(bid/STEP).to_integral_value(rounding=ROUND_FLOOR)*STEP
        if top>=bid: top-=STEP
        return top

    def desired_buy_levels(top,occupied_tp):
        wanted=set()
        price=top
        while len(wanted)<TARGET and price>0:
            if price not in occupied_tp: wanted.add(price)
            price-=STEP
        if len(wanted)<TARGET: die('Insufficient valid grid levels')
        return wanted

    def missing_buy_levels_for_addition(desired, active):
        """Only add BUYs which can survive the bottom-up 25-BUY cleanup.

        At/above target capacity, the 25 HIGHEST existing BUY prices are
        the retained grid. A candidate at/below its lowest price would be
        canceled immediately by bottom-up cleanup: never POST that churn.
        Below target capacity, keep normal full grid replenishment.
        """
        active_prices={D(c['buyPx']) for c in active}
        missing=sorted((px for px in desired if px not in active_prices),reverse=True)
        if len(active)>=TARGET:
            floor=sorted(active_prices,reverse=True)[TARGET-1]
            missing=[px for px in missing if px>floor]
        return missing

    def plan_buy_buffer_cleanup(active,fresh_bid,max_cancels):
        """Select the LOWEST priced, confirmed BUYs for excess-buffer cleanup.

        The existing exchange grid is authoritative when removing surplus BUYs.
        Desired levels are used only to decide where to ADD new BUYs, NEVER to
        select which existing BUY to cancel. The 5-step near-BID guard is a
        fail-closed safety check, not the order-selection strategy.

        Skip partially filled, PLACING, CANCELING and unknown-ID orders. Only
        fully live BUYs strictly BELOW (fresh BID - guard) may be canceled.
        """
        if fresh_bid is None or D(fresh_bid)<=0:
            raise ExchangeUnresolved('Missing fresh BID for BUY buffer cleanup')
        if max_cancels<=0:return [],[]
        protected=[]
        safe=[]
        cutoff=D(fresh_bid)-STEP*BUY_CANCEL_PROTECT_STEPS
        for c in active:
            if c.get('state')!='BUY_LIVE' or not c.get('ordId'):
                continue
            px=D(c['buyPx'])
            if px>=cutoff:
                protected.append(c)
            else:
                safe.append(c)
        # Always cancel bottom-up: smallest numerical BUY price first.
        safe.sort(key=lambda c:D(c['buyPx']))
        return safe[:max_cancels],protected

    def tp_buffer_candidates(active,bid):
        """Only excess REAL TP are eligible for burst cleanup.

        The 25 closest active SELL LIMITs to current BID are protected.  This
        ranking is recalculated from each reconciled snapshot (the 'base' is not
        a permanent tag on a cycle).  Ties are deterministic by price/order ID.
        Returns the surplus sorted farthest-first.
        """
        bid=D(bid)
        ranked=sorted(active,key=lambda row:(abs(D(row[1]['tpPx'])-bid),
                        D(row[1]['tpPx']),str(row[2].get('ordId') or '')))
        base_ids={id(rec) for _,_,rec in ranked[:REAL_TP_TARGET]}
        return sorted((row for row in active if id(row[2]) not in base_ids),
                      key=lambda row:(abs(D(row[1]['tpPx'])-bid),
                          D(row[1]['tpPx']),str(row[2].get('ordId') or '')),
                      reverse=True)

    return (level_free, tp_quantity_placeable, required_safe_tp, grid_top, desired_buy_levels, missing_buy_levels_for_addition, plan_buy_buffer_cleanup, tp_buffer_candidates)
