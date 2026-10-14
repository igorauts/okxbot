#!/usr/bin/env python3
"""Update ETH CLI manager to recognize okx_bot072.py; dry-run default."""
import argparse,os,shutil,subprocess,tempfile
from pathlib import Path
from datetime import datetime,timezone

def patch(text):
    key="        if str(path / 'okx_bot071.py') in effective:"
    engines="engines=('okx_bot071.py','okx_bot070.py',"
    if text.count(key)!=1 or text.count(engines)!=1:
        raise RuntimeError('Unrecognized v071 manager layout; abort without changes')
    if "okx_bot072.py" in text:
        raise RuntimeError('v072 manager update already present')
    addition="""        if str(path / 'okx_bot072.py') in effective:
            if not (path / 'okx_bot072.py').is_file():
                raise RuntimeError('ETH v072 engine missing')
            return 'okx_bot072.py'
"""
    return text.replace(key,addition+key,1).replace(
        engines,"engines=('okx_bot072.py','okx_bot071.py','okx_bot070.py',",1)

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--target',default='/opt/okx-grid-v068/okxbot.py')
    a.add_argument('--apply',action='store_true')
    arg=a.parse_args()
    target=Path(arg.target)
    old=target.read_text()
    new=patch(old)
    compile(new,str(target),'exec')
    print('✅ Manager v072 patch validated')
    if not arg.apply:
        print('DRY RUN: no changes to manager')
        return
    st=target.stat()
    backup=target.with_name(target.name+'.backup-before-eth072-'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S'))
    shutil.copy2(target,backup)
    fd,tmp=tempfile.mkstemp(prefix='.okxbot-v072.',dir=target.parent)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(new)
            f.flush();os.fsync(f.fileno())
        os.chmod(tmp,st.st_mode & 0o777)
        os.chown(tmp,st.st_uid,st.st_gid)
        os.replace(tmp,target)
        subprocess.run(['python3','-m','py_compile',str(target)],check=True)
        subprocess.run(['python3',str(target),'--help'],check=True,stdout=subprocess.DEVNULL)
        print('✅ Manager updated, backup:',backup)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
if __name__=='__main__':main()
