"""Production run_terminal/Application with synthetic I/O and explicit dependencies.

No BioMind startup/config is imported. Cache mode is VIS-only, never deployment.
Raw ANSI and casts are actual PTY bytes; no grayscale picture claims color.
"""
import argparse
import asyncio
import codecs
import fcntl
import hashlib
import importlib.abc
import importlib.metadata
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import sysconfig
import termios
import time

PYTHON='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
CACHE=Path('/tmp/biomind-cli-deps')


def child(source, result, case, dependency_source):
    source=Path(source).resolve();result=Path(result).resolve()
    site=Path(sysconfig.get_path('purelib'))
    sys.path[:0]=[str(source),str(site)]
    if dependency_source=='isolated-cache':sys.path.insert(1,str(CACHE))
    def guard(event,args):
        if event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','sqlite3.connect'}:
            raise RuntimeError('CLI deployment probe forbids network, processes, database')
        if event=='open' and isinstance(args[0],(str,bytes)):
            p=Path(os.fsdecode(args[0])).resolve()
            if p.name.startswith('.env') or p.name in {'启动.sh','.netrc','.gitconfig'} or 'config' in p.parts:
                raise RuntimeError('Protected configuration read denied')
            roots=[source,result,Path(sys.prefix),Path('/usr/lib'),Path('/usr/share')]
            if dependency_source=='isolated-cache':roots.append(CACHE)
            if not any(p.is_relative_to(b) for b in roots) and str(p) not in {'/dev/null','/dev/urandom','/etc/localtime'}:
                raise RuntimeError('Read outside CLI probe: '+str(p))
    sys.addaudithook(guard)
    if case=='missing':
        class Missing(importlib.abc.MetaPathFinder):
            def find_spec(self,fullname,*args):
                if fullname.split('.')[0] in {'prompt_toolkit','wcwidth'}:
                    raise ModuleNotFoundError('Synthetic missing optional CLI dependency')
        sys.meta_path.insert(0,Missing())
    import agent_core.cli_terminal as cli
    from agent_core.cli_display import terminal, emit_public, flush_public
    from agent_core.cli_activity import call_with_activity
    result_data={'dependency_source':dependency_source,'executable':sys.executable,
                 'case':case,'runtime_calls':0,'model_calls':0,'tool_calls':0,'actions':[],
                 'cache_on_path':str(CACHE) in sys.path,'modules':{},'history_writes':[]}
    original_init=cli.Terminal.__init__
    def instrument(owner,*args,**kwargs):
        original_init(owner,*args,**kwargs)
        out=owner.app.output
        current={'attrs':{}}
        set_attrs,reset,write=out.set_attributes,out.reset_attributes,out.write
        def attrs(value,depth):
            current['attrs']=value._asdict();set_attrs(value,depth)
        def reset_attrs():current['attrs']={};reset()
        def written(text):
            # Delegate to real Vt100Output, preserving the actual ANSI stream.
            if text.strip():result_data['history_writes'].append({'text':text,'attrs':dict(current['attrs'])})
            write(text)
        out.set_attributes=attrs;out.reset_attributes=reset_attrs;out.write=written
    cli.Terminal.__init__=instrument
    from tests.replay_cli_v2_pty import pulse
    async def runtime():
        pulse(asyncio.get_running_loop())
        result_data['runtime_calls']+=1;owner=terminal()
        result_data['owner']=type(owner).__name__
        result_data['stdin_tty']=sys.stdin.isatty();result_data['stdout_tty']=sys.stdout.isatty()
        for name in ['prompt_toolkit','wcwidth']:
            m=sys.modules.get(name)
            if m:
                p=Path(m.__file__).resolve()
                result_data['modules'][name]={'file':str(p),'version':getattr(m,'__version__',None),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        emit_public('合成公开说明：先读取小文件，再报告真实返回。',source_message_id='synthetic-public')
        await flush_public()
        p=result/'small.txt';p.write_text('synthetic content\n')
        value=call_with_activity('读取小文件',p.read_text,display_scope={'tool_category':'read','tool_call_id':'synthetic-read'})
        result_data['tool_calls']+=1;result_data['actions'].append({'action':'read','result':value})
        owner.model.finalize_tools()
        if case=='error':
            try:
                call_with_activity('检查合成缺项',lambda: (_ for _ in ()).throw(ValueError('synthetic failure')),display_scope={'tool_call_id':'failed-check'})
            except ValueError:result_data['actions'].append({'action':'failed-check','result':'ValueError'});result_data['tool_calls']+=1
            owner.receive('error','合成错误：输入缺项。\n详细说明保留原文，不代表已经修复。')
            owner.receive('cancel','合成取消：尚未派发执行。')
        emit_public('合成公开结论：文件读取已返回；未运行科学计算。',source_message_id='synthetic-final')
        await flush_public()
        result_data['actions'].append({'action':'finish','result':'synthetic-only'})
    async def entry():
        # Reuse the established sandbox selector-wakeup service on both loops.
        # Production owner, writes, futures and approval parsing stay real.
        pulse(asyncio.get_running_loop())
        await cli.run_terminal(runtime)
    asyncio.run(entry())
    (result/'child.json').write_text(json.dumps(result_data,ensure_ascii=False,indent=2))


def record(source,result,case='normal',dependency_source='installed',cols=80):
    source=Path(source).resolve();result=Path(result).resolve()
    assert source.is_relative_to(Path('/tmp')) and result.is_relative_to(Path('/tmp'))
    manifest=json.loads((source/'export_manifest.json').read_text())
    assert all(hashlib.sha256((source/p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())
    result.mkdir(exist_ok=False)
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','TERM':'dumb' if case=='dumb' else 'xterm-256color',
         'BIOMIND_UI':'plain' if case=='plain' else 'off' if case=='off' else '',
         'PYTHONDONTWRITEBYTECODE':'1'}
    if case=='no-color':env['PROMPT_TOOLKIT_COLOR_DEPTH']='DEPTH_1_BIT'
    command=[PYTHON,'-I','-S',str(Path(__file__).resolve()),'--child',str(source),str(result),case,dependency_source]
    tty=case!='pipe';chunks=[];cast=[];started=time.monotonic()
    if tty:
        master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,cols,0,0))
        def attach():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
        proc=subprocess.Popen(command,cwd=source,env=env,stdin=slave,stdout=slave,stderr=slave,preexec_fn=attach);os.close(slave)
        # The existing decoder is parent-side only. It does not add cache to the child.
        sys.path.insert(0,str(source))
        from tests.replay_cli_v2_pty import VTTextScreen
        screen=VTTextScreen(cols,24);decoder=codecs.getincrementaldecoder('utf-8')('replace')
        while time.monotonic()-started<35:
            if select.select([master],[],[],.03)[0]:
                try:b=os.read(master,65536)
                except OSError:break
                if not b:break
                chunks.append(b);s=decoder.decode(b);cast.append([time.monotonic()-started,'o',s])
                for reply in screen.feed(s):os.write(master,reply.encode())
            elif proc.poll() is not None:break
        if proc.poll() is None:proc.kill()
        rc=proc.wait();os.close(master)
        (result/'screen.txt').write_text(screen.text())
    else:
        p=subprocess.run(command,cwd=source,env=env,input=b'',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=35);rc=p.returncode;chunks=[p.stdout]
    raw=b''.join(chunks);(result/'terminal.ansi').write_bytes(raw)
    with (result/'terminal.cast').open('w') as f:
        f.write(json.dumps({'version':2,'width':cols,'height':24,'title':'Actual production Terminal; dependency source '+dependency_source})+'\n')
        for row in cast:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    data=json.loads((result/'child.json').read_text()) if (result/'child.json').exists() else {}
    expected='PlainTerminal' if case in {'plain','pipe','dumb','off','missing'} else 'Terminal'
    checks={'exit_zero':rc==0,'owner':data.get('owner')==expected,
            'runtime_once':data.get('runtime_calls')==1,'model_calls_zero':data.get('model_calls')==0,
            'real_small_read':data.get('actions',[{}])[0].get('result')=='synthetic content\n',
            'dependency_source_explicit':data.get('cache_on_path')==(dependency_source=='isolated-cache')}
    receipt={'case':case,'dependency_source':dependency_source,'deployment_evidence':dependency_source=='installed',
             'source':manifest['source'],'source_signature':manifest['source_signature'],'command':command,'environment':env,
             'returncode':rc,'checks':checks,'passed':all(checks.values()),'expected_owner':expected,'actual_owner':data.get('owner'),
             'ansi_sha256':hashlib.sha256(raw).hexdigest(),'initial_size':[cols,24],
             'source_unchanged':all(hashlib.sha256((source/p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())}
    (result/'result.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2));print(json.dumps(receipt,ensure_ascii=False))
    return receipt


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':child(*sys.argv[2:])
    else:
        p=argparse.ArgumentParser();p.add_argument('--export',required=True);p.add_argument('--results',required=True)
        p.add_argument('--case',default='normal');p.add_argument('--dependency-source',choices=['installed','isolated-cache'],default='installed');p.add_argument('--cols',type=int,default=80)
        a=p.parse_args();r=record(a.export,a.results,a.case,a.dependency_source,a.cols)
        raise SystemExit(0 if r['passed'] else 1)
