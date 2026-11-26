# 0.7.84 — OFFLINE candidate only

- RUN partial cleanup: 5 seconds and 3 upward grid steps, same existing age reference and AND semantics.
- One-time `partial_buy_policy_revision=2` for existing configs; NEW/CLONE get 5/3. No changes to Reader, Telegram, Funding, fee or regular grid strategy.
- Exact parent BUY plus original single attached limit TP. Persist `partialDetach` before `POST /api/v5/trade/amend-order`, `cxlOnFail=false`, same `newPx`, `newTpOrdPx=0`, unique `reqId`.
- ACK alone never authorizes CANCEL. Explicit empty attachment on a newer exact open BUY, or correlated successful amendment WS/terminal evidence, authorizes the next step.
- After positive detachment and terminal parent proof, canceled partial BUY becomes virtual, then existing reduceOnly MARKET closes exact remaining quantity. Full quantity already executed follows ordinary TP management. No synthetic attached TP on this confirmed path.
- After 10 seconds of unresolved AMEND, stop additional exact status polling and log CRITICAL once; normal reconciliation/WS still observes the intent. This diagnostic window is not a trade timeout and never treats elapsed time as proof. It is separate from the approved 5-second exit trigger.
- Definite AMEND rejection retains parent/attachment; no blind POST retries. Terminal contradiction remains fail-closed with explicit diagnostic. Do not claim closure in these exceptional cases.
- Existing ENSO reservation intentionally not migrated or auto-closed. Emergency SOFT/HARD/PANIC and historical reserve safety paths preserved.

## API basis
OKX v5 documentation: https://www.okx.com/docs-v5/en/#order-book-trading-trade-post-amend-order ; order amendment result: https://www.okx.com/docs-v5/trick_en/ . The exact live response shape after detaching a partially filled attached LIMIT TP has not been captured. No LIVE or DEMO order was submitted for validation.

## Deployment restrictions
Not approved for fleet rollout or GitHub publication. First validate a normal strict-audit canary (not ENSO) after separate operator approval and observe a naturally triggered partial exit. The special isolated single-instance installer is intentionally not applicable: the API dependency changed. Use normal staged shared runtime + standard strict bot upgrade, with rollback/preflight plan. Existing ENSO must be resolved separately.
