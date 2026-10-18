import concurrent.futures
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_fill_singleflight_v0712 import FillSingleFlight
from okx_positions_fifo_v0712 import AccountPositionsFIFO, GateUnavailable


def worker(path, n, output):
    gate=AccountPositionsFIFO(path)
    gate.WINDOW=0.30
    gate.MAX_SHARED=3
    gate.MAX_QUEUE_WAIT=6
    times=[]
    for _ in range(n):
        gate.take()
        times.append(time.time())
    output.put(times)


class TestFIFOGate(unittest.TestCase):
    def test_5_processes_preserve_window(self):
        with tempfile.TemporaryDirectory() as d:
            shared=Path(d)/'gate.json'
            output=multiprocessing.Queue()
            workers=[multiprocessing.Process(target=worker,args=(shared,5,output)) for _ in range(5)]
            for p in workers:p.start()
            for p in workers:p.join(timeout=12)
            self.assertTrue(all(p.exitcode==0 for p in workers), [p.exitcode for p in workers])
            results=[output.get(timeout=2) for _ in workers]
            self.assertTrue(all(len(r)==5 for r in results))
            times=sorted(t for r in results for t in r)
            self.assertEqual(25,len(times))
            # Allow up to 20 ms scheduling jitter after quota reservation.
            for index,t in enumerate(times):
                n=sum(1 for v in times if t-0.28 < v <= t)
                self.assertLessEqual(n,3,(index,t,n))
            final=json.loads(shared.read_text())
            self.assertEqual(final['schema'],1) # compatible with v0711 calls parser

    def test_corrupted_legacy_quota_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'gate.json'
            p.write_text('not-json')
            with self.assertRaises(GateUnavailable):AccountPositionsFIFO(p).take()
            self.assertEqual(p.read_text(),'not-json')

    def test_corrupted_fifo_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'gate.json'
            q=p.with_name(p.name+'.fifo-v0712.json')
            q.write_text('{garbage')
            with self.assertRaises(GateUnavailable):AccountPositionsFIFO(p).take()
            self.assertEqual(q.read_text(),'{garbage')

    def test_crashed_waiter_is_pruned(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'gate.json'
            q=p.with_name(p.name+'.fifo-v0712.json')
            q.write_text(json.dumps({'schema':1,'waiters':[
                {'id':'crashed','pid':99999999,'created':time.time()-4,'seen':time.time()-4}]}))
            AccountPositionsFIFO(p).take()
            data=json.loads(q.read_text())
            self.assertEqual(data['waiters'],[])


class TestSingleFlight(unittest.TestCase):
    def test_one_fetch_for_simultaneous_same_order(self):
        g=FillSingleFlight()
        counter=[0]
        ready=threading.Barrier(8)
        def request(_):
            ready.wait()
            def fetch():
                counter[0]+=1
                time.sleep(.15)
                return 'fills'
            return g.run('ord123',fetch)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(request, range(8)))
        self.assertEqual(counter[0],1)
        self.assertEqual(values,['fills']*8)
        self.assertEqual(g.coalesced,7)

    def test_different_order_ids_can_run_concurrently(self):
        g=FillSingleFlight()
        def fetch(_):time.sleep(.16)
        start=time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda oid:g.run(oid,lambda:fetch(oid)),('a','b')))
        self.assertLess(time.monotonic()-start,.3)

    def test_failure_propagates_and_does_not_poison_future(self):
        g=FillSingleFlight()
        def bad():raise ValueError('API failed')
        with self.assertRaises(ValueError):g.run('a',bad)
        self.assertEqual(g.run('a',lambda:'ok'),'ok')
