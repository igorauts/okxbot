"""No-network/No-LIVE tests for event-offset durable checkpoint B2."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_telegram_delivery import _PERSIST_CACHE, persist, enqueue
from okx_telegram_wire import state_key


class OffsetsDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.json'
        _PERSIST_CACHE.clear()
        self.st = {'schema': 1, 'offsets': {'bot_a': 100},
                   'updates_offset': 42, 'incident_sent': {}}
        self.write(1000)

    def tearDown(self):
        self.tmp.cleanup()
        _PERSIST_CACHE.clear()

    def write(self, epoch):
        with patch('okx_telegram_delivery.time.time', return_value=epoch):
            return persist(self.st, self.path)

    def disk(self):
        return json.loads(self.path.read_text())

    def test_normal_forward_offsets_checkpoint_30s(self):
        count = 1
        for n in range(1, 25):
            self.st['offsets']['bot_a'] += 17
            count += bool(self.write(1000 + n * 5))
            current = self.disk()['offsets']['bot_a']
            self.assertLessEqual(self.st['offsets']['bot_a'] - current, 17 * 5)
        # 1 initial + first change at 1005 + checkpoint at 1035/1065/1095.
        self.assertEqual(count, 5)
        self.assertEqual(self.disk()['offsets']['bot_a'], 100 + 19 * 17)

    def test_unrelated_critical_writes_do_not_reset_cursor_clock(self):
        self.st['offsets']['bot_a'] += 1
        self.assertTrue(self.write(1005))  # begin checkpoint period
        for n in range(2, 7):
            self.st['offsets']['bot_a'] += 1
            self.st['updates_offset'] += 1
            self.assertTrue(self.write(1000 + n * 5))
            self.assertEqual(self.disk()['offsets']['bot_a'], 101)
            self.assertEqual(self.disk()['updates_offset'], 42 + n - 1)
        self.st['offsets']['bot_a'] += 1
        self.assertTrue(self.write(1035))
        self.assertEqual(self.disk()['offsets']['bot_a'], 107)

    def test_new_bot_cursor_is_committed_immediately(self):
        self.st['offsets']['bot_a'] += 1
        self.assertTrue(self.write(1005))
        self.st['offsets']['bot_b'] = 404
        self.assertTrue(self.write(1010))
        self.assertEqual(self.disk()['offsets']['bot_b'], 404)

    def test_rewind_is_committed_immediately(self):
        self.st['offsets']['bot_a'] += 1
        self.assertTrue(self.write(1005))
        self.st['offsets']['bot_a'] = 0
        self.assertTrue(self.write(1010))
        self.assertEqual(self.disk()['offsets']['bot_a'], 0)

    def test_reader_scan_timestamp_not_durable(self):
        for n in range(1, 20):
            self.st['reader_route_scan_at'] = 1000+n*5
            self.assertFalse(self.write(1000+n*5))
        self.assertNotIn('reader_route_scan_at', self.disk())

    def test_alert_enqueue_is_immediate_during_deferred_offset(self):
        self.st['offsets']['bot_a'] = 101
        self.write(1005)
        self.st['offsets']['bot_a'] = 500
        with patch('okx_telegram_delivery.time.time', return_value=1010):
            self.assertTrue(enqueue(self.st, 'event/bot_a/critical', 'TEST', path=self.path))
        disk=self.disk()
        self.assertEqual(disk['offsets']['bot_a'], 101)
        self.assertEqual(disk[state_key('alert_outbox')][0]['key'], 'event/bot_a/critical')
        self.assertIn('event/bot_a/critical', disk['incident_sent'])
        self.assertEqual(disk['updates_offset'], 42)

    def test_restart_uses_durable_cursor_and_keeps_alert_dedup(self):
        self.st['offsets']['bot_a'] = 101
        self.write(1005)
        self.st['offsets']['bot_a'] = 666
        with patch('okx_telegram_delivery.time.time', return_value=1010):
            enqueue(self.st, 'alarm/unique', 'TEST', path=self.path)
        restored=self.disk()
        self.assertEqual(restored['offsets']['bot_a'], 101)
        self.assertIn('alarm/unique', restored['incident_sent'])
        _PERSIST_CACHE.clear()
        with patch('okx_telegram_delivery.time.time', return_value=1012):
            self.assertFalse(enqueue(restored, 'alarm/unique', 'TEST', path=self.path))
            restored['offsets']['bot_a'] = 800
            self.assertFalse(persist(restored, self.path))
        self.assertEqual(self.disk()['offsets']['bot_a'], 101)

    def test_state_change_fsync_does_not_suppress_telegram_ack(self):
        self.st['offsets']['bot_a'] = 101
        self.write(1005)
        self.st['offsets']['bot_a'] = 102
        self.st['updates_offset'] = 43
        self.assertTrue(self.write(1010))
        self.assertEqual(self.disk()['updates_offset'], 43)
        self.assertEqual(self.disk()['offsets']['bot_a'], 101)

if __name__ == '__main__':
    unittest.main()
