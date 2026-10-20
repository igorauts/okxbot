"""Active bot paths. No historical-version fallbacks."""
from pathlib import Path


def state_file(root):
    return Path(root) / 'state' / 'bot_state.json'


def events_file(root):
    return Path(root) / 'logs' / 'bot_events.jsonl'


def pnl_db(root):
    return Path(root) / 'state' / 'pnl.sqlite'


def fills_db(root):
    return Path(root) / 'state' / 'fills.sqlite'


def cycles_db(root):
    return Path(root) / 'state' / 'cycles.sqlite'


def observer_dir(root, mode):
    if mode not in ('burst', 'execution'):
        raise ValueError('Invalid observer kind')
    return Path(root) / 'diagnostics' / mode
