> **Историческая офлайн-заметка к разработке v0.7.69.** Кандидат впоследствии прошёл канареечное обновление SOPH, BARD, BTC и опубликован как GitHub Release v0.7.69. Термин OFFLINE ONLY ниже описывает состояние на момент подготовки кандидата, не актуальный LIVE-статус.

# StatsWorker v2 — HOT fill throughput candidate (OFFLINE ONLY)

## Motivation and observed evidence

The LIVE SOPH worker emitted `STATS_V2_NOT_READY: HOT fills not complete; next pin required` repeatedly under intense trading, and later recovered without a reset. A read-only diagnostic found some filled orders with exact fills **after** the sidecar's pinned `fills` rowid, alongside other orders still absent in the local ledger at the moment of sampling. This does not prove the cause of every missing fill, but it justifies avoiding exchange recovery for fills that have already landed locally.

## Scope

**One non-trading module changes:** `okx_stats_v2_production.py` (worker-owned, derived stats only). `run_checks.sh` includes one additional isolated regression module, `tests/test_stats_v2_hot_scale.py`. This candidate is **not** a release and does not patch LIVE processes. No exchange network calls are used in tests.

1. On an incomplete HOT financial oracle result, evaluate **at most 4** missing orders (oracle's bounded sample) using a new **read-only** FillStore WAL snapshot. Only when **all** sampled orders have exactly the expected quantities, skip the existing API recovery and re-pin the source **once** inside the same StatsWorker compute call. Never publish from the mixed old/new snapshots.
2. Bound this local shortcut to **two consecutive observations**. A persistent gap falls back to the original `FillStore.ensure_expected` API governor. No change to exchange endpoints, request budget, reader, trading loop, fee verification or financial math.
3. Add a throttled (at most once per 5 min) `STATS_V2_HOT_GAP` diagnostic with the oracle's bounded missing-order sample, the pinned fill cursor and HOT cycle count. No order identifiers or fabricated PnL appear in logs.
4. Replace N individual pinned HOT PnL ledger queries with bounded 200-ID `SELECT ... IN` batches. Preserve rowid pinning, missing-row fail-closed behavior and exact Decimal reconciliation.

## Required checks before approving LIVE changes

- Scoped tests, original StatsWorker phase + production contracts, and all general checks must PASS in an isolated test environment (never as root on the LIVE VPS).
- Synthetic heavy load should demonstrate bounded memory, ledger equality, deterministic failures on missing/conflicting fills and no exchange access.
- Compare historical baseline source against candidate: `okx_bot.py`, Reader, Telegram and all trading components remain byte-identical. The candidate changes only one background module and the tests/documentation.
- Explicit operator approval required before any LIVE install/restart. A code file copy does **not** update currently running Python modules; no automatic bot restart is attempted.
- Validate a reversible one-bot canary first (SOPH, as affected bot), preserving all financial databases untouched; protect other bots and Reader. Separate rollback must restore only the code module, not historical state.
- Never publish stale or incomplete finance, reset sidecar cursors, truncate finance databases, or disable strict `complete` checks.

## Limitations

This addresses the observed after-cursor race and per-terminal HOT ledger query volume. It does **not** prove that a specific long stall was solely caused by these paths. If sustained missing exchange executions still occur, the original API recovery remains in place and further targeted evidence is needed. The API governor is not weakened, and the candidate does not promise a specific fill throughput before a LIVE canary and measured stress results.
