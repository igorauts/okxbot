"""Local quota safety after removing synchronous disk flush. No API calls."""
import gzip
import hashlib
import json
import os
import subprocess
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from okx_positions_fifo import AccountPositionsFIFO, FillsHistoryFIFO, GateUnavailable
from okx_positions_priority import PriorityPositionsFIFO

class EphemeralGovernor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'gate.json'
        self.gate = AccountPositionsFIFO(self.path)
        self.path.write_text('{"schema":1,"calls":[]}')

    def test_no_fsync_and_one_shared_quota(self):
        other = PriorityPositionsFIFO(self.path)
        with patch('os.fsync', side_effect=AssertionError('unexpected fsync')) as sync:
            for idx in range(self.gate.MAX_SHARED):
                client = self.gate if idx % 2 else other
                self.assertTrue(client._visit(str(idx), None)[0])
            self.assertFalse(other._visit('blocked', None)[0])
            sync.assert_not_called()
        self.assertEqual(len(json.loads(self.path.read_text())['calls']), 9)
        self.assertEqual(self.gate.queue_path, other.queue_path)

    def test_real_mixed_processes_share_one_inode_and_quota(self):
        # Byte-pinned previous FIFO is data, never part of installed runtime.
        old_path = Path(__file__).parent/'fixtures/okx_positions_fifo_v0788.py.gz'
        old = gzip.decompress(old_path.read_bytes())
        manifest = json.loads((Path(__file__).parents[1]/'engine_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(old).hexdigest(),
                         manifest['engines']['0.7.88']['dependency_sha256']['okx_positions_fifo.py'])
        script = """
import gzip, os, sys, time, types
from pathlib import Path
variant, gate_path, old_path, start, ready, ticket, kind = sys.argv[1:]
if variant == 'old':
    module = types.ModuleType('old_fifo')
    exec(compile(gzip.decompress(Path(old_path).read_bytes()), 'PINNED_OLD_FIFO', 'exec'), module.__dict__)
else:
    import okx_positions_fifo as module
cls = module.AccountPositionsFIFO if kind == 'positions' else module.FillsHistoryFIFO
# A fixed wall clock isolates the quota boundary from slow process startup;
# lock acquisition and all filesystem operations remain real.
module.time.time = lambda: 1000.0
client = cls(gate_path)
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.005)
result, _ = client._visit(ticket, None)
print(int(result), flush=True)
if not result:
    client._visit(ticket, True, cancel=True)
"""
        for kind in ('positions','fills'):
            with self.subTest(kind=kind):
                self.path.write_text('{"schema":1,"calls":[]}')
                self.gate.queue_path.write_text('{"schema":1,"waiters":[]}')
                inode = self.path.stat().st_ino
                start = Path(self.tmp.name)/('start-'+kind)
                jobs=[]
                try:
                    for idx in range(12):
                        ready=Path(self.tmp.name)/f'{kind}-ready-{idx}'
                        proc=subprocess.Popen([sys.executable,'-B','-c',script,
                            'old' if idx%2 else 'new',str(self.path),str(old_path),
                            str(start),str(ready),str(idx),kind],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                        jobs.append((proc,ready))
                    deadline=time.monotonic()+10
                    while not all(ready.exists() for _,ready in jobs):
                        self.assertLess(time.monotonic(),deadline,'children not ready')
                        time.sleep(.01)
                    start.touch()
                    grants=[]
                    for proc,_ in jobs:
                        out,err=proc.communicate(timeout=10)
                        self.assertEqual(proc.returncode,0,err)
                        grants.append(int(out.strip()))
                    self.assertEqual(sum(grants),9)
                    self.assertEqual(self.path.stat().st_ino,inode)
                    self.assertEqual(json.loads(self.path.read_text())['calls'],[1000.0]*9)
                    self.assertEqual(json.loads(self.gate.queue_path.read_text())['waiters'],[])
                finally:
                    for proc,_ in jobs:
                        if proc.poll() is None:
                            proc.kill()
                        proc.communicate(timeout=5)

    def test_host_crash_valid_stale_quota_cannot_grant_during_boot_window(self):
        # Simulate a valid pre-crash persisted snapshot missing all nine
        # newer reservations; unlike a truncated JSON, it passes parsing.
        for cls in (AccountPositionsFIFO, FillsHistoryFIFO):
            gate = cls(self.path)
            self.path.write_text('{"schema":1,"calls":[]}')
            original = self.path.read_bytes()
            inode = self.path.stat().st_ino
            for wall in (1000.1, 2000000000.0, 1.0):
                for uptime in (0.0, 0.1, gate.WINDOW - .001):
                    with self.subTest(cls=cls.__name__, uptime=uptime, wall=wall):
                        with patch('okx_positions_fifo.time.clock_gettime', return_value=uptime), \
                             patch('okx_positions_fifo.time.time', return_value=wall):
                            granted, delay = gate._reserve_if_available(0)
                        self.assertFalse(granted)
                        self.assertAlmostEqual(delay, gate.WINDOW-uptime)
                        self.assertEqual(self.path.read_bytes(),original)
                        self.assertEqual(self.path.stat().st_ino,inode)
            with patch('okx_positions_fifo.time.clock_gettime', return_value=gate.WINDOW), \
                 patch('okx_positions_fifo.time.time', return_value=1000.1):
                self.assertTrue(gate._reserve_if_available(0)[0])
            self.assertEqual(self.path.stat().st_ino,inode)

    def test_unverified_boot_age_fails_closed(self):
        original = self.path.read_bytes()
        for age in (float('nan'), float('inf'), -1.0):
            with patch('okx_positions_fifo.time.clock_gettime', return_value=age):
                with self.assertRaises(GateUnavailable):
                    self.gate._reserve_if_available(0)
        with patch('okx_positions_fifo.time.clock_gettime', side_effect=OSError('clock')):
            with self.assertRaises(GateUnavailable):
                self.gate._reserve_if_available(0)
        self.assertEqual(self.path.read_bytes(), original)

    def test_normal_process_start_has_no_boot_delay(self):
        with patch('okx_positions_fifo.time.clock_gettime', return_value=10000), \
             patch('okx_positions_fifo.time.sleep', side_effect=AssertionError('sleep')):
            self.assertTrue(self.gate._reserve_if_available(0)[0])

    def test_legacy_systemd_barrier_uses_same_full_window(self):
        # The actual deployment template's Python is exercised, not a second
        # implementation of the boot-safety calculation.
        template = (Path(__file__).parents[1]/'deploy/reader/05-quota-boot-safety.conf').read_text()
        command, = (line.partition('=')[2] for line in template.splitlines()
                    if line.startswith('ExecStartPre='))
        args = shlex.split(command)
        self.assertEqual(args[:4], ['/usr/bin/python3','-I','-B','-c'])
        self.assertEqual(len(args),5)
        cases = ((.1, 2.1, 0), (2.1, 2.1, 0), (1000, 1000, 0),
                 (.1, .2, 1), (float('nan'), float('nan'), 1), (-1,-1,1))
        for first,second,expected in cases:
            with self.subTest(first=first,second=second):
                with patch('time.clock_gettime', side_effect=[first,second]), patch('time.sleep') as sleep:
                    with self.assertRaises(SystemExit) as ex:
                        exec(compile(args[4], '<actual ExecStartPre>', 'exec'), {})
                    self.assertEqual(ex.exception.code,expected)
                    if 0 <= first < FillsHistoryFIFO.WINDOW:
                        sleep.assert_called_once_with(FillsHistoryFIFO.WINDOW-first)
                    else:
                        sleep.assert_not_called()

    def test_prefix_barrier_only_for_legacy_including_stopped_not_terminal(self):
        import shutil
        from contextlib import ExitStack
        import okx_live_upgrade as upgrade
        source=Path(__file__).parents[1]
        template=source/'deploy/reader/05-quota-boot-safety.conf'
        unit_dir=Path(self.tmp.name)/'systemd';base=Path(self.tmp.name)/'bots';base.mkdir()
        manifest=json.loads((source/'engine_manifest.json').read_text())
        version=manifest['current_version']
        legacy=next(v for v in manifest['engines'] if v!=version)
        active='okx-grid-active.service';stopped='okx-grid-stopped.service'
        terminal='okx-grid-terminal.service';modern='okx-grid-future.service'
        reader='okx-position-reader.service'
        prefix=unit_dir/'okx-grid-.service.d'/template.name
        reader_path=unit_dir/(reader+'.d')/template.name
        for path in (prefix,reader_path):
            path.parent.mkdir(parents=True);path.write_bytes(template.read_bytes())
        command,=(line.partition('=')[2] for line in template.read_text().splitlines() if line.startswith('ExecStartPre='))
        argv=shlex.split(command)
        effective=[argv[0],argv,False,0,0,0,0,0,0,0]
        props={};entries={}
        for unit in (active,stopped,terminal,modern):
            root=base/unit.removesuffix('.service');root.mkdir();(root/'state').mkdir()
            v=version if unit==modern else legacy
            (root/'config.json').write_text(json.dumps({'version':v}))
            (root/'state/bot_state.json').write_text(json.dumps({'version':v,'terminal_mode':'PANIC_DONE' if unit==terminal else 'RUN'}))
            entries[unit]={'path':str(root),'service':unit.removesuffix('.service')}
            props[unit]=dict(LoadState='loaded',NeedDaemonReload='no',DropInPaths=str(prefix),
                             ActiveState='active' if unit==active else 'inactive',MainPID='123' if unit==active else '0',
                             UnitFileState='enabled' if unit==active else 'disabled',WorkingDirectory=str(root))
            if unit==modern:
                info=manifest['engines'][version]
                for name in [info['file']]+info['dependencies']:shutil.copy2(source/name,root/name)
        props[reader]=dict(LoadState='loaded',NeedDaemonReload='no',DropInPaths=str(reader_path))
        rows={u:[effective.copy()] for u in (active,stopped,reader)}
        seen=[]
        def properties(unit,*keys):return {k:props[unit][k] for k in keys}
        def bus(method,signature,*args):
            self.assertEqual((method,signature),('LoadUnit','s'));return args[0]
        def run(*args,**kwargs):
            self.assertEqual(args[0],'busctl');unit=args[-3];seen.append(unit)
            return SimpleNamespace(stdout=json.dumps({'type':'a(sasbttttuii)','data':rows[unit]}))
        with ExitStack() as stack:
            for name,value in (('STAGE',source),('BASE',base)):
                stack.enter_context(patch.object(upgrade,name,value))
            stack.enter_context(patch.object(upgrade,'quota_unit_inventory',return_value=(set(entries),entries)))
            stack.enter_context(patch.object(upgrade,'guarded_reader_running',return_value=False))
            stack.enter_context(patch.object(upgrade,'systemd_properties',side_effect=properties))
            stack.enter_context(patch.object(upgrade,'systemd_bus',side_effect=bus))
            stack.enter_context(patch.object(upgrade,'run',side_effect=run))
            upgrade.verify_quota_boot_barriers(unit_dir)
            self.assertEqual(set(seen),{active,stopped,reader});self.assertNotIn(terminal,seen);self.assertNotIn(modern,seen)
            self.assertEqual(len(list(unit_dir.rglob('*.conf'))),2)
            for key,value in (('DropInPaths',''),('NeedDaemonReload','yes')):
                saved=props[stopped][key];props[stopped][key]=value
                with self.assertRaisesRegex(RuntimeError,'not effective'):upgrade.verify_quota_boot_barriers(unit_dir)
                props[stopped][key]=saved
            for invalid in ([],[effective[:2]+[True]+effective[3:]],[effective,effective],
                            [[argv[0],argv[:4]+argv[4].split(),False,*effective[3:]]]):
                rows[stopped]=invalid
                with self.assertRaisesRegex(RuntimeError,'not effective'):upgrade.verify_quota_boot_barriers(unit_dir)
            rows[stopped]=[effective]
            prefix.write_text('changed')
            with self.assertRaisesRegex(RuntimeError,'missing or drifted'):upgrade.verify_quota_boot_barriers(unit_dir)
            prefix.unlink()
            with self.assertRaisesRegex(RuntimeError,'Unsafe or missing'):upgrade.verify_quota_boot_barriers(unit_dir)
            # A future guarded bot requires no per-unit file; after all legacy
            # consumers are gone and Reader is attested, even both drop-ins may be absent.
            for unit in (active,stopped):
                props[unit]['ActiveState']='inactive';props[unit]['MainPID']='0';props[unit]['UnitFileState']='disabled'
                (Path(props[unit]['WorkingDirectory'])/'state/bot_state.json').write_text(json.dumps({'terminal_mode':'RETIRED'}))
            reader_path.unlink()
            with patch.object(upgrade,'guarded_reader_running',return_value=True):
                seen.clear();upgrade.verify_quota_boot_barriers(unit_dir);self.assertEqual(seen,[])

    def test_structured_inventory_includes_registry_nonprefix_and_skips_notfound(self):
        import okx_live_upgrade as upgrade
        base=Path(self.tmp.name)
        (base/'.okxbot_registry.json').write_text(json.dumps({'odd':{'service':'okx-custom','path':str(base/'odd')}}))
        a='okx-grid-alpha.service';ghost='okx-grid-ghost.service'
        def bus(method,signature,*args):
            self.assertEqual(signature,'asas')
            if method=='ListUnitFilesByPatterns':return [[f'/etc/systemd/system/{a}','enabled']]
            if method=='ListUnitsByPatterns':return [[a,'desc','loaded','active'],[ghost,'desc','not-found','inactive']]
            self.fail(method)
        with patch.object(upgrade,'BASE',base),patch.object(upgrade,'systemd_bus',side_effect=bus):
            units,entries=upgrade.quota_unit_inventory()
        self.assertEqual(units,{a,'okx-custom.service'});self.assertIn('okx-custom.service',entries)

    def test_telegram_reload_gate_uses_completed_shared_receipt_and_live_invocation(self):
        import shutil
        import okx_live_upgrade as upgrade
        from contextlib import ExitStack
        source=Path(__file__).parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        root=Path(self.tmp.name)/'tg';root.mkdir();shared=Path(self.tmp.name)/'shared';shared.mkdir()
        funding=Path(self.tmp.name)/'funding';funding.mkdir()
        backups=Path(self.tmp.name)/'backups';backups.mkdir();receipt=backups/'one-shared';receipt.mkdir()
        for name in inventory['shared']:shutil.copy2(source/name,shared/name)
        for name in inventory['telegram']:shutil.copy2(source/name,root/name)
        for dest,src in inventory['funding'].items():shutil.copy2(source/src,funding/dest)
        (receipt/'activation.json').write_text(json.dumps({'manifest_sha256':upgrade.sha256(shared/'engine_manifest.json'),
                        'boot_id':upgrade.boot_id(),'monotonic_us':1000}))
        props=dict(ActiveState='active',SubState='running',MainPID='1',InvocationID='a'*32,
                   ExecMainStartTimestampMonotonic='1001',NeedDaemonReload='no')
        journal='OKX Telegram monitor running T1-T4\n'
        with ExitStack() as stack:
            for name,value in (('STAGE',source),('SHARED',shared),('TELEGRAM_ROOT',root),('FUNDING_ROOT',funding),('BACKUPS',backups)):
                stack.enter_context(patch.object(upgrade,name,value))
            stack.enter_context(patch.object(upgrade,'systemd_properties',side_effect=lambda unit,*keys:dict(props)))
            run=stack.enter_context(patch.object(upgrade,'run',return_value=SimpleNamespace(stdout=journal)))
            probe=stack.enter_context(patch.object(upgrade,'telegram_import_preflight'))
            with self.assertRaisesRegex(RuntimeError,'must load'):upgrade.verify_telegram_after_shared()
            (receipt/'complete').touch();props['ExecMainStartTimestampMonotonic']='999'
            with self.assertRaisesRegex(RuntimeError,'must load'):upgrade.verify_telegram_after_shared()
            props['ExecMainStartTimestampMonotonic']='1001'
            run.return_value=SimpleNamespace(stdout='not ready')
            with self.assertRaisesRegex(RuntimeError,'startup proof'):upgrade.verify_telegram_after_shared()
            run.return_value=SimpleNamespace(stdout=journal);upgrade.verify_telegram_after_shared();probe.assert_called_once()
            interrupted=backups/'two-shared';interrupted.mkdir()
            (interrupted/'attempt.json').write_text(json.dumps({'boot_id':upgrade.boot_id(),'monotonic_us':2000}))
            with self.assertRaisesRegex(RuntimeError,'must load'):upgrade.verify_telegram_after_shared()
            (interrupted/'attempt.json').unlink()
            (shared/'okx_funding_view.py').write_text('drift')
            with self.assertRaisesRegex(RuntimeError,'shared source mismatch'):upgrade.verify_telegram_after_shared()

    def test_funding_view_install_copies_only_pinned_view_and_preserves_mode(self):
        import shutil
        import okx_live_upgrade as upgrade
        from contextlib import ExitStack
        source=Path(__file__).parents[1]
        inventory=json.loads((source/'install_manifest.json').read_text())
        funding=Path(self.tmp.name)/'funding';funding.mkdir();shared=Path(self.tmp.name)/'shared';shared.mkdir()
        name='okx_funding_view.py'
        for dest,src in inventory['funding'].items():shutil.copy2(source/src,funding/dest)
        shutil.copy2(source/name,shared/name)
        # Construct a known previous view fixture, with the inventory pin scoped
        # to this test only; the actual migration pin is verified separately.
        old=(source/name).read_bytes().replace(b'90_000',b'75_000')
        self.assertNotEqual(old,(source/name).read_bytes())
        (funding/name).write_bytes(old);(funding/name).chmod(0o600)
        inventory['funding_view_previous_sha256']=hashlib.sha256(old).hexdigest()
        backup=Path(self.tmp.name)/'backup';backup.mkdir()
        before={n:(funding/n).read_bytes() for n in inventory['funding'] if n!=name}
        with ExitStack() as stack:
            for key,value in (('STAGE',source),('SHARED',shared),('FUNDING_ROOT',funding)):
                stack.enter_context(patch.object(upgrade,key,value))
            stack.enter_context(patch.object(upgrade,'stage_check'))
            stack.enter_context(patch.object(upgrade,'backup_dir',return_value=backup))
            stack.enter_context(patch('okx_install_manifest.load',return_value=inventory))
            stack.enter_context(patch.object(upgrade,'run',side_effect=AssertionError('no systemd')))
            (funding/name).write_bytes(b'unrecognized')
            with self.assertRaisesRegex(RuntimeError,'Unrecognized'):upgrade.install_funding_view()
            (funding/name).write_bytes(old)
            replace=upgrade.atomic_replace_file
            def fail_after_replace(src,dst):
                replace(src,dst)
                if src.name=='candidate.py':raise RuntimeError('forced failure after copy')
            with patch.object(upgrade,'atomic_replace_file',side_effect=fail_after_replace):
                with self.assertRaisesRegex(RuntimeError,'forced failure'):upgrade.install_funding_view()
            self.assertEqual((funding/name).read_bytes(),old)
            self.assertEqual((funding/name).stat().st_mode&0o777,0o600)
            upgrade.install_funding_view()
            self.assertEqual((backup/'old.py').read_bytes(),old)
            self.assertEqual((funding/name).read_bytes(),(source/name).read_bytes())
            self.assertEqual((funding/name).stat().st_mode&0o777,0o600)
            self.assertEqual(before,{n:(funding/n).read_bytes() for n in before})
            upgrade.install_funding_view()

    def test_cli_preflights_boot_barrier_before_shared_or_bot_mutation(self):
        import okx_live_upgrade as upgrade
        root=Path(__file__).parents[1]
        manifest=json.loads((root/'engine_manifest.json').read_text())
        old=next(v for v in manifest['engines'] if v!=manifest['current_version'])
        for action,handler in (('preflight','verify_staging'),('shared','install_shared'),('bot','upgrade_bot')):
            argv=['upgrader',action,'--bot','active','--stage',str(root),
                  '--package','not-opened.zip','--sha256','0'*64,'--from-engine',old]
            if action=='bot':argv.append('--confirm-live-stop')
            with self.subTest(action=action), \
                 patch.object(upgrade.sys,'argv',argv), \
                 patch.object(upgrade.os,'geteuid',return_value=0), \
                 patch.object(upgrade,'LOCK',Path(self.tmp.name)/'upgrade.lock'), \
                 patch.object(upgrade,'stage_check') as attest, \
                 patch.object(upgrade,'verify_quota_boot_barriers',side_effect=RuntimeError('barrier not effective')) as verify, \
                 patch.object(upgrade,handler) as mutate:
                with self.assertRaisesRegex(RuntimeError,'barrier not effective'):
                    upgrade.main()
                attest.assert_called_once_with()
                verify.assert_called_once_with()
                mutate.assert_not_called()

    def test_cli_blocks_bot_before_stop_when_telegram_not_reloaded(self):
        import okx_live_upgrade as upgrade
        root=Path(__file__).parents[1]
        m=json.loads((root/'engine_manifest.json').read_text());old=next(v for v in m['engines'] if v!=m['current_version'])
        argv=['upgrader','bot','--bot','active','--stage',str(root),'--package','unused.zip','--sha256','0'*64,'--from-engine',old,'--confirm-live-stop']
        with patch.object(upgrade.sys,'argv',argv),patch.object(upgrade.os,'geteuid',return_value=0), \
             patch.object(upgrade,'LOCK',Path(self.tmp.name)/'upgrade.lock'),patch.object(upgrade,'stage_check'), \
             patch.object(upgrade,'verify_quota_boot_barriers'),patch.object(upgrade,'upgrade_bot') as mutate, \
             patch.object(upgrade,'verify_telegram_after_shared',side_effect=RuntimeError('Telegram must load')):
            with self.assertRaisesRegex(RuntimeError,'Telegram must load'):upgrade.main()
            mutate.assert_not_called()

    def test_quota_corruption_consumes_full_window(self):
        for cls in (AccountPositionsFIFO, FillsHistoryFIFO):
            for raw in (b'', b'\0'*20, b'{"schema":1,"calls":[', b'{"schema":1,"calls":null}'):
                with self.subTest(cls=cls.__name__, raw=raw):
                    gate = cls(self.path)
                    self.path.write_bytes(raw)
                    with patch('okx_positions_fifo.time.time', return_value=1000.0):
                        granted, delay = gate._reserve_if_available(1000.0)
                    self.assertFalse(granted)
                    self.assertEqual(delay, gate.WINDOW)
                    self.assertEqual(json.loads(self.path.read_text())['calls'], [1000.0]*9)
                    with patch('okx_positions_fifo.time.time', return_value=1000.0+gate.WINDOW-.001):
                        self.assertFalse(gate._reserve_if_available(0)[0])
                    with patch('okx_positions_fifo.time.time', return_value=1000.0+gate.WINDOW+.001):
                        self.assertTrue(gate._reserve_if_available(0)[0])

    def test_existing_ticket_lost_by_queue_damage_cannot_grant(self):
        for raw in (b'', b'\0'*20, b'{"schema":1,"waiters":['):
            with self.subTest(raw=raw):
                self.gate.queue_path.write_bytes(raw)
                with self.assertRaises(GateUnavailable):
                    self.gate._visit('existing', True)
                self.assertEqual(json.loads(self.path.read_text())['calls'], [])

    def test_short_write_fails_and_recovers_conservatively(self):
        real_write = os.write
        with patch('okx_positions_fifo.os.write', side_effect=lambda fd,b: real_write(fd,b[:5])):
            with self.assertRaises(GateUnavailable):
                self.gate._reserve_if_available(time.time())
        self.assertFalse(self.gate._reserve_if_available(time.time())[0])
        self.assertEqual(len(json.loads(self.path.read_text())['calls']),9)

    def test_process_death_after_truncate_does_not_grant(self):
        script = """
import os, sys
from okx_positions_fifo import AccountPositionsFIFO
class Crash(AccountPositionsFIFO):
    @staticmethod
    def _write(fd, data):
        os.ftruncate(fd, 0)
        os._exit(73)
Crash(sys.argv[1])._reserve_if_available(0)
"""
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path)], timeout=10)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(self.path.read_bytes(), b'')
        self.assertFalse(self.gate._reserve_if_available(time.time())[0])
        self.assertEqual(len(json.loads(self.path.read_text())['calls']), 9)

    def test_process_death_after_quota_before_queue_commit(self):
        script = """
import os, sys
from okx_positions_fifo import AccountPositionsFIFO
class Crash(AccountPositionsFIFO):
    def _write(self, fd, data):
        if 'waiters' in data:
            os._exit(74)
        return super()._write(fd, data)
Crash(sys.argv[1])._visit('crash', None)
"""
        self.gate.queue_path.write_text('{"schema":1,"waiters":[]}')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path)], timeout=10)
        self.assertEqual(result.returncode, 74)
        self.assertEqual(len(json.loads(self.path.read_text())['calls']), 1)
        for idx in range(8):
            self.assertTrue(self.gate._visit(str(idx),None)[0])
        self.assertFalse(self.gate._visit('over-limit',None)[0])

    def test_cancel_noop_does_not_write(self):
        self.gate.queue_path.write_text('{"schema":1,"waiters":[]}')
        with patch.object(self.gate, '_write', wraps=self.gate._write) as write:
            self.gate._visit('absent', True, cancel=True)
            write.assert_not_called()

    def test_cancel_persists_expired_and_normalized_tickets(self):
        now = time.time()
        self.gate.queue_path.write_text(json.dumps({'schema':1,'waiters':[
            {'id':'expired','pid':os.getpid(),'created':now-40,'seen':now-40},
            {'id':'future','pid':os.getpid(),'created':now+10,'seen':now+10},
            {'id':'cancelled','pid':os.getpid(),'created':now,'seen':now}]}))
        with patch('okx_positions_fifo.time.time', return_value=now):
            self.gate._visit('cancelled',True,cancel=True)
        self.assertEqual(json.loads(self.gate.queue_path.read_text())['waiters'],[
            {'id':'future','pid':os.getpid(),'created':now,'seen':now}])

if __name__ == '__main__': unittest.main()
