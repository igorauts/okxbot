# v0.7.63 — StatsWorker v2 reliability: OFFLINE CANDIDATE ONLY

This is NOT an approved LIVE release and NOT a GitHub Release.

Source parent: verified v0.7.62 candidate (pinned SHA d36746e638b98b85eb164f7774dbac6a7034555697e325cf6bb15cfb3c721fa9), plus the exact published release-notes/README/CHANGELOG prefixes reconstructed from the successful pinned v0.7.62 publisher. The actual published parent ZIP bytes are not available locally, so no byte-for-byte comparison with its ZIP is claimed.

Scope implemented in this offline candidate:
- Creation-time `grid_reference.json` via unchanged `_first_anchor()` CFG branch; unknown legacy anchor remains NotReady.
- State replaced/changed while ingestion is a transient NotReady, not a financial Drift.
- Durable circular scan of pending freeze components (64 attempts per batch), reason histogram, stall counter, one state pin per batch with final source stability validation and single money/index transaction.
- Original idempotent HOT PnL ledger ingestion only when a missing row is detected, always re-pin after attempted write; no guessed fees/fills.
- Worker incidents in bot_events and Telegram event bridge; independent local-only stale-finance alert after 30 minutes, a recovery notification and explicit stale line in compact status.
- Proven LATE_FILL_FROZEN_ORDER only: quarantine derived SQLite (no source ledger changes), bounded incremental rebuild from authoritative ledgers, cooldown >=6 hours; no full-history oracle or FULL on LIVE.
- Updated obsolete low-RAM contract, new offline safety tests, clean current-engine dependency list. Historical manifests remain immutable.

NOT YET COMPLETE / NEVER DEPLOY:
- ORIGINAL StatsWorker v2 164 phase/contract/money/checkpoint and 32 production/integration tests are imported as byte-identical unmodified source fixtures; the pinned v0.7.60 ORIGINAL FULL oracle is test-only. Both suites and the complete `run_checks.sh` returned PASS offline.
- NOT YET PROVEN: prolonged hostile-fill stress, strict memory/CPU/time bounds for rebuild on BLUR-scale data, crash/restart during quarantine, and real Telegram incident delivery. The standard offline regression suite is PASS, but is insufficient for approval of automatic rebuild.
- Independent source review of automatic sidecar quarantine/rebuild is mandatory. No such LIVE change should be installed based solely on this draft.
- Only OFFLINE ORIGINAL FULL comparisons remain available as regression oracles; NO periodic/manual FULL operations are scheduled on LIVE.

## LIVE readiness stress gate (additional offline safety pass)

- Added crash-idempotent, SHA-pinned quarantine PREPARED -> QUARANTINED -> COMPLETED protocol. Interruption at any point resumes from the durable marker before the worker reopens derived SQLite; uncertain file identity refuses automatically. A rebuild is complete ONLY after no pending cycles and a verified financial publication; `finish_rebuild` is not called on a partial HOT-inclusive report.
- Resource guards on automatic rebuild: Linux MemAvailable >=384 MiB, free disk >= max(128 MiB, twice existing derived sidecar), 600-second maximum wall elapsed before requesting manual review. Rebuild batches cap at 64 components, paced at 0.5s ONLY during recovery. No FULL across the lifetime; normal StatsWorker cadence is untouched.
- Synthetic benchmark 20,000 CLOSED cycles, within a 1-GiB virtual address limit: 313 batches, 15.34s COMPUTE ONLY (no 0.5s LIVE pacing), process peak RSS 123.5 MiB, rebuilt sidecar 13.8 MiB. Authoritative SQLite/state sources byte-identical after rebuild. NOT a timing/RSS claim on 1.6-GiB Vultr LIVE or BLUR's real heterogeneous history.
- Hostile frozen-order late fill with incompatible quantity: drift detected; after quarantine and replay, every compute remained NotReady. No invented financial publication and original authoritative sources unchanged. Some contradictory inputs are irreparable automatically: remain fail-closed and alert operator.
- Offline Telegram: persisted incident outbox survives injected send timeout and monitor restart; retries deliver once, and recovery event follows. REAL Telegram delivery must be verified during canary.
- Tests: new crash windows, restored/original suites, queue progress, HOT ledger, Telegram delivery and hostile fill tests. Full `run_checks.sh` must return 0 on final exact ZIP before any LIVE canary.
- This is STILL a non-LIVE candidate. Live preflight must validate pinned parent LIVE source, shared reader/monitor, UID, available memory, disk headroom and one canary bot. BTC updated last; no auto multi-bot rollout. No per-bot tests or blocking network while GitHub release publisher runs.
