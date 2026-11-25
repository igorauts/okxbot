# ENSO P0 single-instance candidate (OFFLINE)

This candidate implements bounded attached LIMIT TP reservation for a canceled
partial BUY. The attachment remains PENDING, not FAILED, and the level remains
occupied. A full fresh independent reconciliation is required on every pass;
no timeout, missing order, or synthetic failCode releases the reservation.
Strict PANIC/HARD and default audit remain strict. An unresolved attachment can
still prevent terminal completion. The explicit single-instance audit requires
both the isolated coverage proof and the existing full financial verification.

The explicit `--single-instance` installer verifies the pinned ZIP, entire
source manifest, baseline engine and all baseline dependency hashes; it rejects
other changed runtime dependencies. It does not install shared runtime, Reader,
Telegram or Funding. It stops only the selected unit (exchange orders remain),
backs up local code/state/config, replaces engine/version and version metadata,
and runs the GET-only exchange and financial audit. Only successful completion
permits one start. On audit failure, leave the service stopped and retain the
candidate plus current state and the backup; do not roll back newly observed
exchange facts. No automated restart or fleet rollout.

Test fixture: ENSO 621 position, 611 virtual, 8 active SELL and 2 reserved;
normal cycle with partial SELL remainder 3 remains unchanged. No hardcoded bot
ID, amount or price in engine or installer. Offline tests use mocks; LIVE
orders, API and actual service startup are not validated offline.
