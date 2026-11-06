"""Version-neutral, offline-only S1 maintenance regression tests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def get(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class S1Contracts(unittest.TestCase):
    def test_watchdog_deploy_copy(self):
        self.assertEqual((ROOT/'okx_position_reader_watchdog.py').read_bytes(),
                         (ROOT/'deploy/reader/okx_position_reader_watchdog.py').read_bytes())

    def test_watchdog_stable_skip_and_heartbeat(self):
        mod = get('offline_watchdog_a', 'okx_position_reader_watchdog.py')
        now = time.time()
        prev = {'pid':'12','fails':0,'restarts':[], 'last_check':now-20}
        current = dict(prev, last_check=now)
        self.assertFalse(mod.should_persist_state(prev,current,healthy=True,now=now))
        prev['last_check'] = now-mod.HEALTHY_HEARTBEAT_SECONDS
        self.assertTrue(mod.should_persist_state(prev,current,healthy=True,now=now))

    def test_watchdog_durable_on_change(self):
        mod = get('offline_watchdog_b', 'okx_position_reader_watchdog.py')
        now = time.time()
        prev = {'pid':'12','fails':0,'restarts':[], 'last_check':now-20}
        curr = dict(prev, last_check=now)
        self.assertTrue(mod.should_persist_state(prev,dict(curr,pid='13'),healthy=True,now=now))
        self.assertTrue(mod.should_persist_state(prev,dict(curr,fails=1),healthy=False,now=now))
        self.assertTrue(mod.should_persist_state(prev,curr,healthy=False,now=now))

    def test_watchdog_failure_threshold(self):
        mod = get('offline_watchdog_c','okx_position_reader_watchdog.py')
        now = time.time()
        state={'pid':'12','fails':0,'restarts':[],'last_check':now-20}
        for i in range(mod.MIN_FAILURES):
            state, eligible = mod.policy(state,now=now+i,pid='12',healthy=False)
            self.assertEqual(eligible, i+1 >= mod.MIN_FAILURES)

    def test_sampler_units_completeness(self):
        mod=get('offline_sampler_a','okx_vps_resource_collector.py')
        self.assertFalse(hasattr(mod,'BOTS'))
        self.assertFalse(hasattr(mod,'UNITS'))
        self.assertTrue(hasattr(mod,'discover_units'))
        answers=iter((
            types.SimpleNamespace(returncode=0,stdout=(
                'okx-grid-testalpha.service loaded active running Test\n'
                'okx-grid-archived.service loaded inactive dead Archived\n')),
            types.SimpleNamespace(returncode=0,stdout=(
                'okx-grid-testbeta.service enabled enabled\n'
                'okx-grid-archived.service disabled enabled\n'))))
        # Isolate the sampler's hourly cache from the host. Otherwise a warm
        # LIVE cache bypasses the mocked list-unit-files response and makes
        # this test depend on whatever services the VPS currently runs.
        with tempfile.TemporaryDirectory() as td:
            cache_root=Path(td)
            with mock.patch.object(mod,'ROOT',cache_root), \
                 mock.patch.object(mod,'UNIT_FILE_CACHE',cache_root/'enabled_unit_files_cache_v1.json'), \
                 mock.patch.object(mod.subprocess,'run',side_effect=lambda *a,**k:next(answers)) as run:
                units=mod.discover_units()
            self.assertEqual(run.call_count,2, 'Cold-cache discovery must query both systemd lists')
            self.assertTrue((cache_root/'enabled_unit_files_cache_v1.json').exists())
        self.assertIn('okx-grid-testalpha.service',units)
        self.assertIn('okx-grid-testbeta.service',units)
        self.assertNotIn('okx-grid-archived.service',units)
        self.assertEqual(set(units[-len(mod.BASE_UNITS):]),set(mod.BASE_UNITS))

    def test_sampler_does_not_call_third_status_fetch(self):
        mod=get('offline_sampler_b','okx_vps_resource_collector.py')
        units=('okx-grid-testalpha.service',)+mod.BASE_UNITS
        g={u:{'cpu_usage_usec':100,'io_bytes':{k:0 for k in ('rbytes','wbytes','rios','wios')}} for u in units}
        states={u:{'ActiveState':'active','MainPID':'9','NRestarts':'0'} for u in units}
        ticks=iter(((1.0,[1,1,1,1,1,1,1,1],{'pswpin':0,'pswpout':0},g,states),
                    (1.25,[2,2,2,2,2,2,2,2],{'pswpin':0,'pswpout':0},g,states)))
        calls=[]
        def observed_tick(given):
            calls.append(given)
            return next(ticks)
        with mock.patch.object(mod,'discover_units',return_value=units), \
             mock.patch.object(mod,'tick',side_effect=observed_tick), \
             mock.patch.object(mod,'active_units',side_effect=AssertionError('third status')), \
             mock.patch.object(mod.time,'sleep',return_value=None), \
             mock.patch.object(mod.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2**30)):
            row=mod.sample()
        self.assertEqual(calls,[units,units])
        self.assertEqual(set(row['units']),set(units))

    def test_sampler_refuses_missing_bot(self):
        mod=get('offline_sampler_c','okx_vps_resource_collector.py')
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(mod,'ROOT',Path(td)), \
             mock.patch.object(mod,'sample',return_value={'utc':'test','units':{}}):
            with self.assertRaisesRegex(RuntimeError,'No dynamically discovered'):
                mod.main()
            self.assertFalse(list(Path(td).glob('*.jsonl')))

    def test_sampler_records_inactive_bot(self):
        mod=get('offline_sampler_d','okx_vps_resource_collector.py')
        unit='okx-grid-testalpha.service'
        observed={unit:{'active':'inactive'}}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(mod,'ROOT',Path(td)), \
             mock.patch.object(mod,'sample',return_value={'utc':'test','units':observed}), \
             mock.patch('builtins.print'):
            mod.main()
            rows=list(Path(td).glob('*.jsonl'))
            self.assertEqual(len(rows),1)
            self.assertEqual(json.loads(rows[0].read_text())['units'][unit]['active'],'inactive')

if __name__ == '__main__':
    unittest.main()
