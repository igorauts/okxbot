"""Independent package release and trading engine identities.

Maintenance-only packages MUST NOT change the engine identity.
"""
RELEASE_VERSION = '0.7.27'
ENGINE_VERSION = '0.7.27'
STATE_SCHEMA = 1


def validate_version_ids(release=RELEASE_VERSION, engine=ENGINE_VERSION):
    """Reject version contamination before creating a new instance."""
    import re
    if not (re.fullmatch(r'\d+\.\d+\.\d+', engine)
            and re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', release)
            and (release==engine or release.startswith(engine+'.'))):
        raise RuntimeError('Package release/engine identities are inconsistent')
    return True
