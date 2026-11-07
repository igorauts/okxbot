"""Offline durable Telegram incident delivery, retry, dedupe, recovery."""
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from okx_telegram_delivery import drain
from okx_telegram_events import event_alert
from okx_telegram_wire import state_key

class FakeTelegram:
    def __init__(self, fail_once=False):
        self.messages=[]
        self.fail_once=fail_once
    def send(self,text,parse_mode=None):
        if self.fail_once:
            self.fail_once=False
            raise TimeoutError('Simulated Telegram timeout (ACK unknown)')
        self.messages.append(text)

class StatsIncidentDelivery(unittest.TestCase):
    def test_durable_queue_survives_timeout_restart_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'tg-state.json'
            st={}
            fake=FakeTelegram(fail_once=True)
            clock=time.time()
            with mock.patch('okx_telegram_delivery.STATE_PATH',path):
                event={'ts':'2026-11-06T22:00:00+00:00','event':'STATS_V2_DRIFT',
                       'reason':'Drift: LATE_FILL_FROZEN_ORDER'}
                event_alert(fake,st,'beat0762',event)
                event_alert(fake,st,'beat0762',event)
                self.assertEqual(len(st[state_key('alert_outbox')]),1)
                self.assertTrue(path.is_file())
                self.assertEqual(drain(fake,st,path=path,now=clock+1),0)
                self.assertEqual(len(st[state_key('alert_outbox')]),1)
                self.assertEqual(fake.messages,[])
                # Full monitor restart reloads durable outbox and incident epoch.
                restored=json.loads(path.read_text())
                self.assertEqual(drain(fake,restored,path=path,now=clock+8),1)
                self.assertEqual(len(fake.messages),1)
                self.assertIn('STATS_V2_DRIFT',fake.messages[0])
                event_alert(fake,restored,'beat0762',
                    {'ts':'2026-11-06T22:00:20+00:00','event':'STATS_V2_RECOVERED'})
                self.assertEqual(drain(fake,restored,path=path,now=clock+15),1)
                self.assertEqual(len(fake.messages),2)
                self.assertIn('STATS_V2_RECOVERED',fake.messages[1])

    def test_stalled_queue_incident_dedup_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'monitor.json'
            st={};fake=FakeTelegram()
            with mock.patch('okx_telegram_delivery.STATE_PATH',path):
                event_alert(fake,st,'blur0738',{'event':'STATS_V2_STALLED',
                    'reason':'Pending freeze no-progress threshold'})
                event_alert(fake,st,'blur0738',{'event':'STATS_V2_STALLED',
                    'reason':'Pending freeze no-progress threshold'})
                self.assertEqual(len(st[state_key('alert_outbox')]),1)
                self.assertEqual(drain(fake,st,path=path),1)
                event_alert(fake,st,'blur0738',{'event':'STATS_V2_QUEUE_RECOVERED'})
                self.assertEqual(drain(fake,st,path=path),1)
                self.assertIn('STATS_V2_QUEUE_RECOVERED',fake.messages[-1])

if __name__=='__main__':unittest.main()
