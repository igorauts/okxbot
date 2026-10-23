"""GET-only OKX Futures/USDT Cross overview for the single-account manager.

The balance endpoint exposes account-wide totals and currency-level Futures fields
in different places. Values are never inferred from unrelated measures. A
cross-position's IMR is not an exclusive allocation of collateral to a bot.
"""
import datetime
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path


def decimal_or_none(value):
    if value is None or value == '':
        return None
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _first_number(*values):
    for value in values:
        if decimal_or_none(value) is not None:
            return value
    return None


def usd(value, places=2, *, unit='$', positive_sign=False):
    number = decimal_or_none(value)
    if number is None:
        return 'н/д'
    absolute = f'{abs(number):,.{places}f}'
    sign = '-' if number < 0 else '+' if positive_sign and number > 0 else ''
    return f'{sign}{unit}{absolute}'


def _quantity(value):
    d = decimal_or_none(value)
    if d is None:
        return 'н/д'
    return f'{d:,.12f}'.rstrip('0').rstrip('.') if d else '0'


def _contract_values(bots):
    """Read local registered bot config for ctVal; NEVER add speculative OKX GETs."""
    values = {}
    for bot in bots:
        instrument = getattr(bot, 'instrument', '')
        root = getattr(bot, 'root', None)
        if not root or not instrument or instrument in values:
            continue
        path = Path(root) / 'config.json'
        if not path.is_file() or path.is_symlink():
            continue
        try:
            cfg = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(cfg, dict) or cfg.get('instrument') != instrument:
                continue
            ct = decimal_or_none(cfg.get('contract_value'))
            if ct is not None and ct > 0:
                values[instrument] = ct
        except (OSError, ValueError, TypeError):
            continue
    return values


def get_account_snapshot(env_file):
    """Authenticated GET-only OKX balance & positions; shared /positions FIFO."""
    import okx_setup
    env, _ = okx_setup.read_env(env_file)
    balances = okx_setup.okx_req('/api/v5/account/balance', env=env)
    from okx_positions_fifo import AccountPositionsFIFO
    gate = AccountPositionsFIFO(Path(env.get('OKX_ACCOUNT_POSITIONS_GATE_PATH',
                              '/root/okx/.account_positions_gate_v075.json')))
    gate.take()
    positions = okx_setup.okx_req('/api/v5/account/positions', env=env,
                                  params={'instType': 'SWAP'})
    if len(balances) != 1 or not isinstance(balances[0], dict):
        raise RuntimeError('OKX не вернула однозначный снимок баланса')
    if not isinstance(positions, list) or any(not isinstance(p, dict) for p in positions):
        raise RuntimeError('Некорректный список позиций OKX')
    account = balances[0]
    details = account.get('details') or []
    if not isinstance(details, list) or any(not isinstance(x, dict) for x in details):
        raise RuntimeError('Некорректный блок валютного баланса OKX')
    usdt_rows = [d for d in details if d.get('ccy') == 'USDT']
    if len(usdt_rows) > 1:
        raise RuntimeError('OKX вернула неоднозначные данные баланса USDT')
    usdt = usdt_rows[0] if usdt_rows else {}
    positions = [p for p in positions
                 if decimal_or_none(p.get('pos')) not in (None, Decimal(0))]
    # Futures mode: account-level upl/imr/availEq are often deliberately blank;
    # details[USDT] contains the corresponding currency-level figures.
    equity_usdt = _first_number(usdt.get('eq'))
    equity_total = _first_number(account.get('totalEq'))
    free_eq = _first_number(usdt.get('availEq'), account.get('availEq'))
    free_bal = _first_number(usdt.get('availBal'))
    return {
        'asof': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'equity': equity_usdt if equity_usdt is not None else equity_total,
        'equity_unit': 'USDT' if equity_usdt is not None else 'USD',
        'floating_pnl': _first_number(usdt.get('upl'), account.get('upl')),
        'margin_in_use': _first_number(usdt.get('imr'), account.get('imr')),
        'orders_margin': _first_number(usdt.get('ordFrozen'), account.get('ordFrozen')),
        'free_margin': free_eq if free_eq is not None else free_bal,
        'free_margin_label': 'Доступная маржа' if free_eq is not None else 'Доступный баланс',
        'positions': positions,
    }


def _age_and_utc(timestamp, now=None):
    """Readable UTC timestamp plus elapsed seconds, no exchange requests."""
    try:
        stamp = datetime.datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('UTC offset unavailable')
        stamp = stamp.astimezone(datetime.timezone.utc)
        now = now or datetime.datetime.now(datetime.timezone.utc)
        elapsed = max(0, int((now-stamp).total_seconds()))
        return stamp.strftime('%d.%m.%Y %H:%M:%S UTC') + f' ({elapsed} сек назад)'
    except (ValueError, TypeError, OverflowError):
        return 'н/д'


def format_account(snapshot, bots, latch=None, now=None):
    """Readable single-account USDT Cross snapshot. All amounts come from OKX.

    ordFrozen is the only allowed source for 'Маржа открытых ордеров'.
    Never present an arithmetic leftover as though OKX explicitly attributed it.
    """
    width=27
    def kv(label,value):
        return f'{label:<{width}} {value}'

    equnit=snapshot.get('equity_unit','USD')
    lines=[
        'OKX — USDT CROSS',
        'Снимок: '+_age_and_utc(snapshot.get('asof'),now=now),
        '',
        kv('Equity:',usd(snapshot.get('equity'),unit='$')+' '+equnit),
        kv('Floating PnL:',usd(snapshot.get('floating_pnl'))),
        kv('In use:',usd(snapshot.get('margin_in_use'))),
        kv(snapshot.get('free_margin_label','Доступная маржа')+':',
           usd(snapshot.get('free_margin'))),
        '',
        'ОТКРЫТЫЕ ПОЗИЦИИ (CROSS)',
        f'{"Позиция / номинал":<31} {"Плечо":>6} {"Маржа":>11} {"Floating PnL":>14}',
        '-'*66,
    ]
    ct_vals=_contract_values(bots)
    margin_sum=Decimal(0)
    known_count=0
    positions=sorted(snapshot.get('positions',[]),key=lambda p:str(p.get('instId','')))
    for item in positions:
        inst=str(item.get('instId',''))
        coin=inst[:-10] if inst.endswith('-USDT-SWAP') else inst or '?'
        contracts=decimal_or_none(item.get('pos'))
        ct=ct_vals.get(inst)
        if ct is not None and contracts is not None:
            qty=_quantity(contracts*ct)+' '+coin
        elif contracts is not None:
            qty=_quantity(contracts)+' контр.'
        else:
            qty='н/д'
        nominal=decimal_or_none(item.get('notionalUsd'))
        if nominal is not None:
            position=f'{qty} ({usd(abs(nominal))})'
        else:
            position=qty
        leverage=decimal_or_none(item.get('lever'))
        lev=_quantity(leverage)+'x' if leverage is not None and leverage>0 else 'н/д'
        imr=decimal_or_none(item.get('imr'))
        if imr is not None:
            known_count+=1
            margin_sum+=imr
        lines.append(f'{position:<31} {lev:>6} {usd(imr):>11} '
                     f'{usd(item.get("upl"),positive_sign=True):>14}')
    if not positions:
        lines.append('Открытых SWAP-позиций нет')
    lines += ['',
              kv('Маржа позиций:',usd(margin_sum) if known_count==len(positions) else 'н/д'),
              kv('Маржа открытых ордеров:',usd(snapshot.get('orders_margin'))),
              kv('Всего In use:',usd(snapshot.get('margin_in_use'))),
             ]
    in_use=decimal_or_none(snapshot.get('margin_in_use'))
    orders=decimal_or_none(snapshot.get('orders_margin'))
    if in_use is not None and orders is not None and known_count==len(positions):
        other=in_use-margin_sum-orders
        if abs(other)>Decimal('0.02'):
            lines.append(kv('Другое / расхождение:',usd(other)))
    lines.extend(['',
        'CROSS: маржа общая для аккаунта.',
        '',
        kv('LIVE-ботов:',('н/д (реестр неполон)' if snapshot.get('registry_partial')
                           else str(sum(b.systemd_state=='active' for b in bots)))),
        'SOFT-блокировка: '+(str(latch.get('code') or latch.get('reason'))
           if latch else 'отсутствует')])
    if snapshot.get('registry_warnings'):
        lines.append('⚠️ Реестр повреждён: часть бот-данных неизвестна; '
                     'не используйте счётчик ботов для аварийных решений.')
        lines.extend('  '+str(x)[:120] for x in snapshot['registry_warnings'][:4])
    return lines
