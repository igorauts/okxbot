# Historical v0.7.46 offline plan — superseded by LIVE v0.7.46 / v0.7.47

**Historical record only.** The checklist and NOT LIVE labels below described the pre-deployment stage. Both v0.7.46 and v0.7.47 have since passed seven-bot LIVE rollout with GET-only financial audits; v0.7.47 is the published source target. This file is not the current deployment guide.

---

# OKX Grid Bot 0.7.46 — OFFLINE candidate (NOT LIVE approved)

Source: exact pinned LIVE v0.7.45 ZIP.  Engine code and Reader service share
this version, but deploying this ZIP requires a SEPARATE reviewed installer.
No trade calls or service changes occur when building this artifact.

## Scope
- B1 ACTIVE: conditional BACKGROUND only on timer RUN after paginated
  orders-pending, durable fill tracking and no fresh protective/event flags.
  Fail-closed on unknown state, stale WS, control change, open intents.
  Historical intent proof is bounded, cached <=30s, invalidated by state saves.
- B2: skip ONE aligned 5s timer boundary when previous completed full
  REST reconcile was less than 1s before that boundary; cap ~6s.
  Private/virtual/grid and protection deadlines still preempt idle waits.
- B3: Reader background-only collection window 50ms from first client.
  New BACKGROUND does not end/extend window. NORMAL/EXIT/EMERGENCY interrupt.
  Disconnect cancels its own pending quota waiter, preserving other clients;
  already-started physical GET cannot be undone.
- B3 client interruption: 50ms receive poll, discard aborted background
  snapshot, repeat full orders-pending -> positions reconciliation. Never use
  outdated position to approve any SELL/BUY. No direct fallback for cancellation.
- Causal timing: private/grid/virtual WS observed perf-counter timestamp
  to next full REST reconciliation start; control-only preempt measured only
  preempt-to-reconcile. No synthetic market event timestamp.
- Three legacy offline contract suites added to run_checks.sh, new unit/integration
  cancellation, grace, protection and B2 boundary tests included.

## Not changed
Trading strategy, NET position ownership, financial reconciliation, panic,
order API/POST semantics, shared quota 8 GET / 2.1s, UID/registry checks,
read-only archives, Telegram commands, or funding collector.

## Separate operational checks before LIVE
1. Review the FULL offline run_checks.sh and all race/fault-injection output.
2. Re-run the 13-mode STOP/PANIC battery and seven-client active/quiet model;
   compare actual GET attempts and event-to-reconcile P95.
3. Verify Reader service rollout and Telegram monitor actual process start
   against file mtime; never assume code-on-disk equals code-in-memory.
4. Verify mixed 0.7.45/0.7.46 account-wide shared priority quotas, rollback,
   no unexplained direct GETs/429/50011, 7/7 financial audits, BTC LAST.
5. No LIVE rollout or GitHub release without separate explicit approval.

Open P1 (not silently merged into this P0 candidate): monitor journalctl -g
polling and incremental StatsWorker. Virtual TP 150ms window separate P0.
