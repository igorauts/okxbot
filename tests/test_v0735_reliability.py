"""Offline-only regression of revised public WS latch, registry and Reader contract.
No live config, private credentials, exchange requests, systemctl changes or orders.
"""
import json
import subprocess
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.test_rc9_ws_wake_trace import watcher_class
import okx_position_reader_service as reader
import okx_position_reader_core as core

class Clock:
    def __init__(self): self.t=10000.0
    def monotonic(self): return self.t
    def time(self): return self.t

class WakeHysteresis(unittest.TestCase):
    def test_rapid_bid_retraces_do_not_flood_and_keep_first_stamp(self):
        ns=watcher_class(); clock=Clock();ns['time']=clock
        w=ns['PublicBidWatcher']()
        w._virtual_targets={'a':Decimal('100')}
        fired=0
        for i in range(100):
            clock.t+=.2
            w._capture_crossings(Decimal('99'))
            fired+=int(w._capture_crossings(Decimal('100')))
        self.assertLessEqual(fired,21)
        self.assertGreaterEqual(fired,19)
        self.assertEqual(w._virtual_crossed['a'],10000.2)

    def test_new_distinct_cycle_and_changed_tp_wake_immediately(self):
        ns=watcher_class();clock=Clock();ns['time']=clock
        w=ns['PublicBidWatcher']()
        w._virtual_targets={'a':Decimal('100')}
        self.assertTrue(w._capture_crossings(Decimal('100')))
        clock.t+=0.1
        w._capture_crossings(Decimal('99'))
        self.assertFalse(w._capture_crossings(Decimal('100')))
        # Separate cycle crossing is not suppressed by another cycle's cooldown.
        w._virtual_targets['b']=Decimal('101')
        self.assertTrue(w._capture_crossings(Decimal('101')))
        # Changed TP is a new epoch with fresh wake, not old cycle history.
        w.connected=False
        w.set_virtual_targets({'a':Decimal('102'),'b':Decimal('101')})
        self.assertNotIn('a',w._virtual_last_wake)
        self.assertTrue(w._capture_crossings(Decimal('102')))

class TerminalUnit(unittest.TestCase):
    def mock_unit(self,load='not-found',active='inactive',pid=0):
        return patch.object(reader.subprocess,'run',return_value=subprocess.CompletedProcess(
            ['systemctl','show'],0,stdout=(f'LoadState={load}\nActiveState={active}\nMainPID={pid}\n')))

    def test_missing_unit_archived_only_with_terminal_ack_and_no_process(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);arch=root/'old';active=root/'new'
            for p in (arch,active):
                (p/'state').mkdir(parents=True)
                (p/'config.json').write_text('{"instrument":"ZEC-USDT-SWAP"}')
            (arch/'state/control_ack.json').write_text('{"phase":"PANIC_DONE"}')
            reg=root/'registry.json'
            reg.write_text(json.dumps({'old':{'path':str(arch),'service':'okx-grid-old'},
                                       'new':{'path':str(active),'service':'okx-grid-new'}}))
            with self.mock_unit(), patch.object(reader,'_bot_engine_process_absent',return_value=True):
                rows=reader.load_registry(reg)
            self.assertEqual([r.name for r in core.validate_registrations(rows)],['new'])
            with self.mock_unit(), patch.object(reader,'_bot_engine_process_absent',return_value=False):
                rows=reader.load_registry(reg)
                with self.assertRaises(core.RegistryError):core.validate_registrations(rows)
            (arch/'state/control_ack.json').write_text('{"phase":"RUNNING"}')
            with self.mock_unit(), patch.object(reader,'_bot_engine_process_absent',return_value=True):
                rows=reader.load_registry(reg)
                with self.assertRaises(core.RegistryError):core.validate_registrations(rows)

    def test_failed_unit_must_have_zero_pid_and_absent_process(self):
        with self.mock_unit(load='loaded',active='failed',pid=123),patch.object(reader,'_bot_engine_process_absent',return_value=True):
            self.assertFalse(reader._terminal_unit_inactive('okx-grid-old',Path('/tmp/old')))
        with self.mock_unit(load='loaded',active='failed',pid=0),patch.object(reader,'_bot_engine_process_absent',return_value=True):
            self.assertTrue(reader._terminal_unit_inactive('okx-grid-old',Path('/tmp/old')))

if __name__=='__main__': unittest.main()
