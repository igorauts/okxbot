"""Offline, mocked systemd dynamic CLI discovery; no production service calls."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

BASE=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import okx_per_bot_speed_readonly as m


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=pathlib.Path(self.tmp.name)
        self.paths={}
        for bot in ('alpha','some_new_bot','unpredictable-xy'):
            path=self.base/bot
            path.mkdir()
            (path/'config.json').write_text('{}')
            (path/'logs').mkdir()
            self.paths[bot]=path

    def runner(self, args):
        if 'list-units' in args:
            return '\n'.join('okx-grid-'+name+'.service loaded active running' for name in self.paths)+'\n'
        name=args[2].removeprefix('okx-grid-').removesuffix('.service')
        return 'ActiveState=active\nMainPID=987\nWorkingDirectory='+str(self.paths[name])+'\n'

    def test_dynamic_service_count_and_names(self):
        found=m.discover_running_bots(runner=self.runner,base=self.base)
        self.assertEqual(found,sorted(self.paths.items()))

    def test_discovery_is_subset_of_active_services(self):
        def runner(args):
            if 'list-units' in args:
                return 'okx-grid-some_new_bot.service loaded active running\n'
            return self.runner(args)
        self.assertEqual(m.discover_running_bots(runner=runner,base=self.base),
                         [('some_new_bot',self.paths['some_new_bot'])])

    def test_rejects_wrong_service_working_directory(self):
        def runner(args):
            if 'list-units' in args:
                return 'okx-grid-alpha.service loaded active running\n'
            return 'ActiveState=active\nMainPID=123\nWorkingDirectory='+str(self.paths['some_new_bot'])+'\n'
        with self.assertRaisesRegex(RuntimeError,'mismatch'):
            m.discover_running_bots(runner=runner,base=self.base)

    def test_rejects_non_running_service_and_pid_zero(self):
        def runner(args):
            if 'list-units' in args:return 'okx-grid-alpha.service loaded active running\n'
            return 'ActiveState=active\nMainPID=0\nWorkingDirectory='+str(self.paths['alpha'])+'\n'
        with self.assertRaisesRegex(RuntimeError,'PID'):
            m.discover_running_bots(runner=runner,base=self.base)

    def test_no_bots_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError,'No active'):
            m.discover_running_bots(runner=lambda args:'',base=self.base)

    def test_dynamic_main_reads_paths_without_hardcoded_bot_names(self):
        with mock.patch.object(m,'discover_running_bots',return_value=[('alpha',self.paths['alpha'])]):
            with mock.patch.object(sys,'argv',['cli','--minutes','15']):
                with mock.patch.object(m.time,'time',return_value=1800000000):
                    out=io.StringIO()
                    with contextlib.redirect_stdout(out):m.main()
        result=out.getvalue()
        self.assertIn('alpha',result)
        self.assertIn('END READ ONLY',result)
        self.assertNotIn('near0721',result)

    def test_monitor_executes_source_sibling_not_versioned_srv_script(self):
        text=(BASE/'okx_telegram_monitor.py').read_text()
        self.assertIn("with_name('okx_per_bot_speed_readonly.py')",text)
        self.assertNotIn('okx_v0747_per_bot_speed_readonly.py',text)

if __name__=='__main__':unittest.main(verbosity=2)
