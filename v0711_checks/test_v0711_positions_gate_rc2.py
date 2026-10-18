#!/usr/bin/env python3
"""RC2 shared positions gate: multiprocessing safety and release-before-sleep.

All tests extract ONLY the gate class from engine AST. Never import or start
trading engine, create an exchange client, or read credentials.
"""
import ast
import fcntl
import json
import multiprocessing as mp
import os
import tempfile
import threading
import time
import unittest

from okx_latency_v0711 import LatencyJournal
from pathlib import Path

SOURCE = Path(__file__).parents[1] / 'okx_bot0711.py'

class Metrics:
    def __init__(self):
        self.values = []
    def record(self, name, ms, **fields):
        self.values.append((name, ms, fields))


def gate(path, window=None):
    src = ast.parse(SOURCE.read_text())
    node = next(n for n in src.body if isinstance(n, ast.ClassDef)
                and n.name == '_AccountPositionsGate')
    metrics = Metrics()
    warnings = []
    env = {
        'Path': Path, 'ENV': {'OKX_ACCOUNT_POSITIONS_GATE_PATH': str(path)},
        'os': os, 'fcntl': fcntl, 'json': json, 'threading': threading,
        'time': time, 'LATENCY': metrics,
        'log': lambda event, **fields: warnings.append((event, fields)),
        'die': lambda why: (_ for _ in ()).throw(RuntimeError(why)),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<isolated gate>', 'exec'),env)
    inst = env['_AccountPositionsGate']()
    if window is not None: inst.WINDOW = window
    return inst, metrics, warnings


def worker(path, count, out):
    try:
        obj, metrics, warnings = gate(path)
        for _ in range(count):
            obj.take()
            out.put(('ok', time.time()))
        out.put(('done', warnings))
    except BaseException as e:
        out.put(('error', repr(e)))

class PositionGateRC2Tests(unittest.TestCase):
    def test_does_not_hold_flock_while_waiting_for_quota(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'gate.json'
            a, _, _ = gate(p)
            for i in range(a.MAX_SHARED): a.take()
            ctx = mp.get_context('fork')
            q = ctx.Queue()
            proc = ctx.Process(target=worker,args=(str(p),1,q))
            proc.start()
            try:
                time.sleep(0.18) # worker is sleeping for ~1.9sec of quota delay
                fd=os.open(p,os.O_RDONLY)
                acquired=False
                try:
                    try:
                        fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired=True
                    except BlockingIOError:
                        acquired=False
                    if acquired:
                        fcntl.flock(fd,fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                self.assertTrue(acquired,'Gate kept flock locked during quota sleep')
            finally:
                proc.join(5)
                if proc.is_alive(): proc.terminate();proc.join(2)
            self.assertEqual(proc.exitcode,0)
            self.assertEqual(q.get(timeout=1)[0], 'ok')
            self.assertEqual(q.get(timeout=1)[0], 'done')

    def test_four_processes_share_one_sliding_window(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'gate.json'
            ctx = mp.get_context('fork')
            q = ctx.Queue()
            procs = [ctx.Process(target=worker,args=(str(p),3,q))
                     for _ in range(4)]
            for proc in procs: proc.start()
            results = [q.get(timeout=10) for _ in range(16)]
            for proc in procs:
                proc.join(3)
                if proc.is_alive():proc.terminate();proc.join(2)
                self.assertEqual(proc.exitcode,0)
            self.assertFalse([r for r in results if r[0]=='error'],results)
            ts=sorted(r[1] for r in results if r[0]=='ok')
            self.assertEqual(len(ts),12)
            self.assertEqual(sum(r[0]=='done' for r in results),4)
            # Reservation times are returned just AFTER the flock is released.
            # A 2.00s subwindow avoids timing noise around WINDOW=2.10s.
            for t in ts:
                self.assertLessEqual(sum(t<=x<t+2.00 for x in ts),8)
            self.assertGreaterEqual(ts[-1]-ts[0],2.0)

    def test_existing_corruption_and_wall_clock_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'gate.json'
            a, metrics, warnings=gate(p)
            p.write_text('broken')
            a.take()
            self.assertEqual(len(json.loads(p.read_text())['calls']),1)
            self.assertTrue(any(x[0]=='ACCOUNT_POSITIONS_GATE_RECOVERED' for x in warnings))
            p.write_text(json.dumps({'schema':1,'calls':[time.time()+1000]*8}))
            a.take()
            self.assertEqual(len(json.loads(p.read_text())['calls']),1)
            names=[n for n,_,_ in metrics.values]
            self.assertEqual(names.count('positions_gate_lock_wait'),2)
            self.assertEqual(names.count('positions_gate_quota_wait'),2)
            self.assertTrue(all(ms>=0 for _,ms,_ in metrics.values))

    def test_new_gate_metrics_reach_real_latency_journal(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'latency.jsonl'
            journal = LatencyJournal(path, enabled=True)
            journal.record('positions_gate_lock_wait', 11.125)
            journal.record('positions_gate_quota_wait', 950.25)
            journal.record('sensitive_invalid_phase', 123)
            journal.queue.join()
            entries = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual([x['phase'] for x in entries], [
                'positions_gate_lock_wait', 'positions_gate_quota_wait'])
            self.assertEqual([x['ms'] for x in entries], [11.125, 950.25])
            self.assertEqual(journal.dropped, 0)

    def test_local_fallback_on_unusable_storage(self):
        with tempfile.TemporaryDirectory() as td:
            a, _, warnings=gate(td)
            for _ in range(3): a.take()
            self.assertEqual(len(a._local_times),3)
            self.assertEqual(sum(name=='ACCOUNT_POSITIONS_GATE_DEGRADED' for name,_ in warnings),1)

if __name__ == '__main__':
    unittest.main(verbosity=2)
