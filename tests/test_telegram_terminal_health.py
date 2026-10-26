"""Regression: terminal PANIC/HARD restarts never become unconfirmed RUN alarms.

Read-only: mock service status, journal and Telegram sender; no exchange or LIVE data.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import okx_telegram_health as h
from okx_telegram_wire import state_key

class TerminalStartupHealth(unittest.TestCase):
    def test_terminal_modes_suppress_pending_and_new_starts(self):
        for control_mode, phase in [('PANIC','PANIC_DONE'), ('PANIC','WAIT_RECONCILIATION'),
                                    ('HARD','HARD_STOPPED'), ('RUN','PANIC_DONE')]:
            with self.subTest(mode=control_mode, phase=phase), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'state').mkdir()
                (root/'state/control.json').write_text(json.dumps({'mode':control_mode}))
                (root/'state/control_ack.json').write_text(json.dumps({'phase':phase}))
                old_id='prior-start'
                storage={state_key('systemd_watch'):{'dood':{
                    'active':True, 'invocation':'old-invocation',
                    'cursor':'old-journal-cursor',
                    'pending_starts':[{'id':old_id,'since':time.time()-1000}]}}}
                notices=[]
                with patch.object(h,'_show',return_value={'ActiveState':'active','InvocationID':'new-invocation'}), \
                     patch.object(h,'_journal',return_value=[]), \
                     patch.object(h,'one',side_effect=lambda api,st,key,msg:notices.append((key,msg))):
                    h.service_check(None,storage,{'dood':'example.service'},{'dood':root})
                self.assertEqual(notices,[])
                self.assertEqual(storage[state_key('systemd_watch')]['dood']['pending_starts'],[])
    def test_normal_run_still_keeps_start_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'state').mkdir()
            (root/'state/control.json').write_text('{"mode":"RUN"}')
            (root/'state/control_ack.json').write_text('{"phase":"RUNNING"}')
            storage={state_key('systemd_watch'):{'bot':{
                'active':True,'invocation':'old-invocation','cursor':'old'}}}
            with patch.object(h,'_show',return_value={'ActiveState':'active','InvocationID':'new-invocation'}), \
                 patch.object(h,'_journal',return_value=[]), patch.object(h,'one') as once:
                h.service_check(None,storage,{'bot':'example.service'},{'bot':root})
            self.assertEqual(len(storage[state_key('systemd_watch')]['bot']['pending_starts']),1)
            self.assertFalse(any('unconfirmed' in x.args[2] for x in once.call_args_list))

if __name__=='__main__':
    unittest.main(verbosity=2)
