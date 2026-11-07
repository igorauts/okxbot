import tempfile
from pathlib import Path
import unittest

from okx_stats_v2_checkpoint import FrozenIndex
from okx_stats_v2_contract import Drift


class CheckpointContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.index = FrozenIndex(self.base/'frozen.sqlite','ZEC-USDT-SWAP')
        self.addCleanup(self.index.close)
        self.cp = self.base/'stats_aggregates_v2.json'

    def test_component_only_once(self):
        self.index.add_verified_component('componentA',['b1','s1'], 'a'*64)
        with self.assertRaisesRegex(Drift, 'already frozen'):
            self.index.add_verified_component('componentA',['b1','s1'], 'a'*64)
        with self.assertRaisesRegex(Drift, 'already belongs'):
            self.index.add_verified_component('componentB',['s1','b2'], 'a'*64)
        self.assertEqual(self.index.lookup('s1'),'componentA')

    def test_index_write_without_json_invalidates_previous_checkpoint(self):
        self.index.finalize_checkpoint(self.cp, {}, {'grid_gain':'1'}, [])
        self.index.add_verified_component('A', ['a-buy', 'a-sell'], 'a'*64)
        with self.assertRaisesRegex(Drift, 'generation mismatch'):
            self.index.verify_checkpoint(self.cp)

    def test_late_fill_rejected_as_anomaly(self):
        self.index.add_verified_component('componentA',['buy1','market1'],'f'*64)
        self.assertEqual(self.index.detect_late_fill({'ordId':'market1','billId':'99'}), 'componentA')
        self.assertIsNone(self.index.detect_late_fill({'ordId':'new','billId':'100'}))

    def test_checkpoint_and_restart(self):
        data=self.index.finalize_checkpoint(self.cp, {'cycles':{'rowid':12}},
                                            {'grid_gain':'10.0012'},[])
        self.assertEqual(self.index.verify_checkpoint(self.cp), data)
        with self.assertRaisesRegex(Drift, 'Stale'):
            self.index.finalize_checkpoint(self.cp, {}, {}, [],expected_generation=0)

    def test_crash_between_index_commit_and_json_is_fail_closed(self):
        self.index.finalize_checkpoint(self.cp, {}, {'grid_gain':'10'},[])
        with self.assertRaisesRegex(RuntimeError, 'Simulated crash'):
            self.index.finalize_checkpoint(self.cp, {}, {'grid_gain':'15'}, [],
                                           inject_crash_after_index_commit=True)
        with self.assertRaisesRegex(Drift, 'generation mismatch'):
            self.index.verify_checkpoint(self.cp)

    def test_foreign_instrument(self):
        with self.assertRaisesRegex(Drift, 'another instrument'):
            wrong=FrozenIndex(self.base/'frozen.sqlite','BLUR-USDT-SWAP')
            wrong.close()

    def test_json_corrupt_fails_closed(self):
        self.index.finalize_checkpoint(self.cp, {}, {'x':'1'}, [])
        self.cp.write_text('bad json')
        with self.assertRaisesRegex(Drift, 'Corrupt checkpoint'):
            self.index.verify_checkpoint(self.cp)

if __name__ == '__main__':
    unittest.main()
