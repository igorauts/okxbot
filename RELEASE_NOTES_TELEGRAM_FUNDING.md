# OKX Grid Bot 0.7.80 — Telegram Funding reports

Maintenance release. Manager/package: **0.7.80**; trading engine: **0.7.78**.

## Changes

- `/status` displays two compact lines per bot: signed total PnL = grid profit + funding + adjusted floating/residual; BID, position, ticker, occupied/max grid, cumulative volume. Grid-profit ordering and existing message-edit behavior are retained. BID is display-only, truncated to five significant digits without scientific notation or rounding up.
- `/full` adds a signed `Funding: +$5.25` line after Floating PnL, incorporates funding in individual and account totals, and updates the existing percentage from 1x. Other fields remain unchanged.
- Uses existing read-only Funding snapshots. Missing funding is not zero. Verified per-bot amounts feed report totals; exchange Equity/Balance are not adjusted again.

## Validation and limits

Targeted status/full/accounting and missing-data contracts, actual Funding loader and 13 BID formatting cases passed in isolated VPS stages. The first LIVE install confirmed unchanged trading and Reader processes. The final status refinement was manually confirmed by the operator. This is a maintenance publication of the verified final source, not a new trading-engine deployment. No full repository test-suite result is claimed for this release. Funding-based trading decisions and TP/SL remain out of scope. No real financial data or credentials are included in the release.

See [README](README.md), [CHANGELOG](CHANGELOG.md) and [Funding Collector documentation](funding/README.md).
