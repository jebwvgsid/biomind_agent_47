"""20260930 implementation: real Domain + ledger + sandbox, synthetic data only."""
import asyncio,json,os,sys,tempfile,unittest
from pathlib import Path
sys.dont_write_bytecode=True
RT=Path(__file__).resolve().parents[1];REPO=RT.parents[1]
sys.path[:0]=[str(RT),str(REPO),str(Path(__file__).parent)]
from test_protection_repair import Fixture,command
from domain_adapter.service import DomainError
from agent_core import execution_permissions as ep

class AuditFixes(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='biomind-fix-')
  self.f=Fixture(Path(self.tmp.name),'audit','separate');await self.f.begin();self.s=self.f.service
 async def asyncTearDown(self):
  await self.s.formal.shutdown();self.tmp.cleanup()
 async def host(self,method,**kw):return await self.s.call(method,kw,host_token=self.s.host_token)
 async def decide(self,result,choice='y'):
  a=result['approval'];return await self.host('approval.decide',**{k:a[k] for k in ['request_id','action_hash','request_scope']},choice=choice)
 async def probe(self,code,call='probe',**extra):
  await self.host('request.begin',request_id='fixture',role='consult')
  args={'call_id':call,'arguments':{**command(code),'workdir':str(self.f.project),**extra}}
  result=await self.host('exec.probe',**args)
  for _ in range(5):
   if result.get('status')!='waiting_approval':break
   await self.decide(result);result=await self.host('exec.probe',**args)
  for _ in range(200):
   if result.get('terminal') or not result.get('run_ref'):break
   result=await self.s.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
  return result
 async def test_project_symlink_precise_read_and_sibling_hidden(self):
  intermediate=self.f.project/'relative-link';intermediate.symlink_to('../source.txt')
  link=self.f.project/'中文 空格.mcool';link.symlink_to('relative-link')
  sibling=self.f.root/'secret-sibling.txt';sibling.write_text('must-hide')
  result=await self.f.run({**command(f"from pathlib import Path; print(Path({str(link)!r}).read_text()); print(Path({str(sibling)!r}).exists())"),'workdir':str(self.f.project),'read_paths':[str(link)+'::/resolutions/10000']})
  self.assertEqual(result.get('exit_code'),0,result);self.assertIn('source-original',result.get('stdout',''));self.assertIn('False',result.get('stdout',''))
 async def test_symlink_swap_rejected_after_approval(self):
  link=self.f.project/'alias';link.symlink_to(self.f.source)
  args={'call_id':'swap','arguments':{**command('pass'),'workdir':str(self.f.project),'read_paths':[str(link)]}}
  pending=await self.s.call('exec.start',args);await self.decide(pending)
  pending=await self.s.call('exec.start',args);await self.decide(pending)
  link.unlink();link.symlink_to(self.f.runtime_file)
  with self.assertRaisesRegex(ValueError,'READ_PATH_TARGET_CHANGED'):await self.s.call('exec.start',args)
 async def test_pause_blocks_changed_call_and_valid_old_permit(self):
  args={'call_id':'one','arguments':{**command('pass'),'workdir':str(self.f.project)}}
  one=await self.s.call('exec.start',args);await self.decide(one)
  args['call_id']='two';two=await self.s.call('exec.start',args);await self.decide(two,'n')
  for call in ['one','three']:
   args['call_id']=call
   with self.assertRaisesRegex(DomainError,'REQUEST_PAUSED'):await self.s.call('exec.start',args)
  with self.assertRaisesRegex(DomainError,'PAUSED'):await self.host('request.begin',request_id='fixture',role='generate')
  await self.host('request.resume')
  args['call_id']='one';result=await self.s.call('exec.start',args);self.assertFalse(result.get('execution_started'))
 async def test_external_cwd_requests_scope_before_launch_without_rebinding_project(self):
  other=self.f.root/'另一个 benchmark';other.mkdir()
  alias=self.f.project/'指向其他项目';alias.symlink_to(other,target_is_directory=True)
  for role,method in [('consult','exec.probe'),('generate','exec.start')]:
   await self.host('request.begin',request_id='fixture',role=role)
   for i,workdir in enumerate([other,alias]):
    result=await self.host(method,call_id=f'{role}-wrong-project-{i}',arguments={**command("from pathlib import Path; Path('MUST_NOT_RUN').write_text('x')"),'workdir':str(workdir)})
    self.assertEqual(result['status'],'waiting_approval',result)
    self.assertEqual(result['approval']['payload']['kind'],'file_read')
    self.assertEqual(result['approval']['payload']['path'],str(other.resolve()))
    self.assertFalse(result['execution_started'])
    self.assertFalse((other/'MUST_NOT_RUN').exists())
  self.assertEqual(self.s.commands.project_for_task(self.s.task_id),self.f.project)
 async def test_probe_project_input_runtime_readonly_tmp_writable_network_isolated(self):
  original=self.f.project/'input';original.write_text('original')
  hidden=self.f.root/'ungranted.txt';hidden.write_text('must stay hidden')
  code=f'''from pathlib import Path
import socket,json,resource
out={{}}
for k,p in [('project',{str(original)!r}),('input',{str(self.f.source)!r}),('runtime',{str(self.f.runtime_file)!r})]:
 try: Path(p).write_text('changed');out[k]='WRITABLE'
 except OSError: out[k]='readonly'
Path('/tmp/test').write_text('temporary')
try: socket.create_connection(('1.1.1.1',443),.2);out['network']='CONNECTED'
except OSError: out['network']='blocked'
out['cpu']=resource.getrlimit(resource.RLIMIT_CPU)[0]
out['ungranted_visible']=Path({str(hidden)!r}).exists()
print(json.dumps(out))'''
  result=await self.probe(code,read_paths=[str(self.f.source)])
  self.assertEqual(result.get('exit_code'),0,result)
  value=json.loads(result['stdout']);self.assertEqual(value,{'project':'readonly','input':'readonly','runtime':'readonly','network':'blocked','cpu':10,'ungranted_visible':False})
  self.assertEqual(original.read_text(),'original');self.assertEqual(self.f.source.read_text(),'source-original')
 async def test_probe_output_limit_and_cleanup(self):
  result=await self.probe("while True: print('X'*4096)")
  self.assertTrue(result.get('execution_started'),result);self.assertNotIn('bwrap:',result.get('stderr',''));self.assertIsInstance(result.get('exit_code'),int);self.assertNotEqual(result.get('exit_code'),0,result);self.assertTrue(result.get('terminal'),result);self.assertTrue(result.get('cleanup_confirmed'),result)
 async def test_probe_timeout(self):
  result=await self.probe('import time; time.sleep(40)')
  self.assertTrue(result.get('execution_started'),result);self.assertNotIn('bwrap:',result.get('stderr',''));self.assertIsInstance(result.get('exit_code'),int);self.assertNotEqual(result.get('exit_code'),0,result);self.assertTrue(result.get('terminal'),result);self.assertTrue(result.get('cleanup_confirmed'),result)
 async def test_probe_cannot_be_requested_on_unprivileged_channel(self):
  with self.assertRaisesRegex(DomainError,'HOST_CHANNEL_REQUIRED'):await self.s.call('exec.probe',{})
  with self.assertRaisesRegex(DomainError,'MODEL_AUTHORITY_FIELD_FORBIDDEN'):await self.s.call('exec.start',{'call_id':'forge','arguments':{'cmd':'true','_biomind_probe':True}})
 async def test_automatic_mode_claim_scope_revoke_and_new_request(self):
  mode=await self.host('permissions.mode',mode='auto');self.assertEqual(mode['source'],'explicit_host_user')
  result=await self.f.run({**command("from pathlib import Path; Path('auto.txt').write_text('ok')"),'workdir':str(self.f.project)})
  self.assertEqual(result.get('exit_code'),0,result)
  rows=list(self.s._state()['task_state']['execution_permissions']['requests'].values())
  self.assertTrue(any(r.get('source')=='policy' and r.get('claim') for r in rows))
  pending=await self.s.call('files.stat',{'path':str(self.f.source),'call_id':'external'})
  self.assertEqual(pending['status'],'waiting_approval');await self.decide(pending,'n')
  with self.assertRaisesRegex(DomainError,'PAUSED'):await self.host('permissions.mode',mode='auto')
  await self.host('request.resume');self.assertEqual((await self.host('request.status'))['approval_mode']['mode'],'manual')
  await self.host('permissions.mode',mode='auto')
  await self.host('request.begin',request_id='new-task',role='generate')
  result=await self.s.call('exec.start',{'call_id':'new','arguments':{**command('pass'),'workdir':str(self.f.project)}})
  self.assertEqual(result['status'],'waiting_approval')
 async def test_automatic_mode_cannot_be_model_enabled_or_install(self):
  with self.assertRaisesRegex(DomainError,'HOST_CHANNEL_REQUIRED'):await self.s.call('permissions.mode',{'mode':'auto'})
  await self.host('permissions.mode',mode='auto')
  result=await self.s.call('install.propose',{'call_id':'install','arguments':{'cmd':'true','env_name':'synthetic'}})
  self.assertEqual(result['status'],'waiting_approval');self.assertEqual(result['approval']['payload']['kind'],'installation')
 async def test_mode_revocation_invalidates_policy_claim(self):
  await self.host('permissions.mode',mode='auto')
  payload=ep.command_payload(self.s.commands,{'cmd':'true','workdir':str(self.f.project)},self.s.task_id)
  row=ep.request_permission(self.s.data_root,self.s.task_id,'policy-pending',payload)
  self.assertEqual(row['source'],'policy')
  permit=ep.claim_permission(self.s.data_root,self.s.task_id,row,'claim')
  await self.host('permissions.mode',mode='manual')
  with self.assertRaisesRegex(ep.PermissionDenied,'REVOKED'):ep.validate_permit(permit)
 async def test_dangling_loop_and_private_target(self):
  link=self.f.project/'link';link.symlink_to('missing')
  with self.assertRaises(FileNotFoundError):await self.host('files.read_authorize',path=str(link))
  link.unlink();link.symlink_to('link')
  with self.assertRaises((ValueError,OSError)):await self.host('files.read_authorize',path=str(link))
  link.unlink();private=self.f.root/'.env';private.write_text('synthetic')
  link.symlink_to(private)
  with self.assertRaisesRegex(ValueError,'PRIVATE'):await self.host('files.read_authorize',path=str(link))
 async def test_resume_requires_fresh_human_decision_for_same_denied_action(self):
  args={'path':str(self.f.source),'call_id':'denied-read'}
  pending=await self.s.call('files.stat',args);await self.decide(pending,'n')
  with self.assertRaisesRegex(DomainError,'PAUSED'):
   await self.s.call('files.stat',{**args,'call_id':'changed-id'})
  await self.host('request.resume');await self.host('permissions.mode',mode='auto')
  again=await self.s.call('files.stat',{**args,'call_id':'fresh-human'})
  self.assertEqual(again['status'],'waiting_approval');await self.decide(again)
  result=await self.s.call('files.stat',{**args,'call_id':'fresh-human'})
  self.assertNotEqual(result.get('status'),'waiting_approval')
  reuse=await self.s.call('files.stat',{**args,'call_id':'reuse-new-grant'})
  self.assertNotEqual(reuse.get('status'),'waiting_approval')
  rows=list(self.s._state()['task_state']['execution_permissions']['requests'].values())
  self.assertEqual(next(r for r in rows if r['request_id']==pending['approval']['request_id'])['status'],'denied')
  permitted=await self.f.run({**command(f"print(open({str(self.f.source)!r}).read())"),'workdir':str(self.f.project),'read_paths':[str(self.f.source)]})
  self.assertEqual(permitted.get('exit_code'),0,permitted)
 async def test_pending_disconnect_pauses_and_old_approval_cannot_be_replayed(self):
  args={'call_id':'disconnect','arguments':{**command('pass'),'workdir':str(self.f.project)}}
  pending=await self.s.call('exec.start',args)
  await self.s.disconnected()
  self.assertEqual((await self.host('request.status'))['paused']['reason'],'approval_transport_disconnected')
  self.assertEqual((await self.decide(pending))['status'],'cancelled')
  with self.assertRaisesRegex(DomainError,'PAUSED'):await self.s.call('exec.start',{**args,'call_id':'new'})
 async def test_probe_cancel_confirms_cleanup(self):
  await self.host('request.begin',request_id='fixture',role='consult')
  code=f"from pathlib import Path; import time; print(Path({str(self.f.source)!r}).exists(),flush=True);time.sleep(40)"
  args={'call_id':'cancel-probe','arguments':{**command(code),'workdir':str(self.f.project)}}
  pending=await self.host('exec.probe',**args);await self.decide(pending)
  started=await self.host('exec.probe',**args)
  for _ in range(30):
   if 'False' in started.get('stdout_delta','') or started.get('terminal'):break
   started=await self.s.call('exec.wait',{'run_ref':started['run_ref'],'max_wait_ms':100})
  self.assertIn('False',started.get('stdout_delta',''),started)
  result=await self.s.call('exec.cancel',{'run_ref':started['run_ref']})
  self.assertTrue(result['terminal'],result);self.assertTrue(result['cleanup_confirmed'],result)
  self.assertFalse((await self.s.call('session.lifecycle',{}))['active_jobs'])
 async def test_cancel_then_resume_does_not_automatically_approve_same_write(self):
  args={'path':str(self.f.project/'cancelled.txt'),'content':'fixture','call_id':'cancel-write'}
  pending=await self.s.call('files.write',args);await self.decide(pending,'cancel')
  await self.host('request.resume');await self.host('permissions.mode',mode='auto')
  again=await self.s.call('files.write',{**args,'call_id':'changed-id'})
  self.assertEqual(again['status'],'waiting_approval');self.assertFalse((self.f.project/'cancelled.txt').exists())
if __name__=='__main__':unittest.main()
