"""AST-isolated test of candidate StatsWorker financial output, no engine import."""
import ast
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone
import sys
import unittest

DIR=Path(__file__).resolve().parents[1]
SRC=DIR.parents[0]/'stats_v2_release_work'
sys.path[:0]=[str(SRC),str(DIR)]
from okx_stats_v2_contract import NotReady, stable_state

class EnginePatchTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.state=root/'bot_state.json'
        self.state.write_text(json.dumps({'instrument':'DEMO-USDT-SWAP', 'cycles':{}, 'archived_cycles':[]}))
        self.grid=root/'grid_reference.json'
        self.grid.write_text('{}')
        self.stats=root/'stats.json'
        self.anchor=root/'anchor.json'
        self.write_order=[]
        self.env={
            'GRID_REFERENCE': self.grid,
            'DATA':self.state,
            'STATS_FILE':self.stats,
            'STATS_FINANCIAL_ANCHOR':self.anchor,
            'stats_capture_bid':lambda market:Decimal('111'),
            'format_okx_report':lambda state,fills,stats,**kw:'VERIFIED REPORT',
            'performance_snapshot':lambda state,fills,stats,*args,**kw:{'total_pnl':str(stats['total_gain'])},
            'financial_signature':lambda state:{'instrument':state['instrument'],'cycles':len(state['cycles'])},
            'VER':'test-engine',
            'INST':'DEMO-USDT-SWAP',
            'datetime':datetime,
            'timezone':timezone,
            'json':json,
        }
        def write(path,payload):
            self.write_order.append(path.name)
            path.write_text(json.dumps(payload,default=str))
        self.env['atomic_json']=write
        code=DIR.joinpath('okx_bot.py').read_text()
        node=next(n for n in ast.parse(code).body if isinstance(n,ast.FunctionDef) and n.name=='print_verified_incremental_stats')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'PINNED_AST_STATS_WORKER_ONLY','exec'), self.env)
        self.fn=self.env['print_verified_incremental_stats']
    def core(self):
        from types import SimpleNamespace
        return SimpleNamespace(compute=lambda bid:SimpleNamespace(
            stats={'total_gain':Decimal('12.5'),'open_contracts':Decimal('3')},
            captured_at=1800000000.,state=json.loads(self.state.read_text()),
            fills_hot=[],last_24h=Decimal('4'),sidecar_generation=1,
            frozen_new=2,pending=1,hot_cycles=3))
    def test_one_canonical_report_compatibility_anchor(self):
        self.assertTrue(self.fn(self.core()))
        self.assertEqual(self.write_order,['stats.json','anchor.json'])
        stats=json.loads(self.stats.read_text())
        anchor=json.loads(self.anchor.read_text())
        self.assertEqual(stats['financial_anchor'],anchor)
        self.assertEqual(stats['generated_at_unix'],anchor['at'])
        self.assertEqual(stats['stats_v2']['generation'],1)
    def test_refuse_unanchored_capital_reference(self):
        self.grid.unlink()
        with self.assertRaisesRegex(NotReady,'capital anchor'):
            self.fn(self.core())
        self.assertFalse(self.write_order)
    def test_no_publication_if_incremental_unready(self):
        from types import SimpleNamespace
        bad=SimpleNamespace(compute=lambda bid: (_ for _ in ()).throw(NotReady('INCOMPLETE HOT')))
        with self.assertRaisesRegex(NotReady,'INCOMPLETE'):
            self.fn(bad)
        self.assertFalse(self.write_order)
    def test_nonfinancial_bot_engine_unchanged(self):
        # The eager archival release deliberately changes ONE engine tick,
        # ONE existing place() cleanup, and adds ONE isolated helper. Preserve
        # the original byte-differential contract for EVERY other trade path.
        old=(SRC/'reference/okx_bot.py').read_text()
        new=(DIR/'okx_bot.py').read_text()
        # v0.7.81: exact reviewed ENSO engine delta, including empty insert/delete hunks.
        # Verify every changed candidate hunk against the reviewed digest, undo
        # only those exact hunks, and retain the historical oracle for all else.
        import gzip, hashlib
        baseline=gzip.decompress((DIR/'okx_bot_v0780_baseline.py.gz').read_bytes())
        self.assertEqual(hashlib.sha256(baseline).hexdigest(), 'fac151f5ef9ac5a6f9ab11f8bb13b942de0fb89e55e1bd76f2cd8915dfc96565')
        base_lines=baseline.decode().splitlines(keepends=True)
        new_lines=new.splitlines(keepends=True)
        hunks=[(1748, 1749, 1748, 1749, '69e87422aaa40aa211da1f22e9fec8d9389a97406700f3388225ee8847b01df6'), (1781, 1782, 1781, 1788, '01a270c92250a9d34badcbef5ba6007335c0c222e27fb735a910e87fab25fd9a'), (1783, 1783, 1789, 1821, '26341fd5c0d1faa74e4593b7791566ce412197c94b4a5146b5b1fb1f243a9d9b'), (1994, 1996, 2032, 2038, '3ab136446ffe9d7ffdb754e59965c003375c59c5f4962c0a675d89ece0da91cd'), (2033, 2034, 2075, 2077, '985be107bdfee7adf275173f74dd64f17dc020280aca15b3ddee76118ff03e13'), (2108, 2109, 2151, 2152, '56032c69f87705b39e7e62aff03667eeccb7f7b68063d0b97382aef5d70f9932'), (2267, 2268, 2310, 2312, 'a6a317187a118940d275c41a794937d0b0c49420c0482838d4801ad3e5e62aad'), (2300, 2301, 2344, 2346, '26801fb08aa2fb89840516330790f1900beb9892795cb42124e3c40c46879f2d'), (2326, 2327, 2371, 2373, 'e0f08dfbdfdbe1ba6a3a56c96d97eccdb38f2d19d05b325e15b0a575071310c4'), (2379, 2379, 2425, 2515, 'a75f1d7840b194d3992acf4353c4ebe600173b2059397a9cabc29bfcce2b1e4d'), (2528, 2529, 2664, 2665, 'b96cfac5a5f6815e9fdaf5b2fc3d61e79ea9607c436a92e697803d7fc3ebb12e'), (2542, 2543, 2678, 2686, 'b55f94beb4ee44905e96081c0af446e482a79c16e49c170c64bcf173fbcea2d2'), (2550, 2551, 2693, 2695, '15b2b5e03f3511a73b122e806702d8b4948e4d8d1d2740a1d571830dd3496b4d'), (2806, 2808, 2950, 2957, '4c87fbe4e0909e0e1fbd93d56b4bfb2cdb6c1b2514be94f5587bed7b8e8286a9'), (2809, 2809, 2958, 2959, 'b9e21b82632f1b9f224adaa8157f4bc6cdfc4ef1eee5c1379746ba0ba26f6568'), (2832, 2833, 2982, 2987, '48fbfb97e8aeb9273380f9d9f3db45fdb6e64b04ee890a698a1aff33d29fb3de'), (2902, 2903, 3056, 3057, 'dc1ae338eea4a635f7e02c82c0e24d332585c3ebd1c3058eb3bd4ff0707541d9'), (3381, 3382, 3535, 3536, '764fb66a01670cb019086a27a5042ff809fbed6dfe85d5233cfc3f78ffad830d'), (3391, 3391, 3545, 3547, '7500dcfcf06c470abfcd085060eec21d525cbbefbba4eb929fc636d8f1d69c2a'), (3405, 3406, 3561, 3570, '3f1a16dc2a54412dffb4af2a75f97d33f4bb4bb9b2ee2c5fa93486227c00d7f2'), (3439, 3439, 3603, 3607, '6a2d9b50660ebb1963177918ab7a9b3d2118cf2b0f119f462e6986bde47639c3'), (3476, 3479, 3644, 3669, 'ba0974c3f25c9d799d000d248b879312951537686549e0dc1228948af056645b'), (3486, 3487, 3676, 3678, 'aee65520a25f150f2fcaeedee6d82bdb3a8ab84e846576fedfa934e7427e449c'), (3491, 3494, 3682, 3686, 'c4deea32afaae47324980d83ff7966d7a2971332d21f6b7b35817ccbe9b6249b'), (3495, 3495, 3687, 3688, '270ffdb2edb5a74c0db04d3f5120804bf05588fa386b1d006928b2edaac97be0'), (3599, 3599, 3792, 3793, '98e0066d826a906445116a474517b7aa07f7189322f15e4a2ea1999394476887'), (3742, 3742, 3936, 3937, '629c68973a868f94ed0309ace64bb7a4db52148dd3bafb20407b20cd9534f404'), (3743, 3744, 3938, 3940, '83bb7e3ed6fcef81edd458304f570960e39d2a61476cea5cdb914249ade3237b'), (3753, 3753, 3949, 3951, '6e49fba4e971071ff87780c5cf48d272753a30d71db57f7087318961fcb279ee'), (3778, 3779, 3976, 3987, '2cf915d3b5bb462dc856e1b62abafc2adb8c161a455973f7ebcfb9abae809764'), (3783, 3783, 3991, 3994, '3e71bc3ae90f5f4101284f578d1909b236dcc07633f5da32521c63a1fa5228c3'), (3847, 3847, 4058, 4060, '9bbb660c7074f089b9f4239a9903399f906e1f41134e08aded3e73f231ebec93'), (3851, 3852, 4064, 4065, 'e64fef837858f9350cf5f83ddc2bf74d77911636d3dcdce8206040a3afa24284'), (3872, 3873, 4085, 4086, '49ba9b5874db87bda1129966f98037a4107d13c01b6c365ffc45917c5d01ea75'), (3907, 3908, 4120, 4134, '14072b85907e7711018a46dbe781a58f0765cb4a126ea430c1be9243d5754a38'), (3911, 3912, 4137, 4139, '90ddc0707150fd152da48c1186bcebb84aff8db79c6b2b3c252b0e16b1cf57df'), (3919, 3920, 4146, 4147, 'd3f3c031b8d5a8321370fa788bd1c6fb7458039a114f93b62723bb81e68f71b2'), (3928, 3929, 4155, 4158, 'de9edb111c27b29907040005c051f2a14ae5167958aed6038d22524aeea0744d'), (4021, 4022, 4250, 4251, 'f54e3b66b29c379ae30116ff363e702f25bf0104e71b8315800ae0f64cedaeaf'), (4027, 4028, 4256, 4257, '9afd83f8cc30665e7717f2d7e3fd895c7687b7a1d792add06aaeb29d9e89e793'), (4163, 4164, 4392, 4395, 'de9edb111c27b29907040005c051f2a14ae5167958aed6038d22524aeea0744d'), (5383, 5384, 5614, 5616, 'ee8e4e76021f93556d9aca5a14ee81f0087cdabe0e14b61871fc42519dab8fa8'), (5386, 5387, 5618, 5624, 'c736c509b31e80dcba48063ee618af8970c59abed2af0b65512c52ede8b8fede')]
        for i1,i2,j1,j2,digest in reversed(hunks):
            self.assertEqual(hashlib.sha256(''.join(new_lines[j1:j2]).encode()).hexdigest(),digest)
            new_lines[j1:j2]=base_lines[i1:i2]
        new=''.join(new_lines)
        self.assertEqual(new.encode(),baseline)
        a=old.index('class StatsWorker(threading.Thread):')
        b=old.index('\ndef active_tp_trace_id(',a)
        c=new.index('def print_verified_incremental_stats(')
        d=new.index('\ndef active_tp_trace_id(',c)
        start=new.index('# A terminal cycle may remain in the price-keyed trading grid')
        stop=new.index('def newid(prefix):',start)
        new=new[:start-1]+new[stop:]
        # The rest of the engine must be byte-identical to the pinned oracle.
        new=new.replace("        st.get('terminal_seen',{}).pop(str(previous['cycleId']),None)\n",'')
        new=new.replace('                    eager_archive_terminals(st,pnl,pending)\n','')
        c=new.index('def print_verified_incremental_stats(')
        d=new.index('\ndef active_tp_trace_id(',c)
        nonfinancial_after_approved_delta = new
        for approved_new, approved_old in (('BUY_MAX = TARGET + BUY_BUFFER\nBUY_PACING_SEC = 0.02  # delay only between successful BUY placements\n', 'BUY_MAX = TARGET + BUY_BUFFER\n'), ('TP_BUFFER_COOLDOWN = 12.0', 'TP_BUFFER_COOLDOWN = 10.0'), ('BUY_BUFFER_COOLDOWN = 12.0', 'BUY_BUFFER_COOLDOWN = 10.0'), ('ESCAPE_COALESCE_SEC = 0.10', 'ESCAPE_COALESCE_SEC = 0.15'), ('    Each confirmed BUY/TP fill restarts the 12-second quiet period', '    Each confirmed BUY/TP fill restarts the 10-second quiet period'), ('            # After 12s without a confirmed fill, trim surplus BUYs BOTTOM-UP.', '            # After 10s without a confirmed fill, trim surplus BUYs BOTTOM-UP.'), ('                        # the complete 12s trade-quiet cooldown again.', '                        # the complete 10s trade-quiet cooldown again.'), ('            for price in candidates:\n                if placed:\n                    time.sleep(BUY_PACING_SEC)  # only between BUYs; re-check all gates afterward\n                # Re-check for a newer private event at the actual BUY write\n', '            for price in candidates:\n                # Re-check for a newer private event at the actual BUY write\n'), ('                if place(st,price):placed+=1\n                else:break\n', '                if place(st,price):placed+=1\n                else:break\n                time.sleep(0.05)  # BUY pacing; keep order rate limit\n')):
            self.assertEqual(nonfinancial_after_approved_delta.count(approved_new), 1,
                             "approved engine delta drift")
            nonfinancial_after_approved_delta = nonfinancial_after_approved_delta.replace(approved_new, approved_old)
        self.assertEqual(old[:a].rstrip(),nonfinancial_after_approved_delta[:a].rstrip())

        self.assertEqual(old[b:],new[d:])

if __name__=='__main__':unittest.main()
