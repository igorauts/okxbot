#!/usr/bin/env python3
"""Pure, strictly fail-closed planning helpers for 0.7.12 -> 0.7.14.

No mutating functions; this is NOT an installer. Real application requires
separate process/state coordination, backups, explicit LIVE consent and
verification of the effective systemd command including all drop-ins.
"""
from __future__ import annotations

from pathlib import Path
import re
import shlex


class RolloutPlanError(RuntimeError):
    pass


def expected_bot_service(root):
    root=Path(root)
    if (not root.is_absolute() or not re.fullmatch(r'[a-z0-9-]+',root.name)
            or root.parent!=Path('/root/okx')):
        raise RolloutPlanError('Invalid bot root for standard service')
    return 'okx-grid-'+root.name


def check_no_command_overrides(dropin_texts):
    """Fail closed: any ExecStart/ExecStopPost assignment in a drop-in blocks migration."""
    for content in dropin_texts:
        for line in content.splitlines():
            if re.match(r'^\s*(?:ExecStart|ExecStopPost)\s*=',line):
                raise RolloutPlanError('Systemd ExecStart/ExecStopPost drop-in override: inspect manually')


def plan_unit_rewrite(unit_content, root, service, *,
                      new_shared='/opt/okx-grid-live'):
    """Only prepare deterministic text; NEVER write a unit or execute host service commands.

    Recognizes the *published* 0.7.12 unit format. Unknown local edits, unit
    drop-ins or service mismatches require manual review, NOT best-effort patching.
    """
    root=Path(root)
    expected=expected_bot_service(root)
    if service!=expected:
        raise RolloutPlanError('Service/root identity mismatch')
    if new_shared!='/opt/okx-grid-live':
        raise RolloutPlanError('Unexpected shared installation root')
    lines=unit_content.splitlines(keepends=True)
    seen={'WorkingDirectory':0,'ExecStart':0,'ExecStopPost':0}
    changed=[]
    for line in lines:
        stripped=line.rstrip('\r\n')
        if '=' not in stripped:
            changed.append(line)
            continue
        key,value=stripped.split('=',1)
        if key not in seen:
            changed.append(line)
            continue
        seen[key]+=1
        if key=='WorkingDirectory':
            if value!=str(root):
                raise RolloutPlanError('WorkingDirectory differs from registered bot')
            changed.append(line)
        elif key=='ExecStart':
            try: args=shlex.split(value)
            except ValueError as exc: raise RolloutPlanError('Unparseable ExecStart') from exc
            expected_py=str(root/'.venv/bin/python')
            expected_engine=str(root/'okx_bot0712.py')
            if args not in ([expected_py,'-u',expected_engine],
                            [expected_py,'-B','-u',expected_engine]):
                raise RolloutPlanError('Unknown/mismatched live ExecStart; no rewrite')
            args[-1]=str(root/'okx_bot.py')
            changed.append('ExecStart='+' '.join(args)+'\n')
        elif key=='ExecStopPost':
            try: args=shlex.split(value)
            except ValueError as exc: raise RolloutPlanError('Unparseable ExecStopPost') from exc
            # Must exactly match published cleanup semantics. No extra arguments.
            old=['/usr/bin/python3','-B',
                 '/opt/okx-grid-v0712-live/okx_panic_cleanup0712.py',
                 '--root',str(root),'--service',service]
            if args!=old:
                raise RolloutPlanError('Unknown/mismatched ExecStopPost; no rewrite')
            args[2]=new_shared+'/okx_panic_cleanup.py'
            changed.append('ExecStopPost='+' '.join(args)+'\n')
    if seen!={'WorkingDirectory':1,'ExecStart':1,'ExecStopPost':1}:
        raise RolloutPlanError('Missing or duplicate WorkingDirectory/ExecStart/ExecStopPost')
    result=''.join(changed)
    if result==unit_content:
        raise RolloutPlanError('No unit migration to perform')
    return result
