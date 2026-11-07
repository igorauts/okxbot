#!/usr/bin/env python3
"""Run imported original StatsWorker-v2 phase + production tests on candidate code.

The historical ORIGINAL FULL oracle is a byte-pinned *offline test fixture*
only. It is never imported as an executable trading engine and never called
by LIVE code. Tests run on temporary synthetic SQLite/state trees. No exchange.
"""
from __future__ import annotations
import gzip
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
SOURCES=HERE/'stats_v2_original'
ORACLE_GZ=HERE/'fixtures/okx_bot_original_full_v0760.py.gz'
ORACLE_SHA='9c6cf1b46ba8f0b045a80ea97b3178657fd296c2e6d564fb8579839e66057cbd'


def require(value,message):
    if not value: raise RuntimeError(message)


def main():
    require(ORACLE_GZ.is_file() and not ORACLE_GZ.is_symlink(),
            'MISSING_PINNED_FULL_ORACLE_FIXTURE: '+str(ORACLE_GZ))
    oracle=gzip.decompress(ORACLE_GZ.read_bytes())
    require(hashlib.sha256(oracle).hexdigest()==ORACLE_SHA,
            'PINNED_ORACLE_SHA_MISMATCH')
    with tempfile.TemporaryDirectory(prefix='stats-v2-original-tests-') as d:
        work=Path(d)
        release=work/'stats_v2_release_work'
        production=work/'stats_v2_prod_candidate'
        (release/'reference').mkdir(parents=True)
        (release/'tests').mkdir(parents=True)
        (production/'tests').mkdir(parents=True)
        (release/'reference'/'okx_bot.py').write_bytes(oracle)
        for f in ROOT.glob('okx_stats_v2_*.py'):
            shutil.copyfile(f,release/f.name)
        shutil.copyfile(ROOT/'okx_bot.py',production/'okx_bot.py')
        for group,loc in [('stats_v2_release_work',release),('stats_v2_prod_candidate',production)]:
            test_src=SOURCES/group/'tests'
            require(test_src.is_dir(),'MISSING_IMPORTED_TESTS '+group)
            paths=sorted(test_src.glob('test_*.py'))
            require(bool(paths),'EMPTY_IMPORTED_TESTS '+group)
            for f in paths:
                require(f.is_file() and not f.is_symlink(),'UNSAFE_TEST '+str(f))
                shutil.copyfile(f,loc/'tests'/f.name)
        env=os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE']='1'
        env['PYTHONPATH']=os.pathsep.join([str(release),str(release/'tests'),str(production),str(production/'tests')])
        # Unmodified original tests; separate processes avoid test-module collisions.
        # The counts are part of the pinned offline-suite contract. A missing
        # file or silently undiscovered test must never appear as a green run.
        for name,folder,expected_count in [('PHASE',release,164),('PRODUCTION',production,32)]:
            cmd=[sys.executable,'-B','-m','unittest','discover','-s',str(folder/'tests'),'-p','test_*.py','-v']
            print('OFFLINE_ORIGINAL_STATS_V2_'+name+'_START',flush=True)
            result=subprocess.run(cmd,cwd=str(work),env=env,timeout=180,
                                  check=False,text=True,capture_output=True)
            if result.stdout:print(result.stdout,end='',flush=True)
            if result.stderr:print(result.stderr,end='',file=sys.stderr,flush=True)
            require(result.returncode==0,'ORIGINAL_STATS_V2_'+name+'_FAILED rc='+str(result.returncode))
            matches=re.findall(r'(?m)^Ran (\d+) tests? in ',result.stderr)
            require(len(matches)==1 and int(matches[0])==expected_count,
                    'ORIGINAL_STATS_V2_'+name+'_TEST_COUNT_MISMATCH: '+repr(matches))
            print('OFFLINE_ORIGINAL_STATS_V2_'+name+'_PASS count='+str(expected_count),flush=True)
    print('===== ORIGINAL_STATS_V2_SUITES_PASS =====',flush=True)


if __name__=='__main__':
    try:main()
    except BaseException as e:
        print('ORIGINAL_STATS_V2_SUITES_ABORTED '+type(e).__name__+': '+str(e),file=sys.stderr)
        raise SystemExit(1)
