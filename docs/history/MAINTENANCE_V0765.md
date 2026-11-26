# v0.7.65 — Manager Maintenance

RELEASE_VERSION=0.7.65
RELEASE_KIND=maintenance
ENGINE_VERSION=0.7.64

## Changes
- Fresh-bot and clone cycles archive bootstrap with integrity / no-overwrite guards.
- Observer provisioning before fresh SQLite bootstrap; pristine checks unchanged.
- Manager UI distinguishes stopped new bots from ERROR.
- Account SOFT STOP warning during startup.
- Separate release identity from trading engine version.

## Validation
- LIVE patch installation: PASS (manager + version metadata).
- Offline SQLite / source contract tests at install: PASS.
- Operator manually created and started a new LIVE bot after install.
- No trading engine update or service restart during publication.
- No LIVE tests during publication.
