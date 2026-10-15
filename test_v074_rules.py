import json
import os
import tempfile
import shutil
import importlib.util
import unittest
from pathlib import Path
from decimal import Decimal

SRC=Path(__file__).with_name('okx_bot074.py')


def conf(coin='ETH',buy=25,sell=25):
    specs={'BTC':('10','50','0.01','0.01','0.01','0.01','95000','0.0001'),
           'ETH':('0.5','2.75','0.3','0.1','0.01','0.01','2800','0.03'),
           'SOL':('0.03','0.13','0.4','0.1','0.1','0.1','115','0.04')}
    step,tp,size,ct,lot,minimum,upper,base=specs[coin]
    return dict(instrument=coin+'-USDT-SWAP',direction='long',td_mode='cross',
        buy_type='post_only',tp_type='attached_limit',grid_step=step,
        take_profit=tp,order_size=size,active_buy_orders=buy,max_grids=500,
        max_exchange_orders=500,active_tp_orders=sell,real_tp_target=sell,
        contract_value=ct,tick_size=('0.1' if coin=='BTC' else '0.01'),
        contract_lot=lot,contract_min=minimum,maker_fee_at_setup='0.0002',
        upper_price=upper,capital_reference_price=upper,buy_base_qty=base,
        live_trading=False,buy_buffer_orders=0,buy_cancel_protect_steps=0,tp_buffer_orders=0)


def load(root,c,st=None):
    root=Path(root)
    (root/'state').mkdir(exist_ok=True);(root/'logs').mkdir(exist_ok=True)
    (root/'config.json').write_text(json.dumps(c))
    (root/'.env').write_text('OKX_API_KEY=FAKE_FOR_TEST\nOKX_API_SECRET=FAKE_FOR_TEST\nOKX_API_PASSPHRASE=FAKE_FOR_TEST\n')
    shutil.copy2(SRC,root/SRC.name)
    if st is not None:(root/'state/bot04_state.json').write_text(json.dumps(st))
    before=os.environ.get('OKX_BOT04_AUDIT')
    os.environ['OKX_BOT04_AUDIT']='READ_ONLY'
    try:
        spec=importlib.util.spec_from_file_location('v074_'+str(id(root)),root/SRC.name)
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        return m
    finally:
        if before is None:os.environ.pop('OKX_BOT04_AUDIT',None)
        else:os.environ['OKX_BOT04_AUDIT']=before

class RuleTests(unittest.TestCase):
    def test_buy_buf_protect_tp_buf_independent_of_legacy_keys(self):
        for coin in ('BTC','ETH','SOL'):
            for n in (1,5,10,20,25):
                with self.subTest(coin=coin,n=n),tempfile.TemporaryDirectory() as d:
                    cfg=conf(coin,buy=n)
                    cfg.update(buy_buffer_orders=0,buy_cancel_protect_steps=0,tp_buffer_orders=0)
                    m=load(d,cfg)
                    self.assertEqual(m.VER,'0.7.4')
                    self.assertEqual(m.BUY_BUFFER,5)
                    self.assertEqual(m.BUY_CANCEL_PROTECT_STEPS,5)
                    self.assertEqual(m.BUY_MAX,n+5)
                    self.assertEqual(m.REAL_TP_RESERVE,n+5)
                    self.assertEqual(m.REAL_TP_MAX,25+n+5)
    def test_legacy_75_config_uses_25(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=conf();cfg['active_tp_orders']=75;del cfg['real_tp_target']
            m=load(d,cfg)
            self.assertEqual(m.REAL_TP_TARGET,25)
            self.assertEqual(m.REAL_TP_MAX,55)
    def test_allowed_real_tp_independent_of_buy(self):
        with tempfile.TemporaryDirectory() as d:
            m=load(d,conf(buy=5,sell=14))
            self.assertEqual((m.REAL_TP_TARGET,m.REAL_TP_MAX,m.BUY_MAX),(14,24,10))
    def test_migrate_073_preserves_cycles_and_aggregate(self):
        with tempfile.TemporaryDirectory() as d:
            st={'version':'0.7.3','instrument':'ETH-USDT-SWAP',
                'cycles':{'2400.00':{'state':'VIRTUAL_TP','cycleId':'xyz','filledBuy':'0.03','filledTP':'0','tpOrders':[]}},
                'completed':101,'aggregate_exits':[{'phase':'UNKNOWN','clOrdId':'abc'}]}
            m=load(d,conf(),st)
            moved=m.ensure_state()
            self.assertEqual(moved['version'],'0.7.4')
            self.assertEqual(moved['completed'],101)
            self.assertEqual(moved['aggregate_exits'],st['aggregate_exits'])
            self.assertEqual(moved['cycles']['2400.00']['cycleId'],'xyz')
            self.assertTrue((Path(d)/'state/bot04_state_before_v074.json').is_file())
    def test_bad_base_counts_rejected(self):
        for typ,x in [('active_buy_orders',0),('active_buy_orders',26),('real_tp_target',0),('real_tp_target',26)]:
            with self.subTest(typ=typ,x=x),tempfile.TemporaryDirectory() as d:
                c=conf();c[typ]=x
                with self.assertRaises(RuntimeError):load(d,c)
    def test_near_bid_guard_five_steps(self):
        with tempfile.TemporaryDirectory() as d:
            m=load(d,conf())
            # protect a stale buy within 5 grid steps of bid, despite buffer cleanup
            for distance in ('0','0.5','1.5','2.5'):
                with self.subTest(distance=distance):
                    at=Decimal('2430')+Decimal(distance)
                    chosen,held=m.plan_buy_buffer_cleanup(
                        [{'buyPx':str(at),'state':'BUY_LIVE','ordId':'x'}],
                        set(),Decimal('2430'),1)
                    self.assertEqual(chosen,[])
                    self.assertEqual(len(held),1)
            chosen,_=m.plan_buy_buffer_cleanup(
                [{'buyPx':'2420','state':'BUY_LIVE','ordId':'far'}],
                set(),Decimal('2430'),1)
            self.assertEqual(len(chosen),1)
    def test_monitor_accepts_074_instead_of_073(self):
        import okx_execution_observer074 as obs
        self.assertIn('0.7.4',obs.__doc__)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'config.json').write_text(json.dumps(conf('SOL',5)))
            monitor=obs.Monitor(root,root/'diagnostics/execution_v074',mode='execution')
            self.assertEqual(monitor.buy_protect_steps,5)
            self.assertEqual(monitor.max_tp,35) # 25 base + 5 BUY + 5 buffer
            self.assertEqual(monitor.max_buy,10)
    def test_tp_scheduler_respects_margins(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            m=load(d,conf())
            obj=m.TPBufferScheduler()
            t=time.monotonic();obj.observe([],t)
            self.assertFalse(obj.cancel_due(26,t))
            self.assertTrue(obj.cancel_due(26,t+1.51) is False) # not observed extra yet
            # Exactly one verified attached SELL resets the quiet period.
            rows=[('2425',{'cycleId':str(i)},{'ordId':str(i)}) for i in range(26)]
            obj.observe(rows,t)
            self.assertFalse(obj.cancel_due(26,t+1))
            self.assertTrue(obj.cancel_due(26,t+1.51))
            self.assertTrue(obj.cancel_due(56,t+0.1)) # exchange overflow

if __name__=='__main__':unittest.main()
