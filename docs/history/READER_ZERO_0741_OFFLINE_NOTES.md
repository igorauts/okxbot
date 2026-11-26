# Reader authoritative zero — OFFLINE candidate 0.7.41

Based on published 0.7.40; uses previously tested Reader zero stage.
Only core/transport/client, version metadata, regression tests and runner differ.
Trading strategy, intents, order API, REST gate and coalescing are untouched.

A proven empty position requires a completed signed GET for the exact requested
SWAP instruments and authenticated Reader account UID, with a per-client monotonic
freshness barrier. Missing proof, invalid/malformed response or stale/mismatched
scope stays UNKNOWN and cannot authorize a zero. Real OKX proof/production performance
remain **unverified** until controlled LIVE observation.

Old clients reject new empty replies and may continue their direct-GET fallback.
Do not mix old clients indefinitely or assume 429 is resolved by this candidate.

This stage does not install, restart or publish anything. A full LIVE rollout
requires separate approval, backups and GET-only financial reconciliation.
