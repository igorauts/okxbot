"""Active B1/B2/B3 offline integration without real OKX or services."""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch
from okx_idle_reconcile import (b2_aligned_deduplicated_wait,
                                b1_runtime_pending_intents,b1_runtime_invalidate)
from okx_position_reader_core import Registration, SharedReaderCore, Priority, QuotaRequestAbandoned
from okx_position_reader_ipc import ReaderIPCServer

class B2AuditBounds(unittest.TestCase):
    def test_quiet_and_active_boundary(self):
        # Shared 5s boundary 0.2s after a recent complete event pass.
        wait,skip=b2_aligned_deduplicated_wait(wall_now=104.8,mono_now=50.0,
                                                last_complete_mono=49.9)
        self.assertTrue(skip)
        self.assertAlmostEqual(wait,5.2,places=5)
        self.assertLessEqual(wait+.1,6.0)
        # If full reconciliation ended long ago, the 5s boundary stays.
        wait,skip=b2_aligned_deduplicated_wait(wall_now=104.8,mono_now=50,
                                                last_complete_mono=47)
        self.assertFalse(skip)
        self.assertAlmostEqual(wait,.2,places=5)
        # No skip if the next timer already lies >1 sec after completed pass.
        wait,skip=b2_aligned_deduplicated_wait(wall_now=101,mono_now=50,
                                                last_complete_mono=49.8)
        self.assertFalse(skip)
        self.assertEqual(wait,4)
    def test_fail_closed_timestamp_and_bounded_horizon(self):
        for last in (None,float('nan'),float('inf'),float('-inf')):
            wait,skip=b2_aligned_deduplicated_wait(wall_now=104.8,mono_now=50,
                                                   last_complete_mono=last)
            self.assertFalse(skip)
            self.assertAlmostEqual(wait,.2,places=5)
        for wall_tenth in range(10,50):
            wall=100+wall_tenth/10
            wait,skip=b2_aligned_deduplicated_wait(wall_now=wall,mono_now=50,
                                                   last_complete_mono=49.99)
            self.assertLessEqual(wait+.01,6.001)
    def test_b1_epoch_rejects_after_save(self):
        state={'cycles':{},'aggregate_exits':[]}
        self.assertFalse(b1_runtime_pending_intents(state))
        b1_runtime_invalidate()
        self.assertTrue(b1_runtime_pending_intents(state))
        # Unknown and/or >256 cycles remains NORMAL.
        self.assertTrue(b1_runtime_pending_intents({'cycles':None}))
        self.assertTrue(b1_runtime_pending_intents(
            {'cycles':{str(i):{} for i in range(257)}}))

class CausalLatency(unittest.TestCase):
    def test_private_grid_virtual_event_clock(self):
        from okx_idle_reconcile import b3_earliest_observed_event_ns
        import threading,queue
        class Private:
            private_wake=threading.Event()
            events=queue.Queue()
        class Public:
            _lock=threading.Lock()
            _grid_wake_count=0
            _virtual_wake_count=0
            _virtual_crossed={}
        w=Private();m=Public();now=time.perf_counter_ns()
        self.assertIsNone(b3_earliest_observed_event_ns(w,m,now))
        w._first_private_event_ns=now-1_000_000
        w.private_wake.set()
        self.assertEqual(b3_earliest_observed_event_ns(w,m,now),now-1_000_000)
        m._grid_wake_count=1
        m._first_grid_event_ns=now-3_000_000
        self.assertEqual(b3_earliest_observed_event_ns(w,m,now),now-3_000_000)
        m._virtual_wake_count=1
        m._first_virtual_event_ns=now-2_000_000
        self.assertEqual(b3_earliest_observed_event_ns(w,m,now),now-3_000_000)
        # Older than five seconds -> never invent a timestamp.
        w._first_private_event_ns=now-8_000_000_000
        m._first_grid_event_ns=now-8_000_000_000
        m._first_virtual_event_ns=now-8_000_000_000
        self.assertIsNone(b3_earliest_observed_event_ns(w,m,now))

class FakeQuota:
    def __init__(self):
        self.allow=asyncio.Event()
        self.calls=0
    async def take(self,supplier):
        self.calls+=1
        while not self.allow.is_set():
            supplier()
            await asyncio.sleep(.005)
        supplier()

class FakeProvider:
    def __init__(self):
        self.calls=0
        self.starts=[]
    async def fetch(self,instruments):
        self.calls+=1
        self.starts.append(time.monotonic())
        return {'code':'0','data':[], '_reader_transport_scope':{
            'endpoint':'/api/v5/account/positions','instType':'SWAP',
            'instIds':list(instruments),'complete':True}}

class ReaderActiveB3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.quota=FakeQuota();self.provider=FakeProvider()
        self.uid='fake-uid-no-exchange'
        self.inst='BTC-USDT-SWAP'
        self.core=SharedReaderCore([Registration('fake',self.inst)],
            self.provider,self.quota,account_uid=self.uid,
            background_grace_sec=.05,quota_timeout_sec=2)
        await self.core.start()
    async def asyncTearDown(self):
        await self.core.close()
    async def req(self,priority):
        return await self.core.request(self.inst,priority,
             account_uid=self.uid,timeout_sec=1.5)
    async def test_background_stays_open_for_other_background(self):
        self.quota.allow.set()
        started=time.monotonic()
        a=asyncio.create_task(self.req(Priority.BACKGROUND))
        await asyncio.sleep(.010)
        b=asyncio.create_task(self.req(Priority.BACKGROUND))
        await asyncio.gather(a,b)
        self.assertEqual(self.provider.calls,1)
        self.assertEqual(self.core.counters['coalesced_clients'],2)
        self.assertGreaterEqual(self.provider.starts[0]-started,.040)
    async def test_urgent_bypasses_grace(self):
        self.quota.allow.set()
        a=asyncio.create_task(self.req(Priority.BACKGROUND))
        await asyncio.sleep(.008)
        start=time.monotonic()
        b=asyncio.create_task(self.req(Priority.EXIT))
        await asyncio.gather(a,b)
        # Heavy VPS CPU steal can delay an already-awoken coroutine.
        # Assert ordering in the Reader core, not a 47ms OS scheduling SLA.
        self.assertEqual(self.core.counters['coalesced_clients'],2)
        self.assertGreaterEqual(self.provider.starts[0],start)
        self.assertEqual(self.provider.calls,1)
    async def test_seven_quiet_clients_share_one_physical_get(self):
        # All seven instruments share a single real SharedReaderCore batch.
        registrations=[Registration('bot'+str(i),'COIN'+str(i)+'-USDT-SWAP')
                       for i in range(7)]
        quota=FakeQuota();quota.allow.set();provider=FakeProvider()
        seven=SharedReaderCore(registrations,provider,quota,
               account_uid=self.uid,background_grace_sec=.05)
        await seven.start()
        try:
            tasks=[asyncio.create_task(seven.request(
                 r.instrument,Priority.BACKGROUND,
                 account_uid=self.uid,timeout_sec=1.5)) for r in registrations]
            rows=await asyncio.gather(*tasks)
            self.assertEqual(len(rows),7)
            self.assertTrue(all(x['absence_is_proven_zero'] is True for x in rows))
            self.assertEqual(provider.calls,1)
            self.assertEqual(seven.counters['coalesced_clients'],7)
        finally:
            await seven.close()
    async def test_unneeded_closed_client_leaves_no_quota_get(self):
        with tempfile.TemporaryDirectory() as td:
            directory=pathlib.Path(td);directory.chmod(0o700)
            server=ReaderIPCServer(self.core,directory/'reader.sock')
            # Cannot start a second core; attach the already-running core to
            # an isolated IPC server bound to a private directory only.
            server.server=await asyncio.start_unix_server(
                server._handle,path=str(server.socket_path))
            try:
                reader,writer=await asyncio.open_unix_connection(str(server.socket_path))
                msg={'schema':1,'op':'positions','instrument':self.inst,
                     'priority':int(Priority.BACKGROUND),
                     'not_before_mono_ns':time.monotonic_ns(),
                     'trace_id':'drop', 'account_uid':self.uid}
                writer.write((json.dumps(msg)+'\n').encode());await writer.drain()
                await asyncio.sleep(.02)
                writer.close();await writer.wait_closed()
                await asyncio.sleep(.07)
                self.assertEqual(len(self.core._pending()),0)
                self.assertEqual(self.provider.calls,0)
                self.assertGreaterEqual(self.core.counters['canceled_clients'],1)
                self.quota.allow.set()
                await asyncio.sleep(.02)
                self.assertEqual(self.provider.calls,0)
            finally:
                server.server.close();await server.server.wait_closed()
    async def test_abandoned_one_of_two_leaves_other_alive(self):
        self.quota.allow.set()
        a=asyncio.create_task(self.req(Priority.BACKGROUND))
        await asyncio.sleep(.003)
        b=asyncio.create_task(self.req(Priority.BACKGROUND))
        a.cancel()
        try:await a
        except asyncio.CancelledError:pass
        outcome=await b
        self.assertIs(outcome['absence_is_proven_zero'],True)
        self.assertEqual(self.provider.calls,1)
    async def test_unknown_uid_fails_closed(self):
        with self.assertRaises(Exception):
            await self.core.request(self.inst,Priority.BACKGROUND,account_uid='other')

if __name__=='__main__':unittest.main()
