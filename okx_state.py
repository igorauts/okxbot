"""Strict fresh-engine state checks; no historical migration."""
from okx_version import STATE_SCHEMA


class IncompatibleState(RuntimeError):
    pass


def validate(st, instrument):
    if not isinstance(st, dict) or st.get('schema_version') != STATE_SCHEMA:
        raise IncompatibleState('Unsupported state schema; no automatic migration')
    if st.get('instrument') != instrument or not isinstance(st.get('cycles'), dict):
        raise IncompatibleState('Instrument/cycles mismatch in state')
    if not isinstance(st.get('archived_cycles', []), list):
        raise IncompatibleState('Invalid archived cycle structure')
    for c in (*st['cycles'].values(), *st.get('archived_cycles', [])):
        if not isinstance(c, dict) or not c.get('cycleId') or not isinstance(c.get('tpOrders'), list):
            raise IncompatibleState('Incomplete cycle identity/TP orders; refusing startup')
    return st


def empty(version, instrument):
    return {'schema_version': STATE_SCHEMA, 'version': version,
            'instrument': instrument, 'cycles': {}, 'completed': 0,
            'archived_cycles': []}
