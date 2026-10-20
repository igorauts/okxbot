"""Validate tradable USDT-settled linear perpetuals, never base-coin whitelist."""
from decimal import Decimal, InvalidOperation
import re

INSTRUMENT_RE = re.compile(r'^[A-Z0-9]{2,32}-USDT-SWAP$')


def base_coin(inst_id):
    if not isinstance(inst_id, str) or not INSTRUMENT_RE.fullmatch(inst_id):
        raise ValueError('Only USDT perpetual SWAP instruments are supported')
    return inst_id[:-10]  # suffix '-USDT-SWAP'


def validate_spec(inst_id, spec):
    coin = base_coin(inst_id)
    if not isinstance(spec, dict) or any((
        spec.get('instId') != inst_id,
        spec.get('instType') != 'SWAP',
        spec.get('ctType') != 'linear',
        spec.get('settleCcy') != 'USDT',
        spec.get('state') != 'live',
        spec.get('ctValCcy') != coin,
    )):
        raise ValueError('Non-live, non-USDT, non-linear or foreign contract')
    try:
        values = [Decimal(str(spec[key])) for key in ('tickSz', 'ctVal', 'lotSz', 'minSz')]
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Missing or invalid contract specification') from exc
    if not all(value.is_finite() and value > 0 for value in values):
        raise ValueError('Invalid contract increments or minimum')
    return coin
