"""Manager/package identity, deliberately separate from pinned trading-engine identity.
Trading instances retain the original okx_version.py and ENGINE_VERSION.
"""
import re
from okx_version import ENGINE_VERSION, validate_version_ids
RELEASE_VERSION = '0.7.74'
RELEASE_KIND = 'maintenance'
def validate_manager_version(release=RELEASE_VERSION, engine=ENGINE_VERSION):
    validate_version_ids()  # immutable trading engine contract
    if (not isinstance(release, str) or not isinstance(engine, str) or
            not re.fullmatch(r'\d+\.\d+\.\d+', release) or
            not re.fullmatch(r'\d+\.\d+\.\d+', engine) or
            tuple(map(int,release.split('.'))) <= tuple(map(int,engine.split('.'))) or
            release != RELEASE_VERSION or engine != ENGINE_VERSION or
            RELEASE_KIND != 'maintenance'):
        raise RuntimeError('Unapproved manager/engine identity combination')
    return True
