# OKX Grid Bot v0.7.4 — Manager and Safety UI

Universal BTC/ETH/SOL Grid Bot with streamlined manager, fixed safety buffer rules and more precise diagnostics.

- **New wizard:** BUY target 1–25 (and <= grid levels); real SELL TP target 1–25. BUY buffer fixed +5; near-BID BUY cancel protection fixed 5 steps; SELL TP temporary buffer = BUY target +5 (range +6 to +30). Config legacy buffer values no longer control runtime safety limits. Names 3–20 chars; upper price may be below current BID if the chosen full grid is mathematically valid.
- **Readable Settings:** actual engine version from systemd (not configuration schema), target/max BUY and SELL TP, grid parameters, bounds, notional capital estimate. Hide margin, fees, contract metadata, LIVE flag, and partial-BUY technical knobs. Old `75 SELL TP` config no longer misrepresented as effective runtime target.
- **Readable Status:** remove duplicate RUN label; process and VPS uptime, current *local* BUY/SELL TP/virtual/partial orders; optional fresh-monitor grid gaps and alerts. Missing data never misreported as zero. Keep `Логи (LIVE)`.
- **Monitoring:** optional per-instance burst and execution monitors; engine now logs `quiet_ms`, `cancel_gap_ms`, `reason` on TP buffer cleanup. The v0.7.4 monitor checks direct engine cooldown evidence, eliminating earlier poll-clock false positives.
- **History / Migration:** carry forward v0.5.9–v0.7.3 CHANGELOG; backup state during 0.7.3→0.7.4 migration. No changes to execution, REST coordination or PnL algorithms beyond fixed parameterization and diagnostics.
- **Deployment safety:** GitHub publication never restarts, stops, or modifies running ETH/SOL bots. No API credentials, local state, SQLite, or private execution logs in release.

**Note:** API-bound LIVE BTC not tested. Status is not an independent exchange audit. Existing running v0.7.3 instances stay on their current engines until migrated separately.
