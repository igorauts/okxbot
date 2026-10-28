#!/usr/bin/env python3
"""Version-independent deterministic OFFLINE source ZIP builder.

Uses source-of-truth RELEASE_VERSION, not any hardcoded release or engine label.
No GitHub, VPS, network, credential, or trading actions.
"""
import argparse
import ast
import hashlib
import os
import stat
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
EXECUTABLES = ('okxbot.py','okx_live_upgrade.py','rollout_preflight.py',
               'run_checks.sh','okx_shared_runtime_deploy.py','build_release_zip.py')


def build(out=None):
    syntax = ast.parse((SOURCE/'okx_version.py').read_text(encoding='utf-8'))
    metadata = {}
    for stmt in syntax.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            for target in stmt.targets:
                if isinstance(target, ast.Name):metadata[target.id]=stmt.value.value
    release=metadata['RELEASE_VERSION']
    if (not isinstance(release,str) or len(release.split('.'))!=3 or
            not all(part.isdigit() for part in release.split('.'))):
        raise RuntimeError('Release must be three-component source metadata')
    dest=(Path(out) if out else SOURCE.parent /
          ('OKX_GRID_BOT_v'+''.join(release.split('.'))+'_OFFLINE_RC1_NOT_LIVE.zip'))
    if dest.exists() and dest.resolve() == (SOURCE/'okx_version.py').resolve():
        raise RuntimeError('Unsafe output')
    from tests.verify_release_asset import allowed_package_member
    files=[]
    for p in SOURCE.rglob('*'):
        if (not p.is_file() or p.is_symlink() or
                p.name.endswith('.pyc') or any(seg in ('__pycache__','.git') for seg in p.relative_to(SOURCE).parts) or
                p.name in ('checks_full.log','checks_full.rc')):
            continue
        name=p.relative_to(SOURCE).as_posix()
        if not allowed_package_member(name):
            raise RuntimeError('Unexpected source file: '+name)
        files.append((name,p))
    files.sort()
    if not all((SOURCE/f).is_file() for f in EXECUTABLES):
        raise RuntimeError('Missing executable source')
    with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,p in files:
            info=zipfile.ZipInfo(name,(1980,1,1,0,0,0))
            info.create_system=3
            info.external_attr=(stat.S_IFREG | (0o755 if name in EXECUTABLES else 0o644))<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            z.writestr(info,p.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    from tests.verify_release_asset import verify
    r,e,h=verify(dest)
    if r!=release:raise RuntimeError('Built release metadata mismatch')
    print('OFFLINE ZIP PASS:',dest,'files',len(files),'release',r,'engine',e)
    print('SHA256:',hashlib.sha256(dest.read_bytes()).hexdigest())
    return dest

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default=None)
    args=parser.parse_args()
    build(args.out)
