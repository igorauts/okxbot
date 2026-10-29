"""Reader Telegram DOWN/RECOVERED/FAILED offline regressions. No exchange/Telegram IO."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
extra=Path(__file__).resolve().parents[1]/'baseline'
sys.path.insert(1,str(extra) if extra.is_dir() else os.environ.get('OKX_LIVE_MODULES','/opt/okx-grid-live'))
import okx_telegram_health as h

SOCK='/run/okx-position-reader/positions.sock'
OK={'reader_running':True,'fetch_failed':2,'quota_failed':0}

class ReaderAlertTests(unittest.TestCase):
    def setUp(self):
        self.st={}
        self.sent=[]
        self._env=patch.dict(os.environ,{'OKX_POSITION_READER_SOCKET':SOCK})
        self._send=patch.object(h,'one',side_effect=lambda _api,_st,key,msg:self.sent.append((key,msg)))
        self._env.start();self._send.start()

    def tearDown(self):
        self._send.stop();self._env.stop()

    def poll(self,info=OK,active='active',result='success',restarts=0):
        data={'ActiveState':active,'Result':result,'NRestarts':str(restarts)}
        with patch.object(h,'_show',return_value=data),patch.object(h,'_reader_health_ipc',return_value=info):
            h.position_reader_health_check(None,self.st)

    def keys(self): return [k for k,v in self.sent]
    def test_healthy_initial_arm_without_old_error_warning(self):
        self.poll()
        self.assertEqual(self.keys(),['reader/watchdog/armed'])
        self.assertEqual(self.st['position_reader_health']['fetch_failed'],2)
        self.poll()
        self.assertEqual(self.keys(),['reader/watchdog/armed'])

    def test_down_then_recovered_once_per_episode(self):
        self.poll();self.sent.clear()
        self.poll(info=None,active='activating')
        self.poll(info=None,active='activating')
        self.poll(info=OK,active='active',restarts=1)
        self.poll(info=OK,active='active',restarts=1)
        self.assertEqual(self.keys(),['reader/unavailable/1','reader/recovered/1'])

    def test_failed_once_then_recovered(self):
        self.poll();self.sent.clear()
        self.poll(info=None,active='failed',result='start-limit-hit',restarts=8)
        self.poll(info=None,active='failed',result='start-limit-hit',restarts=8)
        self.assertEqual(self.keys(),['reader/unavailable/1','reader/failed/1'])
        self.assertIn('start-limit-hit',self.sent[1][1])
        self.poll(info=OK,active='active',restarts=8)
        self.assertEqual(self.keys(),['reader/unavailable/1','reader/failed/1','reader/recovered/1'])
        self.poll(info=None,active='inactive',restarts=8)
        self.assertEqual(self.keys()[-1],'reader/unavailable/2')

    def test_fast_crash_between_polls(self):
        self.poll();self.sent.clear()
        self.poll(info=OK,active='active',restarts=1)
        self.assertEqual(self.keys(),['reader/unavailable/1','reader/recovered/1'])
        self.poll(info=OK,active='active',restarts=1)
        self.assertEqual(len(self.sent),2)
        self.poll(info=OK,active='active',restarts=2)
        self.assertEqual(self.keys()[-2:],['reader/unavailable/2','reader/recovered/2'])

    def test_systemd_counter_reset_is_not_a_crash(self):
        self.poll(restarts=8);self.sent.clear()
        self.poll(restarts=0)
        self.assertEqual(self.sent,[])

    def test_new_error_counters_and_no_repeat(self):
        # Reader v4: one GET failure is recorded, not an immediate Telegram alarm.
        with patch.object(h.time,'time',return_value=1000.0):
            self.poll();self.sent.clear()
            self.poll(info={'reader_running':True,'fetch_failed':3,'quota_failed':0})
            self.poll(info={'reader_running':True,'fetch_failed':3,'quota_failed':0})
            self.assertEqual(self.keys(),[])
            # Five new GET failures within 10 minutes create one incident.
            self.poll(info={'reader_running':True,'fetch_failed':7,'quota_failed':0})
            self.assertEqual(self.keys(),['reader/fetch_episode/1'])
            self.assertIn('GET=5, quota=0',self.sent[0][1])
            self.poll(info={'reader_running':True,'fetch_failed':7,'quota_failed':0})
            self.poll(info={'reader_running':True,'fetch_failed':10,'quota_failed':0})
            self.assertEqual(self.keys(),['reader/fetch_episode/1'])

    def test_quota_failure_immediate_without_fetch_failure(self):
        with patch.object(h.time,'time',return_value=1000.0):
            self.poll();self.sent.clear()
            self.poll(info={'reader_running':True,'fetch_failed':2,'quota_failed':1})
            self.assertEqual(self.keys(),['reader/fetch_episode/1'])
            self.assertIn('GET=0, quota=1',self.sent[0][1])
            self.poll(info={'reader_running':True,'fetch_failed':2,'quota_failed':1})
            self.assertEqual(len(self.sent),1)

    def test_error_incident_recovers_once_after_stability(self):
        good={'reader_running':True,'fetch_failed':7,'quota_failed':0}
        with patch.object(h.time,'time',return_value=1000.0):
            self.poll();self.sent.clear()
            self.poll(info=good)
            self.assertEqual(self.keys(),['reader/fetch_episode/1'])
        with patch.object(h.time,'time',return_value=1599.0):
            self.poll(info=good)
            self.assertEqual(self.keys(),['reader/fetch_episode/1'])
        with patch.object(h.time,'time',return_value=1600.0):
            self.poll(info=good)
            self.poll(info=good)
            self.assertEqual(self.keys(),['reader/fetch_episode/1',
                                          'reader/fetch_recovery/1'])
            self.assertIn('инцидент закрыт',self.sent[-1][1])
        with patch.object(h.time,'time',return_value=1610.0):
            self.poll(info={'reader_running':True,'fetch_failed':12,'quota_failed':0})
            self.assertEqual(self.keys()[-1],'reader/fetch_episode/2')

    def test_reader_error_counter_reset_not_reported(self):
        with patch.object(h.time,'time',return_value=1000.0):
            self.poll();self.sent.clear()
            self.poll(info={'reader_running':True,'fetch_failed':0,'quota_failed':0})
            self.assertEqual(self.keys(),[])

    def test_watcher_disabled_without_environment(self):
        with patch.dict(os.environ,{'OKX_POSITION_READER_SOCKET':''}),patch.object(h,'_show') as show:
            h.position_reader_health_check(None,self.st)
            show.assert_not_called()
        self.assertEqual(self.sent,[])

    def test_no_telegram_credential_or_exchange_usage(self):
        self.poll()
        messages=' '.join(v for _,v in self.sent)
        self.assertNotIn('OKX_API_KEY',messages)
        self.assertNotIn('password',messages.lower())

if __name__=='__main__': unittest.main(verbosity=2)
