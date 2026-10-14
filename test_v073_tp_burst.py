#!/usr/bin/env python3
"""ETH-only regression: attached TP surge 25+30, cooling, base/overflow safety.
No network access, no real orders, no live state/files.
"""
import ast, copy, unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).parent
SOURCE=ROOT/'okx_bot073.py'
TREE=ast.parse(SOURCE.read_text())

def load():
    names={'TPBufferScheduler','tp_buffer_candidates','maintain_virtual_tp'}
    nodes=[copy.deepcopy(n) for n in TREE.body
           if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in names]
    assert {x.name for x in nodes} == names
    class Clock:
        now=0.0
        def monotonic(self):return self.now
        def time(self):return self.now
    clock=Clock()
    events=[]; cancels=[]; restores=[]
    ns={'time':clock,'D':Decimal,'REAL_TP_TARGET':25,'REAL_TP_RESERVE':30,
        'REAL_TP_MAX':55,'TP_BUFFER_COOLDOWN':1.5,'TP_BUFFER_CANCEL_INTERVAL':.1,
        'TP_ACTIONS_PER_PASS':5,'log':lambda *a,**kw:events.append((a,kw)),
        'die':lambda m:(_ for _ in ()).throw(AssertionError(m)),
        'coverage_audit':lambda st,pending,position:(len(st['real']),Decimal(0),Decimal(0)),
        'real_tp_active':lambda st:st['real'],
        'cancel_tp':lambda st,c,r:cancels.append((c['tpPx'],r['ordId'])) or 'CANCELING',
        'virtual_cycles':lambda st:st.get('virtuals',[]),
        'remaining_tp':lambda c:Decimal('0.03'),
        'tp_quantity_placeable':lambda x:True,
        'place_virtual_tp':lambda st,c:restores.append(c['cycleId'])}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),
                 str(SOURCE),'exec'),ns)
    return ns,clock,events,cancels,restores

def row(px):
    t=f'{Decimal(px):.2f}'
    return (t,{'tpPx':t,'cycleId':'ETH-TP-'+t},
            {'phase':'ACTIVE','clOrdId':'T-'+t,'ordId':'O-'+t})

class TestTPBurst(unittest.TestCase):
    def setUp(self):
        self.ns,self.clock,self.events,self.cancels,self.restores=load()
        self.buf=self.ns['TPBufferScheduler']()
        self.st={'real':[row(2430+Decimal(i)/2) for i in range(55)],'virtuals':[]}
        self.bid=Decimal('2432')
        self.action=lambda:self.ns['maintain_virtual_tp'](
            self.st,[],Decimal('1.65'),self.bid,self.buf)

    def test_25_no_cleanup(self):
        self.st['real']=self.st['real'][:25]
        self.assertEqual(self.action(),'NONE')
        self.assertEqual(self.cancels,[])

    def test_25_to_55_waits_1500ms(self):
        self.assertEqual(self.action(),'NONE')
        self.clock.now=1.499
        self.assertEqual(self.action(),'NONE')
        self.assertEqual(self.cancels,[])
        self.clock.now=1.5
        self.assertEqual(self.action(),'CANCEL')
        self.assertEqual(len(self.cancels),1)

    def test_nearest_25_always_base(self):
        ranked=sorted(self.st['real'],key=lambda item:(
            abs(Decimal(item[1]['tpPx'])-self.bid),
            Decimal(item[1]['tpPx']),item[2]['ordId']))
        base={x[2]['ordId'] for x in ranked[:25]}
        extra=self.ns['tp_buffer_candidates'](self.st['real'],self.bid)
        self.assertEqual(len(extra),30)
        self.assertTrue(all(rec['ordId'] not in base for _,_,rec in extra))
        self.assertEqual(extra[0][1]['tpPx'],'2457.00')

    def test_one_cancel_no_five_order_batch(self):
        self.action();self.clock.now=1.5
        self.assertEqual(self.action(),'CANCEL')
        self.assertEqual(len(self.cancels),1)
        # Until REST confirms cancellation, no next TP cancel may take place.
        self.assertEqual(self.action(),'NONE')
        self.clock.now=1.60
        self.st['real']=[x for x in self.st['real'] if x[2]['ordId']!=self.cancels[-1][1]]
        self.assertEqual(self.action(),'CANCEL')
        self.assertEqual(len(self.cancels),2)

    def test_new_attached_tp_restarts_cooldown(self):
        self.st['real']=self.st['real'][:54]
        self.action();self.clock.now=1.30
        self.st['real'].append(row('2457.50'))  # auto created by OKX on BUY fill
        self.assertEqual(self.action(),'NONE')
        self.clock.now=2.799
        self.assertEqual(self.action(),'NONE')
        self.clock.now=2.81
        self.assertEqual(self.action(),'CANCEL')

    def test_55_max_and_56_overflow(self):
        self.action();self.clock.now=.05
        self.st['real'].append(row('2457.50'))
        self.assertEqual(self.action(),'CANCEL', '56 attached TP triggers fast excess cleanup')
        self.assertEqual(len(self.cancels),1)
        self.assertEqual(self.events[-2][0][0],'TP_BUFFER_OVERFLOW')
        self.assertTrue(self.events[-1][1]['above_max'])

    def test_count_reduces_to_exact_25_not_below(self):
        self.action();self.clock.now=1.5
        while len(self.st['real'])>25:
            self.assertEqual(self.action(),'CANCEL')
            oid=self.cancels[-1][1]
            self.st['real']=[x for x in self.st['real'] if x[2]['ordId']!=oid]
            self.clock.now+=.105
        self.assertEqual(len(self.cancels),30)
        self.assertEqual(len(self.st['real']),25)
        self.assertEqual(self.action(),'NONE')
        self.assertEqual(len(self.cancels),30)
        for _,_,rec in self.st['real']:
            self.assertNotIn(rec['ordId'],{x[1] for x in self.cancels})

    def test_nearest_by_distance_not_price_only(self):
        test=[row(Decimal('2429')+Decimal(i)/2) for i in range(28)]
        extra=self.ns['tp_buffer_candidates'](test,Decimal('2432'))
        self.assertEqual(len(extra),3)
        self.assertEqual(extra[0][1]['tpPx'],'2442.50')
        # Unlike v072 sorting by highest price regardless of BID, this
        # will preserve near-market SELLs even across a reversal.
        test.append(row('2400.00'))
        extra=self.ns['tp_buffer_candidates'](test,Decimal('2432'))
        self.assertEqual(extra[0][1]['tpPx'],'2400.00')

    def test_rearm_first_when_below_25(self):
        self.st['real']=self.st['real'][:23]
        self.st['virtuals']=[('V',{'cycleId':'ETH-V', 'tpPx':'2433.00'})]
        self.assertEqual(self.action(),'RESTORE')
        self.assertEqual(self.restores,['ETH-V'])
        self.assertEqual(self.cancels,[])

    def test_virtual_market_escape_order_unchanged(self):
        tree=TREE
        n=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='live_loop')
        action=[x for x in ast.walk(n) if isinstance(x,ast.Call)
                and isinstance(x.func,ast.Name) and x.func.id in
                ('partial_cleanup_action','virtual_escape','maintain_virtual_tp')]
        names=[x.func.id for x in action]
        self.assertLess(names.index('partial_cleanup_action'),names.index('virtual_escape'))
        self.assertLess(names.index('virtual_escape'),names.index('maintain_virtual_tp'))
        self.assertGreaterEqual(names.count('maintain_virtual_tp'),2,
             'Cleanup at end of idle BUY pass must not starve BUY placement')

    def test_instant_attached_creation_not_blocked(self):
        # Check no code path routes exchange-created attached TP via
        # place_virtual_tp's normal 25-real throttle.
        n=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name=='place')
        texts=ast.unparse(n)
        self.assertIn('attachAlgoOrds',texts)
        self.assertNotIn('REAL_TP_MAX',texts)

    def test_real_sell_55_to_54_plus_virtual_preserves_exact_position(self):
        # Exercise REAL coverage_audit (not the burst test stub).  One canceled
        # SELL is turned into virtual exposure only after exchange confirmation.
        fn=next(n for n in TREE.body if isinstance(n,ast.FunctionDef)
                and n.name=='coverage_audit')
        module=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
        env={'D':Decimal,'Decimal':Decimal,'REDUCE_ONLY_HARD_CAP':150,
             'real_tp_active':lambda st:st['real'],
             'virtual_cycles':lambda st:st['virtuals'],
             'remaining_tp':lambda c:Decimal(c['remaining']),
             'unprotected_partial_buy':lambda st:Decimal('0'),
             'die':lambda m:(_ for _ in ()).throw(AssertionError(m))}
        exec(compile(module,str(SOURCE),'exec'),env)
        pending=[{'side':'sell','sz':'.03','accFillSz':'0'} for _ in range(55)]
        self.assertEqual(env['coverage_audit'](self.st,pending,Decimal('1.65')),
                         (55,Decimal('1.65'),Decimal('0')))
        # After an authoritative confirmed TP cancel, position did NOT change.
        self.st['real'].pop()
        self.st['virtuals']=[('v',{'remaining':'.03'})]
        self.assertEqual(env['coverage_audit'](self.st,pending[:54],Decimal('1.65')),
                         (54,Decimal('1.62'),Decimal('.03')))

    def test_saturated_55_count_does_not_block_new_buy_gate(self):
        n=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name=='live_loop')
        texts=ast.unparse(n)
        self.assertIn('REDUCE_ONLY_HARD_CAP - real_tp_count - 5',texts)
        self.assertNotIn('REAL_TP_MAX - real_tp_count',texts)

if __name__=='__main__':unittest.main()
