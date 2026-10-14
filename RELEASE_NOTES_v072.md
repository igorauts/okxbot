# OKX ETH Grid Bot v0.7.2

**LIVE ETH-USDT-SWAP, 14 October 2026.** This release documents the currently deployed engine without modifying trading strategy or existing instances.

## Fix: protect BUY orders near BID

In v0.7.1, at 19:01:50 UTC, BID was around 2430.00. The bot placed a distant BUY at 2417.50 and canceled the nearest BUY at 2430.00 while shrinking its temporary buffer. That removed an entry whose prospective TP was 2432.75.

v0.7.2 changes BUY buffer cleanup:
- Protects BUY orders within **3 grid steps** of fresh BID (±$1.50 for current ETH grid).
- Chooses the most distant obsolete orders for cancellation first.
- Resets the 1.5-second cleanup cooldown when the grid shifts in **either direction**.
- Rechecks current BID before cancellation; if no safe candidate is available, defers the placement rather than evicting near-market BUY.
- Preserves target 25 BUY + temporary buffer up to 30, and 25 real SELL TP + temporary reserve up to 30.
- Retains virtual TP and aggregate reduce-only MARKET escape logic from v0.7.1.

## Verification and compatibility

- 61 offline tests passed (50 inherited + 11 new ETH near-BID regression tests).
- Controlled ETH LIVE migration from v0.7.1 completed; 251 tracked cycles retained, 25 BUY and 25 real TP at start.
- Existing SOL v0.6.10 remains unchanged.
- The first live near-BID protection event and aggregate MARKET escape are **not** yet independently verified. Test success is not a safety guarantee.

## Install / upgrade

See [README_v072_RU.md](README_v072_RU.md) and the package `okx_grid_v072_ETH_final.zip`.

**Important:** Do not replace a live engine without a backup, read-only exchange reconciliation, and controlled systemd switch. Do not publish `.env`, `config.json`, state databases, or logs.
