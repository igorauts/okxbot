"""End-to-end crash/rebuild safety under contradictory late exchange fills.

Only synthetic temporary ledgers, offline bounded ORIGINAL oracle test functions,
no API, no bot state, and no Telegram calls.
"""
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from okx_stats_v2_contract import NotReady, Drift
from okx_stats_v2_production import SingleAuthorityStats
from okx_stats_v2_recovery import eligible, quarantine_sidecar
from tests.stress_stats_v2_rebuild import (
    O, NOW, INST, setup, digest_sources, atom, fill)

class NoNetworkFillStore:
    def ensure_expected(self,expected):
        return []

class HostileLateFill(unittest.TestCase):
    def test_new_executions_for_frozen_order_do_not_publish_wrong_money(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            setup(root,80)
            sidecar=root/'state/stats_v2.sqlite'
            original=SingleAuthorityStats(root,INST,sidecar,oracle=O,
                fill_store=NoNetworkFillStore(),
                expected_filled_orders=lambda *a,**kw:{},
                contract_value=Decimal('1'),freeze_budget=64)
            try:
                for _ in range(3):
                    result=original.compute('110',now=NOW/1000)
                self.assertEqual(result.stats['grid_gain'],Decimal('78.40'))
                self.assertEqual(original.stager.frozen_count(),80)
                late=fill('sell-0','late-bill', '0.01','sell',101)
                with sqlite3.connect(root/'state/fills.sqlite') as db:
                    db.execute('INSERT INTO fills VALUES (?,?,?,?)',
                        (late['ordId'],late['billId'],late['fillSz'],json.dumps(late)))
                authoritative=digest_sources(root)
                with self.assertRaisesRegex(Drift,'LATE_FILL_FROZEN_ORDER'):
                    original.compute('110',now=NOW/1000)
            finally:
                original.close()
            backup=quarantine_sidecar(root,write_json=atom)
            rebuilt=SingleAuthorityStats(root,INST,sidecar,oracle=O,
                fill_store=NoNetworkFillStore(),
                expected_filled_orders=lambda *a,**kw:{},
                contract_value=Decimal('1'),freeze_budget=64)
            try:
                for _ in range(4):
                    with self.assertRaises((Drift,NotReady)):
                        rebuilt.compute('110',now=NOW/1000)
            finally:
                rebuilt.close()
            self.assertTrue(backup.is_file())
            self.assertEqual(authoritative,digest_sources(root))
            self.assertFalse(eligible(root,'LATE_FILL_FROZEN_ORDER',now=2100000000))

if __name__=='__main__':unittest.main()
