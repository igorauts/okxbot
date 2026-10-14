#!/usr/bin/env python3
"""MANUAL release of a v0.6.9 account margin latch. Does NOT resume bots.

Require TWO paths, both SOFT_STOPPED and operator confirmation. Never run
while another bot sharing the same account has not reached SOFT_STOPPED.
"""
import argparse,fcntl,json,os
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--bot',action='append',type=Path,required=True,
               help='Bot directory; pass twice for eth064 and sol065')
p.add_argument('--shared',type=Path,default=Path('/var/lib/okxbot/account_soft_stop.json'))
p.add_argument('--confirm',default='')
a=p.parse_args()
if len(a.bot)!=2 or len({x.resolve() for x in a.bot})!=2:
    p.error('Exactly two different --bot directories are required')
if not a.shared.is_absolute():p.error('--shared must be absolute')

for root in a.bot:
    control=root/'state'/'control.json'
    ack=root/'state'/'control_ack.json'
    try:
        mode=json.loads(control.read_text())['mode']
        rec=json.loads(ack.read_text())
    except (OSError,ValueError,KeyError) as exc:
        p.error(f'{root}: missing/invalid local SOFT status: {exc}')
    if mode!='SOFT' or rec.get('mode')!='SOFT' or rec.get('phase')!='SOFT_STOPPED':
        p.error(f'{root}: must be SOFT + SOFT_STOPPED, got {mode} / {rec}')

lock=a.shared.with_name(a.shared.name+'.lock')
fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
try:
    fcntl.flock(fd,fcntl.LOCK_EX)
    if not a.shared.exists():
        p.error('Shared latch not found: nothing to clear')
    data=json.loads(a.shared.read_text())
    if (data.get('schema')!=1 or data.get('mode')!='SOFT'
        or data.get('reason')!='MARGIN_REJECTION'):
        p.error('Unexpected latch content; refusing release')
    print(json.dumps(data,ensure_ascii=False,indent=2))
    if a.confirm!='I_REVIEWED_MARGIN_AND_BOTH_BOTS':
        print('\nREAD-ONLY check complete: no changes made.')
        print('To clear the latch, repeat with --confirm I_REVIEWED_MARGIN_AND_BOTH_BOTS')
    else:
        a.shared.unlink()
        # Persist directory entry removal so normal restart does not re-latch.
        dirfd=os.open(a.shared.parent,os.O_RDONLY)
        try:os.fsync(dirfd)
        finally:os.close(dirfd)
        print('Common latch cleared; BOTH control.json files remain SOFT.')
        print('Use existing manager to request RUN on each bot SEPARATELY when ready.')
finally:
    fcntl.flock(fd,fcntl.LOCK_UN)
    os.close(fd)
