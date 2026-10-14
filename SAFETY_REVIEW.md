# v0.6.10 Safety Review

Scope: v0.6.9 engine -> v0.6.10 engine; no live orders or exchange API writes performed in the build environment.

## What is deterministic

- Confirmed explicit API rejection is not the same as a network timeout.
- POST intent and `expTime` are stored before HTTP I/O; a request that expires before `_http` is never sent.
- Uncertain TP volume is always counted as reserved. Unrelated independently reconciled virtual SELL may be restored ONE at a time.
- Historical v0.6.9 ambiguous records, which have no expiry, are NEVER auto-cleared.
- Three complete, distinct negative REST history snapshots after a 120 s settling period are required before the new expiry-protected BUY/TP can be released.
- An incomplete paginated history or clock skew fails closed.
- No user secrets in the public code, no Telegram credentials in bot logs.

## Limitations

- OKX `expTime` guarantees late request rejection, NOT retrospective confirmation that an earlier request was never accepted. History consistency is an operational assumption: if history returns an incorrect/incomplete full response, a multi-pass absence decision can be wrong. The release deliberately waits and requires repeated checks, but this is not a formal proof.
- In case of prolonged exchange API failure, the bot blocks new BUY and continues only independently verifiable protection. Critical Telegram alerts require an authorized token/chat and the separate monitor service.
- Margin is assessed by the exchange; no predictive `mgnRatio` strategy was added.
- All checks so far are mocked/offline, not live exercise of forced API errors.
