# v0.7.73 — Reader FIFO diagnostics + 230ms pacing (LIVE)

Maintenance release on top of v0.7.72. The trading engine, manager and order strategy
are unchanged; the engine version intentionally remains as in v0.7.72.

Changes installed on LIVE 2026-11-17 UTC:
- Reader-only FIFO wait attribution diagnostics (quota-full vs queue/flock/fsync).
- Reader pacing: one shared two-permit token bucket, one permit replenished
  every 230 ms. First two independent GET after idle can proceed immediately.
- The pre-existing hard account FIFO gate (9 GET / 2.05s) remains authoritative;
  no position staleness policy, B1, order placement, Virtual TP, account
  permissions, or trading risk limits were changed by this Reader maintenance.
- Verified installed Reader SHA256: `5d9dd0eae49bba478e3a80bd08733e7a9e2804a7654ad0cba4b02f28ae2ff6ef`.

Measured LIVE (different time windows and request mix; not controlled A/B):
- BEFORE 16:45:37–17:02:00 UTC: IPC n=1739, P95=103.9ms, P99=311.5ms,
  max=1346.4ms, >500ms=11.
- AFTER 21:29:06–21:45:06 UTC: IPC n=1764, P95=155.1ms, P99=238.0ms,
  max=474.7ms, >500ms=0.
- NORMAL IPC P95 worsened 98.1ms → 189.1ms; normal P99 improved
  512.1ms → 319.5ms. No blanket latency-improvement claim is made.
- Reader HTTP 429/50011/transport errors=0 in both sampled windows.
- Six LIVE trading services and the Reader remained active at audit time.
- BUY/TP restoration end-to-end regression is not conclusively ruled out.

Source release only, NOT an instruction to restart or redeploy trading engines.
Includes read-only audit and one-shot guarded Reader installer for reproducibility.
Publisher executes NO tests, NO LIVE restarts and NO OKX API calls. Prior
offline synthetic simulation and installation self-test are not equivalent to
full offline regression tests of this exact publication ZIP.
