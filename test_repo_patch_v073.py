#!/usr/bin/env python3
"""Fail-closed public checkout patch test. Only synthetic sources, no git network."""
import unittest
import prepare_repo_v073 as patch

class RepoPatch(unittest.TestCase):
    def test_manager_patch_with_expected_upstream_anchors(self):
        src='''
def engine_file(path):
    for name in ('okx_bot0610.py','okx_bot069.py'):
        pass

def control_supported(entry):
    engines=('okx_bot0610.py','okx_bot069.py')
    return engines

def cmd_clone(new_name):
    cfg={}
    cfg.update(version='0.6.8',name=new_name)
    SOURCE='path'
    for f in ('okx_bot068.py','requirements.txt'):
        pass
    unit_text(dest,'okx_bot068.py',new_name)

def unit_text(*args):
    return args

def interactive_bot_menu(name):
    while True:
        entry={}
        choices=['Статистика','Статус','Настройки','Логи (LIVE)']
        chosen='Статистика'
        if chosen=='Статистика':
            return True

def main(argv=None):
    sub=type('X',(),{'add_parser':lambda *a,**k:None})()
    opts=p.parse_args(argv)
    cmd=''
    name=''
    if cmd=='list':pass
    elif cmd=='audit':audit_entry(find_entry(name)[1])
'''
        transformed=patch.patch_manager(src)
        self.assertIn('okx_bot073.py',transformed)
        self.assertIn("sub.add_parser('monitor'",transformed)
        self.assertIn('Burst Monitor: ',transformed)
        self.assertIn("elif cmd=='monitor'",transformed)
    def test_wizard_patch_source_exact(self):
        src='''
VER='0.6.8'
def validate_numeric(key,x,ctx):
    if key=='buys':pass
    elif key=='real_tp':
        if x!=x.to_integral_value() or not 1<=x<=min(75,99-int(ctx.get('buys',25))):
            raise ValueError(f'Реальных TP допускается 1–{min(75,99-int(ctx.get("buys",25)))}: BUY+TP не больше 99.')
    elif key=='upper':
        pass

def lines_for(key,ctx):
    if key=='real_tp':return [
        'Укажите количество реальных TP на бирже.', '',
        'Минимум: 1',f'Максимум: {min(75,99-int(ctx.get("buys",25)))}','',
        f'Например: {min(75,99-int(ctx.get("buys",25)))}','', 'Количество TP:']
    if key=='upper':
        x1=to_tick_up()

def wizard():
    steps=['step','tp','grids','qty','buys','real_tp','upper','name']

def persist(ctx,out):
    cfg={
        'active_buy_orders':int(ctx.get('buys',25)),'active_tp_orders':int(ctx['real_tp']),
    }
    shutil.copy2(BASE/'okx_bot068.py',out/'okx_bot068.py')

def report(ctx):
    return [
        f'Активных TP:                   {int(ctx["real_tp"])}',
    ]
'''
        updated=patch.patch_wizard(src)
        self.assertIn("'buy_buffer_orders':int(ctx['buy_buf'])",updated)
        self.assertIn("BASE/'okx_bot073.py'",updated)
        self.assertIn("'buy_cancel_protect_steps':int(ctx['buy_protect'])",updated)
        self.assertIn('tp_buffer_orders',updated)
    def test_install_patch(self):
        s='''/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot068.py"
install -m 644 "$SRC/okx_bot068.py" "$DST/okx_bot068.py"
printf 'Установлен OKX Grid Bot manager v0.6.8. Создание новых ботов: okxbot new (v0.6.8)'
'''
        r=patch.patch_install(s)
        self.assertIn('okx_bot073.py',r)
        self.assertIn('okx_monitor073.py',r)
        self.assertIn('v0.7.3',r)
    def test_unknown_manager_rejected_without_changes(self):
        with self.assertRaises(ValueError):patch.patch_manager('def unknown():pass')

if __name__=='__main__':unittest.main()
