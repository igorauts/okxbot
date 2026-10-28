"""Real OS-process flock+quota stress, all temp-file and no OKX calls.

The priority assertion requires both NORMAL tickets and the EMERGENCY ticket
actually to be waiting while the shared quota is exhausted.  A fixed sleep
between spawning processes does not establish that precondition (especially
under a loaded CI runner); pre-start workers and inspect the locked queue.
"""
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from queue import Empty
from okx_positions_fifo import AccountPositionsFIFO
from okx_positions_priority import PriorityPositionsFIFO, EMERGENCY, NORMAL


def _worker(path, priority, output, ready, release):
    gate=PriorityPositionsFIFO(path)
    ready.put(priority)
    if not release.wait(10):
        output.put((priority,time.monotonic(),'start_timeout'))
        return
    try:
        gate.take_prioritized(lambda:priority)
        output.put((priority,time.monotonic(),'ok'))
    except Exception as exc:
        output.put((priority,time.monotonic(),type(exc).__name__))


class SharedQuotaMultiprocess(unittest.TestCase):
    def test_emergency_preempts_queued_normal_without_quota_bypass(self):
        ctx=multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'quota.json'
            path.write_text(json.dumps({'schema':1,'calls':[]}))
            gate=AccountPositionsFIFO(path)
            out=ctx.Queue()
            ready=ctx.Queue()
            normals_go=ctx.Event()
            emergency_go=ctx.Event()
            procs=[]
            try:
                # Fully start workers BEFORE consuming the quota window.
                # This removes process-import/startup time from the race.
                for priority,go in ((NORMAL,normals_go),(NORMAL,normals_go),
                                    (EMERGENCY,emergency_go)):
                    proc=ctx.Process(target=_worker,args=(str(path),priority,out,ready,go))
                    proc.start()
                    procs.append(proc)
                started=[ready.get(timeout=8) for _ in procs]
                self.assertCountEqual(started,[NORMAL,NORMAL,EMERGENCY])

                def queued():
                    # Same flock inode as production code: never read a JSON
                    # file during another process's truncate/write/fsync.
                    fd=gate._open(gate.queue_path)
                    try:
                        gate._lock(fd,time.monotonic()+gate.LOCK_TIMEOUT)
                        obj=gate._read(fd)
                        return list(obj['waiters']) if obj else []
                    finally:
                        os.close(fd)

                def until_queue(predicate,timeout=1.3):
                    deadline=time.monotonic()+timeout
                    while time.monotonic()<deadline:
                        items=queued()
                        if predicate(items):return items
                        time.sleep(0.01)
                    self.fail('Priority test setup failed to queue all waiters '
                              'within a fresh quota window')

                # Keep genuine production quota and timing constraints.
                for _ in range(gate.MAX_SHARED):
                    self.assertTrue(gate._reserve_if_available(time.time())[0])
                normals_go.set()
                normal_items=until_queue(
                    lambda q:len(q)==2 and
                    all(row.get('priority')==NORMAL for row in q))
                self.assertEqual(len(normal_items),2)
                emergency_go.set()
                ordered=until_queue(
                    lambda q:len(q)==3 and
                    q[0].get('priority')==EMERGENCY)
                self.assertEqual([r['priority'] for r in ordered],
                                 [EMERGENCY,NORMAL,NORMAL])

                for proc in procs:proc.join(7)
                self.assertTrue(all(not p.is_alive() for p in procs))
                observed=[out.get(timeout=2) for _ in procs]
                self.assertTrue(all(item[2]=='ok' for item in observed),observed)
                observed.sort(key=lambda item:item[1])
                self.assertEqual(observed[0][0],EMERGENCY,observed)
                obj=json.loads(path.read_text())
                self.assertEqual(obj['schema'],1)
                self.assertLessEqual(len(obj['calls']),gate.MAX_SHARED)
                self.assertEqual(queued(),[])
            finally:
                normals_go.set()
                emergency_go.set()
                for proc in procs:
                    if proc.is_alive():proc.terminate();proc.join(2)
                out.close()
                ready.close()

if __name__=='__main__':unittest.main()
