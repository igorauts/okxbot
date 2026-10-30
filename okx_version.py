"""Independent package and trading-engine identities.

Source-controlled version metadata is the only authority.  A maintenance
package explicitly declares its type instead of adding a hard-coded pair to
an ever-growing compatibility table. Engine identity remains authoritative
in the engine manifest, not the release number.
"""
RELEASE_VERSION = '0.7.43'
ENGINE_VERSION = '0.7.43'
STATE_SCHEMA = 1
RELEASE_KIND = 'engine'


def validate_version_ids(release=RELEASE_VERSION, engine=ENGINE_VERSION):
    """Fail closed for invalid identities and undeclared version divergence."""
    import re
    if not isinstance(release, str) or not isinstance(engine, str):
        raise RuntimeError('Version identities must be strings')
    if not re.fullmatch(r'\d+\.\d+\.\d+', engine):
        raise RuntimeError('Invalid engine identity')
    if not re.fullmatch(r'\d+\.\d+\.\d+', release):
        raise RuntimeError('Invalid release identity')
    # Same-engine full release only; four-part labels are forbidden.
    if release == engine:
        return True
    # Three-part maintenance releases require an explicit package type, and
    # the exact pair declared at the source-of-truth constants above.
    if (RELEASE_KIND == 'maintenance' and release == RELEASE_VERSION
            and engine == ENGINE_VERSION and len(release.split('.')) == 3
            and tuple(map(int, release.split('.'))) > tuple(map(int, engine.split('.')))):
        return True
    raise RuntimeError('Unapproved package/engine identity combination')
