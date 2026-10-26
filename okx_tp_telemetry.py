"""Pure, diagnostics-only timestamps for virtual TP aggregation.

Wall clock is from local BID observation; invalid/skewed or missing timestamps
produce no fabricated latency. No trading decision must depend on this module.
"""
import math


def _ms(start, end):
    try:
        delta=(float(end)-float(start))*1000
        if math.isfinite(delta) and 0<=delta<=3_600_000:
            return round(delta,3)
    except (TypeError,ValueError,OverflowError):
        pass
    return None


def intent_timing(wanted, collection, intent_at, *, window_seconds=0.15):
    """Explain collection latency; NEVER select, sort or modify cycles."""
    collected_at=collection.get('startedAt')
    first_stamps=[]
    for c in wanted:
        ts=c.get('virtualTpCrossedAt')
        try:
            if ts is not None and math.isfinite(float(ts)) and float(ts)>0:
                first_stamps.append(float(ts))
        except (TypeError,ValueError,OverflowError):
            continue
    # A bid observed only in the main reconciled loop has no known crossing
    # instant. Never label collection start as the BID crossing timestamp.
    first=min(first_stamps) if first_stamps else None
    source='ws_bid' if first_stamps else 'crossing_time_unknown'
    early=set(collection.get('initialCycleIds',collection.get('cycles',())))
    later=[c for c in wanted if c.get('cycleId') not in early]
    result={'cross_source':source,'additional_cycles':len(later)}
    # Categorize later crossings by local BID timestamps, not by a guess about
    # when the account FIFO was specifically holding the current REST pass.
    # "after window" may include FIFO, other REST I/O, and scheduling.
    within=after=unknown=0
    for cycle in later:
        try:
            ts=float(cycle['virtualTpCrossedAt'])
            if not math.isfinite(ts) or collected_at is None:
                unknown+=1
            elif ts <= float(collected_at)+float(window_seconds):
                within+=1
            elif ts <= float(intent_at):
                after+=1
            else:
                unknown+=1
        except (KeyError,TypeError,ValueError,OverflowError):
            unknown+=1
    result.update(added_during_window=within,added_after_window=after,
                  added_unclassified=unknown)
    for key,a,b in (('tp_to_intent_ms',first,intent_at),
                    ('cross_to_collect_ms',first,collected_at),
                    ('collect_to_intent_ms',collected_at,intent_at)):
        n=_ms(a,b)
        if n is not None:result[key]=n
    return result
