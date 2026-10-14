#!/usr/bin/env python3
"""Offline update of a CLEAN clone of igorauts/okxbot to v0.6.10.

Never invoke this against /root/okx/{eth064,sol065} or active bot directories.
Existing legacy regression tests are retained, while test_grid.py tests 0610.
"""
import argparse
from pathlib import Path
import shutil
import re

HERE=Path(__file__).resolve().parent


def replace_once(text,old,new,file):
    if text.count(old)!=1:raise RuntimeError(f'{file}: expected once {old!r}, got {text.count(old)}')
    return text.replace(old,new)


def prepare(repo):
    root=Path(repo).resolve()
    if not (root/'.git').is_dir() or not (root/'okxbot.py').is_file():
        raise RuntimeError('Must point to a clean Git repository clone')
    for name in ['okx_bot0610.py','test_v0610_safety.py','verify_real_import.py',
                 'manual_clear_account_soft.py','okx_telegram_monitor.py']:
        shutil.copy2(HERE/name,root/name)
    manager=root/'okxbot.py'
    src=manager.read_text()
    src=replace_once(src,"def engine_file(path):\n    for name in ('okx_bot068.py',", "def engine_file(path):\n    for name in ('okx_bot0610.py', 'okx_bot069.py', 'okx_bot068.py',",manager)
    start=src.index('def control_supported(entry):')
    end=src.index('\ndef read_control(entry):',start)
    src=src[:start]+'''def control_supported(entry):
    """Check the *effective* service command, including ordered systemd drop-ins.

    Unit-file-only checks are wrong after an engine upgrade performed with an
    ExecStart drop-in. No shell or manager writes happen in this function.
    """
    path=bot_path(entry)
    unit=SYSTEMD_DIR/unit_name(entry)
    if not unit.is_file():return False
    files=[unit]
    override=SYSTEMD_DIR/(unit.name+'.d')
    if override.is_dir():files+=sorted(override.glob('*.conf'))
    command=''
    for f in files:
        for line in f.read_text(encoding='utf-8').splitlines():
            if line.startswith('ExecStart='):
                command=line[len('ExecStart='):].strip()
    if not command:return False
    engines=('okx_bot0610.py','okx_bot069.py','okx_bot068.py',
             'okx_bot067.py','okx_bot066.py','okx_bot065.py','okx_bot064.py')
    return any((path/name).is_file() and str(path/name) in command.split()
               for name in engines)

'''+src[end:]
    manager.write_text(src)
    grid=root/'tests/test_grid.py'
    old=grid.read_text()
    # Legacy fixture is kept separately, and remains runnable for historical 068.
    # Prefixing test_ means both suites will be collected by unittest discover.
    (root/'tests/test_grid_legacy_v068.py').write_text(old)
    upgraded=old.replace('okx_bot068.py','okx_bot0610.py')
    upgraded=re.sub(r'format_(?!okx)[a-z]+_report', 'format_okx_report', upgraded)
    upgraded=upgraded.replace("'bot_version'],'0.6.8'", "'bot_version'],'0.6.10'")
    if upgraded==old:raise RuntimeError('Expected legacy grid fixture changes')
    grid.write_text(upgraded)
    (root/'tests/test_v0610_safety.py').write_text('''"""Discover v0.6.10 safety suite (root module) in normal unittest run."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_v0610_safety import SafetyTests
''')
    (root/'README_v0610_RU.md').write_text((HERE/'README_v0610_RU.md').read_text())
    (root/'RELEASE_NOTES_v0610.md').write_text((HERE/'RELEASE_NOTES_v0610.md').read_text())
    change=root/'CHANGELOG.md'
    changelog=change.read_text()
    changelog=replace_once(changelog,'# Changelog\n',
        '# Changelog\n\n'+(HERE/'CHANGELOG_v0610.md').read_text()+'\n',change)
    change.write_text(changelog)
    print('Prepared clone:',root)
    print('Old v068 regression suite retained, primary tests now use v0610.')
    print('Manager handles v0610 and effective systemd ExecStart drop-ins.')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('clone',help='Clean checkout of GitHub repo at previous release')
    args=parser.parse_args()
    prepare(args.clone)
