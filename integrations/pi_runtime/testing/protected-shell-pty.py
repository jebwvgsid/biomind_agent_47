#!/usr/bin/env python3
"""Actual protected Pi TUI: !/!! disabled; external read opens a real cancellable dialog."""
import argparse,fcntl,json,os,pty,re,select,struct,subprocess,sys,tempfile,termios,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--evidence-dir',type=Path,required=True)
out=p.parse_args().evidence_dir.resolve();out.mkdir(parents=True,exist_ok=False)
repo=Path(__file__).resolve().parents[3];rt=repo/'integrations/pi_runtime'
base=Path(tempfile.mkdtemp(prefix='biomind-protected-ui-'));A=base/'A';B=base/'B'
for d in [A,B,base/'home',base/'agent',base/'sessions']:d.mkdir()
(B/'input').write_text('synthetic protected input')
(base/'registry.json').write_text('{"environments":{},"tools":{}}');(base/'prefs.md').write_text('synthetic')
(base/'agent/settings.json').write_text(json.dumps({'enableAnalytics':False,'enableInstallTelemetry':False,'cacheWarming':'off','compaction':{'enabled':False}}))
steps=[{'content':[{'type':'text','text':'PROTECTED_READY'}],'trace_path':str(out/'provider.jsonl')},
    {'content':[{'type':'toolCall','id':'external-stat','name':'file_stat','arguments':{'path':str(B/'input')}}]},
    {'content':[{'type':'text','text':'MUST_NOT_CONTINUE_AFTER_CANCEL'}]}]
(base/'provider.json').write_text(json.dumps(steps))
(base/'config.json').write_text(json.dumps({'executionProfile':'protected','python':sys.executable,
    'agentDir':str(base/'agent'),'sessionDir':str(base/'sessions'),'dataRoot':str(base/'state'),
    'providerFixture':str(base/'provider.json'),'registryPath':str(base/'registry.json'),'preferencesPath':str(base/'prefs.md'),
    'historyEnabled':False,'memoryMode':'disabled'}))
env={'HOME':str(base/'home'),'PATH':f'{rt}/.runtime/node/bin:/usr/bin:/bin','TERM':'xterm-256color','LC_ALL':'C.UTF-8',
    'BIOMIND_PI_PYTHON':sys.executable,'BIOMIND_DISABLE_DOTENV':'1','PYTHONDONTWRITEBYTECODE':'1','PI_OFFLINE':'1',
    'NODE_OPTIONS':'--import='+str(rt/'testing/offline-guard.mjs'),'BIOMIND_PI_TEST_ROOT':str(base),
    'BIOMIND_PI_GUARD_TRACE':str(out/'guard.jsonl'),'BIOMIND_PI_UI_TRACE':str(out/'ui.jsonl')}
master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',34,120,0,0))
argv=[str(repo/'bin/biomind'),'--project-dir',str(A),'--pi-config',str(base/'config.json'),'--prompt','准备合成保护验收']
child=subprocess.Popen(argv,cwd=A,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave)
raw=bytearray();failure=None;checks={}
def pump():
    if select.select([master],[],[],.05)[0]:
        try:raw.extend(os.read(master,65536))
        except OSError:pass
def wait(predicate,label):
    end=time.monotonic()+30
    while not predicate():
        if child.poll() is not None:raise RuntimeError((label,child.returncode))
        if time.monotonic()>end:raise TimeoutError(label)
        pump()
def send(text):os.write(master,text.encode())
def rows(file):
    if not file.exists():return []
    result=[]
    for line in file.read_text().splitlines():
        try:result.append(json.loads(line))
        except ValueError:pass
    return result
def shells():return [r['message'] for f in (base/'sessions').glob('*.jsonl') for r in rows(f) if r.get('type')=='message' and r.get('message',{}).get('role')=='bashExecution']
def state():
    files=list((base/'state/task_outputs').glob('*/plan_state.json'))
    return json.loads(files[0].read_text())['task_state'] if files else {}
try:
    wait(lambda:b'PROTECTED_READY' in raw,'ready')
    send('!printf escape > escaped-one\r');wait(lambda:len(shells())>=1,'! receipt')
    send('!!printf escape > escaped-two\r');wait(lambda:len(shells())>=2,'!! receipt')
    receipts=shells();assert all(r['exitCode']==1 and 'protected' in r['output'] for r in receipts)
    assert not (A/'escaped-one').exists() and not (A/'escaped-two').exists()
    send('检查合成项目外输入\r')
    wait(lambda:any(r['type']=='ui_prompt_start' for r in rows(out/'ui.jsonl')),'actual protected prompt')
    send('\x1b')
    wait(lambda:state().get('pi_pause',{}).get('reason')=='approval_cancelled','cancelled ledger')
    book=state()['execution_permissions'];requests=list(book['requests'].values())
    assert len(requests)==1 and requests[0]['status']=='cancelled' and not book['grants']
    assert (B/'input').read_text()=='synthetic protected input'
    prompts=[r for r in rows(out/'ui.jsonl') if r['type']=='ui_prompt_start'];assert len(prompts)==1,prompts
    assert len(rows(out/'provider.jsonl'))==2
    inventory=next(r['tools'] for r in rows(out/'ui.jsonl') if r['type']=='session_start')
    assert 'bash' not in inventory
    checks={'shell_shortcuts_disabled':True,'no_shell_effect':True,'native_bash_not_registered':True,
        'blocking_ui_calls':1,'permission_requests':1,'pending':0,'cancelled':1,'grants':0,'provider_calls':2,'input_unchanged':True}
    (out/'ledger.json').write_text(json.dumps({'profile':state()['pi_execution_profile']['profile'],'pause':state()['pi_pause'],'permissions':book},ensure_ascii=False,indent=2))
except Exception as error:failure=repr(error)
finally:
    send('/quit\r');end=time.monotonic()+3
    while child.poll() is None and time.monotonic()<end:pump()
    if child.poll() is None:child.terminate();child.wait(3)
    os.close(master);(out/'terminal.ansi').write_bytes(raw)
    result={'passed':failure is None,'failure':failure,'base':str(base),'argv':argv,'checks':checks,'exit_code':child.returncode}
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
    sys.exit(0 if failure is None else 1)
