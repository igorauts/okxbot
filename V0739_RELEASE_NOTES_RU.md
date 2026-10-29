# OKX Grid Bot v0.7.39 — Virtual MARKET P0 + Priority FIFO

LIVE-verified: 2026-10-29 UTC. Scope intentionally limited to two changes.

## P0 — trading exits and emergency responsiveness
- Fixed private-WS/epoch-change starvation of `virtual_escape` and PANIC/HARD in `live_loop`.
- Existing BUY freshness and uncertain-intent fail-closed checks remain unchanged.
- Adversarial 1/3/10s simulated WS bursts (private events and epoch changes):
  corrected loop reaches virtual escape on first round rather than deferring for the burst.
- Existing trading decision logic, TP grouping, 150ms coalescing, durable intents,
  reduceOnly, exchange order ownership and financial accounting remain unchanged.

## Account-wide Priority FIFO
- All seven LIVE bots upgraded from v0.7.38 to v0.7.39, with per-bot backups,
  GET-only financial audits, and verified fresh engine heartbeat.
- Priority FIFO activated 2026-10-29 00:34:39.296248 UTC; shared Reader uses the
  original single account-wide quota, one shared gate, and priority-aware clients.
- `EMERGENCY`, `EXIT`, `NORMAL`, `BACKGROUND` routing is active; no quota increase.
- Priority-mode diagnostics false `CONFIG BYPASS` classification fixed, monitor-only
  LIVE hotfix verified 2026-10-29 UTC (backup at 00:46:23 UTC).
- All seven bots demonstrated successful recent Reader IPC in account diagnostics.

## Release discipline
- `ENGINE_VERSION = RELEASE_VERSION = 0.7.39`; engine SHA256 `a7a3fc8f12adaf68cab5963f0f2477a26b51d03cc990f6d18c0e17a480a91004`.
- No strategy redesign, WS-heartbeat change, cooldown redesign or StatsWorker change.
- Source release and GitHub publication do NOT alter running bots or Reader services.
- Tested offline; LIVE rollout completed before GitHub Release publication.
