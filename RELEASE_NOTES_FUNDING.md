# OKX Grid Bot 0.7.79 — Funding Collector

Maintenance release; engine remains 0.7.78.

Account-wide GET-only collector with hourly updates, historical backfill and daily reconciliation. Exact Decimal ledger, deduplication, per-instrument isolation, atomic snapshots and seven daily local backups. Manager adds funding to existing reconciled total PnL and shows age and a colored dot. No trading changes, no engine/Reader/Governor/Telegram changes, no terminal archive changes. No funding-driven TP/SL yet. See [funding/README.md](funding/README.md) for deployment and tests.
