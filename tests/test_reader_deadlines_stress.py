"""Reader offline cancellation, bounded IPC, and contention regression suite.

No OKX, VPS, credentials, or trading actions. These tests model local
monotonic deadlines and original schema-1 account quota under real locks.
"""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_position_reader_core import (
    Priority, Registration, SharedReaderCore, QuotaRequestAbandoned,
)
from okx_position_reader_client import read_positions, PositionReaderUnavailable
from okx_position_reader_service import SharedOriginalQuota
from okx_positions_fifo import AccountPositionsFIFO
from okx_positions_priority import PriorityPositionsFIFO, EMERGENCY, NORMAL

UID = 'OFFLINE_ACCOUNT_ONLY'

class NeverFetch:
    def __init__(self):
        self.calls = 0
    async def fetch(self, instruments):
        self.calls += 1
        return {'code':'0','data':[]}

class LiveQuotaCancel(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_clients_do_not_hold_phantom_quota_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            gate_path = Path(d)/'quota.json'
            gate_path.write_text(json.dumps({'schema':1,'calls':[]}))
            old = AccountPositionsFIFO(gate_path)
            for _ in range(old.MAX_SHARED):
                self.assertTrue(old._reserve_if_available(time.time())[0])
            initial = json.loads(gate_path.read_text())['calls']
            provider = NeverFetch()
            reader = SharedReaderCore(
                [Registration('zec','ZEC-USDT-SWAP')],
                provider, SharedOriginalQuota(gate_path), account_uid=UID,
                quota_timeout_sec=3.0)
            await reader.start()
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await reader.request('ZEC-USDT-SWAP',Priority.EXIT,
                                         account_uid=UID,timeout_sec=.15)
                # Original file-lock queue, not just a fake test API: the
                # thread must observe all clients leaving and cancel ticket.
                deadline = time.monotonic() + 1.2
                while time.monotonic() < deadline:
                    await asyncio.sleep(.03)
                    if reader.counters['quota_abandoned']:
                        break
                self.assertEqual(reader.counters['quota_abandoned'],1)
                self.assertEqual(reader.counters['unused_quota_grants'],0)
                self.assertEqual(provider.calls,0)
                self.assertEqual(json.loads(gate_path.read_text())['calls'], initial)
                queue_path=old.queue_path
                self.assertFalse(queue_path.exists() and
                                 json.loads(queue_path.read_text())['waiters'])
            finally:
                await reader.close()

    async def test_cancel_while_quota_waiting_releases_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'quota.json'
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            old=AccountPositionsFIFO(path)
            for _ in range(old.MAX_SHARED):
                old._reserve_if_available(time.time())
            provider=NeverFetch()
            core=SharedReaderCore([Registration('zec','ZEC-USDT-SWAP')],
                 provider,SharedOriginalQuota(path),account_uid=UID,quota_timeout_sec=3.0)
            await core.start()
            try:
                task=asyncio.create_task(core.request('ZEC-USDT-SWAP',
                    Priority.EXIT,account_uid=UID,timeout_sec=2.0))
                await asyncio.sleep(.07)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                deadline=time.monotonic()+1.3
                while time.monotonic()<deadline and not core.counters['quota_abandoned']:
                    await asyncio.sleep(.03)
                self.assertEqual(core.counters['quota_abandoned'],1)
                self.assertEqual(provider.calls,0)
                self.assertFalse(old.queue_path.exists() and
                                 json.loads(old.queue_path.read_text())['waiters'])
            finally:
                await core.close()

class TotalIPCDeadline(unittest.TestCase):
    def test_drip_feed_cannot_extend_five_second_budget(self):
        with tempfile.TemporaryDirectory() as d:
            parent=Path(d); parent.chmod(0o700)
            timeout_values=[]
            recv_count=[]
            class FakeSocket:
                def __enter__(self):return self
                def __exit__(self,*args):return None
                def settimeout(self,v):timeout_values.append(v)
                def connect(self,p):pass
                def sendall(self,data):pass
                def recv(self,count):
                    recv_count.append(count)
                    return b'x'
            class Clock:
                n=0
                @classmethod
                def monotonic(cls):
                    cls.n+=1
                    return 100+.60*cls.n
            # Socket sends one byte at a time without newline, fooling a
            # per-recv timeout. New client must enforce a TOTAL deadline.
            with patch('okx_position_reader_client.socket.socket',return_value=FakeSocket()), \
                 patch('okx_position_reader_client.time.monotonic',Clock.monotonic):
                with self.assertRaisesRegex(PositionReaderUnavailable,'total IPC deadline'):
                    read_positions(parent/'reader.sock','ZEC-USDT-SWAP',account_uid=UID)
            self.assertGreater(len(recv_count),1)
            self.assertLess(len(recv_count),12)
            self.assertTrue(all(v>0 for v in timeout_values))
            self.assertLess(timeout_values[-1],timeout_values[0])

class PriorityContention(unittest.TestCase):
    def test_ten_waiters_same_quota_file_emergency_not_behind_normal(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'quota.json'
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            gate=AccountPositionsFIFO(path)
            for _ in range(gate.MAX_SHARED):gate._reserve_if_available(time.time())
            # Use actual 10 distinct threads (same socket-less FIFO/lock),
            # no mocks for filesystem/quota. All wait > first window.
            results=[]
            lock=threading.Lock()
            def worker(priority, tag):
                obj=PriorityPositionsFIFO(path)
                try:
                    obj.take_prioritized(lambda:priority)
                    state='ok'
                except Exception as exc:
                    state=type(exc).__name__
                with lock:results.append((time.monotonic(),priority,tag,state))
            threads=[threading.Thread(target=worker,args=(NORMAL,i),daemon=True)
                     for i in range(9)]
            for t in threads:t.start()
            time.sleep(.25)
            urgent=threading.Thread(target=worker,args=(EMERGENCY,99),daemon=True)
            urgent.start()
            for t in threads+[urgent]:t.join(timeout=9)
            self.assertTrue(all(not t.is_alive() for t in threads+[urgent]))
            self.assertTrue(all(r[3]=='ok' for r in results),results)
            results.sort()
            self.assertEqual(results[0][1],EMERGENCY,results)
            self.assertEqual(json.loads(gate.queue_path.read_text())['waiters'],[])
            calls=json.loads(path.read_text())['calls']
            self.assertLessEqual(len(calls),gate.MAX_SHARED)

if __name__=='__main__':unittest.main()

class BatchTelemetry(unittest.IsolatedAsyncioTestCase):
    async def test_five_clients_receive_same_batch_observability(self):
        from okx_position_reader_ipc import ReaderIPCServer
        from okx_position_reader_client import read_positions
        class HaltedQuota:
            def __init__(self): self.allow=asyncio.Event();self.calls=0
            async def take(self, priority):
                self.calls+=1
                await self.allow.wait()
        class MultiProvider:
            def __init__(self):self.calls=0
            async def fetch(self, inst):
                self.calls+=1
                return {'code':'0','data':[{'instId':i,'posSide':'net','pos':'2'}
                                            for i in inst]}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);root.chmod(0o700);sock=root/'r.sock'
            coins=['ZEC','BTC','NEAR','WLD','RLS']
            quota=HaltedQuota();prov=MultiProvider()
            core=SharedReaderCore([Registration(c,c+'-USDT-SWAP') for c in coins],
                      prov,quota,account_uid=UID)
            server=ReaderIPCServer(core,sock)
            await server.start()
            try:
                seen={}
                async def client(coin):
                    received=[]
                    trace='trace'+coin.lower()
                    rows=await asyncio.to_thread(read_positions,sock,coin+'-USDT-SWAP',
                                     account_uid=UID,trace_id=trace,
                                     on_trace=lambda fields:received.append(fields))
                    seen[coin]=(rows,received)
                tasks=[asyncio.create_task(client(coin)) for coin in coins]
                deadline=time.monotonic()+1.0
                while len(core._pending())<len(coins) and time.monotonic()<deadline:
                    await asyncio.sleep(.01)
                self.assertEqual(len(core._pending()),5)
                quota.allow.set()
                await asyncio.gather(*tasks)
                self.assertEqual(prov.calls,1)
                self.assertEqual(quota.calls,1)
                self.assertEqual(core.counters['coalesced_clients'],5)
                for coin in coins:
                    rows,diagnostics=seen[coin]
                    self.assertEqual(rows[0]['instId'],coin+'-USDT-SWAP')
                    self.assertEqual(len(diagnostics),1)
                    self.assertEqual(diagnostics[0]['batch_size'],5)
                    self.assertGreaterEqual(diagnostics[0]['quota_wait_ms'],0)
                    self.assertGreaterEqual(diagnostics[0]['fetch_ms'],0)
            finally:
                await server.close()

class TelemetryAllowlist(unittest.TestCase):
    def test_reader_trace_metrics_accepted_without_sensitive_fields(self):
        from okx_latency import LatencyJournal
        with tempfile.TemporaryDirectory() as d:
            journal=LatencyJournal(Path(d)/'latency.jsonl')
            journal._thread=object()  # Capture in-memory; do not spawn I/O
            journal.record('position_reader_quota', 1350.3, trace_id='abc123',
                    priority='exit',batch_size=4, secret='SHOULD_NOT_LOG')
            data=journal.queue.get_nowait();journal.queue.task_done()
            self.assertEqual(data['phase'],'position_reader_quota')
            self.assertEqual(data['trace_id'],'abc123')
            self.assertEqual(data['ms'],1350.3)
            self.assertNotIn('secret',data)
            journal.record('position_reader_http',18,trace_id='bad/token')
            data=journal.queue.get_nowait();journal.queue.task_done()
            self.assertNotIn('trace_id',data)

def _multiprocess_mixed_gate(path, new, priority, result):
    """Real separate process, no network: use one original lock inode."""
    from okx_positions_fifo import AccountPositionsFIFO
    from okx_positions_priority import PriorityPositionsFIFO
    import time
    try:
        if new:
            PriorityPositionsFIFO(path).take_prioritized(lambda:priority)
        else:
            AccountPositionsFIFO(path).take()
        result.put(('ok', time.monotonic(), new, priority))
    except Exception as exc:
        result.put((type(exc).__name__, time.monotonic(), new, priority))


class MixedVersionMultiprocess(unittest.TestCase):
    def test_ten_real_processes_one_original_account_quota(self):
        import multiprocessing
        from okx_positions_priority import EXIT, BACKGROUND
        ctx=multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'quota.json'
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            gate=AccountPositionsFIFO(path)
            # Force real contention: both original and new clients must
            # wait for the same quota to refill. No separate token bucket.
            for _ in range(gate.MAX_SHARED):
                self.assertTrue(gate._reserve_if_available(time.time())[0])
            out=ctx.Queue()
            modes=[(False,NORMAL)]*4+[(True,NORMAL)]*3+[(True,EXIT)]*2+[(True,BACKGROUND)]
            procs=[]
            try:
                for new,priority in modes:
                    p=ctx.Process(target=_multiprocess_mixed_gate,
                                  args=(str(path),new,priority,out))
                    p.start();procs.append(p)
                for p in procs:p.join(timeout=11)
                self.assertTrue(all(not p.is_alive() for p in procs),
                                'Gate process exceeded bounded wait')
                items=[out.get(timeout=2) for _ in modes]
                self.assertEqual(len(items),10)
                self.assertTrue(all(r[0]=='ok' for r in items),items)
                self.assertEqual(json.loads(gate.queue_path.read_text())['waiters'],[])
                state=json.loads(path.read_text())
                self.assertEqual(state['schema'],1)
                self.assertLessEqual(len(state['calls']),gate.MAX_SHARED)
            finally:
                for p in procs:
                    if p.is_alive():p.terminate();p.join(2)
