"""Offline manager/engine version split and strict source bundle regression."""
import json
import tempfile
import unittest
from pathlib import Path
import okxbot
import okx_setup
from okx_manager_version import RELEASE_VERSION, validate_manager_version
from okx_version import ENGINE_VERSION
from okx_source_attestation import verify_bundle
ROOT = Path(__file__).resolve().parents[1]
class SplitVersionContract(unittest.TestCase):
    def test_bundle_and_identity(self):
        self.assertTrue(validate_manager_version())
        self.assertEqual(verify_bundle(ROOT)['release_version'], RELEASE_VERSION)
        self.assertEqual(okxbot.latest_engine(ROOT)[1],ENGINE_VERSION)
        self.assertEqual(okxbot.RELEASE_VERSION, RELEASE_VERSION)
        self.assertEqual(okx_setup.MANAGER_RELEASE_VERSION,RELEASE_VERSION)
        self.assertEqual(okx_setup.suggest_bot_name('SOL'), 'sol'+''.join(RELEASE_VERSION.split('.')))
    def test_bad_version_and_tampered_manager_fail_closed(self):
        self.assertTrue(validate_manager_version(ENGINE_VERSION,ENGINE_VERSION))
        import shutil
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)
            manifest=json.loads((ROOT/'okx_source_bundle.json').read_text())
            for filename in [*manifest['files'],'okx_source_bundle.json']:
                shutil.copy2(ROOT/filename,target/filename)
            self.assertEqual(verify_bundle(target)['engine_version'],ENGINE_VERSION)
            with (target/'okx_manager_version.py').open('ab') as f:f.write(b'\n# tampered\n')
            with self.assertRaisesRegex(RuntimeError, 'SOURCE_PIN_MISMATCH'):
                verify_bundle(target)
