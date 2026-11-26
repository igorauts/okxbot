# OKX Grid Bot v0.7.87 — Terminal Funding Lifecycle

Maintenance release: Manager/package 0.7.87; engine 0.7.85 and all 25 engine dependencies unchanged byte-for-byte. No trading strategy, Reader, or archive-file modifications.

- Persist terminal Funding by attested instance lifetime; archive and Telegram use the same confirmed amount. Restore eligible historical lifetimes without replaying old Telegram messages.
- PANIC flattens independently of Funding. One financial notification: immediate zero-GET path from a covered settlement proof; otherwise targeted collection after the settlement minute, with a 75-second bound from PANIC_DONE and explicit unconfirmed Funding on failure. No later corrective message.
- Include late-posted settlement-minute bills only when attribution is unambiguous. Preserve frozen records and surface later contradictions; HARD excludes funding after termination and does not claim a final total for an open position.
- Keep ordinary hourly :05 UTC collection, silent minute checks and metadata cache. Terminal-only GET does not refresh active snapshots. Changes to credential file metadata can invalidate the fast path. Deploy Collector first, run explicit --mode update and attest active proofs before updating Telegram.
- OFFLINE: 1079 run_checks test executions, 90 Funding tests; independent audit 108/108 test files, new/clone and first-start audit, actual-code settlement and PANIC timeline tests passed.
- LIVE 2026-11-26 UTC: Funding install and explicit GET-only update, shared runtime (76 files, 11 changed), Telegram (11 files), final source and database postflight passed. Eight active trading instances and Reader remained unchanged; only Telegram restarted. Eligible historical Funding records restored; operator confirmed INIT archive and Telegram status/full. No new real PANIC was triggered to test notification; that behavior remains verified on the independent offline stand. Ambiguous or insufficient historical evidence remains unconfirmed.
- Publication changes README, CHANGELOG and adds these notes only relative to the audited candidate. No trading actions, service restarts or full test rerun during publication.
