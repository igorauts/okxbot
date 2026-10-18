#!/usr/bin/env python3
"""Pure, offline-first migration contracts for OKX v0.7.14.

No trading-side effects. It is **not** a LIVE installer and it never calls
systemctl or exchange APIs. Real application needs a systemd-enforced start
fence (a file lock held only by the installer does not prevent `systemctl start`),
an exclusive maintenance lock, the stopped-bot snapshot and reconciled resume.
"""
from __future__ import annotations
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import re

from okx_rollout_plan import plan_unit_rewrite

PREVIOUS='0.7.12'
TARGET='0.7.14'
LOCK_BASENAME='okx-v0714-deploy.lock'
BOT_RX=re.compile(r'[a-z0-9-]+\Z')

class UpgradeBlocked(RuntimeError):
    pass


def _read(path: Path):
    path=Path(path)
    if not path.is_file() or path.is_symlink():
        raise UpgradeBlocked('Unsafe or absent JSON: '+str(path))
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise UpgradeBlocked('Corrupt JSON: '+str(path)) from exc
    if not isinstance(data,dict):
        raise UpgradeBlocked('Expected JSON object: '+str(path))
    return data


def _check_versions(config: dict, state: dict):
    if config.get('version')!=PREVIOUS or state.get('version')!=PREVIOUS:
        raise UpgradeBlocked('Upgrade requires matching 0.7.12 config and state')
    inst=config.get('instrument')
    if not isinstance(inst,str) or not inst or state.get('instrument')!=inst:
        raise UpgradeBlocked('Config and state instruments disagree')
    if not isinstance(state.get('cycles'),dict):
        raise UpgradeBlocked('State cycles missing or not a dictionary')
    return inst


def planned_state_transition(config:dict,state:dict):
    """Plan minimal version promotion on a FROZEN bot; does not modify inputs.

    The final installer must snapshot first and ensure systemd cannot start
    during the two-file update. Crash/recovery handling is required for LIVE.
    """
    _check_versions(config,state)
    new_config=copy.deepcopy(config)
    new_state=copy.deepcopy(state)
    new_config['version']=TARGET
    new_state['version']=TARGET
    # No cycle/position/order/fee/drawdown edits permitted here.
    return new_config,new_state


def read_only_bot_plan(root:Path, service:str, unit_text:str):
    """Inspect legacy LIVE files without changing them. No process inspection.

    Must be called ONLY after Stage9 live process + systemd checks. This is
    NOT sufficient as an exclusive lifecycle lock or LIVE rollout approval.
    """
    root=Path(root)
    if not root.is_absolute() or root.parent!=Path('/root/okx') or root.is_symlink() or not BOT_RX.fullmatch(root.name):
        raise UpgradeBlocked('Invalid bot root')
    if service!='okx-grid-'+root.name:
        raise UpgradeBlocked('Invalid bot service')
    if not (root/'state').is_dir() or (root/'state').is_symlink():
        raise UpgradeBlocked('Unsafe state path')
    config=_read(root/'config.json')
    state=_read(root/'state/bot04_state.json')
    instrument=_check_versions(config,state)
    try:
        planned=plan_unit_rewrite(unit_text,root,service)
    except Exception as exc:
        raise UpgradeBlocked('Unit rewrite blocked: '+str(exc)) from exc
    return {'bot':root.name,'service':service,'instrument':instrument,
            'from':PREVIOUS,'to':TARGET,
            'state_cycles':len(state['cycles']),
            'changed_unit':planned,
            'snapshot_required':True,
            'lifecycle_start_fence_required':True,
            'exchange_reconciliation_required':True}


@contextmanager
def exclusive_installer_lock(directory:Path):
    """Prevents TWO installers; NOT a systemd start fence.

    The file is never unlinked: preserves inode-based flock coordination.
    Requires a private pre-existing directory controlled by the operator.
    """
    directory=Path(directory)
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o022:
        raise UpgradeBlocked('Unsafe installer lock directory')
    path=directory/LOCK_BASENAME
    flags=os.O_RDWR|os.O_CREAT|os.O_CLOEXEC|os.O_NOFOLLOW
    fd=os.open(path,flags,0o600)
    try:
        if not os.path.isfile(path) or path.is_symlink():
            raise UpgradeBlocked('Unsafe lock file')
        if os.stat(path).st_ino!=os.fstat(fd).st_ino or os.fstat(fd).st_nlink!=1:
            raise UpgradeBlocked('Lock file inode mismatch')
        if os.fstat(fd).st_mode & 0o077:
            raise UpgradeBlocked('Lock file permission too broad')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise UpgradeBlocked('Another rollout holds the lock') from exc
        yield path
    finally:
        try:fcntl.flock(fd,fcntl.LOCK_UN)
        finally:os.close(fd)


def manager_bundle_readiness(bundle:Path):
    """Check stage13 manager executable and integrity before any future install."""
    from okx_rollout_installer import verify_bundle
    meta=verify_bundle(bundle)
    manager=bundle/'okxbot.py'
    mode=manager.stat().st_mode & 0o777
    if mode!=0o700:
        raise UpgradeBlocked('Manager entrypoint must be executable and private (0700)')
    for filename in meta['files']:
        p=bundle/filename
        if p.is_symlink() or not p.is_file():
            raise UpgradeBlocked('Bundle contains unsafe entry')
        expected=0o700 if filename=='okxbot.py' else 0o600
        if p.stat().st_mode & 0o777 !=expected:
            raise UpgradeBlocked('Incorrect bundle mode: '+filename)
    if not manager.read_bytes().startswith(b'#!/usr/bin/env python3'):
        raise UpgradeBlocked('Manager shebang missing')
    return {'release':meta['release'],'manager':str(manager),
            'engine':meta['primary_engine'],'items':len(meta['files'])}
