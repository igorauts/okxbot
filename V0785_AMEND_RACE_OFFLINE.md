# v0.7.85 — OFFLINE candidate

Based on exact v0.7.84 engine SHA256 `ffe84de337f7d097aca2912a3a3c8e55a6a42d351cae937d2f9486a0e628b0be`.

## Corrections
- An exact terminal BUY with unresolved AMEND is recorded as an explicitly uncertain attachment, with positive terminal BUY evidence and exact filled quantity. No automatic MARKET against that quantity. Independent RUN/HARD/PANIC requires complete current pending/position coverage. PANIC can liquidate unreserved contracts; never reports DONE while an unresolved quantity remains. Late exact child discovery wins; late correlated positive removal evidence plus compatible terminal snapshot and fresh child lookup can resolve it.
- New terminal-AMEND branch never pages order history. For RUN/HARD/PANIC, exact child lookups during the 10 s diagnostic window are bounded; subsequent checks occur on fresh evidence, position mismatch, mode change or restart, not a periodic history scan. Contradictory identity or unexpected attachment remains fail-closed. If proof never arrives, an operator must resolve the exceptional exposure. Absence or timeout is not exchange proof of non-placement.
- Telegram adds all three critical partial-detach events. It remains read-only and requires a separate monitor reload to activate alerts.
- Compatible target declares resume schema 1. Stop guard permits exact durable open AMEND after 10 s; unresolved BUY/TP CANCELING/PLACING and aggregate MARKET guards remain. Resume audit may isolate only new terminal-AMEND records, not unrelated legacy reserves. No stale-state rollback after resume audit begins. Incompatible downgrade refused by this installer, not a guarantee against manually running historical installers.
- Agreed policy 5 seconds AND 3 steps unchanged; log prior and new parameters on first migration. No special ENSO migration. First-start fix retained.

## Limitations
Offline tests model exchange responses. No new real AMEND was observed at preparation; `attachAlgoOrds=[]` remains required as positive removal evidence. A nonempty row with zeroed price is not assumed to prove removal. Live canary validation and exact reply shape remain required before fleet rollout. This document does not claim LIVE installation, publication or a guaranteed automatic exit from all venue ambiguity.
