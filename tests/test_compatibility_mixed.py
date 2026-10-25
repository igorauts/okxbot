"""Adversarial offline assertions for account mixed-runtime compatibility.
No OKX, systemd, VPS, secrets, live process, or network contact.
"""
import hashlib
import gzip
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
LEGACY_FIFO=ROOT/'tests/previous_positions_fifo.py'
LEGACY_ENGINE_BYTES=gzip.decompress((ROOT/'tests/previous_engine.py.gz').read_bytes())

from okx_positions_fifo import AccountPositionsFIFO, FillsHistoryFIFO
from okx_engine_integrity import attested_engine
from okx_telegram_wire import state_key
import okx_observers,okx_panic_cleanup,okx_telegram_views,okx_telegram_delivery


def previous_module(filename):
    spec=importlib.util.spec_from_file_location('previous_'+filename, LEGACY_FIFO)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def offline_monitor():
    import types
    original=(ROOT/'okx_telegram_monitor.py').read_text()
    anchor="_SHARED_CODE = Path('/opt/okx-grid-live')"
    assert original.count(anchor)==1
    adapted=original.replace(anchor, "_SHARED_CODE = Path(__file__).resolve().parent")
    module=types.ModuleType('monitor_compatibility_test')
    module.__file__=str(ROOT/'okx_telegram_monitor.py')
    exec(compile(adapted,module.__file__,'exec'),module.__dict__)
    return module


class Mixed(unittest.TestCase):
    def test_shared_positions_and_fills_paths_are_the_same(self):
        previous=previous_module('okx_positions_fifo.py')
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'.account_positions_gate_v075.json'
            p.write_text(json.dumps({'schema':1,'calls':[]}))
            old=previous.AccountPositionsFIFO(p);new=AccountPositionsFIFO(p)
            self.assertEqual(old.path,new.path)
            self.assertEqual(old.queue_path,new.queue_path)
            self.assertEqual(old.MAX_SHARED,new.MAX_SHARED)
            grants=[(client._reserve_if_available(time.time()))[0] for client in ([old,new]*8)]
            self.assertEqual(sum(grants),old.MAX_SHARED)
            self.assertEqual(json.loads(p.read_text())['schema'],1)
            f=Path(temp)/'.account_fills_history_gate_v0712.json'
            self.assertEqual(previous.FillsHistoryFIFO(f).path,FillsHistoryFIFO(f).path)

    def test_account_fifo_thread_contention(self):
        # Same exact flock inode/quota under concurrent old + new contenders.
        import multiprocessing as mp
        previous=previous_module('okx_positions_fifo.py')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'rate.json'
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            gates=[previous.AccountPositionsFIFO(path), AccountPositionsFIFO(path)]
            # flock is process-wide but each thread opens a separate fd; exercising
            # lock competition and persistence without touching the real account.
            with ThreadPoolExecutor(max_workers=16) as pool:
                rows=list(pool.map(lambda k:gates[k%2]._reserve_if_available(time.time())[0],range(16)))
            self.assertEqual(sum(rows),AccountPositionsFIFO.MAX_SHARED)

    def test_two_os_processes_old_new_share_quota_inode(self):
        import subprocess
        child = r"""
import importlib.util,json,sys,time
from pathlib import Path
p=Path(sys.argv[1]); modulepath=Path(sys.argv[2]);
spec=importlib.util.spec_from_file_location('gatechild',modulepath)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
gate=m.AccountPositionsFIFO(p)
count=0
for i in range(8):
    count+=bool(gate._reserve_if_available(time.time())[0])
print(json.dumps({'grants':count,'path':str(gate.path),'queue':str(gate.queue_path)}))
"""
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'shared_positions.json';p.write_text(json.dumps({'schema':1,'calls':[]}))
            procs=[]
            for source in (LEGACY_FIFO,ROOT/'okx_positions_fifo.py'):
                procs.append(subprocess.Popen([sys.executable,'-B','-c',child,str(p),str(source)],
                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            rows=[]
            for childproc in procs:
                stdout,stderr=childproc.communicate(timeout=8)
                self.assertEqual(childproc.returncode,0,stderr)
                rows.append(json.loads(stdout))
            self.assertEqual(sum(r['grants'] for r in rows),AccountPositionsFIFO.MAX_SHARED)
            self.assertEqual(rows[0]['queue'],rows[1]['queue'])
            self.assertEqual(json.loads(p.read_text())['schema'],1)

    def test_persisted_telegram_survives_exactly_with_critical_alert(self):
        monitor=offline_monitor()
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'telegram_monitor_state.json'
            state={'schema':1,'updates_offset':748,'offsets':{'near':419},
                   'incident_sent':{'panic/near':123.0},
                   state_key('alert_outbox'):[{'key':'critical/panic','text':'PANIC!',
                         'html':False,'created':200,'tries':0,'retry_at':0}],
                   state_key('common_engine_stall'):{'active':True,'members':['near']},
                   state_key('systemd_watch'):{'near':{'cursor':'123'}}}
            f.write_text(json.dumps(state),encoding='utf-8')
            before=f.read_bytes()
            with patch.object(monitor,'STATE_PATH',f):
                actual=monitor.load_state()
            self.assertEqual(actual,state)
            self.assertEqual(f.read_bytes(),before)  # read-only startup
            class API:
                def __init__(self):self.sent=[]
                def send(self,text,parse_mode=None):self.sent.append(text)
            api=API()
            self.assertEqual(okx_telegram_delivery.drain(api,actual,path=f,now=time.time()+1),1)
            self.assertEqual(api.sent,['PANIC!'])
            after=json.loads(f.read_text())
            self.assertEqual(after['updates_offset'],748)
            self.assertEqual(after['offsets'],{'near':419})
            self.assertEqual(after[state_key('alert_outbox')],[])
            self.assertEqual(after[state_key('common_engine_stall')],state[state_key('common_engine_stall')])

    def test_engine_attestation_legacy_new_and_tampered(self):
        from okx_version import ENGINE_VERSION
        m=json.loads((ROOT/'engine_manifest.json').read_text())
        self.assertIn(hashlib.sha256(LEGACY_ENGINE_BYTES).hexdigest(),
                      [v.get('sha256') for v in m['engines'].values()])
        with tempfile.TemporaryDirectory() as td:
            bots=Path(td);bot=bots/'near';bot.mkdir();(bot/'state').mkdir()
            versions=list(m['engines']);self.assertEqual(len(versions),2)
            for version,source in [(v,None if v!=ENGINE_VERSION else ROOT/'okx_bot.py') for v in versions]:
                cfg={'version':version,'instrument':'NEAR-USDT-SWAP'}
                st={'version':version,'schema_version':1,'instrument':'NEAR-USDT-SWAP',
                    'terminal_mode':'PANIC_DONE',
                    'terminal_snapshot':{'position_contracts':'0','mode':'PANIC_DONE'}}
                (bot/'config.json').write_text(json.dumps(cfg))
                (bot/'state/bot_state.json').write_text(json.dumps(st))
                (bot/'okx_bot.py').write_bytes(LEGACY_ENGINE_BYTES if source is None else source.read_bytes())
                self.assertTrue(attested_engine(bot,cfg,st))
                self.assertTrue(okx_observers.supports_versionless(bot,'okx-grid-near',bots))
                (bot/'state/control.json').write_text('{"mode":"PANIC"}')
                (bot/'state/control_ack.json').write_text('{"mode":"PANIC","phase":"PANIC_DONE","remaining_position":"0"}')
                self.assertTrue(okx_panic_cleanup.confirmed_panic(bot))
                (bot/'okx_bot.py').write_bytes(b'evil')
                self.assertFalse(attested_engine(bot,cfg,st))
                self.assertFalse(okx_observers.supports_versionless(bot,'okx-grid-near',bots))
                self.assertFalse(okx_panic_cleanup.confirmed_panic(bot))

    def test_latency_rotated_log_included(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'logs').mkdir()
            (root/'config.json').write_text('{"active_buy_orders":10}')
            now=datetime.now(timezone.utc).isoformat()
            (root/'logs/latency.jsonl.1').write_text(json.dumps({'ts':now,'phase':'http','ms':25})+'\n')
            with patch.object(okx_telegram_views,'engine_count_history',return_value=[]), \
                 patch.object(okx_telegram_views,'observer_dir',return_value=root/'empty'):
                rendered=okx_telegram_views.latency_one('near',root,1)
            self.assertIn('Записей latency: 1',rendered)

if __name__=='__main__':unittest.main(verbosity=2)
