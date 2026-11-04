"""P0 regression: quiet elective-cancel timer must NOT delay real TP replenishment.
AST only, no imports of live engine, no network, no trading.
"""
import ast
import pathlib
import unittest
from decimal import Decimal

ROOT=pathlib.Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'okx_bot.py').read_text())
CODE=compile(ast.Module(body=[n for n in TREE.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in ('TPBufferScheduler','maintain_virtual_tp')],type_ignores=[]),str(ROOT/'okx_bot.py'),'exec')
class FakeClock:
    def __init__(self):self.t=100.0
    def monotonic(self):return self.t
    def time(self):return self.t

class TPRearmTests(unittest.TestCase):
    def env(self, target, real_count, last_fill=100.0, now=100.01):
        clock=FakeClock();clock.t=now
        real=[]
        for i in range(real_count):
            c={'cycleId':'r'+str(i),'tpPx':str(100+i),'state':'TP_WAIT','tpOrders':[],'buyPx':str(99+i)}
            rec={'phase':'ACTIVE','clOrdId':'t'+str(i),'ordId':'id'+str(i)}
            c['tpOrders']=[rec]; real.append((str(i),c,rec))
        virtual={'cycleId':'v','state':'VIRTUAL_TP','tpPx':'50',
                 'tpOrders':[], 'filledBuy':'1','filledTP':'0','buyPx':'49'}
        st={'cycles':{'v':virtual}}
        events=[]
        ns={'time':clock,'CLEANUP_LAST_FILL_AT':last_fill,
            'TP_BUFFER_CANCEL_INTERVAL':0.1,'TP_BUFFER_COOLDOWN':10.0,
            'REAL_TP_MAX':100,'BUY_MAX':6,'REAL_TP_TARGET':target,'TP_ACTIONS_PER_PASS':5,
            'coverage_audit':lambda *a:(len(real),None,None),
            'real_tp_active':lambda st:real,
            'virtual_cycles':lambda st:[('v',virtual)],
            'tp_quantity_placeable':lambda v:True,
            'remaining_tp':lambda c:Decimal('1'),
            'place_virtual_tp':lambda st,c:events.append(('RESTORE',c['cycleId'])) or True,
            'tp_buffer_candidates':lambda active,bid:active,
            'tp_price_limit_allows':lambda c:(True,Decimal('0')),
            'D':lambda v:Decimal(str(v)),
            'log':lambda *args,**kw:events.append(('LOG',args)),
            'die':lambda s:(_ for _ in ()).throw(RuntimeError(s)),
            'save':lambda *a:None}
        exec(CODE,ns)
        scheduler=ns['TPBufferScheduler']()
        def cancel(st,c,r,pressure):
            events.append(('CANCEL',c['cycleId']))
            return True
        def act():return ns['maintain_virtual_tp'](st,[],Decimal(12),Decimal(49),scheduler,cancel)
        return clock,ns,scheduler,events,act

    def test_zero_real_tp_rearms_immediately_after_confirmed_fill(self):
        _,_,_,events,act=self.env(target=1,real_count=0,now=100.03)
        self.assertEqual(act(),'RESTORE')
        self.assertEqual(events,[('RESTORE','v')])

    def test_below_configured_target_rearms_immediately(self):
        _,_,_,events,act=self.env(target=25,real_count=1,now=100.02)
        self.assertEqual(act(),'RESTORE')
        self.assertEqual(events,[('RESTORE','v')])

    def test_at_target_does_not_rotate_during_quiet_grace(self):
        clock,_,_,events,act=self.env(target=1,real_count=1,now=100.02)
        self.assertEqual(act(),'NONE')
        self.assertFalse(any(e[0]=='CANCEL' for e in events))
        clock.t=110.03
        self.assertEqual(act(),'CANCEL')
        self.assertEqual(len([e for e in events if e[0]=='CANCEL']),1)

    def test_above_target_does_not_cleanup_during_quiet_grace(self):
        clock,_,_,events,act=self.env(target=1,real_count=2,now=100.02)
        self.assertEqual(act(),'NONE')
        clock.t=110.03
        self.assertEqual(act(),'CANCEL')
        self.assertEqual(len([e for e in events if e[0]=='CANCEL']),1)

    def test_zero_real_tp_remains_prompt_even_after_newer_fill(self):
        _,ns,_,events,act=self.env(target=3,real_count=0,now=200.001,last_fill=200.0)
        self.assertEqual(act(),'RESTORE')
        self.assertEqual(events,[('RESTORE','v')])

    def test_emergency_cap_ignores_quiet_but_one_cancel_per_pass(self):
        _,ns,_,events,act=self.env(target=1,real_count=2,now=100.01)
        ns['BUY_MAX']=98
        self.assertEqual(act(),'CANCEL')
        self.assertEqual(act(),'NONE')
        self.assertEqual(len([e for e in events if e[0]=='CANCEL']),1)

if __name__=='__main__':unittest.main(verbosity=2)
