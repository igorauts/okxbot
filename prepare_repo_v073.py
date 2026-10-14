#!/usr/bin/env python3
"""Fail-closed source-only patch of a clean igorauts/okxbot checkout.

This script NEVER touches a live instance, trading state, SQLite or .env.
Run only from the public checkout root cloned by publish_v073.sh.
"""
from __future__ import annotations
import ast
import argparse
from pathlib import Path
import shutil

SOURCE=Path(__file__).resolve().parent

def sub(text,old,new,desc):
    count=text.count(old)
    if count!=1:raise ValueError(f'{desc}: expected one anchor, got {count}: {old[:80]!r}')
    return text.replace(old,new,1)

def patch_wizard(src):
    if "VER='0.7.3'" in src:raise ValueError('Repository already prepared for 0.7.3')
    src=sub(src,"VER='0.6.8'","VER='0.7.3'",'wizard version')
    old='''    elif key=='real_tp':
        if x!=x.to_integral_value() or not 1<=x<=min(75,99-int(ctx.get('buys',25))):
            raise ValueError(f'Реальных TP допускается 1–{min(75,99-int(ctx.get("buys",25)))}: BUY+TP не больше 99.')
    elif key=='upper':'''
    new='''    elif key=='real_tp':
        if x!=x.to_integral_value() or not 1<=x<=25:
            raise ValueError('Количество базовых реальных TP: 1–25.')
    elif key in ('buy_buf','buy_protect','tp_buf'):
        max_allowed={'buy_buf':5,'buy_protect':10,'tp_buf':30}[key]
        if x!=x.to_integral_value() or not 0<=x<=max_allowed:
            raise ValueError(f'{key}: целое число 0–{max_allowed}.')
        if key=='buy_buf' and int(ctx.get('buys',25))+int(x)>30:
            raise ValueError('BUY вместе с буфером не может быть больше 30.')
    elif key=='upper':'''
    src=sub(src,old,new,'wizard validators')
    src=sub(src,"'Минимум: 1',f'Максимум: {min(75,99-int(ctx.get(\"buys\",25)))}','',\n        f'Например: {min(75,99-int(ctx.get(\"buys\",25)))}','', 'Количество TP:']", "'Минимум: 1','Максимум: 25','',\n        'Например: 25','', 'Базовых реальных TP:']",'wizard tp bounds')
    src=sub(src,"    if key=='upper':\n        x1=to_tick_up", """    if key in ('buy_buf','buy_protect','tp_buf'):
        cap={'buy_buf':5,'buy_protect':10,'tp_buf':30}[key]
        title={'buy_buf':'Дополнительные BUY','buy_protect':'Зона запрета отмены BUY (в шагах)',
               'tp_buf':'Временный буфер SELL TP'}[key]
        default={'buy_buf':5,'buy_protect':3,'tp_buf':30}[key]
        return [title,'',f'Допустимо: 0–{cap}',f'Рекомендуется: {default}',
                'BUY buffer — число ордеров, защита BID — шаги сетки.',
                'Укажите значение:']
    if key=='upper':
        x1=to_tick_up""",'wizard new prompts')
    src=sub(src,"steps=['step','tp','grids','qty','buys','real_tp','upper','name']", "steps=['step','tp','grids','qty','buys','real_tp','buy_buf','buy_protect','tp_buf','upper','name']",'wizard steps')
    src=sub(src,"'active_buy_orders':int(ctx.get('buys',25)),'active_tp_orders':int(ctx['real_tp']),", """'active_buy_orders':int(ctx.get('buys',25)),'active_tp_orders':int(ctx['real_tp']),
         'real_tp_target':int(ctx['real_tp']),
         'buy_buffer_orders':int(ctx['buy_buf']),
         'buy_cancel_protect_steps':int(ctx['buy_protect']),
         'tp_buffer_orders':int(ctx['tp_buf']),""",'wizard config')
    src=sub(src,"shutil.copy2(BASE/'okx_bot068.py',out/'okx_bot068.py')", "shutil.copy2(BASE/'okx_bot073.py',out/'okx_bot073.py')",'wizard engine copy')
    src=sub(src,"        f'Активных TP:                   {int(ctx[\"real_tp\"])}',", """        f'Активных TP:                   {int(ctx["real_tp"])}',
        f'BUY буфер:                     {int(ctx["buy_buf"])}',
        f'Защита BUY (шаги):             {int(ctx["buy_protect"])}',
        f'SELL TP буфер:                 {int(ctx["tp_buf"])}',""",'wizard report')
    ast.parse(src)
    return src

def patch_manager(src):
    src=sub(src,"for name in ('okx_bot0610.py',", "for name in ('okx_bot073.py','okx_bot072.py','okx_bot071.py','okx_bot070.py','okx_bot0610.py',",'manager engine priority')
    src=sub(src,"engines=('okx_bot0610.py',", "engines=('okx_bot073.py','okx_bot072.py','okx_bot071.py','okx_bot070.py','okx_bot0610.py',",'manager control capabilities')
    src=sub(src,"cfg.update(version='0.6.8',name=new_name", "cfg.update(version='0.7.3',name=new_name",'manager clone version')
    src=sub(src,"for f in ('okx_bot068.py','requirements.txt'):", "for f in ('okx_bot073.py','requirements.txt'):",'manager clone files')
    src=sub(src,"unit_text(dest,'okx_bot068.py',new_name)", "unit_text(dest,'okx_bot073.py',new_name)",'manager clone engine')
    anchor='def interactive_bot_menu(name):'
    monitor='''def cmd_monitor(name, kind, action):
    """Optional observer per instance. NO trading service start/stop."""
    import okx_monitor073 as monitors
    _,entry=find_entry(name)
    service=entry['service']
    root=bot_path(entry)
    script=SOURCE/'okx_monitor073.py'
    run([sys.executable,str(script),'--root',str(root),'--service',service,
         '--kind',kind,action])

def monitor_enabled(entry,kind):
    import okx_monitor073 as monitors
    return service_running(monitors.monitor_unit(entry['service'],kind))

'''+anchor
    src=sub(src,anchor,monitor,'manager monitor commands')
    src=sub(src,"choices=['Статистика','Статус','Настройки','Логи (LIVE)']", """choices=['Статистика','Статус','Настройки','Логи (LIVE)',
                 'Burst Monitor: '+('ON' if monitor_enabled(entry,'burst') else 'OFF'),
                 'Execution Monitor: '+('ON' if monitor_enabled(entry,'execution') else 'OFF')]""",'manager menu')
    src=sub(src,"        if chosen=='Статистика':", """        if chosen.startswith(('Burst Monitor:','Execution Monitor:')):
            kind='burst' if chosen.startswith('Burst') else 'execution'
            cmd_monitor(name,kind,'off' if monitor_enabled(entry,kind) else 'on')
        elif chosen=='Статистика':""",'manager menu handler')
    src=sub(src,"    opts=p.parse_args(argv)", """    mon=sub.add_parser('monitor',help='read-only monitor for selected instance')
    mon.add_argument('name');mon.add_argument('kind',choices=('burst','execution'))
    mon.add_argument('setting',choices=('on','off','status','report'))
    opts=p.parse_args(argv)""",'manager cli parser')
    src=sub(src,"    elif cmd=='audit':audit_entry(find_entry(name)[1])", """    elif cmd=='audit':audit_entry(find_entry(name)[1])
    elif cmd=='monitor':cmd_monitor(name,opts.kind,opts.setting)""",'manager cli dispatch')
    ast.parse(src)
    return src

def patch_install(src):
    src=sub(src, '/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot068.py"', '/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot073.py" "$SRC/okx_monitor073.py" "$SRC/okx_execution_observer073.py"', 'installer compile check')
    src=sub(src, 'install -m 644 "$SRC/okx_bot068.py" "$DST/okx_bot068.py"', '''install -m 644 "$SRC/okx_bot073.py" "$DST/okx_bot073.py"
install -m 644 "$SRC/okx_monitor073.py" "$DST/okx_monitor073.py"
install -m 644 "$SRC/okx_execution_observer073.py" "$DST/okx_execution_observer073.py"''', 'installer files')
    src=sub(src, "Установлен OKX Grid Bot manager v0.6.8.","Установлен OKX Grid Bot manager v0.7.3.",'installer welcome')
    src=sub(src, "Создание новых ботов: okxbot new (v0.6.8)","Создание новых ботов: okxbot new (v0.7.3)",'installer create')
    return src

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--repo',type=Path,required=True)
    a.add_argument('--check',action='store_true')
    opt=a.parse_args()
    repo=opt.repo.resolve()
    for name in ('okxbot.py','okx_setup068.py','install.sh','requirements.txt'):
        if not (repo/name).is_file():raise ValueError('Missing upstream file '+name)
    transformed={
        'okxbot.py':patch_manager((repo/'okxbot.py').read_text()),
        'okx_setup068.py':patch_wizard((repo/'okx_setup068.py').read_text()),
        'install.sh':patch_install((repo/'install.sh').read_text()),
    }
    public_files=('okx_bot073.py','okx_monitor073.py','okx_execution_observer073.py',
                  'prepare_repo_v073.py',
                  'test_universal_v073.py','README_v073_UNIVERSAL_RU.md',
                  'RELEASE_NOTES_v073.md','run_checks_universal_v073.sh',
                  'manual_clear_account_soft.py',
                  'test_universal_observer_v073.py','test_repo_patch_v073.py',
                  *tuple('test_v073_'+name+'.py' for name in (
                        'safety','state','crossing','aggregate','buffer','near_bid','profiling','tp_burst')))
    for name in public_files:
        if not (SOURCE/name).is_file():raise FileNotFoundError(name)
    if opt.check:
        print('✅ Manager/wizard/install strict anchors & syntax validated, no writes')
        return
    for name,val in transformed.items(): (repo/name).write_text(val)
    for name in public_files:shutil.copy2(SOURCE/name,repo/name)
    # Appends, never rewrites historical releases.
    readme=repo/'README.md'
    old=readme.read_text()
    info='''# OKX Grid Bot v0.7.3 — BTC / ETH / SOL

**Universal v0.7.3:** [documentation](README_v073_UNIVERSAL_RU.md) · [release notes](RELEASE_NOTES_v073.md). One configurable engine, instance manager, optional read-only burst / execution monitors. Default monitoring OFF.

**Publishing does NOT upgrade any running bot.** Do not commit .env, config.json, SQLite or user state. New bots are created with live trading disabled and require explicit read-only exchange audit and live confirmation.

---

'''
    if 'OKX Grid Bot v0.7.3 — BTC / ETH / SOL' not in old:readme.write_text(info+old)
    changelog=repo/'CHANGELOG.md';old=changelog.read_text()
    entry='''## v0.7.3 — 2026-10-14

- One BTC/ETH/SOL-USDT-SWAP LONG grid engine, from ETH/SOL live-tested v0.7.3 code.
- Independently configurable BUY target (1–25), temporary BUY buffer (0–5), near-BID protection (0–10 grid steps), SELL TP base (1–25), temporary SELL TP buffer (0–30).
- BUY following, TP 1500ms grace, one-at-a-time cleanup (>=100ms), virtual TP and aggregate reduce-only market exits.
- Instance wizard and manager select one universal engine. Optional read-only Burst and Execution monitors selectable per instance (off by default).
- Offline tests across BTC/ETH/SOL. No automatic LIVE upgrades or trading restarts.

'''
    if '## v0.7.3' not in old:changelog.write_text(entry+old)
    print('✅ v0.7.3 staged in repository checkout. No live changes.')

if __name__=='__main__':main()
