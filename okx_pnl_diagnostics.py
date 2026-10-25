"""Read-only PnL cross-check for active bot; no exchange calls or DB writes.

A mismatch in this snapshot is NOT evidence of missing money: background ledger
and the independently calculated 10-minute stats publish asynchronously.
"""
import json
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


def inspect(root, *, now=None):
    root=Path(root)
    now=time.time() if now is None else now
    state=root/'state'
    report_path=state/'stats.json'; ledger=state/'pnl.sqlite'
    if any(p.is_symlink() for p in (root,state,report_path,ledger)):
        raise ValueError('Symlink in PnL diagnostic inputs')
    if not report_path.is_file() or not ledger.is_file():
        return {'complete':False,'reason':'stats or ledger unavailable'}
    doc=json.loads(report_path.read_text())
    if not isinstance(doc,dict) or doc.get('complete') is not True:
        return {'complete':False,'reason':'last report incomplete'}
    report=(doc.get('performance') or doc.get('performance_v0724'))
    if not isinstance(report,dict):return {'complete':False,'reason':'financial snapshot unavailable'}
    expected=Decimal(str(report['grid_profit']))
    if not expected.is_finite():raise ValueError('Non-finite reported PnL')
    # Immutable SQLite URI, query_only and no WAL checkpoints or schema changes.
    import urllib.parse
    # Do not use immutable=1 here: it would ignore active WAL updates.
    uri='file:'+urllib.parse.quote(str(ledger.resolve()))+'?mode=ro'
    db=sqlite3.connect(uri,uri=True,timeout=2)
    try:
        db.execute('PRAGMA query_only=ON')
        rows=db.execute("SELECT net FROM accounted WHERE kind='CLOSED'").fetchall()
        total=sum((Decimal(str(x[0])) for x in rows),Decimal(0))
    finally:db.close()
    if not total.is_finite():raise ValueError('Non-finite PnL ledger')
    ts=float(doc.get('generated_at_unix') or 0)
    return {'complete':True,'ledger_grid_profit':str(total),
            'report_grid_profit':str(expected),'delta':str(total-expected),
            'closed_ledger_rows':len(rows),'report_generated_at':ts,
            'report_age_seconds':round(max(0,now-ts),1),
            'note':'Async ledger and report may differ transiently; never auto-correct balances'}
