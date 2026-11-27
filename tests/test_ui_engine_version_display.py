"""UI engine version must be pinned installed code, NOT config/state version."""
import ast
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import okx_ui

ROOT = Path(__file__).resolve().parents[1]

def release_engine_version():
    tree=ast.parse((ROOT/'okx_version.py').read_text())
    values=[ast.literal_eval(n.value) for n in tree.body
            if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='ENGINE_VERSION' for t in n.targets)]
    assert len(values)==1
    return values[0]

class UiVersionDisplay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.raw=(ROOT/'okx_version.py').read_bytes()
        (self.root/'okx_version.py').write_bytes(self.raw)
        self.ver=release_engine_version()
        self.man=ROOT/'engine_manifest.json'
        entry=json.loads(self.man.read_text())['engines'][self.ver]
        for name in [entry['file']]+entry['dependencies']:
            shutil.copyfile(ROOT/name,self.root/name)
        self.cfg={
            'instrument':'TEST-USDT-SWAP','version':'0.0.0',
            'grid_step':'0.01','take_profit':'0.01','order_size':'1',
            'contract_value':'1','tick_size':'0.01',
            'max_grids':2,'upper_price':'1', 'created_at':1000,
            'active_buy_orders':1,'real_tp_target':1
        }
        (self.root/'config.json').write_text(json.dumps(self.cfg))

    def test_pinned_engine_ignores_older_config_version(self):
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},self.man),'v'+self.ver)

    def test_missing_or_tampered_engine_unknown(self):
        self.assertTrue(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},self.man).startswith('v'))
        (self.root/'okx_version.py').write_bytes(self.raw+b'\n# tampered\n')
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},self.man),'не определена')
        (self.root/'okx_version.py').unlink()
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},self.man),'не определена')

    def test_tampered_engine_or_dependency_is_unknown(self):
        for name in ('okx_bot.py','okx_positions_fifo.py','okx_stats_v2_contract.py'):
            with self.subTest(name=name):
                p=self.root/name
                raw=p.read_bytes()
                p.write_bytes(raw+b'\n# drift\n')
                self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},self.man),'не определена')
                p.write_bytes(raw)
        p=self.root/'okx_positions_fifo.py'
        p.unlink()
        p.symlink_to(ROOT/p.name)
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},self.man),'не определена')

    def test_incomplete_dependency_manifest_is_unknown(self):
        manifest=json.loads(self.man.read_text())
        manifest['engines'][self.ver]['dependency_sha256'].pop('okx_positions_fifo.py')
        path=self.root/'manifest.json';path.write_text(json.dumps(manifest))
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'inactive'},path),'не определена')

    def test_active_requires_matching_process_not_merely_disk_file(self):
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'active','MainPID':'0'},self.man),'не определена')
        self.assertEqual(okx_ui.installed_engine_version(self.root,{'ActiveState':'active','MainPID':'999999999'},self.man),'не определена')

    def test_settings_version_is_installed_engine_not_config(self):
        with patch.object(okx_ui,'systemd_info',return_value={'ActiveState':'inactive'}):
            output=okx_ui.settings('test',self.root,'okx-grid-test.service')
        self.assertIn('v'+self.ver,output)
        self.assertNotIn('v0.0.0',output)
        self.assertIn('Версия движка',output)

    def test_status_version_is_installed_engine_not_config(self):
        with (patch.object(okx_ui,'systemd_info',return_value={'ActiveState':'inactive'}),
              patch.object(okx_ui,'last_local_bid',return_value=None),
              patch.object(okx_ui,'read_monitor',return_value=None),
              patch.object(okx_ui,'_monitor_label',return_value='нет данных'),
              patch.object(okx_ui,'history_multifill',return_value=None),
              patch.object(okx_ui,'uptimes',return_value=(None,None)),
              patch.object(okx_ui,'state_file',return_value=self.root/'missing.json')):
            output=okx_ui.status('test',self.root,'okx-grid-test.service')
        self.assertIn('v'+self.ver,output)
        self.assertNotIn('v0.0.0',output)
        self.assertIn('Версия движка',output)

    def test_version_source_call_sites_have_no_config_fallback(self):
        source=(ROOT/'okx_ui.py').read_text()
        self.assertEqual(source.count('ver=installed_engine_version(root, info)'),2)
        self.assertNotIn('str(cfg.get(\'version\'))',source)

if __name__=='__main__': unittest.main()
