#!/usr/bin/env python3
"""Build standalone component source copies in a NEW directory; no LIVE install."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_install_manifest import load, regular
from okx_dependency_attestation import verify

def export(component, destination, root=ROOT):
    root=Path(root)
    destination=Path(destination).absolute()
    if component not in ('reader','funding'):raise RuntimeError('Unknown standalone component')
    # Tool is for producing a local staging directory, never overwriting a runtime.
    if destination.exists() or destination.is_symlink():raise RuntimeError('Destination must not exist')
    parent=destination.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve()!=parent:
        raise RuntimeError('Unsafe export parent')
    if any(p==destination or p in destination.parents for p in map(Path,('/root/okx','/opt/okx-grid-live','/opt/okx-funding','/etc/systemd/system'))):
        raise RuntimeError('LIVE destination forbidden')
    manifest=json.loads(regular(root,'engine_manifest.json').read_text())
    verify(root,manifest)
    inventory=load(root,manifest)
    destination.mkdir(mode=0o700)
    try:
        for name,src in inventory[component].items():
            target=destination/name
            data=regular(root,src).read_bytes()
            if hashlib.sha256(data).hexdigest()!=inventory['component_sha256'][src]:raise RuntimeError('Source changed: '+src)
            with target.open('xb') as f:f.write(data)
            target.chmod(0o600)
            if hashlib.sha256(target.read_bytes()).hexdigest()!=inventory['component_sha256'][src]:raise RuntimeError('Copy drift: '+name)
    except BaseException:
        # This directory was created by this invocation; no preexisting files.
        shutil.rmtree(destination)
        raise
    return destination

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('component',choices=('reader','funding'))
    p.add_argument('--out',required=True,type=Path)
    a=p.parse_args()
    print('COMPONENT EXPORT PASS',export(a.component,a.out))
