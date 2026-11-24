"""Manager-side read API only. No network, no writes, no engine dependency."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
import re
import time
from typing import Optional
from okx_funding_ledger import read_snapshot, integer_ms, require, creation_ms

@dataclass(frozen=True)
class FundingValue:
    instrument: str
    created_ms: int
    amount: Decimal
    last_bill_ms: Optional[int]
    synced_ms: int
    complete: bool  # internal diagnostic; never suppress known amount in UI

    def age_text(self, now_ms=None):
        now_ms = int(time.time()*1000) if now_ms is None else integer_ms(now_ms)
        seconds = max(0, (now_ms-self.synced_ms)//1000)
        hours, rem = divmod(seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        return f'{hours}ч {minutes:02d}м {seconds:02d}с назад'

    def line(self, now_ms=None):
        with localcontext() as ctx:
            ctx.prec = 320
            amount = f'{self.amount:+.6f}'
        return f'FUNDING: {amount.replace("-", "−")}, {self.age_text(now_ms)}'

    def add_to(self, current_total):
        """Pure arithmetic on an already loaded value; no I/O, suitable for later cache."""
        require(isinstance(current_total, Decimal) and current_total.is_finite(), 'INVALID_TOTAL')
        with localcontext() as ctx:
            ctx.prec = 320
            return current_total + self.amount


def load_funding(path, instrument, created_at, *, terminal):
    """Call outside trading loop. Explicit terminal gate; no archive lookup.

    No age cutoff. Absence is not a verified zero. Caller retains its existing
    report if no snapshot has ever been published, or on local read error.
    This module deliberately does not alter the manager report yet (F8).
    """
    require(type(terminal) is bool, 'INVALID_TERMINAL_FLAG')
    if terminal:
        return None
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return None
    row = read_snapshot(path, instrument, created_at)
    if row is None:
        return None
    synced = integer_ms(row['synced_ms'])
    last = row['last_bill_ms']
    created = creation_ms(created_at)
    require(last is None or created <= integer_ms(last) <= synced, 'INVALID_LAST_BILL')
    require(row['complete'] in (0, 1), 'INVALID_COMPLETENESS')
    # A sum can be wider than a single API amount.
    raw = row['amount']
    require(isinstance(raw, str) and len(raw) <= 320 and
            re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', raw) is not None, 'INVALID_SNAPSHOT_AMOUNT')
    return FundingValue(instrument, created, Decimal(raw), last, synced, bool(row['complete']))


def initial_line():
    """Display placeholder only, not a FundingValue or confirmed monetary total."""
    return 'FUNDING: +0.000000, ещё не обновлялся'
