"""Offline cleanup contracts: unchanged live paths, exact FULL specialization,
immutable history and narrowly scoped installer retirement. No OKX or systemd.
"""
import ast
import copy
import gzip
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import okx_live_upgrade as upgrade
from okx_install_manifest import load

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'engine_manifest.json').read_text())
INSTALL=load(ROOT,MANIFEST)
OLD_ENGINE=gzip.decompress((ROOT/'tests/fixtures/okx_bot_v0785_baseline.py.gz').read_bytes())
OLD_SHA='c68f370655cb7c59a0741d3089c6a5a17f87e3e67e7f6e96f9481c13dc1302cb'
REMOVED_FUNCTIONS={'bot_dd_mark','expected_cycle_sells','controller_owns_orders','stats_amount'}
PROTOTYPES={'okx_stats_v2_checkpoint.py','okx_stats_v2_phase4.py'}
OBSOLETE={'okx_stats_finance_incremental.py','okx_stats_incremental_core.py','okx_stats_incremental_finance.py','okx_stats_incremental_runtime.py','okx_stats_sqlite_delta_v0751.py'}
REMOVED=PROTOTYPES|OBSOLETE

def digest(raw):return hashlib.sha256(raw).hexdigest()
def dump(node):return ast.dump(node,include_attributes=False)
def old_version():
    matches=[v for v,e in MANIFEST['engines'].items() if e.get('sha256')==OLD_SHA]
    if len(matches)!=1:raise AssertionError('Ambiguous historical engine')
    return matches[0]

class SpecializeNoneCaches(ast.NodeTransformer):
    """Independent specialization of the old FULL function's only used path.
    No execution of engine code or arbitrary expressions.
    """
    cache_names={'stats_cache','finance_cache'}
    def visit_FunctionDef(self,n):
        pairs=[(a,d) for a,d in zip(n.args.kwonlyargs,n.args.kw_defaults) if a.arg not in self.cache_names]
        n.args.kwonlyargs=[a for a,d in pairs];n.args.kw_defaults=[d for a,d in pairs]
        return self.generic_visit(n)
    def visit_Compare(self,n):
        if (isinstance(n.left,ast.Name) and n.left.id in self.cache_names
                and len(n.ops)==1 and isinstance(n.ops[0],ast.IsNot)
                and isinstance(n.comparators[0],ast.Constant) and n.comparators[0].value is None):
            return ast.Constant(False)
        return self.generic_visit(n)
    def visit_BoolOp(self,n):
        n=self.generic_visit(n)
        if isinstance(n.op,ast.Or):
            n.values=[v for v in n.values if not(isinstance(v,ast.Constant) and v.value is False)]
            if len(n.values)==1:return n.values[0]
            if not n.values:return ast.Constant(False)
        if isinstance(n.op,ast.And) and any(isinstance(v,ast.Constant) and v.value is False for v in n.values):
            return ast.Constant(False)
        return n
    def visit_IfExp(self,n):
        n=self.generic_visit(n)
        if isinstance(n.test,ast.Constant) and n.test.value is False:return n.orelse
        return n
    def visit_If(self,n):
        n=self.generic_visit(n)
        if isinstance(n.test,ast.Constant) and n.test.value is False:return n.orelse
        return n
    def visit_Assign(self,n):
        if any(isinstance(t,ast.Name) and t.id in ('cached_past24','finance_mode') for t in n.targets):return None
        return self.generic_visit(n)

class CleanupContracts(unittest.TestCase):
    def test_only_approved_engine_nodes_changed(self):
        self.assertEqual(digest(OLD_ENGINE),OLD_SHA)
        before=ast.parse(OLD_ENGINE); after=ast.parse((ROOT/'okx_bot.py').read_bytes())
        def keep(n):return not(isinstance(n,ast.FunctionDef) and n.name in REMOVED_FUNCTIONS|{'print_full_stats'})
        self.assertEqual([dump(n) for n in before.body if keep(n)],[dump(n) for n in after.body if keep(n)])
        self.assertFalse(any(isinstance(n,ast.Name) and n.id in REMOVED_FUNCTIONS for n in ast.walk(after)))
        self.assertFalse(any(isinstance(n,ast.FunctionDef) and n.name in REMOVED_FUNCTIONS for n in after.body))
    def test_full_path_is_exact_none_cache_specialization(self):
        def full(raw):return next(n for n in ast.parse(raw).body if isinstance(n,ast.FunctionDef) and n.name=='print_full_stats')
        old=full(OLD_ENGINE);new=full((ROOT/'okx_bot.py').read_bytes())
        self.assertEqual(dump(SpecializeNoneCaches().visit(old)),dump(new))
    def test_runtime_imports_and_install_inventory(self):
        for name in REMOVED:
            self.assertFalse((ROOT/name).exists(),name)
            self.assertNotIn(name,INSTALL['engine']);self.assertNotIn(name,INSTALL['shared']);self.assertNotIn(name,INSTALL['source_only'])
            self.assertIn(name,INSTALL['retire_shared'])
        # Check all installed and source-only production modules, not merely engine.
        for path in list(ROOT.glob('*.py'))+list((ROOT/'funding').glob('*.py')):
            for node in ast.walk(ast.parse(path.read_bytes())):
                mods=([n.name for n in node.names] if isinstance(node,ast.Import) else [node.module] if isinstance(node,ast.ImportFrom) and node.module else [])
                for module in mods:self.assertNotIn(module.split('.')[0]+'.py',REMOVED,(path,module))
    def test_retired_prototypes_are_exact_historical_bytes(self):
        old=MANIFEST['engines'][old_version()]
        for name in PROTOTYPES:
            h=digest((ROOT/'historical'/name).read_bytes())
            self.assertEqual(h,old['dependency_sha256'][name]);self.assertEqual(h,INSTALL['retire_shared'][name])
    def test_current_dependency_changes_are_only_identity_and_retirement(self):
        old=MANIFEST['engines'][old_version()];new=MANIFEST['engines'][MANIFEST['current_version']]
        self.assertEqual(set(old['dependencies'])-set(new['dependencies']),PROTOTYPES)
        self.assertEqual(set(new['dependencies'])-set(old['dependencies']),set())
        for name in new['dependencies']:
            if name!='okx_version.py':self.assertEqual(new['dependency_sha256'][name],old['dependency_sha256'][name],name)
    def test_real_canary_preflight_allows_exact_retired_set_only(self):
        oldid=old_version();newid=MANIFEST['current_version'];old=MANIFEST['engines'][oldid]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in old['dependencies']:
                if name in PROTOTYPES:shutil.copyfile(ROOT/'historical'/name,root/name)
                elif name=='okx_version.py':
                    data=(ROOT/name).read_text().replace("RELEASE_VERSION = %r"%newid,"RELEASE_VERSION = %r"%oldid).replace("ENGINE_VERSION = %r"%newid,"ENGINE_VERSION = %r"%oldid)
                    (root/name).write_text(data)
                else:shutil.copyfile(ROOT/name,root/name)
                self.assertEqual(digest((root/name).read_bytes()),old['dependency_sha256'][name])
            with patch.object(upgrade,'STAGE',ROOT),patch.object(upgrade,'OLD_VERSION',oldid),patch.object(upgrade,'NEW_VERSION',newid):
                self.assertTrue(upgrade.verify_single_instance_sources(root))
                invalid=copy.deepcopy(INSTALL);invalid['retire_shared'].pop(sorted(PROTOTYPES)[0])
                with patch('okx_install_manifest.load',return_value=invalid):
                    with self.assertRaisesRegex(RuntimeError,'removal not pinned'):upgrade.verify_single_instance_sources(root)
                (root/sorted(PROTOTYPES)[0]).write_text('tampered')
                with self.assertRaisesRegex(RuntimeError,'Installed dependency drift'):upgrade.verify_single_instance_sources(root)
    def test_shared_retirement_does_not_touch_unrecognized_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'shared';root.mkdir();backup=Path(td)/'backup';backup.mkdir()
            names=sorted(PROTOTYPES); exact=names[0];unknown=names[1]
            shutil.copyfile(ROOT/'historical'/exact,root/exact);(root/unknown).write_text('operator custom')
            with patch.object(upgrade,'STAGE',ROOT),patch.object(upgrade,'SHARED',root),patch.object(upgrade,'stage_check'),patch.object(upgrade,'backup_dir',return_value=backup),patch.object(upgrade,'run',side_effect=AssertionError('systemd forbidden')):
                upgrade.install_shared()
            self.assertFalse((root/exact).exists());self.assertEqual((root/unknown).read_text(),'operator custom')
            self.assertEqual(digest((backup/'old'/exact).read_bytes()),INSTALL['retire_shared'][exact])

if __name__=='__main__':unittest.main()
