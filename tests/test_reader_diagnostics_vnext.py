"""Offline, GET-free account Reader diagnostics and Telegram incident regressions.

No version literals. All IO uses temporary fixtures or mocked local services.
"""
import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import okxbot
import okx_reader_diagnostics as diag
import okx_telegram_health as health
from okx_position_reader_core import Registration, SharedReaderCore
from okx_position_reader_ipc import ReaderIPCServer
from okx_version import ENGINE_VERSION, RELEASE_VERSION, RELEASE_KIND, validate_version_ids


class _OfflineQuota:
    def __init__(self): self.n=0
    async def take(self, supplier): self.n+=1


class _OfflineProvider:
    def __init__(self): self.n=0
    async def fetch(self, instruments):
        self.n+=1
        return {'code':'0','data':[{'instId':x,'pos':'1','posSide':'net'} for x in instruments]}


class DiagnosticsFixture(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.root=Path(self.dir.name)
        self.now=time.time()
        self.bots={}
        self.units={}
    def tearDown(self): self.dir.cleanup()
    def add(self, name, instrument, *, config=True, registered=True, ok=0, direct=0, fail=0, minutes_ago=0):
        root=self.root/name
        (root/'logs').mkdir(parents=True,exist_ok=True)
        (root/'state').mkdir(exist_ok=True)
        (root/'config.json').write_text(json.dumps({'instrument':instrument}))
        data='OKX_API_KEY=FAKE_SECRET\n'
        if config:
            data+='OKX_POSITION_READER_SOCKET='+diag.SOCKET+'\nOKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo\n'
        (root/'.env').write_text(data)
        ts=datetime.fromtimestamp(self.now-60*minutes_ago, timezone.utc).isoformat()
        rows=([{'ts':ts,'phase':'position_reader_ipc','outcome':'ok','ms':1}]*ok
              +[{'ts':ts,'phase':'position_reader_ipc','outcome':'unavailable','ms':1}]*fail
              +[{'ts':ts,'phase':'positions_gate','route':diag.POSITIONS_ROUTE,'ms':1}]*direct)
        (root/'logs/latency.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
        self.bots[name]=root
        self.units[name]='okx-grid-'+name+'.service'
        return root
    def snap(self, instruments, **kw):
        h={'reader_running':True,'fetch_failed':0,'quota_failed':0,'pending':0,
           'instruments':instruments}
        return diag.collect(self.bots,self.units,health=h,now=self.now,active=lambda unit:True,**kw)


class AccountReport(DiagnosticsFixture):
    def test_single_account_report_with_four_bots(self):
        instruments=[]
        for name in ('near','wld','btc','dood'):
            instrument=name.upper()+'-USDT-SWAP'
            instruments.append(instrument)
            self.add(name,instrument,ok=7,direct=1)
        data=self.snap(instruments)
        self.assertEqual(len(data['bots']),4)
        self.assertTrue(all(b['state']=='READER OK' for b in data['bots']))
        text=diag.report(data)
        self.assertEqual(text.count('Активных ботов:'),1)
        self.assertIn('GET OKX: 0',text)
        self.assertIn('Reader IPC OK/ошибки: 7/0',text)
        self.assertIn('Прямой GET',text)
        self.assertNotIn('FAKE_SECRET',text)
    def test_missing_config_detected_even_if_log_has_direct_reads(self):
        self.add('dood','DOOD-USDT-SWAP',config=False,direct=12)
        row=self.snap(['DOOD-USDT-SWAP'])['bots'][0]
        self.assertEqual(row['state'],'CONFIG BYPASS')
    def test_registry_missing_detected_without_exchange_get(self):
        self.add('dood','DOOD-USDT-SWAP',ok=0,fail=5,direct=8)
        self.assertEqual(self.snap([])['bots'][0]['state'],'UNREGISTERED')
    def test_temp_fallback_does_not_trigger_sustained_state(self):
        self.add('dood','DOOD-USDT-SWAP',ok=4,fail=1,direct=2)
        self.assertEqual(self.snap(['DOOD-USDT-SWAP'])['bots'][0]['state'],'READER OK')
    def test_flat_bot_failures_are_ambiguous_for_telegram(self):
        self.add('dood','DOOD-USDT-SWAP',fail=5,direct=8)
        self.assertEqual(self.snap(['DOOD-USDT-SWAP'])['bots'][0]['state'],'SUSTAINED FALLBACK')
    def test_closed_bot_excluded(self):
        p=self.add('dood','DOOD-USDT-SWAP',ok=1)
        (p/'state/final_stats.json').write_text('{}')
        self.assertEqual(self.snap(['DOOD-USDT-SWAP'])['bots'],[])
    def test_secrets_never_in_report(self):
        self.add('dood','DOOD-USDT-SWAP',ok=2)
        txt=diag.report(self.snap(['DOOD-USDT-SWAP']))
        self.assertNotIn('FAKE_SECRET',txt)
    def test_invalid_config_unknown(self):
        root=self.add('dood','DOOD-USDT-SWAP')
        (root/'config.json').write_text('{broken')
        self.assertEqual(self.snap([])['bots'][0]['state'],'UNKNOWN CONFIG')


class TelegramBypass(DiagnosticsFixture):
    def setUp(self):
        super().setUp()
        self.add('dood','DOOD-USDT-SWAP',config=False,direct=10)
        self.messages=[]
        self.state={}
    def call(self, seconds, instruments=('DOOD-USDT-SWAP',)):
        h={'reader_running':True,'fetch_failed':0,'quota_failed':0,'pending':0,
           'instruments':list(instruments)}
        snap=diag.collect(self.bots,self.units,now=self.now+seconds,
                          health=h,active=lambda unit:True)
        with patch('okx_reader_diagnostics.collect',return_value=snap),\
             patch('okx_telegram_health.one',side_effect=lambda api,st,key,msg:self.messages.append((key,msg))):
            health.reader_bypass_check(None,self.state,self.units,self.bots,now=self.now+seconds)
    def test_one_warning_one_recovery_no_repeats(self):
        self.call(0); self.call(31)
        self.assertEqual(len(self.messages),0)
        self.call(65)
        self.assertEqual(len(self.messages),1)
        self.assertIn('BYPASS',self.messages[0][1])
        self.call(100); self.call(140)
        self.assertEqual(len(self.messages),1)
        # Restore configuration and supply an actual successful IPC sample.
        p=self.bots['dood']
        (p/'.env').write_text('OKX_POSITION_READER_SOCKET='+diag.SOCKET+'\nOKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo\n')
        ts=datetime.fromtimestamp(self.now+150,timezone.utc).isoformat()
        (p/'logs/latency.jsonl').write_text(json.dumps({'ts':ts,'phase':'position_reader_ipc','outcome':'ok','ms':1})+'\n')
        self.call(171);self.call(202);self.call(233)
        self.assertEqual(len(self.messages),2)
        self.assertIn('RECOVERED',self.messages[1][1])
        self.call(270)
        self.assertEqual(len(self.messages),2)
    def test_paused_bot_has_no_reader_bypass_warning(self):
        p=self.bots['dood'];(p/'state/control.json').write_text('{"mode":"PAUSE"}')
        self.call(0);self.call(65);self.call(160)
        self.assertEqual(self.messages,[])

    def test_no_message_when_reader_down(self):
        with patch('okx_reader_diagnostics.collect',return_value={'health':None,'bots':[]}),\
             patch('okx_telegram_health.one',side_effect=AssertionError('unexpected')):
            health.reader_bypass_check(None,self.state,self.units,self.bots,now=self.now+90)
        self.assertEqual(self.messages,[])
    def test_unknown_instrument_episode(self):
        p=self.bots['dood'];(p/'.env').write_text('OKX_POSITION_READER_SOCKET='+diag.SOCKET+'\nOKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo\n')
        self.call(0,instruments=())
        self.call(65,instruments=())
        self.assertEqual(len(self.messages),1)
        self.assertIn('не зарегистрирована',self.messages[0][1])
    def test_sustained_fallback_does_not_spam_flat_bot(self):
        p=self.bots['dood'];(p/'.env').write_text('OKX_POSITION_READER_SOCKET='+diag.SOCKET+'\nOKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo\n')
        self.add('dood','DOOD-USDT-SWAP',config=True,ok=0,fail=5,direct=15)
        self.call(0);self.call(65);self.call(130)
        self.assertEqual(self.messages,[])


class IPCHealth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.d=tempfile.TemporaryDirectory()
        self.sock=Path(self.d.name)/'reader.sock'
        Path(self.d.name).chmod(0o700)
        self.quota=_OfflineQuota();self.provider=_OfflineProvider()
        self.core=SharedReaderCore([Registration('near','NEAR-USDT-SWAP')],
                                   self.provider,self.quota,account_uid='FAKE_UID')
        self.server=ReaderIPCServer(self.core,self.sock,registry_loader=lambda:[Registration('near','NEAR-USDT-SWAP'),Registration('dood','DOOD-USDT-SWAP')])
        await self.server.start()
    async def asyncTearDown(self):
        await self.server.close();self.d.cleanup()
    async def call(self, op, instrument=None):
        r,w=await asyncio.open_unix_connection(str(self.sock))
        try:
            request={'schema':1,'op':op}
            if instrument:request['instrument']=instrument
            w.write((json.dumps(request)+'\n').encode());await w.drain()
            return json.loads(await r.readline())
        finally:
            w.close();await w.wait_closed()
    async def test_health_readonly_lists_registration(self):
        reply=await self.call('health')
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['data']['instruments'],['NEAR-USDT-SWAP'])
        self.assertEqual(self.provider.n,0)
        self.assertEqual(self.quota.n,0)
        self.assertTrue((await self.call('refresh_registry','DOOD-USDT-SWAP'))['ok'])
        reply=await self.call('health')
        self.assertEqual(set(reply['data']['instruments']),{'NEAR-USDT-SWAP','DOOD-USDT-SWAP'})
        self.assertEqual(self.provider.n,0)


class ReleaseMetadata(unittest.TestCase):
    def test_three_part_package_and_engine_only(self):
        self.assertEqual(len(RELEASE_VERSION.split('.')),3)
        self.assertEqual(len(ENGINE_VERSION.split('.')),3)
        self.assertTrue(validate_version_ids())
        self.assertNotEqual(RELEASE_VERSION,ENGINE_VERSION)
        self.assertEqual(RELEASE_KIND,'maintenance')
        with self.assertRaises(RuntimeError):
            validate_version_ids(RELEASE_VERSION+'.1',ENGINE_VERSION)
    def test_reader_error_new_episode_after_long_quiet_period(self):
        import okx_telegram_health as h
        sent=[];state={}
        props={'ActiveState':'active','NRestarts':'0'}
        values={'reader_running':True,'fetch_failed':0,'quota_failed':0}
        now=time.time()
        with patch.object(h,'_reader_health_ipc',side_effect=lambda _:dict(values)), \
             patch.object(h,'_show',return_value=props), \
             patch.object(h,'one',side_effect=lambda api,st,k,text:sent.append(text)), \
             patch.dict(__import__('os').environ,{'OKX_POSITION_READER_SOCKET':diag.SOCKET}):
            with patch.object(h.time,'time',return_value=now):
                h.position_reader_health_check(None,state)
            values['fetch_failed']=1
            with patch.object(h.time,'time',return_value=now+1):
                h.position_reader_health_check(None,state)
            values['fetch_failed']=2
            with patch.object(h.time,'time',return_value=now+1800):
                h.position_reader_health_check(None,state)
        alerts=[x for x in sent if 'GET/quota' in x]
        self.assertEqual(len(alerts),2)
        self.assertTrue(all('новый эпизод' in x for x in alerts),alerts)

    def test_manager_has_single_account_diagnostics_menu(self):
        source=Path(okxbot.__file__).read_text()
        self.assertEqual(source.count("'🔎 Reader Diagnostics (весь аккаунт)'"),1)
        self.assertIn("sub.add_parser('reader-diagnostics'",source)

if __name__=='__main__':unittest.main()
