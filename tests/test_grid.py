"""Offline tests only: no internet, no trading account and no order writes."""
import importlib.util
import json
import queue
import shutil
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

SRC=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('wizard060',SRC/'okx_setup065.py')
wizard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

class FakeScreen:
    def __init__(self,keys):
        self.keys=iter(keys)
        self.frames=[]
    def erase(self):self.frames.append([])
    def getmaxyx(self):return 45,120
    def addnstr(self,y,x,value,n):self.frames[-1].append((y,str(value[:n])))
    def refresh(self):pass
    def get_wch(self):return next(self.keys)


def fake_api(path,env=None,params=None):
    coin=params.get('instId','').split('-',1)[0] if params else ''
    if path.endswith('instruments'):
        return [{'instId':params['instId'],'ctVal':{'BTC':'0.01','ETH':'0.1','SOL':'1'}[coin],
            'ctValCcy':coin,'ctType':'linear','instFamily':f'{coin}-USDT',
            'lotSz':'0.01','minSz':'0.01',
            'tickSz':'0.1' if coin=='BTC' else '0.01','state':'live','groupId':'1'}]
    if path.endswith('ticker'):
        return [{'bidPx':{'BTC':'83200','ETH':'2505.21','SOL':'106'}[coin]}]
    if path.endswith('trade-fee'):
        assert env is not None
        return [{'level':'VIP0','feeGroup':[{'groupId':'1','maker':'-0.0002','taker':'-0.0005'}]}]
    raise AssertionError(path)

class WizardTests(unittest.TestCase):
    def setUp(self):
        self.feepatch=patch.object(wizard,'okx_req',fake_api)
        self.feepatch.start()
    def tearDown(self):self.feepatch.stop()
    def contract(self,coin):
        return wizard.load_contract(coin,{'OKX_API_KEY':'fake'})
    def test_contracts_and_negative_fee_sign(self):
        for coin,small,minimum in [('BTC','0.0001','33.30'),('ETH','0.001','1.01'),('SOL','0.01','0.05')]:
            c=self.contract(coin)
            self.assertEqual(c['min_qty'],Decimal(small))
            self.assertEqual(c['break_even'],Decimal(minimum))
            self.assertEqual(c['maker'],Decimal('0.0002'))
            self.assertEqual(c['taker'],Decimal('0.0005'))
        self.assertEqual(self.contract('SOL')['min_tp'],Decimal('0.06'))
        self.assertEqual(self.contract('BTC')['min_tp'],Decimal('40.00'))
    def test_full_grid_capital_upper_and_unbounded(self):
        low,size,cap=wizard.capital(Decimal('95680'),Decimal('10'),500,Decimal('0.0001'))
        self.assertEqual((low,size,cap),(Decimal('90690'),Decimal('0.05'),Decimal('4659.25')))
        _,_,cap_unbound=wizard.capital(Decimal('83200'),Decimal('10'),500,Decimal('0.0001'))
        self.assertEqual(cap_unbound,Decimal('4035.25'))
        sol=self.contract('SOL');sol.update(step=Decimal('0.05'),grids=Decimal(500),qty=Decimal('0.01'))
        self.assertEqual(wizard.validate_numeric('upper','80',sol),Decimal(80))
        self.assertEqual(wizard.validate_numeric('upper','0',sol),Decimal(0))
        self.assertEqual(wizard.capital(Decimal(80),Decimal('.05'),500,Decimal('.01'))[0],Decimal('55.05'))
    def test_validation_and_retries(self):
        c=self.contract('BTC');c.update(step=Decimal(10),grids=Decimal(500),qty=Decimal('0.0001'))
        samples=[('step',''),('step','abc'),('step','10.55'),('tp','25'),('qty','0.00005'),('qty','0.00011'),
            ('grids','501'),('buys','26'),('real_tp','76'),('upper','-1'),('upper','80000.05')]
        for key,raw in samples:
            with self.subTest(key=key,raw=raw),self.assertRaises(ValueError):
                wizard.validate_numeric(key,raw,c)
        self.assertEqual(wizard.validate_numeric('qty','0.0001',c)/c['ct'],Decimal('.01'))
        self.assertEqual(wizard.validate_numeric('upper','80000',c),Decimal(80000))
        s=FakeScreen(['a','b','c','\n','1','0','.','0','\n'])
        result=wizard.numeric_input(s,'BOT',wizard.lines_for('step',c),lambda v:wizard.validate_numeric('step',v,c))
        self.assertEqual(result,Decimal('10.0'))
        self.assertTrue(any('Ошибка' in text for frame in s.frames for _,text in frame))
    def test_menu_keys(self):
        s=FakeScreen([wizard.curses.KEY_DOWN,wizard.curses.KEY_DOWN,'\n'])
        self.assertEqual(wizard.menu(s,'BOT',list(wizard.ASSETS)),2)
        self.assertIsNone(wizard.menu(FakeScreen(['\x1b']),'BOT',list(wizard.ASSETS)))
    def test_persist_without_overwrite_and_live_disabled(self):
        c=self.contract('SOL')
        c.update(step=Decimal('.05'),tp=Decimal('.2'),grids=Decimal(500),qty=Decimal('.01'),
                 buys=Decimal(25),real_tp=Decimal(75),upper=Decimal(80))
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'sol_bot'
            ret=wizard.persist(c,out,'OKX_API_KEY=x\nOKX_API_SECRET=y\nOKX_API_PASSPHRASE=z\n')
            self.assertEqual(ret,out)
            cfg=json.loads((out/'config.json').read_text())
            self.assertFalse(cfg['live_trading'])
            self.assertEqual(cfg['order_size'],'0.01')
            self.assertEqual(cfg['capital_reference_price'],'80')
            self.assertEqual(cfg['active_buy_orders'],25)
            self.assertEqual((out/'.env').stat().st_mode & 0o777,0o600)
            with self.assertRaisesRegex(RuntimeError,'не перезаписываем'):
                wizard.persist(c,out,'SECRET=another')

class EngineTests(unittest.TestCase):
    def make_engine(self,coin='BTC',price='83200',upper='80000',step='10',tp='50',qty='0.0001',cap=500):
        td=tempfile.TemporaryDirectory()
        root=Path(td.name)
        shutil.copy2(SRC/'okx_bot065.py',root/'okx_bot065.py')
        ct={'BTC':'0.01','ETH':'0.1','SOL':'1'}[coin]
        min_quantity={'BTC':'0.0001','ETH':'0.001','SOL':'0.01'}[coin]
        min_contract=Decimal(min_quantity)/Decimal(ct)
        cfg={'instrument':f'{coin}-USDT-SWAP','direction':'long','td_mode':'cross',
            'buy_type':'post_only','tp_type':'attached_limit','grid_step':step,
            'take_profit':tp,'order_size':str(Decimal(qty)/Decimal(ct)),
            'buy_base_qty':qty,'contract_value':ct,'contract_lot':'0.01',
            'contract_min':str(min_contract),'tick_size':'0.10' if coin=='BTC' else '0.01',
            'upper_price':upper,'capital_reference_price':upper if upper!='0' else price,
            'maker_fee_at_setup':'0.0002','active_buy_orders':25,
            'active_tp_orders':75,'max_grids':cap,'max_exchange_orders':500,
            'created_at':1720000000,'live_trading':False}
        (root/'config.json').write_text(json.dumps(cfg))
        (root/'.env').write_text('OKX_API_KEY=dev\nOKX_API_SECRET=dev\nOKX_API_PASSPHRASE=dev\nOKX_DEMO=0\n')
        (root/'state').mkdir();(root/'logs').mkdir()
        spec=importlib.util.spec_from_file_location('engine060_test',root/'okx_bot065.py')
        engine=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        return td,engine
    def test_internal_grid_self_tests(self):
        for coin,price,upper,step,tp,qty in [('BTC','83200','80000','10','50','.0001'),
               ('ETH','2505.21','2600','.25','2.5','.001'),
               ('SOL','106','121.9','.05','.2','.01')]:
            with self.subTest(coin=coin):
                td,e=self.make_engine(coin,price,upper,step,tp,qty)
                try:
                    e.run_tests()
                    self.assertEqual(e.INST,f'{coin}-USDT-SWAP')
                    self.assertEqual(e.REAL_TP_TARGET,75)
                finally:td.cleanup()
    def test_required_tp_and_virtual_lot(self):
        td,e=self.make_engine()
        try:
            self.assertEqual(e.required_safe_tp(Decimal('83200'),Decimal('0.0002')),Decimal('40.0'))
            self.assertEqual(e.QTY,Decimal('0.01'))
            self.assertEqual(e.CONTRACT_LOT,Decimal('0.01'))
        finally:td.cleanup()
    def test_sol_real_fill_pnl_and_capital_reference(self):
        td,e=self.make_engine('SOL','106','121.9','.05','.2','.01')
        try:
            st={'instrument':'SOL-USDT-SWAP','cycles':{'100.00':{
                'cycleId':'sol-synthetic','ordId':'BUY123','state':'CLOSED',
                'filledBuy':'0.01','filledTP':'0.01',
                'tpOrders':[{'ordId':'SELL123','filled':'0.01','phase':'FILLED'}]}},
                'archived_cycles':[],'completed':1,'started_at':1720001111}
            e.save(st)
            buy={'ordId':'BUY123','tradeId':'B1','billId':'X1','fee':'-0.000212',
                 'feeCcy':'USDT','side':'buy','execType':'M','fillPx':'106','fillSz':'0.01',
                 'ts':'1720001111000'}
            sell={'ordId':'SELL123','tradeId':'S1','billId':'X2','fee':'-0.0002124',
                  'feeCcy':'USDT','side':'sell','execType':'M','fillPx':'106.2','fillSz':'0.01',
                  'ts':'1720001112000'}
            result=e.full_stats_snapshot(st,[buy,sell],Decimal('106.2'),Decimal('1'))
            self.assertTrue(result['complete'])
            self.assertEqual(result['grid_gain'],Decimal('0.0015756'))
            report=e.format_weex_report(st,[buy,sell],result,now=1720002222)
            self.assertIn('Solana' if False else 'SOL GRID STATISTICS',report)
            self.assertIn('$547.13',report)
            self.assertIn('5.00000000 SOL',report)
        finally:td.cleanup()

    def test_live_statistics_publish_atomic_snapshot(self):
        td,e=self.make_engine('SOL','106','121.9','.05','.2','.01')
        try:
            st={'instrument':'SOL-USDT-SWAP','cycles':{},'archived_cycles':[]}
            e.DATA.write_text(json.dumps(st))
            fake={'complete':True}
            with patch.object(e,'LIVE',True), \
                 patch.object(e.A,'ticker',return_value=Decimal('106')), \
                 patch.object(e,'stats_exchange_fills',return_value=[]), \
                 patch.object(e,'full_stats_snapshot',return_value=fake), \
                 patch.object(e,'format_weex_report',return_value='📊 OKX SOL GRID STATISTICS\n✅ Все закрытые циклы сопоставлены.'):
                e.print_full_stats()
            obj=json.loads(e.STATS_FILE.read_text())
            self.assertEqual(obj['schema_version'],1)
            self.assertEqual(obj['instrument'],'SOL-USDT-SWAP')
            self.assertEqual(obj['bot_version'],'0.6.5')
            self.assertTrue(obj['complete'])
            self.assertTrue(obj['generated_at_unix']>0)
            self.assertIn('SOL GRID STATISTICS',obj['report'])
        finally:td.cleanup()

    def test_audit_or_partial_history_never_publish_as_complete(self):
        td,e=self.make_engine('SOL','106','121.9','.05','.2','.01')
        try:
            e.DATA.write_text(json.dumps({'instrument':'SOL-USDT-SWAP','cycles':{},'archived_cycles':[]}))
            with patch.object(e,'LIVE',False), \
                 patch.object(e.A,'ticker',return_value=Decimal('106')), \
                 patch.object(e,'stats_exchange_fills',return_value=[]), \
                 patch.object(e,'full_stats_snapshot',return_value={'complete':True}), \
                 patch.object(e,'format_weex_report',return_value='Read-only report'):
                e.print_full_stats()
            self.assertFalse(e.STATS_FILE.exists())
            with patch.object(e,'LIVE',True), \
                 patch.object(e.A,'ticker',return_value=Decimal('106')), \
                 patch.object(e,'stats_exchange_fills',return_value=[]), \
                 patch.object(e,'full_stats_snapshot',return_value={
                     'complete':False,'fill_orders_seen':0,
                     'fill_orders_expected':1,'missing':['X']}):
                e.print_full_stats()
            self.assertFalse(e.STATS_FILE.exists())
        finally:td.cleanup()

    def test_upper_waiting_never_places_buy(self):
        td,e=self.make_engine()
        class Market:
            def get_bid(self):return Decimal(83200)
        class Wake:
            def wait(self,timeout=None):raise StopIteration('stop test')
            def clear(self):pass
        class Watcher:
            events=queue.Queue();wake=Wake()
        class Pnl:
            def status(self,st):return 'ok'
        class Worker:
            def submit(self,st):pass
        called=[]
        with patch.object(e,'LIVE',True),patch.object(e,'reconcile_position',return_value=([],True,Decimal(0))),\
             patch.object(e,'maintain_virtual_tp',return_value='NONE'),patch.object(e.A,'maker_fee',return_value=Decimal('0.0002')),\
             patch.object(e,'place',side_effect=lambda *x:called.append(x)),\
             self.assertRaises(StopIteration):
            e.live_loop({'cycles':{},'completed':0},Watcher(),Market(),Pnl(),Worker())
        self.assertFalse(called)
        td.cleanup()
    def test_under_upper_one_buy_submission(self):
        td,e=self.make_engine(price='79200',upper='80000')
        class Market:
            def get_bid(self):return Decimal('79200')
        class Watcher:
            events=queue.Queue();wake=threading.Event()
        class Pnl:
            def status(self,st):return 'ok'
        class Worker:
            def submit(self,st):pass
        class StopLoop(Exception):pass
        buys=[]
        def fake_place(_,price):buys.append(price);raise StopLoop
        with patch.object(e,'LIVE',True),patch.object(e,'reconcile_position',return_value=([],True,Decimal(0))),\
             patch.object(e,'maintain_virtual_tp',return_value='NONE'),patch.object(e.A,'maker_fee',return_value=Decimal('0.0002')),\
             patch.object(e,'place',fake_place),self.assertRaises(StopLoop):
            e.live_loop({'cycles':{},'completed':0},Watcher(),Market(),Pnl(),Worker())
        self.assertEqual(buys[0],Decimal('79190'))
        td.cleanup()

if __name__=='__main__':unittest.main()
