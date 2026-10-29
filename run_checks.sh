#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -B tests/test_offline.py
python3 -B tests/test_emergency_hold.py
python3 -B tests/test_telegram_0722.py
python3 -B tests/test_v0723.py
python3 -B tests/test_v0723_final_ui.py
python3 -B tests/test_v0723_fifo_alert.py
python3 -B tests/test_v07231_telegram_start.py
python3 -B tests/test_v0724.py
python3 -B tests/test_v0725.py
python3 -B tests/test_v0726.py

python3 -B tests/test_v0727.py
python3 -B tests/test_v0728.py
python3 -B tests/test_v0729_maintenance.py
python3 -B tests/test_refactor_contract.py
python3 -B tests/test_compatibility_mixed.py
python3 -B tests/test_rollout_guard.py
python3 -B tests/test_final_release_guards.py
python3 -B tests/test_tp_observability.py

python3 -B tests/test_market_message_ui.py
python3 -B tests/test_financial_snapshot_0732.py
python3 -B tests/test_attached_tp_recovery.py
python3 -B tests/test_stats_worker_optimization.py
python3 -B tests/test_telegram_terminal_health.py

# New offline-only reader/manager regressions, no OKX API access.
# Reader tests run as isolated processes to prevent async/multiprocess test
# cleanup from leaking between unrelated modules. No checks are skipped.
python3 -B -m unittest tests.test_position_reader -q
python3 -B -m unittest tests.test_position_reader_integration -q
python3 -B -m unittest tests.test_reader_manager_cap -q
python3 -B -m unittest tests.test_position_priority_rc4 -q
python3 -B -m unittest tests.test_reader_service_archive_rc4 -q
python3 -B -m unittest tests.test_priority_multiprocess_rc4 -q
python3 -B -m unittest tests.test_stats_fills_stages_rc5 -q
python3 -B -m unittest tests.test_reader_resilience_rc6 -q
python3 -B -m unittest tests.test_reader_deadlines_stress -q

# RC8: quota fairness during mixed rollout, direct tickets, breaker and registry.
python3 -B -m unittest tests.test_reader_mixed_rollout_rc8 -q
# RC9: event-specific WS wake and linked GET/intent telemetry (offline).
python3 -B -m unittest tests.test_rc9_ws_wake_trace -q

# WAL concurrency and causal-only reconciliation diagnostics, OFFLINE.
python3 -B -m unittest tests.test_rc10_stats_wal_recon -q

# RC11 P0 blockers: no repeated WS wake in PAUSE/SOFT, strict mixed FIFO.
python3 -B -m unittest tests.test_rc11_wake_latch_mixed_fifo -q

# Shared Reader Telegram watchdog incident/dedup regressions.
python3 -B -m unittest tests.test_reader_telegram_alerts -q

# Repeated TP crossings, terminal/missing service, Reader safety.
python3 -B -m unittest tests.test_v0735_reliability -q

# Account-wide Reader diagnostics + new/clone auto-registration (GET-free).
python3 -B -m unittest tests.test_newbot_reader_autoreg tests.test_reader_diagnostics_vnext -q

# Wake attribution: diagnostic-only, no REST/POST scheduling changes.
python3 -B -m unittest tests.test_reconcile_wake_attribution -q

# Minimal 5-second audit, independent deadlines, control/WS gate (offline).
python3 -B -m unittest tests.test_idle_reconcile_minimal -q

# Offline integration-level fault injection on actual live_loop AST: no exchange access.
python3 -B -m unittest tests.test_live_loop_chaos_rc4 -q
# Exact-scope proof and real Unix IPC regression (mocked provider; no OKX).
python3 -B -m unittest tests.test_reader_authoritative_zero_contract tests.test_reader_zero_ipc_end_to_end -q
# Candidate-only finance/transport regression tests.
python3 -B tests/test_stats_light_contract.py
