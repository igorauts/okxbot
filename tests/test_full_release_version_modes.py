"""Full/maintenance version identity contracts; no IO to trading services."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import okx_manager_version as manager
from okx_manager_version import validate_manager_version
from okx_version import ENGINE_VERSION, RELEASE_VERSION as ENGINE_RELEASE_VERSION, RELEASE_KIND, validate_version_ids
from okx_source_attestation import verify_bundle

ROOT=Path(__file__).resolve().parents[1]

def bump(v):
    x=[int(a) for a in v.split('.')]
    x[2]+=1
    return '.'.join(map(str,x))

class ReleaseIdentityContract(unittest.TestCase):
    def test_declared_release_identities(self):
        self.assertEqual(RELEASE_KIND,'engine')
        self.assertIn(manager.RELEASE_KIND, ('engine','maintenance'))
        self.assertEqual(ENGINE_VERSION,ENGINE_RELEASE_VERSION)
        if manager.RELEASE_KIND=='engine':
            self.assertEqual(manager.RELEASE_VERSION,ENGINE_VERSION)
        else:
            self.assertGreater(tuple(map(int,manager.RELEASE_VERSION.split('.'))),
                               tuple(map(int,ENGINE_VERSION.split('.'))))
        self.assertTrue(validate_version_ids())
        self.assertTrue(validate_manager_version())
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertEqual(manifest['current_version'], ENGINE_VERSION)
        self.assertEqual(verify_bundle(ROOT)['engine_version'], ENGINE_VERSION)
    def test_version_mismatch_fails_closed(self):
        with self.assertRaises(RuntimeError):validate_manager_version(bump(manager.RELEASE_VERSION),ENGINE_VERSION)
        if manager.RELEASE_KIND=='maintenance':
            with self.assertRaises(RuntimeError):validate_manager_version(ENGINE_VERSION,ENGINE_VERSION)
        with self.assertRaises(RuntimeError):validate_manager_version(ENGINE_VERSION, bump(ENGINE_VERSION))
        with self.assertRaises(RuntimeError):validate_manager_version(ENGINE_VERSION+'.1',ENGINE_VERSION)
    def test_both_modes_share_one_contract(self):
        # Maintenance is supported for the NEXT manager-only release.
        later=bump(ENGINE_VERSION)
        with patch.object(manager,'RELEASE_KIND','maintenance'), patch.object(manager,'RELEASE_VERSION',later):
            self.assertTrue(validate_manager_version(later,ENGINE_VERSION))
            with self.assertRaises(RuntimeError):validate_manager_version(ENGINE_VERSION,ENGINE_VERSION)
        with patch.object(manager,'RELEASE_KIND','engine'), patch.object(manager,'RELEASE_VERSION',ENGINE_VERSION):
            self.assertTrue(validate_manager_version(ENGINE_VERSION,ENGINE_VERSION))
        with patch.object(manager,'RELEASE_KIND','engine'), patch.object(manager,'RELEASE_VERSION',later):
            with self.assertRaises(RuntimeError):validate_manager_version(later,ENGINE_VERSION)
    def test_manifests_pin_exact_engine(self):
        m=json.loads((ROOT/'engine_manifest.json').read_text())
        b=json.loads((ROOT/'okx_source_bundle.json').read_text())
        self.assertEqual(b['release_version'],manager.RELEASE_VERSION)
        self.assertEqual(b['engine_version'],m['current_version'])
        self.assertEqual(b['engine_dependencies'],m['engines'][m['current_version']]['dependencies'])
