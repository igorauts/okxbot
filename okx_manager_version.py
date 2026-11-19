"""Manager/package identity, deliberately separate from pinned trading-engine identity.
Trading instances retain the original okx_version.py and ENGINE_VERSION.
"""
import re
from okx_version import ENGINE_VERSION, validate_version_ids
RELEASE_VERSION = '0.7.76'
RELEASE_KIND = 'engine'
def validate_manager_version(release=RELEASE_VERSION, engine=ENGINE_VERSION):
    """Full release: package == engine; maintenance: package > engine."""
    validate_version_ids()
    if (not isinstance(release, str) or not isinstance(engine, str) or
            not re.fullmatch(r'\d+\.\d+\.\d+', release) or
            not re.fullmatch(r'\d+\.\d+\.\d+', engine) or
            release != RELEASE_VERSION or engine != ENGINE_VERSION):
        raise RuntimeError('Unapproved manager/engine identity combination')
    if RELEASE_KIND == 'engine':
        if release != engine:
            raise RuntimeError('Full release must match engine identity')
    elif RELEASE_KIND == 'maintenance':
        if tuple(map(int, release.split('.'))) <= tuple(map(int, engine.split('.'))):
            raise RuntimeError('Maintenance release must be newer than engine')
    else:
        raise RuntimeError('Unknown release kind')
    return True
