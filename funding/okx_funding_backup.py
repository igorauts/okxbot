"""Daily local consistent SQLite backup. Caller holds collector flock."""
from contextlib import closing
from datetime import datetime, timezone
import os
import re
from pathlib import Path
import sqlite3
import tempfile
from okx_funding_ledger import SCHEMA, require


def validate(db):
    require(db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA, 'BACKUP_SCHEMA')
    require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'BACKUP_INTEGRITY')
    require(db.execute("SELECT value FROM meta WHERE key='synced_ms'").fetchone() is not None,
            'BACKUP_NO_COMMITTED_GENERATION')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rotate(folder, target, keep=7):
    # Only our dated regular files, never symlinks or arbitrary files.
    copies = []
    for p in folder.iterdir():
        if re.fullmatch(r'funding-[0-9]{4}-[0-9]{2}-[0-9]{2}\.sqlite', p.name) and p.is_file() and not p.is_symlink():
            try:
                datetime.strptime(p.name, 'funding-%Y-%m-%d.sqlite')
            except ValueError:
                continue
            copies.append(p)
    # Never prune a freshly validated target (including after a clock rollback).
    others = sorted((p for p in copies if p != target), reverse=True)
    for p in others[keep-1:]:
        p.unlink()
    sync_dir(folder)


def daily_backup(db_path, *, now=None):
    """One validated copy per UTC day, retain seven daily copies.

    sqlite backup API includes committed WAL data. Atomic publication, fsync,
    no changes to source DB or bot files. All outputs stay beside source DB.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return 'NO_DATABASE'
    require(db_path.is_file() and not db_path.is_symlink(), 'BACKUP_SOURCE_INVALID')
    folder = db_path.parent/'backups'
    folder.mkdir(mode=0o700, exist_ok=True)
    require(folder.is_dir() and not folder.is_symlink(), 'BACKUP_DIR_INVALID')
    sync_dir(db_path.parent)
    now = datetime.now(timezone.utc) if now is None else now
    require(now.tzinfo is not None, 'BACKUP_TIMEZONE')
    target = folder/('funding-'+now.astimezone(timezone.utc).strftime('%Y-%m-%d')+'.sqlite')
    require(not target.is_symlink(), 'BACKUP_TARGET_SYMLINK')
    if target.exists():
        with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as check:
            validate(check)
        rotate(folder,target)
        return 'ALREADY_DONE'
    fd, name = tempfile.mkstemp(prefix='.funding-', suffix='.tmp', dir=folder)
    os.close(fd)
    tmp = Path(name)
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as source, \
             closing(sqlite3.connect(tmp,timeout=1)) as dest:
            validate(source)
            source.backup(dest, pages=128, sleep=0.05)
            require(dest.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete','BACKUP_JOURNAL_MODE')
            validate(dest)
            dest.commit()
        with tmp.open('rb') as f:
            os.fsync(f.fileno())
        os.replace(tmp,target)
        sync_dir(folder)
        rotate(folder,target)
        return 'CREATED'
    finally:
        tmp.unlink(missing_ok=True)
