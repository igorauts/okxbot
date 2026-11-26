# Engine 0.7.40 — finance performance candidate (OFFLINE)

- Missing fills: second SQLite lookup only for missing order IDs.
- Financial-signature-triggered heavy report at most every 300 seconds; 600-second normal interval, failure retry unchanged.
- Archive cycles read-only SQLite. Finance full-history verification stays enabled.
- Preserve LIVE manager Priority FIFO new-bot fix and Telegram monitor batching/incident policy.
- No changes to BUY, SELL, TP cleanup, Reader, account quota or strategy.
- Existing stats instrumentation regression updated for missing-only SQL fetch, with explicit negative test for unrequested rows.
- Reader Telegram regression aligned to existing v4 incident thresholds, including GET/quota, recovery, deduplication, and counter reset; DOWN/FAILED checks unchanged.
- New-bot Priority Reader fixture aligned with LIVE mode/ACK, with Reader health mocked; fail-closed checks retained.
- Reader diagnostics quiet-window test aligned to threshold, recovery and new-episode semantics.
- No LIVE deployment or Github publication by this stage builder.
