# OKX Grid Bot v0.7.88 — Dead-code cleanup

Full engine release: Engine and Manager 0.7.88. No intentional change to trading strategy, active financial calculations, state schema, Reader, Funding, or Telegram behavior.

- Remove four unreachable engine functions and unused FULL-cache branches; remove five obsolete cache modules and their unused standalone test.
- Relocate two original Stats v2 prototypes to historical offline fixtures, retaining original regression suites. Pin shared retirement and allow only explicitly pinned dependency removals in the isolated canary.
- Engine files 26 to 24; source bundle 32 to 30; source module inventory 79 to 72; shared inventory 76 to 74. Retain all 38 prior engine manifest records.
- OFFLINE: 1086 test executions passed; independent audit of 108/108 test files, NEW/CLONE, first-start and partial-detach regression passed, as reported by the operator.
- LIVE 2026-11-26 UTC: NES canary, shared runtime, eight sequential engine upgrades (BTC last), and final attestation of nine active bots passed. Seven shared legacy files and 18 per-instance legacy files removed with verified backups; no restarts during cleanup, no archive writes. Reader and Telegram process identities and Funding source unchanged.
- Final LIVE journal audit: 63 TP profit events, 24 verified StatsWorker publications, no matched critical failures or automatic restarts. Earlier NES and ETH HOT gaps recovered; two new pending PnL calculations were present at the final snapshot, not claimed individually verified. No new real PANIC triggered to test terminal Funding.
- Publication changes README and CHANGELOG and adds these notes only relative to the pinned OFFLINE candidate. No LIVE writes, service restarts or full test rerun during publication.
