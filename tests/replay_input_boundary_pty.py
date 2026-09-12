"""Kernel PTY replay of the production task reader followed by real code review."""
import argparse
import codecs
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
import time

TASK='检查合成输入\n最终保留真实检查结论。\n\n'


def test_input_boundary_pty_child():
    if os.getenv('BIOMIND_INPUT_BOUNDARY_PTY')!='1':
        import pytest
        pytest.skip('kernel PTY child only')
    import tests.replay_approval_visibility_pty as replay
    original=replay.production_reviews
    async def with_task(owner,root,scenario):
        import bio_agents
        agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
        agent.input_timeout_seconds=0;agent.data_path=str(root/'scratch');agent.state_manager=None
        replay.atomic_json(root/'input-phase.json',{'phase':'task'})
        value=await agent._aprompt_user('synthetic-input','interactive_prompt','TASK > ')
        if value=='/paste':
            replay.atomic_json(root/'input-phase.json',{'phase':'multiline'})
            value=await agent._aprompt_user('synthetic-input','interactive_prompt','BLOCK /end > ',multiline=True)
        replay.atomic_json(root/'input-task.json',{'value':value})
        await original(owner,root,scenario)
    replay.production_reviews=with_task
    try:replay.test_production_approval_pty_child()
    finally:replay.production_reviews=original


def record(source,root,mode='plain',framing='bracketed',ending='lf',decision='y'):
    from tests.replay_cli_v2_pty import VTTextScreen, PYTHON, DEPS
    source=Path(source).resolve();root=Path(root).resolve()
    assert source.is_relative_to(Path('/tmp')) and root.is_relative_to(Path('/tmp'))
    manifest=json.loads((source/'export_manifest.json').read_text())
    assert all(hashlib.sha256((source/f).read_bytes()).hexdigest()==v for f,v in manifest['files'].items())
    root.mkdir(exist_ok=False);(root/'scratch').mkdir()
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONPATH':DEPS if mode=='tty' else '',
        'TERM':'xterm-256color','PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1',
        'TMPDIR':str(root/'scratch'),'BIOMIND_WIKI_ENABLED':'0','LOG_LEVEL':'CRITICAL',
        'BIOMIND_OFFLINE_RESULTS':str(root/'pytest-results.json'),'BIOMIND_CLI_PROGRESS_ARTIFACTS':str(root),
        'BIOMIND_GER_ARTIFACTS':str(root),'BIOMIND_EXEC_LOG_DIR':str(root/'scratch/executor-logs'),
        'BIOMIND_APPROVAL_PTY':'1','BIOMIND_APPROVAL_SCENARIO':'approve','BIOMIND_INPUT_BOUNDARY_PTY':'1',
        'BIOMIND_UI':mode,'BIOMIND_CLI_VERBOSITY':'compact'}
    command=[PYTHON,str(source/'tests/cli_model_progress_support.py'),'--guarded-child',str(source),'-q','-s',
        'tests/replay_input_boundary_pty.py::test_input_boundary_pty_child']
    master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0))
    def attach():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
    child=subprocess.Popen(command,cwd=source,env=env,stdin=slave,stdout=slave,stderr=slave,preexec_fn=attach);os.close(slave)
    raw=bytearray();sent=[];screen=VTTextScreen(100,24);decoder=codecs.getincrementaldecoder('utf-8')('replace')
    began=time.monotonic();first=None;pending=None;tail=False;submitted=False;details=False;snapshot={}
    newline=b'\r\n' if ending=='crlf' else b'\n'
    def read(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    def send(data):os.write(master,data);sent.append({'at':time.monotonic(),'hex':data.hex()})
    try:
        while time.monotonic()-began<50:
            if select.select([master],[],[],.02)[0]:
                try:data=os.read(master,65536)
                except OSError as e:
                    if e.errno==errno.EIO:break
                    raise
                if not data:break
                raw.extend(data)
                for reply in screen.feed(decoder.decode(data)):os.write(master,reply.encode())
            text=raw.decode('utf-8','replace');phase=read('input-phase.json')
            if first is None and ('TASK > ' in text or (mode=='tty' and read('frame-state.json').get('input_ready') and read('frame-state.json').get('prompt')=='TASK > ')):
                send(b'/paste'+(b'\r' if mode=='tty' else newline) if framing=='explicit' else b'\x1b[200~'+TASK.split('\n')[0].encode()+newline)
                first=time.monotonic()
            if first is not None and not tail and time.monotonic()-first>.25 and (framing!='explicit' or 'BLOCK /end > ' in text or (mode=='tty' and read('frame-state.json').get('prompt')=='BLOCK /end > ')):
                content=TASK if framing=='explicit' else '\n'.join(TASK.split('\n')[1:])
                send(content.encode().replace(b'\n',newline)+(b'/end' if framing=='explicit' else b'\x1b[201~')+(b'\r' if mode=='tty' else newline))
                tail=True
            b=read('business.json');frame=read('frame-state.json')
            visible=bool(frame.get('pending') and (frame.get('input_ready') if mode=='tty' else '[y/Enter=run' in text))
            if tail and visible and pending is None:pending=time.monotonic()
            if pending and not submitted and time.monotonic()-pending>.5:
                snapshot={'task':read('input-task.json'),'business':b,'frame':frame,'screen':screen.text()}
                (root/'pending.screen.txt').write_text(screen.text())
                if decision=='details':
                    if not details:
                        send(b'/details\r');details=True;continue
                    if mode=='tty':
                        if details is True and not frame.get('modal'):
                            continue
                        if frame.get('modal'):
                            if details is True:
                                send(b'\x1b');details='closing'
                            continue
                    elif 'body-line-429' not in text:
                        continue
                submitted=True
                if decision=='eof':send(b'\x04')
                elif decision=='cancel':send(b'\x03')
                else:send((TASK.split('\n')[1] if decision=='feedback' else '' if decision=='enter' else 'y' if decision=='details' else decision).encode()+b'\r')
            if read('phase.json').get('phase')=='business_done' or (root/'child-error.json').exists():
                (root/'finish').write_text('finish')
            if child.poll() is not None:break
        if child.poll() is None:
            child.terminate();child.wait(timeout=5)
    finally:os.close(master)
    b=read('business.json');actual=read('input-task.json').get('value')
    checks={'task_complete':actual==TASK.removesuffix('\n') if framing=='explicit' else actual==TASK,
        'pending_execution_zero':snapshot.get('business',{}).get('executions')==[],
        'pending_no_decision':snapshot.get('business',{}).get('decisions')==[],
        'model_calls_zero':b.get('model_calls')==0,
        'execution_count':len(b.get('executions',[]))==(1 if decision in {'y','enter','details'} else 0),
        'exit_zero':child.returncode==0}
    result={'source_tree':manifest['source'],'replay_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'mode':mode,'framing':framing,'ending':ending,'decision':decision,'command':command,'exit':child.returncode,
        'checks':checks,'passed':all(checks.values()),'pending':snapshot,'business':b,'task':actual,'sent':sent}
    (root/'raw.pty').write_bytes(raw);(root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    return {k:result[k] for k in ('passed','checks','exit')}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('results');p.add_argument('--mode',default='plain');p.add_argument('--framing',default='bracketed');p.add_argument('--ending',default='lf');p.add_argument('--decision',default='y');a=p.parse_args()
    print(json.dumps(record(a.source,a.results,a.mode,a.framing,a.ending,a.decision)))
