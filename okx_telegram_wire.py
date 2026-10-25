"""Frozen on-disk Telegram ABI shared with already-running monitors.

The wire names below are historical protocol identifiers, NOT version predicates.
Never rewrite or delete them merely to tidy names. A separate, explicitly
reviewed schema migration is required before any on-disk renaming.
"""
MONITOR_STATE_SCHEMA = 1
PERSISTED_WIRE_KEYS = {'alert_outbox': 'alert_outbox_v0722', 'alert_dead_letters': 'alert_dead_letters_v0725', 'alert_dead_letter_total': 'alert_dead_letter_total_v0725', 'positions_gate_pending': 'positions_gate_pending_v0723', 'positions_gate_episode': 'positions_gate_episode_v0723', 'event_incidents': 'event_incidents_v0722', 'event_incident_counters': 'event_incident_counters_v0722', 'partial_followups': 'partial_followups_v0720', 'observing_since': 'v0720_observing_since', 'panic_seen': 'v0720_panic_seen', 'systemd_watch': 'systemd_watch_v0722', 'engine_heartbeat': 'engine_heartbeat_v0722', 'common_engine_stall': 'common_engine_stall_v0727', 'observers_health': 'observers_health_v0722', 'panic_ack_alarms': 'panic_ack_alarms_v0725'}
HEALTH_RULE_WIRE_ID = 'debounced-v0722-1'

def state_key(semantic):
    return PERSISTED_WIRE_KEYS[semantic]
