"""Real OS-process flock+quota stress, all temp-file and no OKX calls."""
import json
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from okx_positions_fifo import AccountPositionsFIFO
from okx_positions_priority import PriorityPositionsFIFO, EMERGENCY, NORMAL


def _worker(path, priority, output):
    gate=PriorityPositionsFIFO(path)
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
            for _ in range(gate.MAX_SHARED):
                self.assertTrue(gate._reserve_if_available(time.time())[0])
            out=ctx.Queue()
            procs=[]
            try:
                for priority in (NORMAL,NORMAL):
                    proc=ctx.Process(target=_worker,args=(str(path),priority,out))
                    proc.start();procs.append(proc)
                time.sleep(0.25)
                proc=ctx.Process(target=_worker,args=(str(path),EMERGENCY,out))
                proc.start();procs.append(proc)
                for proc in procs:proc.join(7)
                self.assertTrue(all(not p.is_alive() for p in procs))
                observed=[out.get(timeout=2) for _ in procs]
                self.assertTrue(all(item[2]=='ok' for item in observed),observed)
                observed.sort(key=lambda item:item[1])
                self.assertEqual(observed[0][0],EMERGENCY,observed)
                obj=json.loads(path.read_text())
                self.assertEqual(obj['schema'],1)
                self.assertLessEqual(len(obj['calls']),gate.MAX_SHARED)
                self.assertEqual(json.loads(gate.queue_path.read_text())['waiters'],[])
            finally:
                for proc in procs:
                    if proc.is_alive():proc.terminate();proc.join(2)

if __name__=='__main__':unittest.main()
