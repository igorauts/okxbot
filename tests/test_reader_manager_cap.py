"""Account cap preflight, read-only/unit mocked, no systemd mutation."""
import unittest
from unittest.mock import patch
import okxbot as M

class ManagerCap(unittest.TestCase):
    def test_cap_and_retired(self):
        from pathlib import Path
        import tempfile,json
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);regs={}
            for i in range(10):
                p=base/f'bot{i}';p.mkdir();(p/'config.json').write_text(json.dumps({'instrument':f'A{i}-USDT-SWAP'}))
                regs[f'bot{i}']={'path':str(p),'service':f'okx-grid-bot{i}','retired':False}
            with patch.object(M,'BASE',base),patch.object(M,'service_running',return_value=False),\
                 patch.object(M,'instance_terminal',side_effect=lambda e:e.get('retired',False)):
                self.assertEqual(M.require_account_capacity(regs),10)
                with self.assertRaisesRegex(RuntimeError,'maximum ten'):
                    M.require_account_capacity(regs,proposed_instrument='ANOTHER-USDT-SWAP')
                regs['bot9']['retired']=True
                self.assertEqual(M.require_account_capacity(regs),9)
                self.assertEqual(M.require_account_capacity(regs,proposed_instrument='NEW-USDT-SWAP'),10)
                with self.assertRaisesRegex(RuntimeError,'Only one nonterminal'):
                    M.require_account_capacity(regs,proposed_instrument='A0-USDT-SWAP')
    def test_bad_registry_fails_closed(self):
        with self.assertRaises(RuntimeError):M.require_account_capacity({'bad':{}})

if __name__=='__main__':unittest.main(verbosity=2)
