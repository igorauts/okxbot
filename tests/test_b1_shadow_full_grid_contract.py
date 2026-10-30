"""Offline B1 eligibility + false BUY-zero at full grids. No OKX/Telegram I/O."""
import json
import sys
import time
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_idle_reconcile import (classify_background_candidate_after_orders,
                                wait_for_idle_reconcile)
import okx_telegram_events as te

class QuietPolicy(unittest.TestCase):
    def defaults(self):
        return dict(trigger_source='timer',mode='RUN',orders_complete=True,
                    facts_changed=False,unresolved=False,pending_intents=False,
                    protective_deadline_due=False,
                    private_or_virtual_or_grid_wake=False,ws_healthy=True,
                    control_changed=False,external_uncertainty=False)
    def test_only_verified_quiet_is_eligible(self):
        self.assertEqual(classify_background_candidate_after_orders(**self.defaults()),
                         (True,'quiet_after_verified_orders'))
    def test_each_untrusted_or_trade_cause_blocks(self):
        bad={'trigger_source':'private','mode':'SOFT','orders_complete':False,
             'facts_changed':True,'unresolved':True,'pending_intents':True,
             'protective_deadline_due':True,'private_or_virtual_or_grid_wake':True,
             'ws_healthy':False,'control_changed':True,'external_uncertainty':True}
        for k,v in bad.items():
            with self.subTest(k=k):
                cfg=self.defaults();cfg[k]=v
                self.assertFalse(classify_background_candidate_after_orders(**cfg)[0])
    def test_missing_proof_fails_conservatively(self):
        c=self.defaults();c.pop('orders_complete')
        with self.assertRaises(TypeError):classify_background_candidate_after_orders(**c)
        for field in ('orders_complete','facts_changed','unresolved',
                      'pending_intents','protective_deadline_due',
                      'private_or_virtual_or_grid_wake','ws_healthy',
                      'control_changed','external_uncertainty'):
            c=self.defaults();c[field]=None
            self.assertEqual(classify_background_candidate_after_orders(**c),
                             (False,'unverified_fact_flag'))
    def test_wait_before_reader_is_event_interruptible(self):
        wake=threading.Event()
        start=time.monotonic()
        t=threading.Thread(target=lambda:(time.sleep(.04),wake.set()),daemon=True)
        t.start()
        result=wait_for_idle_reconcile(wake=wake,control_read=lambda:'RUN',
            original_control='RUN',ws_health=lambda:('OK',),
            initial_ws_health=('OK',),extra_due=None,max_wait_seconds=1.0,
            local_poll_seconds=.05)
        t.join(timeout=1)
        self.assertEqual(result,'wake')
        self.assertLess(time.monotonic()-start,.30)

class ZeroBuyGridFull(unittest.TestCase):
    def run_scan(self,buy,target,grid,cap, prior=None, include_grids=True):
        now=time.time()
        suffix=f' | гриды {grid}/{cap}' if include_grids else ''
        message=f'📊 РЕЖИМ=RUN | BID=1 | BUY {buy}/{target} | TP 8 (реальных 2){suffix} | закрыто 100'
        row={'_SYSTEMD_UNIT':'okx-grid-near0721.service',
             '__REALTIME_TIMESTAMP':str(int(now*1_000_000)),
             'MESSAGE':message}
        proc=SimpleNamespace(returncode=0,stdout=json.dumps(row,ensure_ascii=False)+'\n')
        state={'buy_zero':dict(prior or {})}
        emitted=[]
        with tempfile.TemporaryDirectory() as td:
            real_exists=Path.exists
            def safe_exists(self):
                if str(self)=='/var/lib/okxbot/account_soft_stop.json':return False
                return real_exists(self)
            with patch.object(te.subprocess,'run',return_value=proc), \
                 patch.object(te,'one',side_effect=lambda api,st,key,msg:emitted.append(msg)), \
                 patch.object(Path,'exists',safe_exists):
                te.zero_buy_scan(None,state,{'near0721':Path(td)})
        return emitted,state['buy_zero']
    def test_full_grid_suppresses_false_red(self):
        events,inc=self.run_scan(0,40,40,40)
        self.assertEqual(events,[]);self.assertEqual(inc,{})
    def test_full_grid_clears_stale_false_incident(self):
        events,inc=self.run_scan(0,50,50,50,{'near0721':{'since':1,'target':50}})
        self.assertEqual(events,[]);self.assertEqual(inc,{})
    def test_true_empty_grid_keeps_red(self):
        events,inc=self.run_scan(0,40,0,40)
        self.assertEqual(len(events),1)
        self.assertIn('BUY 0/40',events[0]);self.assertIn('near0721',inc)
    def test_missing_grid_proof_keeps_original_alarm(self):
        events,inc=self.run_scan(0,40,0,40,include_grids=False)
        self.assertEqual(len(events),1);self.assertIn('near0721',inc)
    def test_partial_grid_does_not_suppress(self):
        events,_=self.run_scan(0,100,99,100)
        self.assertEqual(len(events),1)

if __name__=='__main__':unittest.main()
