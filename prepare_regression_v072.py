#!/usr/bin/env python3
"""Add new ETH-only v0.7.2 fixtures to a disposable v0.6.10 repo clone.
Keep the original 0.6.10 legacy tests untouched; never use LIVE files/API.
"""
import argparse,shutil
from pathlib import Path
HERE=Path(__file__).resolve().parent
SUITES=('safety','profiling','aggregate','state','buffer','crossing','near_bid')

def prepare(root):
    root=Path(root).resolve()
    if not (root/'.git').exists() or not (root/'tests/test_grid.py').is_file():
        raise RuntimeError('Must provide disposable repo checkout with tests/test_grid.py')
    for name in ('okx_bot072.py','verify_import_v072.py','verify_upgrade_v072.py',
                 'manual_clear_account_soft.py',*(f'test_v072_{n}.py' for n in SUITES)):
        shutil.copy2(HERE/name,root/name)
    for suite in SUITES:
        clazz={'safety':'SafetyTests','profiling':'Tests','aggregate':'TestAggregate',
               'state':'TestState','buffer':'TestBuffer','crossing':'CrossingTests',
               'near_bid':'TestNearBid'}[suite]
        (root/'tests'/f'test_{suite}_v072.py').write_text(
            'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0,str(Path(__file__).resolve().parents[1]))\n'
            f'from test_v072_{suite} import {clazz}\n')
    print('Added v0.7.2 ETH tests to disposable checkout; no API calls.')

if __name__=='__main__':
    a=argparse.ArgumentParser()
    a.add_argument('disposable_clone')
    prepare(a.parse_args().disposable_clone)
