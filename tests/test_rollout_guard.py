"""Focused offline checks for the operator preparation/upgrade stop guards."""
import importlib.util
import os
import fcntl
import threading
import time
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def get(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m
P=get('rollout_preflight');U=get('okx_live_upgrade')
from okx_gate_snapshot import read_gate_snapshot, GateSnapshotUnavailable

class GuardTests(unittest.TestCase):
    def test_aggregated_market_terminal_phase_is_not_blocker(self):
        st={'cycles':{'x':{'state':'TP_WAIT','tpOrders':[{'phase':'ACTIVE'}]}},
            'aggregate_exits':[{'phase':'FILLED'}]}
        self.assertIsNone(P.state_risk(st))
        st['aggregate_exits'][0]['phase']='PLACING'
        self.assertIn('MARKET',P.state_risk(st))

    def test_read_only_preflight_rejects_custom_gate_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'.env').write_text('OKX_ACCOUNT_POSITIONS_GATE_PATH=/tmp/other.json\n')
            (root/'.env').chmod(0o600)
            with self.assertRaises(P.Unsafe):P.env_gate_check(root/'.env')

    def test_live_stop_guard_blocks_unknown_intent_and_custom_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'.env').write_text('OKX_DEMO=0\n');(root/'.env').chmod(0o600)
            st={'cycles':{'x':{'state':'BUY_LIVE','tpOrders':[{'phase':'ACTIVE'}]}},
                'aggregate_exits':[{'phase':'FILLED'}]}
            self.assertTrue(U.stop_safety_readonly(root,st))
            st['cycles']['x']['state']='PLACING'
            with self.assertRaisesRegex(RuntimeError,'BUY intent'):
                U.stop_safety_readonly(root,st)
            st['cycles']['x']['state']='BUY_LIVE'
            st['aggregate_exits'][0]['phase']='ACTIVE'
            with self.assertRaisesRegex(RuntimeError,'MARKET'):
                U.stop_safety_readonly(root,st)
            st['aggregate_exits'][0]['phase']='FILLED'
            (root/'.env').write_text('OKX_FILL_HISTORY_GATE_PATH=/tmp/quota.json\n')
            with self.assertRaisesRegex(RuntimeError,'override'):
                U.stop_safety_readonly(root,st)

    def test_live_inplace_writer_is_not_observed_half_written(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'quota.json'
            p.write_text(json.dumps({'schema':1,'calls':[]}))
            # Simulate the real legacy writer: lock -> truncate -> pause -> write.
            holding=threading.Event()
            def writer():
                fd=os.open(p,os.O_RDWR)
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX)
                    os.ftruncate(fd,0)
                    holding.set()
                    time.sleep(0.07)
                    os.write(fd,b'{"schema":1,"calls":[100]}')
                finally:
                    os.close(fd)
            task=threading.Thread(target=writer)
            task.start()
            self.assertTrue(holding.wait(timeout=1.0))
            try:
                self.assertEqual(read_gate_snapshot(p,'calls')['calls'],[100])
                self.assertEqual(P.quota_schema_check(p),'schema=1')
            finally:
                task.join(timeout=2)
            self.assertFalse(task.is_alive())

    def test_empty_and_invalid_locked_snapshot_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'fifo.json'
            p.write_bytes(b'')
            with self.assertRaises(GateSnapshotUnavailable):read_gate_snapshot(p,'waiters')
            p.write_text('{bad')
            before=p.read_bytes()
            with self.assertRaises(GateSnapshotUnavailable):read_gate_snapshot(p,'waiters')
            self.assertEqual(p.read_bytes(),before)
            p.write_text(json.dumps({'schema':2,'waiters':[]}))
            with self.assertRaises(GateSnapshotUnavailable):read_gate_snapshot(p,'waiters')
            self.assertEqual(read_gate_snapshot(p.parent/'absent.json','waiters',allow_missing=True),None)
            self.assertFalse((p.parent/'absent.json').exists())

    def test_unknown_quota_schema_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'q.json';p.write_text(json.dumps({'schema':2,'calls':[]}))
            old=p.read_bytes()
            with self.assertRaises(P.Unsafe):P.quota_schema_check(p)
            self.assertEqual(p.read_bytes(),old)

if __name__=='__main__':unittest.main(verbosity=2)
