"""Synthetic 365-day hourly funding for 10 instruments; no network."""
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import json, resource, sqlite3, statistics, tempfile, time
from okx_funding_ledger import apply_complete_windows
from okx_funding_view import load_funding
from test_funding_ledger import bill, T
H=3600000; D=24*H
insts=[f'ASSET{i}-USDT-SWAP' for i in range(10)]
start=T-365*D; bots={i:start/1000 for i in insts}
with tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'funding.sqlite'; t=time.perf_counter(); bid=0
    for day in range(0,365,30):
        a=start+day*D; b=start+min(365,day+30)*D; rows=[]
        for ts in range(a,b,H):
            for inst in insts:
                bid+=1; rows.append(bill(bid,inst,ts,amount='-0.0001234567890123'))
        apply_complete_windows(path,[(a,b,rows)],bots,b)
    build=time.perf_counter()-t
    assert bid==87600
    v=load_funding(path,insts[0],bots[insts[0]],terminal=False)
    assert v.amount==Decimal('-0.0001234567890123')*8760
    t=time.perf_counter()
    rows=[]
    for inst in insts:
        bid+=1; rows.append(bill(bid,inst,T,amount='-0.0001234567890123'))
    apply_complete_windows(path,[(T,T+H,rows)],bots,T+H)
    update=time.perf_counter()-t
    lat=[]
    for n in range(1000):
        i=insts[n%10];t=time.perf_counter_ns();v=load_funding(path,i,bots[i],terminal=False);lat.append((time.perf_counter_ns()-t)/1e6)
    t=time.perf_counter_ns()
    for i in range(100000): v.add_to(Decimal('123.45'))
    add=(time.perf_counter_ns()-t)/100000/1000
    with closing(sqlite3.connect(path)) as db:
        plan=db.execute('EXPLAIN QUERY PLAN SELECT amount,last_bill_ms,synced_ms,complete FROM snapshot WHERE inst_id=? AND created_ms=?',(insts[0],start)).fetchall()
        count=db.execute('SELECT count(*) FROM bills').fetchone()[0]
    print(json.dumps(dict(synthetic_records=count,build_seconds=round(build,4),hour_update_ms=round(update*1000,3),read_p50_ms=round(statistics.median(lat),4),read_p95_ms=round(sorted(lat)[949],4),read_max_ms=round(max(lat),4),pure_add_mean_us=round(add,4),db_bytes=path.stat().st_size,peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,query_plan=plan),indent=2))
