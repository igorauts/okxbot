#!/bin/sh
set -eu
# Contract: offline regression tests must never have root write access to
# /var/lib/okxbot or the live bot directories. Never execute them as root.
if [ "$(id -u)" -eq 0 ]; then
    # Root is allowed ONLY in a disposable isolated test container: NEVER on
    # the LIVE systemd VPS, or while LIVE Telegram/trading state is mounted.
    if [ "${OKX_DISPOSABLE_TEST_CONTAINER:-}" != 'CONFIRMED_ISOLATED' ] || \
       [ "$(cat /proc/1/comm)" = 'systemd' ] || \
       [ -e /root/okx ] || [ -e /etc/okxbot/telegram.env ] || \
       [ -e /var/lib/okxbot/telegram_monitor_state.json ]; then
        echo 'FAIL_CLOSED: tests require unprivileged execution or a disposable container without LIVE paths' >&2
        exit 89
    fi
fi
cd "$(dirname "$0")"
python3 -B tests/test_offline.py
python3 -B tests/test_emergency_hold.py
python3 -B tests/test_telegram_0722.py
python3 -B tests/test_v0723.py
python3 -B tests/test_v0723_final_ui.py
python3 -B -m unittest tests.test_manager_next_maintenance -q
python3 -B -m unittest tests.test_manager_reader_registration_order -q
# v0.7.70: durable pre-ACK, multi-leg market parity and full source SHA audit.
python3 -B -m unittest tests.test_v0770_telegram_contract tests.test_v0770_manifest -q
# Telegram-only FD/market/ACK regression tests.
python3 -B -m unittest tests.test_telegram_fd_market_ack_vnext -q
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
python3 -B -m unittest tests.test_ephemeral_governor_v0790 -v
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
# Batch recovery quantity/identity regressions
python3 -B tests/test_batch_fills_recovery_contract.py

# P0 Reader observability: exact physical HTTP attempts; never exchange IO.
python3 -B -m unittest tests.test_reader_physical_http_telemetry_contract -q

# opt-in quiet audit phase alignment contract; strictly offline
python3 -B -m unittest tests.test_aligned_idle_audit_contract -q

# Monitor maintenance: follower recovery, IO probe, M4 incremental, HTTPS reuse.
python3 -B -m unittest tests.test_telegram_follower_recovery_vnext -q

# B1 shadow eligibility + Telegram full-grid BUY-zero regression (offline)
python3 -B -m unittest tests.test_b1_shadow_full_grid_contract -q

# B1 shadow-only live-loop classification offline contract
python3 -B -m unittest tests.test_b1_wired_shadow_contract -q

# Candidate full regression: finance_snapshot_regression
python3 -B -m unittest tests.test_finance_snapshot_regression -q

# Candidate full regression: p0_trade_exit_liveness
python3 -B -m unittest tests.test_p0_trade_exit_liveness -q

# Candidate full regression: reader_priority_diagnostics
python3 -B -m unittest tests.test_reader_priority_diagnostics -q

# Candidate full regression: b1_b3_active_contract
python3 -B -m unittest tests.test_b1_b3_active_contract -q

# Candidate full regression: b2_b3_integrated_contract
python3 -B -m unittest tests.test_b2_b3_integrated_contract -q

# vnext scoped threshold/B1 contract (offline)
python3 -B -m unittest tests.test_v0747_threshold_and_b1_contract -q

# S1 maintenance: version-neutral offline regressions.
python3 -B -m unittest tests.test_s1_maintenance -q
# Current LIVE StatsWorker must not retain full-history source caches.
python3 -B -m unittest tests.test_stats_low_ram_fallback -q

# RC2 elective cancellation: transport, owner durable intent and 228-step model.
python3 -B -m unittest tests.test_cleanup_transport_rc2 tests.test_cancel_owner_integration_rc2 tests.test_deep_grid_capacity_model_rc2 -q
# Isolated elective-CANCEL reconciliation: remote live with exact reservation,
# absence/identity mismatch fail-closed, no global hold from one known TP.
python3 -B -m unittest tests.test_cancel_isolation_rc3 -q
python3 -B -m unittest tests.test_partial_attached_isolation -q
python3 -B -m unittest tests.test_single_instance_install -q
# Verified exact-ID, bounded same-order CANCEL retry; never duplicate an order.
python3 -B -m unittest tests.test_cancel_retry_rc3 -q

# P0: Restore under-target protective real TP without elective 10s grace.
python3 -B -m unittest tests.test_tp_rearm_without_quiet_wait_p0 -q

# v0.7.57 candidate-only offline tests; NEVER run from the GitHub publisher.
python3 -B -m unittest tests.test_v0757_idle_cancel_socket -q
python3 -B -m unittest tests.test_v0757_latency_discovery -q

# Current observer / Telegram persistent queue / deployment safety contracts.
python3 -B -m unittest tests.test_v0761_maintenance -q

# Telegram event cursor checkpoint: bounded replay + durable alerts and ACK.
python3 -B -m unittest tests.test_b2_offsets_checkpoint -q

# StatsWorker v2 reliability and fail-closed regression (offline, no OKX).
python3 -B -m unittest tests.test_stats_v2_hot_scale -q
python3 -B -m unittest tests.test_stats_v2_reliability -q
python3 -B -m unittest tests.test_stats_v2_recovery_crash -q
python3 -B -m unittest tests.test_stats_v2_telegram_delivery -q
python3 -B -m unittest tests.test_stats_v2_hostile_late_fill -q

# Pinned original FULL oracle and finance regression suites. One legacy
# engine-unchanged assertion is revised to prove only approved archive edits.
python3 -B tests/run_stats_v2_original_suites.py

# Eager terminal archival + legitimate STALLED suppression (offline).
python3 -B -m unittest tests.test_v0764_eager_archive -q

# P0: urgent-only fallback with Reader IPC failure classification
python3 -B -m unittest tests.test_reader_narrow_fallback_contract -q

# GET-only first-transport-failure fast retry; POST and rate-limit invariants.
python3 -B -m unittest tests.test_get_fast_retry_contract -q

# Verify new engine ↔ old Reader fail-closed transitions before Reader rollout.
python3 -B -m unittest tests.test_p0p1_transition_contract -q

# Dynamic manager/engine identity: full vs maintenance; strict SHA source bundle.
python3 -B -m unittest tests.test_manager_release_divergence tests.test_full_release_version_modes -q
# v-next: real missing Reader runtime directory and urgent pacing
python3 -B -m unittest tests.test_reader_missing_runtime_contract -q
# v0.7.77: curses/SSH START confirmation must fail closed on malformed UTF-8.
python3 -B -m unittest tests.test_manager_tty_confirmation -q
python3 -B -m unittest tests.test_engine_history_immutable -q
python3 -B -m unittest tests.test_ui_engine_version_display -q
python3 -B -m unittest tests.test_funding_manager_ui -q
python3 -B -m unittest tests.test_funding_color -q
python3 -B -m unittest tests.test_v0781_audit -q
python3 -B -m unittest tests.test_v0782_audit -q

# Standalone funding collector regression suite (offline).
PYTHONPATH="$PWD:$PWD/funding" python3 -B -m unittest discover -s tests/funding -v

# Full first-start integration, including mandatory accounting and confirmed systemd boundary.
python3 -B -m unittest tests.test_first_start_full_audit -v

# Partial BUY detachment and one-time policy migration (OFFLINE).
python3 -B -m unittest tests.test_partial_detach_v0784 tests.test_partial_detach_transport_policy_v0784 tests.test_v0784_partial_policy_draft -v

# Maintenance install transaction, explicit inventory and standalone exports.
python3 -B -m unittest tests.test_maintenance_install_v0786 -v

# Dead code removal: exact active FULL specialization and pinned retirement.
python3 -B -m unittest tests.test_dead_code_cleanup_v0788 -v

# Unified read-only financial consumers, exact generation and golden values.
python3 -B -m unittest tests.test_r6_maintenance -v
