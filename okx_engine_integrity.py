"""Read-only engine identity attestation from a pinned release manifest.

A version string alone is never sufficient to identify an engine. This module
accepts an already-running legacy engine only with EXACT release-byte digest,
and current engine only with its own released digest; no numeric ranges.
"""
import hashlib
import json
from pathlib import Path

from okx_version import STATE_SCHEMA


def attested_engine(root, config, state=None, *, manifest_path=None):
    try:
        root=Path(root)
        engine=root/'okx_bot.py'
        manifest=Path(manifest_path) if manifest_path else Path(__file__).with_name('engine_manifest.json')
        if engine.is_symlink() or not engine.is_file() or manifest.is_symlink():
            return False
        cfg_version=config.get('version')
        if not isinstance(cfg_version,str):return False
        if state is not None:
            if (not isinstance(state,dict) or state.get('version')!=cfg_version
                or state.get('instrument')!=config.get('instrument')
                or state.get('schema_version')!=STATE_SCHEMA):
                return False
        record=json.loads(manifest.read_text(encoding='utf-8'))
        if record.get('schema_version')!=1:return False
        entry=record.get('engines',{}).get(cfg_version)
        digest=entry.get('sha256') if isinstance(entry,dict) else None
        if not isinstance(digest,str) or len(digest)!=64 or entry.get('file')!='okx_bot.py':
            return False
        return hashlib.sha256(engine.read_bytes()).hexdigest()==digest
    except (OSError,TypeError,ValueError,KeyError):
        return False
