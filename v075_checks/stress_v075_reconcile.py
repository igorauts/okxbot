#!/usr/bin/env python3
"""Offline integration replay of actual v074/v075 AST-extracted reconciliation.

No imports of the live engine, no OKX credentials, no API network access, no
service modification. State is synthetic, padded to match user-provided file sizes.
Only artifacts in a private temporary directory are written and fsynced.
"""
import ast
import contextlib
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import secrets
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SOURCES = {
    '074': HERE / 'original_okx_bot074.py',
    '075': HERE.parent / 'okx_bot075.py',
}
FUNCTIONS = [
    'D', 'atomic_json', 'save', 'migrate_cycle_records', 'tp_records',
    'cycle_sell_fill', 'active_tp_records', 'register_cancel_attached',
    'resolve_partial_cancel', 'verify_existing', 'parent_tp_failure',
    'tp_record_update', 'reconcile_aggregate', 'track',
    'reconcile_position', 'panic_executions', 'coverage_audit',
    'real_tp_active', 'virtual_cycles', 'remaining_tp', 'unprotected_partial_buy',
]
EXTRA_075 = ['_durable_save', '_flush_confirmed_facts', '_batch_confirmed_facts']

class OfflineAPI:
    def __init__(self, inst, buy_orders, sell_orders, pending, actual_position):
        self.inst = inst
        self._pending = pending
        self._position = actual_position
        self.orders = {}
        for order in buy_orders + sell_orders:
            self.orders[('oid', order['ordId'])] = order
            self.orders[('cid', order['clOrdId'])] = order
        self.order_reads = 0
        self.position_reads = 0

    def pending(self):
        return copy.deepcopy(self._pending)

    def order(self, oid='', cid=''):
        self.order_reads += 1
        return self.orders.get(('oid', oid) if oid else ('cid', cid))

    def positions(self):
        self.position_reads += 1
        return ([{'pos':str(self._position)}] if self._position else [])

    def req(self, *args, **kwargs):
        raise AssertionError('Raw API requests forbidden in offline replay')


def fixture(inst, n, padding_bytes, scenario):
    quantity = '0.003' if inst.startswith('ETH') else '0.04'
    step = Decimal('0.50') if inst.startswith('ETH') else Decimal('0.03')
    base = Decimal('2400') if inst.startswith('ETH') else Decimal('102')
    tp = Decimal('2.75') if inst.startswith('ETH') else Decimal('0.13')
    cycles = {}
    buy_orders = []
    sell_orders = []
    pending = []
    pos_per_cycle = Decimal(0) if scenario == 'closed' else (
        Decimal(quantity) if scenario == 'tp_live' else Decimal('0.001' if inst.startswith('ETH') else '0.02'))
    for idx in range(n):
        px = base - idx * step
        k = f'{px:.2f}'
        tp_px = f'{px + tp:.2f}'
        bid = f'B{idx:05d}'
        sid = f'S{idx:05d}'
        bcid = f'CB{idx:05d}'
        scid = f'CS{idx:05d}'
        cycles[k] = {
            'state': 'BUY_LIVE', 'buyPx': k, 'tpPx': tp_px, 'sz': quantity,
            'placedAt': 0, 'expTimeMs': 0, 'cycleId': f'cycle-{idx:05d}',
            'tpOrders': [], 'clOrdId': bcid, 'tpClOrdId': scid,
            'ordId': bid, 'tpOrdId': '', 'filledBuy': '0', 'filledTP': '0',
        }
        buy = {'instId': inst, 'side': 'buy', 'ordId': bid,
               'clOrdId': bcid,
               'state': ('partially_filled' if scenario == 'partial_buy' else 'filled'),
               'accFillSz': str(pos_per_cycle) if scenario == 'partial_buy' else quantity,
               'sz': quantity}
        buy_orders.append(buy)
        if scenario == 'partial_buy':
            pending.append(buy)
        else:
            sell = {'instId': inst, 'side': 'sell', 'ordId': sid,
                    'clOrdId': scid,
                    'state': 'live' if scenario == 'tp_live' else 'filled',
                    'px': tp_px, 'reduceOnly': 'true',
                    'accFillSz': '0' if scenario == 'tp_live' else quantity,
                    'sz': quantity}
            sell_orders.append(sell)
            if scenario == 'tp_live':
                pending.append(sell)
    state = {'version': '0.7.4', 'instrument': inst, 'cycles': cycles,
             'completed': 0, 'archived_cycles': []}
    # Synthetic padding only; no real user data or keys. Match live state size.
    current = len(json.dumps(state, indent=2, ensure_ascii=False).encode('utf-8'))
    if padding_bytes > current + 256:
        state['synthetic_padding'] = 'x' * (padding_bytes - current - 50)
    return state, buy_orders, sell_orders, pending, n * pos_per_cycle


def load_engine(version, storage, inst, buy_orders, sell_orders, pending, actual_position):
    tree = ast.parse(SOURCES[version].read_text())
    nodes = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    env = {
        '__builtins__': __builtins__, 'Decimal': Decimal, 'datetime': datetime,
        'timezone': timezone, 'json': json, 'os': os, 'Path': Path,
        'time': SimpleNamespace(time=lambda: 1700000000.0, monotonic=time.monotonic, sleep=time.sleep), 'contextlib': contextlib, 'threading': threading,
        'sys': sys, 'secrets': secrets, 'INST': inst,
        'COIN': inst.split('-')[0], 'QTY': Decimal('0.003'),
        'REDUCE_ONLY_HARD_CAP': 150,
        '_PERF_LOCAL': threading.local(), '_PERF_RECON_ENABLED': False,
        'DATA': storage, 'LOGS': storage.parent, 'log': lambda *x, **kw: None,
        'journal_tp': lambda *x, **kw: None,
        'die': lambda msg: (_ for _ in ()).throw(RuntimeError(msg)),
        'trip_if_margin_cancel': lambda *x, **kw: None,
        'ExchangeUnresolved': type('ExchangeUnresolved',(RuntimeError,),{}),
    }
    # Compile only selected functions: NEVER run the module initialization,
    # read .env, create a REST connection, or run the main trading loop.
    for name in FUNCTIONS + (EXTRA_075 if version == '075' else []):
        code = compile(ast.Module(body=[nodes[name]],type_ignores=[]),
                       str(SOURCES[version]) + '::' + name, 'exec')
        exec(code, env)
    real_writer = env['atomic_json']
    counts = {'writes': 0, 'write_seconds': 0.0, 'bytes': 0}
    def writer(path, state):
        t = time.perf_counter()
        real_writer(path, state)
        counts['write_seconds'] += time.perf_counter() - t
        counts['writes'] += 1
        counts['bytes'] += path.stat().st_size
    env['atomic_json'] = writer
    api = OfflineAPI(inst, buy_orders, sell_orders, pending, actual_position)
    env['A'] = api
    return env, api, counts


def run_once(version, inst, n, padding, scenario):
    st, buys, sells, pending_expected, pos_expected = fixture(inst, n, padding, scenario)
    with tempfile.TemporaryDirectory(prefix='okx075_replay_', dir=HERE) as tmp:
        storage = Path(tmp) / 'bot04_state.json'
        env, api, counts = load_engine(version, storage, inst, buys, sells, pending_expected, pos_expected)
        # Base state exists, exactly as it would before the exchange burst.
        env['atomic_json'](storage, st)
        counts.update(writes=0, write_seconds=0, bytes=0)
        started = time.perf_counter()
        pending, resolved, actual = env['reconcile_position'](st)
        elapsed = time.perf_counter() - started
        assert pending == pending_expected, 'Unexpected pending orders'
        assert resolved and actual == pos_expected, 'Position mismatch or unresolved orders'
        target_state = {'closed':'CLOSED','tp_live':'TP_WAIT',
                        'partial_buy':'BUY_PARTIAL'}[scenario]
        assert st['completed'] == (n if scenario == 'closed' else 0), 'Incorrect TP closure count'
        assert all(c['state'] == target_state for c in st['cycles'].values()), 'Wrong final states'
        if scenario == 'closed':
            assert all(Decimal(c['filledBuy']) == Decimal(c['filledTP'])
                       for c in st['cycles'].values()), 'Incorrect filled quantity'
        if scenario != 'closed':
            # Run the engine's real safety coverage audit, not an approximation.
            env['coverage_audit'](st, pending, actual)
        persisted = json.loads(storage.read_text())
        assert persisted == st, f'Lost durable terminal facts in v{version}'
        assert counts['writes'] > 0, 'Replay did not exercise physical fsync writes'
        assert api.position_reads == 1, 'Unexpected retries'
        return {
            'version': version, 'inst': inst, 'cycles': n, 'scenario':scenario,
            'seconds': elapsed, 'writes': counts['writes'],
            'write_seconds': counts['write_seconds'],
            'written_mib': counts['bytes']/1024/1024,
            'order_reads': api.order_reads, 'position_reads': api.position_reads,
            'state_digest': __import__('hashlib').sha256(
                json.dumps(st, sort_keys=True).encode()).hexdigest(),
        }


def read_sizes():
    # Only stat existing live state files; never open/read contents.
    sizes = {'ETH-USDT-SWAP': 830*1024, 'SOL-USDT-SWAP': 600*1024}
    for name,inst in [('eth064','ETH-USDT-SWAP'),('sol065','SOL-USDT-SWAP')]:
        path = Path('/root/okx') / name / 'state' / 'bot04_state.json'
        if path.is_file(): sizes[inst] = path.stat().st_size
    return sizes


def main():
    print('OFFLINE AST RECONCILIATION REPLAY — NO IMPORTS / NO API / NO LIVE WRITES',flush=True)
    print('Scenarios: 40/145 closed cycles, 25 attached live TP, 25 partial BUY.',flush=True)
    sizes = read_sizes()
    for inst, padding in sizes.items():
        for scenario, n in [('closed',40),('closed',145),('tp_live',25),('partial_buy',25)]:
            rows=[]
            for ver in ('074','075'):
                row=run_once(ver, inst, n, padding, scenario)
                rows.append(row)
                print(f'{inst} {scenario} cycles={n} v{ver}: '
                      f'total={row["seconds"]:.3f}s writes={row["writes"]} '
                      f'fsync={row["write_seconds"]:.3f}s '
                      f'written={row["written_mib"]:.1f} MiB '
                      f'GET-ord={row["order_reads"]}',flush=True)
            old,new=rows
            assert old['state_digest']==new['state_digest'], 'DIVERGENT FINAL STATES'
            assert new['writes'] < old['writes'], 'NO WRITE REDUCTION'
            assert new['writes'] <= 2, 'TOO MANY NEW WRITES'
            print(f'PASS identical final states; writes {old["writes"]} -> {new["writes"]}; '
                  f'elapsed speedup {old["seconds"]/max(new["seconds"],1e-9):.2f}x',flush=True)
    print('ALL OFFLINE REPLAYS PASSED. LIVE SERVICES AND STATES UNTOUCHED.',flush=True)

if __name__ == '__main__':
    main()
