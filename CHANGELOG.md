## v0.7.3 — 2026-10-14

- One BTC/ETH/SOL-USDT-SWAP LONG grid engine, from ETH/SOL live-tested v0.7.3 code.
- Independently configurable BUY target (1–25), temporary BUY buffer (0–5), near-BID protection (0–10 grid steps), SELL TP base (1–25), temporary SELL TP buffer (0–30).
- BUY following, TP 1500ms grace, one-at-a-time cleanup (>=100ms), virtual TP and aggregate reduce-only market exits.
- Instance wizard and manager select one universal engine. Optional read-only Burst and Execution monitors selectable per instance (off by default).
- Offline tests across BTC/ETH/SOL. No automatic LIVE upgrades or trading restarts.

## v0.7.2 — 2026-10-14

- ETH BUY Grid Following: protect near-BID entries during 25→30→25 buffer cleanup.
- Prefer canceling farthest obsolete BUY; recheck BID before cancel.
- Bidirectional 1.5s cleanup cooldown, deferred placement if no safe slot.
- Retain previous real/virtual TP, aggregate MARKET logic, PnL and ETH state.
- 61 offline tests, including 11 ETH regression tests; ETH LIVE migration completed.
- No changes to SOL v0.6.10.

# Changelog

## v0.6.10 — POST expiry, isolated TP recovery, Telegram monitoring

- Added OKX REST `expTime` header to BUY and virtual TP placement; deadlines are persisted before HTTP transmission.
- If placement ACK is lost, reconcile by exact client ID. Only new deadline-protected intents may be automatically resolved after three independent, complete negative history checks and a 120-second grace period; legacy unknown orders remain fail-closed.
- An uncertain SELL reserves its own quantity but no longer stops unrelated, separately covered virtual TP from being restored.
- Added optional external Telegram monitor with critical event notifications, service health checks, daily 09:00 Santiago-time digest, and read-only `/status` command.
- Updated manager engine discovery and systemd drop-in handling (applied to repository clone by `prepare_repository_v0610.py`).
- Updated main engine tests for the new version while keeping v0.6.8 historical regression tests.
- Removed obsolete references to unrelated exchanges from the engine code and renamed the OKX report formatter without changing the report output.
- State v0.6.9 migrates with a durable backup; existing SQLite tables, trading settings and LIVE modes remain unchanged.


## v0.6.9 — Safety Fixes

- Fixed explicit OKX BUY/TP rejection handling.
- Added account-wide persistent AUTO SOFT STOP on margin rejection.
- Both bots enter SOFT together; manual resume required.
- Improved uncertain-order reconciliation without duplicate POST.
- Prevented unresolved BUY from blocking unrelated TP tracking.
- Added handling for rejected cancellations and TP recovery.
- Added 18 automated safety tests.
- Preserved trading strategy, state and SQLite databases.


## v0.6.8 — Reliability & performance
- Introduced durable SQLite archive of verified exchange fills and completed PnL.
- One-time verified migration of previous fill history; fetch only missing order executions afterward.
- Reworked per-bot maximum drawdown sampling using reconciled state snapshots.
- Added retry for safe GET errors; uncertain POST outcomes and position mismatch now enter safe reconciliation wait.
- Added per-thread HTTPS keep-alive.
- Capped combined active BUY + real TP orders at 99 (25 BUY + 74 real TP).
- Fixed 24-hour profit after completed cycles are archived in SQLite.
- Fixed drawdown worker retry failures caused by five-element snapshots.
- Both fixes validated on running ETH and SOL bots.
- Trading strategy, minimum TP, funding exclusion and partial-BUY cleanup stay unchanged.

## v0.6.7 — Hotfix
- Added persistent per-bot maximum negative total PnL tracking; removed account-level equity monitoring.

## v0.6.6 — Trading update
- Added stale partially filled BUY cleanup and reduce-only market exit after attached TP cancellation.
- Added separate realized PnL for partial-market exits; confirmation commands standardized in English.

## v0.6.5 — Hotfix
- Reconciled partially filled BUY exposure and TP state; added tests for fill/cancel transitions.
