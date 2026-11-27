"""Explicit, hash-attested installation inventory. Never performs deployment."""
import hashlib
import json
import re
from pathlib import Path

FILE = 'install_manifest.json'
ROOT_FILE = re.compile(r'[A-Za-z][A-Za-z0-9_]*\.py|engine_manifest\.json|requirements\.txt|okx_source_bundle\.json|install_manifest\.json')
SHA = re.compile(r'[0-9a-f]{64}')
EXTRA_RETIRED_NAMES = frozenset({'BUNDLE_SHA256.json','run_checks.sh'})

def regular(root, name):
    root=Path(root)
    p=root/name
    if root.is_symlink() or p.is_symlink() or not p.is_file() or p.resolve()!=root.resolve()/name:
        raise RuntimeError('Unsafe or missing install source: '+name)
    return p

def load(root, engine_manifest, *, installed=False):
    root=Path(root)
    raw=regular(root,FILE).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=engine_manifest.get('install_manifest_sha256'):
        raise RuntimeError('Install manifest digest mismatch')
    m=json.loads(raw)
    current=engine_manifest['current_version']
    entry=engine_manifest['engines'][current]
    pins=engine_manifest['source_modules_sha256']
    if m.get('schema')!=1 or m.get('engine_version')!=current or not re.fullmatch(r'\d+\.\d+\.\d+',str(m.get('release_version'))):
        raise RuntimeError('Install manifest identity mismatch')
    def names(key):
        v=m.get(key)
        if not isinstance(v,list) or any(not isinstance(n,str) or not ROOT_FILE.fullmatch(n) for n in v) or len(v)!=len(set(v)):
            raise RuntimeError('Bad install inventory: '+key)
        return set(v)
    engine=names('engine');shared=names('shared');telegram=names('telegram');source_only=names('source_only')
    required=set(entry['dependencies'])|{entry['file']}
    meta={'engine_manifest.json','requirements.txt','okx_source_bundle.json',FILE}
    if engine!=required or not engine|meta <= shared or not telegram<=shared or not telegram:
        raise RuntimeError('Incomplete install inventory')
    if shared & source_only or (shared-meta)|source_only != set(pins):
        raise RuntimeError('Root source classification mismatch')
    if not all(n.endswith('.py') for n in telegram):raise RuntimeError('Invalid Telegram inventory')
    if not {'okxbot.py','okx_setup.py','okx_source_attestation.py','okx_install_manifest.py','okx_manager_version.py','okx_live_upgrade.py'} <= shared:
        raise RuntimeError('Incomplete manager inventory')
    retired=m.get('retire_shared')
    if not isinstance(retired,dict) or any(
            (not ROOT_FILE.fullmatch(n) and n not in EXTRA_RETIRED_NAMES)
            or n in shared or (n in pins and n not in source_only)
            or not SHA.fullmatch(str(v)) for n,v in retired.items()):
        raise RuntimeError('Invalid retired inventory')
    shadows=m.get('retire_telegram_shadows',{})
    if not isinstance(shadows,dict) or any(
            n not in {'okx_reader_diagnostics.py','okx_per_bot_speed_readonly.py'}
            or n not in shared or n in telegram or not isinstance(digests,list)
            or not digests or len(set(digests))!=len(digests)
            or any(not isinstance(v,str) or not SHA.fullmatch(v) for v in digests)
            for n,digests in shadows.items()):
        raise RuntimeError('Invalid Telegram shadow retirement inventory')
    component_pins=m.get('component_sha256')
    if not isinstance(component_pins,dict):raise RuntimeError('Missing component pins')
    component_sources=set()
    for key in ('reader','funding'):
        mapping=m.get(key)
        if not isinstance(mapping,dict) or not mapping:raise RuntimeError('Missing component: '+key)
        for dest,src in mapping.items():
            if not isinstance(dest,str) or not dest.endswith('.py') or not ROOT_FILE.fullmatch(dest) or src not in (dest,'funding/'+dest):
                raise RuntimeError('Unsafe component mapping: '+key)
            component_sources.add(src)
            expected=component_pins.get(src)
            if not isinstance(expected,str) or not SHA.fullmatch(expected):raise RuntimeError('Missing component pin: '+src)
            if not installed or src in shared:
                if hashlib.sha256(regular(root,src).read_bytes()).hexdigest()!=expected:
                    raise RuntimeError('Component source drift: '+src)
    if set(component_pins)!=component_sources:raise RuntimeError('Component pin inventory mismatch')
    for name in shared:regular(root,name)
    if not installed:
        for name in source_only:regular(root,name)
        for name in m.get('tools',[]):
            if not re.fullmatch(r'tools/[A-Za-z0-9_]+\.py',name):raise RuntimeError('Unsafe tool path')
            regular(root,name)
    return m
