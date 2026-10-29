"""Offline-only new-bot Reader registration regressions: no OKX IO/systemctl/trades."""
import asyncio
import ast
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import okxbot
from okx_position_reader_core import Registration, SharedReaderCore, RegistryError
from okx_position_reader_ipc import ReaderIPCServer

class NoQuota:
    def __init__(self):self.takes=0
    async def take(self, priority_supplier):self.takes+=1
class NoHTTP:
    def __init__(self):self.fetches=0
    async def fetch(self, insts):
        self.fetches+=1
        return {'code':'0','data':[{'instId':i,'pos':'1','posSide':'net'} for i in insts]}

class IPCRegistration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.path=Path(self.t.name);self.path.chmod(0o700)
        self.sock=self.path/'s.sock'
        self.quota=NoQuota();self.http=NoHTTP()
        self.core=SharedReaderCore([Registration('near','NEAR-USDT-SWAP')],self.http,self.quota,account_uid='FAKE_UID')
        self.entries=[Registration('near','NEAR-USDT-SWAP'), Registration('dood','DOOD-USDT-SWAP')]
        self.server=ReaderIPCServer(self.core,self.sock,registry_loader=lambda:self.entries)
        await self.server.start()
    async def asyncTearDown(self):
        await self.server.close();self.t.cleanup()
    async def cmd(self,op,inst='DOOD-USDT-SWAP',bot=None):
        r,w=await asyncio.open_unix_connection(str(self.sock))
        try:
            message={'schema':1,'op':op,'instrument':inst}
            if bot is not None:message['bot']=bot
            w.write((json.dumps(message)+'\n').encode());await w.drain()
            return json.loads(await asyncio.wait_for(r.readline(),2.5))
        finally:
            w.close();await w.wait_closed()
    async def test_readonly_register_before_first_get(self):
        self.assertNotIn('DOOD-USDT-SWAP',self.core.instruments)
        out=await self.cmd('refresh_registry')
        self.assertTrue(out['ok'],out)
        self.assertEqual(out['data']['instrument'],'DOOD-USDT-SWAP')
        self.assertEqual(self.http.fetches,0)
        self.assertEqual(self.quota.takes,0)
        self.assertIn('DOOD-USDT-SWAP',self.core.instruments)
        self.assertTrue((await self.cmd('refresh_registry'))['ok'])
        self.assertEqual(self.http.fetches,0)
    async def test_busy_fails_without_state_change(self):
        self.core._inflight=True
        try:
            out=await self.cmd('refresh_registry')
            self.assertFalse(out['ok'])
            self.assertNotIn('DOOD-USDT-SWAP',self.core.instruments)
        finally:self.core._inflight=False
        self.assertTrue((await self.cmd('refresh_registry'))['ok'])
    async def test_registry_duplicate_does_not_change_active_set(self):
        self.entries=[Registration('near','NEAR-USDT-SWAP'),Registration('bad','NEAR-USDT-SWAP')]
        reply=await self.cmd('refresh_registry')
        self.assertFalse(reply['ok'])
        self.assertEqual(self.core.instruments,('NEAR-USDT-SWAP',))
    async def test_unknown_not_falsely_acknowledged(self):
        reply=await self.cmd('refresh_registry','BTC-USDT-SWAP')
        self.assertFalse(reply['ok'])
        self.assertNotIn('BTC-USDT-SWAP',self.core.instruments)
    async def test_manager_sync_before_first_live_start(self):
        bot=self.path/'bot';bot.mkdir()
        (bot/'.env').write_text('OKX_API_KEY=fake\nOKX_API_SECRET=fake\nOKX_API_PASSPHRASE=fake\nOKX_POSITION_READER_SOCKET='+str(self.sock)+'\n')
        # Patch only socket pathname; simulated setup uses fake credentials.
        with patch.object(okxbot,'READER_SOCKET',str(self.sock)), \
             patch.object(okxbot,'bot_path',return_value=bot):
            ready=await asyncio.to_thread(okxbot.reader_registration_ready,
                                           'dood',{'path':str(bot)},
                                           {'instrument':'DOOD-USDT-SWAP','live_trading':False},timeout_sec=3)
        self.assertTrue(ready)
        self.assertEqual(self.quota.takes,0)
        self.assertEqual(self.http.fetches,0)

    async def test_same_instrument_old_owner_is_not_new_owner(self):
        # Regression: cache contains the SAME pair but belongs to an archived
        # predecessor; a new clone must demand proof of its own registration.
        old=[Registration('near','NEAR-USDT-SWAP'),
             Registration('archived-dood','DOOD-USDT-SWAP')]
        self.core.replace_registrations(old)
        self.entries=[Registration('near','NEAR-USDT-SWAP'),
                      Registration('new-dood','DOOD-USDT-SWAP'),
                      Registration('archived-dood','DOOD-USDT-SWAP',True)]
        reply=await self.cmd('refresh_registry','DOOD-USDT-SWAP',bot='new-dood')
        self.assertTrue(reply['ok'],reply)
        self.assertEqual(reply['data']['bot'],'new-dood')
        self.assertIn('new-dood',[r.name for r in self.core.registrations])
        self.assertNotIn('archived-dood',[r.name for r in self.core.registrations])
        self.assertEqual(self.http.fetches,0)
        self.assertEqual(self.quota.takes,0)

    async def test_same_instrument_wrong_owner_fails_closed(self):
        self.core.replace_registrations([Registration('near','NEAR-USDT-SWAP'),
                                         Registration('old-dood','DOOD-USDT-SWAP')])
        self.entries=[Registration('near','NEAR-USDT-SWAP'),
                      Registration('old-dood','DOOD-USDT-SWAP')]
        reply=await self.cmd('refresh_registry','DOOD-USDT-SWAP',bot='unknown-dood')
        self.assertFalse(reply['ok'],reply)
        self.assertEqual(self.http.fetches,0)
        self.assertEqual(self.quota.takes,0)

    async def test_invalid_instrument_rejected(self):
        reply=await self.cmd('refresh_registry','DOGE-USDC-SWAP')
        self.assertFalse(reply['ok'])
        self.assertEqual(self.http.fetches,0)

class ManagerTemplate(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.master=self.root/'.okxbot_account.env'
        self.master.write_text('OKX_API_KEY=fake\nOKX_API_SECRET=fake\nOKX_API_PASSPHRASE=fake\nOKX_DEMO=0\nOKX_POSITION_READER_SOCKET=/run/okx-position-reader/positions.sock\nOKX_POSITION_READER_QUOTA_SCHEDULING=priority\nOKX_POSITION_READER_PRIORITY_ACK=ALL_BOTS_PRIORITY_AWARE\n')
        self.patch=patch.object(okxbot,'BASE',self.root);self.patch.start()
        self.reader_patch=patch('okx_reader_diagnostics.reader_health',return_value={'scheduling':'priority'})
        self.reader_patch.start()
    def tearDown(self):
        self.reader_patch.stop();self.patch.stop();self.temp.cleanup()
    def test_clone_uses_master_not_archived_source_env(self):
        tree=ast.parse(Path(okxbot.__file__).read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_clone')
        literals=ast.unparse(fn)
        self.assertIn('validated_newbot_env',literals)
        self.assertNotIn("(source / '.env').read_bytes()",literals)
    def test_one_account_master_settings_added_to_new_bot(self):
        custom=self.root/'custom.env'
        custom.write_text('OKX_API_KEY=fake\nOKX_API_SECRET=fake\nOKX_API_PASSPHRASE=fake\nOKX_DEMO=0\nOKX_POSITION_READER_SOCKET=/old.sock\nOKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo\n')
        env,raw=okxbot.validated_newbot_env(custom)
        self.assertEqual(raw.count('OKX_POSITION_READER_SOCKET='),1)
        self.assertIn('OKX_POSITION_READER_SOCKET=/run/okx-position-reader/positions.sock',raw)
        self.assertEqual(raw.count('OKX_POSITION_READER_QUOTA_SCHEDULING='),1)
        self.assertIn('OKX_POSITION_READER_QUOTA_SCHEDULING=priority',raw)
        self.assertEqual(raw.count('OKX_POSITION_READER_PRIORITY_ACK='),1)
        self.assertIn('OKX_POSITION_READER_PRIORITY_ACK=ALL_BOTS_PRIORITY_AWARE',raw)
        self.assertEqual(env['OKX_POSITION_READER_QUOTA_SCHEDULING'],'priority')
        self.assertEqual(env['OKX_POSITION_READER_PRIORITY_ACK'],'ALL_BOTS_PRIORITY_AWARE')
    def test_mismatched_credentials_fail_closed(self):
        other=self.root/'another.env';other.write_text('OKX_API_KEY=other\nOKX_API_SECRET=fake\nOKX_API_PASSPHRASE=fake\n')
        with self.assertRaisesRegex(RuntimeError,'differ'):
            okxbot.validated_newbot_env(other)
    def test_missing_shared_reader_in_master_fails_closed(self):
        self.master.write_text('OKX_API_KEY=fake\nOKX_API_SECRET=fake\nOKX_API_PASSPHRASE=fake\n')
        with self.assertRaisesRegex(RuntimeError,'Shared Reader account template differs from running Reader; no new bot'):
            okxbot.validated_newbot_env(self.master)
    def test_manager_start_has_pretrade_reader_sync(self):
        tree=ast.parse(Path(okxbot.__file__).read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_start')
        calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
        self.assertEqual(sum(n.func.id=='reader_registration_ready' for n in calls),2)

if __name__=='__main__': unittest.main()
