#!/usr/bin/env python3
"""Offline tests for the read-only Telegram alert service; no real requests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import time

SOURCE=Path(__file__).with_name('okx_telegram_monitor.py')
spec=importlib.util.spec_from_file_location('monitor_v0610',SOURCE)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class TelegramMonitorTests(unittest.TestCase):
    def test_config_requires_private_file(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'telegram.env'
            p.write_text('TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJKLMNO\nTELEGRAM_CHAT_ID=-10012345\n')
            p.chmod(0o644)
            with self.assertRaises(RuntimeError):m.read_env(p)
            p.chmod(0o600)
            self.assertEqual(m.read_env(p),('123456:ABCDEFGHIJKLMNO','-10012345'))

    def test_poll_only_authorized_chat_and_read_only_status(self):
        sent=[]
        class API:
            chat='333'
            def call(self,method,params):
                return [{'update_id':10,'message':{'chat':{'id':222},'text':'/status'}},
                        {'update_id':11,'message':{'chat':{'id':333},'text':'/status'}}]
            def send(self,text):sent.append(text)
        st={'schema':1,'updates_offset':0}
        with patch.object(m,'status_report',return_value='OKX test status'),patch.object(m,'save_state'):
            m.poll_commands(API(),st)
        self.assertEqual(sent,['OKX test status'])
        self.assertEqual(st['updates_offset'],12)

    def test_no_historic_log_spam(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);log=base/'logs/bot04_events.jsonl'
            log.parent.mkdir();log.write_text(json.dumps({'event':'ACCOUNT_AUTO_SOFT_STOP'})+'\n')
            class API:
                def send(self,text):raise AssertionError('Historical message spam')
            st={'schema':1,'offsets':{},'last_alerts':{}}
            with patch.object(m,'BOT_DIRS',{'ETH':str(base)}):
                m.ingest_events(API(),st)
                self.assertEqual(st['offsets']['ETH'],log.stat().st_size)

    def test_new_critical_alert_is_sent_once_with_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);log=base/'logs/bot04_events.jsonl'
            log.parent.mkdir();log.write_text('')
            class API:
                def __init__(self):self.sent=[]
                def send(self,text):self.sent.append(text)
            st={'schema':1,'offsets':{'ETH':0},'last_alerts':{}}
            log.write_text(json.dumps({'event':'ACCOUNT_AUTO_SOFT_STOP','code':'51008'})+'\n')
            api=API()
            with patch.object(m,'BOT_DIRS',{'ETH':str(base)}),patch.object(m,'STATE_PATH',base/'saved.json'):
                m.ingest_events(api,st)
                m.ingest_events(api,st)
            self.assertEqual(len(api.sent),1)
            self.assertIn('51008',api.sent[0])

if __name__=='__main__':unittest.main()
