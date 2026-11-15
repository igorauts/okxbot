"""Pinned source-bundle attestation for new OKX bots. No API or state operations.

The manager may be newer than the engine. Every engine dependency must match
this release's approved SHA256. This module never upgrades a running instance.
"""
import hashlib
import json
import re
from pathlib import Path
from okx_version import RELEASE_VERSION, ENGINE_VERSION, STATE_SCHEMA, validate_version_ids

FILENAME = 'okx_source_bundle.json'
_PATTERN = re.compile(r'[a-zA-Z][a-zA-Z0-9_]*\.py|requirements\.txt|engine_manifest\.json')
_SHA = re.compile(r'[0-9a-f]{64}')


def _read_pinned(path, expected):
    if path.is_symlink() or not path.is_file() or not _SHA.fullmatch(str(expected)):
        raise RuntimeError('SOURCE_PIN_UNSAFE_OR_MISSING: ' + str(path))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError('SOURCE_PIN_MISMATCH: ' + path.name)
    return digest


def verify_bundle(root):
    """Verify coherent manager and bot runtime sources before ANY creation."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('SOURCE_ROOT_UNSAFE')
    path = root / FILENAME
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('SOURCE_BUNDLE_MANIFEST_MISSING')
    raw = json.loads(path.read_text(encoding='utf-8'))
    validate_version_ids()
    if (not isinstance(raw,dict) or raw.get('schema') != 1
            or raw.get('release_version') != RELEASE_VERSION
            or raw.get('engine_version') != ENGINE_VERSION
            or raw.get('state_schema') != STATE_SCHEMA):
        raise RuntimeError('SOURCE_BUNDLE_VERSION_MISMATCH')
    files = raw.get('files')
    deps = raw.get('engine_dependencies')
    if not isinstance(files,dict) or not isinstance(deps,list) or len(set(deps))!=len(deps):
        raise RuntimeError('SOURCE_BUNDLE_SHAPE_INVALID')
    if not all(isinstance(n,str) and _PATTERN.fullmatch(n) for n in files):
        raise RuntimeError('SOURCE_BUNDLE_UNSAFE_FILE')
    if not all(isinstance(n,str) and re.fullmatch(r'[A-Za-z0-9_]+\.py',n) for n in deps):
        raise RuntimeError('SOURCE_BUNDLE_BAD_DEPENDENCIES')
    required = set(deps)|{'okx_bot.py','okxbot.py','okx_setup.py',
                         'okx_version.py','okx_source_attestation.py',
                         'engine_manifest.json','requirements.txt'}
    if set(files) != required:
        raise RuntimeError('SOURCE_BUNDLE_FILE_SET_MISMATCH')
    manifest = json.loads((root/'engine_manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('schema_version') != STATE_SCHEMA or
            manifest.get('current_version') != ENGINE_VERSION or
            manifest.get('engines',{}).get(ENGINE_VERSION,{}).get('file') != 'okx_bot.py'):
        raise RuntimeError('SOURCE_BUNDLE_ENGINE_MANIFEST_MISMATCH')
    entry = manifest['engines'][ENGINE_VERSION]
    if entry.get('dependencies') != deps or entry.get('sha256') != files['okx_bot.py']:
        raise RuntimeError('SOURCE_BUNDLE_ENGINE_DEPENDENCY_MISMATCH')
    for name, digest in files.items():
        _read_pinned(root/name,digest)
    return raw


def verify_instance(root, source):
    """Verify copied engine/dependencies and requirements in a NEW instance."""
    bundle = verify_bundle(source)
    dest = Path(root)
    if dest.is_symlink() or not dest.is_dir():
        raise RuntimeError('NEW_BOT_UNSAFE_DIRECTORY')
    for name in ['okx_bot.py','requirements.txt']+bundle['engine_dependencies']:
        _read_pinned(dest/name,bundle['files'][name])
    return True
