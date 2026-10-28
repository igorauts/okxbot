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
    if len(parts)==2:return parts[0] in ('tests','historical')
    return len(parts)==3 and parts[:2]==('deploy','reader')


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
        for executable in ('okxbot.py','okx_live_upgrade.py','rollout_preflight.py'):
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
        if b'def audit_output_complete(' not in z.read('okx_live_upgrade.py'):
            raise ValueError('Missing financial audit completion guard')
    return release, engine, actual

if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: python3 tests/verify_release_asset.py FILE.zip')
    print('RELEASE ASSET PASS:',*verify(Path(sys.argv[1])))
