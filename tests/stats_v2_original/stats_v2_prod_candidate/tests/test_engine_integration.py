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
