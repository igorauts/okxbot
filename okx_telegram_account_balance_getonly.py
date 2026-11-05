#!/usr/bin/env python3
"""Isolated, user-triggered read-only account balance for Telegram.

Reuse the SAME credential reader, authenticated GET and account numeric
selection as the already-working OKX account manager. No writes, no POST,
no retries, no positions GET, no raw credentials/payloads in the output.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

SHARED = Path('/opt/okx-grid-live')
CREDENTIALS = Path('/root/okx/.okxbot_account.env')
BALANCE_ENDPOINT = '/api/v5/account/balance'


def read_balance():
    if not SHARED.is_dir() or SHARED.is_symlink():
        raise RuntimeError('Trusted OKX manager directory unavailable')
    if CREDENTIALS.is_symlink() or not CREDENTIALS.is_file():
        raise RuntimeError('Trusted account credentials unavailable')
    # Imports are isolated in the child; parent Telegram monitor never reads secrets.
    if str(SHARED) not in sys.path:
        sys.path.insert(0, str(SHARED))
    import okx_setup
    from okx_account_ui import decimal_or_none, _first_number
    env, _ = okx_setup.read_env(CREDENTIALS)
    rows = okx_setup.okx_req(BALANCE_ENDPOINT, env=env)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError('Ambiguous account balance response')
    account = rows[0]
    details = account.get('details') or []
    if not isinstance(details, list) or any(not isinstance(x, dict) for x in details):
        raise RuntimeError('Invalid currency rows')
    usdt_rows = [x for x in details if x.get('ccy') == 'USDT']
    if len(usdt_rows) > 1:
        raise RuntimeError('Ambiguous USDT balance')
    usdt = usdt_rows[0] if usdt_rows else {}
    # Same source priorities as okx_account_ui.get_account_snapshot().
    equity_usdt = _first_number(usdt.get('eq'))
    equity_raw = equity_usdt if equity_usdt is not None else _first_number(account.get('totalEq'))
    floating_raw = _first_number(usdt.get('upl'), account.get('upl'))
    equity = decimal_or_none(equity_raw)
    floating = decimal_or_none(floating_raw)
    if equity is None or floating is None or equity < 0:
        raise RuntimeError('Account balance fields unavailable')
    balance = equity - floating
    if not all(v.is_finite() for v in (equity, floating, balance)):
        raise RuntimeError('Invalid numeric account fields')
    return {'schema': 1, 'asof': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'unit': 'USDT' if equity_usdt is not None else 'USD',
            'equity': str(equity), 'balance': str(balance), 'floating': str(floating)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fetch', action='store_true')
    args=parser.parse_args()
    if not args.fetch:
        parser.error('Use --fetch')
    try:
        print(json.dumps(read_balance(), separators=(',', ':'), ensure_ascii=True))
        return 0
    except Exception as exc:
        # Avoid including exception messages: external errors may contain details.
        print('ACCOUNT_BALANCE_UNAVAILABLE=' + type(exc).__name__, file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
