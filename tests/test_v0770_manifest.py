"""Read-only source attestation v0.7.70; all files are offline copies."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from okx_dependency_attestation import verify

ROOT=Path(__file__).resolve().parents[1]

class ManifestAttestation(unittest.TestCase):
    def test_full_package_matches_manifest(self):
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertEqual(verify(ROOT,manifest),len(manifest['source_modules_sha256']))
        self.assertIn('okx_stats_v2_production.py',manifest['engines'][manifest['current_version']]['dependency_sha256'])
        self.assertIn('okx_telegram_monitor.py',manifest['source_modules_sha256'])
        self.assertIn('okx_position_reader_service.py',manifest['source_modules_sha256'])

    def test_tampered_engine_dependency_fails_closed(self):
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        engine=manifest['engines'][manifest['current_version']]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            for name in [engine['file']]+engine['dependencies']:
                shutil.copy2(ROOT/name,p/name)
            self.assertEqual(verify(p,manifest,bot_only=True),len(engine['dependencies'])+1)
            target=p/'okx_stats_v2_production.py'
            target.write_bytes(target.read_bytes()+b'\n# drift\n')
            with self.assertRaisesRegex(RuntimeError,'dependency SHA mismatch'):
                verify(p,manifest,bot_only=True)
            target.unlink()
            with self.assertRaisesRegex(RuntimeError,'Missing or symlinked'):
                verify(p,manifest,bot_only=True)

    def test_unlisted_module_fails_closed(self):
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            for module in ROOT.glob('*.py'):
                shutil.copy2(module,p/module.name)
            (p/'unlisted.py').write_text('assert False')
            with self.assertRaisesRegex(RuntimeError,'inventory mismatch'):
                verify(p,manifest)

if __name__=='__main__':unittest.main()
