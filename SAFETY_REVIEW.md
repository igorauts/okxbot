# OKX Grid Bot — v0.7.28 Safety Review (release)

**Baseline:** immutable published v0.7.27 final ZIP SHA256 `7c3eeb52385902ea47a621f7835ee2f1a4d2fec281919bfbc1eca791ae632a60`, engine SHA256 `e0d66fc153a2a20d2b9c5df6e79915b73deb1cbd694eae1d153f305284b02717`.
**Status (2026-10-23):** full offline regressions passed. Four LIVE bots (`near0721`, `btc0721`, `dood0721-2`, `zec0724`) separately passed verified preflight, backup, GET-only audit, upgraded engine v0.7.28 and fresh heartbeat. Four trading services, eight execution/burst observers and Telegram monitor confirmed `active`; provided post-upgrade error check was empty. Each bot created a fresh append-only UTC-hour latency segment with minute records. These point-in-time checks do not certify a full 24-hour retention cycle or destructive LIVE stall scenarios. GitHub publication leaves LIVE untouched.
**Scope:** two diagnostic fixes and log-field clarity; `ENGINE_VERSION=0.7.28`, `RELEASE_VERSION=0.7.28`, state schema 1; no change to order choices, FIFO quotas, network GET timeout/retries, position ownership, TP targets, or emergency trading operations.

## Grouped heartbeat invariants

- Each confirmed fault stays in the persisted individual watch even when grouped. A common episode remains active while any previously confirmed member is unresolved. Joining/leaving members are announced, and a member that remains stale after two recover is explicitly named in `PARTIAL RECOVERY` / `COMMON STALL UPDATE`; a completed group recovery requires no unresolved members.
- A missing service record, failed systemd status query, process restart, or inactive unit cannot serve as proof of heartbeat recovery. A fresh active-process heartbeat, independently debounced for the recovery threshold, is needed. Systemd STOP/START alerts remain separate.
- Telegram durable queue may still duplicate under ambiguous network acknowledgment, but incident identifiers are stable for one episode and progress updates have separate keys.

## Append-only latency storage

- All filesystem work occurs in the existing isolated latency writer worker. Each completed minute is written once, by append to an hour segment. No historic-file `fsync` per minute, no 5MB rewrite. An interrupted final JSONL line is truncated before the next append, and malformed records are skipped by read-only diagnostics. Dropped samples can still occur if worker queue is full or service is interrupted.
- The old `latency_24h_rollups.jsonl` remains read-only until fully expired. Consumers use both sources during migration, while the queried time horizon is bounded to 24h; UTC-hour segment retention may use approximately one additional hour physically. No seven-day storage or claims of exact histogram percentiles.
- Disk-write and journal failures are diagnostic errors only; they never become trade-order retries or overrides of safety gates.

## Clear TP diagnostic fields

- Structured events include `buyPx`, `tpPx`, and `cycleId` alongside historical ambiguous `price`. Existing observers/old parsers can continue using `price`; new audits can distinguish BUY level and TP SELL level without assuming the event's price semantics. Financial calculations and the meaning of `price` are unchanged.
- Existing safety AST guard for 25 critical trading symbols stays pinned. New tests cover group membership transitions, durable restart state, torn latency record, bounded retention, and metadata existence. No destructive LIVE PANIC testing is claimed.

## Explicitly postponed

REST GET timeouts/retries, position polling architecture (/positions FIFO vs WS), BUY protection step count, ZEC real TP target experiment, shared NET ownership and unresolved PANIC MARKET protection remain outside the v0.7.28 patch. They require separate safety validation.

---

# OKX Grid Bot — v0.7.27 Safety Review (release)

**Baseline:** immutable public v0.7.26 release ZIP SHA256 `9a6cffb7bf0028632368966cab39c36e474d664e60831006a0008628654aed08`; pinned engine `13751a351640627035298f77bd436e16a8f81a86711af05f2c42f9d33d82b926`.
**Status (2026-10-23):** full offline regression PASS. Four LIVE instances near0721, btc0721, dood0721-2, zec0724 individually upgraded to v0.7.27 via backup + GET-only audit + fresh heartbeat; 4 engine services, 8 observers and Telegram Monitor reported active after controlled rollout. User-supplied post-update journal check showed no listed engine/observer errors. This does not certify continuous health or destructive emergency paths. GitHub publication does not touch LIVE.
**Scope:** diagnostics, state-persistence correctness, read-only accounting drift analysis, reliable heartbeat alerts, local 24h latency aggregation and reduced noise. No new order strategy, exchange API timeouts, FIFO quota, position-source, BUY buffer policy, Virtual TP targets, emergency operations or schema changes.

## New safety checkpoints

- `place()` still makes the durable PLACING write before every POST. No duplicate network POST on unknown outcome. The successful placement batch now fsyncs confirmed BUY_LIVE state. A disk failure still propagates and fails closed. This does not assert that PLACING is an OKX order.
- Observer compares persistent BUY counts only when its state timestamp is at least as recent as the corresponding live journald sample; PLACING is separately classified as unresolved and cannot verify a confirmed exchange BUY. No automatic destructive response to discrepancy; Telegram mismatch spam remains suppressed.
- Long-pass observer event is emitted only AFTER a completed pass; external heartbeat monitor may detect a blocked pass earlier (45s stale + 15s confirmation). Common alert grouping is based on correlated stale local engine heartbeats, not proof of OKX outage. No automatic restart or unreviewed service action.
- 24h telemetry writer uses background-only I/O, bounded queue, local JSON, approximate histograms with explicit uncertainty, and retains raw records for outlier investigation. Any write errors are telemetry-only and must not block trading.
- PnL checker connects to SQLite in `mode=ro`, PRAGMA query_only, performs no network or persistence and does not claim an asynchronous discrepancy proves a missing fill. Original case at ZEC $0.010084 still requires an independently corroborated investigation; balances are never automatically adjusted.
- Both burst/execution observer services remain independently supervised; only redundant detailed journal outputs removed. Daily counters are local diagnostic counters, not exchange fill ledgers.
- `okx_live_upgrade.py` hard-pins exact public v0.7.26 engine, requires operator-approved STOP, per-bot backups, GET-only audit and fresh heartbeat. LIVE deployment was separately authorized and verified on 2026-10-23; future deployments still require explicit operator consent before any technical STOP.

## Historical v0.7.27 deferrals (remain unresolved)

Changes to HTTP GET timeout/retry budget, position synchronization through private WebSocket / shared reader, BUY protection step count, and ZEC real TP target experiment remain explicitly out of scope. Previous v0.7.26 emergency risks (unknown NET ownership, uncertain/rejected PANIC MARKET, HARD→PANIC, Virtual TP MARKET floor) remain open; do not infer these are fixed.

---

# OKX Grid Bot — v0.7.26 Safety Review (release)

**Baseline:** public v0.7.25 ZIP SHA256 `4456dc9eada6b50bef61ebe69b93ad6bec726b3fcf547a458b45ea0370b6dc9f`.
**Scope:** suppressed non-actionable State/Engine BUY mismatch notifications in Telegram while preserving local observer evidence and actionable alerts; compact manager statistics with consistent real+virtual TP count and contract-precision quantities. Trading strategy and emergency decisions unchanged. State schema stays `1`; previous v0.7.25 safety controls retained.
**Live status (2026-10-23 rollout):** four LIVE bots `near0721`, `btc0721`, `dood0721-2`, `zec0724` passed the controlled v0.7.26 upgrade procedure, individual GET-only audit and fresh engine-heartbeat verification; all four trading services and Telegram monitor were reported active. This point-in-time verification does not prove destructive emergency branches on the real exchange or continuing availability.

## Emergency ownership and unresolved orders

- During HARD/PANIC a single manually edited or mismatched BUY is **never canceled**. Verified other BUY orders with exact `ordId`, `clOrdId`, instrument, side, state, size and price can be canceled independently (maximum five per batch) using existing durable cancellation intent. After any mismatch the routine raises `ExchangeUnresolved`: **no permission to MARKET SELL, no inference that NET position is safe**. Repeated passes skip `CANCELING` intents; partial fills still need full reconciliation.
- A duplicated/missing order ID in a fresh exchange pending snapshot remains a global error. UNKNOWN POST outcomes remain ambiguous; no duplicate blind POST.
- Partial PANIC ALL reads all active systemd units and their WorkingDirectory, instrument and MainPID. Every target needs valid registry linkage, config/state equality, stable active process, known command mode, credentials file and executable engine. Unknown active instrument or duplicated NET pair is **global refusal**. A separately identifiable bot already in PANIC/HARD, missing credentials, or with malformed registry record is **SKIP**, not a veto for another independently verified pair. Two complete checks before the first durable write; service identity rechecked for each bot. Each write is independent and partial results result in nonzero CLI outcome. SKIP is never represented as flat.
- This remains a **single main OKX account**, one engine per instrument. No new support for multiple engines/manual positions on a shared NET instrument. No modification to Virtual TP, market price bounds, general grid strategy, emergency SELL quotas, or position reconciliation.

## Telegram fault visibility and delivery

- v0.7.26: `PERSISTED_STATE_DIFFERS_FROM_ENGINE` and its diagnostic recovery are retained locally but excluded from Telegram delivery; stale messages of that type are removed from the pending Telegram queue only. Critical PANIC, BUY deficit, TP coverage, engine health and other actionable alerts remain enabled. No observer or trading-engine safety decision is changed.

- Critical event alarms added for `PANIC_MARKET_REJECTED`, `PANIC_RETRY_EXHAUSTED`, `EMERGENCY_POSITION_UNRESOLVED` and `EMERGENCY_BUY_IDENTITY_CONFLICT`.
- Read-only independently persisted PANIC ACK monitoring reports `PANIC_MANUAL_REVIEW` once per continuous episode (without rereporting on changed timestamps/position), including remaining-position text. No claim of successful PANIC is inferred from a failed attempt.
- Durable Telegram queue distinguishes definite HTTP 400/403 errors from 429/transient failures. Permanently rejected messages are moved into a bounded local dead-letter log and do not block subsequent alerts; temporary/ambiguous failures remain queued in order. A bounded sent-key index preserves keys of still-queued items. A permanently blocked bot/chat requires local investigation: no Telegram channel can report its own incapacity.

## Management, upkeep and deploy

- v0.7.26 terminal statistics show the bot RUN uptime and start time, signed BID percentage versus highest filled/open BUY, combined active-real plus virtual TP counts from one snapshot, and quantities rounded to contract specifications with USD notional displayed to cents. The legacy GET-only audit output remains unchanged; reporting never alters order placement.

- PANIC cleanup accepts the exact anomalous historical engine identity `0.7.23.1` but continues to require config/state equality, PANIC_DONE, ACK, terminal snapshot, zero contracts, and confirmed systemd WorkingDirectory. Arbitrary four-part versions remain forbidden.
- Versionless observers admit only clean-engine three-part versions within `[0.7.19, ENGINE_VERSION]`, with schema 1 and persisted state/config identity. This checks stated ABI compatibility, not merely a version string; old engines outside the clean compatibility range remain disabled.
- Read-only manager list, menu and account display isolate malformed individual registry records with visible warnings. They never rewrite/erase the record. Safety-sensitive account-reset, create and control flows keep strict validation. The account display must not treat an incomplete bot registry as a verified bot count.
- `okx_live_upgrade.py` is included in the package. It requires explicit ZIP SHA256, verified unpacked stage, one named bot, v0.7.25 engine hash, backup, stop/inactive and state recheck, durable version migration, GET-only audit and fresh heartbeat on new PID. Any audit failure restores files and leaves bot **stopped**; no automatic exchange orders or blind restart. Stopping systemd does **not** cancel exchange orders; virtual TP can be unprotected while process is stopped. Never run updater without separately authorized LIVE maintenance.
- `okxbot.py` and `run_checks.sh` ZIP mode must remain `0755`, checked as part of packaging. `README.md` describes the verified rollout and the release procedures; the separate GitHub publication never changes LIVE services.

## Explicit outstanding risks (NOT fixed in v0.7.26)

1. Unknown manual/foreign orders on the same NET instrument can still prevent bot startup; intentional ownership restriction.
2. Ambiguous PANIC MARKET POST can leave an unprotected position while safe reconciliation waits. Do not blindly retry or market-sell unknown NET exposure.
3. Confirmed rejected PANIC MARKET attempts may leave a position requiring manual intervention; Telegram warning does **not** automatically provide TP protection.
4. HARD → PANIC transition remains unsupported; requires separate safety design.
5. Virtual TP aggregate MARKET exits have no new execution-price floor in this release.
6. At-least-once Telegram delivery may duplicate notifications when network ACK is ambiguous. Permanent Telegram 400/403 responses are dead-lettered; operator should inspect the local monitor state if Telegram itself becomes unavailable.
7. Strict fail-closed gate, durable intents and terminal accounting do not guarantee an exchange venue outage cannot delay emergency protection.

## Verification limits

`run_checks.sh` includes existing contract/state/finance/Telegram regressions, v0.7.25 emergency fault injections and v0.7.26 mismatch-suppression and terminal-statistics regression. The Telegram delivery suppression is scoped only to diagnostic State/Engine BUY mismatch and associated recovery; other alarms remain unchanged. All are deterministic/mocked; they do NOT prove end-to-end live exchange behavior. Controlled one-bot-at-a-time LIVE preflight and authorized rollout completed on 2026-10-23 with four instances; intentionally destructive emergency fault-injection was only tested offline. Publication must verify immutable release assets and must not restart or alter LIVE instances.


## v0.7.29 — Maintenance / UI only (2026-10-24)
- ENGINE_VERSION stays 0.7.28; trading engine, FIFO, funding, strategy and state schema unchanged.
- Integrate already-LIVE verified observer provisioning for pristine unstarted bots.
- /status: fail-closed account total PnL from complete, fresh per-bot accounting; two decimals and round sign icons.
- Market SELL: compact header (base volume, number of closed grids, actual PnL indicator), execution deviation indicator and timing.
- Manager status: original bot uptime and independent process uptime.
- Current-version tests validate against metadata and manifest without literal release/engine pins.
- Existing legacy okx_live_upgrade.py is for 0.7.27 → 0.7.28 only. DO NOT use for v0.7.29.
- Package build/push does not update any running bot. Never copy okx_version.py into per-bot engine dependencies.
