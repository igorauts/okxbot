#!/usr/bin/env python3
"""Regression: buffer max 30 does not crash after 26th BUY."""
import ast
import copy
import unittest
from pathlib import Path

SRC=Path(__file__).with_name('okx_bot072.py')
TREE=ast.parse(SRC.read_text())

def load_helper():
    nodes=[copy.deepcopy(n) for n in TREE.body
           if isinstance(n, ast.FunctionDef) and n.name=='validate_buy_buffer']
    assert len(nodes)==1
    ns={'BUY_MAX':30,'die':lambda msg: (_ for _ in ()).throw(ValueError(msg))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),
                 str(SRC),'exec'),ns)
    return ns['validate_buy_buffer']

class TestBuffer(unittest.TestCase):
    def setUp(self):
        self.check=load_helper()

    def test_normal_25(self):
        self.check([None]*25)

    def test_first_buffer_level_26(self):
        self.check([None]*26)

    def test_full_buffer_30(self):
        self.check([None]*30)

    def test_overflow_31_rejected(self):
        with self.assertRaisesRegex(ValueError,'buffer maximum'):
            self.check([None]*31)

    def test_live_loop_uses_buffer_check(self):
        loops=[n for n in TREE.body if isinstance(n,ast.FunctionDef)
               and n.name=='live_loop']
        self.assertEqual(len(loops),1)
        self.assertTrue(any(isinstance(n,ast.Call)
             and isinstance(n.func,ast.Name)
             and n.func.id=='validate_buy_buffer'
             for n in ast.walk(loops[0])))
        self.assertFalse(any(isinstance(n,ast.Compare)
             and any(isinstance(c,ast.Name) and c.id=='TARGET' for c in n.comparators)
             and isinstance(n.left,ast.Call) and getattr(n.left.func,'id',None)=='len'
             and any(isinstance(op,ast.Gt) for op in n.ops)
             for n in ast.walk(loops[0])))

if __name__=='__main__':unittest.main()
