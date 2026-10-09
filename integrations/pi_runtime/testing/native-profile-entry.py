#!/usr/bin/env python3
"""Actual biomind entry: legacy binding, explicit migration and protected rollback.

Scripted provider, synthetic persisted Pi session and original Domain ledger.
No user sessions, installs, paid requests or external network.
"""
import argparse, asyncio, hashlib, json, os, select, subprocess, sys, tempfile, time
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('--evidence-dir',type=Path,required=True)
out=parser.parse_args().evidence_dir.resolve();out.mkdir(parents=True,exist_ok=False)
repo=Path(__file__).resolve().parents[3];rt=repo/'integrations/pi_runtime'
base=Path(tempfile.mkdtemp(prefix='biomind-profile-entry-'));A=base/'A';B=base/'B'
for p in [A,B,base/'home',base/'agent',base/'sessions']:p.mkdir()
registry=base/'registry.json';registry.write_text('{"environments":{},"tools":{}}')
prefs=base/'prefs.md';prefs.write_text('Synthetic profile lifecycle test.')
fixture=base/'provider.json';fixture.write_text(json.dumps([{'trace_path':str(out/'provider.jsonl'),'content':[{'type':'text','text':'PROFILE_SCRIPTED_RESULT'}]}]))
config=base/'config.json';config.write_text(json.dumps({'python':sys.executable,'agentDir':str(base/'agent'),
    'sessionDir':str(base/'sessions'),'dataRoot':str(base/'state'),'providerFixture':str(fixture),
    'registryPath':str(registry),'preferencesPath':str(prefs),'historyEnabled':False,'memoryMode':'disabled'}))
env={'HOME':str(base/'home'),'PATH':f'{rt}/.runtime/node/bin:/usr/bin:/bin','LC_ALL':'C.UTF-8',
    'BIOMIND_PI_PYTHON':sys.executable,'BIOMIND_DISABLE_DOTENV':'1','PYTHONDONTWRITEBYTECODE':'1','PI_OFFLINE':'1',
    'NODE_OPTIONS':'--import='+str(rt/'testing/offline-guard.mjs'),'BIOMIND_PI_TEST_ROOT':str(base),
    'BIOMIND_PI_GUARD_TRACE':str(out/'guard.jsonl')}
active=[];observed=[]
class Host:
    def __init__(self,name,*flags,extra_env=None):
        self.name=name;self.frames=[];self.pending=b'';self.closed=False
        self.err=(out/(name+'.stderr')).open('wb')
        self.argv=[str(repo/'bin/biomind'),'--pi-mode','json','--project-dir',str(A),'--pi-config',str(config),*map(str,flags)]
        self.process=subprocess.Popen(self.argv,cwd=A,env={**env,**(extra_env or {})},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.err)
        active.append(self)
    def pump(self,seconds=.05):
        if select.select([self.process.stdout],[],[],seconds)[0]:
            self.pending+=os.read(self.process.stdout.fileno(),65536)
            while b'\n' in self.pending:
                line,self.pending=self.pending.split(b'\n',1)
                if line:self.frames.append(json.loads(line))
    def wait(self,predicate,timeout=20):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            found=next((f for f in self.frames if predicate(f)),None)
            if found is not None:return found
            self.pump()
            if self.process.poll() is not None:
                self.pump(0)
                found=next((f for f in self.frames if predicate(f)),None)
                if found is not None:return found
                raise AssertionError((self.name,self.process.returncode,self.frames[-3:]))
        raise TimeoutError(self.name)
    def ready(self,profile,source=None):
        r=self.wait(lambda f:f['type']=='ready');assert r['execution_profile']==profile,r
        if source:assert r['profile_source']==source,r
        assert len(r['extension_sha256'])==64
        return r
    def prompt(self,request,text):
        self.process.stdin.write((json.dumps({'type':'prompt','requestId':request,'text':text})+'\n').encode());self.process.stdin.flush()
        return self.wait(lambda f:f['type']=='result' and f.get('requestId')==request)
    def close(self):
        if self.closed:return
        self.closed=True
        self.process.stdin.close()
        end=time.monotonic()+5
        while self.process.poll() is None and time.monotonic()<end:self.pump()
        if self.process.poll() is None:self.process.terminate();self.process.wait(3)
        self.pump(0);self.err.close()
        (out/(self.name+'.json')).write_text(json.dumps(self.frames,ensure_ascii=False,indent=2))
        observed.append({'name':self.name,'argv':self.argv,'exit_code':self.process.returncode,
            'ready':[f for f in self.frames if f['type']=='ready'],
            'diagnostics':[f for f in self.frames if f['type']=='diagnostic'],
            'approval_ui_calls':sum(f['type']=='approval' for f in self.frames)})

checks={};failure=None
try:
    h=Host('protected-original','--execution-profile','protected');ready=h.ready('protected','cli')
    result=h.prompt('original','开始合成旧会话');assert result['runtime_status']=='completed',result
    session=Path(result['sessionFile']);session_id=ready['sessionId'];h.close();assert h.process.returncode==0
    sys.path[:0]=[str(repo),str(rt)]
    from domain_adapter.service import DomainService
    from agent_core.pending_input import mutate
    async def seed():
        s=DomainService(cwd=A,data_root=base/'state',install_root=repo,session_id=session_id,
            registry_path=registry,preferences_path=prefs,execution_profile='protected',session_existing=True)
        states={}
        for name,choice in [('denied','n'),('cancelled','cancel'),('pending',None)]:
            target=B/name;target.write_text('synthetic input')
            r=await s.call('files.read_authorize',{'path':str(target),'call_id':'old-'+name},host_token=s.host_token)
            a=r['approval'];states[name]=a['request_id']
            if choice:
                await s.call('approval.decide',{**{k:a[k] for k in ['request_id','action_hash','request_scope']},'choice':choice},host_token=s.host_token)
                await s.call('request.resume',{},host_token=s.host_token)
        await s.disconnected()
        # Synthetic legacy fixture only: remove the new field, keeping original Pi history and ledger.
        mutate(s.data_root,s.task_id,lambda state:state['task_state'].pop('pi_execution_profile'))
        return s.data_root/'task_outputs'/s.task_id/'plan_state.json',states
    state_file,old_rows=asyncio.run(seed())
    state=lambda:json.loads(state_file.read_text())['task_state']
    history_before=session.read_bytes();book_before=state()['execution_permissions']
    legacy=Host('legacy-default','--session',session);legacy.ready('protected','session-binding')
    checks['legacy_missing_profile_stays_protected']=True
    # An explicit mismatch never gets as far as a usable session.
    mismatch=Host('mismatch','--session',session,'--execution-profile','native')
    mismatch.wait(lambda f:f['type']=='error');mismatch.close()
    assert mismatch.process.returncode==2 and not any(f['type']=='ready' for f in mismatch.frames)
    assert 'EXECUTION_PROFILE_RESTART_REQUIRED' in (out/'mismatch.stderr').read_text()
    checks['mismatch_requires_new_process_migration']=True
    legacy.close()
    assert session.read_bytes()==history_before and state()['execution_permissions']==book_before
    migrated=Host('migrated-native','--session',session,'--execution-profile','native','--migrate-execution-profile')
    migrated.ready('native','cli')
    before_count=len((out/'provider.jsonl').read_text().splitlines())
    refused=migrated.prompt('old-continue','继续当前任务')
    assert refused['runtime_status']=='error',refused
    assert len((out/'provider.jsonl').read_text().splitlines())==before_count
    rows=state()['execution_permissions']['requests']
    assert {k:rows[v]['status'] for k,v in old_rows.items()}=={'pending':'cancelled','denied':'denied','cancelled':'cancelled'}
    assert not list((base/'state').rglob('observation_intent.json'))
    checks['old_rows_not_replayed']=True;checks['continue_does_not_implicitly_restart']=True
    # Check the real live Pi host via the same Domain constructor, without
    # retaining two Node runtimes in a resource-constrained test environment.
    try:
        DomainService(cwd=A,data_root=base/'state',install_root=repo,session_id=session_id,
            registry_path=registry,preferences_path=prefs,execution_profile='protected',migrate_profile=True)
        raise AssertionError('live host was migrated')
    except ValueError as error:assert str(error)=='EXECUTION_PROFILE_MIGRATION_BUSY',error
    assert migrated.process.poll() is None and state()['pi_execution_profile']['profile']=='native'
    checks['live_host_migration_blocked']=True
    fresh=migrated.prompt('explicit-new','[新任务] 明确开始新的合成任务');assert fresh['runtime_status']=='completed',fresh
    assert not state().get('pi_profile_requires_new_request');assert fresh['sessionId']==session_id
    migrated.close()
    # A damaged owner binding must fail initialization without a false ready.
    saved_state=state_file.read_bytes();damaged=json.loads(saved_state)
    damaged['task_state']['pi_execution_profile']['project']=str(B)
    state_file.write_text(json.dumps(damaged))
    try:
        invalid=Host('binding-failure','--session',session)
        bad=invalid.wait(lambda f:f.get('code')=='EXECUTION_PROFILE_BINDING_FAILED');invalid.close()
        assert bad.get('reason')=='EXECUTION_PROFILE_OWNER_MISMATCH',bad
        assert invalid.process.returncode==2 and not any(f['type']=='ready' for f in invalid.frames)
        checks['failed_binding_never_reports_ready']=True
    finally:state_file.write_bytes(saved_state)
    # Roll back by host configuration and explicit idle migration, not reset/delete.
    rollback=Host('rollback-protected','--session',session,'--execution-profile','protected','--migrate-execution-profile')
    rollback.ready('protected','cli');rollback.close()
    assert state()['pi_execution_profile']['profile']=='protected'
    assert all(state()['execution_permissions']['requests'][v]['status']==('cancelled' if k=='pending' else k) for k,v in old_rows.items())
    assert session.read_bytes().startswith(history_before)
    checks['protected_rollback_preserves_history_and_decisions']=True
    forced=Host('forced-protection','--execution-profile','native',extra_env={'BIOMIND_PI_FORCE_EXECUTION_PROFILE':'protected'})
    forced.wait(lambda f:f['type']=='error');forced.close()
    assert forced.process.returncode==2 and 'EXECUTION_PROFILE_FORCED_POLICY_CONFLICT' in (out/'forced-protection.stderr').read_text()
    checks['deployment_force_preserved']=True
    checks['all_approval_ui_calls']=sum(v['approval_ui_calls'] for v in observed)
    assert checks['all_approval_ui_calls']==0
except Exception as error:failure=repr(error)
finally:
    for h in active:h.close()
    result={'passed':failure is None,'failure':failure,'base':str(base),'checks':checks,'processes':observed,
        'extension_sha256':hashlib.sha256((rt/'dist/extensions/biomind.js').read_bytes()).hexdigest()}
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
    sys.exit(0 if failure is None else 1)
