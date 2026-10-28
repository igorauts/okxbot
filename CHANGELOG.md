## 0.7.36 — Account Reader diagnostics and new-bot registration (LIVE-verified 2026-10-28 UTC)

- Package 0.7.36 with unchanged trading engine 0.7.35; no four-component patch tags.
- Merge verified LIVE new/clone Reader auto-registration and pre-start fail-closed handshake.
- One read-only, account-wide Reader Diagnostics menu and CLI report.
- Telegram: durable one-incident bypass/recovered alerts for confirmed configuration/registration faults only; no false alarm for transient flat-position fallback.
- Reader: GET-free health lists currently registered instruments; no increase to account quota.
- Restore executable modes in Git ZIP; clarify Reader GET/quota error episodes.
- Priority FIFO, Funding and TP wake cooldown are unchanged.

## v0.7.35 — LIVE-verified reliability update (2026-10-27)

- Controlled LIVE rollouts: `near0721`, `wld0728`, `btc0721` upgraded separately from 0.7.34 to engine 0.7.35, with backups, GET-only financial audits and fresh heartbeats; ZEC/DOOD terminal instances not restarted.
- P0: suppress repeated same-TP WS BID wakeups to at most one per second without delaying the first crossing; no change to durable intent, MARKET POST, or 150 ms coalescing.
- Shared Position Reader reads existing `/root/okx/.okxbot_account.env`; fail-closed registry/archive rules, IPC health and bounded watchdog timer. `mixed-fifo`, no Priority FIFO migration.
- Telegram Reader alerts: durable anti-spam and FLAPPING status; actual running `/root/okx/okx_telegram_health.py` updated, monitor restarted alone.
- GET-free offline diagnostics for historical MARKET fill stages, systemd recovery templates, historical RC docs archived. Funding and trading strategy unchanged.
- Offline regression 179 tests PASS; asset SHA256 and engine manifest verified. Post-deploy: three engines/Reader/watchdog/Telegram ACTIVE, Reader IPC and watchdog checks PASS; final account GET-only snapshot at 2026-10-27 22:50:02 UTC. No LIVE fault-injection to provoke reader failure or test actual Telegram alarm delivery.
- Full checks and limits: [V0735_LIVE_RELEASE_NOTES_RU.md](V0735_LIVE_RELEASE_NOTES_RU.md).

# 0.7.35 — OFFLINE Reliability candidate (НЕ LIVE, НЕ GitHub)
- Исправлен повторный WS wake, Reader watchdog / terminal registry / Telegram FLAPPING.
- Новый Reader unit читает существующие account credentials, без новых ключей.
- READ-ONLY WS→intent→exchange fill analysis, systemd recovery templates, архив RC-документов.
- Trading POST, PANIC, стратегия, комиссия, PnL, FIFO/priority сохранены.
- Детали: [V0735_OFFLINE_RELEASE_NOTES_RU.md](V0735_OFFLINE_RELEASE_NOTES_RU.md).

## v0.7.34 — Shared Position Reader + faster virtual TP MARKET (LIVE verified, 2026-10-27)

- LIVE: four bots verified on 0.7.34, common GET-only Reader RC12, Telegram Reader alerts RC13 and durable service recovery; priority FIFO remains OFF (`mixed-fifo`).
- Financial pre/post GET-only audits 4/4 PASS; four ZEC MARKET SELL completed after rollout, Telegram delivery confirmed. First-cross TP→intent N=4, P50 558.4 ms (not fill latency).
- Offline regressions and release archive verification, no LIVE changes during GitHub publication. See `V0734_LIVE_RELEASE_NOTES_RU.md`.

# v0.7.33 — LIVE-verified StatsWorker optimization and Telegram PANIC hotfix (2026-10-26)

- Full-source final release incorporating 0.7.31 TP observability and MARKET SELL presentation, 0.7.32 consistent financial snapshots and attached TP failure handling, 0.7.33 CPU optimization, plus an independently deployed Telegram-only terminal startup health hotfix.
- Preserve exact live engine `okx_bot.py` SHA256 `62202f16e683ef9a1ec91774f033af22d33b3fff2c6e3d4b86b7e86ded1efd97`. Financial calculations and Decimal PnL remain equivalent; full LIVE snapshot duration reduced from approximately two minutes under the preceding engine to 0.78–9.46 seconds over ten observed snapshots. Calculations remained fresh after upgrade with 7,700+ cycles and 12,500+ fills. This is sampled real-world evidence, not a latency SLO.
- For confirmed attached TP failure `51006`, keep original TP price; virtual TP stays virtual until a free real-TP slot and acceptable price allow safe independent reduceOnly LIMIT placement. Account-wide SOFT latch for confirmed `51008`; unknown failure codes block new BUY and require investigation. Never infer a rejected attached TP from `51603` alone.
- Unchanged grid strategy, FIFO, 150 ms MARKET grouping and account NET ownership safety.
- Telegram `okx_telegram_health.py` hotfix: no false `STARTED ... RUN not confirmed` alarm after legitimate PANIC/HARD restart or terminal ACK. Trading engine unaffected.
- Four live engines `zec0724`, `btc0721`, `near0721`, `wld0728` confirmed ACTIVE on `0.7.33` 2026-10-26. DOOD closed with PANIC SELL and archived, zero position and final exact trading PnL. Offline regressions including terminal startup health, financial snapshots, TP recovery and source-archive integrity passed. No destructive failure injection performed.
- Publishing a GitHub release does not modify LIVE systemd services, trading state, positions or orders.

## v0.7.32 — Financial snapshots and attached TP recovery (integrated)

- Freeze state/cycle/quantity boundary before REST fill retrieval; verify a complete consistent ledger and retry if incomplete.
- Handle attached TP failures without changing price or exceeding the configured real SELL TP limit. Hold BUY when uncertainty persists; do not duplicate a SELL.

## v0.7.31 — TP Observability and MARKET SELL message layout (integrated)


- Record first locally observed public-WS BID crossing for each virtual TP before subsequent REST reconciliation; the crossing remains non-authoritative for trading. Missing WS timestamps are not replaced by invented crossing times.
- Diagnostics: BID crossing → collection, collection → durable MARKET intent, intent → REST handoff, additional cycles collected during the 150ms window and after the window. Original FIFO timing remains available independently; after-window time is not falsely labelled as exclusively FIFO.
- Telegram: `🟢/🔴 Market SELL qty COIN (cycles) @confirmed VWAP`, verified net `к профиту (actual/positive_plan %)`, execution deviation `(+/- % relative to positive plan)`, original Fill/ACK/Full and an optional `⏱ BID TP→Intent` observation.
- Financial amounts are unchanged. Confirmed SELL VWAP is weighted from accounted exchange fills. The added measurements are diagnostic only: MARKET strategy, REST submission, intent durability, FIFO/quota, current 150ms coalescing, state schema and PANIC/HARD are unchanged.
- Unpublished candidate: tests offline, LIVE rollout requires separate approval, individual backup/GET-only audit and verified release packaging. No trading services changed by preparing this candidate.

## v0.7.30 — Compatible Clean Refactor (LIVE-verified, 2026-10-25)

- Genuine trading-engine source identity bump with immutable SHA256 entry in `engine_manifest.json`; preserved trading decisions and account-level FIFO quotas.
- Removed version-labelled runtime function/data dependencies while freezing wire-format filenames and Telegram durable-state schema to avoid double quota or lost alerts.
- Dual financial snapshot keys for mixed-fleet compatibility, archive discovery by actual files, `/latency` rotated JSONL support, verified old/new engine attestation for observers and PANIC cleanup.
- Dynamic one-bot updater with backup, guard against unresolved intents, per-bot GET-only exchange audit and an explicit **complete financial report marker** requirement even when an engine exits zero after a caught exception.
- Regression checks for missing/truncated financial audit, ZIP Unix regular-file type, source identity and mixed FIFO.
- Five independent LIVE upgrades on 2026-10-25 (UTC): near0721, btc0721, zec0724, dood0721-2, wld0728. GET-only ownership/TP checks and required services passed in reported post-upgrade audit. GitHub publication does not stop/start any service.
- No funding collector or trading strategy redesign in this release.

## v0.7.29 — Maintenance / UI only (2026-10-24)
- ENGINE_VERSION stays 0.7.28; trading engine, FIFO, funding, strategy and state schema unchanged.
- Integrate already-LIVE verified observer provisioning for pristine unstarted bots.
- /status: fail-closed account total PnL from complete, fresh per-bot accounting; two decimals and round sign icons.
- Market SELL: compact header (base volume, number of closed grids, actual PnL indicator), execution deviation indicator and timing.
- Manager status: original bot uptime and independent process uptime.
- Current-version tests validate against metadata and manifest without literal release/engine pins.
- Existing legacy okx_live_upgrade.py is for 0.7.27 → 0.7.28 only. DO NOT use for v0.7.29.
- Package build/push does not update any running bot. Never copy okx_version.py into per-bot engine dependencies.

## v0.7.28 — Monitoring Reliability Fix (2026-10-23 — GitHub Release)

- P0 Telegram: common engine heartbeat incident now retains independently confirmed failed members. A join, partial recovery or registry disappearance cannot silently clear an unresolved bot. The monitor sends composition updates (joined/recovered/still stalled) and issues `ALL RECOVERED` ONLY after every member's fresh heartbeat has independently met the recovery threshold; systemd STOP, invocation change and unavailable service metadata do not count as recovery. Persisted Telegram incident state and durable outbox identities survive monitor restart; standalone alarms still work.
- P1 latency: replace full-file per-minute JSONL rewrite + `fsync` with one append-only minute record into a UTC-hourly segment. Keep 24h query horizon (up to one extra boundary hour physically), prune expired whole segments hourly; validate and truncate an interrupted final line before first new append. The old v0.7.27 rollup file remains a read-only migration source until it expires, so existing 24h UI/FIFO diagnostics are continuous. Histograms remain approximate for quantiles; raw outlier retention and dropped/write-error counters unchanged.
- TP observability: structured `TP_VIRTUAL`, `TP_BUFFER_CLEANUP`, `TP_FILLED`, `TP_LINKED`, `ATTACHED_TP_DISCOVERED` and associated events now include explicit `buyPx`, `tpPx`, `cycleId` while retaining historical `price` keys for old parsers. TP transition journal gains `tpPx` alongside existing cycle identity and BUY price. No change to placement/cancellation decisions or accounting.
- Test coverage: simulate three-bot join/partial recovery, missing service, STOP/START, Telegram state serialization/restart; isolated alerts; append-only prefix integrity, torn JSONL line recovery, legacy handover and 24h retention. Existing offline trade safety AST checks PASS. Controlled 2026-10-23 LIVE rollout of all four bots completed sequentially with individual backup, GET-only audit and fresh heartbeat; eight observers and Telegram Monitor active. Post-rollout grep found no listed errors. Real stall scenarios were not injected into LIVE.
- `ENGINE_VERSION=RELEASE_VERSION=0.7.28` (three components), state schema 1. Fail-closed updater source engine SHA256 pinned to public v0.7.27 `e0d66fc153a2a20d2b9c5df6e79915b73deb1cbd694eae1d153f305284b02717`.
- LIVE latency proof: one fresh UTC-hourly segment for each bot with minute rows; prior `latency_24h_rollups.jsonl` files retained unchanged. Short observational window does not prove a full 24-hour rotation on LIVE. GitHub publication does not stop/restart trading.
- Still deferred: HTTP GET timeouts, shared /positions optimization / private WS sourcing, BUY protection, increased ZEC real TP target; emergency ownership/PANIC issues remain unchanged.

## v0.7.27 — Reliability & Observability (2026-10-23)

- P0: detect completed trading passes above 30s; Telegram heartbeat alert now confirms after 45s stale + 15s persistence (three polls). Correlate concurrent stalled engines into one group incident with one recovery; no change in GET retries, 12s timeout, REST/FIFO quotas, POST safeguards, position reconciliation, grid or Virtual TP.
- P0: preserve durable pre-POST BUY `PLACING` intent, additionally persist positively acknowledged `BUY_LIVE` facts after successful BUY placement batch. Observer keeps `PLACING` separate from exchange-ACKed BUY, does not compare older persisted-state generations against later journald samples, and logs unmatched evidence locally without restoring non-actionable Telegram spam.
- P1: read-only SQLite PnL ledger vs latest independently audited stats in Execution Diagnostics, with explicit warning that async snapshots may differ. No PnL balance changes or historical mutation; original $0.010084 ZEC anomaly requires separate live read-only follow-up.
- P1: per-route REST HTTP latency status/attempt metadata; 24h rolled-up FIFO/HTTP/loop metrics, visible in Latency Diagnostics. BUY fill to TP link local event-order lag by exact BUY `ordId`, never by price or claim of independent exchange execution timing.
- P1: group multiple engine-heartbeat failures without flooding Telegram; emit `TRADING_LOOP_LONG_PASS` for slow completed passes. `BUY_CANCEL_PROTECTED` log only on a change in the exact protected order set; actual BUY cancellation decisions and all confirmation events unchanged.
- P2: 24-hour ONLY minute-by-minute latency histogram aggregates; raw sub-1ms lock and quota samples omitted after inclusion in histograms; outlier raw events remain. P50/P95/P99 are upper bucket approximations, MAX exact from captured observations. Bounded raw rotation remains; no 7-day storage.
- P2: restart-safe UTC-day observer event counters, narrower adaptive burst detection, eliminate duplicate detailed snapshots/timeline between burst and execution services while keeping both independent observers.
- P2: absence of PANIC control file is expected housekeeping rather than FileNotFoundError; revised UTC diagnostic collector calculates each file's age when read, preventing negative ages from slow multi-stage collection.
- Three-component ENGINE_VERSION/RELEASE_VERSION `0.7.27`, state schema `1`. Controlled v0.7.26→v0.7.27 updater pinned to published v0.7.26 engine SHA256 `13751a351640627035298f77bd436e16a8f81a86711af05f2c42f9d33d82b926`.
- Scope intentionally excludes changing REST timeout/retry policy, shared `/positions` quota, exchange position sourcing, BUY protection distance, number of real ZEC TP, any strategy/fill accounting changes, and all emergency trading behavior. All offline regressions PASS; on 2026-10-23 four LIVE instances (near0721, btc0721, dood0721-2, zec0724) passed sequential technical STOP, per-instance backup, GET-only audit, restart and fresh-heartbeat validation on engine v0.7.27. Eight observers and Telegram Monitor were active after rollout; no relevant exceptions appeared in the supplied post-deploy journal check. This is a point-in-time check, not destructive PANIC testing; GitHub publication does not modify LIVE.

## v0.7.26 — Telegram non-actionable mismatch suppression & terminal statistics (2026-10-23)

- Drop only Telegram delivery of `PERSISTED_STATE_DIFFERS_FROM_ENGINE` diagnostic mismatch and its `BACK TO NORMAL` counterpart. Local observer alert history, paired samples and summary remain untouched, as do BUY 0/N, TP coverage, PANIC, health and other actionable alarms. Clear already-queued obsolete mismatch notifications safely before the next Telegram outbox drain; retain all unrelated queued messages.
- Manager statistics: status merged into uptime line, start time below, BID relative to top filled/open BUY uses signed percentage only; awaiting TP counts real ACTIVE records plus nonempty virtual TP cycles from one engine snapshot. Older snapshot without the new count displays `н/д` rather than silently presenting real-only counts.
- Base quantities displayed without padding according to `contract_value × contract_lot`; nominal and historical peak marked to USD cents and shown in parentheses. GRID and total highlighted; LIMIT/MARKET/Partial breakdowns uncolored. No PnL formulas, trade decisions, Virtual TP execution, FIFO or safety paths modified.
- v0.7.26 engine identity (three components), state schema 1; pinned v0.7.25→v0.7.26 fail-closed updater. Offline regression PASS. On 2026-10-23, four controlled LIVE upgrades with backups, individual GET-only audit and fresh heartbeat succeeded for `near0721`, `btc0721`, `dood0721-2`, `zec0724`; four trading services and Telegram monitor were active after rollout. Publication is separate and does not touch LIVE.

## v0.7.25 — Emergency Reliability audit hardening (2026-10-22)

- P0: Independently verified owned BUY cancels proceed despite an unrelated manually edited/mismatched BUY; the conflict remains unresolved and prevents an unsafe NET MARKET SELL. Existing durable cancel, batch cap, unknown-POST and position-gate protections retained.
- P0: Partial PANIC ALL for independent verified NET instruments: unknown or duplicate active NET identity still global fail-closed, other identifiable but unready instances explicitly SKIP, each ready instance gets its own durable intent. Strict confirmation and PID snapshots, partial outcomes return error.
- P0: PANIC reject/exhausted/unresolved events and persisted PANIC_MANUAL_REVIEW ACK alarms, without recurring ACK spam; Telegram queue bounded, definite permanent HTTP failures dead-lettered without blocking new alerts.
- P1: Exact `0.7.23.1` PANIC cleanup anomaly accepted with stringent terminal-state proof. Observer compatibility uses schema+version range rather than a manual whitelist.
- P1: Included one-at-a-time fail-closed in-repository LIVE updater; malformed registry records isolated on read-only list/menu/account pages, safety-sensitive actions retain strong checks; scripts preserve executable modes, updated SAFETY_REVIEW.
- Offline deterministic tests expanded for failure modes, negative cases and independent return values. On 2026-10-22, the controlled rollout passed GET-only audits and fresh-engine-heartbeat checks for `near0721`, `btc0721`, `dood0721-2` and `zec0724`; all four trading services and Telegram monitor were reported active at the completion of rollout. Live destructive PANIC fault-injection was not attempted. GitHub publication does not alter LIVE processes.

## v0.7.24 — Statistics & Performance (2026-10-22)

- P0: package `RELEASE_VERSION` separated from three-component `ENGINE_VERSION`, including manager, wizard, observer and state generation. Maintenance simulation `0.7.24.1` creates engine `0.7.24`. State schema stays 1. Existing LIVE config/state not migrated.
- Financial statistics: separate audited LIMIT TP and aggregate MARKET TP returns, partial MARKET exits, current open-position Floating PnL marked to the validated snapshot BID, exact nominal, top executed BUY still open, runtime and peak position. Keep legacy report format for older engines and unchanged closed-cycle accounting; unclosed partial-exit realized components remain in total PnL without incorrectly calling them Floating.
- Terminal statistics highlight profitable/losing PnL. Last 24h, average tempo, volume, commissions, capital projection and drawdown retained. Financial reports remain 10-minute fill-audited snapshots, and stale reports never claim current RUN.
- Local latency 60-minute window, 32 MiB maximum tail, P50/P95/P99/MAX, N, sidecar drops/write errors. No time-window selector.
- Passive FIFO diagnostics include flock waiting time, recommended quota backoff, full FIFO wait, categorized `GateUnavailable` and storage recoveries; diagnostic callbacks are error-isolated and never alter quotas, lock ownership, retry or trading decisions.
- Execution Diagnostics displays trustworthy local BUY/TP event counts, aggregate events and confirmed SYNC recoveries; unavailable fill proportions and uncorrelated latency/durations explicitly reported as unknown, not fabricated.
- Offline regression completed. On 22 October 2026, controlled LIVE upgrade verified v0.7.24 on `near0721` (PID 3719473), `btc0721` (PID 3719547) and `dood0721-2` (PID 3719581); all four trading/Telegram systemd services reported active. This is a historical rollout verification, not a continuous health assertion. Publishing this GitHub release does not alter LIVE services, exchange orders or state.

## v0.7.23.1 — Telegram START/RUN maintenance fix

- Fixed false `STARTED in systemd, but RUN not confirmed after 60s` alarms after normal trading-bot restarts: an absent `state/control.json` now means `RUN`, matching the trading engine, and `state/control_ack.json` is read independently.
- A valid, fresh `RUNNING` acknowledgment and heartbeat are still required for positive START confirmation; corrupt or unreadable existing `control.json` remains fail-closed.
- Telegram-only functional change. GRID, Virtual TP, FIFO, PANIC execution, state schema and all trading-engine modules are unchanged. This patch can be deployed by updating only `okx_telegram_health.py` and restarting only `okx-telegram-monitor.service`.

## v0.7.23 — account management, panic-all, wizard, latency and diagnostic I/O

- Final monitoring fixes (2026-10-22): a brief ACCOUNT_POSITIONS_GATE_DEGRADED waits 15s for confirmed RECONCILIATION_RECOVERED before Telegram queues an alert; queued undelivered alarms are canceled if the gate recovers first. A sustained gate failure still alerts, and then confirmed recovery is reported. Durable pending incidents survive Telegram restarts; other CRITICAL events retain immediate delivery. Terminal Latency Diagnostics reads up to 32 MiB from the log tail (6-minute stats window unchanged).

- Final UI corrections (2026-10-22): account positions show base-asset quantity and OKX USD notional without redundant ticker column, 20x leverage, Floating PnL, margin per position; total In use and *exchange-reported* USDT ordFrozen for open-order margin, with explicit residual when reconciliation differs. No unsupported adjusted equity/risk ratio, or GET-only title. UTC timestamp includes seconds elapsed. Dedicated Refresh / Back actions; periodic timestamp redraw makes no API calls.
- Bot-creation numeric screens render all live previews immediately (zero or suggested values before typing), tick-aligned TP examples with stable columns, correctly signed +$ / −$ profit, smaller BUY labels and risk projection. Final summary shows GRID/TP percentages, BUY notional and per-trade maker net while hiding fixed buffers and internal `live_trading` UI flag. Edit keeps current values; Enter preserves, first typing replaces. Internal validation and fail-closed LIVE authorization remain untouched.
- Shared patch deployment preserves `okxbot.py` executable permissions to avoid Permission denied.

- Manager: compact Futures/USDT Cross overview matching OKX: Equity, Floating PnL, total In use (including order margin), available funds and coin-sized positions with leverage as 20x and exchange-reported per-position IMR. Omits unsupported adjusted equity, margin-ratio percentage and redundant LIVE/instrument text. Fields come from details[USDT] when account-level Futures fields are intentionally empty; never infer a margin amount from an unrelated quantity.
- Account-wide PANIC ALL: single exact phrase, two preflight snapshots of ALL running services including PID and instrument; independent durable PANIC commands; partial submission is reported and never treated as all flat. No exchange POST from manager.
- Account-wide SOFT latch recovery: engine re-acknowledges SOFT_STOPPED after a NEW shared margin latch, while preserving manager's strict timestamp and manual-reset requirements.
- Local drawdown recording: non-blocking bounded queue and daemon worker for noncritical atomic JSON persistence. Durable trading state and SQLite FULL untouched.
- Latency Diagnostics: local per-bot P50/P95 by phase, sample counts and latest timestamp; worker-side dropped/write-errors sidecar; missing diagnostics shown unavailable instead of a fictitious zero.
- New bot wizard: LIVE local GRID percentage, GRID depth, BUY notional/full-grid spend and floating-loss estimate at hypothetical bottom, TP examples rounded UP to tick and estimated maker net profit. Numeric screens refresh from cached setup BID without additional REST per keystroke. ANSI/curses colors with graceful fallback.
- Comprehensive statistics redesign deferred. Virtual TP, GRID strategy, FIFO, reconciliation, HARD/PANIC execution engine and state schema unchanged.
- Package creation does not change LIVE processes or publish a GitHub Release; operator performs deployment separately.

## v0.7.22.1 — Telegram hotfix: precise BID and debounced health

- `/status` now obtains an exact positive BID from the engine's `state/stats_financial_anchor.json`, pairing it with `state/stats.json` by instrument, schema and identical sample timestamp; a missing/mismatched price is displayed as `н/д` without a false −100% change.
- Includes the Telegram heartbeat/observer debounce hotfix previously installed on VPS: sustained stale readings before alarming, and three fresh recovery samples spanning at least 15 seconds.
- The v0.7.22 trading strategy, Virtual TP, FIFO, PANIC order safety, schema 1 and per-bot LIVE trading code are unchanged. Only shared release identity and Telegram read-only modules are updated by deployment.

## v0.7.22 — Telegram reliability and UI (candidate)

- Telegram systemd journal cursor and invocation-ID detection for fast STOP/START; engine heartbeat from runtime_stats.json; observer process/freshness checks with recovery alerts.
- Durable Telegram incident outbox with bounded retry/backoff; repeating same fault after 30min quiet or explicit recovery; alerts for EMERGENCY_POSITIONS_* and COVERAGE_RECOVERY_WAIT.
- PANIC terminal notifications: only final verified report when accounting is ready; pending at >15sec; unresolved warning >60sec.
- Manager PANIC naming and single deliberate PANIC confirmation (no FINAL prompt).
- Precise display prices at instrument tick precision in statistical report, peak price and Telegram; early profit projections unchanged.
- Menu presets include DOOD, exclude APR. Schema 1 unchanged. No FIFO/Virtual TP/HARD/PANIC order mechanics, SOFT latch, drawdown fsync changes.
- This is an OFFLINE candidate. No LIVE services, exchange orders or GitHub publication are affected by building the archive.

## v0.7.21 — Offline candidate: emergency safety + Coverage HOLD

- HARD/PANIC: owner-verified pending BUY cancellations before position reconciliation; cannot cancel foreign/unproven orders; durable single-send cancel intents preserved.
- PANIC MARKET: expTime-backed durable intent, definitive rejection accounting, three-stage history/pending absence checks for uncertain POST, bounded retry with a new client ID only after positive proof, terminal accounting excludes rejected/absent zero-fill attempts.
- Coverage HOLD: 0.5 → 1 → 2 second bounded backoff for unchanged conditions; private-order event wake; public BID changes do not reset it; all resume decisions still need fresh REST reconciliation.
- FIFO, normal position reconciliation 150/300/500 ms, virtual TP strategy, Telegram and unrelated functions unchanged. State schema remains 1.
- OFFLINE ONLY: no GitHub publication, LIVE installation or trading orders as part of this candidate.

## v0.7.20 — Telegram monitoring (2026-10-20)

- Telegram PANIC notifications: confirmed closure, then verified terminal PnL.
- Partial BUY/SELL alerts: contracts, base-coin equivalent and price.
- Partial-cycle follow-ups after final closing: LIMIT/MARKET and verified net PnL.
- Existing daily report and Market SELL format unchanged.
- Trading strategy, trading-engine code, orders, risk controls and schema unchanged.
- Includes v0.7.19.1 Telegram import-path hotfix.

## v0.7.19 — 2026-10-20 — Clean-engine baseline

- Removed historical state/cycle/fill migrations and old monitoring adapters.
- Refactored API/accounting/strategy/state; unified version and runtime paths.
- Validated USDT-settled linear SWAP instruments without base-coin allowlist.
- Archive view shows final confirmed PnL before historical snapshot.
- Preserved trade safety and FIFO behavior; new bot states only.

## v0.7.18 — 2026-10-20 — Internal runtime paths

- LIVE state/SQLite/logs/diagnostics migrated to stable file and directory names; existing history preserved.
- Introduced `okx_runtime_paths.py`; current bots use new paths, archived older bots remain readable through legacy paths.
- Updated engine, manager, Telegram and observers; included verified STATE MISMATCH / BACK TO NORMAL hotfix.
- Explicitly deferred account-wide FIFO gate renames, compatibility-module refactoring and any trading/TP strategy changes.
- Published the exact verified LIVE public source without restarting trading services.

## v0.7.17 — 2026-10-19 — Project cleanup

- GitHub main: один README, удалены исторические README/Release Notes, устаревшие исходники и тестовые комплекты; старые теги сохранены.
- Проверка состава и зависимостей текущего кода; действующие compatibility-модули оставлены без изменений.
- LIVE: версии менеджера, движка, манифеста, текущего config/state согласованы на v0.7.17.
- Торговая стратегия, ордера, FIFO, DD, Telegram-функции и данные архивных ботов без изменений.
- Миграция внутренних версионированных SQLite/JSON и compatibility-файлов намеренно отложена.

## v0.7.16 — 2026-10-19 — DD + Telegram + version alignment

- LIVE-движок и менеджер согласованы на v0.7.16; архивные экземпляры не изменены.
- DD: каждые 5 секунд из оперативного PnL/позиции/BID, без дополнительных API/fills/retries; исторический минимум сохранён.
- DD-файлы без суффиксов версий; свежая максимальная просадка доступна Telegram.
- Telegram `/status`, `/latency`, `/latency8h`, STATE MISMATCH и BACK TO NORMAL: уточнены данные, формат и дедупликация.
- Архив двух выведенных из эксплуатации исторических экземпляров размещён отдельно, без секретов.
- Не изменены торговая стратегия, общий FIFO, Coverage Recovery, virtual_escape, funding.
- Оставшиеся исторические имена SQLite, логов и compatibility-файлов требуют отдельной миграции.

## v0.7.14 — post-release operational verification (2026-10-19)

All four engines, manager and eight versionless observer services verified on LIVE. Six v0.7.12 observer systemd drop-ins were removed after backups; the published v0.7.14 runtime code was already correct. See [verification](LIVE_POST_RELEASE_v0714.md).

## v0.7.13 — 2026-10-18 — Management/monitoring hotfix
- Общая ручная разблокировка после account SOFT STOP и корректный запрет Resume при активной блокировке.
- Постоянные systemd-имена наблюдателей; совместимость lifecycle handler.
- Исправлено отображение и управление наблюдателями из меню okxbot без дублирования служб.
- Размер ротации встроенной latency-телеметрии увеличен с 16 до 256 МиБ на файл.

## v0.7.12 — 2026-10-18 — GitHub Release
- Межпроцессный FIFO для `/account/positions` (8/2,1 с) без локального обходного лимитера; устаревшие резервации корректно очищаются, некорректные данные fail-closed.
- Общий `fills-history` (9/2,1 с) и Single-Flight для `ordId`; справедливость фоновых GET и расширенная телеметрия торгового потока.
- Coverage Recovery: временное несовпадение позиции блокирует новые BUY без безусловного прекращения сверки уже работающих TP; критические ограничения сохраняются.
- Ротация профиля reconciliation (16 MiB + backup), точность отображения цен TP и частичных выходов, антиспам `PERSISTED_STATE_DIFFERS_FROM_ENGINE`.
- Миграция конфигурации и state 0.7.11 → 0.7.12 с сохранением истории/SQLite. LIVE ETH079/ZEC0710/SUI0710-2; шесть read-only наблюдателей переведены на новый код.
- Базовые 142/142 PASS относятся к оригинальной v0.7.11; в v0.7.12 проводились отдельные компонентные и LIVE-проверки. Исторический install.sh не предназначен для миграции этой версии.

## v0.7.11 — 2026-10-18 — GitHub Release
- Safe fast retry при неполной сверке позиции (150/300/500+ мс); полные REST-проверки и защита от неопределённых POST сохранены.
- Ротация незащитных SELL TP не задерживает BUY-refill; обязательное покрытие позиции и аварийные режимы без ослабления.
- Асинхронные метрики (`latency_v0711.jsonl`) + отчёт P50/P95/P99 и раздельные `positions_gate_lock_wait`/`positions_gate_quota_wait`.
- RC2: `flock` освобождается во время ожидания квоты, повторная проверка квоты атомарная. RC3: исправлен allowlist метрик.
- Офлайн 142/142; LIVE все четыре экземпляра ETH079/ZEC0710/SUI0710-2/SOL0711 на одном RC3; постмиграционный аудит служб, мониторов, SQLite, BUY 5/5 и PnL pending 0.
- Остались наблюдавшиеся `ACCOUNT_POSITIONS_GATE_DEGRADED` и фоновые задержки fills-history; механизм fallback не переработан.

## v0.7.10 — 2026-10-17 — GitHub Release
- Поддержка NEAR/ZEC/SUI в движке и новом мастере `okx_setup0710.py`, сохранена совместимость старого мастера.
- Форматирование ордеров и ценовых ключей с учётом `tickSz`; устранено схлопывание близких уровней и ошибочное округление attached TP.
- Исправлены встроенный self-test и отображение малых шагов/TP в настройках, максимум реальных TP показан как 150.
- `ExecStopPost` отключает автозапуск после подтверждённого `PANIC_DONE`, останавливает и отключает привязанные наблюдатели; глобальный Telegram-монитор не трогает.
- 99 офлайн-тестов (45+13+22+19); LIVE ZEC/SUI. Остановка двух активных наблюдателей после PANIC пока проверена только mock-systemd, а не LIVE.

## v0.7.9 — 2026-10-17 — GitHub Release

- **BUY Refill Priority:** избыточные реальные SELL TP не задерживают пополнение недостающих BUY после завершённой REST-сверки и `coverage_audit`; сохранены лимиты BUY, защита от дубликатов и неопределённого POST.
- **TP Quiet Cleanup:** при TP выше цели отмена по одному после 1,5 с без новых TP, с повторной сверкой; защитный предел 150 TP и резерв 5 ордеров для attached TP.
- **Мониторинг:** отдельный read-only v0.7.9 observer и контроллер; исправлена маршрутизация менеджера, предотвращён ошибочный fallback к монитору v0.7.4; Telegram-меню сохранено.
- **Тестирование:** 45 регрессионных v0.7.6 + 13 v0.7.8 + 9 v0.7.9 + 13 поведенческих офлайн стресс-тестов = 80.
- **Без изменений:** `virtual_escape`, отправка ордеров, учёт комиссий, работающие LIVE-службы и состояние старых ботов. Автоматический `disable` после PANIC_DONE и инструменты NEAR/ZEC/SUI запланированы для следующего релиза.

## v0.7.8 — 2026-10-16 — GitHub Release (включает v0.7.7)

- **v0.7.7 / Terminal Accounting:** после PANIC_DONE расчёт окончательного торгового PnL по фактическим исполнениям и комиссиям, без изменения обычного GRID PROFIT; HARD оставляет исторический снимок позиции/нереализованного PnL, без итоговой прибыли. При неполных исполнениях итог не публикуется. Служебный GET-only повторный расчёт, включая ранее завершённые экземпляры.
- **Полная история:** завершённые PANIC/HARD показывают весь прежний статистический отчёт плюс финальный блок, а не заменяют его сокращённым.
- **Manager:** выбор последней установленной версии движка и проверка зависимостей из `engine_manifest.json`, без необходимости переписывать менеджер для каждого будущего релиза; мастер создания используется только при создании нового экземпляра.
- **Telegram:** `/status` показывает только активные боты, `/status NAME` — адресные отчёты, включая завершённые. Автообнаружение экземпляров через реестр/systemd.
- **v0.7.8 / Monitoring:** длительность дефицита BUY теперь измеряется по журналу движка, а не по запаздывающему `state.json`; предусмотрена диагностика расхождения источников. Burst/Execution наблюдатели ограничены в ресурсах, буферизируют записи и не делают OKX API вызовов.
- **Attached TP:** частичное исполнение больше не предполагается только из факта раннего обнаружения прикреплённого TP. Событие `PARTIAL_ATTACHED_TP_DISCOVERED` только для доказанной частичности, иначе `ATTACHED_TP_DISCOVERED`.
- **LIVE:** проверены ETH077 и SOL077 (v0.7.8, по 5 BUY/5 TP) с отдельными наблюдателями. Стратегия, `virtual_escape`, торговые лимитеры и REST-оптимизации не менялись.

## v0.7.6 — 2026-10-16 — GitHub Release

- **P0 — межпроцессный лимитер `/account/positions`:** самовосстановление повреждённого JSON под `flock`, отбрасывание временных меток из будущего, ограничение ожидания блокировки 5 сек, ограниченный локальный резервный лимитер (4 запроса за 2,1 сек на процесс) при сбое общего файла, предупреждения `ACCOUNT_POSITIONS_GATE_RECOVERED/DEGRADED`. Устранён сценарий общей блокировки сверки ETH и SOL из-за сбоя лимитера.
- **P2 — Telegram MARKET SELL:** отдельные уведомления `AGG_MARKET_INTENT`, `AGG_MARKET_ACK`, `AGG_MARKET_REJECTED`, `AGG_MARKET_TERMINAL` без десятиминутного подавления; терминальное событие содержит фактические `filled`, `avgPx`, `state`, `ordId` при наличии биржевых данных. Итоговый PnL не рассчитывается искусственно.
- **P3 — неопределённые POST:** продлена проверка отрицательного результата размещения самостоятельных BUY/TP до 6 суток вместо 30 минут; сохранены защита `expTime`, три независимые проверки полной истории и **запрет слепого повторного POST**.
- **Включён ранее установленный LIVE hotfix r2 (no-churn):** устранено циклическое выставление и немедленная отмена нижнего BUY при заполненной 25-ордерной сетке; сохранены hotfix r1 — отмена BUY снизу вверх, защита 5 шагов и временный BUY-буфер +5.
- **Не изменялись:** политика MARKET SELL `virtual_escape` (P1), REST/JSON-производительность (P4), торговые параметры BUY/TP и PnL.
- **LIVE-проверка:** 16.10.2026 ETH064 и SOL065 запущены на v0.7.6 с единым SHA256 `218024dc611a85d076d1ce40d924ca555f21300b861663b7f0876ac729329481`; обе службы активны, 25/25 BUY, 0 рестартов в контрольном снимке, Telegram-монитор активен. 45 изолированных тестов прошли. Публикация GitHub сама по себе не изменяет действующие службы или состояние торговли.

## v0.7.5 — 2026-10-16 — GitHub Release

- **Ускорение сверки:** компактный JSON и объединённое сохранение уже подтверждённых биржевых фактов; `fsync` и атомарная замена сохранены. Перед каждым POST обязателен долговечный барьер и сохранение намерения с уникальным client ID; сохранность после потери ACK проверялась офлайн.
- **Более быстрый Grid Following:** повтор REST-сверки позиции через 500 мс, общая квота account positions для ботов на одном VPS, промежуток выставления BUY 50 мс. Размер и условия TP-буфера не менялись.
- **BUY cleanup hotfix r1:** при сокращении BUY-буфера отменяем сначала ордера с **самой низкой ценой**, строго снизу вверх; убран неверный отбор по `desired` сетке. Повторная проверка актуальных BUY, BID и `ordId` перед POST; защита ближайших пяти шагов сохранена. Подтверждено на LIVE ETH/SOL 16.10.2026.
- **Telegram:** обновлён независимый монитор с критическими уведомлениями и новым `/status`: сводка двух ботов, время с первого запуска, BID, циклы, прибыль, комиссии, объём, открытая позиция, PnL, темпы, просадка и исторический максимум позиции через helper менеджера.
- 34 офлайн-теста прошли (в том числе AST-контроль safety-функций, crash/ACK-loss recovery, Telegram); дополнительный синтетический replay доступен отдельно. Мастер новых ботов обновлён до `okx_bot075.py`; отсутствовавший в v0.7.4 модуль совместимости мониторов включён в репозиторий. BTC не заявлен LIVE-протестированным. Публикация не перезапускает торговые службы, не затрагивает их состояние и ключи.

## v0.7.4 — 2026-10-15

- Unified manager/status/settings: actual version from systemd, real-time local BUY/SELL/virtual/partial counts, process and VPS uptime, monitored grid gaps with freshness checks. LIVE journal preserved.
- New wizard: only BUY and real SELL TP base counts configurable (1..25); fixed BUY buffer +5, near-BID cancel protection 5 steps; SELL buffer dynamically BUY base +5 (6..30); unique bot names 3..20 characters.
- Old config `active_tp_orders=75` no longer shown as effective target; legacy buffer/protection entries ignored by v0.7.4 engine. Historical state is preserved during v0.7.3→v0.7.4 migration.
- Trading engine exposes its actual TP cooldown and cancel-gap decision timings to avoid false observer warnings. No performance changes to REST execution or durable order handling.
- One package for BTC/ETH/SOL, optional per-instance monitors. No automatic LIVE changes.

# OKX Grid Bot — полная история версий

История разработки начиная с **v0.5.9 — первого подтверждённого запуска торгового ETH-бота на реальном OKX**. Включены публичные релизы GitHub, промежуточные LIVE-версии и внутренние сборки. Это история разработки продукта, **не история сделок, счёта или конфигураций конкретных пользователей**.

> **О статусах.** «GitHub Release» означает подтверждённую публикацию тега и страницы релиза. «Внутренняя/LIVE» означает, что версия использовалась на реальном боте, но отдельный публичный релиз для неё не подтверждён. «Внутренняя» означает задокументированный этап разработки; без утверждения о публикации или LIVE. Ранняя история v0.5.9–v0.6.4 восстановлена ретроспективно из журналов разработки; точные даты отдельных промежуточных сборок не установлены. Запись в CHANGELOG **не создаёт задним числом Git-тег или исходный архив**.

| Версия | Статус | Основной этап |
|---|---|---|
| **v0.7.3** | GitHub Release · 14.10.2026 | Универсальный BTC/ETH/SOL, SELL TP 25+30, выборочные мониторы |
| **v0.7.2** | GitHub Release · 14.10.2026 | Защита ближайших BUY при Grid Following |
| **v0.7.1** | Внутренняя/LIVE · 14.10.2026 | Быстрое сопровождение BUY, агрегированный MARKET для виртуальных TP |
| **v0.7.0** | Внутренняя/LIVE · 14.10.2026 | Профилирование задержек сверки; наблюдение бурстов |
| **v0.6.10** | GitHub Release · 14.10.2026 | `expTime`, безопасное восстановление POST, Telegram |
| **v0.6.9** | GitHub Release · 14.10.2026 | Реакция на ошибки OKX, общий AUTO SOFT STOP |
| **v0.6.8** | GitHub Release · 14.10.2026 | SQLite, PnL, надёжность, лимит 99 заявок |
| **v0.6.7** | Внутренняя | Максимальная просадка PnL отдельного бота |
| **v0.6.6** | Внутренняя | Завершение устаревшего частичного BUY |
| **v0.6.5** | Внутренняя | Сверка частичных исполнений и TP |
| **v0.6.4** | Внутренняя | Pause/Soft/Hard/Panic и подтверждения действий |
| **v0.6.3** | Внутренняя | Интерактивные статистика и LIVE-журналы |
| **v0.6.2** | Внутренняя | `stats.json` без запуска второго движка |
| **v0.6.1** | Внутренняя | Именованные экземпляры и CLI-менеджер |
| **v0.6.0** | Внутренняя/LIVE | Выбор BTC/ETH/SOL, запуск LIVE SOL |
| **v0.5.9** | Внутренняя/LIVE | Первый подтверждённый LIVE ETH |

## v0.7.3 — 2026-10-14 — [GitHub Release](https://github.com/igorauts/okxbot/releases/tag/v0.7.3)

- Объединённый LONG/CROSS-движок `okx_bot073.py` для `BTC-USDT-SWAP`, `ETH-USDT-SWAP` и `SOL-USDT-SWAP`; до объединения отдельные адаптации v0.7.3 работали на ETH и SOL.
- Настраиваемые **независимо для каждого экземпляра**: целевые BUY (1–25), временный BUY-буфер (0–5), защита от отмены около BID (0–10 шагов сетки), базовые реальные SELL TP (1–25), временный буфер TP (0–30).
- SELL TP: 25 базовых + максимум 30 временных, 1500 мс тихого периода, последовательная отмена самых дальних не чаще одной за 100 мс. Биржа может кратковременно создать attached TP сверх программного порога; очистка выполняется после сверки.
- Сохранены виртуальные TP, агрегированный reduce-only MARKET, безопасное восстановление неизвестных POST и учёт фактических комиссий/PnL.
- Мастер и менеджер создают экземпляры из универсального движка; Burst Monitor и Execution Monitor можно включать **для выбранного бота**, по умолчанию выключены.
- Пройдены 104 офлайн-теста (74 регрессии и 30 проверок универсальности/наблюдателей/интеграции). **BTC не заявлен как LIVE-протестированный**, универсальный файл проверен офлайн. Публикация GitHub не обновляет запущенных ботов.

## v0.7.2 — 2026-10-14 — [GitHub Release](https://github.com/igorauts/okxbot/releases/tag/v0.7.2)

- Исправлена ошибка ETH v0.7.1: при BID около 2430.00 бот мог отменить ближайший BUY 2430.00, поставив далёкий 2417.50.
- В очистке BUY-буфера 25→30→25 защищены заявки в пределах **трёх шагов GRID_STEP** от текущего BID (при ETH step $0.50 — ±$1.50). Буфер +5 — **количество** дополнительных BUY, защита 3 шага — **расстояние** до BID; это разные параметры.
- Отмена выбирает самый дальний устаревший BUY, повторно проверяет BID непосредственно перед отменой; 1500-мс cooldown учитывает движение сетки **в обе стороны**. Если все 30 мест заняты и безопасной отмены нет, размещение откладывается.
- Лог `BUY_CANCEL_PROTECTED`, миграция ETH с сохранением циклов/TP/PnL. 61 офлайн-тест; SOL при этом оставался на v0.6.10.

## v0.7.1 — 2026-10-14 — внутренняя LIVE-версия ETH, без отдельного GitHub Release

- Быстрое сопровождение BUY: 25 штатных LIMIT + буфер до 30 при движении рынка; новый BUY ставится прежде отмены устаревшего, когда есть свободное место.
- Пробуждение торгового цикла по публичному WebSocket BID с ограничением частоты price-only сигнала (500 мс); секундный периодический цикл сохранён.
- Цель 25 реальных SELL TP; остальные сопровождаются как виртуальные. При полном исчерпании реальных SELL и ранее зафиксированном пересечении виртуальных TP допускается **один агрегированный reduce-only MARKET SELL** с подтверждённым объёмом, а не отдельный MARKET на каждый цикл.
- Защита от повторных POST, частичных исполнений, дублирования PnL и комиссий; распределение fills одного MARKET между родительскими BUY по FIFO. Исправлены внутренние кандидаты r2 (лимит 30 BUY), r3 (24h PnL агрегированного SELL), r4 (фиксация пересечения TP при последующем откате BID).
- В ходе LIVE была обнаружена ошибка отмены BUY возле BID; исправлена в v0.7.2.

## v0.7.0 — 2026-10-14 — внутренняя LIVE-версия ETH, без отдельного GitHub Release

- Инструменты профилирования задержек REST и сверки: `pending`, `register_attached`, `track`, `positions`, `retry_sleep`, `state_save`, `rest_governor`, `rest_http`.
- Профиль `logs/reconcile_profile_v070.jsonl` фиксирует медленные проходы и повторные сверки; построен внешний read-only Burst Observer для наблюдения за восстановлением BUY после массовых исполнений.
- Сохраняется консервативная сверка позиции, attached TP и исполнений OKX; ранний development checkpoint был *только* инструментированием, без изменения торговой стратегии. Позже версия запускалась LIVE на ETH.

## v0.6.10 — 2026-10-14 — [GitHub Release](https://github.com/igorauts/okxbot/releases/tag/v0.6.10)

- Добавлен OKX REST `expTime` для размещения BUY и виртуальных TP: срок приёма заявки записывается **до** HTTP POST (не означает срок жизни LIMIT после принятия).
- При потерянном ACK восстановление только по точному `clOrdId`. Для новых намерений с deadline: ожидание ≥120 с и три независимые полные отрицательные проверки истории; старые неизвестные ордера продолжают блокироваться безопасным режимом.
- Неопределённый SELL резервирует собственный объём, но не мешает сопровождать другие виртуальные TP, покрытие которых доказано отдельно.
- Опциональный внешний Telegram-монитор: критические события, состояние сервисов, сводка в 09:00 по Santiago и read-only `/status`.
- Обновлены обнаружение движка и systemd drop-in в менеджере; подготовка репозитория и тесты. Миграция v0.6.9 с резервной копией без сброса SQLite или торгового режима.

## v0.6.9 — 2026-10-14 — [GitHub Release](https://github.com/igorauts/okxbot/releases/tag/v0.6.9)

- Обработка окончательных отказов OKX при BUY, TP и отменах с проверкой идентичности заявки.
- Общий для ботов одного аккаунта **AUTO SOFT STOP** при проблеме недостаточной маржи. Блокировка переживает перезапуски, возвращение в RUN только вручную.
- Сверка неопределённых POST по исходным идентификаторам, без слепой повторной отправки; неподтверждённый BUY не должен блокировать независимые TP.
- Восстановление отклонённых TP и отмен с backoff; предупреждения при длительном рассогласовании. Добавлены специальные тесты безопасности. Состояние и SQLite сохраняются.

## v0.6.8 — 2026-10-14 — [GitHub Release](https://github.com/igorauts/okxbot/releases/tag/v0.6.8)

- Долговременный архив подтверждённых исполнений `fills_v068.sqlite`, закрытых циклов `cycles_v068.sqlite`, реализованного PnL `pnl_v068.sqlite`.
- Проверенный однократный перенос прежней истории. После миграции догружаются только недостающие исполнения, а не вся история OKX при каждом расчёте.
- Просадка отдельного бота рассчитывается по согласованным снимкам; исправлены ошибки worker retry с пятиэлементным снимком и расчёта прибыли за 24 часа после переноса завершённых циклов.
- Безопасные повторы GET и HTTPS keep-alive. При неопределённом POST или расхождении позиции — ожидание подтверждённой сверки, без слепой заявки.
- Историческое ограничение: суммарно 99 активных BUY + реальных SELL TP, при 25 BUY — максимум 74 реальные TP (не актуальная схема 25+30 из v0.7.3).
- Сохранены стратегия, правила частичных BUY, минимальный TP, отсутствие учёта funding.

## v0.6.7 — внутренняя версия, точная дата не установлена

- Постоянное отслеживание **максимального отрицательного итогового PnL отдельного бота**; отказ от некорректной привязки к equity всего аккаунта.
- Историческая метрика — максимум `max(0, -total_PnL)` с момента начала замеров, **не** классическая просадка equity «от пика до впадины»; funding не включён.

## v0.6.6 — внутренняя версия, точная дата не установлена

- Механика освобождения «застоявшегося» частично исполненного BUY: ожидание заданного времени и достаточного роста BID над уровнем.
- Подтверждённая отмена остатка BUY, сопоставление появившегося attached TP, затем при необходимости отдельный reduce-only MARKET на доказанный остаток. Никакого повторного MARKET при неизвестном результате.
- PnL таких частичных выходов считается отдельно от обычной GRID-прибыли. Стандартизованы английские подтверждения управляющих команд.

## v0.6.5 — внутренняя версия, точная дата не установлена

- Исправление согласованности частично исполненных BUY, остаточного объёма и прикреплённых TP.
- Дополнительные тесты состояний fill/cancel/TP, чтобы не потерять позицию и не создать дублирующий SELL.

## v0.6.4 — внутренняя версия, точная дата не установлена

- В управлении экземплярами появились **Pause**, **Soft Stop**, **Hard Stop**, **Panic Sell** с разной семантикой новых BUY, существующих ордеров и позиции.
- Добавлены явные подтверждения рискованных действий. Hard оставляет позицию открытой без TP; Panic предполагает MARKET-выход. Эти функции требовали отдельной проверки безопасности и не должны автоматически переноситься на старые движки.
- Улучшался интерактивный интерфейс менеджера, в том числе управление отображением LIVE-журнала.

## v0.6.3 — внутренняя версия, точная дата не установлена

- Интерактивные экраны `stats` и `logs` для именованных экземпляров; просмотр статистики и LIVE-журнала без запуска ещё одного торгового бота.

## v0.6.2 — внутренняя версия, точная дата не установлена

- Универсальный `stats.json` как снимок статистики экземпляра; менеджер быстро читает состояние **без второго торгового процесса**.

## v0.6.1 — внутренняя версия, точная дата не установлена

- Имена и независимое управление экземплярами; CLI `okxbot` с командами `start`, `stop`, `stats`, `logs`, `list`.
- Действующие экземпляры могли быть зарегистрированы в менеджере без замены кода LIVE-движка.

## v0.6.0 — внутренняя/LIVE, точная дата не установлена

- Универсальный выбор инструмента из **BTC / ETH / SOL**, динамические параметры сетки и инструмента, верхняя граница торгового диапазона, мастер настройки.
- Запущен подтверждённый **LIVE-бот SOL-USDT-SWAP**; существующий ETH v0.5.9 продолжал работать.

## v0.5.9 — первая подтверждённая LIVE-версия ETH, точная дата не установлена

- Запущен ETH-USDT-SWAP LONG Grid Bot как отдельный systemd-процесс на реальном счёте OKX.
- Из ранних журналов подтверждены 25 активных BUY и до 75 реальных attached SELL TP, исполнение торговых циклов и расчёт комиссий/PnL.
- Это отправная точка истории **работающего OKX Grid Bot**; более ранние эксперименты не включены в этот перечень.

---

**Правило ведения истории:** добавлять каждую следующую версию сразу при разработке, даже если она останется внутренней; после GitHub-публикации добавлять ссылку на релиз. Ретроспективные исправления документации не требуют изменения прежних Git-тегов или архивов.
