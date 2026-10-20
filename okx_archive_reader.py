"""Historical bot paths — READ ONLY. Never used by the trading engine.

Never infer missing *new* state from old files: archived legacy modes only.
"""
import json
from pathlib import Path


def _legacy(root):
    root = Path(root)
    try:
        cfg = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        ver = tuple(int(x) for x in cfg['version'].split('.'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Unrecognized archive config; refusing fallback')
    return ver < (0, 7, 18)


def _old_or_new(root, old, new):
    root = Path(root)
    return root / (old if _legacy(root) else new)


def state_file(root):
    return _old_or_new(root, 'state/bot04_state.json', 'state/bot_state.json')


def fills_db(root):
    return _old_or_new(root, 'state/fills_v068.sqlite', 'state/fills.sqlite')


def pnl_db(root):
    return _old_or_new(root, 'state/pnl_v068.sqlite', 'state/pnl.sqlite')


def cycles_db(root):
    return _old_or_new(root, 'state/cycles_v068.sqlite', 'state/cycles.sqlite')


def observer_dir(root, mode):
    if mode not in ('burst', 'execution'):
        raise ValueError('Invalid observer kind')
    return _old_or_new(root, 'diagnostics/' + mode + '_v0711', 'diagnostics/' + mode)
