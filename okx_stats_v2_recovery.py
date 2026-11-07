"""Crash-recoverable quarantine of *derived* StatsWorker-v2 SQLite sidecar.

No trading operations, no OKX REST, and no historical FULL oracle.  Only the
StatsWorker thread invokes these functions after closing its sidecar handle.
The original sidecar is retained as an immutable evidence file; all primary
fills, PnL and cycles ledgers are left intact.

The durable PREPARED marker precedes rename.  At next worker startup, an
interrupted PREPARED operation is either finalized (when the original inode is
unambiguous) or refused without touching source data.  Successful publication
marks the rebuild COMPLETED, allowing a *later* incident after the cooldown.
"""
from __future__ import annotations
import json
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

REBUILD_COOLDOWN_SECONDS = 6 * 3600
REBUILD_TRIGGER = 'LATE_FILL_FROZEN_ORDER'
RECOVERY_SCHEMA = 2
# Soft in-process headroom guard; strict per-process address-space limits are
# verified only in OFFLINE stress tests, never imposed on the trading engine.
MIN_REBUILD_MEM_AVAILABLE_BYTES = 384 * 1024 * 1024
MAX_REBUILD_WALL_SECONDS = 10 * 60
PHASE_PREPARED = 'PREPARED'
PHASE_QUARANTINED = 'QUARANTINED'
PHASE_COMPLETED = 'COMPLETED'

class RebuildRefused(RuntimeError):
    pass


def _paths(root):
    state = Path(root) / 'state'
    if state.is_symlink() or not state.is_dir():
        raise RebuildRefused('Recovery state dir missing or symlinked')
    return state, state / 'stats_v2.sqlite', state / 'stats_v2_recovery.json'


def _safe_regular(p, *, optional=False):
    if p.is_symlink():
        raise RebuildRefused('Recovery symlink refused: ' + p.name)
    if not p.exists():
        if optional:
            return None
        raise RebuildRefused('Recovery file missing: ' + p.name)
    if not p.is_file():
        raise RebuildRefused('Recovery nonregular file: ' + p.name)
    meta = p.stat()
    if meta.st_nlink != 1:
        raise RebuildRefused('Recovery hardlink refused: ' + p.name)
    return meta


def _read_marker(marker):
    if _safe_regular(marker, optional=True) is None:
        return None
    try:
        record = json.loads(marker.read_text(encoding='utf-8'))
    except (ValueError, OSError, UnicodeError) as exc:
        raise RebuildRefused('Invalid recovery marker') from exc
    if (not isinstance(record, dict) or record.get('schema') != RECOVERY_SCHEMA
        or record.get('trigger') != REBUILD_TRIGGER
        or record.get('phase') not in (PHASE_PREPARED, PHASE_QUARANTINED, PHASE_COMPLETED)):
        raise RebuildRefused('Foreign recovery marker')
    when = record.get('started_at_unix')
    if type(when) not in (int, float) or not 0 < float(when) < 10**11:
        raise RebuildRefused('Invalid recovery timestamp')
    backup_name = record.get('quarantine')
    if (not isinstance(backup_name, str) or not backup_name.startswith('stats_v2.quarantine.')
        or not backup_name.endswith('.sqlite') or '/' in backup_name or '\\' in backup_name):
        raise RebuildRefused('Invalid recovery backup name')
    check = record.get('original_sha256')
    if not isinstance(check,str) or len(check)!=64 or any(c not in '0123456789abcdef' for c in check):
        raise RebuildRefused('Missing original sidecar checksum')
    for field in ('original_device', 'original_inode'):
        value = record.get(field)
        if type(value) is not int or value < 1:
            raise RebuildRefused('Missing original sidecar inode')
    return record


def _original(meta, record):
    return (meta is not None and meta.st_dev == record['original_device']
            and meta.st_ino == record['original_inode'])


def _fingerprint(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for piece in iter(lambda:f.read(1024*1024), b''):
            h.update(piece)
    return h.hexdigest()


def _no_sqlite_journal(canonical):
    for suffix in ('-wal', '-shm', '-journal'):
        suffix_path = Path(str(canonical) + suffix)
        if suffix_path.exists() or suffix_path.is_symlink():
            raise RebuildRefused('Sidecar SQLite WAL/SHM/journal still present')


def available_memory():
    """Linux MemAvailable in bytes; missing/malformed telemetry fails closed."""
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                parts=line.split()
                if len(parts)==3 and parts[2]=='kB' and parts[1].isdigit():
                    return int(parts[1])*1024
    except (OSError, UnicodeError):
        pass
    return None


def sufficient_headroom():
    available=available_memory()
    return (available is not None and available>=MIN_REBUILD_MEM_AVAILABLE_BYTES)


def _sync_dir(state):
    fd = os.open(state, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reconcile(root, record, *, write_json):
    """Finish interrupted quarantine; call BEFORE reopening canonical sidecar."""
    state, canonical, marker = _paths(root)
    backup = state / record['quarantine']
    backup_meta = _safe_regular(backup, optional=True)
    current_meta = _safe_regular(canonical, optional=True)
    if backup_meta is not None and not _original(backup_meta, record):
        raise RebuildRefused('Quarantine backup inode does not match marker')

    if record['phase'] == PHASE_PREPARED:
        if backup_meta is not None and _fingerprint(backup) != record['original_sha256']:
            raise RebuildRefused('Original sidecar backup content mismatch')
        if backup_meta is None:
            if not _original(current_meta, record):
                raise RebuildRefused('Interrupted quarantine original inode missing')
            _no_sqlite_journal(canonical)
            if _fingerprint(canonical) != record['original_sha256']:
                raise RebuildRefused('Interrupted original sidecar content changed')
            os.replace(canonical, backup)
            _sync_dir(state)
        elif current_meta is not None and _original(current_meta, record):
            raise RebuildRefused('Original sidecar exists at both canonical and quarantine paths')
        next_record = dict(record, phase=PHASE_QUARANTINED)
        write_json(marker, next_record)
        return next_record

    if backup_meta is None and record['phase'] != PHASE_COMPLETED:
        raise RebuildRefused('Incomplete quarantine evidence missing')
    if current_meta is not None and _original(current_meta, record):
        raise RebuildRefused('Original sidecar unexpectedly returned to canonical path')
    # Completed recovery is financially verified. Later operator cleanup of
    # its *derived* backup cannot make an otherwise healthy bot permanently
    # unreportable. PREPARED/QUARANTINED remain strictly fail-closed.
    if record['phase'] == PHASE_COMPLETED and current_meta is None:
        raise RebuildRefused('Completed recovery canonical sidecar missing')
    return record


def resume_interrupted(root: Path, *, write_json):
    """Idempotent startup gate, before Stats-v2 opens its sidecar connection."""
    state, canonical, marker = _paths(root)
    record = _read_marker(marker)
    if record is None:
        return None
    return _reconcile(root, record, write_json=write_json)


def eligible(root: Path, reason: str, now: float | None = None) -> bool:
    now = time.time() if now is None else float(now)
    if not isinstance(reason, str) or not reason.startswith(REBUILD_TRIGGER):
        return False
    _state, _sidecar, marker = _paths(root)
    record = _read_marker(marker)
    if record is None:
        return True
    if float(record['started_at_unix']) > now + 60:
        raise RebuildRefused('Recovery timestamp in future')
    # No second quarantine while the previous one has not yet published.
    if record['phase'] != PHASE_COMPLETED:
        return False
    return now - float(record['started_at_unix']) >= REBUILD_COOLDOWN_SECONDS


def quarantine_sidecar(root: Path, *, write_json, now: float | None = None) -> Path:
    """Write PREPARED first; rename original; make QUARANTINED durable.

    A crash at any of those boundaries is resumed by resume_interrupted() at
    the beginning of the next worker attempt (never by a separate process).
    """
    now = time.time() if now is None else float(now)
    state, canonical, marker = _paths(root)
    if _read_marker(marker) is not None:
        if not eligible(root, REBUILD_TRIGGER, now=now):
            raise RebuildRefused('Previous recovery not complete or within cooldown')
    source = _safe_regular(canonical)
    _no_sqlite_journal(canonical)
    if not sufficient_headroom():
        raise RebuildRefused('Insufficient available RAM for automatic sidecar rebuild')
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = state / ('stats_v2.quarantine.' + stamp + '.sqlite')
    if backup.exists() or backup.is_symlink():
        raise RebuildRefused('Recovery quarantine collision')
    # Need enough space for a new sidecar while retaining forensic evidence.
    disk = os.statvfs(state)
    free = disk.f_bavail * disk.f_frsize
    if free < max(128 * 1024 * 1024, source.st_size * 2):
        raise RebuildRefused('Not enough disk space for safe rebuilt sidecar')
    record = {'schema': RECOVERY_SCHEMA, 'trigger': REBUILD_TRIGGER,
              'started_at_unix': now, 'phase': PHASE_PREPARED,
              'quarantine': backup.name, 'original_device': source.st_dev,
              'original_inode': source.st_ino,
              'original_sha256': _fingerprint(canonical)}
    write_json(marker, record)
    _reconcile(root, record, write_json=write_json)
    return backup


def finish_rebuild(root: Path, *, write_json):
    """Call ONLY after successful verified incremental financial publication."""
    state, canonical, marker = _paths(root)
    record = _read_marker(marker)
    if record is None:
        return False
    record = _reconcile(root, record, write_json=write_json)
    if record['phase'] == PHASE_COMPLETED:
        return False
    _safe_regular(canonical)
    if not _safe_regular(state / record['quarantine']):
        raise RebuildRefused('Rebuild evidence disappeared')
    completed = dict(record, phase=PHASE_COMPLETED,
                     completed_at_unix=time.time())
    write_json(marker, completed)
    return True
