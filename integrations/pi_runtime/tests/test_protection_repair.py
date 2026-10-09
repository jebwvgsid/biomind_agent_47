"""Real DomainService/executor/bwrap regression fixtures; no protection mocks.

Synthetic host decisions only. Known concurrent-host failure remains a visible
expectedFailure, not a security PASS. Optional source root loads an isolated
candidate checkout; it is a test runner choice, never model authority.
"""
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode=True
RUNTIME=Path(__file__).resolve().parents[1]; REPO=RUNTIME.parents[1]
sys.path[:0]=[os.environ.get('BIOMIND_REPAIR_SOURCE_ROOT',str(REPO)),str(RUNTIME),str(REPO)]
from domain_adapter.service import DomainService
from agent_core import command_execution as ce

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def command(code):return {'argv':['/usr/bin/python3','-I','-B','-c',code]}


class Fixture:
    def __init__(self,root,name,layout='nested'):
        self.root=root/name;self.root.mkdir(parents=True)
        self.project=self.root/'project';self.project.mkdir()
        self.runtime=(self.project/'runtime' if layout=='nested' else self.project if layout=='equal'
                      else self.root if layout=='ancestor' else self.root/'runtime')
        self.runtime.mkdir(exist_ok=True)
        self.source=self.root/'source.txt';self.source.write_text('source-original')
        self.runtime_file=self.runtime/'runtime.txt';self.runtime_file.write_text('runtime-original')
        registry=self.root/'registry.json';registry.write_text('{"environments":{},"tools":{}}')
        prefs=self.root/'preferences.md';prefs.write_text('fixture')
        skills=self.root/'skills';skills.mkdir()
        self.service=DomainService(cwd=self.project,data_root=self.root/'state',install_root=self.runtime,
            session_id=name,registry_path=registry,preferences_path=prefs,skills_root=skills)
        self.service.commands.install_dir=self.runtime
        self.before={str(p):digest(p) for p in (self.source,self.runtime_file)}
        self.events=[];self.index=0
    async def begin(self):
        await self.service.call('request.begin',{'request_id':'fixture','role':'generate'},host_token=self.service.host_token)
    async def run(self,args,*,call_id=None,choice='y'):
        self.index+=1;call_id=call_id or f'call-{self.index}'
        params={'call_id':call_id,'arguments':args}
        result=await self.service.call('exec.start',params)
        for _ in range(5):
            if result.get('status')!='waiting_approval':break
            a=result['approval'];decision={k:a[k] for k in ('request_id','action_hash','request_scope')}
            await self.service.call('approval.decide',{**decision,'choice':choice},host_token=self.service.host_token)
            result=await self.service.call('exec.start',params)
            if choice!='y':break
        for _ in range(200):
            if result.get('terminal') or not result.get('run_ref'):break
            result=await self.service.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
        self.events.append({'call_id':call_id,'arguments':args,'result':result})
        return result
    async def finish(self):
        await self.service.formal.shutdown()
        evidence={'root':str(self.root),'layout':{'project':str(self.project),'runtime':str(self.runtime)},
                  'before_sha256':self.before,'after_sha256':{p:digest(Path(p)) if Path(p).is_file() else None for p in self.before},
                  'events':self.events,'host_decisions':'synthetic fixture only'}
        (self.root/'evidence.json').write_text(json.dumps(evidence,indent=2))
        return evidence


class ProtectionRepair(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=Path(tempfile.mkdtemp(prefix='biomind-protection-repair-'))
        self.fixtures=[];self.restore=[]
        try:ce.formal_protection_capability({'bwrap_path':'/usr/bin/bwrap'})
        except ce.FormalProtectionError as exc:self.skipTest('real backend unavailable: '+str(exc))
    async def asyncTearDown(self):
        for callback in self.restore:callback()
        for f in self.fixtures:await f.finish()
        # Preserve fixtures only when evidence collection was explicitly asked.
        if not os.environ.get('BIOMIND_REPAIR_EVIDENCE'):
            import shutil
            shutil.rmtree(self.tmp)
    async def fixture(self,name,layout='nested'):
        f=Fixture(self.tmp,name,layout);self.fixtures.append(f);await f.begin();return f
    def blocked(self,result,reason):
        self.assertEqual(result.get('status'),'stage_blocked',result)
        self.assertIs(result.get('execution_started'),False,result)
        self.assertEqual(result.get('reason'),reason,result)
        self.assertIsNone(result.get('exit_code'))
    async def test_file_type_errors_preserve_original_contents(self):
        f=await self.fixture('files');folder=f.project/'directory';folder.mkdir()
        metadata=await f.service.call('files.read_authorize',{'path':str(folder)},host_token=f.service.host_token)
        self.assertEqual(metadata['kind'],'directory')
        self.assertNotIn('content',metadata)
        with self.assertRaises(IsADirectoryError):await f.service.call('files.read',{'path':str(folder)})
        fifo=f.project/'fifo';os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError,'non_regular'):await f.service.call('files.stat',{'path':str(fifo)})
        file=f.project/'file';file.write_text('original');os.link(file,f.project/'second')
        with self.assertRaisesRegex(ValueError,'multiple_links'):await f.service.call('files.stat',{'path':str(file)})
        self.assertEqual(file.read_text(),'original')
        # FILE_READ_REQUIRED belongs to the native readVersion bridge, covered
        # by native-mutation.test.ts, not this direct Python file API.
    async def test_clean_real_mounts_fd_and_input_semantics(self):
        f=await self.fixture('clean');inside=f.project/'input.txt';inside.write_text('project-input')
        (f.project/'alias').symlink_to(f.runtime_file)
        paths=[str(f.source),str(f.runtime_file),str(f.project/'alias'),
               str(f.service.data_root/'task_outputs'/f.service.task_id/'plan_state.json')]
        code='''import pathlib,os,json
out={}
for p in PATHS:
 try:pathlib.Path(p).write_text('bad');out[p]='WRITTEN'
 except OSError as e:out[p]=e.errno
pathlib.Path('output').write_text('allowed');pathlib.Path('input.txt').write_text('still-RW')
out['fds']=[p for p in os.listdir('/proc/self/fd') if p.isdigit() and int(p)>2 and os.path.exists('/proc/self/fd/'+p)]
out['mountinfo']=pathlib.Path('/proc/self/mountinfo').read_text();print(json.dumps(out))
'''.replace('PATHS',repr(paths))
        fd=os.open(f.source,os.O_RDWR);os.set_inheritable(fd,True)
        try:result=await f.run({**command(code),'read_paths':[str(f.source)]})
        finally:os.close(fd)
        self.assertEqual(result['exit_code'],0,result)
        data=json.loads(result['stdout']);self.assertEqual(data['fds'],[])
        for path in paths:self.assertEqual(data[path],30)
        self.assertEqual(inside.read_text(),'still-RW')
        self.assertEqual(digest(f.source),f.before[str(f.source)])
        self.assertEqual(digest(f.runtime_file),f.before[str(f.runtime_file)])
    async def test_nested_readonly_hardlinks_and_scan_statistics(self):
        f=await self.fixture('nested');os.link(f.runtime_file,f.runtime/'second')
        for i in range(80):(f.runtime/f'dep-{i}').write_text('dependency')
        for cmd in ('echo ok','ls .','/usr/bin/python3 -I -B -c "print(123)"'):
            result=await f.run({'cmd':cmd})
            if os.environ.get('BIOMIND_TEST_RO_PRUNING')=='1':
                self.assertEqual(result['exit_code'],0,result)
                raw=json.loads(Path(result['receipt_ref']).read_text())
                metrics=raw['runtime_metadata']['filesystem_protection']
                self.assertEqual(metrics['ro_subtrees_pruned'],1)
                self.assertTrue(metrics['scan_complete']);self.assertLess(metrics['rw_entries'],20)
            else:self.blocked(result,'hardlink_in_writable_scope')
        if os.environ.get('BIOMIND_TEST_RO_PRUNING')=='1':
            result=await f.run(command('import pathlib; pathlib.Path('+repr(str(f.runtime_file))+').write_text("bad")'))
            self.assertNotEqual(result['exit_code'],0);self.assertIn('Read-only file system',result['stderr'])
        self.assertEqual(digest(f.runtime_file),f.before[str(f.runtime_file)])
    async def test_rw_unknown_alias_and_same_prefix_still_rejected(self):
        f=await self.fixture('rw-alias');sibling=f.project/'runtime-extra';sibling.mkdir()
        os.link(f.source,sibling/'alias')
        result=await f.run({'cmd':'touch MUST_NOT_EXIST'})
        self.blocked(result,'hardlink_in_writable_scope');self.assertFalse((f.project/'MUST_NOT_EXIST').exists())
        self.assertEqual(digest(f.source),f.before[str(f.source)])
    async def test_deeper_cache_remains_readonly(self):
        f=await self.fixture('cache');cache=f.runtime/'cache';cache.mkdir()
        with self.assertRaisesRegex(ValueError,'PROTECTED_RUNTIME_PATH'):
            await f.run({**command('import pathlib; pathlib.Path('+repr(str(cache/'out'))+').write_text("bad")'),
                            'write_paths':[str(cache)]})
        self.assertFalse((cache/'out').exists())
    async def test_ancestor_equal_and_external_runtime_layouts(self):
        for layout in ('ancestor','equal','external'):
            f=await self.fixture(layout,layout)
            result=await f.run({'cmd':'echo allowed > output'})
            self.assertEqual(result['exit_code'],0,result)
            if layout in {'ancestor','equal'}:
                os.link(f.source,f.project/'writable-alias')
                self.blocked(await f.run({'cmd':'true'}),'hardlink_in_writable_scope')
            else:
                os.link(f.runtime_file,f.runtime/'alias')
                result=await f.run({'cmd':'cat '+str(f.runtime_file)})
                self.assertEqual(result['exit_code'],0,result)
    async def test_cwd_and_read_declarations_do_not_rebind(self):
        f=await self.fixture('cwd');sub=f.project/'sub';sub.mkdir();inside=f.project/'input';inside.write_text('in')
        for extra in ({},{'read_paths':[]},{'read_paths':[str(inside)]}):
            self.assertEqual((await f.run({'cmd':'echo ok','workdir':str(sub/'..'),**extra}))['exit_code'],0)
        result=await f.run({'cmd':'pwd','workdir':str(f.root)})
        self.assertEqual(result['exit_code'],0,result)
        self.assertEqual(result['stdout'].strip(),str(f.root))
        self.assertEqual(f.service.cwd,f.project)
    async def test_denied_cancelled_replay_preserves_classification(self):
        f=await self.fixture('decisions')
        for choice,status,error in [('n','denied','EXECUTION_PERMISSION_DENIED'),('cancel','cancelled','EXECUTION_PERMISSION_CANCELLED')]:
            await f.service.call('request.resume',{},host_token=f.service.host_token)
            for i in range(2):
                if i==0:
                    pending=await f.service.call('exec.start',{'call_id':status,'arguments':{'cmd':'touch '+status}})
                    a=pending['approval']
                    await f.service.call('approval.decide',{**{k:a[k] for k in ('request_id','action_hash','request_scope')},'choice':choice},host_token=f.service.host_token)
                    with self.assertRaisesRegex(Exception,'REQUEST_PAUSED'):
                        await f.service.call('exec.start',{'call_id':'changed-'+status,'arguments':{'cmd':'touch '+status}})
                    await f.service.call('request.resume',{},host_token=f.service.host_token)
                result=await f.run({'cmd':'touch '+status},call_id=status)
                self.assertEqual(result['permission_status'],status);self.assertEqual(result['error'],error)
                self.assertIs(result['execution_started'],False)
            self.assertFalse((f.project/status).exists())
        rows=f.service._state()['task_state']['execution_permissions']['requests'].values()
        cancelled=next(row for row in rows if row['status']=='cancelled')
        self.assertEqual(cancelled['reason'],'host_cancel_decision');self.assertEqual(cancelled['source'],'human')
    async def test_blocked_call_replays_exact_receipt_new_call_normal_authority(self):
        f=await self.fixture('replay');os.link(f.source,f.project/'alias')
        first=await f.run({'cmd':'true'},call_id='same');raw=Path(first['receipt_ref']).read_bytes()
        second=await f.run({'cmd':'true'},call_id='same')
        self.assertEqual(first['run_ref'],second['run_ref']);self.assertEqual(Path(first['receipt_ref']).read_bytes(),raw)
        pending=await f.service.call('exec.start',{'call_id':'new','arguments':{'cmd':'true'}})
        self.assertEqual(pending['status'],'waiting_approval')
    async def test_missing_backend_and_os_eacces(self):
        f=await self.fixture('backend');f.service.commands.bwrap=str(f.root/'missing')
        self.blocked(await f.run({'cmd':'touch marker'}),'bubblewrap_missing_or_platform_unsupported')
        backend=f.root/'no-exec';backend.write_text('not executable');f.service.commands.bwrap=str(backend)
        result=await f.run({'cmd':'touch marker'})
        self.blocked(result,'bubblewrap_namespace_unavailable');self.assertEqual(result['errno'],13)
        self.assertFalse((f.project/'marker').exists())
    async def test_unreadable_directory_is_actual_scan_failure(self):
        if os.geteuid()==0:self.skipTest('requires non-root DAC denial')
        f=await self.fixture('unreadable');denied=f.project/'denied';denied.mkdir(mode=0)
        self.restore.append(lambda:os.chmod(denied,0o700))
        with self.assertRaises(PermissionError):list(denied.iterdir())
        result=await f.run({'cmd':'touch marker'})
        self.blocked(result,'writable_scope_scan_failed');self.assertEqual(result['errno'],13)
        self.assertFalse((f.project/'marker').exists())
    async def trace_run(self,f,action,args,*,at_entry=False):
        filename=inspect.getsourcefile(ce.formal_protected_spawn)
        # contextmanager wrapper's file can be contextlib: use the wrapped body.
        filename=inspect.getsourcefile(ce.formal_protected_spawn.__wrapped__)
        lines=Path(filename).read_text().splitlines()
        target=next(i for i,line in enumerate(lines,1) if ('    formal_protection_capability(projection)' if at_entry else '            argv += bind(root, writable=True)')==line)
        fired=[]
        def trace(frame,event,arg):
            if not fired and event=='line' and frame.f_code.co_filename==filename and frame.f_lineno==target:
                fired.append(True);action()
            return trace
        sys.settrace(trace)
        try:result=await f.run(args)
        finally:sys.settrace(None)
        self.assertTrue(fired);return result
    @unittest.expectedFailure
    async def test_known_security_gap_concurrent_host_link_must_not_modify_runtime(self):
        f=await self.fixture('KNOWN-UNRESOLVED-concurrent-link')
        result=await self.trace_run(f,lambda:os.link(f.runtime_file,f.project/'late'),
            command('import pathlib; pathlib.Path("late").write_text("ATTACK")'))
        self.assertEqual(result['exit_code'],0,result)
        # Deliberately remains a security failure. Do not invert this assertion.
        self.assertEqual(digest(f.runtime_file),f.before[str(f.runtime_file)])
    async def test_late_readonly_link_is_still_readonly(self):
        f=await self.fixture('late-ro')
        result=await self.trace_run(f,lambda:os.link(f.runtime_file,f.runtime/'late'),
            command('import pathlib; pathlib.Path('+repr(str(f.runtime/'late'))+').write_text("bad")'))
        self.assertNotEqual(result['exit_code'],0);self.assertIn('Read-only file system',result['stderr'])
        self.assertEqual(digest(f.runtime_file),f.before[str(f.runtime_file)])
    async def test_external_anchor_replacement_refused(self):
        f=await self.fixture('anchor');retained=f.root/'retained'
        def change():f.source.rename(retained);f.source.write_text('replacement')
        result=await self.trace_run(f,change,{'cmd':'touch marker','read_paths':[str(f.source)]},at_entry=True)
        self.blocked(result,'read_anchor_changed_at_mount')
        self.assertEqual(digest(retained),f.before[str(f.source)]);self.assertFalse((f.project/'marker').exists())
    async def test_project_root_replacement_with_state_outside_project(self):
        f=await self.fixture('root-swap');retained=f.root/'retained-project'
        def change():f.project.rename(retained);f.project.mkdir()
        result=await self.trace_run(f,change,{'cmd':'touch marker'})
        self.blocked(result,'read_anchor_changed_at_mount')
        self.assertFalse((f.project/'marker').exists());self.assertFalse((retained/'marker').exists())
        self.assertEqual(digest(retained/'runtime/runtime.txt'),f.before[str(f.runtime_file)])
    async def test_backend_setup_failure_does_not_claim_target_exec(self):
        f=await self.fixture('chdir');sub=f.project/'cwd';sub.mkdir()
        result=await self.trace_run(f,lambda:sub.rename(f.project/'moved'),
            {'cmd':'touch '+str(f.project/'marker'),'workdir':str(sub)})
        self.assertIs(result['execution_started'],True);self.assertNotEqual(result['exit_code'],0)
        self.assertIn("Can't chdir",result['stderr']);self.assertFalse((f.project/'marker').exists())
    async def test_real_bind_aliases_and_escaped_mount_paths(self):
        for kind in ('submount','root','ancestor','spaces'):
            result=subprocess.run(
                ['/usr/bin/unshare','-Ur','-m','--',sys.executable,'-B',__file__,'--bind-kind',kind,str(self.tmp)],
                capture_output=True,text=True,env=os.environ.copy(),timeout=60)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            data=json.loads(result.stdout)
            self.assertEqual(data['setup_exit'],0,data)
            self.blocked(data['result'],'unsupported_writable_mount_layout')
            self.assertEqual(data['before'],data['after']);self.assertFalse(data['marker'])


async def bind_fixture(kind,root):
    f=Fixture(root,'bind-'+kind+(' space\\name' if kind=='spaces' else ''),'external')
    if kind in ('submount','spaces'):
        target=f.project/'bound space\\name';target.mkdir();source=f.runtime;alias=target/f.runtime_file.name
    elif kind=='root':target=f.project;source=f.runtime;alias=target/f.runtime_file.name
    else:
        parent=f.root/'mount-parent';parent.mkdir();target=parent;source=f.runtime
        original=f.runtime/'project-child';original.mkdir();(original/'runtime.txt').write_text('ancestor-original')
        f.runtime_file=original/'runtime.txt';f.project=parent/'project-child';alias=f.project/'runtime.txt'
    before=digest(f.runtime_file)
    setup=subprocess.run(['/usr/bin/mount','--bind',str(source),str(target)],capture_output=True,text=True)
    if kind=='ancestor' and setup.returncode==0:
        f.service.cwd=f.project
        f.service.commands.context=ce.ProjectContext.discover(f.project,startup_dir=f.project)
    # Establish the host owner after the synthetic mount topology is fixed.
    # Mutating an already-bound project is now correctly rejected earlier.
    await f.begin()
    result=await f.run(command('import pathlib; pathlib.Path('+repr(str(alias))+').write_text("ATTACK"); pathlib.Path("marker").touch()')) if setup.returncode==0 else {}
    data={'setup_exit':setup.returncode,'setup_stderr':setup.stderr,'result':result,'before':before,
          'after':digest(f.runtime_file),'marker':(f.project/'marker').exists(),'mountinfo':Path('/proc/self/mountinfo').read_text()}
    await f.finish();(f.root/'bind-evidence.json').write_text(json.dumps(data,indent=2));print(json.dumps(data))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--bind-kind':asyncio.run(bind_fixture(sys.argv[2],Path(sys.argv[3])))
    else:unittest.main(verbosity=2)
