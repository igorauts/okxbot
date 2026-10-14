# OKX Grid Bot v0.6.9 — Safety Fixes

## Changes

- Fixed definitive OKX BUY order rejections.
- Fixed TP placement and cancellation rejection handling.
- Improved handling of uncertain POST outcomes.
- Prevented unresolved BUY orders from blocking unrelated TP tracking.
- Added account-wide AUTO SOFT STOP on insufficient margin.
- All bots sharing the account enter SOFT STOP together.
- Existing BUY orders are canceled through verified individual requests.
- Existing real and virtual TP continue to be managed.
- AUTO SOFT STOP persists across restarts.
- Resuming trading requires explicit manual intervention.
- Added persistent warnings for prolonged unresolved states.
- Preserved existing SQLite databases and trading history.

## Validation

- 18 automated safety tests passed.
- Full-module import and state migration verified.
- Read-only OKX audits passed for ETH and SOL.
- LIVE deployment verified on both running bots.

## Unchanged

- Grid trading strategy.
- TP calculation.
- SQLite database schema.
- Existing trading configuration.

Performance optimizations are reserved for v0.7.0.
