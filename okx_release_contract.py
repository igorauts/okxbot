"""Version-independent integrity contract for source/package metadata.

No concrete package or engine version is pinned here. The metadata, manifest,
and safety rules must agree on identities, schema, and engine dependency shape.
"""
from pathlib import Path
import json
import importlib.util
import re


def check(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('_okx_contract_version', root/'okx_version.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate_version_ids(), 'package/engine contract invalid'
    manifest = json.loads((root/'engine_manifest.json').read_text(encoding='utf-8'))
    engine = module.ENGINE_VERSION
    assert manifest['current_version'] == engine, 'engine version differs from manifest'
    assert manifest['schema_version'] == module.STATE_SCHEMA, 'schema mismatch'
    registered = manifest['engines'][engine]
    assert registered['file'] == 'okx_bot.py', 'unexpected trading-engine name'
    dependencies = registered['dependencies']
    assert isinstance(dependencies, list) and len(dependencies) == len(set(dependencies))
    assert all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_]+\.py', name)
               and (root/name).is_file() for name in dependencies)
    assert (root/'okx_bot.py').is_file()
    return True
