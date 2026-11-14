# Telegram test isolation correction (v0.7.68 candidate)

The first v0.7.68 offline regression used synthetic Telegram update_id=500.
In its third-failure branch, `_CommandBridge.commit_completion` called the real
`okx_telegram_delivery.enqueue` with the default LIVE persistent path.
This overwrote the actual Telegram monitor state during an offline stage run
as root. Never run that earlier stage script again.

Corrective controls:

- The synthetic dead-letter test explicitly routes all delivery persistence
  to a private `TemporaryDirectory`, including the third-failure branch.
- `run_checks.sh` refuses uid 0 on the LIVE systemd VPS. Root test runs are
  permitted only in an explicitly opted-in disposable test container without
  mounted LIVE paths; prefer an unprivileged isolated test user.
- The GitHub package release identity advances to v0.7.68, while the
already-installed manager runtime remains at v0.7.67 and the trading engine
remains at v0.7.64. No manager installation is required for this Telegram
maintenance release. The installed Telegram production modules remain byte-for-byte identical
  to the previously LIVE-installed v0.7.68 candidate. No trading changes.
- This repository test fix is not a state-repair instruction; reset the Telegram
  observer state separately, after validating the monitor is stopped, using
  the strict one-shot Telegram-only reset tool.
