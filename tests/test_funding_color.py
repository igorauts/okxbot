import ast
import re
import unittest
from pathlib import Path

class FakeCurses:
    A_NORMAL=0; A_BOLD=1; A_DIM=2; COLOR_GREEN=2; COLOR_RED=1
    class error(Exception): pass
    def __init__(self, colors=True): self.colors=colors
    def has_colors(self): return self.colors
    def start_color(self): pass
    def use_default_colors(self): pass
    def init_pair(self,*args): pass
    def color_pair(self,n): return n*256

class Screen:
    def __init__(self, width=80, fail=False):
        self.width=width; self.fail=fail; self.text=[]; self.attrs=[]
    def getmaxyx(self): return (12,self.width)
    def erase(self): pass
    def refresh(self): pass
    def addnstr(self,y,x,text,width,attr=0): self.text.append((y,x,text[:width],attr))
    def chgat(self,y,x,n,attr):
        if self.fail: raise FakeCurses.error()
        self.attrs.append((y,x,n,attr))

class FundingColor(unittest.TestCase):
    def render(self,line,colors=True,width=80,fail=False,paint=True):
        source=Path(__file__).resolve().parents[1]/'okxbot.py'
        tree=ast.parse(source.read_text())
        funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_put','_viewer_frame')]
        env={'curses':FakeCurses(colors),'re':re}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),str(source),'exec'),env)
        scr=Screen(width,fail)
        env['_viewer_frame'](scr,'Title',[line],0,'Back',paint_stats=paint)
        return scr
    def test_positive_negative_and_zero(self):
        for amount,attr in [('+19.164214',256),('−4.556413',512),('-1.000001',512),('+0.000000',2),('−0.000000',2)]:
            with self.subTest(amount=amount):
                line='FUNDING: '+amount+', 0ч 01м 02с назад'
                scr=self.render(line)
                self.assertEqual(scr.text[0],(0,1,'FUNDING: ● '+line[9:],0))
                self.assertEqual(scr.attrs,[(0,10,1,attr)])
    def test_no_colors_and_color_failure(self):
        for kw in ({'colors':False},{'fail':True}):
            scr=self.render('FUNDING: +1.000000, 1ч 00м 00с назад',**kw)
            self.assertTrue(scr.text[0][2].startswith('FUNDING: ● +1.000000'))
            self.assertFalse(scr.attrs)
    def test_existing_pnl_colors_unchanged(self):
        for line,attr in [('GRID PROFIT: +$1',257),('ИТОГОВЫЙ PnL: −$1',513),('Floating PnL: −$1',512)]:
            scr=self.render(line)
            self.assertEqual(scr.text[0],(0,1,line,attr));self.assertFalse(scr.attrs)
    def test_error_and_other_views_unchanged(self):
        line='FUNDING: ошибка чтения'
        scr=self.render(line);self.assertEqual(scr.text[0],(0,1,line,0));self.assertFalse(scr.attrs)
        line='FUNDING: +1.000000, 0ч 00м 00с назад'
        scr=self.render(line,paint=False);self.assertEqual(scr.text[2][2],line);self.assertFalse(scr.attrs)
    def test_narrow_terminal(self):
        scr=self.render('FUNDING: +1.000000, 0ч 00м 00с назад',width=24)
        self.assertEqual(len(scr.text[0][2]),22);self.assertEqual(scr.attrs,[(0,10,1,256)])
