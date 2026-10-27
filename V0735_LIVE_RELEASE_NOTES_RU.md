# v0.7.35 — LIVE rollout verification · 2026-10-27 UTC

## Installed and checked

- `near0721`, `wld0728`, `btc0721`: engine `0.7.35`, separate backup + GET-only financial/TP audit by upgrade utility, fresh engine heartbeat; final snapshot and source-identity checks PASS.
- `zec0724` remains `inactive` and terminal after manual PANIC SELL; `dood0721-2` terminal. No restart or trading control action was performed on these archives.
- Shared runtime: 55 files installed from offline RC2; previous shared files backed up. Trading and Reader PIDs unchanged during shared stage.
- Reader: switched only Reader systemd unit from archived ZEC `.env` to existing account-wide `/root/okx/.okxbot_account.env`, using same exact credentials; local IPC subsequently passed. Existing account quotas and `mixed-fifo` unchanged.
- Watchdog: `okx-position-reader-watchdog.timer` enabled; first and subsequent sampled GET-free IPC checks OK, consecutive failures zero; policy allows bounded Reader-only recovery (3 failures, minimum 10 minutes between restarts, at most 2 per hour). No forced outage test.
- Telegram: updated real module `/root/okx/okx_telegram_health.py`, restarted only the Telegram monitor, startup log and IPC checks PASS; trading and Reader PIDs unchanged.
- 2026-10-27 22:50:02 UTC final exchange GET-only account snapshot: NEAR 33, WLD 980, BTC 0.072; matching bot statistics. Account SOFT blocked: no. The account has another separate 100-contract position, not attributed to these bots.
- WLD and BTC upgrade logs independently reported `AUDIT PASS` and `LIVE VERIFIED`; NEAR had prior successful controlled upgrade and final version+heartbeat checks; final console output doesn't independently repeat NEAR's saved `AUDIT PASS` marker.
- ZIP source engine SHA256 `c0774039572335eba1db2ceec0cfee37055a4a7cc7670c81aa78aacafd81b14d` matches engine manifest. OFFLINE RC2 had 179 regression tests PASS. This public final ZIP modifies only the release documents and adds this LIVE verification note; all runtime/test/script file bytes match the validated RC2 exactly.

## Scope and caveats

- No Funding Collector, Priority FIFO migration, new API keys, subaccounts, DEMO, Telegram trading commands, or strategy changes.
- No guarantee of continuous availability from point-in-time checks. Reader DOWN/RECOVERED/FAILED fault delivery was not intentionally exercised in LIVE.
- Market BID crossing/intent latency must not be presented as first/last actual exchange-fill latency. Historical-only fill diagnostics include only existing verified fills and distinguish missing clock offsets.
- GitHub publication, when performed, is separate from LIVE rollout. It must not restart systemd units or send trading POST.
