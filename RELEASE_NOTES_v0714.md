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

## Post-release LIVE verification (2026-10-19 UTC)

- Four engines v0.7.14 and the v0.7.14 manager verified on VPS.
- Eight observers v0.7.14 running with versionless paths.
- Six stale v0.7.12 systemd overrides found and removed with backups.
- PANIC cleanup is correctly installed in shared `/opt/okx-grid-live`, not in each bot directory.
- Verified live engine SHA256 `c2a48d902d6e5b71d86a0a30bd8a8044e5146f2d76244b3fb61adfdbbe8d6d0c`.
- GitHub runtime ZIP and tag source cross-checked against installed bundle.

See the [post-release verification report](https://github.com/igorauts/okxbot/blob/main/LIVE_POST_RELEASE_v0714.md).

**Release integrity:** original published tag and ZIP are unchanged.
