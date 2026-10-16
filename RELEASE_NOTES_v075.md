# OKX Grid Bot v0.7.5 — Faster Reconciliation, Telegram Status & BUY Cleanup Fix

16 October 2026. Based on the published v0.7.4 release. This release does **not** deploy or restart any trading process.

## Performance and crash safety

- Compact on-disk JSON (`separators=(',', ':')`) while preserving atomic replace, file `fsync` and directory `fsync`.
- Coalesce durable writes for already confirmed exchange facts during `track()` / position reconciliation. Flush confirmed facts before any new order POST; durable POST intentions (unique client IDs and cancellation intent) remain committed **before** the HTTP request.
- BUY placement combines a closed-cycle archive update with the new durable `PLACING` intention. An ACK does not trigger an extra immediate full-state write; recovery uses the original durable client order ID and cannot blindly submit a duplicate.
- Position reconciliation retry delay 500 ms (previously 1000 ms); conservative shared VPS limiter for OKX account positions GET (8 calls / 2.10 s across participating bot processes). BUY placement pacing 50 ms (previously 100 ms).
- Controlled offline replay across ETH/SOL: 40 and 145-cycle bursts, live attached TP and partial BUY scenarios. The measured offline case of 145 completed cycles required 1 consolidated save instead of hundreds of individual saves; this is **not a guaranteed real-market speedup**. Crash/ACK-loss regression scenarios included.

## Correct BUY cancellation order — bottom-up hotfix r1

- **Overflow cleanup cancels the BUY with the lowest price first.** Further cancellations proceed in ascending BUY-price order; selection is independent of the recalculated `desired` grid.
- The previous v0.7.5 candidate could consider a BUY at the current BID for cancellation while overlooking a lower BUY. This is corrected both in candidate selection and in a fresh, whole-grid recheck immediately before the cancellation POST.
- Five-grid-step protection near the BID is preserved. The 25 base BUY + up to 5 transient buffer arrangement is unchanged; cleanup returns to base target after the existing cooldown. This fix does not introduce automatic liquidations when the strict 30-BUY invariant is exceeded.
- Confirmed on ETH and SOL LIVE on 2026-10-16: actual cancellation logs show increasing price sequences, while trading and TP continued. Full independent exchange snapshots were not saved for each cancellation.

## Telegram monitor

- External read-only monitor continues critical alerts, systemd and heartbeat warnings, the Santiago daily digest and `/status` for ETH and SOL.
- `/status` now includes runtime health, **age since the bot's original start** (not the systemd restart), BID, closed cycles, grid profit, volume, actual fees, 24-hour profit, open position, unrealized/total PnL, average daily/monthly/yearly pace, max drawdown and historical peak position.
- Financial figures come from the engine's existing stats report, not re-computed by the Telegram monitor. Historical peak position reuses the optional installed helper `okx_position_peak.format_peak`; when unavailable, that field is shown as `н/д` instead of estimating it.
- Monitor has **no exchange API keys** and cannot place/cancel orders. Telegram network failures must not interrupt trading.

## Validation & limitations

- Exactly **34 offline unit tests** in `v075_checks/` passed on the packaged code: 24 engine/bottom-up, 6 Telegram status, 3 crash-recovery test methods (covering multiple ETH/SOL subcases), and 1 wizard regression test. Additional synthetic reconcile replay available as `v075_checks/stress_v075_reconcile.py`.
- LIVE: on both `eth064` and `sol065`, hotfix r1 ran under v0.7.5 with the identical engine SHA256 `14e96dc7aa4088811f5848e790f7e0449346a0c723531c79589eb3acf8a1cf93`; Telegram `/status` was verified in the real chat. Real-market stress comparable to the earlier sharp selloff has **not** been demonstrated yet.
- The existing diagnostic observer `okx_monitor074.py` and the bundled missing manager dependency `okx_monitor_compat074.py` remain v0.7.4-specific; they must not be described as full v0.7.5 observers. This release updates the independent Telegram monitor, not those trading diagnostic observers.
- BTC support is offline-only, not claimed LIVE-tested. No changes to SELL TP buffer, execution fees, realized PnL accounting, API credentials, or existing user state.
- This source release is **not an updater**: migrating pre-existing LIVE instances requires their own backup, read-only exchange audit, safe systemd cutover and observation. **Never overwrite state JSON/SQLite from a backup on top of newer real fills.**

## Contents

`okx_bot075.py`, `okxbot.py` manager with v0.7.5 detection/cloning, `okx_setup068.py` wizard updated to create v0.7.5 instances, `okx_ui074.py` (read-only v0.7.5 display support), updated `okx_telegram_monitor.py`, `v075_checks/` offline validation and this release documentation. The previously missing `okx_monitor_compat074.py` is included for the manager; historical files and CHANGELOG from v0.7.4 are preserved. The release archive also bundles read-only observers, setup, peak-position helper, and dependency manifest.
