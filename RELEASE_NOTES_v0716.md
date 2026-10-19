# OKX Grid Bot v0.7.16 — LIVE DD, Telegram and version alignment

Published from the **already installed and operator-verified LIVE** Python sources.

- Unified active engine, manager, manifest and Telegram status on v0.7.16.
  Archived terminal bots retain their genuine historical versions.
- Simplified drawdown telemetry: every five seconds, use locally held realized
  PnL, open BUY cycles and cached BID; store the minimum observed total PnL.
  DD introduces **zero additional OKX API calls**, fill-history lookups or
  retry queues. Persisted historical max drawdown is retained.
- Versionless DD files: `state/bot_drawdown.json` and
  `state/stats_financial_anchor.json`, with historical DD migrated on LIVE.
- Telegram `/status`: compact mono rows without copy block; upper grid equals
  highest already-filled BUY cycle awaiting TP; BID below it with percentage;
  fresh persisted drawdown; correct display version.
- Telegram `/latency` and `/latency8h`: click-friendly commands, no copy block;
  P50/P95/P99/MAX, FIFO, BUY POST, REST, reconciliation, burst, deficits.
- Telegram `STATE MISMATCH`: one alert per incident; `BACK TO NORMAL` follows
  multiple verified matching state/engine observations (not silence).
- CLI manager: active/archived menu and correct executable permissions;
  retired bot01/bot02 instances were removed and sanitized old Python sources
  were archived in GitHub main before this release.
- Existing grid strategy, account-wide FIFO, Coverage Recovery,
  virtual_escape, ownership logic and order safety guards are unchanged.

**Operational scope:** the active LIVE ZEC instance was migrated and tested.
Other archived bot instances are **not rewritten or restarted**. Some
historically suffixed SQLite, telemetry and compatibility filenames remain;
this release does not claim their migration. Funding is out of scope.

**Deployment note:** GitHub publication does not install or restart any LIVE
service. The source archive mirrors source files installed on the VPS;
config, state, `.env`, API keys, SQLite and logs are excluded.
