# OKX Grid Bot v0.7.3 — universal BTC / ETH / SOL

**Public release: source code and optional diagnostics. Does NOT change live bot processes.**

## Trading engine

- Unified `okx_bot073.py` for BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP (LONG/CROSS, post-only BUY, attached LIMIT TP).
- Temporary BUY buffer beyond base target (default 25+5). Distinct near-BID cancellation protection (default 3 grid steps, asset-appropriate scale).
- Temporary attached real SELL TP burst capacity (default 25+30). 1.5s cooldown, single cancellation per verified pass no faster than 100ms, farthest SELL first, preserve closest baseline.
- Retains virtual TP, aggregate reduce-only MARKET emergency exit, fee/PnL accounting, state migration and fail-closed exchange reconciliation from prior engine.
- Strict envelopes: <=30 BUY, <=55 real TP target+buffer; actual exchange attached orders may transiently exceed configured threshold.

## Multi-bot CLI and read-only monitoring

- New instance wizard selects BTC/ETH/SOL and validates OKX contract properties, fee schedule, lot/tick sizes, trading grid parameters, and independent BUY/TP buffer settings.
- Manager keeps independent lifecycle and statistics. Adds per-instance `okxbot monitor NAME burst|execution on|off|status|report`, plus interactive ON/OFF switches.
- Optional standalone read-only observer processes for each selected instance. No OKX API keys, no order writes, no market actions. Monitoring OFF by default.

## Validation and known limits

- 74 inherited engine regressions + 10 multi-asset tests + 16 observer tests + 4 packaging/manager integration tests = **104 offline tests passed** in local build environment.
- Live ETH and SOL have been operating on asset-specific v0.7.3 ports. Universal merged file is validated OFFLINE; BTC has **no claimed LIVE validation**.
- Monitoring detects internal violations and delays but cannot independently confirm fills/fees/positions on exchange.
- No automatic conversion of already-running instances. Use backup and fresh read-only OKX audit before a controlled LIVE upgrade.

See [Russian documentation](README_v073_UNIVERSAL_RU.md).
