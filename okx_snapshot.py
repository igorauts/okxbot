#!/usr/bin/env python3
"""Stop-consistent bot snapshot builder.

Not a rollout tool. CALLER must first stop the designated engine, establish
exclusive control over that unit's lifecycle, verify it has no trading Python
process, and retain that exclusion until snapshot finishes. A mere momentary
systemctl inactive check is NOT an exclusive lock against future starts.

Back up config.json, optional bot .env and every file in state/. SQLite DBs
are captured with SQLite's online Backup API (merging any committed WAL), not
as unsafe byte copies. SQLite -wal/-shm sidecars are NOT copied beside a
consolidated backup DB. Backup directory is created atomically and private.

No systemd, exchange, network, or source modification commands in this module.
Do not restore these files over LIVE trading state after subsequent fills.
"""
from __future__ import annotations
import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
from okx_paths import state_file
import shutil
import sqlite3
import tempfile
from urllib.parse import quote

class SnapshotError(RuntimeError):
    pass

DB_SUFFIXES={'.sqlite', '.sqlite3', '.db'}


def _is_db(path: Path) -> bool:
    return path.suffix.lower() in DB_SUFFIXES


def _sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def _read_json(path: Path):
    try:
        doc=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError, OSError) as exc:
        raise SnapshotError('Invalid JSON: '+str(path.name)) from exc
    if not isinstance(doc,dict):
        raise SnapshotError('Non-object JSON: '+str(path.name))
    return doc


def _source_inventory(root: Path, expected_version: str):
    root=Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise SnapshotError('Invalid bot root')
    state=root/'state'
    if state.is_symlink() or not state.is_dir():
        raise SnapshotError('Missing or linked state directory')
    conf=root/'config.json'
    trade_state=state_file(root)
    for p in (conf,trade_state):
        if p.is_symlink() or not p.is_file():
            raise SnapshotError('Missing or linked mandatory snapshot source: '+p.name)
    config=_read_json(conf)
    st=_read_json(trade_state)
    if config.get('version')!=expected_version or st.get('version')!=expected_version:
        raise SnapshotError('Config/state version mismatch; snapshot aborted')

    paths=[conf]
    env=root/'.env'
    if env.is_symlink():
        raise SnapshotError('Linked bot .env is not supported')
    if env.is_file():
        paths.append(env)
    elif env.exists():
        raise SnapshotError('Unexpected bot .env type')
    all_state_files=[]
    for p in sorted(state.rglob('*')):
        if p.is_symlink():
            raise SnapshotError('Symlink in state: '+p.name)
        if p.is_dir():
            continue
        if not p.is_file():
            raise SnapshotError('Unexpected non-file in state: '+p.name)
        all_state_files.append(p)
    state_set=set(all_state_files)
    dbs=[p for p in all_state_files if _is_db(p)]
    if not dbs:
        raise SnapshotError('No SQLite databases found')
    sidecars=set()
    for db in dbs:
        for suffix in ('-wal','-shm'):
            p=Path(str(db)+suffix)
            if p in state_set:
                sidecars.add(p)
    for p in all_state_files:
        if p.name.endswith(('-wal','-shm')) and p not in sidecars:
            raise SnapshotError('Orphan or unexpected SQLite sidecar: '+p.name)
        if p not in sidecars:
            paths.append(p)
    # Hash signatures, not source data, are retained for mutation detection.
    def stat_sig(p):
        st=p.stat()
        if not p.is_file() or p.is_symlink():
            raise SnapshotError('Source changed type: '+p.name)
        return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns
    # SQLite Backup API may checkpoint/refresh WAL and SHM metadata as a
    # consequence of opening/closing connections. Its transaction-level
    # snapshot is coherent; other files still require strict stability.
    signatures={p:stat_sig(p) for p in paths}
    return paths,dbs,signatures,stat_sig


def _copy_private(src: Path, dst: Path) -> None:
    fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as target,src.open('rb') as inp:
        shutil.copyfileobj(inp,target,1024*1024)
        target.flush()
        os.fsync(target.fileno())


def _sqlite_backup(source: Path, destination: Path):
    # Create private destination BEFORE sqlite opens it, irrespective of umask.
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    os.close(fd)
    # URI-escape special characters; readonly permits correct WAL handling.
    uri='file:'+quote(str(source),safe='/')+'?mode=ro'
    try:
        with closing(sqlite3.connect(uri,uri=True,timeout=5)) as src:
            with closing(sqlite3.connect(str(destination),timeout=5)) as dst:
                src.backup(dst,pages=256,sleep=0.05)
                result=dst.execute('PRAGMA integrity_check').fetchone()
                if not result or result[0]!='ok':
                    raise SnapshotError('Backed-up SQLite integrity check failed: '+source.name)
    except sqlite3.DatabaseError as exc:
        raise SnapshotError('SQLite snapshot failed: '+source.name) from exc
    with destination.open('rb') as f:
        os.fsync(f.fileno())


def _sync_dir(path: Path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def capture_stopped_bot(root: Path, destination: Path, *, expected_version: str,
                        stopped_guard):
    """Capture coherent backup only while caller **holds** bot stop exclusion.

    A momentary stopped_guard check alone cannot prevent a concurrent restart.
    Production installer must hold a separate process/lifecycle exclusion lock
    across the entire operation and must NOT merely use a pre/post status check.
    The guard runs before and after, as a second line of defence.
    """
    root,destination=Path(root),Path(destination)
    if not callable(stopped_guard):
        raise SnapshotError('Missing mandatory lifecycle guard')
    if not stopped_guard(root):
        raise SnapshotError('Engine is not confirmed stopped')
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise SnapshotError('Invalid or existing snapshot destination')
    parent=destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise SnapshotError('Snapshot parent missing or linked')
    if parent==root or parent.is_relative_to(root) or root.is_relative_to(parent):
        raise SnapshotError('Snapshot directory must be isolated from bot root')

    files,dbs,signatures,stat_sig=_source_inventory(root,expected_version)
    temp=Path(tempfile.mkdtemp(prefix='.pending-okx-snapshot-',dir=parent))
    os.chmod(temp,0o700)
    committed=False
    try:
        out_files=[]
        for source in files:
            rel=source.relative_to(root)
            dest=temp/rel
            dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            if _is_db(source):
                _sqlite_backup(source,dest)
            else:
                _copy_private(source,dest)
            out_files.append({'file':rel.as_posix(),
                              'size':dest.stat().st_size,
                              'sha256':_sha(dest)})
        # All ordinary source files must stay unchanged. SQLite is copied
        # transactionally via Backup API; WAL/SHM may change during its close.
        now_state = set()
        for path in (root/'state').rglob('*'):
            if path.is_symlink():
                raise SnapshotError('New symlink appeared in state')
            if path.is_file() and not path.name.endswith(('-wal','-shm')):
                now_state.add(path)
        prior_state={path for path in files if path.is_relative_to(root/'state')}
        if now_state!=prior_state:
            raise SnapshotError('Source file set changed during snapshot')
        for path,before in signatures.items():
            if stat_sig(path)!=before:
                raise SnapshotError('Source changed during snapshot: '+path.name)
        if not stopped_guard(root):
            raise SnapshotError('Engine restarted during snapshot')
        manifest={'schema':1,'bot':root.name,'version':expected_version,
                  'sqlite_count':len(dbs),'files':out_files,
                  'source_state_files':len(signatures)}
        man=temp/'snapshot_manifest.json'
        _copy_json_private(man,manifest)
        # Verify every file after writing, before atomic commit.
        for item in out_files:
            p=temp/item['file']
            if p.stat().st_size!=item['size'] or _sha(p)!=item['sha256']:
                raise SnapshotError('Snapshot checksum mismatch: '+item['file'])
        # fsync directory entries bottom-up before atomically publishing.
        for directory in sorted((p for p in temp.rglob('*') if p.is_dir()),
                                key=lambda p:len(p.parts), reverse=True):
            _sync_dir(directory)
        _sync_dir(temp)
        os.replace(temp,destination)
        _sync_dir(parent)
        committed=True
        return manifest
    finally:
        if not committed and temp.exists():
            shutil.rmtree(temp)


def _copy_json_private(path: Path, data):
    raw=(json.dumps(data,sort_keys=True,indent=2)+'\n').encode('utf-8')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
