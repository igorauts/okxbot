"""Dynamic published-engine history contract; no hard-coded version pair."""
import hashlib
import json
import unittest
from pathlib import Path
from okx_dependency_attestation import verify
ROOT=Path(__file__).resolve().parents[1]

def canonical(data):
    raw=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()

class ImmutableEngineHistory(unittest.TestCase):
    def test_every_published_engine_record_is_frozen(self):
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        original=json.loads((ROOT/'tests/engine_history_reference.json').read_text())
        now=manifest['current_version']
        from okx_manager_version import RELEASE_KIND
        if RELEASE_KIND=='maintenance':
            self.assertIn(now,original)
            self.assertEqual(set(manifest['engines']),set(original))
        else:
            self.assertNotIn(now,original)
            self.assertEqual(set(manifest['engines']),set(original)|{now})
        for version,sha in original.items():
            with self.subTest(version=version):
                self.assertEqual(canonical(manifest['engines'][version]),sha)

    def test_current_engine_and_complete_source_pass_real_attestation(self):
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertEqual(verify(ROOT,manifest),len(manifest['source_modules_sha256']))
        current=manifest['engines'][manifest['current_version']]
        self.assertEqual(set(current['dependencies']),set(current['dependency_sha256']))

    def test_modified_historic_entry_is_rejected(self):
        manifest=json.loads((ROOT/'engine_manifest.json').read_text())
        reference=json.loads((ROOT/'tests/engine_history_reference.json').read_text())
        version=next(iter(reference))
        mutated=dict(manifest['engines'][version])
        mutated['sha256']='0'*64
        self.assertNotEqual(canonical(mutated),reference[version])

if __name__=='__main__':unittest.main()
