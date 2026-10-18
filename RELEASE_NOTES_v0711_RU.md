## OKX Grid Bot v0.7.11 — BUY priority, faster reconciliation & latency telemetry

- **Reconciliation:** 150 / 300 / 500+ ms retry after incomplete checks; full `orders-pending → order identity/TP coverage → positions` safety validation preserved. No blind order retries.
- **BUY refill:** new BUY orders take precedence over optional at-baseline real-TP rotation; necessary protective TP handling and emergency modes unchanged.
- **Account positions gate RC2:** release `flock` while waiting for the shared 8 GET / 2.1s quota; reacquire and recheck quota atomically. Does not increase OKX request limits.
- **RC3 telemetry fix:** local asynchronous latency journal + P50/P95/P99 report; separate lock vs quota waits. The telemetry allowlist now records both new phases.
- **142/142 offline tests** (45+13+22+19+43). LIVE: four bots ETH079, ZEC0710, SUI0710-2, SOL0711 migrated to v0.7.11 RC3; post-migration audit PASS for active services, both observers, SQLite integrity, BUY 5/5, and zero locally unaccounted cycles.
- **Known limitations:** `ACCOUNT_POSITIONS_GATE_DEGRADED` / `TimeoutError` was observed during rollout; the per-process fallback does not coordinate across all bots. Delayed background `/fills-history` has not yet been optimized. Avoid representing v0.7.11 as a complete fix for API quota contention.
- **Deployment:** release does not change running trading bots. Legacy `install.sh` is not a v0.7.11 installer; do not deploy with it.
