# Changelog

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
