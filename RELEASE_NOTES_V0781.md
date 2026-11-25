# OKX Grid Bot v0.7.81

Engine and manager/package: 0.7.81.

- P0: isolate an unresolved attached LIMIT TP after a confirmed canceled, partially filled parent BUY. Preserve its reserved quantity and occupied level. Independent trading is permitted only with fresh reconciliation, position and coverage proof. No timeout-based release, fabricated failure or duplicate SELL. PANIC/HARD retain strict resolution requirements.
- Funding: display last available identity-matched amount or explicitly estimated zero if unavailable. An overdue warning appears strictly after eight hours without complete verification. Missing funding alone does not invalidate otherwise verified totals. Manager and Telegram share the calculation. No funding-driven trading or archived/final PnL changes.
- Telegram: use the embedded atomic financial anchor when available; retain legacy compatibility, compact status and BID truncation. Update help and obsolete manual-trading wording.
- Packaging: source bundle, module and dependency pins, shared installer and final ZIP verification; new/clone paths covered by offline tests.

## Validation and limitations

The pinned integrated archive completed run_checks.sh (973 unittest tests plus standalone checks). The release asset differs only in README, CHANGELOG and these release notes; all other archive entries, including tests, source and manifests, remain byte-identical. The release asset verifier and byte comparison are repeated before publication. The full suite was run on the pre-documentation archive, not rerun for this documentation-only repack.

The operator verified the canary, sequential active-fleet deployment, source attestation, financial startup audits, Telegram reports and scheduled Funding collection. The incident attachment remains unresolved and reserved; isolation is not proof of terminal cancellation. Strict terminal operations can still wait for resolution. Long-term behavior beyond the observed rollout is not guaranteed.

No Reader, order sizing, AMEND, TP/SL or Hot Settings change. Broker tagging and completed-bot Funding are separate tasks. Publication itself does not install code or invoke exchange operations.
