"""StatsWorker v2 OFFLINE PROTOTYPE: fail-closed two-store checkpoint protocol.

The index and checkpoint are deliberately independent files. There is no
multi-file atomic transaction: a crash between writes MUST cause a FULL rebuild.
No live paths, no trading imports or network access.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

from okx_stats_v2_contract import Drift, json_compact

SCHEMA = 2


def _check_instrument(value):
    if not isinstance(value, str) or not value.endswith('-USDT-SWAP') or len(value) > 120:
        raise ValueError('Invalid stats instrument')


def _atomic_json(path: Path, body: dict):
    """Atomic replace, fsync file + directory, mode 0600. OFFLINE ONLY."""
    path = Path(path)
    if path.is_symlink():
        raise Drift('Refusing symlink checkpoint')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.stats-v2-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json_compact(body) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class FrozenIndex:
    """SQLite index: one order points to at most one verified component.

    The caller is responsible for full financial verification before calling
    add_verified_component. The index alone is NEVER monetary authority.
    """
    def __init__(self, path, instrument):
        self.path = Path(path)
        self.instrument = instrument
        _check_instrument(instrument)
        if self.path.is_symlink():
            raise Drift('Refusing symlink frozen index')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, val TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS frozen_orders ('
                            'order_id TEXT PRIMARY KEY, component_id TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS verified_components ('
                            'component_id TEXT PRIMARY KEY, oracle_sha256 TEXT NOT NULL)')
            with self.db:
                identity = self._meta('identity')
                if identity is None:
                    self._set_meta('identity', str(uuid.uuid4()))
                    self._set_meta('instrument', instrument)
                    self._set_meta('generation', '0')
                elif self._meta('instrument') != instrument:
                    raise Drift('Frozen index belongs to another instrument')
            os.chmod(self.path, 0o600)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _meta(self, key):
        result = self.db.execute('SELECT val FROM meta WHERE key=?', (key,)).fetchone()
        return result[0] if result else None

    def _set_meta(self, key, val):
        self.db.execute('INSERT INTO meta(key,val) VALUES (?,?) '
                        'ON CONFLICT(key) DO UPDATE SET val=excluded.val', (key, str(val)))

    def lookup(self, order_id):
        result = self.db.execute('SELECT component_id FROM frozen_orders WHERE order_id=?',
                                 (str(order_id),)).fetchone()
        return result[0] if result else None

    def detect_late_fill(self, fill: dict) -> str | None:
        oid = str(fill.get('ordId') or '')
        if not oid or not str(fill.get('billId') or ''):
            raise Drift('Missing new fill identity')
        return self.lookup(oid)

    def add_verified_component(self, component_id: str, order_ids, oracle_sha256: str):
        """Call ONLY after existing FULL oracle validates these exact orders."""
        if not component_id or not order_ids or len(oracle_sha256) != 64:
            raise Drift('Component lacks verified oracle evidence')
        try:
            bytes.fromhex(oracle_sha256)
        except ValueError as exc:
            raise Drift('Invalid oracle digest') from exc
        ids = sorted(set(str(x) for x in order_ids))
        if not all(ids) or len(ids) != len(order_ids):
            raise Drift('Duplicate/blank order in component')
        if self.db.execute('SELECT 1 FROM verified_components WHERE component_id=?',
                           (component_id,)).fetchone():
            raise Drift('Component already frozen; refuse double count')
        for order in ids:
            if self.lookup(order) is not None:
                raise Drift('Order already belongs to a frozen component')
        with self.db:
            self.db.execute('INSERT INTO verified_components VALUES (?,?)',
                            (component_id, oracle_sha256))
            self.db.executemany('INSERT INTO frozen_orders VALUES (?,?)',
                                [(order, component_id) for order in ids])
            # Invalidate the prior JSON checkpoint AT THE SAME COMMIT as every
            # frozen-index change. Crash before finalize -> generation mismatch.
            self._set_meta('generation', str(int(self._meta('generation')) + 1))

    def finalize_checkpoint(self, path, cursors, totals, deferred, *, expected_generation=None,
                            inject_crash_after_index_commit=False):
        """Persist index generation first, then atomic JSON. Rebuild on split commit.

        Aggregates/cursors MUST be calculated before invoking this method.
        This prototype only enforces ordering/integrity, not financial parity.
        """
        if not isinstance(totals, dict) or not isinstance(cursors, dict) or not isinstance(deferred, list):
            raise Drift('Invalid checkpoint structure')
        generation = int(self._meta('generation'))
        if expected_generation is not None and generation != expected_generation:
            raise Drift('Stale checkpoint writer')
        identity = self._meta('identity')
        snapshot = {
            'schema': SCHEMA, 'instrument': self.instrument,
            'index_uuid': identity, 'generation': generation + 1,
            'cursors': cursors, 'totals': totals, 'deferred': deferred,
        }
        with self.db:
            self._set_meta('generation', str(generation + 1))
        if inject_crash_after_index_commit:
            raise RuntimeError('Simulated crash after SQLite commit')
        _atomic_json(Path(path), snapshot)
        return snapshot

    def verify_checkpoint(self, path) -> dict:
        checkpoint_path = Path(path)
        if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
            raise Drift('Missing/symlinked checkpoint: FULL rebuild required')
        try:
            payload = json.loads(checkpoint_path.read_text())
        except (OSError, ValueError, UnicodeError) as exc:
            raise Drift('Corrupt checkpoint: FULL rebuild required') from exc
        if (payload.get('schema') != SCHEMA or
            payload.get('instrument') != self.instrument or
            payload.get('index_uuid') != self._meta('identity') or
            payload.get('generation') != int(self._meta('generation'))):
            raise Drift('Index/JSON generation mismatch: FULL rebuild required')
        if not isinstance(payload.get('cursors'), dict) or not isinstance(payload.get('totals'), dict):
            raise Drift('Incomplete checkpoint')
        return payload
