"""Strict offline source ZIP verifier; executable as a standalone tool.

This is a build/publish gate. Nothing touches LIVE services or exchange.
"""
import ast, hashlib, json, stat, sys, zipfile
from pathlib import Path

def allowed_package_member(name):
    """Strict source ZIP layout; Reader deployment recovery templates are data."""
    if (not isinstance(name,str) or not name or name.endswith('/') or
            '\\' in name or '\x00' in name or name.startswith('/')):
        return False
    parts=Path(name).parts
    if '..' in parts or any(x in ('', '.') or x.startswith('.') for x in parts[1:]):
        return False
    if name.startswith('.') and name not in ('.gitignore','.env.example'):
        return False
    if len(parts)==1:return True
    if len(parts)==2 and parts[0] in ('tests','historical'):return True
    if len(parts)==3 and parts[:2]==('tests','legacy') and parts[2] in ('__init__.py','single_transition_upgrader.py','okx_shared_runtime_deploy.py'):
        return True
    if len(parts)==2 and parts[0]=='tools' and parts[1] in (
            'README.md','build_release_zip.py',
            'okx_market_fill_audit.py','okx_reconcile_trigger_report.py',
            'export_component.py'):
        return True
    if len(parts)==3 and parts[:2]==('docs','history') and parts[2].endswith('.md'):
        return True
    if len(parts)==3 and parts[:2]==('tests','funding') and parts[2] in (
            'benchmark_funding.py','f1_bills_fixture.json','test_funding_collector.py',
            'test_funding_f6.py','test_funding_f61.py','test_funding_ledger.py',
            'test_funding_view_backup.py','test_terminal_funding_lifetime.py'):
        return True
    if len(parts)==3 and parts[:2]==('deploy','reader'):
        return True
    # Imported, BYTE-PINNED offline regression fixtures only. These are not
    # deployed into a trading-bot runtime; reject arbitrary nested ZIP paths.
    if name in (
        'deploy/resources/README.md',
        'deploy/resources/collector.py',
        'deploy/resources/http429_audit.py',
        'funding/README.md',
        'funding/okx_funding_backup.py',
        'funding/okx_funding_collector.py',
        'funding/systemd/okx-funding-collector.service',
        'funding/systemd/okx-funding-collector.timer',
        'funding/systemd/paths.conf.example',
    ):return True
    if parts in (('tests','fixtures','okx_bot_original_full_v0760.py.gz'),
                 ('tests','fixtures','okx_bot_v0780_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0781_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0782_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0783_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0784_baseline.py.gz'),
                 ('tests','fixtures','okx_bot_v0785_baseline.py.gz'),
                 ('tests','fixtures','okx_positions_fifo_v0788.py.gz')):
        return True
    if (len(parts)==5 and parts[:2]==('tests','stats_v2_original')
        and parts[2] in ('stats_v2_release_work','stats_v2_prod_candidate')
        and parts[3]=='tests' and parts[4].startswith('test_')
        and parts[4].endswith('.py')):
        return True
    return False


def verify_member_types(z):
    members=z.infolist()
    names=[x.filename for x in members]
    if not members or len(set(names)) != len(names):
        raise ValueError('Empty or duplicated ZIP members')
    for info in members:
        name=info.filename
        if not allowed_package_member(name):
            raise ValueError('Unsafe ZIP path: '+name)
        mode=(info.external_attr>>16)
        if info.create_system!=3 or stat.S_IFMT(mode)!=stat.S_IFREG:
            raise ValueError('Non-regular ZIP entry: '+name)
        if info.file_size > 6_000_000:
            raise ValueError('Oversized ZIP member: '+name)
    if z.testzip() is not None:
        raise ValueError('ZIP CRC mismatch')

def verify(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        verify_member_types(z)
        names=set(z.namelist())
        for name in ('okx_bot.py','okx_live_upgrade.py',
                     'engine_manifest.json','okx_version.py',
                     'run_checks.sh','tests/test_final_release_guards.py'):
            if name not in names:raise ValueError('Missing: '+name)
        # Git/ZIP must preserve CLI executable modes, not merely shebangs.
        for executable in ('okxbot.py','okx_live_upgrade.py','rollout_preflight.py','okx_dependency_attestation.py'):
            if executable not in names or (z.getinfo(executable).external_attr>>16)&0o777!=0o755:
                raise ValueError('CLI executable permission missing: '+executable)
        manifest=json.loads(z.read('engine_manifest.json'))
        version_ast=ast.parse(z.read('okx_version.py').decode())
        fields={}
        for node in version_ast.body:
            if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant):
                for target in node.targets:
                    if isinstance(target,ast.Name): fields[target.id]=node.value.value
        engine=fields.get('ENGINE_VERSION')
        release=fields.get('RELEASE_VERSION')
        kind=fields.get('RELEASE_KIND')
        import re
        if not all(isinstance(v,str) and re.fullmatch(r'\d+\.\d+\.\d+',v) for v in (release,engine)):
            raise ValueError('Only three-component release / engine versions allowed')
        if release!=engine and (kind!='maintenance' or tuple(map(int,release.split('.'))) <= tuple(map(int,engine.split('.')))):
            raise ValueError('Undeclared maintenance release / engine divergence')
        if (engine != manifest.get('current_version') or
              fields.get('STATE_SCHEMA') != manifest.get('schema_version')):
            raise ValueError('Release / engine / schema contract mismatch')
        actual=hashlib.sha256(z.read('okx_bot.py')).hexdigest()
        expected=manifest['engines'][engine]['sha256']
        if actual!=expected:raise ValueError('Engine checksum mismatch')
        # Check source-bundle identity and every pin against the actual ZIP,
        # not merely the engine digest. This also gates maintenance releases.
        manager_fields={}
        for node in ast.parse(z.read('okx_manager_version.py').decode()).body:
            if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant):
                for target in node.targets:
                    if isinstance(target,ast.Name): manager_fields[target.id]=node.value.value
        manager_release=manager_fields.get('RELEASE_VERSION')
        manager_kind=manager_fields.get('RELEASE_KIND')
        if not isinstance(manager_release,str) or not re.fullmatch(r'\d+\.\d+\.\d+',manager_release):
            raise ValueError('Invalid manager release identity')
        if not ((manager_kind=='engine' and manager_release==engine) or
                (manager_kind=='maintenance' and tuple(map(int,manager_release.split('.')))>tuple(map(int,engine.split('.'))))):
            raise ValueError('Invalid manager release mode')
        bundle=json.loads(z.read('okx_source_bundle.json'))
        entry=manifest['engines'][engine]
        deps=entry['dependencies']
        required=set(deps)|{'okx_bot.py','okxbot.py','okx_setup.py','okx_version.py',
                          'okx_manager_version.py','okx_source_attestation.py','engine_manifest.json','requirements.txt'}
        if (bundle.get('schema')!=1 or bundle.get('release_version')!=manager_release or
            bundle.get('engine_version')!=engine or bundle.get('state_schema')!=fields['STATE_SCHEMA'] or
            bundle.get('engine_dependencies')!=deps or set(bundle.get('files',{}))!=required):
            raise ValueError('Source bundle identity/file set mismatch')
        for name,digest in bundle['files'].items():
            if name not in names or hashlib.sha256(z.read(name)).hexdigest()!=digest:
                raise ValueError('Source bundle checksum mismatch: '+name)
        if entry.get('dependency_sha256') and any(entry['dependency_sha256'].get(name)!=bundle['files'][name] for name in deps):
            raise ValueError('Engine dependency pins disagree with source bundle')
        module_pins=manifest.get('source_modules_sha256')
        root_modules={n for n in names if '/' not in n and n.endswith('.py')}
        if not isinstance(module_pins,dict) or set(module_pins)!=root_modules:
            raise ValueError('Source package module inventory mismatch')
        for name,digest in module_pins.items():
            if hashlib.sha256(z.read(name)).hexdigest()!=digest:
                raise ValueError('Source package module checksum mismatch: '+name)
        if 'install_manifest_sha256' in manifest:
            raw=z.read('install_manifest.json')
            if hashlib.sha256(raw).hexdigest()!=manifest['install_manifest_sha256']:
                raise ValueError('Install manifest SHA mismatch')
            install=json.loads(raw)
            shared=set(install['shared']); source_only=set(install['source_only'])
            if (install.get('schema')!=1 or install['release_version']!=manager_release or install['engine_version']!=engine
                    or set(install['engine'])!=set(deps)|{entry['file']}
                    or not set(install['engine'])<=shared or not set(install['telegram'])<=shared
                    or shared & source_only or {n for n in shared if n.endswith('.py')}|source_only!=root_modules
                    or not shared|source_only<=names):
                raise ValueError('Install inventory mismatch')
            for key in ('reader','funding'):
                for dest,src in install[key].items():
                    if src not in names or src not in (dest,'funding/'+dest):raise ValueError('Component mapping mismatch')
                    if hashlib.sha256(z.read(src)).hexdigest()!=install['component_sha256'].get(src):raise ValueError('Component SHA mismatch')
            if not set(install['tools'])<=names:raise ValueError('Tool inventory mismatch')
        if b'def audit_output_complete(' not in z.read('okx_live_upgrade.py'):
            raise ValueError('Missing financial audit completion guard')
    return manager_release, engine, actual

if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: python3 tests/verify_release_asset.py FILE.zip')
    print('RELEASE ASSET PASS:',*verify(Path(sys.argv[1])))
