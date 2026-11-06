"""Offline regression tests for observer compatibility and Telegram durability."""
import json
import os
import queue
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import okx_telegram_health as health
def offline_monitor():
    source=Path(__file__).resolve().parents[1]/'okx_telegram_monitor.py'
    text=source.read_text()
    anchor="_SHARED_CODE = Path('/opt/okx-grid-live')"
    if text.count(anchor)!=1:raise AssertionError('Unexpected bootstrap')
    code=text.replace(anchor,"_SHARED_CODE = Path(__file__).resolve().parent")
    module=types.ModuleType('okx_telegram_v0761_offline')
    module.__file__=str(source)
    exec(compile(code,str(source),'exec'),module.__dict__)
    return module
monitor=offline_monitor()
import okx_telegram_delivery as delivery
import okx_monitor


class CombinedObserverMonitoring(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        for kind in ('burst','execution'):
            p=self.root/kind/'summary.json';p.parent.mkdir();p.write_text('{}')
            os.utime(p,(1000,1000))
        self.st={'schema':1}
        self.alerts=[]
        self.services={'any':'okx-grid-any.service'}
        self.roots={'any':self.root}
        self.combined={'ActiveState':'active','LoadState':'loaded','InvocationID':'one'}
        self.shows={'okx-grid-any.service':{'ActiveState':'active'},
                    'observer-okx-grid-any-combined.service':self.combined}
        self.mock_show=patch.object(health,'_show',side_effect=lambda k:self.shows.get(k));self.mock_show.start()
        self.mock_enabled=patch.object(health,'_observer_enabled',return_value='enabled');self.mock_enabled.start()
        self.mock_one=patch.object(health,'one',side_effect=lambda api,st,key,msg:self.alerts.append((key,msg)));self.mock_one.start()
        self.mock_dir=patch.object(health,'observer_dir',side_effect=lambda root,kind:self.root/kind);self.mock_dir.start()
    def tearDown(self):
        self.mock_show.stop();self.mock_enabled.stop();self.mock_one.stop();self.mock_dir.stop();self.tmp.cleanup()
    def check(self,now):health.observers_check(None,self.st,self.services,self.roots,now)
    def test_combined_loss_alert_and_recovery(self):
        self.check(1001)
        self.shows.pop('observer-okx-grid-any-combined.service')
        for t in (1006,1011,1016,1021):self.check(t)
        faults=[msg for key,msg in self.alerts if '/fault/' in key]
        self.assertEqual(len(faults),1)
        self.assertIn('combined service missing',faults[0])
        self.shows['observer-okx-grid-any-combined.service']=self.combined
        for kind in ('burst','execution'):
            p=self.root/kind/'summary.json';os.utime(p,(1025,1025))
        for t in (1025,1030,1040,1045):self.check(t)
        recoveries=[msg for key,msg in self.alerts if '/normal/' in key]
        self.assertEqual(len(recoveries),1)
    def test_either_summary_stale(self):
        self.check(1001)
        p=self.root/'execution'/'summary.json';os.utime(p,(800,800))
        for t in (1006,1011,1030,1068,1073):self.check(t)
        faults=[msg for key,msg in self.alerts if '/fault/' in key]
        self.assertEqual(len(faults),1)
        self.assertIn('execution',faults[0])
    def test_failed_combined_service_alerts(self):
        self.check(1001)
        self.shows['observer-okx-grid-any-combined.service']={'ActiveState':'failed'}
        for t in (1006,1011,1016,1021):self.check(t)
        alarms=[msg for key,msg in self.alerts if '/fault/' in key]
        self.assertEqual(len(alarms),1)
        self.assertIn('service inactive',alarms[0])

    def test_disabled_combined_not_alerted(self):
        with patch.object(health,'_observer_enabled',return_value='disabled'):
            self.shows['observer-okx-grid-any-combined.service']={'ActiveState':'inactive'}
            for t in (1000,1010,1030):self.check(t)
        self.assertFalse(self.alerts)


class CommandDeadLetter(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'telegram.json'
        delivery._PERSIST_CACHE.clear()
        self.st={'schema':1,'updates_offset':0}
        self.bridge=monitor._CommandBridge.__new__(monitor._CommandBridge)
        self.bridge.done=queue.Queue(maxsize=1)
        self.bridge.ack=queue.Queue(maxsize=1)
        self.saver=patch.object(monitor,'save_state',side_effect=lambda st:delivery.persist(st,self.path));self.saver.start()
        self.state=patch.object(delivery,'STATE_PATH',self.path);self.state.start()
    def tearDown(self):self.saver.stop();self.state.stop();self.tmp.cleanup()
    def failure(self,id=11):
        self.bridge.done.put((id,False))
        self.assertTrue(self.bridge.commit_completion(self.st))
        return self.bridge.ack.get_nowait()
    def test_poison_command_does_not_starve_next(self):
        self.assertIsNone(self.failure())
        self.assertIsNone(self.failure())
        self.assertEqual(self.failure(),12)
        disk=json.loads(self.path.read_text())
        self.assertEqual(disk['updates_offset'],12)
        self.assertEqual(disk['telegram_failed_updates_v1']['11']['attempts'],3)
        self.assertEqual(len(disk['alert_outbox_v0722']),1)
        self.bridge.done.put((12,True));self.assertTrue(self.bridge.commit_completion(self.st))
        self.assertEqual(self.bridge.ack.get_nowait(),13)
    def test_failure_counter_survives_restart(self):
        self.assertIsNone(self.failure())
        self.assertEqual(json.loads(self.path.read_text())['telegram_command_failures_v1']['11']['attempts'],1)
    def test_deadletter_alert_persists_before_offset_commit(self):
        self.assertIsNone(self.failure());self.assertIsNone(self.failure())
        real=delivery.persist
        calls=[]
        def persist_then_fail(st, path=None):
            calls.append(1)
            if len(calls)==2:raise OSError('simulate final ACK failure')
            return real(st,path)
        with patch.object(delivery,'persist',side_effect=persist_then_fail):
            self.bridge.done.put((11,False))
            with self.assertRaises(OSError):self.bridge.commit_completion(self.st)
        self.assertIsNone(self.bridge.ack.get_nowait())
        durable=json.loads(self.path.read_text())
        self.assertEqual(durable['updates_offset'],0)
        self.assertEqual(len(durable['alert_outbox_v0722']),1)

    def test_no_ack_on_deadletter_persist_error(self):
        self.assertIsNone(self.failure());self.assertIsNone(self.failure())
        with patch.object(delivery,'persist',side_effect=OSError('disk full')):
            self.bridge.done.put((11,False))
            with self.assertRaises(OSError):self.bridge.commit_completion(self.st)
        self.assertIsNone(self.bridge.ack.get_nowait())
        self.assertEqual(json.loads(self.path.read_text())['updates_offset'],0)


class TransientMonitorState(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.json';delivery._PERSIST_CACHE.clear()
    def tearDown(self):self.tmp.cleanup()
    def test_cursor_coalesces_but_critical_write_is_immediate(self):
        st={'schema':1,'offsets':{},'incident_sent':{},'updates_offset':0}
        writes=0
        with patch.object(delivery.time,'time',return_value=1000):
            self.assertTrue(delivery.persist(st,self.path));writes+=1
        for i in range(1,25):
            ts=1000+i*5
            st['buy_zero_stream_cursor_v4']='cursor'+str(i)
            st['buy_zero_stream_cursor_at_v4']=ts
            st['buy_zero_stream_health_v4']={'running':True,'observed_at':ts}
            with patch.object(delivery.time,'time',return_value=ts):
                writes+=bool(delivery.persist(st,self.path))
                if i==6:
                    st['incident_sent']['critical']=ts
                    self.assertTrue(delivery.persist(st,self.path))
                    writes+=1
                    self.assertIn('critical',json.loads(self.path.read_text())['incident_sent'])
        self.assertLessEqual(writes,8)
        self.assertGreaterEqual(writes,3)
        saved=json.loads(self.path.read_text())
        self.assertGreaterEqual(saved['buy_zero_stream_cursor_at_v4'],1090)
    def test_heartbeat_only_does_not_force_disk_write(self):
        st={'schema':1,'buy_zero_stream_health_v4':{'running':True,'observed_at':1000}}
        with patch.object(delivery.time,'time',return_value=1000):
            self.assertTrue(delivery.persist(st,self.path))
        for i in range(1,25):
            st['buy_zero_stream_health_v4']['observed_at']=1000+i*5
            with patch.object(delivery.time,'time',return_value=1000+i*5):
                self.assertFalse(delivery.persist(st,self.path))
        self.assertEqual(json.loads(self.path.read_text())['buy_zero_stream_health_v4']['observed_at'],1000)

    def test_command_offset_ignores_cursor_checkpoint_throttle(self):
        st={'schema':1,'updates_offset':0}
        with patch.object(delivery.time,'time',return_value=1000):
            delivery.persist(st,self.path)
        st['buy_zero_stream_cursor_v4']='cursor'
        st['buy_zero_stream_cursor_at_v4']=1005
        st['updates_offset']=99
        with patch.object(delivery.time,'time',return_value=1005):
            self.assertTrue(delivery.persist(st,self.path))
        self.assertEqual(json.loads(self.path.read_text())['updates_offset'],99)

    def test_expire_sent_keys_but_preserve_outbox(self):
        now=10000000
        st={'incident_sent':{'old':1,'queued':1,'recent':now},
            'alert_outbox_v0722':[{'key':'queued'}]}
        delivery.prune_sent(st,now)
        self.assertNotIn('old',st['incident_sent'])
        self.assertIn('queued',st['incident_sent'])
        self.assertIn('recent',st['incident_sent'])


class ObserverUI(unittest.TestCase):
    def test_combined_on_is_visible_for_both_labels(self):
        with patch.object(okx_monitor,'supports_versionless',return_value=True),\
             patch.object(okx_monitor,'_active',side_effect=lambda u:u.endswith('-combined.service')),\
             patch.object(okx_monitor,'_enabled',return_value=False):
            self.assertEqual(okx_monitor.label('okx-grid-any','/not-relevant','burst'),'ON')
            self.assertEqual(okx_monitor.label('okx-grid-any','/not-relevant','execution'),'ON')


class DeploymentSafety(unittest.TestCase):
    def test_legacy_timer_absent_and_retired_installer(self):
        root=Path(__file__).resolve().parents[1]
        self.assertFalse((root/'deploy/reader/okx-position-reader-watchdog.timer').exists())
        unit=(root/'deploy/reader/okx-position-reader-watchdog.service').read_text()
        self.assertIn('Type=simple',unit)
        self.assertIn('--loop',unit)
        import okx_shared_runtime_deploy as dep
        self.assertTrue(dep.WATCHDOG_UNIT.endswith('.service'))
        with self.assertRaisesRegex(RuntimeError,'RETIRED'):
            dep.apply(None)

if __name__=='__main__':unittest.main()
