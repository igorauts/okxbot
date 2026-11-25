"""Manager-side read API only. No network, no writes, no engine dependency."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
import re
import sqlite3
from collections import OrderedDict
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
    last_complete_ms: Optional[int] = None

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
    last_complete = row.get('last_complete_ms')
    require(synced >= created, 'INVALID_SYNC_TIME')
    require(last_complete is None or created <= integer_ms(last_complete) <= synced, 'INVALID_COMPLETE_TIME')
    return FundingValue(instrument, created, Decimal(raw), last, synced, bool(row['complete']), last_complete)


def initial_line():
    """Display placeholder only, not a FundingValue or confirmed monetary total."""
    return 'FUNDING: +0.000000, ещё не обновлялся'


# Read-only, bounded, process-local cache. Identity includes DB and creation time;
# never carry a previous instance's funding into a new bot on the same instrument.
_LAST_GOOD = OrderedDict()
WARNING_MS = 8 * 3600 * 1000

@dataclass(frozen=True)
class FundingDisplay:
    amount: Decimal
    snapshot: Optional[FundingValue]
    warning: bool
    fallback: bool
    unavailable: bool = False

    @property
    def estimated(self):
        return self.fallback or self.snapshot is None or not self.snapshot.complete or self.warning

    def note(self):
        if self.warning:
            return '⚠ Funding: нет полной актуальной сверки >8ч'+(' (база недоступна)' if self.unavailable else '')
        if self.unavailable:
            return 'база недоступна; последний доступный снимок' if self.snapshot is not None else 'нет данных (база недоступна)'
        if self.snapshot is None:
            return 'пока нет данных'
        if self.fallback:
            return 'последний доступный снимок'
        if not self.snapshot.complete:
            return 'неполная история'
        return ''

    def manager_line(self, now_ms):
        if self.snapshot is None:
            line = ('FUNDING: +0.000000, нет данных (база недоступна)'
                    if self.unavailable else initial_line())
        else:
            line = self.snapshot.line(now_ms)
        note = self.note()
        return line + (' · '+note if note and (self.snapshot is not None or self.warning) else '')


def display_funding(path, instrument, created_at, *, now_ms=None):
    """One policy for manager and Telegram, never a source of trading facts.

    Use last persisted snapshot, including incomplete/stale, or last validated
    in-process snapshot on read failure; otherwise use an explicitly estimated 0.
    Legacy incomplete rows without a completeness watermark use creation time.
    """
    now = int(time.time()*1000) if now_ms is None else integer_ms(now_ms)
    try:
        created = creation_ms(created_at)
        require(created <= now, 'FUTURE_CREATED_AT')
        key = (str(Path(path).absolute()),instrument,created)
        require(isinstance(instrument,str), 'INVALID_INSTRUMENT')
    except (ValueError,TypeError,ArithmeticError,OSError):
        return FundingDisplay(Decimal(0),None,False,True)
    fallback = False
    unavailable = False
    try:
        snap = load_funding(path,instrument,created_at,terminal=False)
        if snap is not None:
            require(snap.synced_ms <= now, 'FUTURE_SYNC_TIME')
            _LAST_GOOD[key] = snap
            _LAST_GOOD.move_to_end(key)
            while len(_LAST_GOOD)>256:
                _LAST_GOOD.popitem(last=False)
        else:
            fallback = True
            db=Path(path)
            unavailable=not db.is_file() or db.is_symlink()
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ArithmeticError):
        snap = None
        fallback = True
        unavailable = True
    if snap is None:
        snap = _LAST_GOOD.get(key)
        if snap is not None and snap.synced_ms > now:
            snap = None
    last_complete = (snap.synced_ms if snap.complete else snap.last_complete_ms) if snap else None
    return FundingDisplay(snap.amount if snap else Decimal(0), snap,
                          now-(last_complete if last_complete is not None else created)>WARNING_MS, fallback, unavailable)
