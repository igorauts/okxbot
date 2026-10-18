#!/usr/bin/env python3
"""Persistent systemd start-fence building blocks for OKX v0.7.14 rollouts.

*** NO LIVE apply CLI. ***

Safe operator protocol, to be implemented in a separately audited installer:
  1. Preflight all active instances and unit overrides; select ONE bot.
  2. Install one validated [Unit] drop-in; daemon-reload; prove it is loaded.
  3. Durably create its .hold marker (without replacing any existing marker).
  4. Stop the bot; wait for unit inactive, PID=0 and no matching engine PIDs.
  5. Snapshot state/config/SQLite under exclusive installer lock.
  6. Migrate config+state and engine/unit while service is fenced.
  7. Verify files and systemd settings, then remove hold BEFORE start.
  8. Start, reconcile OKX positions/orders/TP; verify new engine and observers.
  9. On any ambiguity, fail closed. Never overwrite progressed trade state.

The marker persists across VPS reboot: maintenance can cause missed virtual
TPs. Operators must restore the bot promptly after safe inspection.
The drop-in persists for future upgrades; no permanent marker means start allowed.
A [Unit] ConditionPathExists guard skips starts when hold exists. The unit
remains loadable and its service's existing ExecStopPost is not overridden.

This file NEVER imports systemctl and cannot itself stop/start trading.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat

SERVICE_RE = re.compile(r'okx-grid-[a-z][a-z0-9-]{0,63}\Z')
DEFAULT_HOLD_DIR = Path('/var/lib/okxbot/upgrade-fences')
DROPIN_BASENAME = '90-okx-v0714-start-fence.conf'

class FenceError(RuntimeError):
    pass


def checked_service(service:str) -> str:
    if not isinstance(service,str) or SERVICE_RE.fullmatch(service) is None:
        raise FenceError('Invalid OKX trading service')
    return service


def hold_path(service:str, hold_dir:Path=DEFAULT_HOLD_DIR) -> Path:
    service=checked_service(service)
    hold_dir=Path(hold_dir)
    if not hold_dir.is_absolute() or not hold_dir.parts or '..' in hold_dir.parts:
        raise FenceError('Hold directory must be absolute, canonical and private')
    if any(ch.isspace() for ch in str(hold_dir)) or not str(hold_dir).isascii():
        raise FenceError('Unsafe hold directory')
    return hold_dir/(service+'.hold')


def dropin_text(service:str, hold_dir:Path=DEFAULT_HOLD_DIR) -> str:
    marker=hold_path(service,hold_dir)
    return ('[Unit]\n'
            '# Prevent trading service start while this bot is under upgrade.\n'
            '# Marker is persistent across reboot; never remove automatically.\n'
            f'ConditionPathExists=!{marker}\n')


def require_exact_dropin(text:str, service:str, hold_dir:Path=DEFAULT_HOLD_DIR):
    expected=dropin_text(service,hold_dir)
    if text!=expected:
        raise FenceError('Start-fence drop-in content differs from expected')
    return True


def check_private_directory(directory:Path) -> None:
    directory=Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise FenceError('Fence directory missing or linked')
    st=os.lstat(directory)
    if st.st_mode & 0o077:
        raise FenceError('Fence directory must be mode 0700')
    if os.geteuid()==0 and st.st_uid!=0:
        raise FenceError('Fence directory must be root-owned')
    # Avoid a symlinked immediate parent of the hold directory.
    if directory.parent.is_symlink():
        raise FenceError('Symlinked fence parent')


def _fsync_dir(directory:Path) -> None:
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def create_durable_hold(service:str, hold_dir:Path, *, token:str) -> Path:
    """Create only; cannot overwrite. Installer must first prove drop-in loaded.

    This function is used by OFFLINE fixture tests only in Stage14. Future
    LIVE installer MUST enforce its own lifecycle/systemd verification first.
    """
    check_private_directory(hold_dir)
    path=hold_path(service,hold_dir)
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}',token):
        raise FenceError('Unsafe/weak rollout token')
    payload=(json.dumps({'schema':1,'service':service,'token':token},sort_keys=True)+'\n').encode()
    flags=os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW|os.O_CLOEXEC
    fd=os.open(path,flags,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_dir(hold_dir)
    except BaseException:
        # Preserve marker if fsync failed: human must investigate ambiguous durability.
        raise
    return path


def read_hold(service:str,hold_dir:Path) -> dict:
    check_private_directory(hold_dir)
    p=hold_path(service,hold_dir)
    if p.is_symlink() or not p.is_file():
        raise FenceError('No regular hold marker')
    st=os.lstat(p)
    if st.st_nlink!=1 or stat.S_IMODE(st.st_mode)!=0o600:
        raise FenceError('Unsafe hold metadata')
    data=json.loads(p.read_text(encoding='utf-8'))
    if (not isinstance(data,dict) or data.get('schema')!=1
            or data.get('service')!=service or not isinstance(data.get('token'),str)):
        raise FenceError('Hold marker differs from expected schema')
    return data


def check_before_releasing_hold(service:str,hold_dir:Path,*, token:str, approved:bool,
                                engine_verified:bool, unit_verified:bool,
                                snapshot_verified:bool):
    """Validate guard only; does NOT delete hold. Live release requires operator approval.

    The installer must release a marker only during the controlled START stage,
    not as a generic exception handler/finally block.
    """
    if not (approved and engine_verified and unit_verified and snapshot_verified):
        raise FenceError('Upgrade prerequisites or explicit authorization missing')
    data=read_hold(service,hold_dir)
    if data['token']!=token:
        raise FenceError('Mismatched rollout token')
    return True


def condition_allows_start(dropin:str,service:str,hold_dir:Path,*,exists:bool) -> bool:
    """Fixture-only model of exact ConditionPathExists=!... semantics."""
    require_exact_dropin(dropin,service,hold_dir)
    return not exists


def planned_dropin_location(systemd_root:Path,service:str)->Path:
    """Compute, do not write. Future installer must check existing overrides."""
    return Path(systemd_root)/(checked_service(service)+'.service.d')/DROPIN_BASENAME
