# OKX Grid Bot v0.7.85

Includes the unpublished v0.7.84 candidate changes and subsequent independent audit corrections.

- Partial BUY cleanup requires both 5 seconds and 3 upward grid steps. One-time policy migration; NEW/CLONE defaults updated. Persist AMEND intent, confirm attached limit TP removal before canceling the BUY remainder, then use the existing exact-quantity reduce-only MARKET exit. Full-fill races preserve ordinary TP handling.
- Isolate terminal AMEND ambiguity with exact positive parent evidence and confirmed coverage. Continue independent RUN/HARD/PANIC work; never liquidate the unresolved quantity blindly or declare PANIC complete while it remains. No periodic history paging on the new path; bounded diagnostic window, then evidence-driven reconciliation. Operator resolution may be necessary.
- Three critical Telegram detachment alarms, compatible durable-intent upgrade/resume, no stale-state rollback after the resume audit begins. The installer logs the one-time policy migration. No ENSO-specific repair or trading history migration.
- Final pinned offline candidate: 1045 unittest executions, 83 groups, complete successful run_checks.sh. First-start and financial regressions retained.
- LIVE: normal AMEND → CANCEL → MARKET sequence confirmed on SLX with v0.7.84; 0.7.85 canaries subsequently traded normally for 25 minutes. All nine active bots upgraded to 0.7.85, full financial audits and source/policy attestation passed, BTC last. Telegram monitor updated separately; Reader and Funding unchanged. Rare AMEND races tested offline, not reproduced LIVE; exact raw OKX reply shape has not been captured independently.
- Publication repacks documentation only; no code or manifest changes, no repeat of full suite on the documentation-only repack. The old installer console report remains unchanged in this release.
