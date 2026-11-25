# OKX Grid Bot v0.7.83

- Fix P0: mandatory financial audit on a pristine new or cloned bot consumes the already exchange-validated empty in-memory state instead of requiring a not-yet-created bot_state.json.
- No synthetic persisted state and no bypass of mandatory accounting. Prior trading evidence, orphan financial records and nonempty or inconsistent exchange state remain fail-closed.
- Full NEW/CLONE → AUDIT → START offline regression through live initialization, with external exchange and service boundaries mocked. 999 tests passed on the final offline candidate.
- LIVE: shared runtime updated; BIGTIME started successfully on 0.7.83 and completed 24 trading cycles as observed in the operator's final audit. Seven existing trading bots remain on verified 0.7.82 without restarts. Reader, Telegram and Funding remain unchanged.
- Engine and manager/package: 0.7.83. This release does not require restarting existing trading instances solely for the first-start fix.
- Release repack changes documentation only; full tests were not repeated after that repack.
