"""Executable offline checks for the minimal 5-second audit; zero OKX calls."""
import ast
import threading
import time
import unittest
from pathlib import Path
from okx_idle_reconcile import audit_jitter_seconds, idle_deadline, wait_for_idle_reconcile, wait_error_backoff

ROOT=Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'okx_bot.py').read_text())
LOOP=next(n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='live_loop')
SOURCE=ast.unparse(LOOP)


def wait(wake=None,control=None,initial='RUN',status=None,earliest=None,period=.12,poll=.05):
    return wait_for_idle_reconcile(
        wake=wake or threading.Event(),control_read=control or (lambda:'RUN'),
        original_control=initial,ws_health=status or (lambda:(1,1,True,True)),
        initial_ws_health=(1,1,True,True),extra_due=earliest,
        max_wait_seconds=period,local_poll_seconds=poll)


class MinimalIdleTests(unittest.TestCase):
    def test_stable_jitter_range(self):
        for name in ('dood0735','near0721','btc0721','wld0728'):
            x=audit_jitter_seconds(name,'DOOD-USDT-SWAP')
            self.assertGreaterEqual(x,0.)
            self.assertLess(x,.8)
            self.assertEqual(x,audit_jitter_seconds(name,'DOOD-USDT-SWAP'))
        self.assertGreater(len({audit_jitter_seconds(n,'DOOD-USDT-SWAP') for n in ('a','b','c')}),1)

    def test_quiet_no_calls_to_exchange_and_timer(self):
        count=[]
        def control():count.append(1);return 'RUN'
        start=time.monotonic()
        cause=wait(control=control)
        self.assertEqual(cause,'timer')
        self.assertGreaterEqual(time.monotonic()-start,.10)
        self.assertGreaterEqual(len(count),2)

    def test_private_event_immediate(self):
        e=threading.Event()
        timer=threading.Timer(.025,e.set);timer.start()
        try:self.assertEqual(wait(wake=e,period=.2),'wake')
        finally:timer.join()

    def test_emergency_panics_locally_under_200ms(self):
        mode=['RUN']
        timer=threading.Timer(.025,lambda:mode.__setitem__(0,'PANIC'))
        timer.start()
        try:self.assertEqual(wait(control=lambda:mode[0],period=.2,poll=.05),'control')
        finally:timer.join()

    def test_ws_health_transition_wakes_but_no_timer_speedup(self):
        status=[(1,1,True,True)]
        timer=threading.Timer(.025,lambda:status.__setitem__(0,(2,1,True,True)))
        timer.start()
        try:self.assertEqual(wait(status=lambda:status[0],period=.2),'ws_health')
        finally:timer.join()
        self.assertEqual(wait(status=lambda:(1,1,True,True),period=.1),'timer')

    def test_buy_cooldown_and_tp_deadline_take_nearest(self):
        due=idle_deadline(now=10,buy_over_target=True,buy_shift_at=9.5,buy_cooldown=1.5,
            tp_over_target=True,tp_quiet_since=9.8,tp_cooldown=1.5,
            tp_last_cancel_at=None,tp_cancel_gap=.1,partial_expiries=[],wall_now=0)
        self.assertAlmostEqual(due,11.)

    def test_partial_expiry_not_overwritten(self):
        due=idle_deadline(now=10,buy_over_target=False,buy_shift_at=0,buy_cooldown=1.5,
            tp_over_target=True,tp_quiet_since=10,tp_cooldown=1.5,
            tp_last_cancel_at=None,tp_cancel_gap=.1,partial_expiries=[100.2,101.4],wall_now=100)
        self.assertAlmostEqual(due,10.2)

    def test_elapsed_due_does_not_spin(self):
        kwargs=dict(now=10,buy_over_target=True,buy_shift_at=0,buy_cooldown=1.5,
            tp_over_target=True,tp_quiet_since=0,tp_cooldown=1.5,
            tp_last_cancel_at=0,tp_cancel_gap=.1,partial_expiries=[0],wall_now=100)
        self.assertIsNone(idle_deadline(**kwargs))

    def test_extra_deadline_preempts_background_audit(self):
        self.assertEqual(wait(earliest=time.monotonic()+.03,period=.2),'timer')

    def test_reject_unsafe_wait_configuration(self):
        with self.assertRaises(ValueError):wait(poll=1.5)
        with self.assertRaises(ValueError):wait(period=120)

    def test_rest_failure_backoff_ignores_ws_noise(self):
        start=time.monotonic()
        self.assertEqual(wait_error_backoff(control_read=lambda:'RUN',
            original_control='RUN',seconds=.12,local_poll_seconds=.05),'backoff')
        self.assertGreaterEqual(time.monotonic()-start,.10)

    def test_panic_interrupts_rest_failure_backoff(self):
        mode=['RUN']
        t=threading.Timer(.03,lambda:mode.__setitem__(0,'PANIC'))
        t.start()
        try:
            start=time.monotonic()
            self.assertEqual(wait_error_backoff(control_read=lambda:mode[0],
                original_control='RUN',seconds=3,local_poll_seconds=.05),'control')
            self.assertLess(time.monotonic()-start,.2)
        finally:t.join()

    def test_private_public_ws_health_fails_closed_after_pong_deadline(self):
        import queue
        import types
        namespace={'threading':threading,'queue':queue,'time':time,
                   'WS_PONG_DEADLINE_SEC':10.0}
        for cls_name in ('WSWatcher','PublicBidWatcher'):
            cls=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name==cls_name)
            # Only the actual health logic, not networking or class initialization.
            healthy=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='healthy')
            env=dict(namespace)
            exec(compile(ast.fix_missing_locations(ast.Module(body=[healthy],type_ignores=[])),'<health>','exec'),env)
            watcher=types.SimpleNamespace(connected=False,_ping_outstanding_at=None,_pong_verified=False,PONG_DEADLINE_SEC=10.0)
            self.assertFalse(env['healthy'](watcher))
            watcher.connected=True
            self.assertFalse(env['healthy'](watcher), 'subscribe alone is insufficient')
            watcher._pong_verified=True
            self.assertTrue(env['healthy'](watcher))
            watcher._ping_outstanding_at=time.monotonic()-11
            self.assertFalse(env['healthy'](watcher))
            watcher._ping_outstanding_at=time.monotonic()-3
            self.assertTrue(env['healthy'](watcher))

    def test_real_loop_contains_protection_and_no_fallback_to_one_sec(self):
        self.assertIn('reconcile_position(',SOURCE)
        self.assertLess(SOURCE.index('reconcile_position('),SOURCE.index('coverage_audit('))
        self.assertLess(SOURCE.index('coverage_audit('),SOURCE.index('if place(st, price)'))
        self.assertIn('wait_for_idle_reconcile(',SOURCE)
        self.assertIn('BUY_HOLD_WS_HEALTH',SOURCE)
        self.assertIn('ws_epoch_at_start',SOURCE)
        self.assertIn('idle_deadline(',SOURCE)
        self.assertIn('if placed or moved:',SOURCE)
        self.assertNotIn('EVENT_DRIVEN_EXPERIMENT',SOURCE)
        self.assertNotIn('INDEPENDENT_AUDIT_SEC_EXPERIMENT',SOURCE)

    def test_ws_classes_ping_pong_and_epochs(self):
        classes={n.name:ast.unparse(n) for n in TREE.body if isinstance(n,ast.ClassDef)}
        for name in ('WSWatcher','PublicBidWatcher'):
            src=classes[name]
            self.assertIn('pong deadline exceeded',src)
            self.assertIn('connection_epoch',src)
            self.assertIn('self.connected = True',src)
            self.assertIn('def healthy(',src)
            self.assertIn('_pong_verified',src)


if __name__=='__main__':unittest.main()
