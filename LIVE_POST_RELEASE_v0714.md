# OKX Grid Bot v0.7.14 — LIVE deployment verification / post-release notes

This document records the *verified operational deployment* after the initial
v0.7.14 GitHub release. It does not modify the immutable release tag.

## Verified on the production VPS (2026-10-19 UTC)

- Four bots running `okx_bot.py` with engine version `0.7.14`:
  `eth079`, `near0713`, `sui0710-2`, `zec0710`.
- Manager: `/usr/local/bin/okxbot` -> `/opt/okx-grid-live/okxbot.py`.
- Engine files and principal runtime module SHA256s match the published bundle.
  Observed engine SHA256: `c2a48d902d6e5b71d86a0a30bd8a8044e5146f2d76244b3fb61adfdbbe8d6d0c`.
- `ExecStart` uses per-instance `okx_bot.py`; `ExecStopPost` uses shared
  `/opt/okx-grid-live/okx_panic_cleanup.py` (no per-instance copy needed).
- Eight systemd observers active (Burst and Execution for all four bots), each
  using `/opt/okx-grid-live/okx_execution_observer.py`.
- Six legacy `20-v0712.conf` observer drop-ins on ETH/SUI/ZEC were removed after
  backing up systemd unit files. NEAR already used the correct new observer units.
- No remaining `20-v0712.conf` observer overrides; no duplicate active observers.
- Latency log rotation is 256 MiB per file.
- No LIVE trading process was restarted during the *observer-only* correction.

## Why observer corrections were needed after the release

Existing v0.7.12 observer services had `ExecStart` overridden by
`/etc/systemd/system/observer-...service.d/20-v0712.conf`. A replacement
base unit alone did not change systemd's effective `ExecStart`. Consequently,
these services continued trying to use the old observer module even though
v0.7.14 `okx_execution_observer.py` was already included in the release.
The deployed observer migration preserved backups and removed these obsolete
specific drop-ins, after checking the unit configuration. Existing operators
must inspect `systemctl show <unit> -p DropInPaths -p ExecStart` before migrating
old services; blindly removing arbitrary drop-ins is unsafe.

## Runtime coverage and deferred items

After the upgrade, journals showed new BUY fills and position reconciliation
for ETH and SUI, correct price-bound WAITING for NEAR, and continuing
accounting/status for ZEC. These journal excerpts alone did not independently
prove a new SELL TP fill after the upgrade.

**Deferred from v0.7.14:** Coverage Recovery alert/throttle changes, shared
account-wide GET positions, multiple bots on the same instrument, multiple
OKX accounts, and changes to `virtual_escape`.

This is an operational record, not a new trading-engine release. The original
v0.7.14 tag and original ZIP asset should remain stable for reproducibility.
