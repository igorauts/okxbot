#!/usr/bin/env python3
"""GET-only terminal accounting retry for completed v0.7.7 instances.

Runs from a separate systemd timer; never starts a bot service, places orders,
or accesses credentials itself. Each child engine has an explicit read-only
--terminal-finalize entrypoint.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

BASE=Path(os.environ.get('OKXBOT_BASE_DIR','/root/okx')).resolve()


def candidates():
    for state in sorted(BASE.glob('*/state/bot04_state.json')):
        root=state.parent.parent.resolve()
        if BASE not in root.parents:continue
        try:
            st=json.loads(state.read_text())
            if st.get('terminal_mode') not in ('PANIC_DONE','RETIRED'):continue
            engine=next((root/name for name in ('okx_bot078.py','okx_bot077.py')
                         if (root/name).is_file()),None)
            executable=root/'.venv/bin/python'
            if engine is None or not executable.is_file():continue
            target=root/'state/final_stats.json'
            if target.is_file():
                old=json.loads(target.read_text())
                if (old.get('complete') and old.get('mode')==st['terminal_mode'] and
                    old.get('terminal_state_sha256')==hashlib.sha256(state.read_bytes()).hexdigest()):
                    continue
            yield root
        except (OSError,ValueError,TypeError,KeyError):continue


def retry_once():
    count=0
    for root in candidates():
        name=root.name
        unit='okx-grid-'+name+'.service'
        active=subprocess.run(['systemctl','is-active',unit],capture_output=True,
                              text=True,timeout=8).stdout.strip()
        if active=='active':continue
        env={k:v for k,v in os.environ.items()
             if k not in ('OKX_BOT04_LIVE','OKX_BOT04_PROBE','OKX_BOT05_STATS')}
        engine=next((root/name for name in ('okx_bot078.py','okx_bot077.py')
                     if (root/name).is_file()),None)
        if engine is None:continue
        task=[str(root/'.venv/bin/python'),str(engine),'--terminal-finalize']
        try:
            r=subprocess.run(task,cwd=root,env=env,capture_output=True,
                             text=True,timeout=120)
            if r.returncode:
                print('TERMINAL PNL pending',name,'exit=',r.returncode,flush=True)
            else:print('TERMINAL PNL verified',name,flush=True)
        except (OSError,subprocess.TimeoutExpired):
            print('TERMINAL PNL retry needed',name,flush=True)
        count+=1
        if count>=4:break

if __name__=='__main__':retry_once()
