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
        self.assertEqual(len(mod.UNITS[:len(mod.BOTS)]),len(mod.BOTS))
        self.assertEqual(len(set(mod.BOTS)),len(mod.BOTS))
        self.assertIn('if len(result)<len(UNITS)',(ROOT/'okx_vps_resource_collector.py').read_text())

    def test_sampler_does_not_call_third_status_fetch(self):
        mod=get('offline_sampler_b','okx_vps_resource_collector.py')
        units={u:{'ActiveState':'active','MainPID':'9','NRestarts':'0'} for u in mod.UNITS}
        g={u:{'cpu_usage_usec':100,'io_bytes':{x:0 for x in ('rbytes','wbytes','rios','wios')}} for u in mod.UNITS}
        ticks=iter(((1.0,[1,1,1,1,1,1,1,1],{'pswpin':0,'pswpout':0},g,units),
                    (1.25,[2,2,2,2,2,2,2,2],{'pswpin':0,'pswpout':0},g,units)))
        with mock.patch.object(mod,'tick',side_effect=lambda:next(ticks)), \
             mock.patch.object(mod,'active_units',side_effect=AssertionError('third systemctl')), \
             mock.patch.object(mod.time,'sleep',return_value=None), \
             mock.patch.object(mod.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2**30)):
            row=mod.sample()
        self.assertEqual(set(row['units']),set(mod.UNITS))

    def test_sampler_refuses_missing_bot(self):
        mod=get('offline_sampler_c','okx_vps_resource_collector.py')
        botunits=mod.UNITS[:len(mod.BOTS)]
        observed={u:{'active':'active'} for u in list(botunits[1:])+list(mod.UNITS[len(mod.BOTS):])}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(mod,'ROOT',Path(td)), \
             mock.patch.object(mod,'sample',return_value={'utc':'test','units':observed}):
            with self.assertRaisesRegex(RuntimeError,'seven-bot'):
                mod.main()
            self.assertFalse(list(Path(td).glob('*.jsonl')))

    def test_sampler_records_inactive_bot(self):
        mod=get('offline_sampler_d','okx_vps_resource_collector.py')
        observed={u:{'active':('inactive' if u == mod.UNITS[0] else 'active')}
                  for u in mod.UNITS[:len(mod.BOTS)]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(mod,'ROOT',Path(td)), \
             mock.patch.object(mod,'sample',return_value={'utc':'test','units':observed}):
            mod.main()
            rows=list(Path(td).glob('*.jsonl'))
            self.assertEqual(len(rows),1)
            self.assertEqual(json.loads(rows[0].read_text())['units'][mod.UNITS[0]]['active'],'inactive')

if __name__ == '__main__':
    unittest.main()
