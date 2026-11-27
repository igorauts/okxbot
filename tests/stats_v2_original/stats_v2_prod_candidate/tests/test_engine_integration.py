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
        # v0.7.90: reverse only reviewed unused imports and dangling comment. The exact
        # previous published engine digest must be recovered before any older
        # differential contract runs. No permission to change trading paths.
        v90_lines=new.splitlines(keepends=True)
        for j1,j2,old_text,digest in reversed([(7, 7, 'import http.client\nimport ssl\nimport socket\n', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (13, 13, 'import shutil\n', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (16, 16, 'import urllib.error\nimport urllib.parse\nimport urllib.request\n', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (17, 18, 'from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, ROUND_CEILING\n', '1bee5b0579bd99b8d5543689251f6ea6b887778a5774fa633e55fd7fadd142aa'), (23, 24, 'from okx_version import ENGINE_VERSION as VER, STATE_SCHEMA\n', 'b7c6cf53b371bcc1aa9a530e64ea2499c438e33510b52312f16f45e98d9ba448'), (25, 26, 'from okx_contracts import base_coin, validate_spec\n', 'a60a560a15dd8dc4e8d9035b309e3581c2c6b28ec682f53a394f0e3cc5506195'), (1208, 1208, '\n\n        # verify_existing already checked tracked cycles.\n', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (4293, 4294, '    from okx_idle_reconcile import (audit_jitter_seconds, aligned_idle_audit_wait,\n                                    wait_for_idle_reconcile, idle_deadline, wait_error_backoff,\n                                    b1_shadow_probe)\n', '45aaf377656798af50ccffa40ecbee352b9d802d292fef0a4fd31c6e91109b3c')]):
            self.assertEqual(hashlib.sha256(''.join(v90_lines[j1:j2]).encode()).hexdigest(),digest)
            v90_lines[j1:j2]=old_text.splitlines(keepends=True)
        new=''.join(v90_lines)
        self.assertEqual(hashlib.sha256(new.encode()).hexdigest(),'cf499d9dd0e859e3bd467ca90a484ef657f06f119a7acb6286abe01404fa9b15')
        # v0.7.88: reverse exact dead-code-only hunks before the original checks.
        v85=gzip.decompress((DIR/'okx_bot_v0785_baseline.py.gz').read_bytes())
        self.assertEqual(hashlib.sha256(v85).hexdigest(), 'c68f370655cb7c59a0741d3089c6a5a17f87e3e67e7f6e96f9481c13dc1302cb')
        v85_base=v85.decode().splitlines(keepends=True)
        v88_lines=new.splitlines(keepends=True)
        for i1,i2,j1,j2,digest in reversed([(427, 441, 427, 427, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (928, 940, 914, 914, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (1243, 1252, 1217, 1217, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (5077, 5081, 5042, 5042, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (5593, 5594, 5554, 5555, '768930abd0201d9d5889d184878509197bab198e9023009635cd51809807576c'), (5608, 5609, 5569, 5570, '3f4b54d14f96938582af9a9432a67c22aea9b1fd147d048bc46d94e627370fc2'), (5620, 5621, 5581, 5582, 'fc17d32e4173612e547a8668ecbf00cdaefea00cbf91638cd91b72824a0e39bd'), (5627, 5630, 5588, 5589, '361b99799944fa9e82f7bfab4cf9890e7b51a64993a095fa082d04b0ef0f4ad2'), (5640, 5651, 5599, 5600, 'd0fa4c9ad07a73855a83b0585874d2bdf1d8392a9d7aaba987c21db23ba5a48b'), (5682, 5684, 5631, 5632, 'bf130a04e8762a2e7580abf3bcd87ae75e4b29793253f3a68f5e2c70cbc2129d'), (5705, 5712, 5653, 5653, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')]):
            self.assertEqual(hashlib.sha256(''.join(v88_lines[j1:j2]).encode()).hexdigest(),digest)
            v88_lines[j1:j2]=v85_base[i1:i2]
        new=''.join(v88_lines)
        self.assertEqual(new.encode(),v85)
        # v0.7.85: reverse only the reviewed terminal-AMEND race / alarm / audit delta.
        v84=gzip.decompress((DIR/'okx_bot_v0784_baseline.py.gz').read_bytes())
        self.assertEqual(hashlib.sha256(v84).hexdigest(), 'ffe84de337f7d097aca2912a3a3c8e55a6a42d351cae937d2f9486a0e628b0be')
        v84_base=v84.decode().splitlines(keepends=True)
        v85_lines=new.splitlines(keepends=True)
        v85_hunks=[(1784, 1784, 1784, 1786, '9e9d11495bf1f90f50f66e28c71cb089c2891bc07c5e01fcba27d854b0a4fd62'), (2280, 2284, 2282, 2287, '36b60ef85f4d264a9b360de1491e6dade38541b9184d7f675392a57cf59f4bd0'), (2352, 2356, 2355, 2359, '94f0e3a572201caefd2526dc4a0ecd52ffa0c9d604848ec9e08c7b375b5e0ab7'), (2397, 2397, 2400, 2435, '49976c86dfab3ee63645880e6dd7da565a7a2a961515104387e76bdb1ee6763b'), (2770, 2771, 2808, 2814, 'afd17005eee5869bec57d1d7dc19288f1ddc7878e0ace6dd368189c695bc2590'), (2789, 2790, 2832, 2836, '0e715fb014cc2ce7a41e7ef1919bbf80cf164e0a1046aa94b5da45680d061650'), (2817, 2817, 2863, 2918, '4e83089a1abfafeb69b67e3fe6082f4e9c57ce3afbc513a3184107909861b97a'), (2818, 2819, 2919, 2920, '4aacb58720d24daddb6502ddcfb56dc40a3b98a5c0a6bba54af7bb9e33a74a52'), (2820, 2823, 2921, 2924, 'f43aae60d5d566be628ace4b457d30c0a1dba2555cb41023ce9c69ed416eb8a1'), (2826, 2826, 2927, 2940, 'db1cbe55147af6c74ecd5c063b2fb730ea4dd4d0ad0401df9ab98b7a95729bcb'), (2827, 2839, 2941, 2970, 'c86736513bf3dec30fff26f05948351b92a5d9bc335b0c65c938b65d1a439b3e'), (2897, 2897, 3028, 3033, '075593e34fa56e0ade7bd4ffe4212b518c54ab9f7701b1b9ab2acfcf23a4120a'), (4003, 4004, 4139, 4140, '67fcd5c6e081372f7ef2a976ff006ba67aa779b0426bb66d013598f157534a33'), (4017, 4017, 4153, 4157, 'a29b2df48fb888ff0f59e99b04bb8beb7867f6f669e15e41f36a61bc1a2d3bf9'), (6045, 6046, 6185, 6189, '5365be7a6f34688b8043cb7c0a5f2b4c35cc0eb4cb5c5297b6b240973b4034f0')]
        for i1,i2,j1,j2,digest in reversed(v85_hunks):
            self.assertEqual(hashlib.sha256(''.join(v85_lines[j1:j2]).encode()).hexdigest(),digest)
            v85_lines[j1:j2]=v84_base[i1:i2]
        new=''.join(v85_lines)
        self.assertEqual(new.encode(),v84)
        # v0.7.84: exact reviewed partial-detachment delta, then all earlier guards.
        v83=gzip.decompress((DIR/'okx_bot_v0783_baseline.py.gz').read_bytes())
        self.assertEqual(hashlib.sha256(v83).hexdigest(), '5e73eff2be5d109a5cf222d5375bce4773a17bfdb69eebd1c1dc1f64e61f252b')
        v83_base=v83.decode().splitlines(keepends=True)
        v84_lines=new.splitlines(keepends=True)
        v84_hunks=[(89, 93, 89, 93, 'cb633a3e384561f14f0ce64bf36a81558ba481a0492d43ae66c25b052db4f81f'), (97, 98, 97, 98, 'cd20a50545f0083df8b4d03d0066fa425e04ff5caeb8ec9c8e3c781751194d37'), (1662, 1665, 1662, 1665, '2f8741dd6e69b93b0bae27570598e77f4916379b3a8cf4e6c63887af34912523'), (2278, 2278, 2278, 2283, '3ad4e4d5df0bfad9040f240893525befcd8b81443a4242ce93b074dca67a774d'), (2345, 2345, 2350, 2355, '3ad4e4d5df0bfad9040f240893525befcd8b81443a4242ce93b074dca67a774d'), (2346, 2346, 2356, 2360, '4d68085fe8b90d3dad9612a604d698fe558cc8d25f202cbde671a716f90b2323'), (2728, 2728, 2742, 2940, '81a7065e0c2f0a649754d00ab766faf397d8118a89b04295b87f4366dbe42272'), (2786, 2787, 2998, 3003, '2919f4f1a5ec04be7e188a3534670a34121257d3bc65b7f68323853b3a0dea1d'), (2811, 2812, 3027, 3028, '738db396ce6c500fa232c8846c536b7c902cf152c021e0f770a3bc3e466f70cf'), (2819, 2821, 3035, 3036, 'e9cb0b6c3b6564a2a10f326142981916a37437382eb71b6121c9aa67e93f1ee2'), (3316, 3316, 3531, 3539, '1ee82f2d8420653933f481bacbf28254a9cc9e679dfd29f0a68ea455a4283194'), (4007, 4007, 4230, 4232, '8824f3f9d5918581c3375c7402670c59d08f2217c6c25966b638239e6fbfce63')]
        for i1,i2,j1,j2,digest in reversed(v84_hunks):
            self.assertEqual(hashlib.sha256(''.join(v84_lines[j1:j2]).encode()).hexdigest(),digest)
            v84_lines[j1:j2]=v83_base[i1:i2]
        new=''.join(v84_lines)
        self.assertEqual(new.encode(),v83)
        # v0.7.83: pin first-start audit-only changes before all historical guards.
        v82=gzip.decompress((DIR/'okx_bot_v0782_baseline.py.gz').read_bytes())
        self.assertEqual(hashlib.sha256(v82).hexdigest(), '0d6bcd37f7908df0bb3442a5443e32a92aaae366611b7648219744674882663d')
        v82_base=v82.decode().splitlines(keepends=True)
        v83_lines=new.splitlines(keepends=True)
        v83_hunks=[(5199, 5200, 5199, 5230, 'cfa3f009d754a0aafd92e72446950d239ee045ac901e2d1c4d456d269e9c5030'), (5212, 5212, 5242, 5247, '2e6daa4ae4d90b1f898e4a4b46f15c850357360d663fd491bd9fd817028a76d6'), (5769, 5769, 5804, 5805, '9ab1de3d7a349945223a0de740320e3e1ad2140cfede669d61821bb8df72422a'), (5770, 5770, 5806, 5808, 'cd7afe11620bf8ac6e580e5b8a79703826c5172be5abf154a5497a7fc1ecd239'), (5785, 5786, 5823, 5827, '18c515aa7fa097f0f1838b58dda5a4e73139ea124857cd7f2f1cff12227b5c9c')]
        for i1,i2,j1,j2,digest in reversed(v83_hunks):
            self.assertEqual(hashlib.sha256(''.join(v83_lines[j1:j2]).encode()).hexdigest(),digest)
            v83_lines[j1:j2]=v82_base[i1:i2]
        new=''.join(v83_lines)
        self.assertEqual(new.encode(),v82)
        # v0.7.82: reverse ONLY the exact reviewed changes relative to the
        # published 0.7.81 engine; then run the original 0.7.81 and older guards.
        v81=gzip.decompress((DIR/'okx_bot_v0781_baseline.py.gz').read_bytes())
        self.assertEqual(hashlib.sha256(v81).hexdigest(), 'fff0220fc5b1f825089325c143b1026491b6b9805be898dadfc12ac9823d21c3')
        v81_lines=v81.decode().splitlines(keepends=True)
        v82_lines=new.splitlines(keepends=True)
        v82_hunks=[(1380, 1381, 1380, 1381, 'ccf338179d5af817274b94431720fb5ed48eaa84179fe1e7a23e072427651166'), (1387, 1389, 1387, 1391, '0e16f21053fc2b01a7cfb0ebbc2a94b9001c45f8453b2a8ca6dd47423cddb047'), (1390, 1392, 1392, 1405, 'e9e5bf3a964eeb74cbbf1667654c49056290cd0adc23cc8b12ba5a1435d25f33'), (1398, 1398, 1411, 1412, 'bd67bde1b3d0c9418cc92cf3c9fafd68aa9abaf9c42eef517585a20e238dae5b'), (1404, 1405, 1418, 1421, '862878808387e7bfbd4e1aae057236aaf7f6d7697873dcd4fdb5f2ad44d4ba52'), (1410, 1410, 1426, 1430, 'a6e3eaf638c083d1fbf2d0ecc6d518cc89aeba637145d3b2756c566fa581a4c4'), (1438, 1438, 1458, 1466, '88911fb207948ddbf071e87519030e15754966a688c1b2871852373b265a3826'), (1450, 1451, 1478, 1481, '9f8af3e2a0785e3d382865c1c6235546dd178a2501cc2430d933f983406dab86'), (1748, 1749, 1778, 1804, 'b9bfdc7042f50f791c3a53dccae2d1356a7fa28ef6cdc5398813f0dc430063a2'), (1757, 1757, 1812, 1813, '46a0aca4e97ac061c4d9d83053cea1046d283a1b8c6754cac956b975dec0acd7'), (1759, 1759, 1815, 1818, '95886be60b6d93878ecfe423e5d26397793b528ecbb77fdbe6dce3bdce6bc78d'), (1760, 1760, 1819, 1823, '92c4e3cfde130cf3fc3c7df227e0647dc04ac70b9e114310f70ea6085b805565'), (1778, 1778, 1841, 1848, '6d577b5174f1aad31cbe7541d085aeed7113e17d879ccf8a635a7d95de1ab058'), (1784, 1787, 1854, 1868, '7924aadeedb1b22d63af9b057d895de16b8f8b0897eff376f2de570cbc3f8ee5'), (2151, 2152, 2232, 2233, '6052f0080f483ba6c4abc530834a4e1f353f43a6c5a6103d02250d555aa4e6bc'), (2302, 2302, 2383, 2394, '571f6511959c3e4603942ea59edc91b072f8ad2d1bca65f8538b31f55a32322e'), (2310, 2312, 2402, 2408, '2d7ea60e404d01a1392cfe343c7249babd90dd746ebe5f05b210281547ce73fb'), (2343, 2345, 2439, 2441, 'bf3a3dadd6fcbe253ae688ca7f2572b4f14d57cc3ac3d28e9831e92283992858'), (2371, 2372, 2467, 2468, 'c737145f89699c12e4ce75343057cde9e3e215741bf4b11e744eeac20ee08b03'), (2397, 2397, 2493, 2498, '30c7debf73cbdfb3577609f2bcec572ba8ab3548fa2e08f3b91c04220d0a8407'), (2427, 2428, 2528, 2529, 'b5fce5d583f9aff812dc553daa77c4a04223109e55fa852944ff2f395e7e3da4'), (2434, 2435, 2535, 2536, 'a983af956411ee7dfcfb01c4e6ef8c2b0c7f5a931657a7d039567b43f8b99cdc'), (2436, 2436, 2537, 2547, '3ca05fdd950b0f87bf1a71861a501d4c6b9194d7951bf62301ef6135b39feca9'), (2452, 2454, 2563, 2566, 'b8675d8b37e31d0a110ad83f2ff2b34088070b114d5dac45dc10c551c2aead53'), (2476, 2478, 2588, 2588, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'), (2487, 2488, 2597, 2598, '83f449ce7772d84ce9ac0a9b9cdcb16383617e3becc1541ab318e90348fe57db'), (2499, 2500, 2609, 2610, '4827b0b7360c50d17cdf5a0b3592fe3d64a4239f5d693241a2a2c1ba8488f2e6'), (2502, 2504, 2612, 2614, 'bfbc4cc0acafae6438387e6e4bebf14f17bc149016e15a0ae6abb14690c89d62'), (2512, 2513, 2622, 2625, '03913141bcf113ddbf337a7ff2c6ad3715c43ec7844886321d9cd3aad6f78a76'), (2679, 2680, 2791, 2792, 'e4687962531e4b2f9d32415c8bfdc41fba0397427d5a01a860c243df16edd9ce'), (2682, 2683, 2794, 2795, '6031d21db4197987aa08211f0c51e6abaebae94254904ad94f768b4a81087666'), (2958, 2959, 3070, 3071, 'd5d6251c2c770d32d0fedea47f1db21889e89bb700b8e33c17c7924ff9cc17eb'), (2965, 2967, 3077, 3079, 'ce094c5278317c35244523b68d115d17a5a80e16541cb7e7fe3465e41f61d1a9'), (2969, 2970, 3081, 3082, 'e5cb3bdd55488858c739b1187664b1e303c7a801d194a99ceaa7ab41223e5b52'), (2978, 2979, 3090, 3091, '52fe28bb74f896a8e1e252fedabe5e5ec099f5b01fea34e83f47f2410cfddb1f'), (2988, 2989, 3100, 3101, '4780c07f429d36ca6bc78db5d264ef8498449c80d836d5236aeca31c1e79cb39'), (3004, 3005, 3116, 3117, '4a9d247e9a5544f2ba45e44e3bba96b653262921e85701770a5840f5f3f87931'), (3006, 3007, 3118, 3119, 'ecc35afecbb6f12f1e97729ca23b2da1c361aaab2d9e83d3c26a4e490c906ece'), (3054, 3054, 3166, 3169, 'bcbfd9eca831c08d2f433660ac10a76c6572158079016d87bbc90ce190c71154'), (3056, 3057, 3171, 3172, '15b015e080dcf932b4f02f3863d0594e054c892cff78fcb8e549165551fb1c6d'), (3535, 3536, 3650, 3651, '97243bf00dab142dfafdcc79f6acfcf145935980e6dd205f1f3df1deda85d0c8'), (3547, 3547, 3662, 3664, 'dfec83ca767e1519c64c73084b19d73a491d562d33bde389c232d59659114c6c'), (3567, 3569, 3684, 3688, 'febe4eafe67aecf1ee37467ae1e0f573938107176c8968f674f1190f75a19bf3'), (3574, 3575, 3693, 3696, 'e67a874153446d4f6bc32c91534fed99b0f3eafc507bbf0cf5c7f686e962fe7d'), (3603, 3607, 3724, 3738, 'a18589c49f3475420abdff5c4731f07cc65b8a0bc4d0d226b913154cc38bf2ae'), (3609, 3609, 3740, 3745, 'f55ac229408ff4e5b19b09a088aba75133174fa2797d7482b653be2cbccc0560'), (3660, 3661, 3796, 3797, '9f076b790a62fbf8f00f7aa12bd002e45affa44c7368ae4e23fed8e7a711d1dd'), (3793, 3793, 3929, 3931, 'bf2c1dd8c051cadc9fc5732d50e27edba61c0813dfa17e34ab7d0d39d508654d'), (3866, 3867, 4004, 4008, '1f5e3a90db10cbf52dbd9e36daf3bd844f54535a202c908d232e5feb3b46e40c'), (3936, 3937, 4077, 4081, 'deb87a38c843886a8980913f4c2df0f728727c7cfd4c7a365c2c1798428fea28'), (3939, 3940, 4083, 4086, '1912dcbd8f91edc52864ae1f8e4c04cbbd430144b5317b4c7c471b766b9d8fff'), (3949, 3950, 4095, 4096, 'c5b828d7f8754416c1e967cd784a7b139d61df0ab0f2d6667d234dd7207260ff'), (3979, 3981, 4125, 4128, 'a3692a2295620326b7c7725b61eb11066bee78eb85dbbfa586faab7f758e1eac'), (3992, 3993, 4139, 4147, '4a81ced0214296df690aec684f59ad2e09cbfde0469f6fa66ede5c77c296c92b'), (4028, 4029, 4182, 4184, 'b7e755d6977210fb6365dd31803e4f19ac37d7dbf283b720fa4eea047d0d3259'), (4030, 4030, 4185, 4194, 'bc849f665edcd3bb9d9101b8251f327f514116b3b8c4223824953098ea8d76c3'), (4059, 4060, 4223, 4227, '10bef6e809de56e3d400d59b0043fc2cdab99ec9cbab5c6ed3b638358f06b006'), (4128, 4129, 4295, 4296, '4e13bd2581ca8e532cdeed86530787611203fc5fb87f645715546a9570455e16'), (4146, 4147, 4313, 4314, '70023e09c43a169a3bdf43de367bec1fbba743292c9754ff3c1c84b2fd2e5e04'), (4149, 4150, 4316, 4317, '1fb9d6028255b2dd26a59d47acbbb3818dd99939c61f8765b82bc0d3773f8f1f')]
        for i1,i2,j1,j2,digest in reversed(v82_hunks):
            self.assertEqual(hashlib.sha256(''.join(v82_lines[j1:j2]).encode()).hexdigest(),digest)
            v82_lines[j1:j2]=v81_lines[i1:i2]
        new=''.join(v82_lines)
        self.assertEqual(new.encode(),v81)
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
