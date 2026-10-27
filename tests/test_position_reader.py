"""Deterministic-ish offline contracts; NO HTTP, secret, systemd or live calls."""
import asyncio
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from okx_position_reader_core import (
    MAX_ACCOUNT_BOTS, Priority, Registration, RegistryError, ReaderError,
    SharedReaderCore, SnapshotError, assert_can_create, build_snapshot,
    validate_registrations,
)
from okx_position_reader_ipc import ReaderIPCServer, ipc_request


def sample(n=5):
    return [Registration(f'bot{i}', f'ASSET{i}-USDT-SWAP') for i in range(n)]


class FakeProvider:
    def __init__(self):
        self.calls=[]
        self.started=asyncio.Event()
        self.release=None
        self.fail=False
    async def fetch(self,instruments):
        self.calls.append(instruments)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            raise RuntimeError('mock failure')
        return {'code':'0','data':[{'instId':inst, 'pos':'2', 'posSide':'net'} for inst in instruments]}


class ManualQuota:
    def __init__(self, initially_open=True):
        self.allowed=asyncio.Event()
        if initially_open:self.allowed.set()
        self.takes=0
    async def take(self, priority_supplier=None):
        self.takes+=1
        await self.allowed.wait()


class RegistryTests(unittest.TestCase):
    def test_ten_bot_cap(self):
        x=sample(MAX_ACCOUNT_BOTS)
        self.assertEqual(len(validate_registrations(x)),MAX_ACCOUNT_BOTS)
        with self.assertRaises(RegistryError):
            assert_can_create(x,Registration('eleventh','ELEVENTH-USDT-SWAP'))
    def test_archived_does_not_occupy_slot(self):
        x=sample(MAX_ACCOUNT_BOTS)+[Registration('old','OLD-USDT-SWAP',verified_archived=True)]
        self.assertEqual(len(validate_registrations(x)),MAX_ACCOUNT_BOTS)
    def test_stopped_returnable_occupies_slot(self):
        with self.assertRaises(RegistryError):
            validate_registrations(sample(MAX_ACCOUNT_BOTS+1))
    def test_duplicate_bot_name(self):
        with self.assertRaises(RegistryError):
            validate_registrations([Registration('x','A-USDT-SWAP'),Registration('x','B-USDT-SWAP')])
    def test_one_bot_per_pair(self):
        with self.assertRaises(RegistryError):
            validate_registrations([Registration('a','A-USDT-SWAP'),Registration('b','A-USDT-SWAP')])
    def test_bad_registry_fails_closed(self):
        for x in (None, [{}], [Registration('x','BTC-USDC-SWAP')]):
            with self.assertRaises(RegistryError):
                validate_registrations(x)


class SnapshotTests(unittest.TestCase):
    def test_own_slice_and_missing_is_not_proven_zero(self):
        start=time.monotonic_ns()
        s=build_snapshot(response={'code':'0','data':[{'instId':'A-USDT-SWAP','pos':'2','posSide':'net','meta':{'nested':['ok']}}]},
                         instruments=('A-USDT-SWAP','B-USDT-SWAP'),started_ns=start,completed_ns=start+10)
        x=s.slice('A-USDT-SWAP',not_before_ns=start)
        self.assertEqual(x['rows'][0]['pos'],'2')
        y=s.slice('B-USDT-SWAP',not_before_ns=start)
        self.assertEqual(y['rows'],[])
        self.assertFalse(y['absence_is_proven_zero'])
        x['rows'][0]['pos']='999'
        x['rows'][0]['meta']['nested'].append('tampered')
        self.assertEqual(s.slice('A-USDT-SWAP',not_before_ns=start)['rows'][0]['pos'],'2')
        self.assertEqual(s.slice('A-USDT-SWAP',not_before_ns=start)['rows'][0]['meta']['nested'],['ok'])
    def test_snapshot_before_event_rejected(self):
        start=time.monotonic_ns()
        s=build_snapshot(response={'code':'0','data':[]},instruments=('A-USDT-SWAP',),
                         started_ns=start,completed_ns=start+10)
        with self.assertRaises(SnapshotError):
            s.slice('A-USDT-SWAP',not_before_ns=start+1)
    def test_invalid_exchange_response_fails_closed(self):
        for x in ({'code':'50011','data':[]}, {'code':'0','data':[3]}):
            with self.assertRaises(SnapshotError):
                build_snapshot(response=x,instruments=('A-USDT-SWAP',),started_ns=1,completed_ns=2)
        # Other instruments in an instType=SWAP response are ignored.
        snap=build_snapshot(response={'code':'0','data':[
            {'instId':'OTHER-USDT-SWAP','pos':'7','posSide':'net'}]},
            instruments=('A-USDT-SWAP',),started_ns=1,completed_ns=2)
        self.assertEqual(snap.slice('A-USDT-SWAP',not_before_ns=1)['rows'],[])


class AsyncCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider=FakeProvider()
        self.quota=ManualQuota()
        self.core=SharedReaderCore(sample(),self.provider,self.quota,account_uid='TEST_UID')
        await self.core.start()
    async def asyncTearDown(self):
        await self.core.close()
    async def test_five_waiters_one_fresh_get(self):
        self.quota.allowed.clear()
        tasks=[asyncio.create_task(self.core.request(f'ASSET{i}-USDT-SWAP',Priority.EXIT,account_uid='TEST_UID'))
               for i in range(5)]
        await asyncio.sleep(0.01)
        self.quota.allowed.set()
        data=await asyncio.gather(*tasks)
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(data),5)
        self.assertEqual(len({d['snapshot_id'] for d in data}),1)
        self.assertTrue(all(len(d['rows'])==1 for d in data))
    async def test_late_arrival_gets_another_fresh_get(self):
        self.provider.release=asyncio.Event()
        first=asyncio.create_task(self.core.request('ASSET0-USDT-SWAP',Priority.EXIT,account_uid='TEST_UID'))
        await self.provider.started.wait()
        second=asyncio.create_task(self.core.request('ASSET1-USDT-SWAP',Priority.EXIT,account_uid='TEST_UID'))
        await asyncio.sleep(0.005)
        self.provider.release.set()
        a,b=await asyncio.gather(first,second)
        self.assertNotEqual(a['snapshot_id'],b['snapshot_id'])
        self.assertEqual(len(self.provider.calls),2)
        self.assertLess(a['get_started_mono_ns'],b['get_started_mono_ns'])
    async def test_event_barrier_is_respected(self):
        prev=time.monotonic_ns()
        result=await self.core.request('ASSET0-USDT-SWAP',Priority.EXIT,not_before_ns=prev,account_uid='TEST_UID')
        self.assertGreaterEqual(result['get_started_mono_ns'],prev)
    async def test_provider_failure_fails_closed(self):
        self.provider.fail=True
        with self.assertRaises(ReaderError):
            await self.core.request('ASSET0-USDT-SWAP',Priority.EMERGENCY,account_uid='TEST_UID')
    async def test_unknown_pair_rejected(self):
        with self.assertRaises(RegistryError):
            await self.core.request('UNREGISTERED-USDT-SWAP',Priority.EXIT,account_uid='TEST_UID')
    async def test_closing_blocked_quota_does_not_hang(self):
        self.quota.allowed.clear()
        request = asyncio.create_task(self.core.request('ASSET0-USDT-SWAP', Priority.EXIT,account_uid='TEST_UID'))
        await asyncio.sleep(0.005)
        await asyncio.wait_for(self.core.close(), 0.2)
        with self.assertRaises(ReaderError):
            await request

    async def test_timeout_no_stale_reuse(self):
        self.quota.allowed.clear()
        with self.assertRaises(asyncio.TimeoutError):
            await self.core.request('ASSET0-USDT-SWAP',Priority.EXIT,timeout_sec=0.01,account_uid='TEST_UID')
        self.quota.allowed.set()
        await asyncio.sleep(0.01)
        self.assertFalse(self.core._waiters)


class IPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_unix_socket_filters_position_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            os.chmod(folder,0o700)
            core=SharedReaderCore(sample(2),FakeProvider(),ManualQuota(),account_uid='TEST_UID')
            socket=folder/'reader.sock'
            ipc=ReaderIPCServer(core,socket)
            await ipc.start()
            try:
                self.assertEqual(stat.S_IMODE(socket.stat().st_mode),0o600)
                data=await ipc_request(socket,'ASSET0-USDT-SWAP',Priority.NORMAL,account_uid='TEST_UID')
                self.assertEqual(data['instrument'],'ASSET0-USDT-SWAP')
                self.assertEqual({r['instId'] for r in data['rows']},{'ASSET0-USDT-SWAP'})
            finally:
                await ipc.close()
            self.assertFalse(socket.exists())


class RC2NegativeContracts(unittest.TestCase):
    def test_wrong_net_and_duplicate_rows_rejected(self):
        start = time.monotonic_ns()
        good = {'instId': 'A-USDT-SWAP', 'posSide': 'net', 'pos': '2'}
        wrong = dict(good, posSide='long')
        missing = {'instId': 'A-USDT-SWAP', 'posSide': 'net'}
        for rows in ([wrong], [missing], [good, good],
                     [dict(good, pos='NaN')], [dict(good, pos='Infinity')]):
            with self.subTest(rows=rows):
                snap=build_snapshot(response={'code':'0','data':rows},
                                   instruments=('A-USDT-SWAP',),
                                   started_ns=start, completed_ns=start+100)
                with self.assertRaises(SnapshotError):
                    snap.slice('A-USDT-SWAP',not_before_ns=start)
    def test_broken_snapshot_boundary(self):
        row = {'instId': 'A-USDT-SWAP', 'posSide': 'net', 'pos': '2'}
        for a, b in ((0, 2), (20, 10), (True, 30)):
            with self.assertRaises(SnapshotError):
                build_snapshot(response={'code':'0','data':[row]},
                               instruments=('A-USDT-SWAP',), started_ns=a, completed_ns=b)
    def test_registry_capacity_and_returnable_archive(self):
        all_active = sample(10)
        with self.assertRaises(RegistryError):
            validate_registrations(all_active + [Registration('stopped', 'Z-USDT-SWAP')])
        self.assertEqual(len(validate_registrations(all_active +
            [Registration('old', 'Z-USDT-SWAP', True)])), 10)


class RC2Scheduling(unittest.IsolatedAsyncioTestCase):
    async def test_background_yields_to_exit_before_quota(self):
        provider, quota = FakeProvider(), ManualQuota()
        core = SharedReaderCore(sample(2), provider, quota, background_grace_sec=0.08, account_uid='TEST_UID')
        await core.start()
        try:
            bg = asyncio.create_task(core.request('ASSET0-USDT-SWAP', Priority.BACKGROUND,account_uid='TEST_UID'))
            await asyncio.sleep(0.006)
            self.assertEqual(quota.takes, 0, 'Background should not reserve immediately')
            ex = asyncio.create_task(core.request('ASSET1-USDT-SWAP', Priority.EXIT,account_uid='TEST_UID'))
            a, b = await asyncio.gather(bg, ex)
            self.assertEqual(quota.takes, 1)
            self.assertEqual(a['snapshot_id'], b['snapshot_id'])
            self.assertEqual(core.metrics[-1]['highest_priority'], int(Priority.EXIT))
        finally:
            await core.close()

    async def test_slow_provider_timeout_fails_closed(self):
        provider, quota = FakeProvider(), ManualQuota()
        provider.release = asyncio.Event()
        core = SharedReaderCore(sample(2), provider, quota, get_timeout_sec=0.025, account_uid='TEST_UID')
        await core.start()
        try:
            with self.assertRaises(ReaderError):
                await core.request('ASSET0-USDT-SWAP', Priority.EMERGENCY, timeout_sec=1,account_uid='TEST_UID')
            self.assertEqual(core.counters['fetch_failed'], 1)
            self.assertEqual(core.counters['get_batches'], 0)
        finally:
            await core.close()

    async def test_quota_timeout_no_http(self):
        provider, quota = FakeProvider(), ManualQuota(initially_open=False)
        core = SharedReaderCore(sample(), provider, quota, quota_timeout_sec=0.02, account_uid='TEST_UID')
        await core.start()
        try:
            with self.assertRaises(ReaderError):
                await core.request('ASSET0-USDT-SWAP', Priority.EXIT, timeout_sec=1,account_uid='TEST_UID')
            self.assertEqual(len(provider.calls), 0)
            self.assertEqual(core.counters['quota_failed'], 1)
        finally:
            await core.close()

    async def test_cancelled_waiter_not_discharged_from_old_snapshot(self):
        provider, quota = FakeProvider(), ManualQuota()
        provider.release = asyncio.Event()
        core = SharedReaderCore(sample(2), provider, quota, account_uid="TEST_UID")
        await core.start()
        try:
            first = asyncio.create_task(core.request('ASSET0-USDT-SWAP', Priority.EXIT,account_uid='TEST_UID'))
            await provider.started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(core.request('ASSET1-USDT-SWAP', Priority.EXIT,account_uid='TEST_UID'))
            provider.release.set()
            later = await second
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(later['instrument'], 'ASSET1-USDT-SWAP')
        finally:
            await core.close()

    async def test_bounded_metrics(self):
        provider, quota = FakeProvider(), ManualQuota()
        core = SharedReaderCore(sample(1), provider, quota, account_uid="TEST_UID")
        await core.start()
        try:
            for _ in range(1040):
                await core.request('ASSET0-USDT-SWAP', Priority.NORMAL,account_uid='TEST_UID')
            self.assertEqual(len(core.metrics), 1024)
            self.assertEqual(core.counters['get_batches'], 1040)
        finally:
            await core.close()

    async def test_trace_is_per_client_and_not_shared(self):
        provider, quota = FakeProvider(), ManualQuota(initially_open=False)
        core = SharedReaderCore(sample(2), provider, quota, account_uid="TEST_UID")
        await core.start()
        try:
            a = asyncio.create_task(core.request('ASSET0-USDT-SWAP', Priority.NORMAL,trace_id='a-only',account_uid='TEST_UID'))
            b = asyncio.create_task(core.request('ASSET1-USDT-SWAP', Priority.NORMAL,trace_id='b-only',account_uid='TEST_UID'))
            await asyncio.sleep(0.002)
            quota.allowed.set()
            x, y = await asyncio.gather(a, b)
            self.assertEqual((x['trace_id'], y['trace_id']), ('a-only','b-only'))
            self.assertEqual(x['snapshot_id'], y['snapshot_id'])
            self.assertEqual(set(r['instId'] for r in x['rows']), {'ASSET0-USDT-SWAP'})
            self.assertEqual(set(r['instId'] for r in y['rows']), {'ASSET1-USDT-SWAP'})
        finally:
            await core.close()


class RC2RegistryReload(unittest.IsolatedAsyncioTestCase):
    async def test_reload_only_when_drained(self):
        provider, quota = FakeProvider(), ManualQuota(initially_open=False)
        core = SharedReaderCore(sample(5), provider, quota, account_uid="TEST_UID")
        await core.start()
        try:
            req = asyncio.create_task(core.request('ASSET0-USDT-SWAP', Priority.EXIT,account_uid='TEST_UID'))
            await asyncio.sleep(0.002)
            with self.assertRaises(RegistryError):
                core.replace_registrations(sample(6))
            quota.allowed.set()
            await req
            core.replace_registrations(sample(6))
            self.assertEqual(len(core.instruments), 6)
            self.assertEqual((await core.request('ASSET5-USDT-SWAP', Priority.NORMAL,account_uid='TEST_UID'))['instrument'],
                             'ASSET5-USDT-SWAP')
            with self.assertRaises(RegistryError):
                core.replace_registrations(sample(11))
            self.assertEqual(len(core.instruments), 6)
        finally:
            await core.close()

    async def test_pending_queue_has_bounded_capacity(self):
        from okx_position_reader_core import MAX_PENDING_CLIENTS
        provider, quota = FakeProvider(), ManualQuota(initially_open=False)
        core = SharedReaderCore(sample(1), provider, quota, account_uid="TEST_UID")
        await core.start()
        tasks = []
        try:
            tasks = [asyncio.create_task(core.request('ASSET0-USDT-SWAP', Priority.NORMAL,
                                                      timeout_sec=5,account_uid="TEST_UID"))
                     for _ in range(MAX_PENDING_CLIENTS)]
            await asyncio.sleep(0.025)
            self.assertEqual(len(core._pending()), MAX_PENDING_CLIENTS)
            with self.assertRaises(ReaderError):
                await core.request('ASSET0-USDT-SWAP', Priority.EMERGENCY,account_uid='TEST_UID')
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await core.close()


class RC2TenBotBatch(unittest.IsolatedAsyncioTestCase):
    async def test_ten_concurrent_different_pairs_one_get(self):
        provider, quota = FakeProvider(), ManualQuota(initially_open=False)
        core = SharedReaderCore(sample(10), provider, quota, account_uid="TEST_UID")
        await core.start()
        try:
            tasks = [asyncio.create_task(core.request(f'ASSET{i}-USDT-SWAP', Priority.EXIT,
                                                      trace_id=f'exit-{i}',account_uid="TEST_UID")) for i in range(10)]
            await asyncio.sleep(0.005)
            quota.allowed.set()
            out = await asyncio.gather(*tasks)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(len(provider.calls[0]), 10)
            self.assertEqual(len({r['snapshot_id'] for r in out}), 1)
            self.assertEqual({r['instrument'] for r in out},
                             {f'ASSET{i}-USDT-SWAP' for i in range(10)})
            self.assertEqual(core.counters['coalesced_clients'], 10)
        finally:
            await core.close()
