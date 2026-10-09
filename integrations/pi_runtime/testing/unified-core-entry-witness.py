#!/usr/bin/env python3
"""Actual product entry + real Domain, using only isolated scripted providers."""
import argparse, fcntl, hashlib, json, os, pty, re, select, struct, subprocess, sys, tempfile, termios, time
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--node',type=Path,required=True)
    parser.add_argument('--expected-core-marker')
    parser.add_argument('--repo-root',type=Path)
    args=parser.parse_args()
    repo=(args.repo_root or Path(__file__).resolve().parents[3]).resolve();runtime=repo/'integrations/pi_runtime'
    out=args.evidence_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    base=Path(tempfile.mkdtemp(prefix='biomind-unified-entry-'))
    for name in ('A','B','C','agent','home','sessions','tmp'): (base/name).mkdir()
    registry=base/'registry.json';registry.write_text('{"environments":{},"tools":{}}')
    prefs=base/'preferences.md';prefs.write_text('Synthetic isolated acceptance only.\n')
    extensions=base/'agent/extensions';extensions.mkdir()
    (extensions/'fixture.ts').write_text("export default function(pi){pi.registerTool({name:'fixture_echo',label:'Fixture',description:'isolated no-I/O tool',parameters:{type:'object',properties:{},additionalProperties:false},async execute(){return {content:[{type:'text',text:'PLUGIN_REAL_REGISTERED_TOOL'}],details:{synthetic:true}}}});pi.registerCommand('fixture_command',{description:'isolated no-I/O command',handler:async()=>{}});}\n")
    (base/'agent/settings.json').write_text(json.dumps({'enableAnalytics':False,'enableInstallTelemetry':False,'cacheWarming':'off','compaction':{'enabled':False},'retry':{'enabled':False}}))
    env={'HOME':str(base/'home'),'PATH':str(args.node.parent)+':/usr/bin:/bin','TERM':'xterm-256color','LC_ALL':'C.UTF-8','LANG':'C.UTF-8',
         'TMPDIR':str(base/'tmp'),'BIOMIND_DISABLE_DOTENV':'1','PYTHONDONTWRITEBYTECODE':'1','PI_OFFLINE':'1','BIOMIND_PI_PYTHON':sys.executable,
         'NODE_OPTIONS':'--import='+str(runtime/'testing/offline-guard.mjs'),'BIOMIND_PI_TEST_ROOT':str(base),'BIOMIND_PI_GUARD_TRACE':str(out/'guard.jsonl')}
    def configuration(label):
        fixture=base/(label+'-provider.json')
        fixture.write_text(json.dumps([
            {'trace_path':str(out/(label+'-provider.jsonl')),'content':[{'type':'toolCall','id':'plugin','name':'fixture_echo','arguments':{}}]},
            {'content':[{'type':'toolCall','id':'managed','name':'run_command','arguments':{'workdir':str(base/label),'argv':['/usr/bin/python3','-I','-B','-c',"from pathlib import Path;Path('artifact.txt').write_text('UNIFIED_REAL_DOMAIN_ARTIFACT\\n');print('MANAGED_OK')"]}}]},
            {'resolve_last_run_ref':True,'content':[{'type':'toolCall','id':'observe','name':'wait_run','arguments':{'run_ref':'$last','max_wait_ms':10000}}]},
            {'content':[{'type':'text','text':'UNIFIED_NATIVE_DONE_'+label}]}]))
        config=base/(label+'-config.json');config.write_text(json.dumps({'python':sys.executable,'agentDir':str(base/'agent'),'sessionDir':str(base/'sessions'),
            'dataRoot':str(base/'domain'),'installRoot':str(repo),'executionProfile':'native','providerFixture':str(fixture),
            'registryPath':str(registry),'preferencesPath':str(prefs),'historyEnabled':False,'memoryMode':'disabled'}))
        return config
    def command(label,mode='json',session=None):
        argv=[sys.executable,'-B',str(repo/'bio_agents.py'),'--pi-node',str(args.node),'--project-dir',str(base/label),'--pi-mode',mode,'--pi-config',str(configuration(label))]
        if session:argv+=['--session',str(session)]
        return argv
    active=[];runs=[];checks={};failure=None
    class Host:
        def __init__(self,label,session=None):
            self.label=label;self.frames=[];self.pending=b'';self.closed=False
            self.err=(out/(label+('-resume' if session else '')+'.stderr')).open('wb')
            self.argv=command(label,session=session);self.process=subprocess.Popen(self.argv,cwd=base/label,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.err)
            active.append(self)
        def pump(self,delay=.05):
            if select.select([self.process.stdout],[],[],delay)[0]:
                self.pending+=os.read(self.process.stdout.fileno(),65536)
                while b'\n' in self.pending:
                    line,self.pending=self.pending.split(b'\n',1)
                    if line:self.frames.append(json.loads(line))
        def wait(self,predicate,timeout=90):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                row=next((f for f in self.frames if predicate(f)),None)
                if row is not None:return row
                self.pump()
                if self.process.poll() is not None:
                    self.pump(0);raise AssertionError({'phase':self.label,'exit':self.process.returncode,'frames':self.frames[-3:]})
            raise TimeoutError(self.label)
        def send(self,frame):
            if frame.get('clientMessageId'):
                ready=next(f for f in self.frames if f.get('type')=='ready')
                frame={**frame,'ownerGeneration':ready['ownerGeneration'],'sessionId':ready['sessionId'],'project':ready['cwd']}
            self.process.stdin.write((json.dumps(frame)+'\n').encode());self.process.stdin.flush()
        def close(self):
            if self.closed:return
            self.closed=True
            if self.process.poll() is None:
                try:self.send({'type':'shutdown','id':'explicit-shutdown'});self.process.stdin.close()
                except (BrokenPipeError,OSError):pass
            deadline=time.monotonic()+30
            while self.process.poll() is None and time.monotonic()<deadline:self.pump()
            if self.process.poll() is None:self.process.terminate();self.process.wait(5)
            self.pump(0);self.err.close()
            filename=out/(self.label+'-'+str(len(runs))+'-frames.json');filename.write_text(json.dumps(self.frames,ensure_ascii=False,indent=2))
            runs.append({'label':self.label,'argv':self.argv,'exitCode':self.process.returncode,'frames':str(filename)})
    try:
        a=Host('A');ready=a.wait(lambda f:f.get('type')=='ready')
        assert Path(ready['loaded_build']['releaseRoot']).resolve()==repo.resolve()
        assert ready['resourceOwnership']['protocol']=='linux_flock_v1'
        a.send({'type':'get_tools','id':'tools'});tools=a.wait(lambda f:f.get('id')=='tools')
        assert 'fixture_echo' in tools['data']['allowed']
        a.send({'type':'get_commands','id':'commands'});commands=a.wait(lambda f:f.get('id')=='commands')
        assert any(c['name']=='fixture_command' for c in commands['data']['commands'])
        a.send({'type':'prompt','id':'A-task','clientMessageId':'A-one','text':'Explicit isolated synthetic task.'})
        result=a.wait(lambda f:f.get('type')=='result')
        assert result['runtime_status']=='completed',result
        assert (base/'A/artifact.txt').read_text()=='UNIFIED_REAL_DOMAIN_ARTIFACT\n'
        ends=[f['event'] for f in a.frames if f.get('type')=='event' and f.get('event',{}).get('type')=='tool_execution_end']
        assert {'fixture_echo','run_command','wait_run'} <= {e['toolName'] for e in ends}
        assert all(not e.get('isError') for e in ends),ends
        session_a=result['sessionFile'];id_a=ready['sessionId'];checks['jsonEntryFullBioMindDomainAndPlugin']=True
        a.send({'type':'set_tools','id':'tools-empty','tools':[]});assert a.wait(lambda f:f.get('id')=='tools-empty')['data']['effective']==[]
        a.send({'type':'prepare_packages_shutdown','id':'prepare'});assert a.wait(lambda f:f.get('id')=='prepare')['packagesIdle'] is True
        a.send({'type':'prompt','id':'must-not-run','text':'must never execute'});assert a.wait(lambda f:f.get('id')=='must-not-run')['code']=='PACKAGES_OWNER_QUIESCING'
        a.send({'type':'resume_packages_operations','id':'cancel'});assert a.wait(lambda f:f.get('id')=='cancel')['type']=='packages_shutdown_cancelled'
        checks['idlePreparationReversibleWithoutTaskReplay']=True
        a.close();assert a.process.returncode==0
        b=Host('B');ready_b=b.wait(lambda f:f.get('type')=='ready');assert ready_b['sessionId']!=id_a and ready_b['cwd']==str(base/'B')
        b.send({'type':'prompt','id':'B-task','clientMessageId':'B-one','text':'Explicit isolated second project task.'})
        result_b=b.wait(lambda f:f.get('type')=='result');assert result_b['runtime_status']=='completed'
        assert result_b['sessionFile']!=session_a and (base/'B/artifact.txt').read_text()=='UNIFIED_REAL_DOMAIN_ARTIFACT\n'
        b.close();assert b.process.returncode==0;checks['distinctProjectsAndOwners']=True
        before=(out/'A-provider.jsonl').read_bytes()
        resumed=Host('A',session_a);again=resumed.wait(lambda f:f.get('type')=='ready')
        assert again['sessionId']==id_a and again['sessionFile']==session_a
        resumed.send({'type':'get_tools','id':'restored-tools'});assert resumed.wait(lambda f:f.get('id')=='restored-tools')['data']['effective']==[]
        assert (out/'A-provider.jsonl').read_bytes()==before
        resumed.close();assert resumed.process.returncode==0;checks['exactNativeResumeNoReplayAndEmptyTools']=True
        master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',34,120,0,0))
        native_trace_start=len((out/'A-provider.jsonl').read_text().splitlines())
        same_config_hash=hashlib.sha256((base/'A-config.json').read_bytes()).hexdigest()
        native_argv=command('A','interactive')+['--prompt','Explicit isolated native CLI task.']
        assert hashlib.sha256((base/'A-config.json').read_bytes()).hexdigest()==same_config_hash
        native=subprocess.Popen(native_argv,cwd=base/'A',env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave)
        raw=bytearray();deadline=time.monotonic()+90
        try:
            while time.monotonic()<deadline and native.poll() is None:
                if select.select([master],[],[],.1)[0]:
                    try:raw.extend(os.read(master,65536))
                    except OSError:break
                if b'UNIFIED_NATIVE_DONE_A' in raw:break
            assert b'UNIFIED_NATIVE_DONE_A' in raw,{'nativeExit':native.poll(),'tail':raw[-1500:].decode(errors='replace')}
            assert (base/'A/artifact.txt').read_text()=='UNIFIED_REAL_DOMAIN_ARTIFACT\n'
            os.write(master,b'\x04')
            deadline=time.monotonic()+15
            while native.poll() is None and time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try:raw.extend(os.read(master,65536))
                    except OSError:break
            if native.poll() is None:os.write(master,b'\x03\x03')
            native.wait(10);assert native.returncode==0
            checks['nativeCliSameBuildRealDomainAndPlugin']=True
            checks['nativeCliSameProjectAndConfiguration']=True
        finally:
            if native.poll() is None:native.terminate();native.wait(5)
            os.close(master);(out/'native-pty.log').write_bytes(raw)
            runs.append({'label':'native-A','argv':native_argv,'exitCode':native.returncode})
        if args.expected_core_marker:
            requests=[json.loads(line) for line in (out/'A-provider.jsonl').read_text().splitlines()]
            for segment in (requests[:native_trace_start],requests[native_trace_start:]):
                assert any(args.expected_core_marker in json.dumps([m for m in row['context']['messages'] if m.get('role')=='system'],ensure_ascii=False) for row in segment)
            checks['authoritativeCoreMarkerBothEntrypoints']=True
        guard=[json.loads(line) for line in (out/'guard.jsonl').read_text().splitlines()]
        assert not any(r.get('allowed') is False for r in guard),guard
        checks['noExternalNetworkOrCredentialRead']=True
    except Exception as error:failure={'type':type(error).__name__,'detail':str(error)}
    finally:
        for host in active:host.close()
        receipt={'schema':'BioMindUnifiedEntryWitnessV1','result':'PASS' if failure is None else 'FAIL','repo':str(repo),'fixtureRoot':str(base),'checks':checks,'runs':runs,'failure':failure,
                 'realDomain':True,'realProductEntry':True,'provider':'scripted; no external model','testExtension':'isolated temporary agent only; no production defaults','scriptSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        (out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'result':receipt['result'],'checks':checks,'failure':failure},ensure_ascii=False))
    return 0 if failure is None else 1

if __name__=='__main__':sys.exit(main())
