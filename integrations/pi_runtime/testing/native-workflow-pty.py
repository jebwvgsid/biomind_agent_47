#!/usr/bin/env python3
"""Actual biomind/Pi PTY, scripted model, real native tools/Domain/Git.

Only small synthetic data and isolated Git configuration. No real installation,
network, credential reads, user session changes or production Git writes.
"""
import argparse, codecs, fcntl, hashlib, json, os, pty, re, select, shlex, signal, struct, subprocess, sys, tempfile, termios, time
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--evidence-dir',type=Path,required=True)
parser.add_argument('--default-profile',action='store_true')
args=parser.parse_args()
repo=Path(__file__).resolve().parents[3];rt=repo/'integrations/pi_runtime'
out=args.evidence_dir.resolve();out.mkdir(parents=True,exist_ok=False)
base=Path(tempfile.mkdtemp(prefix='biomind-native-product-'))
A=base/'A 项目';B=base/'B 中文 space';O=base/'O';W=base/'linked-worktree';G=base/'missing-identity'
for p in [A,B,O,G,*[base/n for n in ['home','agent','sessions','tmp','xdg-config','xdg-cache','xdg-data','xdg-state']]]:p.mkdir()
agent=base/'agent';(agent/'bin').mkdir()
for name in ['rg','fd']:(agent/'bin'/name).symlink_to(Path('/public/home/liusiqi/.pi/agent/bin')/name)
env={'HOME':str(base/'home'),'PATH':f'{agent}/bin:{rt}/.runtime/node/bin:/usr/bin:/bin',
     'PI_CODING_AGENT_DIR':str(agent),'TERM':'xterm-256color','LC_ALL':'C.UTF-8','LANG':'C.UTF-8',
     'BIOMIND_DISABLE_DOTENV':'1','BIOMIND_PI_PYTHON':sys.executable,'PYTHONDONTWRITEBYTECODE':'1',
     'NODE_OPTIONS':'--import='+str(rt/'testing/offline-guard.mjs'),'BIOMIND_PI_TEST_ROOT':str(base),
     'BIOMIND_PI_GUARD_TRACE':str(out/'guard.jsonl'),'BIOMIND_PI_UI_TRACE':str(out/'ui.jsonl'),'PI_OFFLINE':'1'}
# Observe child exit reasons without changing spawn, signals or tool behavior.
observer=base/'process-observer.mjs'
observer.write_text("import fs from 'node:fs';import cp from 'node:child_process';import {syncBuiltinESMExports} from 'node:module';\n"
    "const record=e=>fs.appendFileSync("+json.dumps(str(out/'processes.jsonl'))+",JSON.stringify({pid:process.pid,...e})+'\\n');\n"
    "const spawn=cp.spawn.bind(cp);cp.spawn=(...args)=>{const child=spawn(...args);child.once('exit',(code,signal)=>record({type:'child_exit',child:child.pid,code,signal}));return child;};syncBuiltinESMExports();\n"
    "process.on('exit',code=>record({type:'process_exit',code,maxRSS:process.resourceUsage().maxRSS}));\n")
env['NODE_OPTIONS']+=' --import='+str(observer)
for key,name in [('TMPDIR','tmp'),('XDG_CONFIG_HOME','xdg-config'),('XDG_CACHE_HOME','xdg-cache'),('XDG_DATA_HOME','xdg-data'),('XDG_STATE_HOME','xdg-state')]:env[key]=str(base/name)
def git(cwd,*command):
    return subprocess.check_output(['git','-C',str(cwd),*command],env=env,stderr=subprocess.STDOUT,text=True).strip()
identity={key:subprocess.check_output(['git','-C',str(repo),'config','--get',key],text=True).strip() for key in ['user.name','user.email']}
git(B,'init','-q');git(G,'init','-q')
for key,value in identity.items():git(B,'config',key,value)  # Fixture-local copy of actual configured identity.
(B/'source.txt').write_text('version-one\n');(B/'unrelated-staged').write_text('base\n');(B/'unrelated-unstaged').write_text('base\n')
git(B,'add','--','source.txt','unrelated-staged','unrelated-unstaged');git(B,'commit','-qm','synthetic fixture baseline')
initial_commit=git(B,'rev-parse','HEAD');git(B,'worktree','add','-q','-b','synthetic-linked',str(W))
(B/'unrelated-staged').write_text('staged-before\n');git(B,'add','--','unrelated-staged')
(B/'unrelated-unstaged').write_text('unstaged-before\n');(B/'unrelated-untracked').write_text('untracked-before\n')
staged_before=git(B,'diff','--cached','--binary');unstaged_before=git(B,'diff','--binary','--','unrelated-unstaged')
for name in ['one.vcf','two.fasta','three.gtf']:(B/name).write_text('synthetic-'+name+'\n')
(A/'external-link').symlink_to(B/'one.vcf')
(B/'.pi/extensions').mkdir(parents=True)
(B/'.pi/extensions/trap.ts').write_text("import fs from 'node:fs';export default ()=>fs.writeFileSync("+json.dumps(str(base/'EXTERNAL_EXTENSION_LOADED'))+",'bad');")
(agent/'settings.json').write_text(json.dumps({'enableAnalytics':False,'enableInstallTelemetry':False,'compaction':{'enabled':False},'cacheWarming':'off','retry':{'enabled':False}}))
(base/'registry.json').write_text('{"environments":{},"tools":{}}');(base/'preferences.md').write_text('合成验收；不做科学分析。')
def text(t):return {'content':[{'type':'text','text':t}]}
def tool(i,name,arguments):return {'content':[{'type':'toolCall','id':i,'name':name,'arguments':arguments}]}
q=lambda p:shlex.quote(str(p))
steps=[text('PTY_NATIVE_READY')]
steps += [tool(n,'read',{'path':str(B/n)}) for n in ['one.vcf','two.fasta','three.gtf']]
steps += [tool('edit','edit',{'path':str(B/'source.txt'),'edits':[{'oldText':'version-one','newText':'version-two'}]}),
    tool('write','write',{'path':str(O/'new/report.md'),'content':'native-report'}),
    tool('link','read',{'path':'external-link'}),
    tool('dynamic','bash',{'command':f'printf DYNAMIC_MARKER > {q(B/"late.index")}'}),
    tool('ls','ls',{'path':str(B)}),tool('find','find',{'path':str(B),'pattern':'*.index'}),
    tool('grep','grep',{'path':str(B),'pattern':'DYNAMIC_MARKER'}),
    tool('managed','run_command',{'workdir':str(B),'argv':['/usr/bin/python3','-I','-B','-c',f"from pathlib import Path;assert Path('source.txt').read_text()=='version-two\\n';Path({str(O/'managed.txt')!r}).write_text(Path('two.fasta').read_text());print('MANAGED_NATIVE_OK')"]}),
    {**tool('wait-managed','wait_run',{'run_ref':'$last','max_wait_ms':10000}),'resolve_last_run_ref':True},
    tool('git-main','bash',{'command':f'cd {q(B)} && test "$(cat source.txt)" = version-two && git add -- source.txt && git commit -m "synthetic native edit" --only -- source.txt && git rev-parse HEAD'}),
    tool('worktree-write','write',{'path':str(W/'linked.txt'),'content':'linked native write'}),
    tool('git-worktree','bash',{'command':f'cd {q(W)} && git rev-parse --git-dir --git-common-dir && git add -- linked.txt && git commit -m "synthetic native worktree" --only -- linked.txt && git rev-parse HEAD'}),
    tool('identity-file','write',{'path':str(G/'identity.txt'),'content':'fixture'}),
    tool('identity-failure','bash',{'command':f'cd {q(G)} && git add -- identity.txt && git -c user.useConfigOnly=true commit -m "must fail missing identity"'}),
    tool('hook-write','write',{'path':str(B/'.git/hooks/pre-commit'),'content':'#!/bin/sh\nexit 29\n'}),
    tool('hook-failure','bash',{'command':f'cd {q(B)} && chmod +x .git/hooks/pre-commit && git commit -m "must fail hook"'}),
    tool('signing-failure','bash',{'command':f'cd {q(G)} && git -c user.name={q(identity["user.name"])} -c user.email={q(identity["user.email"])} -c commit.gpgsign=true -c gpg.program=/synthetic-signing-program-unavailable commit -m "must fail signing"'}),
    text('PTY_NATIVE_DONE')]
steps[0]['trace_path']=str(out/'provider.jsonl');(base/'provider.json').write_text(json.dumps(steps,ensure_ascii=False))
config={'python':sys.executable,'agentDir':str(agent),'sessionDir':str(base/'sessions'),'dataRoot':str(base/'state'),
        'providerFixture':str(base/'provider.json'),'registryPath':str(base/'registry.json'),
        'historyEnabled':False,'memoryMode':'disabled'}
if not args.default_profile:config['executionProfile']='native'
(base/'config.json').write_text(json.dumps(config))
argv=[str(repo/'bin/biomind'),'--engine','pi','--project-dir',str(A),'--pi-config',str(base/'config.json'),'--prompt','只准备隔离合成验收']
master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',34,120,0,0))
process=subprocess.Popen(argv,cwd=A,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True,close_fds=True);os.close(slave)
raw=bytearray();decoder=codecs.getincrementaldecoder('utf-8')('replace');decoded='';events=[];failure=None;passed=False;checks={}
ansi=re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]')
def pump(seconds=.1):
    global decoded
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if select.select([master],[],[],.05)[0]:
            try:part=os.read(master,65536)
            except OSError:break
            if not part:break
            raw.extend(part);decoded+=decoder.decode(part)
def wait_for(predicate,label,timeout=35):
    end=time.monotonic()+timeout
    while not predicate():
        if process.poll() is not None:raise RuntimeError(f'exit {process.returncode} before {label}')
        if time.monotonic()>end:raise TimeoutError(label)
        pump()
    events.append({'seen':label,'offset':len(raw)})
def wait(marker):wait_for(lambda:marker in ansi.sub('',decoded),marker)
def send(value,label):os.write(master,value.encode());events.append({'input':label,'offset':len(raw)});pump(.25)
def ui():return [json.loads(s) for s in (out/'ui.jsonl').read_text().splitlines()] if (out/'ui.jsonl').exists() else []
def shell_receipts():
    rows=[]
    for filename in (base/'sessions').rglob('*.jsonl'):
        for line in filename.read_text().splitlines():
            try:entry=json.loads(line)
            except ValueError:continue
            if entry.get('type')=='message' and entry.get('message',{}).get('role')=='bashExecution':rows.append(entry['message'])
    return rows
try:
    wait('PTY_NATIVE_READY');wait('execution:native')
    send('!printf PTY_VISIBLE_SHELL_MARKER\r','native ! shortcut')
    wait_for(lambda:any('PTY_VISIBLE_SHELL_MARKER' in r.get('output','') and r.get('exitCode')==0 for r in shell_receipts()),'native ! terminal receipt')
    send('!!printf PTY_HIDDEN_SHELL_MARKER\r','native !! shortcut')
    wait_for(lambda:any('PTY_HIDDEN_SHELL_MARKER' in r.get('output','') and r.get('exitCode')==0 and r.get('excludeFromContext') for r in shell_receipts()),'native !! terminal receipt')
    send('执行已列明的合成读改、命令与本地 Git 验收\r','explicit synthetic task')
    wait('PTY_NATIVE_DONE');pump(.5)
    traces=[json.loads(line) for line in (out/'provider.jsonl').read_text().splitlines()]
    receipts={m['toolCallId']:m for row in traces for m in row['context']['messages'] if m.get('role')=='toolResult'}
    wire=json.dumps(traces,ensure_ascii=False)
    assert 'PTY_VISIBLE_SHELL_MARKER' in wire and 'PTY_HIDDEN_SHELL_MARKER' not in wire
    assert '开发修改默认按验证通过的逻辑单元创建本地 Git commit' in wire
    assert '项目外数据通过 file_stat/read/write/edit/run_command 自动申请' not in wire
    failures={'identity-failure','hook-failure','signing-failure'}
    expected={part['id'] for step in steps for part in step['content'] if part['type']=='toolCall'}
    assert set(receipts)==expected,(set(receipts),expected)
    for name,receipt in receipts.items():assert bool(receipt.get('isError'))==(name in failures),(name,receipt)
    assert (B/'source.txt').read_text()=='version-two\n';assert (O/'new/report.md').read_text()=='native-report'
    assert (O/'managed.txt').read_text()=='synthetic-two.fasta\n'
    assert git(B,'diff','--cached','--binary')==staged_before
    assert git(B,'diff','--binary','--','unrelated-unstaged')==unstaged_before
    assert (B/'unrelated-untracked').read_text()=='untracked-before\n'
    assert git(B,'show','--pretty=format:','--name-only','HEAD')=='source.txt'
    assert git(W,'show','--pretty=format:','--name-only','HEAD')=='linked.txt'
    assert not (base/'EXTERNAL_EXTENSION_LOADED').exists()
    checks.update(main_commit=git(B,'rev-parse','HEAD'),worktree_commit=git(W,'rev-parse','HEAD'),
        initial_commit=initial_commit,worktree_git_dir=git(W,'rev-parse','--git-dir'),worktree_common_dir=git(W,'rev-parse','--git-common-dir'),
        unrelated_staged_unchanged=True,unrelated_unstaged_unchanged=True,untracked_unchanged=True,
        actual_tool_results=len(receipts),expected_git_failures=sorted(failures),external_extension_loaded=False,
        native_shell_shortcuts=True,hidden_shell_not_in_model_context=True,effective_git_preferences=True)
    # Actual Pi lifecycle, no replacement session manager.
    before=[e for e in ui() if e['type']=='session_start'];send('/new\r','native /new')
    wait_for(lambda:len([e for e in ui() if e['type']=='session_start'])>len(before),'native new session')
    after=[e for e in ui() if e['type']=='session_start'];assert after[-1]['reason']=='new'
    assert after[-1]['sessionId']!=before[-1]['sessionId'];assert 'bash' in after[-1]['tools']
    assert not [e for e in ui() if e['type']=='ui_request' and e['request']['kind']=='approval']
    assert not [e for e in ui() if e['type']=='ui_prompt_start'],ui()
    states=[json.loads(p.read_text()) for p in (base/'state/task_outputs').glob('*/plan_state.json')]
    assert len(states)==2
    assert all(s['task_state']['pi_execution_profile']['profile']=='native' for s in states)
    assert all(not s['task_state'].get('execution_permissions',{}).get('requests') for s in states)
    checks.update(permission_ui_calls=0,blocking_ui_calls=0,ui_counter_source='Pi ui_prompt_start plus BioMind ui_request',
        permission_requests=0,pending=0,native_new_identity=True,ledger_profiles=['native','native'])
    passed=True
except Exception as error:failure=repr(error)
finally:
    send('/quit\r','quit fixture only');pump(2)
    if process.poll() is None:process.terminate()
    try:process.wait(timeout=4)
    except subprocess.TimeoutExpired:process.kill();process.wait()
    os.close(master)
    (out/'terminal.ansi').write_bytes(raw);(out/'terminal.txt').write_text(ansi.sub('',decoded))
    artifacts=['bin/biomind','agent_core/pi_entry.py','agent_core/native_command.py','execution/executor.py',
        'integrations/pi_runtime/dist/host.js','integrations/pi_runtime/dist/extensions/biomind.js','integrations/pi_runtime/dist/domain-client.js',
        'integrations/pi_runtime/domain_adapter/service.py','integrations/pi_runtime/dist/testing/native-fixture.js']
    result={'passed':passed,'failure':failure,'base':str(base),'argv':argv,'default_profile':args.default_profile,'checks':checks,
        'events':events,'exit_code':process.returncode,'loaded_artifacts':{p:hashlib.sha256((repo/p).read_bytes()).hexdigest() for p in artifacts}}
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False));sys.exit(0 if passed else 1)
