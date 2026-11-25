# OKX Grid Bot v0.7.82

Engine and manager/package: 0.7.82.

- Attached TP reservation: durable validated terminal parent proof; full history probe at most once per 60 seconds in stable operation, immediate recheck on contradictory evidence. Fresh pending, position and coverage checks remain mandatory; missing history is not proof of terminal child status.
- Exact known elective cancellations may coexist with isolation; unresolved placement and in-flight cancel transport remain protected. Stable client identities, hold reasons and separate real TP target/reservation accounting; hard caps still include reservation.
- HARD cancels confirmed orders without selling. PANIC may reduce independently proven free volume after confirmed cancellations. Neither declares completion while the reserved child remains unresolved. No synthetic failure or timeout release.
- Reserve visibility in manager and Telegram, funding database availability messaging, packaging and installer fixture compatibility.

## Validation and limitations

The final CANARY_FIXED archive passed run_checks.sh (988 unittest tests plus standalone checks). The published asset changes documentation only, preserving all source, test and manifest bytes. Historical release notes and changelog are preserved. No full suite rerun after documentation-only packaging.

Operator LIVE canary: 1237.4 seconds, history 233 / pending 283 = 0.823 per pass versus 12 in v0.7.81, 4 short holds all CANCEL_TRANSPORT_BUSY, 3 BUY and 3 TP fills, durable canceled parent proof, 2 contracts still reserved. All seven active engines passed sequential deployment and source/financial startup audits. Telegram reports and scheduled Funding collection verified. PANIC/HARD tested offline only; no intentional live emergency close. Unknown child remains reserved; no guarantee of future market behavior.

No Reader, broker, strategy parameters, HTTP policy, TP/SL, Hot Settings, archive accounting or funding settlement changes. Publication does not modify LIVE or call OKX/Telegram APIs.
