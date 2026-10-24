# v0.7.29 source release conditions

## v0.7.29 — Maintenance / UI only (2026-10-24)
- ENGINE_VERSION stays 0.7.28; trading engine, FIFO, funding, strategy and state schema unchanged.
- Integrate already-LIVE verified observer provisioning for pristine unstarted bots.
- /status: fail-closed account total PnL from complete, fresh per-bot accounting; two decimals and round sign icons.
- Market SELL: compact header (base volume, number of closed grids, actual PnL indicator), execution deviation indicator and timing.
- Manager status: original bot uptime and independent process uptime.
- Current-version tests validate against metadata and manifest without literal release/engine pins.
- Existing legacy okx_live_upgrade.py is for 0.7.27 → 0.7.28 only. DO NOT use for v0.7.29.
- Package build/push does not update any running bot. Never copy okx_version.py into per-bot engine dependencies.

### Verified source alignment

This release overlays the published v0.7.28 ZIP with SHA256 544006081c4381cf042b82e6a7af98f34552882b30055e08eb87d073ad261661.
The Telegram formatter is the **/root/okx** active import copy, not the stale shared copy.
The observer fix is the VPS-local SHA256 e5684009eb898091f68384fbfaee550c085ac9fd4ad0536fd78975ed727732fd.
### Release gates

1. Run entire original offline suite plus tests/test_v0729_maintenance.py from clean extracted ZIP.
2. Compare all LIVE source hashes; require confirmed /status, uptime and an actual naturally-occurring Market SELL.
3. No trading bot stop/restart, live upgrade, funding, FIFO changes, or order POST.
4. Publish GitHub main + annotated tag v0.7.29 + release only after the checks.
