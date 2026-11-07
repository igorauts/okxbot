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
        self.assertEqual(old[:a].rstrip(),new[:c].rstrip())
        self.assertEqual(old[b:],new[d:])

if __name__=='__main__':unittest.main()
