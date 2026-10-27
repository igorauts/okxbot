import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import okx_position_reader_service as S

class ServiceArchiveRegistry(unittest.TestCase):
    def test_archive_ack_matches_manager_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);bot=base/'rz';(bot/'state').mkdir(parents=True)
            (bot/'config.json').write_text(json.dumps({'instrument':'RLS-USDT-SWAP'}))
            (bot/'state/control_ack.json').write_text(json.dumps({'phase':'PANIC_DONE'}))
            reg=base/'.okxbot_registry.json'
            reg.write_text(json.dumps({'rls0733':{'path':str(bot),'service':'okx-grid-rz','retired':True}}))
            with patch.object(S.subprocess,'run',return_value=type('Return',(),{'returncode':0,'stdout':'LoadState=loaded\nActiveState=inactive\nMainPID=0\n'})()):
                entries=S.load_registry(reg)
                self.assertEqual(len(entries),1)
                self.assertTrue(entries[0].verified_archived)
            # Uncertain active service must keep the slot, never claim archived.
            with patch.object(S.subprocess,'run',return_value=type('Return',(),{'returncode':0,'stdout':'LoadState=loaded\nActiveState=active\nMainPID=4321\n'})()):
                self.assertFalse(S.load_registry(reg)[0].verified_archived)
    def test_terminal_without_ack_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); bot=base/'ra';(bot/'state').mkdir(parents=True)
            (bot/'config.json').write_text(json.dumps({'instrument':'RLS-USDT-SWAP'}))
            reg=base/'.okxbot_registry.json'
            reg.write_text(json.dumps({'rls0733':{'path':str(bot),'service':'okx-grid-ra','retired':True}}))
            with self.assertRaisesRegex(RuntimeError, r"Retired bot.*rls0733.*control_ack.json"):S.load_registry(reg)

if __name__=='__main__': unittest.main()
