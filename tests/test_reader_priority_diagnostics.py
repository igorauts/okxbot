"""Priority Reader diagnostics contract (version-independent)."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import okx_reader_diagnostics as d

class ReaderPriorityDiagnostic(unittest.TestCase):
    def test_priority_ack_and_ipc_proof(self):
        with tempfile.TemporaryDirectory() as base:
            root=Path(base)
            (root/'config.json').write_text(json.dumps({'instrument':'TEST-USDT-SWAP'}))
            env={'OKX_POSITION_READER_SOCKET':d.SOCKET,
                 'OKX_POSITION_READER_QUOTA_SCHEDULING':'priority',
                 'OKX_POSITION_READER_PRIORITY_ACK':'ALL_BOTS_PRIORITY_AWARE'}
            now=time.time()
            metrics={'last_ok':now-2,'ipc_ok':2,'ipc_fail':0,'direct':0,
                     'last_fail':None,'last_direct':None,'recent_direct':0,
                     'recent_fail':0,'truncated':False}
            health={'instruments':['TEST-USDT-SWAP'],'scheduling':'priority'}
            with patch.object(d,'_reader_env',side_effect=lambda *_:env), patch.object(
                    d,'_latency_summary',side_effect=lambda *a,**k:metrics),patch.object(
                    d,'reader_health',return_value=None):
                def status(h):
                    return d.collect({'bot':root},{'bot':'okx-grid-bot.service'},
                                     health=h,now=now,active=lambda *_:True)['bots'][0]['state']
                self.assertEqual(status(health),'READER OK')
                env.pop('OKX_POSITION_READER_PRIORITY_ACK')
                self.assertEqual(status(health),'CONFIG BYPASS')
                env['OKX_POSITION_READER_PRIORITY_ACK']='ALL_BOTS_PRIORITY_AWARE'
                self.assertEqual(status({'instruments':['TEST-USDT-SWAP'],
                                         'scheduling':'mixed-fifo'}),'CONFIG BYPASS')
                self.assertEqual(status(None),'READER DOWN / UNKNOWN')
                metrics['last_ok']=None
                self.assertEqual(status(health),'CONFIGURED / NO RECENT IPC')
                env['OKX_POSITION_READER_QUOTA_SCHEDULING']='mixed-fifo'
                env.pop('OKX_POSITION_READER_PRIORITY_ACK')
                metrics['last_ok']=now-2
                self.assertEqual(status({'instruments':['TEST-USDT-SWAP'],
                                         'scheduling':'mixed-fifo'}),'READER OK')

if __name__=='__main__':unittest.main()
