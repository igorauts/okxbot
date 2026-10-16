import importlib.util
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]; ROOT=BASE/'v077' if (BASE/'v077').exists() else BASE
spec=importlib.util.spec_from_file_location('acct_test',ROOT/'okx_terminal_accounting_v077.py')
acct=importlib.util.module_from_spec(spec);spec.loader.exec_module(acct)


def fill(oid,bill,side,qty,px,fee):
    return dict(ordId=oid,billId=bill,tradeId=bill,side=side,fillSz=str(qty),
                fillPx=str(px),fee=str(fee),feeCcy='USDT',execType='T')

class FakeEngine:
    CONTRACT_VALUE=Decimal('1')
    COIN='SOL';INST='SOL-USDT-SWAP'
    @staticmethod
    def all_stat_cycles(st):return st['all_cycles']
    @staticmethod
    def expected_filled_orders(st):return {}
    @staticmethod
    def full_stats_snapshot(st,rows,bid,cv):
        qty=sum((Decimal(str(c.get('filledBuy',0)))-Decimal(str(c.get('filledTP',0)))
                 for c in st['all_cycles'] if c['state'] not in ('CLOSED','EXITED','CANCELED')),Decimal(0))
        return dict(complete=True,grid_gain=Decimal(str(st['grid_gain'])),
                    partial_exit_net=Decimal('0'),open_contracts=qty,closed_count=2)

class TerminalAccountingTests(unittest.TestCase):
    def test_panic_20_minus_6_equals_14(self):
        cycles=[dict(state='VIRTUAL_TP',ordId='b1',filledBuy='1',filledTP='0',tpOrders=[])]
        st=dict(terminal_mode='PANIC_DONE',all_cycles=cycles,grid_gain='20',
                panic_orders=[dict(ordId='p1',filled='1',state='filled')],
                terminal_snapshot=dict(position_contracts='0'))
        rows=[fill('b1','b-bill','buy',1,100,-.5),fill('p1','p-bill','sell',1,95,-.5)]
        d=acct.compute(FakeEngine,st,rows,bid='1')
        self.assertEqual(d['grid_profit'],Decimal('20'))
        self.assertEqual(d['panic_pnl'],Decimal('-6.0'))
        self.assertEqual(d['final_trade_pnl'],Decimal('14.0'))
        self.assertIn('ИТОГОВЫЙ PnL',acct.format_report(d,'SOL','2026-10-16'))

    def test_hard_snapshot_frozen_no_final(self):
        c=dict(state='VIRTUAL_TP',ordId='b1',filledBuy='2',filledTP='0',tpOrders=[])
        st=dict(terminal_mode='RETIRED',all_cycles=[c],grid_gain='20',
                terminal_snapshot=dict(position_contracts='2',bid='90'))
        rows=[fill('b1','bill','buy',2,100,-1)]
        d=acct.compute(FakeEngine,st,rows,bid='90')
        self.assertIsNone(d['final_trade_pnl'])
        self.assertEqual(d['frozen_unrealized'],Decimal('-20'))
        self.assertEqual(d['remaining_buy_fee'],Decimal('-1'))
        report=acct.format_report(d,'SOL','2026-10-16')
        self.assertNotIn('ИТОГОВЫЙ PnL:',report)
        self.assertIn('НЕ ОПРЕДЕЛЁН',report)

    def test_partial_tp_buy_fee_not_double_counted(self):
        c=dict(state='TP_PARTIAL',ordId='b1',filledBuy='2',filledTP='1',
               tpOrders=[dict(ordId='tp1',filled='1')])
        st=dict(terminal_mode='PANIC_DONE',all_cycles=[c],grid_gain='20',
                panic_orders=[dict(ordId='p1',filled='1',state='filled')],
                terminal_snapshot=dict(position_contracts='0'))
        rows=[fill('b1','b','buy',2,100,-2),fill('tp1','t','sell',1,110,-.5),
              fill('p1','p','sell',1,95,-.5)]
        d=acct.compute(FakeEngine,st,rows,bid='1')
        self.assertEqual(d['other_closed_profit'],Decimal('8.5'))
        self.assertEqual(d['panic_pnl'],Decimal('-6.5'))
        self.assertEqual(d['final_trade_pnl'],Decimal('22'))

    def test_panic_missing_execution_fails_closed(self):
        c=dict(state='VIRTUAL_TP',ordId='b1',filledBuy='1',filledTP='0',tpOrders=[])
        st=dict(terminal_mode='PANIC_DONE',all_cycles=[c],grid_gain='20',
                panic_orders=[dict(ordId='p1',filled='1',state='filled')])
        with self.assertRaises(ValueError):
            acct.compute(FakeEngine,st,[fill('b1','b','buy',1,100,-1)],bid='1')

    def test_panic_qty_mismatch_fails_closed(self):
        c=dict(state='VIRTUAL_TP',ordId='b1',filledBuy='2',filledTP='0',tpOrders=[])
        st=dict(terminal_mode='PANIC_DONE',all_cycles=[c],grid_gain='20',
                panic_orders=[dict(ordId='p1',filled='1',state='filled')])
        with self.assertRaises(ValueError):
            acct.compute(FakeEngine,st,[fill('b1','b','buy',2,100,-1),
                     fill('p1','p','sell',1,95,-1)],bid='1')

    def test_no_estimated_fees(self):
        c=dict(state='VIRTUAL_TP',ordId='b1',filledBuy='1',filledTP='0',tpOrders=[])
        st=dict(terminal_mode='RETIRED',all_cycles=[c],grid_gain='0')
        rows=[fill('b1','b','buy',1,100,-1)];rows[0]['fee']=None
        with self.assertRaises(ValueError):acct.compute(FakeEngine,st,rows,bid='99')

    def test_manager_selects_future_version_without_code_update(self):
        sys.modules['okx_setup068']=types.ModuleType('okx_setup068')
        spec=importlib.util.spec_from_file_location('manager077_test',ROOT/'okxbot.py')
        mgr=importlib.util.module_from_spec(spec);spec.loader.exec_module(mgr)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for name,version in [('okx_bot076.py','0.7.6'),('okx_bot077.py','0.7.7'),
                                 ('okx_bot078.py','0.7.8'),('okx_bot0710.py','0.7.10')]:
                (p/name).write_text("VER = '"+version+"'\n")
            self.assertEqual(mgr.latest_engine(p),('okx_bot0710.py','0.7.10'))
            (p/'okx_bot0710.py').write_text("VER = '0.7.9'\n")
            with self.assertRaises(RuntimeError):mgr.latest_engine(p)

    def test_dynamic_monitor_discovers_two_bots(self):
        spec=importlib.util.spec_from_file_location('tg077_test',ROOT/'okx_telegram_monitor.py')
        mon=importlib.util.module_from_spec(spec);spec.loader.exec_module(mon)
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)/'bots';base.mkdir()
            units=Path(tmp)/'systemd';units.mkdir()
            for name in ('foo','bar'):
                d=base/name;d.mkdir();(d/'config.json').write_text('{}')
                (units/f'okx-grid-{name}.service').write_text('[Service]\nWorkingDirectory='+str(d)+'\n')
            original=(mon.BASE,mon.REGISTRY,mon.SYSTEMD_DIR)
            try:
                mon.BASE=base.resolve();mon.REGISTRY=base/'registry-missing.json';mon.SYSTEMD_DIR=units
                found=mon.discover_bots()
                self.assertEqual(set(found),{'foo','bar'})
            finally:
                mon.BASE,mon.REGISTRY,mon.SYSTEMD_DIR=original

if __name__=='__main__':unittest.main()
