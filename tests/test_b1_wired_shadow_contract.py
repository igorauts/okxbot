"""B1 shadow wiring safety: contract is observational and fail-closed."""
import ast, pathlib, unittest
from okx_idle_reconcile import b1_shadow_pending_intents, classify_background_candidate_after_orders, b1_shadow_probe

class B1WiredContract(unittest.TestCase):
    def test_fail_closed_intents(self):
        self.assertTrue(b1_shadow_pending_intents({'cycles':None}))
        self.assertTrue(b1_shadow_pending_intents({'cycles':{'x':{'state':'PLACING'}}}))
        self.assertTrue(b1_shadow_pending_intents({'cycles':{},'panic_orders':[1]}))
        self.assertFalse(b1_shadow_pending_intents({'cycles':{}}))
    def test_verified_quiet_only(self):
        inputs=dict(trigger_source='timer',mode='RUN',orders_complete=True,
                    facts_changed=False,unresolved=False,pending_intents=False,
                    protective_deadline_due=False,private_or_virtual_or_grid_wake=False,
                    ws_healthy=True,control_changed=False,external_uncertainty=False)
        self.assertTrue(classify_background_candidate_after_orders(**inputs)[0])
        for k in ('facts_changed','pending_intents','private_or_virtual_or_grid_wake',
                  'protective_deadline_due','unresolved','external_uncertainty'):
            p=dict(inputs);p[k]=True
            self.assertFalse(classify_background_candidate_after_orders(**p)[0])
    def test_original_position_get_unchanged(self):
        src=pathlib.Path(__file__).resolve().parents[1]
        tree=ast.parse((src/'okx_bot.py').read_text())
        f=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)
               and n.name=='reconcile_position')
        calls=[ast.unparse(n.func) for n in ast.walk(f) if isinstance(n,ast.Call)]
        self.assertIn('A.pending',ast.unparse(f))
        self.assertIn('A.positions',ast.unparse(f))
        self.assertNotIn('classify_background_candidate_after_orders',ast.unparse(f))
        # No branch in reconciliation uses a shadow verdict.
    def test_shadow_never_changes_reader_priority(self):
        src=pathlib.Path(__file__).resolve().parents[1]
        tree=ast.parse((src/'okx_bot.py').read_text())
        f=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='live_loop')
        # Only one live callsite; all get and position gates remain original.
        self.assertEqual(sum(isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                             and n.func.id=='b1_shadow_probe'
                             for n in ast.walk(f)),1)
        for n in ast.walk(f):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='reconcile_position':
                self.assertFalse(any(x.arg=='priority' for x in n.keywords))
    def test_no_override_of_original_reader_client(self):
        src=pathlib.Path(__file__).resolve().parents[1]
        assert (src/'okx_position_reader_client.py').is_file()
        self.assertNotIn('b1_shadow',(src/'okx_position_reader_client.py').read_text())

if __name__=='__main__':unittest.main()
