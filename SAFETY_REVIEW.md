# OKX Grid Bot — v0.7.25 Safety Review (release)

**Baseline:** public v0.7.24 ZIP SHA256 `2cc176d0860e822ea9674bf7870c008cfd11b743475aefc22f0f7e372d6d572b`.
**Scope:** hardening of emergency commands, independent PANIC ALL, Telegram notifications, observer compatibility, manager resilience and controlled upgrade. State schema stays `1`.
**Live status (2026-10-22 rollout):** four LIVE bots `near0721`, `btc0721`, `dood0721-2`, `zec0724` passed the controlled v0.7.25 upgrade procedure, individual GET-only audit and fresh engine-heartbeat verification; all four trading services and Telegram monitor were reported active. This point-in-time verification does not prove destructive emergency branches on the real exchange or continuing availability.

## Emergency ownership and unresolved orders

- During HARD/PANIC a single manually edited or mismatched BUY is **never canceled**. Verified other BUY orders with exact `ordId`, `clOrdId`, instrument, side, state, size and price can be canceled independently (maximum five per batch) using existing durable cancellation intent. After any mismatch the routine raises `ExchangeUnresolved`: **no permission to MARKET SELL, no inference that NET position is safe**. Repeated passes skip `CANCELING` intents; partial fills still need full reconciliation.
- A duplicated/missing order ID in a fresh exchange pending snapshot remains a global error. UNKNOWN POST outcomes remain ambiguous; no duplicate blind POST.
- Partial PANIC ALL reads all active systemd units and their WorkingDirectory, instrument and MainPID. Every target needs valid registry linkage, config/state equality, stable active process, known command mode, credentials file and executable engine. Unknown active instrument or duplicated NET pair is **global refusal**. A separately identifiable bot already in PANIC/HARD, missing credentials, or with malformed registry record is **SKIP**, not a veto for another independently verified pair. Two complete checks before the first durable write; service identity rechecked for each bot. Each write is independent and partial results result in nonzero CLI outcome. SKIP is never represented as flat.
- This remains a **single main OKX account**, one engine per instrument. No new support for multiple engines/manual positions on a shared NET instrument. No modification to Virtual TP, market price bounds, general grid strategy, emergency SELL quotas, or position reconciliation.

## Telegram fault visibility and delivery

- Critical event alarms added for `PANIC_MARKET_REJECTED`, `PANIC_RETRY_EXHAUSTED`, `EMERGENCY_POSITION_UNRESOLVED` and `EMERGENCY_BUY_IDENTITY_CONFLICT`.
- Read-only independently persisted PANIC ACK monitoring reports `PANIC_MANUAL_REVIEW` once per continuous episode (without rereporting on changed timestamps/position), including remaining-position text. No claim of successful PANIC is inferred from a failed attempt.
- Durable Telegram queue distinguishes definite HTTP 400/403 errors from 429/transient failures. Permanently rejected messages are moved into a bounded local dead-letter log and do not block subsequent alerts; temporary/ambiguous failures remain queued in order. A bounded sent-key index preserves keys of still-queued items. A permanently blocked bot/chat requires local investigation: no Telegram channel can report its own incapacity.

## Management, upkeep and deploy

- PANIC cleanup accepts the exact anomalous historical engine identity `0.7.23.1` but continues to require config/state equality, PANIC_DONE, ACK, terminal snapshot, zero contracts, and confirmed systemd WorkingDirectory. Arbitrary four-part versions remain forbidden.
- Versionless observers admit only clean-engine three-part versions within `[0.7.19, ENGINE_VERSION]`, with schema 1 and persisted state/config identity. This checks stated ABI compatibility, not merely a version string; old engines outside the clean compatibility range remain disabled.
- Read-only manager list, menu and account display isolate malformed individual registry records with visible warnings. They never rewrite/erase the record. Safety-sensitive account-reset, create and control flows keep strict validation. The account display must not treat an incomplete bot registry as a verified bot count.
- `okx_live_upgrade.py` is included in the package. It requires explicit ZIP SHA256, verified unpacked stage, one named bot, v0.7.24 engine hash, backup, stop/inactive and state recheck, durable version migration, GET-only audit and fresh heartbeat on new PID. Any audit failure restores files and leaves bot **stopped**; no automatic exchange orders or blind restart. Stopping systemd does **not** cancel exchange orders; virtual TP can be unprotected while process is stopped. Never run updater without separately authorized LIVE maintenance.
- `okxbot.py` and `run_checks.sh` ZIP mode must remain `0755`, checked as part of packaging. `README.md` describes the verified rollout and the release procedures; the separate GitHub publication never changes LIVE services.

## Explicit outstanding risks (NOT fixed in v0.7.25)

1. Unknown manual/foreign orders on the same NET instrument can still prevent bot startup; intentional ownership restriction.
2. Ambiguous PANIC MARKET POST can leave an unprotected position while safe reconciliation waits. Do not blindly retry or market-sell unknown NET exposure.
3. Confirmed rejected PANIC MARKET attempts may leave a position requiring manual intervention; Telegram warning does **not** automatically provide TP protection.
4. HARD → PANIC transition remains unsupported; requires separate safety design.
5. Virtual TP aggregate MARKET exits have no new execution-price floor in this release.
6. At-least-once Telegram delivery may duplicate notifications when network ACK is ambiguous. Permanent Telegram 400/403 responses are dead-lettered; operator should inspect the local monitor state if Telegram itself becomes unavailable.
7. Strict fail-closed gate, durable intents and terminal accounting do not guarantee an exchange venue outage cannot delay emergency protection.

## Verification limits

`run_checks.sh` includes existing contract/state/finance/Telegram regressions plus v0.7.25 fault injections for mismatched BUY, partial PANIC ALL, invalid NET identity, ACK suppression, dead-letter queue, historical cleanup and registry resilience. All are deterministic/mocked; they do NOT prove end-to-end live exchange behavior. Controlled one-bot-at-a-time LIVE preflight and authorized rollout completed on 2026-10-22 with four instances; intentionally destructive emergency fault-injection was only tested offline. Publication must verify immutable release assets and must not restart or alter LIVE instances.
