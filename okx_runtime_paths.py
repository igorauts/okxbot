#!/usr/bin/env python3
"""Stable 0.7.18+ runtime paths with read-only historical bot compatibility.

Never switches a current bot to stale legacy state when a new file is missing.
Do not use this helper for account-wide FIFO lock paths.
"""
from pathlib import Path
import json

NEW_FROM = (0, 7, 18)

def is_current(root):
    root=Path(root)
    try:
        cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
        ver=cfg.get('version','')
        parts=tuple(int(x) for x in ver.split('.'))
        return len(parts)==3 and parts>=NEW_FROM
    except (OSError,ValueError,TypeError,AttributeError):
        return False

def path(root, legacy, current):
    """Return version-specific path; do NOT fall back on current missing file."""
    root=Path(root)
    return root/(current if is_current(root) else legacy)

def state_file(root):
    return path(root,'state/bot04_state.json','state/bot_state.json')

def events_file(root):
    return path(root,'logs/bot04_events.jsonl','logs/bot_events.jsonl')

def pnl_db(root):
    return path(root,'state/pnl_v068.sqlite','state/pnl.sqlite')

def fills_db(root):
    return path(root,'state/fills_v068.sqlite','state/fills.sqlite')

def cycles_db(root):
    return path(root,'state/cycles_v068.sqlite','state/cycles.sqlite')

def observer_dir(root,mode):
    if mode not in ('execution','burst'):
        raise ValueError('Unknown observer type')
    return path(root,'diagnostics/'+mode+'_v0711','diagnostics/'+mode)
