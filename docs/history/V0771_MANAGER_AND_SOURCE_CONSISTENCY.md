# v0.7.71 — manager and source consistency

Maintenance release (engine unchanged).

- LIVE manager moved from v0.7.67 to v0.7.71.
- New/clone provisioning pins 25 engine dependencies by SHA256 and fails closed on mismatch.
- Shared StatsWorker reconciled with the verified v0.7.69 HOT-scale module.
- Telegram v0.7.71 installed source retained; existing v0.7.70 fixes preserved.
- All five LIVE bots source-synchronized to this release; BTC updated last.
- StatsWorker 600/300-second scheduling unchanged, deferred to later work.
- Trading engine v0.7.64 unchanged; trading logic, configs, state and credentials untouched.
- Source deployment had five individual post-restart verified financial publications.
- No tests are executed by the GitHub publisher.
