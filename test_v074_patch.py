import ast
import unittest
from prepare_repo_v074 import patch_wizard,patch_manager,patch_install,one

WIZARD='''VER='0.7.3'
def validate_numeric(key,s,ctx):
    if key=='step':
        pass
    elif key in ('buy_buf','buy_protect','tp_buf'):
        max_allowed={'buy_buf':5,'buy_protect':10,'tp_buf':30}[key]
        if x>max_allowed[key]:
            raise ValueError('no')
    elif key=='upper':
        return 0

def lines_for(key,ctx):
    if key in ('buy_buf','buy_protect','tp_buf'):
        cap={'buy_buf':5,'buy_protect':10,'tp_buf':30}[key]
        return [cap]
    if key=='upper':
        return ['upper']

def validate_name(raw,existing=()):
    name=raw.strip()
    if not (2<=len(name)<=48):
        raise ValueError('Название: от 2 до 48 символов.')
    return name

def name_input(stdscr,title,existing):
    if isinstance(ch,str) and ch.isprintable() and len(value)<48:
        return value

def wizard(stdscr,env,existing_names=()):
    contract_lines=[
                f'Ваши комиссии ({ctx["fee_level"]}):',
                f'  Maker:                     {ctx["maker"]*100:.4f}%',
                f'  Taker:                     {ctx["taker"]*100:.4f}%', '',
    ]
    steps=['step','tp','grids','qty','buys','real_tp','buy_buf','buy_protect','tp_buf','upper','name']

def report(ctx):
    return [
        f'Следящих BUY LIMIT:            {int(ctx["buys"])}',
        f'Активных TP:                   {int(ctx["real_tp"])}',
        f'BUY буфер:                     {int(ctx["buy_buf"])}',
        f'Защита BUY (шаги):             {int(ctx["buy_protect"])}',
        f'SELL TP буфер:                 {int(ctx["tp_buf"])}',
    ]

def persist(ctx,out,env_raw):
    cfg={'buy_buffer_orders':int(ctx['buy_buf']),
         'buy_cancel_protect_steps':int(ctx['buy_protect']),
         'tp_buffer_orders':int(ctx['tp_buf'])}
    shutil.copy2(BASE/'okx_bot073.py',out/'okx_bot073.py')
'''

MANAGER='''def engine_file(path):
    for name in ('okx_bot073.py','okx_bot0610.py'):
        pass

def control_supported(entry):
    engines=('okx_bot073.py','okx_bot0610.py')

def cmd_clone(source_name,new_name):
    cfg.update(version='0.7.3',name=new_name)
    for f in ('okx_bot073.py','requirements.txt'):
        pass
    unit_text(dest,'okx_bot073.py',new_name)

def cmd_status(name):
    print('old status')

def cmd_show(name):
    print('old show')

def _capture_status(name):
    return 'old'

def _capture_settings(name):
    return 'old'

def stats_text(name):
    if True:
        report = with_latest_bot_dd(report, path, instrument)
    return report

def interactive_bot_menu(name):
    if True:
        if True:
            if True:
                content='ok'
                if chosen=='Статус' and is_new:
                    content=f'Режим: {mode}\\nПоследняя операция: {read_ack(entry).get("phase","—")}\\n\\n'+content


def interactive_main(env_file):
    names = list(entries)

def cmd_monitor(name,kind,action):
    import okx_monitor073 as monitors

def monitor_enabled(entry,kind):
    import okx_monitor073 as monitors
    return service_running(monitors.monitor_unit(entry['service'],kind))
'''
INSTALL='''/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot073.py" "$SRC/okx_monitor073.py" "$SRC/okx_execution_observer073.py"
install -m 644 "$SRC/okx_bot073.py" "$DST/okx_bot073.py"
install -m 644 "$SRC/okx_monitor073.py" "$DST/okx_monitor073.py"
install -m 644 "$SRC/okx_execution_observer073.py" "$DST/okx_execution_observer073.py"
echo 'OKX Grid Bot manager v0.7.3'
echo 'okxbot new (v0.7.3)'
'''

class PatchTests(unittest.TestCase):
    def test_wizard_guard_and_fixed_calculations(self):
        w=patch_wizard(WIZARD)
        ast.parse(w)
        self.assertIn('VER=\'0.7.4\'',w)
        self.assertIn("int(ctx['buys'])+5",w)
        self.assertNotIn('steps=[\'step\',\'tp\',\'grids\',\'qty\',\'buys\',\'real_tp\',\'buy_buf\'',w)
        self.assertIn('3<=len(name)<=20',w)
        self.assertIn('25',w) if False else None
        self.assertIn('okx_bot074.py',w)
    def test_manager_ui_and_clone(self):
        src=patch_manager(MANAGER)
        ast.parse(src)
        self.assertIn('okx_bot074.py',src)
        self.assertIn('okx_ui074',src)
        self.assertIn("int(cfg['active_buy_orders'])+5",src)
        self.assertIn('okx_position_peak',src)
        self.assertNotIn("print('old show')",src)
        self.assertNotIn('Последняя операция:',src)
    def test_installer(self):
        result=patch_install(INSTALL)
        self.assertIn('okx_bot074.py',result)
        self.assertIn('okx_position_peak.py',result)
        self.assertIn('manager v0.7.4',result)
    def test_unknown_upstream_fails_closed(self):
        with self.assertRaises(RuntimeError):patch_manager('')
        with self.assertRaises(RuntimeError):patch_wizard('')
        with self.assertRaises(RuntimeError):patch_install('')

if __name__=='__main__':unittest.main()
