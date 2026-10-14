# Changelog

## v0.6.7 — Hotfix
- Added per-bot maximum drawdown tracking based on total PnL.
- Removed account-wide equity monitoring.
- Added persistent drawdown history and 5-second local sampling.
- Drawdown is now displayed in bot statistics.
- Trading grid and order execution logic remain unchanged.

## v0.6.6 — Trading Update
- Added automatic cleanup of stale partially filled BUY orders.
- Default trigger: 60 seconds and 5 grid steps above BUY price.
- Added safe cancellation of attached TP before reduce-only MARKET exit.
- Added separate PnL accounting for partial MARKET exits.
- Standardized interactive confirmation commands to English.
- Improved recovery and duplicate-order protection.

## v0.6.5 — Hotfix
- Fixed partial BUY accounting and position coverage checks.
- Improved partial-fill recovery and TP handling.
- Added regression tests for partial executions.
