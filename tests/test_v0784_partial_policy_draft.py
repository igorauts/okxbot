"""Draft only: source thresholds and exact boundary; no real exchange or files."""
import ast
import time
import unittest
from decimal import Decimal
from pathlib import Path
from tests import test_partial_attached_isolation as legacy

ROOT=Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'okx_bot.py').read_text())

def scope(cfg):
    nodes=[n for n in TREE.body if (
        isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('PARTIAL_AGE','PARTIAL_STEPS') for t in n.targets)
        or isinstance(n,ast.If) and 'Invalid partial BUY cleanup thresholds' in ast.unparse(n)
        or isinstance(n,ast.FunctionDef) and n.name=='partial_cleanup_due')]
    assert len(nodes)==4
    ns={'CFG':cfg,'Decimal':Decimal,'D':lambda x:Decimal(str(x or 0)), 'STEP':Decimal('0.0006'),'time':time}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<offline>','exec'),ns)
    return ns

class Thresholds(unittest.TestCase):
    def test_default_and_explicit_new_values(self):
        for cfg in ({},{'partial_buy_timeout_sec':5,'partial_buy_distance_steps':3}):
            ns=scope(cfg)
            self.assertEqual((ns['PARTIAL_AGE'],ns['PARTIAL_STEPS']),(5,Decimal(3)))
    def test_exact_and_boundary(self):
        f=scope({})['partial_cleanup_due']
        c={'state':'BUY_PARTIAL','filledBuy':'2','buyPx':'0.3954','partial_since':100.0}
        for age,price,expected in [(4.999,'0.3972',False),(5,'0.3971',False),(5,'0.3972',True),(5,'0.3950',False),(10,'0.3972',True)]:
            with self.subTest(age=age,price=price):self.assertEqual(f(c,Decimal(price),100+age),expected)
        for state,filled in [('BUY_LIVE','0'),('TP_WAIT','2'),('BUY_PARTIAL','0')]:
            self.assertFalse(f(dict(c,state=state,filledBuy=filled),Decimal('.4000'),200))
    def test_invalid_time_rejected(self):
        with self.assertRaises(RuntimeError):scope({'partial_buy_timeout_sec':4})
    def test_legacy_explicit_values_not_silently_overridden(self):
        ns=scope({'partial_buy_timeout_sec':60,'partial_buy_distance_steps':5})
        self.assertEqual((ns['PARTIAL_AGE'],ns['PARTIAL_STEPS']),(60,Decimal(5)))

class Reproduction(unittest.TestCase):
    def test_legacy_unresolved_cycle_not_force_closed(self):
        case=legacy.Cases();case.setUp()
        result,isolated=case.pass_()
        self.assertEqual(isolated,{legacy.TPID})
        self.assertFalse(result[1])
        calls=[]
        case.ns['market_exit_partial']=lambda *a:calls.append(a) or True
        action=case.ns['partial_cleanup_action'](case.st,Decimal('.4100'),case.pos,'RUN',isolated=isolated,reserved_qty=Decimal(2))
        self.assertEqual(action,'NONE')
        self.assertEqual(calls,[])
        self.assertEqual(case.rec['phase'],'PENDING')
        print('LEGACY_RESERVE_UNCHANGED: parent canceled; exact reserve isolated; market not attempted despite higher BID')

if __name__=='__main__':unittest.main()
