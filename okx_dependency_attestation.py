#!/usr/bin/env python3
"""Read-only SHA256 attestation of exact source-package / engine dependencies.

Never reads trading credentials, trading state, exchange APIs or live services.
An optional --bot-root is audit-only and NEVER blocks running engines.
"""
import argparse
import hashlib
import hmac
import json
from pathlib import Path


def digest(path):
    p=Path(path)
    if not p.is_file() or p.is_symlink():
        raise RuntimeError('Missing or symlinked source: '+p.name)
    return hashlib.sha256(p.read_bytes()).hexdigest()


def verify(root, manifest, *, bot_only=False):
    root=Path(root)
    engine=manifest['engines'][manifest['current_version']]
    required=[engine['file']]+list(engine['dependencies'])
    expected=engine.get('dependency_sha256')
    if not isinstance(expected,dict) or set(expected)!=set(engine['dependencies']):
        raise RuntimeError('Incomplete current engine dependency manifest')
    if digest(root/engine['file'])!=engine['sha256']:
        raise RuntimeError('Engine SHA mismatch: '+engine['file'])
    for filename in engine['dependencies']:
        if not hmac.compare_digest(digest(root/filename), expected[filename]):
            raise RuntimeError('Engine dependency SHA mismatch: '+filename)
    if bot_only:
        return len(required)
    package=manifest.get('source_modules_sha256')
    if not isinstance(package,dict) or not package:
        raise RuntimeError('No full source package SHA manifest')
    actual={p.name for p in root.glob('*.py') if p.is_file()}
    if set(package)!=actual:
        raise RuntimeError('Source package Python inventory mismatch')
    for filename, sha in package.items():
        if not hmac.compare_digest(digest(root/filename),sha):
            raise RuntimeError('Package module SHA mismatch: '+filename)
    return len(package)


def main():
    cli=argparse.ArgumentParser()
    cli.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    cli.add_argument('--bot-root',type=Path,default=None)
    ns=cli.parse_args()
    source=ns.source.resolve()
    manifest_path=source/'engine_manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SystemExit('FAIL_CLOSED: missing source manifest')
    manifest=json.loads(manifest_path.read_text())
    count=verify(source,manifest)
    print('SOURCE_PACKAGE_SHA256=PASS',count,'ENGINE=',manifest['current_version'])
    if ns.bot_root:
        try:
            count=verify(ns.bot_root,manifest,bot_only=True)
            print('BOT_DEPENDENCY_SHA256=PASS',count)
        except RuntimeError as exc:
            print('BOT_DEPENDENCY_SHA256=DRIFT',str(exc))
            raise SystemExit(2)
    print('NO_TRADING_MUTATIONS=YES NO_OKX_API=YES AUDIT_ONLY=YES')

if __name__=='__main__':main()
