# OKX Grid Bot v0.7.6 — Resilient Position Gate, MARKET Alerts & BUY No-Churn

**16 October 2026.** Source release built from the LIVE-tested v0.7.6 engine deployed to `eth064` and `sol065`. Publishing the repository does **not** install, stop, restart, or change live trading bots.

### Included changes

- **P0 — shared account-positions rate gate:** corrupted/truncated JSON is recovered while holding a filesystem lock rather than raising `ExchangeUnresolved` indefinitely; wall-clock timestamps from the future are discarded; previous-file tails cannot survive the next write; a 5s lock wait deadline and a bounded 4-requests/2.1-second *per-process* fallback limit protect trading when the shared gate is unavailable. On two bot processes this means up to 8 requests per 2.1 seconds during degraded operation. `ACCOUNT_POSITIONS_GATE_RECOVERED` and `ACCOUNT_POSITIONS_GATE_DEGRADED` are logged and surfaced in Telegram. The fallback is not a distributed rate limiter across additional hosts/processes.
- **P2 — Telegram aggregate MARKET SELL alerts:** `AGG_MARKET_INTENT`, `AGG_MARKET_ACK`, `AGG_MARKET_REJECTED`, `AGG_MARKET_TERMINAL` are delivered per event without the ordinary 600s deduplication window. Terminal notifications include confirmed quantity, average execution price and status where available. **No fabricated realized PnL.** Telegram delivery remains at-least-once: a duplicate is possible if the process crashes between sending and saving its log offset.
- **P3 — uncertain POST recovery:** standalone BUY/TP placement requests protected with `expTime` can continue evidence-based negative reconciliation for up to six days (previously 30 minutes), within the seven-day history horizon. They are never blindly re-sent. Checks require clock agreement, history coverage, and three independent successful negative snapshots. Incomplete history remains fail-closed.
- **Includes LIVE hotfix r2 no-churn:** prevents rapid bottom BUY placement/cancellation loops when there are already 25 active BUY; missing BUY are still replenished and new upper BUY may be added on an upward move. Preserves r1 bottom-up cancellation of lowest-price BUY and the fixed five-step BID protection.

### Explicitly unchanged

- **P1 `virtual_escape` MARKET SELL policy** is unchanged at the operator's request. A pending virtual TP signal may still lead to a market exit below the original entry price after a sudden reversal; market orders also carry slippage and taker fees.
- **P4 REST, state-save/JSON performance and pacing** unchanged from the LIVE v0.7.5 r2 baseline. BUY and TP trading logic, financial accounting and risk envelopes unchanged.
- Support remains the BTC/ETH/SOL USDT-SWAP LONG/CROSS envelope enforced by the trading engine. The account's public instrument list is not an API key's per-crypto trading authorization.

### LIVE evidence and reproducibility

The operator's live cutover finished at **2026-10-16 06:32 UTC**. Both services remained active at the post-deployment check: ETH `BUY 25/25`, SOL `BUY 25/25`, no restarts, Telegram monitor active. Both installed engines: SHA256 `218024dc611a85d076d1ce40d924ca555f21300b861663b7f0876ac729329481`. The original service entrypoint filename on that VPS is still `okx_bot075.py` for compatibility, but its actual code and `VER` are v0.7.6. New clones and wizard-created instances from the source release use `okx_bot076.py`.

45 isolated offline tests cover gate corruption, clock rollback, fallback limit, uncertain POST safety, MARKET Telegram alerts, and BUY cleanup/no-churn. They do not open orders or access the running exchange account. Live MARKET SELL alert delivery has not been triggered specifically to test Telegram; code and offline tests were checked.

The bundled `okx_monitor074.py` / `okx_execution_observer074.py` remain historical optional v0.7.4 observer components; v0.7.6 provides an updated standalone Telegram monitor. Do not treat observer074 as a complete v0.7.6 observer.

### Source package

`okx_bot076.py` trading engine; `okxbot.py` manager; `okx_setup068.py` wizard (creates v0.7.6 bots); `okx_ui074.py` read-only interface; `okx_telegram_monitor.py`; unchanged auxiliary helpers and dependencies; `v076_checks/` offline safety tests and fixtures; docs/CHANGELOG.

**Do not deploy this GitHub archive over active state files or run the historical `install.sh` to upgrade live bots.** Existing bot upgrades require backups, exchange GET-only audit and an individually controlled cutover, and must never overwrite newer exchange-derived state from an old backup.
