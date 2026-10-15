#!/usr/bin/env python3
"""Strict, fail-closed PATCH of a clean v0.7.3 checkout. No live server writes.

Only meant for an isolated git clone of igorauts/okxbot. Never invoke on a
working trading bot directory. This tool doesn't access .env or the OKX API.
"""
from __future__ import annotations
import argparse
import ast
import shutil
from pathlib import Path

SOURCE=Path(__file__).resolve().parent


def one(source,old,new,tag):
    n=source.count(old)
    if n!=1:raise RuntimeError(f'Unexpected upstream {tag}: anchor count={n}. FAIL CLOSED.')
    return source.replace(old,new,1)


def function_replace(source, name, new):
    tree=ast.parse(source)
    matches=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==name]
    if len(matches)!=1:raise RuntimeError(f'Unexpected upstream function {name}: {len(matches)}')
    node=matches[0]
    lines=source.splitlines(keepends=True)
    return ''.join(lines[:node.lineno-1])+new.rstrip()+'\n'+''.join(lines[node.end_lineno:])


def patch_wizard(source):
    source=one(source,"VER='0.7.3'","VER='0.7.4'",'wizard version')
    source=one(source,"steps=['step','tp','grids','qty','buys','real_tp','buy_buf','buy_protect','tp_buf','upper','name']",
               "steps=['step','tp','grids','qty','buys','real_tp','upper','name']",'wizard questions')
    source=one(source,"if not (2<=len(name)<=48):", "if not (3<=len(name)<=20):",'wizard name bounds')
    source=one(source,"Название: от 2 до 48 символов.","Название: от 3 до 20 символов.",'wizard name message')
    source=one(source,"and len(value)<48:","and len(value)<20:",'wizard name input cap')
    source=one(source,'f\'BUY буфер:                     {int(ctx["buy_buf"])}\'',"f'BUY буфер:                     +5'",'wizard report buy buffer')
    source=one(source,'f\'Защита BUY (шаги):             {int(ctx["buy_protect"])}\'',"f'Защита BUY (шаги):             5'",'wizard report protect')
    source=one(source,'f\'SELL TP буфер:                 {int(ctx["tp_buf"])}\'','f\'SELL TP буфер:                 +{int(ctx["buys"])+5}\'','wizard report tp buffer')
    source=one(source,"'buy_buffer_orders':int(ctx['buy_buf'])", "'buy_buffer_orders':5",'config buy buffer')
    source=one(source,"'buy_cancel_protect_steps':int(ctx['buy_protect'])", "'buy_cancel_protect_steps':5",'config protect')
    source=one(source,"'tp_buffer_orders':int(ctx['tp_buf'])", "'tp_buffer_orders':int(ctx['buys'])+5",'config tp buffer')
    source=one(source,"shutil.copy2(BASE/'okx_bot073.py',out/'okx_bot073.py')",
               "shutil.copy2(BASE/'okx_bot074.py',out/'okx_bot074.py')",'wizard engine install')
    # Even direct validate_numeric() calls cannot change the constants.
    start=source.index("    elif key in ('buy_buf','buy_protect','tp_buf'):")
    stop=source.index("    elif key=='upper':",start)
    source=source[:start]+"    elif key in ('buy_buf','buy_protect','tp_buf'):\n        raise ValueError('Служебные буферы и защита BUY фиксированы в v0.7.4.')\n"+source[stop:]
    start=source.index("    if key in ('buy_buf','buy_protect','tp_buf'):")
    stop=source.index("    if key=='upper':",start)
    source=source[:start]+source[stop:]
    # The bot must not expose maker/taker as inputs/visible fields, though the
    # wizard still queries rates privately to validate minimum profitable TP.
    source=one(source,"                f'Ваши комиссии ({ctx[\"fee_level\"]}):',\n",'', 'wizard fee title')
    source=one(source,"                f'  Maker:                     {ctx[\"maker\"]*100:.4f}%',\n",'', 'wizard maker')
    source=one(source,"                f'  Taker:                     {ctx[\"taker\"]*100:.4f}%', '',\n",'', 'wizard taker')
    source=one(source,"        f'Активных TP:                   {int(ctx[\"real_tp\"])}',",
               "        f'Реальных SELL TP:              {int(ctx[\"real_tp\"])} (макс. {int(ctx[\"real_tp\"])+int(ctx[\"buys\"])+5})',",'wizard tp display')
    source=one(source,"        f'Следящих BUY LIMIT:            {int(ctx[\"buys\"])}',",
               "        f'Активных BUY LIMIT:            {int(ctx[\"buys\"])} (макс. {int(ctx[\"buys\"])+5})',",'wizard buy display')
    ast.parse(source);return source


def patch_manager(source):
    if 'import okx_ui074' in source or 'v0.7.4' in source:
        raise RuntimeError('Already patched manager or unknown v0.7.4 source')
    source=one(source,"for name in ('okx_bot073.py'", "for name in ('okx_bot074.py','okx_bot073.py'",'manager engine priority')
    source=one(source,"engines=('okx_bot073.py'", "engines=('okx_bot074.py','okx_bot073.py'",'manager lifecycle support')
    source=one(source,"cfg.update(version='0.7.3',name=new_name", """cfg['real_tp_target']=int(cfg.get('real_tp_target',min(25,int(cfg.get('active_tp_orders',25)))))
    cfg['active_tp_orders']=cfg['real_tp_target']
    cfg['buy_buffer_orders']=5
    cfg['buy_cancel_protect_steps']=5
    cfg['tp_buffer_orders']=int(cfg['active_buy_orders'])+5
    cfg.update(version='0.7.4',name=new_name""",'manager clone config')
    source=one(source,"for f in ('okx_bot073.py','requirements.txt'):","for f in ('okx_bot074.py','requirements.txt'):",'clone engine file')
    source=one(source,"unit_text(dest,'okx_bot073.py',new_name)","unit_text(dest,'okx_bot074.py',new_name)",'clone unit engine')
    source=function_replace(source,'cmd_status', '''def cmd_status(name):
    from okx_ui074 import status
    name, entry = find_entry(name)
    print(status(name,bot_path(entry),unit_name(entry)))
''')
    source=function_replace(source,'cmd_show', '''def cmd_show(name):
    from okx_ui074 import settings
    name, entry = find_entry(name)
    print(settings(name,bot_path(entry),unit_name(entry)))
''')
    source=function_replace(source,'_capture_status', '''def _capture_status(name):
    from okx_ui074 import status
    name, entry = find_entry(name)
    return status(name,bot_path(entry),unit_name(entry))
''')
    source=function_replace(source,'_capture_settings', '''def _capture_settings(name):
    from okx_ui074 import settings
    name, entry = find_entry(name)
    return settings(name,bot_path(entry),unit_name(entry))
''')
    # Bring VPS readable peak-exposure overlay into the public manager; stats
    # remains a separate full financial screen.
    source=one(source,'        report = with_latest_bot_dd(report, path, instrument)',
               '''        report = with_latest_bot_dd(report, path, instrument)
        try:
            from okx_position_peak import format_peak
            peak=format_peak(path,instrument)
            if peak:
                report += "\\n"+"\\n".join(peak)
        except (OSError,ValueError,KeyError,ImportError):
            pass''','stats peak overlay')
    source=one(source,"                if chosen=='Статус' and is_new:\n                    content=f'Режим: {mode}\\nПоследняя операция: {read_ack(entry).get(\"phase\",\"—\")}\\n\\n'+content\n",'', 'remove duplicate RUN')
    # Manager is a single unified installation, but engine version is detected
    # per systemd ExecStart, so an older still-running bot remains accurate.
    source=one(source,"names = list(entries)","names = sorted(entries,key=lambda name: (not service_running(unit_name(entries[name])),name.casefold()))", 'sort active bots')
    # Monitoring per bot: new tools; existing v073 live services aren't killed.
    source=function_replace(source,'cmd_monitor', '''def cmd_monitor(name, kind, action):
    """Optional read-only observer per instance. Never touch trading service."""
    import okx_monitor074 as monitors
    _,entry=find_entry(name)
    service=entry['service'];root=bot_path(entry)
    script=SOURCE/'okx_monitor074.py'
    # If older v073 observer is enabled, turning OFF should disable it, too.
    if action=='off':
        try:
            import okx_monitor073 as legacy
            old=legacy.monitor_unit(service,kind)
            if service_running(old):
                oldscript=SOURCE/'okx_monitor073.py'
                run([sys.executable,str(oldscript),'--root',str(root),
                     '--service',service,'--kind',kind,'off'])
        except ImportError:
            pass
    run([sys.executable,str(script),'--root',str(root),
         '--service',service,'--kind',kind,action])
''')
    source=function_replace(source,'monitor_enabled', '''def monitor_enabled(entry,kind):
    import okx_monitor074 as monitors
    try:
        import okx_monitor073 as legacy
        old=service_running(legacy.monitor_unit(entry['service'],kind))
    except ImportError:
        old=False
    return (service_running(monitors.monitor_unit(entry['service'],kind)) or old)
''')
    ast.parse(source);return source


def patch_install(source):
    old_compile='/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot073.py" "$SRC/okx_monitor073.py" "$SRC/okx_execution_observer073.py"'
    new_compile='/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot074.py" "$SRC/okx_monitor074.py" "$SRC/okx_execution_observer074.py" "$SRC/okx_ui074.py" "$SRC/okx_position_peak.py"'
    source=one(source,old_compile,new_compile,'installer compile')
    source=one(source,'install -m 644 "$SRC/okx_bot073.py" "$DST/okx_bot073.py"',
               'install -m 644 "$SRC/okx_bot074.py" "$DST/okx_bot074.py"', 'installer engine')
    source=one(source,'install -m 644 "$SRC/okx_monitor073.py" "$DST/okx_monitor073.py"',
               'install -m 644 "$SRC/okx_monitor074.py" "$DST/okx_monitor074.py"', 'installer monitor')
    source=one(source,'install -m 644 "$SRC/okx_execution_observer073.py" "$DST/okx_execution_observer073.py"',
               'install -m 644 "$SRC/okx_execution_observer074.py" "$DST/okx_execution_observer074.py"\ninstall -m 644 "$SRC/okx_ui074.py" "$DST/okx_ui074.py"\ninstall -m 644 "$SRC/okx_position_peak.py" "$DST/okx_position_peak.py"', 'installer observer/ui')
    source=one(source,'OKX Grid Bot manager v0.7.3','OKX Grid Bot manager v0.7.4','installer banner')
    source=one(source,'okxbot new (v0.7.3)','okxbot new (v0.7.4)','installer instructions')
    return source


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--repo',required=True,type=Path);p.add_argument('--check',action='store_true')
    a=p.parse_args();repo=a.repo.resolve()
    needed=('okx_bot073.py','okx_setup068.py','okxbot.py','install.sh','README.md','CHANGELOG.md','okx_monitor073.py')
    for fn in needed:
        if not (repo/fn).is_file():raise FileNotFoundError(f'Missing upstream file: {fn}')
    # Pinned prior tag content is checked before changing anything. Source
    # updates after v0.7.3 are allowed if anchors remain unambiguous.
    if "VER = '0.7.3'" not in (repo/'okx_bot073.py').read_text():
        raise RuntimeError('v0.7.3 engine missing / unexpected upstream')
    modified={
        'okx_setup068.py':patch_wizard((repo/'okx_setup068.py').read_text()),
        'okxbot.py':patch_manager((repo/'okxbot.py').read_text()),
        'install.sh':patch_install((repo/'install.sh').read_text()),
    }
    new_files=('okx_bot074.py','okx_monitor074.py','okx_execution_observer074.py',
               'okx_ui074.py','okx_position_peak.py','README_v074_RU.md','RELEASE_NOTES_v074.md',
               'prepare_repo_v074.py','run_checks_universal_v074.sh','test_v074_rules.py',
               'test_v074_ui.py','test_v074_patch.py')
    for fn in new_files:
        if not (SOURCE/fn).is_file():raise FileNotFoundError(f'Missing release file: {fn}')
    if '## v0.7.4' in (repo/'CHANGELOG.md').read_text():
        raise RuntimeError('CHANGELOG already contains v0.7.4')
    readme=repo/'README.md';old=readme.read_text()
    addition='''# OKX Grid Bot v0.7.4 — BTC / ETH / SOL

**Текущий релиз:** [v0.7.4](https://github.com/igorauts/okxbot/releases/tag/v0.7.4) · [Документация](README_v074_RU.md) · [Изменения](RELEASE_NOTES_v074.md). Менеджер и движок v0.7.4, встроенный optional мониторинг для каждого экземпляра.

**Важно:** публикация релиза не перезапускает и не обновляет уже работающие боты. Новые экземпляры создаются с отключённой LIVE-торговлей.

---

'''
    # Remove superseded v073/v072 top-level current headers without altering
    # long-form historical instructions (they remain marked as archive).
    import re
    old=re.sub(r'\A# OKX Grid Bot v0\.7\.3 — BTC / ETH / SOL\n.*?\n---\n(?:\n)?','',old,flags=re.S)
    if old.startswith('# OKX Grid Bot v0.7.3'):
        raise RuntimeError('Stale v0.7.3 README heading could not be replaced')
    # The previous v0.7.3 documentation update introduced a separate current
    # version box; update it instead of leaving conflicting CURRENT releases.
    old=old.replace('**Текущая версия: [v0.7.3](https://github.com/igorauts/okxbot/releases/tag/v0.7.3)**',
                    '**Предыдущая версия: [v0.7.3](https://github.com/igorauts/okxbot/releases/tag/v0.7.3)**')
    old=old.replace('**[Полный CHANGELOG от v0.5.9 до v0.7.3](CHANGELOG.md)**',
                    '**[Полный CHANGELOG от v0.5.9 до v0.7.4](CHANGELOG.md)**')
    old=old.replace('не актуальные лимиты, версию движка или порядок установки v0.7.3',
                    'не актуальные лимиты, версию движка или порядок установки v0.7.4')
    old=old.replace('Для новой установки используйте [документацию v0.7.3](README_v073_UNIVERSAL_RU.md)',
                    'Для новой установки используйте [документацию v0.7.4](README_v074_RU.md)')
    modified['README.md']=addition+old
    changelog='''## v0.7.4 — 2026-10-15

- Unified manager/status/settings: actual version from systemd, real-time local BUY/SELL/virtual/partial counts, process and VPS uptime, monitored grid gaps with freshness checks. LIVE journal preserved.
- New wizard: only BUY and real SELL TP base counts configurable (1..25); fixed BUY buffer +5, near-BID cancel protection 5 steps; SELL buffer dynamically BUY base +5 (6..30); unique bot names 3..20 characters.
- Old config `active_tp_orders=75` no longer shown as effective target; legacy buffer/protection entries ignored by v0.7.4 engine. Historical state is preserved during v0.7.3→v0.7.4 migration.
- Trading engine exposes its actual TP cooldown and cancel-gap decision timings to avoid false observer warnings. No performance changes to REST execution or durable order handling.
- One package for BTC/ETH/SOL, optional per-instance monitors. No automatic LIVE changes.

'''
    modified['CHANGELOG.md']=changelog+(repo/'CHANGELOG.md').read_text()
    if a.check:
        print('✅ v0.7.3 -> v0.7.4: all three upstream patches validated; no changes')
        return
    for fn,txt in modified.items():(repo/fn).write_text(txt,encoding='utf-8')
    for fn in new_files:shutil.copy2(SOURCE/fn,repo/fn)
    print('✅ v0.7.4 source staged in isolated checkout, only source/docs changed')

if __name__=='__main__':main()
