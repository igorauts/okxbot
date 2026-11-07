"""Fault-injected crash tests for StatsWorker sidecar quarantine (offline)."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from okx_stats_v2_recovery import (
    RebuildRefused, eligible, quarantine_sidecar, resume_interrupted,
    finish_rebuild, REBUILD_TRIGGER, PHASE_COMPLETED)

class RecoveryCrashCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.original = self.state / 'stats_v2.sqlite'
        db = sqlite3.connect(self.original)
        db.execute('CREATE TABLE evidence (value TEXT)')
        db.execute('INSERT INTO evidence VALUES (?)', ('original_money',))
        db.commit(); db.close()
        self.original_bytes = self.original.read_bytes()
        self.now = 1800000000.123456

    @staticmethod
    def write_json(path, obj):
        tmp = path.with_name(path.name + '.new')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(obj, f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def _marker(self):
        return json.loads((self.state / 'stats_v2_recovery.json').read_text())

    def _backup(self):
        return self.state / self._marker()['quarantine']

    def test_normal_quarantine_and_final_success(self):
        backup = quarantine_sidecar(self.root, now=self.now, write_json=self.write_json)
        self.assertFalse(self.original.exists())
        self.assertEqual(backup.read_bytes(),self.original_bytes)
        self.assertFalse(eligible(self.root, REBUILD_TRIGGER,now=self.now+1))
        self.assertFalse(eligible(self.root, REBUILD_TRIGGER,now=self.now+7*3600))
        self.assertEqual(resume_interrupted(self.root,write_json=self.write_json)['phase'],'QUARANTINED')
        db=sqlite3.connect(self.original);db.execute('CREATE TABLE fresh (value TEXT)');db.commit();db.close()
        self.assertTrue(finish_rebuild(self.root,write_json=self.write_json))
        self.assertFalse(finish_rebuild(self.root,write_json=self.write_json))
        self.assertEqual(self._marker()['phase'],PHASE_COMPLETED)
        self.assertTrue(eligible(self.root,REBUILD_TRIGGER,now=self.now+7*3600))
        self.assertEqual(backup.read_bytes(),self.original_bytes)

    def test_completed_rebuild_can_restart_after_old_derived_backup_cleanup(self):
        backup=quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)
        self.original.write_bytes(b'verified_fresh_sqlite_sidecar')
        self.assertTrue(finish_rebuild(self.root,write_json=self.write_json))
        backup.unlink()
        self.assertEqual(resume_interrupted(self.root,write_json=self.write_json)['phase'],
                         PHASE_COMPLETED)
        self.assertTrue(eligible(self.root,REBUILD_TRIGGER,now=self.now+7*3600))

    def test_crash_after_prepared_before_rename_replays_once(self):
        actual_replace=os.replace
        def injected(src,dst):
            if str(src)==str(self.original) and '.quarantine.' in str(dst):
                raise RuntimeError('POWER LOSS BETWEEN PREPARE AND RENAME')
            return actual_replace(src,dst)
        with mock.patch('okx_stats_v2_recovery.os.replace',side_effect=injected):
            with self.assertRaisesRegex(RuntimeError,'POWER LOSS'):
                quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)
        self.assertEqual(self._marker()['phase'],'PREPARED')
        self.assertEqual(self.original.read_bytes(),self.original_bytes)
        resume_interrupted(self.root,write_json=self.write_json)
        self.assertEqual(self._marker()['phase'],'QUARANTINED')
        self.assertEqual(self._backup().read_bytes(),self.original_bytes)
        self.assertFalse(self.original.exists())

    def test_crash_after_rename_before_quarantined_record_recovers(self):
        calls=[]
        def write_fault(path,obj):
            calls.append(obj['phase'])
            if obj['phase']=='QUARANTINED':
                raise RuntimeError('POWER LOSS BEFORE FINAL MARKER')
            self.write_json(path,obj)
        with self.assertRaisesRegex(RuntimeError,'POWER LOSS'):
            quarantine_sidecar(self.root,now=self.now,write_json=write_fault)
        self.assertEqual(calls,['PREPARED','QUARANTINED'])
        self.assertEqual(self._marker()['phase'],'PREPARED')
        self.assertFalse(self.original.exists())
        self.assertEqual(self._backup().read_bytes(),self.original_bytes)
        resume_interrupted(self.root,write_json=self.write_json)
        self.assertEqual(self._marker()['phase'],'QUARANTINED')

    def test_fresh_sidecar_started_before_interrupted_marker_finalization(self):
        def write_fault(path,obj):
            if obj['phase']=='QUARANTINED':raise RuntimeError('simulated crash')
            self.write_json(path,obj)
        with self.assertRaisesRegex(RuntimeError,'simulated crash'):
            quarantine_sidecar(self.root,now=self.now,write_json=write_fault)
        self.original.write_bytes(b'fresh_partial_rebuild')
        resume_interrupted(self.root,write_json=self.write_json)
        self.assertEqual(self.original.read_bytes(),b'fresh_partial_rebuild')
        self.assertEqual(self._backup().read_bytes(),self.original_bytes)

    def test_foreign_sidecar_replacement_refuses_any_rename(self):
        actual_replace=os.replace
        def injected(src,dst):
            if '.quarantine.' in str(dst):raise RuntimeError('before rename')
            return actual_replace(src,dst)
        with mock.patch('okx_stats_v2_recovery.os.replace',side_effect=injected):
            with self.assertRaisesRegex(RuntimeError,'before rename'):
                quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)
        self.original.unlink()
        self.original.write_bytes(b'foreign')
        with self.assertRaises(RebuildRefused):
            resume_interrupted(self.root,write_json=self.write_json)
        self.assertEqual(self.original.read_bytes(),b'foreign')

    def test_missing_original_and_backup_refuses(self):
        actual_replace=os.replace
        def fail(src,dst):
            if '.quarantine.' in str(dst):raise RuntimeError('before rename')
            return actual_replace(src,dst)
        with mock.patch('okx_stats_v2_recovery.os.replace',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'before rename'):
                quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)
        self.original.unlink()
        with self.assertRaises(RebuildRefused):resume_interrupted(self.root,write_json=self.write_json)

    def test_wal_or_symlink_preserves_original(self):
        (self.state/'stats_v2.sqlite-wal').write_text('unused')
        with self.assertRaises(RebuildRefused):
            quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)
        (self.state/'stats_v2.sqlite-wal').unlink()
        self.original.unlink()
        self.original.symlink_to(self.state/'elsewhere')
        with self.assertRaises(RebuildRefused):
            quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)

    def test_failed_publication_cannot_mark_completed(self):
        backup=quarantine_sidecar(self.root,now=self.now,write_json=self.write_json)
        with self.assertRaises(RebuildRefused):finish_rebuild(self.root,write_json=self.write_json)
        self.assertFalse(eligible(self.root,REBUILD_TRIGGER,now=self.now+24*3600))
        self.assertTrue(backup.is_file())

if __name__=='__main__': unittest.main()
