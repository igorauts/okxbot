# OKX Grid Bot v0.7.14

Safety and lifecycle release, based on v0.7.13.

- Automatic, synchronized recovery of corrupted shared FIFO quotas and backward clock drift.
- Controlled emergency HARD/PANIC position-read path for FIFO storage failure.
- Versionless runtime filenames with version recorded inside engine and manifest.
- Dynamic single-account bot discovery and account SOFT reset.
- Automatically provision Burst and Execution observers for new bots.
- Manager, wizard, UI and historical service compatibility fixes.
- 256 MiB latency log rotation; snapshot and guarded rollout support.

Deferred: Coverage Recovery changes; shared account-wide GET positions; multi-account support.

LIVE: Four instances migrated independently; read runtime journal/diagnostics for exchange reconciliation.
