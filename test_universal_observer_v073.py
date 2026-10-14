#!/usr/bin/env python3
"""ETH v073 read-only observer tests. All fixtures are synthetic; no API."""
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from okx_execution_observer073 import Monitor, EventTail, ranked_tps, summarize_state

def state(buys=25,tps=25):
    cycles={}
    for i in range(buys):
        px=Decimal('2429.5')-Decimal(i)*Decimal('.5')
        cycles[f'{px:.2f}']={'state':'BUY_LIVE','buyPx':str(px),'tpPx':str(px+Decimal('2.75')),'sz':'0.03','ordId':f'B{i}','tpOrders':[]}
    for i in range(tps):
        px=Decimal('2440')+Decimal(i)*Decimal('.5')
        cycles[f'{px:.2f}']={'state':'TP_WAIT','buyPx':str(px),'tpPx':str(px+Decimal('2.75')),'sz':'0.03','filledBuy':'0.03','cycleId':f'C{i}',
                            'tpOrders':[{'phase':'ACTIVE','ordId':f'T{i}','clOrdId':f'TC{i}','sz':'0.03','filled':'0'}]}
    return {'version':'0.7.3','instrument':'ETH-USDT-SWAP','cycles':cycles,'aggregate_exits':[]}

class ObsTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.root=Path(self.t.name)/'eth';(self.root/'logs').mkdir(parents=True);(self.root/'state').mkdir()
        (self.root/'config.json').write_text(json.dumps({'instrument':'ETH-USDT-SWAP','grid_step':'.5'}))
        (self.root/'state/bot04_state.json').write_text(json.dumps(state()))
        self.m=Monitor(self.root,self.root/'diagnostics/test')
        self.m.feed_bid_line('📊 РЕЖИМ=RUN | BID=2430.00 | BUY 25/25 | TP 25')
    def tearDown(self):self.t.cleanup()
    def test_snapshot_exact_expected_grid(self):
        s=summarize_state(state(),Decimal('2430.00'))
        self.assertEqual(s['buy'],25);self.assertEqual(s['real_tp'],25)
        self.assertEqual(s['missing_buy'],0)
    def test_missing_near_bid_detection(self):
        st=state();st['cycles'].pop('2429.50');s=summarize_state(st,Decimal('2430'))
        self.assertEqual(s['near_missing_buy'],1)
        self.assertEqual(s['near_missing_prices'],['2429.5'])
    def test_protect_nearest_25_tp(self):
        st=state(tps=55);s=summarize_state(st,Decimal('2442'))
        ranked=ranked_tps(s['tp_rows'],Decimal('2442'))
        base={r['ordId'] for r in ranked[:25]}
        self.assertEqual(len(base),25)
        self.assertNotIn(ranked[-1]['ordId'],base)
    def test_cancel_near_bid_critical(self):
        self.m.feed_event({'ts':datetime.now(timezone.utc).isoformat(),'event':'BUY_CANCEL_REQUESTED','cancelled':'2430.00','ordId':'X1'})
        self.m.feed_event({'ts':datetime.now(timezone.utc).isoformat(),'event':'BUY_BUFFER_CLEANUP','bid':'2430.00','count':1})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],1)
    def test_far_buy_cancel_ok(self):
        self.m.feed_event({'ts':datetime.now(timezone.utc).isoformat(),'event':'BUY_CANCEL_REQUESTED','cancelled':'2400.00','ordId':'X2'})
        self.m.feed_event({'ts':datetime.now(timezone.utc).isoformat(),'event':'BUY_BUFFER_CLEANUP','bid':'2430.00','count':1})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],0)
    def test_tp_cancel_count_violation(self):
        self.m.feed_event({'ts':datetime.now(timezone.utc).isoformat(),'event':'TP_BUFFER_CLEANUP','price':'2445.00','bid':'2430.00','active_before':25})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],1)
    def test_tp_cancel_interval_violation(self):
        t=datetime.now(timezone.utc)
        self.m.feed_event({'ts':t.isoformat(),'event':'TP_BUFFER_CLEANUP','price':'2445.00','bid':'2430.00','active_before':28})
        self.m.feed_event({'ts':(t+timedelta(milliseconds=50)).isoformat(),'event':'TP_BUFFER_CLEANUP','price':'2445.50','bid':'2430.00','active_before':27})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],1)
    def test_tp_cancel_slow_allowed(self):
        t=datetime.now(timezone.utc)
        self.m.feed_event({'ts':t.isoformat(),'event':'TP_BUFFER_CLEANUP','price':'2445.00','bid':'2430.00','active_before':28})
        self.m.feed_event({'ts':(t+timedelta(milliseconds=150)).isoformat(),'event':'TP_BUFFER_CLEANUP','price':'2445.50','bid':'2430.00','active_before':27})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],0)
    def test_nearest_base_tp_cancellation_detected(self):
        st=state(tps=26)
        target=st['cycles']['2440.00']['tpOrders'][0]
        target['phase']='CANCELING'
        (self.root/'state/bot04_state.json').write_text(json.dumps(st))
        self.m.feed_event({'ts':datetime.now(timezone.utc).isoformat(),
                           'event':'TP_BUFFER_CLEANUP','price':'2442.75',
                           'bid':'2455.00','active_before':26})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],0)
        # Rebuild a snapshot with a cancel of the nearest TP among 26.
        target['phase']='ACTIVE'
        st['cycles']['2440.00']['tpOrders'][0]['phase']='ACTIVE'
        cancel=st['cycles']['2442.50']['tpOrders'][0]
        cancel['phase']='CANCELING'
        (self.root/'state/bot04_state.json').write_text(json.dumps(st))
        self.m.feed_event({'ts':(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),
                           'event':'TP_BUFFER_CLEANUP','price':'2445.25',
                           'bid':'2445.25','active_before':26})
        self.assertEqual(self.m.counters['alerts_CRITICAL'],1)

    def test_alert_bursts_cannot_change_orders_or_state(self):
        before=(self.root/'state/bot04_state.json').read_bytes()
        now=datetime.now(timezone.utc)
        self.m.feed_event({'ts':now.isoformat(),'event':'TP_BUFFER_CLEANUP',
                           'price':'2448.00','bid':'2430.00','active_before':24})
        self.m._sample()
        self.assertEqual((self.root/'state/bot04_state.json').read_bytes(),before)

    def test_state_above_55_warning(self):
        st=state(tps=56);(self.root/'state/bot04_state.json').write_text(json.dumps(st))
        self.m._sample();self.assertEqual(self.m.counters['alerts_WARNING'],1)
    def test_burst_counts_and_summary(self):
        t=datetime.now(timezone.utc)
        for i in range(6):
            self.m.feed_event({'ts':(t+timedelta(seconds=i)).isoformat(),'event':'BUY_FILLED','price':str(2410-i),'ordId':f'O{i}'})
            self.m.feed_event({'ts':(t+timedelta(seconds=i,milliseconds=350)).isoformat(),'event':'BUY_PLACED','price':str(2400-i),'ordId':f'P{i}'})
        self.assertIsNotNone(self.m.first_burst)
        self.assertEqual(len(self.m.first_burst['fills']),6)
        self.assertEqual(self.m.first_burst['counts']['BUY_PLACED'],6)
        self.m._finalize_burst()
        report=json.loads((self.m.output/'bursts_v073.jsonl').read_text().splitlines()[-1])
        self.assertEqual(report['fills'],6);self.assertEqual(report['new_buy_placements'],6)
    def test_solana_allowed(self):
        st=state();st['instrument']='SOL-USDT-SWAP'
        self.assertEqual(summarize_state(st,Decimal('2430'))['buy'],25)
    def test_partial_tp_cycle_not_false_mismatch(self):
        st=state(tps=1);c=st['cycles']['2440.00'];c['tpOrders'][0]['filled']='0.01'
        s=summarize_state(st,Decimal('2430'))
        self.assertEqual(s['bad_cycles'],[])
        self.assertEqual(s['open_covered_contracts'],'0.02')
    def test_tail_future_only(self):
        p=self.root/'logs/bot04_events.jsonl'
        p.write_text(json.dumps({'event':'OLD'})+'\n')
        tail=EventTail(p)
        self.assertEqual(tail.read(),[])
        with p.open('a') as f:f.write(json.dumps({'event':'NEW'})+'\n')
        self.assertEqual([e['event'] for e in tail.read()],['NEW'])
        tail.close()
    def test_no_live_changes(self):
        before=(self.root/'state/bot04_state.json').read_bytes()
        self.m._sample()
        self.assertEqual((self.root/'state/bot04_state.json').read_bytes(),before)

if __name__=='__main__':unittest.main()
