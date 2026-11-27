"""Manager-side read API only. No network, no writes, no engine dependency."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
import re
import sqlite3
from collections import OrderedDict
import time
from typing import Optional
from okx_funding_ledger import (read_snapshot, integer_ms, require, creation_ms,
    terminal_identity, read_terminal_snapshot_status, terminal_horizon,
    terminal_env_token, read_terminal_local, terminal_failure, terminal_registry_conflict)

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


TERMINAL_DB = Path('/srv/okx-funding/funding.sqlite')

@dataclass(frozen=True)
class TerminalFunding:
    identity: tuple
    amount: Decimal
    complete: bool
    conflict: bool = False

    def add_to(self, trade):
        require(self.complete and isinstance(trade,Decimal) and trade.is_finite(),
                'TERMINAL_FINANCE_NOT_CONFIRMED')
        with localcontext() as ctx:
            ctx.prec=320
            return trade+self.amount


def terminal_funding(root, *, path=None):
    """Local only. Missing, corrupt, or not-yet-covered is never final zero."""
    try:
        ident,_=terminal_identity(root)
        db=TERMINAL_DB if path is None else path
        row,conflict=read_terminal_snapshot_status(db,ident)
        if (row is None or not row[3]) and not conflict:
            # The same previously verified live snapshot already covers the
            # last settlement. No network, no waiting for the next timer.
            try:
                if not terminal_registry_conflict(root,ident):
                    local=read_terminal_local(db,ident,terminal_env_token(root))
                    if local is not None:
                        row=local
            except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ArithmeticError):
                pass
        if row is None:
            return None
        raw,last,synced,complete=row
        require(isinstance(raw,str) and len(raw)<=320 and
                re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?',raw) is not None,'INVALID_TERMINAL_AMOUNT')
        require(complete in (0,1) and (not complete or integer_ms(synced)>=terminal_horizon(ident)), 'INVALID_TERMINAL_FUNDING_TIME')
        stop=max(ident[3],terminal_horizon(ident)) if ident[5]=='PANIC_DONE' else ident[3]
        require(last is None or ident[2]<=integer_ms(last)<stop, 'INVALID_TERMINAL_LAST_BILL')
        return TerminalFunding(ident,Decimal(raw),bool(complete) and not conflict,conflict)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ArithmeticError):
        return None


TERMINAL_REPORT_DEADLINE_MS = 90_000


def terminal_wait(root, *, path=None, now_ms=None):
    """Return (wait, reason) for a single final PANIC financial report.

    At most one minute tick plus a 30-second allowance after PANIC,
    including any remaining settlement minute. Never block Telegram on Funding I/O or retry forever.
    """
    db=TERMINAL_DB if path is None else path
    try:
        ident,_=terminal_identity(root)
        token=terminal_env_token(root)
        if terminal_registry_conflict(root,ident):
            return False,'OVERLAPPING_INSTRUMENT_LIFETIMES'
        reason=terminal_failure(db,ident,env_token=token)
        if reason:
            return False,reason
        now=int(time.time()*1000) if now_ms is None else integer_ms(now_ms)
        deadline=ident[3]+TERMINAL_REPORT_DEADLINE_MS
        return now<deadline,'INCOMPLETE_HISTORY_OR_COLLECTION_TIMEOUT'
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ArithmeticError) as exc:
        reason=str(exc)
        if not re.fullmatch(r'[A-Z0-9_]+',reason):
            reason='TERMINAL_EVIDENCE_UNAVAILABLE'
        return False,reason


def _terminal_usdt(amount):
    with localcontext() as ctx:
        ctx.prec=320
        return f'{"+" if amount>=0 else "−"}${abs(amount):,.8f}'


def terminal_report(root, final, *, path=None):
    """Render the historical trade report without editing the immutable archive."""
    text=final['report']
    # Old engines correctly stored only trading accounting. Preserve it and
    # replace the display label, not the file, when showing the unified result.
    text=text.replace('ИТОГОВЫЙ PnL:', 'ТОРГОВЫЙ PnL:')
    text='\n'.join(line for line in text.splitlines() if not line.startswith('Фандинг не включён.'))
    funding=terminal_funding(root,path=path)
    if funding is not None and funding.identity[4]!=final.get('terminal_state_sha256'):
        funding=None
    hard_note='\nHARD: позиция может оставаться открытой; Funding после остановки не включён.' if final.get('mode')=='RETIRED' else ''
    if funding is None or not funding.complete:
        conflict=funding is not None and funding.conflict
        wait,reason=(False,'LEDGER_CONTRADICTION') if conflict else terminal_wait(root,path=path)
        note=('FUNDING: расхождение после финализации; сохранённый результат' if conflict else 'FUNDING: не подтверждён')
        if not wait:
            note+=' ('+reason+')'
        if funding is not None:
            note+=f' (известно {_terminal_usdt(funding.amount)})'
        return text+'\n'+note+('\nИТОГОВЫЙ PnL: '+('ожидает финансовой финализации' if wait else 'не подтверждён') if final.get('mode')=='PANIC_DONE' else '')+hard_note
    text+='\nFUNDING: '+_terminal_usdt(funding.amount)
    if final.get('mode')=='PANIC_DONE':
        try:
            vals=final['values'];trade=Decimal(str(vals['final_trade_pnl']))
            with localcontext() as ctx:
                ctx.prec=320
                require(abs(Decimal(str(vals['grid_profit']))+Decimal(str(vals['other_closed_profit']))+Decimal(str(vals['panic_pnl']))-trade)<=Decimal('0.00000001'), 'INVALID_TRADE_TOTAL')
            text+='\nИТОГОВЫЙ PnL: '+_terminal_usdt(funding.add_to(trade))
        except (KeyError,ValueError,TypeError,ArithmeticError):
            text+='\nИТОГОВЫЙ PnL: не подтверждён'
    return text+'\nВсе суммы — USDT. Funding ограничен сроком жизни этого экземпляра.'+hard_note
