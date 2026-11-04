# v0.7.56 — TP zero-real rearm P0 (OFFLINE REVIEW ONLY)

Exact source: embedded 190-member v0.7.55 ZIP, SHA256
`89e6c02817ba4596f79b09f698035d90b056844743c9cfb62736d24a5e106958`.

## P0 cause
`maintain_virtual_tp` enforced elective `TP_BUFFER_COOLDOWN=10s` even
while `real < REAL_TP_TARGET` (zero remaining limit TPs included).
During that period it returned `NONE` instead of restoring a real LIMIT TP.

## Exact fix
Only guard elective TP rotation when `real >= REAL_TP_TARGET`.
Under-target real TP restore bypasses the quiet timer; full exchange-side
position/order coverage, price-limit, existing order identity and fail-closed
logic are unchanged. Above-target surplus cancellation still obeys the quiet
interval and hard cap. Emergency virtual-market handling remains ahead of
limit TP replenishment and is intentionally unchanged.

## Isolated test result
6 new scenarios PASS in candidate; source v0.7.55 has 3 expected FAIL
under-target/zero-real, and 3 PASS for existing elective policies.

## Existing tests
All 62 `run_checks.sh` commands PASS as independently isolated invocations;
not claiming one uninterrupted shell script EXIT 0. Exact grouped results
in external log in the ChatGPT workspace.

## Source scope
Only trading AST definition changed: `maintain_virtual_tp`.
Metadata updated: `okx_version.py` and `engine_manifest.json`.
`run_checks.sh` references one new test module.
All other engine functions/classes and dependencies byte-identical.

## LIVE
NO LIVE DEPLOYMENT, NO OKX API, NO SERVICE RESTART, NO GITHUB PUSH.
This ZIP is NOT an installer.
WLD unresolved CANCEL and BLUR unbound attached PENDING are separate P0
incidents requiring fresh exchange proof before mutation.
