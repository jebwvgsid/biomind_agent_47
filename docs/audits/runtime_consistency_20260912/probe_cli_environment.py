"""Real interpreter/package inventory + production owner entry; no application startup."""
import asyncio
import hashlib
import importlib.machinery
import importlib.metadata
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import sysconfig
import time

PYTHON='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
if len(sys.argv)>1 and sys.argv[1]=='--child':
    root=Path(sys.argv[2]).resolve()
    # -I -S avoids site hooks, user-site and PYTHONPATH. Only actual conda site is added.
    site=Path(sysconfig.get_path('purelib'))
    sys.path[:0]=[str(root),str(site)]
    def guard(event,args):
        if event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','sqlite3.connect'}:
            raise RuntimeError('CLI inventory forbids network/process/database')
        if event=='open' and isinstance(args[0],(str,bytes)):
            p=Path(os.fsdecode(args[0])).resolve()
            if p.name.startswith('.env') or p.name in {'启动.sh','.netrc'} or 'config' in p.parts:
                raise RuntimeError('Protected read denied')
            if not any(p.is_relative_to(b) for b in (root,Path(sys.prefix),Path('/usr/lib'),Path('/usr/share'))) and str(p) not in {'/dev/null','/dev/urandom','/etc/localtime'}:
                raise RuntimeError('Read outside CLI inventory: '+str(p))
    sys.addaudithook(guard)
    from agent_core.cli_terminal import run_terminal
    import agent_core.cli_display as display
    async def runtime():
        data={'executable':sys.executable,'python':sys.version.split()[0], 'owner':type(display._terminal).__name__,
              'stdin_tty':sys.stdin.isatty(),'stdout_tty':sys.stdout.isatty(),
              'synthetic_mode':os.getenv('BIOMIND_UI',''), 'synthetic_term':os.getenv('TERM'),
              'prompt_toolkit_spec':str(importlib.machinery.PathFinder.find_spec('prompt_toolkit',[str(site)])),
              'module':display.__file__,'module_sha256':hashlib.sha256(Path(display.__file__).read_bytes()).hexdigest(),
              'cache_on_path':any('/tmp/biomind-cli-deps' in p for p in sys.path), 'runtime_calls':1,'model_calls':0,'commands':0}
        print('CLI_RECEIPT '+json.dumps(data),flush=True)
    asyncio.run(run_terminal(runtime))
else:
    root=Path(sys.argv[1]).resolve();dest=Path(sys.argv[2]).resolve();dest.mkdir(exist_ok=False)
    receipts=[]
    for label,tty,mode in [('auto-pty',True,''),('forced-plain-pty',True,'plain'),('auto-pipe',False,'')]:
        env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','TERM':'xterm-256color','BIOMIND_UI':mode,'PYTHONDONTWRITEBYTECODE':'1'}
        cmd=[PYTHON,'-I','-S',str(Path(__file__).resolve()),'--child',str(root)]
        if tty:
            master,slave=pty.openpty()
            proc=subprocess.Popen(cmd,env=env,stdin=slave,stdout=slave,stderr=slave,cwd=root)
            os.close(slave);chunks=[];deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try:b=os.read(master,65536)
                    except OSError:break
                    if not b:break
                    chunks.append(b)
                elif proc.poll() is not None:break
            if proc.poll() is None:proc.kill()
            rc=proc.wait();os.close(master);raw=b''.join(chunks)
        else:
            p=subprocess.run(cmd,env=env,input=b'',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,cwd=root,timeout=20);rc=p.returncode;raw=p.stdout
        (dest/(label+'.raw')).write_bytes(raw)
        lines=raw.decode().splitlines();row=next((json.loads(s.split('CLI_RECEIPT ',1)[1]) for s in lines if 'CLI_RECEIPT ' in s),None)
        result={'label':label,'returncode':rc,'command':cmd,'environment':env,'receipt':row,'missing_notice':'动态界面依赖缺失' in raw.decode()}
        receipts.append(result)
    (dest/'results.json').write_text(json.dumps(receipts,indent=2,ensure_ascii=False))
    assert all(r['returncode']==0 and r['receipt']['owner']=='PlainTerminal' and not r['receipt']['cache_on_path'] for r in receipts),receipts
    assert [r['missing_notice'] for r in receipts]==[True,False,False]
    print(json.dumps(receipts,ensure_ascii=False))
