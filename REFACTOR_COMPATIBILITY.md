# OKX Grid Bot — compatible clean refactor: migration design and verification history

**Source:** uploaded published v0.7.29 at commit `7dfd67cdf1fe0723be5470356893cc26416dfe89`.
**Release:** package and engine 0.7.30. Historical design document; the five per-bot LIVE upgrades and post-upgrade exchange audits were reported successful on 2026-10-25 UTC. Publication itself changes no VPS process.

## Key decision: freeze the wire protocol, not version-based business logic

Old and newly compiled engines must share the **exact same account quota inode**, FIFO queue path and schema. The historical suffixes in these disk filenames represent a *frozen protocol namespace*, not selection by engine release. This is deliberate. There is no quota increase and no automatic rename of these files.

- `/root/okx/.account_positions_gate_v075.json` and its `.fifo-v0712.json` tickets remain schema 1 (8 permits per 2.1 s).
- `/root/okx/.account_fills_history_gate_v0712.json` and its tickets remain schema 1 (9 permits per 2.1 s).
- The account screen and every engine use the same positions quota path. Per-bot configuration overrides must also resolve to the same account-wide path before any LIVE rollout; fail closed on unexpected overrides.

## Telegram safety

No destructive migration or reset: persisted `telegram_monitor_state.json` remains schema 1. The original wire keys retain their exact spelling and semantics. The new module `okx_telegram_wire.py` centrally maps semantic in-code names to these frozen keys. The original heartbeat, PANIC alerts, offsets, deduplication, outbox, dead letters and `debounced-v0722-1` rule identity remain compatible.

The actual Telegram Monitor, alert queues and status handlers were tested offline using a nonempty persisted schema-1 state with a queued PANIC warning.

## Stats / mixed fleet

Engine `performance_snapshot()` writes BOTH `performance` and the old `performance_v0724` for this compatibility transition. Both writers' financial data are identical; no monetary formula is changed. UI, manager, diagnostics and Telegram readers prefer the neutral field, falling back to the old field for bots still running the published engine.

There must be a separately approved future removal of legacy keys; do **not** remove until all consumers have migrated and their retention is understood.

## Engine identity, observers, PANIC

`ENGINE_VERSION=0.7.30` is a genuinely new code identity; immutable release-engine SHA256 entries for both the old published engine and this candidate are recorded in `engine_manifest.json`.

`okx_engine_integrity.py` checks actual on-disk bytes against the exact manifest record, not `version >= (0,7,n)`. Observer management and PANIC cleanup accept an attested old OR new engine with matching config, state schema and instrument. Unknown/tampered engines fail closed. New greenfield provisioning remains limited to an unstarted current engine.

## Release/updater implementation

`okx_live_upgrade.py` implements a **dynamic** staged upgrader instead of an unsupported stub: the old version must be explicitly provided and have a pinned digest in the new release manifest, and the target is resolved from the staged manifest. All actions are guarded by a published ZIP SHA256; a trading-instance stop requires explicit `--confirm-live-stop`. The historical updater is retained for test evidence only.

The implementation preserves the original single-bot backup → stop → fsync metadata → GET-only audit → controlled start → heartbeat verification flow. **This code has NOT been executed against the VPS, real systemd, exchange positions, or a complete five-bot rollout.** Its package staging contract and import/safety guards have been exercised offline. An explicit LIVE deployment review is mandatory; do not run it against active bots simply because the package tests pass.

## What is verified (offline)

- Full repository `run_checks.sh` passes (legacy trading, Telegram, observer, accounting, updater tests + new compatibility tests).
- The two independent OS processes, one from the published FIFO implementation and one from this candidate, together receive **eight**, not sixteen, permits from the same shared positions gate.
- A nonempty old Telegram state loads unchanged and delivers a queued critical message; offsets and active incidents persist.
- Exact old/new engine hashes pass observer and PANIC cleanup attestation; modified engine bytes are rejected.
- Telegram `/latency` includes rotated `latency.jsonl.1`.
- Clean extracted candidate ZIP runs the regression suite, and the stage-package verification accepts its byte-for-byte source.

## Historical LIVE prerequisites (completed in staged operator rollout)

1. One GET-only current VPS audit: bot list, service/observer health, current hashes, queue filenames, **any custom gate-path overrides**, config/state versions, unknown POST intents and current positions/orders; no stop or write in this phase.
2. Review actual shared-module imports and all versioned historical bots (the attested old engine is the published 0.7.28 engine included in v0.7.29; other archived engines are not implicitly authorized).
3. Review the live-upgrade script's stop/start/recovery behavior under staged systemd mocks and actual GET-only OKX verification. Verify backup/rollback on a nontrading isolated instance before REAL money.
4. Explicit human approval for each bot and sequential rollout; no bulk stop, no automatic rollback restart, no changes to trading strategy.
5. Controlled post-upgrade heartbeat + order/position coverage + Telegram audit before considering GitHub Release.

**This section records the original pre-rollout caution; do not replay upgrades on already updated bots.**
