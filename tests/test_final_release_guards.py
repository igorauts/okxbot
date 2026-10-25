"""Audit completion and archive metadata regressions. No release-number pins."""
import importlib.util, os, sys, unittest, zipfile, stat, tempfile
from pathlib import Path
R=Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R))
import okx_live_upgrade as U
from tests.verify_release_asset import verify_member_types

GOOD='AUDIT CONSISTENT\nGRID PROFIT: +$1.0\n✅ Все закрытые циклы сопоставлены.\n'
class ReleaseSafety(unittest.TestCase):
    def test_audit_must_finish_accounting(self):
        self.assertTrue(U.audit_output_complete(0, GOOD))
        for rc,output in [
            (1, GOOD),
            (0, 'AUDIT CONSISTENT\n'),
            (0, '✅ Все закрытые циклы сопоставлены.'),
            (0, GOOD+'\n⛔ STOP: history failure'),
            (0, GOOD+'\n⚠️ Статистика неполная: late failure'),
            (0, GOOD+'\nФинансовые суммы не выводятся.'),
            (0, ''),
            (0, None),
        ]:
            with self.subTest(rc=rc,output=output):
                self.assertFalse(U.audit_output_complete(rc,output))
    def test_zip_member_types_must_be_regular(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'z.zip'
            for kind in (stat.S_IFREG, stat.S_IFLNK, 0):
                with zipfile.ZipFile(p,'w') as z:
                    inf=zipfile.ZipInfo('module.py')
                    inf.create_system=3
                    inf.external_attr=(kind|0o644)<<16
                    z.writestr(inf,b'print(123)\n')
                with zipfile.ZipFile(p) as z:
                    if kind==stat.S_IFREG:
                        self.assertIsNone(verify_member_types(z))
                    else:
                        with self.assertRaisesRegex(ValueError,'Non-regular'):
                            verify_member_types(z)
if __name__=='__main__': unittest.main(verbosity=2)
