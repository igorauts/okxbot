# Telegram-only v0.7.68 — SQLite FD / MARKET retries / ACK, OFFLINE CANDIDATE

Parent: published v0.7.67 tested ZIP (`manager_reliability_offline_candidate.zip`),
SHA256 `26bf4d2e0b2ce2502c810867cb3b65509a321f13e40e1f2db1dca6d6f8f2caad`.

The initial offline candidate was not LIVE-ready; this release candidate is an
updated tested package after correction of the test-isolation issue. LIVE
Telegram modules were installed and monitored separately.


## Scope

- `okx_telegram_events.py`: explicit close of SQLite READ ONLY fills handle;
  FILLED-but-not-accounted MARKET events retain their durable queue record and
  retry in 60/120/240/... up to 900 seconds; 24h unresolved alarm preserved.
- `okx_telegram_views.py`: explicit close of pnl/fills/cycles READ ONLY SQLite;
  fixed categorical reason for each failure to confirm MARKET net PnL, without
  exposing IDs or amounts. No unverified PnL is emitted.
- `okx_telegram_followups.py`: explicitly close READ ONLY cycle/pnl connections.
- `okx_telegram_monitor.py`: command update offset is fsynced before a reply
  can be dispatched; a failed persistence never sends a reply; original 3-fail
  durable dead-letter applies to failures before send, while uncertain sends
  are at most once. Protect against false 0/0; log phase/type/errno; one FD
  pressure alert per episode at >=50% soft RLIMIT_NOFILE.
- `run_checks.sh` + `tests/test_telegram_fd_market_ack_vnext.py`: new offline
  regressions. Under gc.disable(), 1000 repeated MARKET accounting misses and
  1000 follow-up reads must not increase FD count. Existing v0.7.61 durable
  3-fail contract tests remain active and passing.

## Deliberately NOT changed

Manager runtime source (except package release identity), trading engine v0.7.64, shared position
Reader, StatsWorker, `okx_stats_incremental_core.py`, FIFO, order strategy,
Telegram service unit/systemd, state files, credentials and bot databases.
In particular the shared StatsWorker core's SQLite connection lifetime should
be audited as a distinct, explicitly authorized scope.

## Evidence so far

- New `tests.test_telegram_fd_market_ack_vnext`: 6 offline tests, PASS.
- Combined Telegram/Reader legacy and new regressions: 55 PASS.
- Legacy Telegram 0722 standalone: PASS.
- Market message presentation: 6 PASS.
- Additional statistics/archival scoped: 36 PASS.
- Full `run_checks.sh`: previously PASS 740 tests on the isolated pre-final
  candidate. The exact final ZIP must undergo the full suite again after the
  release metadata update; only that result authorizes GitHub publication.

## LIVE rollout gates (not performed)

1. Full offline suite PASS on supported isolated runner; independently
   validate patch inventory and source pins.
2. Read-only VPS preflight with Telegram STOPPED and no live source drift.
3. Separate approved Telegram-only installer with backup/rollback of four
   Telegram `.py` files and state snapshot, no changes to trading or Reader.
4. Controlled restart and FD/RSS telemetry trend; compare `0/0`/ACK counters.
5. GitHub atomic main/tag publisher with pinned tree ZIP parity, remote SHA,
   and NO test execution during publication.
