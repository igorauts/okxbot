#!/usr/bin/env python3
"""Clean-engine refactor guards. No LIVE access, external API or service operations."""
from pathlib import Path
import importlib.util
import json
import re
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from okx_version import ENGINE_VERSION, STATE_SCHEMA
from okx_positions_fifo import AccountPositionsFIFO, GateUnavailable
from okx_archive_reader import state_file, fills_db

# This is intentionally about RELEASE-NUMBER artifacts, not schema/protocol
# generations: the latter must still be used for fail-closed compatibility.
RELEASE_LITERAL=re.compile(r'(?<!\w)0\.\d+\.\d+(?:\.\d+)?(?!\w)')
VERSIONED_IDENTIFIER=re.compile(r'(?:_v\d{3,5}\b|[-./]v\d{3,5}\b|\bperformance_v\d+\b)')

class CleanEngineRefactorTests(unittest.TestCase):
    def test_release_numbers_not_used_as_compatibility_thresholds(self):
        for name in ('okx_observers.py','okx_panic_cleanup.py'):
            src=(ROOT/name).read_text()
            self.assertNotRegex(src, r'\(0,\s*7,\s*\d+\)')
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertIn(ENGINE_VERSION, manifest['engines'])
        self.assertTrue(all(len(row['sha256'])==64 for row in manifest['engines'].values()))

    def test_financial_writer_reader_dual_wire_contract(self):
        engine=(ROOT/'okx_bot.py').read_text()
        self.assertIn('def performance_snapshot(',engine)
        self.assertIn("'performance': detail",engine)
        self.assertIn("'performance_v0724': detail",engine)
        for name in ('okxbot.py','okx_ui.py','okx_telegram_monitor.py','okx_pnl_diagnostics.py'):
            self.assertIn('performance_v0724',(ROOT/name).read_text(),name)

    def test_shared_fifo_frozen_protocol_is_used_by_all_callers(self):
        from okx_positions_fifo import AccountPositionsFIFO
        for name in ('okx_bot.py','okx_account_ui.py'):
            self.assertIn('.account_positions_gate_v075.json',(ROOT/name).read_text())
        from okx_positions_fifo import FillsHistoryFIFO
        self.assertIn('.account_fills_history_gate_v0712.json',(ROOT/'okx_bot.py').read_text())
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'quota.json';gate=AccountPositionsFIFO(path)
            self.assertEqual(gate.queue_path.name,'quota.json.fifo-v0712.json')
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            self.assertTrue(gate._reserve_if_available(0)[0])
            self.assertEqual(json.loads(path.read_text())['schema'],1)

    def test_existing_monitor_state_schema_and_legacy_keys_stable(self):
        from okx_telegram_wire import state_key
        self.assertEqual(state_key('alert_outbox'),'alert_outbox_v0722')
        self.assertIn("return {'schema':1",(ROOT/'okx_telegram_monitor.py').read_text())
        self.assertIn('latency*.jsonl*',(ROOT/'okx_telegram_views.py').read_text())

    def test_archive_path_selects_existing_bytes_not_declared_version(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);(base/'state').mkdir()
            (base/'config.json').write_text('{"version":"unparseable"}')
            old=base/'state/fills_abandoned.sqlite';old.touch()
            self.assertEqual(fills_db(base),old)
            (base/'state/fills.sqlite').touch()
            self.assertEqual(fills_db(base),base/'state/fills.sqlite')
            (base/'state/fills.sqlite').unlink()
            (base/'state/fills_other.sqlite').touch()
            with self.assertRaises(ValueError):fills_db(base)

    def test_sequential_installer_gated_and_uses_manifest_hashes(self):
        code=(ROOT/'okx_live_upgrade.py').read_text()
        for required in ('--from-engine','--confirm-live-stop',
                         "OLD_ENGINE_SHA256=old_entry.get('sha256')", 'READ_ONLY',
                         'backup_instance(',"run('systemctl','stop'",'AUDIT CONSISTENT'):
            self.assertIn(required,code)
        self.assertTrue((ROOT/'historical/single_transition_upgrader.py').is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
