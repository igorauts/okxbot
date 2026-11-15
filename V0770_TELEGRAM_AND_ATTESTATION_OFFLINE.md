# v0.7.70 — Telegram delivery, MARKET multi-leg, source attestation

**Status as packaged:** OFFLINE candidate. **NOT installed LIVE and NOT published on GitHub.**
The currently demonstrated live release is v0.7.69; changing this file's
status later requires actual service/commit/remote ZIP evidence.

## Scope

- Telegram command: durably record the pre-ACKed in-flight update together
  with `updates_offset`. On an ambiguous delivery failure, send no automatic
  command retry. Persist an update-specific, deduplicated informational alert
  in the existing outbox. At startup, reify any orphaned durable receipt into
  the same alert. Telegram Bot API may duplicate an *alert* on ambiguous ACK;
  no exactly-once delivery claims.
- MARKET notifications: verify LIMIT TP + aggregate MARKET legs with actual
  exchange fills, proportional BUY fee and exact parity to accounted net PnL.
  If a cycle cannot be verified, return no profit and emit an informational
  multi-leg limitation rather than a false financial alarm at 30s / 24h.
- Telegram discovery: partial registry/systemd read failure retains previously
  verified bot mappings until independently archived; log partial failure
  with a 300s suppression window. Do not assume missing means shut down.
- Package SHA attestation: `engine_manifest.json` gains hashes of all current
  engine dependencies and of every top-level Python module. New
  `okx_dependency_attestation.py` checks source and optional bot directory;
  **audit-only** — no new automatic startup blocker or mutation.
- Historical v0.7.69 offline candidate note is explicitly identified as a
  development-stage document. Engine remains 0.7.64, as `okx_bot.py` and
  trading mechanics are unchanged; package release becomes 0.7.70.

## LIVE history carried forward (not a new rollout)

- v0.7.69 was published and BARD/BTC/SOPH updated with StatsWorker SHA256
  `a7d83ea29bef676e404260e38b63c5a320906f78f83028734fe4c2a46fa2aef2`.
- The prior Telegram test damaged historical monitor state on 2026-11-14;
  user elected a **fresh observer state** retaining `updates_offset`, with
  old alerts/market queue deliberately skipped, **not reconstructed**.
- Current LIVE source hashes and service PIDs must be read from VPS immediately
  before any v0.7.70 rollout. Do not infer them from these offline notes.

## Exclusions

No changes to order placement, cancellation, fill ingestion, Reader/FIFO,
financial state, StatsWorker v2 or OKX quota parameters. No automatic replays
of failed Telegram commands. No live tests or GitHub publish until explicitly
staged, validated and authorized.
