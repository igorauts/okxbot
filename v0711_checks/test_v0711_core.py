"""v0.7.11: offline safety and latency tests. Never import live engine or call OKX."""
import ast
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from unittest import mock

import okx_latency_v0711 as metrics
import okx_setup0711
import okx_panic_cleanup0711
import okx_monitor0711
import okx_latency_report0711
import io
import okxbot

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'okx_bot0711.py').read_text()
TREE = ast.parse(SRC)
NODES = {n.name:n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


class Clock:
    def __init__(self): self.t = 100.0; self.sleeps=[]
    def monotonic(self): return self.t
    def sleep(self, duration): self.sleeps.append(duration); self.t += duration
    def perf_counter_ns(self): return int(self.t * 1_000_000_000)


class ReconcileTests(unittest.TestCase):
    def test_retry_delays_are_conservative_and_bounded(self):
        ns={}
        exec(compile(ast.Module(body=[NODES['position_retry_delay']],type_ignores=[]),'<retry>','exec'),ns)
        fn=ns['position_retry_delay']
        self.assertEqual([fn(i) for i in range(1,6)],[.15,.30,.50,.50,.50])
        self.assertEqual(fn(1,pause=.1),.1)
        self.assertEqual(fn(2,pause=.2),.2)

    def reconcile(self, positions, timeout=30, soft=False):
        clk=Clock(); events=[]; calls=[]
        pos_iter=iter(positions)
        A=SimpleNamespace(
            pending=lambda:calls.append('pending') or [{'side':'buy','ordId':'B1'}],
            positions=lambda:calls.append('positions') or [{'pos':str(next(pos_iter))}])
        ns={'_PERF_RECON_ENABLED':False,'_PERF_LOCAL':SimpleNamespace(current=None),
            '_ReconProfiler':lambda:None, 'time':clk, 'sys':__import__('sys'),
            'A':A,'_batch_confirmed_facts':lambda s:contextlib.nullcontext(),
            'register_cancel_attached':lambda *a:calls.append('attached'),
            'track':lambda *a:calls.append('track') or True,
            'verify_existing':lambda *a:calls.append('verified'),
            'panic_executions':lambda *a:Decimal(0), 'D':lambda x:Decimal(str(x or 0)),
            'Decimal':Decimal,'log':lambda *a,**kw:events.append((a,kw)),
            'ExchangeUnresolved':RuntimeError}
        exec(compile(ast.Module(body=[NODES[x] for x in
            ('position_retry_delay','reconcile_position')],type_ignores=[]),
            '<reconcile>','exec'),ns)
        st={'cycles':{'1':{'filledBuy':'0','filledTP':'0'}}, 'panic_orders':[]}
        return ns['reconcile_position'],st,clk,calls,events

    def test_all_retries_require_full_snapshot(self):
        fn,st,clock,calls,events=self.reconcile([1,1,1,0])
        pending,resolved,actual=fn(st)
        self.assertTrue(resolved)
        self.assertEqual(actual,Decimal(0))
        self.assertEqual(clock.sleeps,[.15,.30,.50])
        self.assertEqual(calls,['pending','attached','track','verified','positions']*4)
        self.assertEqual([x[0][0] for x in events],['POSITION_SYNC_WAIT','POSITION_SYNC_OK'])

    def test_persistent_mismatch_fails_closed(self):
        fn,st,clock,calls,_=self.reconcile([1]*20)
        with self.assertRaisesRegex(RuntimeError,'Position mismatch persisted'):
            fn(st,timeout=.7)
        self.assertTrue(calls)
        self.assertNotIn('POST',calls)
        self.assertIn('positions',calls)

    def test_soft_mode_mismatch_never_enables_buys(self):
        fn,st,clock,calls,_=self.reconcile([1]*20)
        _,resolved,actual=fn(st,timeout=.7,allow_mismatch=True)
        self.assertFalse(resolved)
        self.assertEqual(actual,Decimal(1))

    def test_safety_critical_functions_bytecode_structurally_unchanged(self):
        older=ast.parse((ROOT/'okx_bot0710.py').read_text())
        old={n.name:n for n in older.body if isinstance(n,(ast.ClassDef,ast.FunctionDef))}
        for name in ('coverage_audit','track','verify_existing','virtual_escape',
                     'partial_cleanup_action','maintain_virtual_tp',
                     'cancel_tp','cancel_lowest_specific','place_panic_sell',
                     'controlled_shutdown','desired_buy_levels',
                     'missing_buy_levels_for_addition'):
            with self.subTest(name=name):
                self.assertEqual(ast.dump(old[name],include_attributes=False),
                                 ast.dump(NODES[name],include_attributes=False))

    def test_priority_order_is_coverage_safe(self):
        body=ast.get_source_segment(SRC,NODES['live_loop'])
        self.assertLess(body.index('coverage_audit(st,pending,actual)'),
                        body.index('            placed=0'))
        self.assertLess(body.index('partial_cleanup_action(st,bid,actual,mode)'),
                        body.index('            placed=0'))
        self.assertLess(body.index('virtual_escape(st,bid,actual,pending,mode)'),
                        body.index('            placed=0'))
        self.assertIn('if real_tp_now>=REAL_TP_TARGET:',body)
        self.assertIn('else:\n                tp_action=maintain_virtual_tp(',body)
        self.assertGreater(body.index('rotate_action=maintain_virtual_tp('),
                           body.index('            placed=0'))
        self.assertIn('not placed and not moved',body)
        self.assertIn('len(real_tp_active(st))==REAL_TP_TARGET',body)
        self.assertIn('if not all_resolved:',body)
        self.assertIn("if control_mode()!='RUN':break",body)


class TelemetryTests(unittest.TestCase):
    def test_disabled_never_starts_worker(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'latency.jsonl'; j=metrics.LatencyJournal(p,enabled=False)
            j.record('http',30,route='/api/v5/account/positions')
            self.assertIsNone(j._thread)
            self.assertFalse(p.exists())

    def test_fields_allowlisted_and_no_query_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'latency.jsonl'
            j=metrics.LatencyJournal(p)
            j.record('http',92.17153,method='GET',route='/api/v5/trade/orders-pending',
                     api_key='SECRET_DO_NOT_LOG',headers='SECRET_DO_NOT_LOG',
                     body='SECRET_DO_NOT_LOG',token='SECRET_DO_NOT_LOG')
            j.record('http',24,route='/api/v5/trade/order?secret=SECRET_DO_NOT_LOG')
            j.queue.join()
            data=p.read_text()
            self.assertNotIn('SECRET_DO_NOT_LOG',data)
            lines=[json.loads(x) for x in data.splitlines()]
            self.assertEqual(lines[0]['ms'],92.172)
            self.assertEqual(lines[0]['route'],'/api/v5/trade/orders-pending')
            self.assertNotIn('route',lines[1])

    def test_drop_when_overloaded_never_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            j=metrics.LatencyJournal(Path(td)/'latency.jsonl',max_queue=1)
            j._thread=object() # force no writer for deterministic full queue
            j.record('http',1)
            j.record('http',2)
            self.assertEqual(j.dropped,1)
            self.assertEqual(j.queue.qsize(),1)

    def test_journal_rotates_and_is_private(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'latency.jsonl';j=metrics.LatencyJournal(p)
            j.MAX_BYTES=350
            for i in range(25):j.record('buy_post',i,outcome='accepted')
            j.queue.join()
            self.assertTrue(p.exists())
            self.assertTrue((Path(td)/'latency.jsonl.1').exists())
            self.assertEqual(p.stat().st_mode & 0o777,0o600)


class ReportTests(unittest.TestCase):
    def test_percentiles_and_local_report(self):
        self.assertEqual(okx_latency_report0711.percentile([10,20,30],.50),20)
        self.assertEqual(okx_latency_report0711.percentile([10,20],.95),19.5)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'logs'/'latency_v0711.jsonl'
            p.parent.mkdir()
            now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)
            rows=[dict(ts=now.isoformat(),phase='position_reconcile',ms=x,dropped=2)
                  for x in (100,200,300)]
            p.write_text('\n'.join(json.dumps(x) for x in rows))
            buf=io.StringIO()
            with contextlib.redirect_stdout(buf):
                n=okx_latency_report0711.report(Path(td),4,now=now)
            self.assertEqual(n,3)
            self.assertIn('200.0',buf.getvalue())
            self.assertIn('dropped since process start: 2',buf.getvalue())


class IntegrationTests(unittest.TestCase):
    def test_wizard_and_manifest_latest(self):
        self.assertEqual(okxbot.latest_engine(ROOT),('okx_bot0711.py','0.7.11'))
        self.assertEqual(okxbot.latest_dependencies('okx_bot0711.py','0.7.11',ROOT),
                         ['okx_terminal_accounting_v077.py','okx_latency_v0711.py'])
        self.assertEqual(okx_setup0711.VER,'0.7.11')

    def test_systemd_cleanup_versioned(self):
        p=Path('/root/okx/sui0711')
        new=okxbot.unit_text(p,'okx_bot0711.py','sui0711')
        old=okxbot.unit_text(p,'okx_bot0710.py','sui0710')
        self.assertIn('okx_panic_cleanup0711.py',new)
        self.assertIn('okx_panic_cleanup0710.py',old)
        self.assertIn('ExecStart=/root/okx/sui0711/.venv/bin/python -u /root/okx/sui0711/okx_bot0711.py',new)

    def test_manager_new_monitor_on_dispatch_is_versioned(self):
        import okx_monitor_compat078 as compat
        with tempfile.TemporaryDirectory() as td:
            r=Path(td);(r/'state').mkdir()
            (r/'state/bot04_state.json').write_text(json.dumps({'version':'0.7.11'}))
            with mock.patch.object(compat.legacy, 'service_active', return_value=False), \
                 mock.patch.object(compat, '_previous_monitor_action_0711') as prior, \
                 mock.patch.object(compat.subprocess,'run') as runner:
                compat.monitor_action('okx-grid-test711',r,'burst','on',ROOT)
                self.assertEqual(runner.call_count,1)
                args=runner.call_args.args[0]
                self.assertIn('okx_monitor0711.py',str(args))
                prior.assert_not_called()

    def test_new_cleanup_fails_closed_when_not_panic(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'bot';(root/'state').mkdir(parents=True)
            (root/'config.json').write_text(json.dumps({'version':'0.7.11','instrument':'SUI-USDT-SWAP'}))
            st={'version':'0.7.11','instrument':'SUI-USDT-SWAP','terminal_mode':'RUN'}
            (root/'state/bot04_state.json').write_text(json.dumps(st))
            self.assertFalse(okx_panic_cleanup0711.confirmed_panic(root))

    def test_monitors_accept_v0711_only(self):
        self.assertIn('v0711',okx_monitor0711.monitor_unit('okx-grid-sui0711','burst'))
        mon=(ROOT/'okx_execution_observer0711.py').read_text()
        self.assertIn('0.7.11',mon)
        self.assertIn('v0711',mon)


if __name__=='__main__':unittest.main()
