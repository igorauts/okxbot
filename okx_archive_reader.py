"""Read-only active and archived paths, chosen by observed files, not release numbers.

No engine imports this module. It never writes files or synthesizes missing state.
Ambiguous historical candidates fail closed rather than selecting by a version.
"""
from pathlib import Path


def _file(root, canonical, historical_glob):
    base=Path(root)
    current=base/canonical
    if current.is_symlink():
        raise ValueError('Unsafe archive symlink')
    if current.is_file():
        return current
    old=sorted(p for p in base.glob(historical_glob) if p.is_file() or p.is_symlink())
    if len(old)>1 or any(p.is_symlink() for p in old):
        raise ValueError('Ambiguous or unsafe archived file')
    return old[0] if old else current


def state_file(root):
    return _file(root,'state/bot_state.json','state/bot*_state.json')


def fills_db(root):
    return _file(root,'state/fills.sqlite','state/fills_*.sqlite')


def pnl_db(root):
    return _file(root,'state/pnl.sqlite','state/pnl_*.sqlite')


def cycles_db(root):
    return _file(root,'state/cycles.sqlite','state/cycles_*.sqlite')


def observer_dir(root,mode):
    if mode not in ('burst','execution'):
        raise ValueError('Invalid observer kind')
    base=Path(root)/'diagnostics'
    current=base/mode
    if current.is_symlink():raise ValueError('Unsafe observer directory')
    if current.is_dir():return current
    old=sorted(p for p in base.glob(mode+'_*') if p.is_dir() or p.is_symlink())
    if len(old)>1 or any(p.is_symlink() for p in old):
        raise ValueError('Ambiguous or unsafe observer archive directory')
    return old[0] if old else current
