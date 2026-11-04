"""Offline deterministic 228-step capacity model using actual v0.7.54
TPBufferScheduler + maintain_virtual_tp AST. Exchange fills/cancel confirmations
are simulated instantly, so this is NOT the full execution grid stress test.
"""
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
import ast,unittest
SRC=Path(__file__).resolve().parents[1]/'okx_bot.py'
TREE=ast.parse(SRC.read_text())
N={n.name:n for n in TREE.body if isinstance(n,(ast.ClassDef,ast.FunctionDef))}

class FakeClock:
    def __init__(self):self.t=0.
    def monotonic(self):return self.t

class DeepGridCapacityModel(unittest.TestCase):
    def run_model(self,buy_max,tp_target):
        clock=FakeClock();events=[];state={'cycles':{}};all_fills=[]
        def active(st):
            return [(k,c,r) for k,c in st['cycles'].items()
                    for r in c['tpOrders'] if r['phase']=='ACTIVE']
        def coverage(st,pending,pos):return len(active(st)),None,None
        ns={'time':clock,'CLEANUP_LAST_FILL_AT':0.0,
            'TP_BUFFER_CANCEL_INTERVAL':.1,'TP_BUFFER_COOLDOWN':10.,
            'REAL_TP_MAX':100,'BUY_MAX':buy_max,
            'REAL_TP_TARGET':tp_target,'REDUCE_ONLY_HARD_CAP':150,
            'coverage_audit':coverage,'real_tp_active':active,
            'tp_buffer_candidates':lambda rows,bid:[(k,c,r) for k,c,r in rows],
            'log':lambda *a,**kw:events.append((a,kw)),
            'die':lambda s:(_ for _ in ()).throw(RuntimeError(s)),
            'D':Decimal}
        exec(compile(ast.Module(body=[N['TPBufferScheduler'],N['maintain_virtual_tp']],type_ignores=[]),str(SRC),'exec'),ns)
        scheduler=ns['TPBufferScheduler']()
        def cancel(st,c,r,emergency):
            self.assertTrue(emergency)
            r['phase']='CANCELED' # simulate *final confirmed* exchange cancel
            return True
        for step in range(228):
            clock.t+=.16
            if step < 200:
                # Every falling step fills one BUY and creates attached real TP.
                rec={'clOrdId':'tp'+str(step),'ordId':'id'+str(step),'phase':'ACTIVE'}
                state['cycles'][str(step)]={'cycleId':'c'+str(step),
                  'buyPx':str(1000-step),'tpPx':str(1001-step),
                  'tpOrders':[rec]}
                all_fills.append(step)
                ns['CLEANUP_LAST_FILL_AT']=clock.t  # never 10s quiet
            # One full confirmed exchange snapshot per tick.
            real=len(active(state))
            if real+buy_max+5>=100 and real>tp_target:
                self.assertEqual(ns['maintain_virtual_tp'](state,[],None,Decimal(2),scheduler,cancel), 'CANCEL')
            self.assertLessEqual(len(active(state))+buy_max+5,100)
        self.assertEqual(len(all_fills),200)
        real=len(active(state));virtual=200-real
        self.assertGreaterEqual(real,tp_target)
        self.assertGreater(virtual,0)
        self.assertEqual(real+virtual,200)
        return real,virtual
    def test_228_steps_grid_200_target_1_buy_max_30(self):
        self.assertEqual(sum(self.run_model(30,1)),200)
    def test_228_steps_grid_200_target_25_buy_max_30(self):
        self.assertEqual(sum(self.run_model(30,25)),200)
    def test_228_steps_grid_200_target_25_buy_max_6(self):
        self.assertEqual(sum(self.run_model(6,25)),200)

if __name__=='__main__':unittest.main(verbosity=2)
