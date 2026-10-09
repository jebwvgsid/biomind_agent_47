"""Incremental scope integration tests: production Domain, ledger and real bwrap."""
import asyncio,json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/'integrations/pi_runtime')]
from domain_adapter.service import DomainService

class PathPermissions(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.root=Path(tempfile.mkdtemp(prefix='path-permissions-'));self.events=[];self.index=0
  for n in ['A','B','O','C','A/runtime','skills']:(self.root/n).mkdir(parents=True,exist_ok=True)
  self.A,self.B,self.O,self.C,self.R=[self.root/n for n in ['A','B','O','C','A/runtime']]
  for p in [self.R/'original',self.B/'one',self.B/'two',self.C/'secret']:p.write_text('original')
  (self.root/'registry.json').write_text('{"environments":{},"tools":{}}');(self.root/'prefs.md').write_text('fixture')
  self.s=DomainService(cwd=self.A,data_root=self.root/'state',install_root=self.R,session_id='fixture',registry_path=self.root/'registry.json',preferences_path=self.root/'prefs.md',skills_root=self.root/'skills',memory_mode='disabled')
  await self.call('request.begin',{'request_id':'r1','role':'generate'},True)
 async def asyncTearDown(self):
  await self.s.formal.shutdown()
  out=Path(os.environ.get('BIOMIND_PERMISSION_EVIDENCE',str(self.root)))
  (out/(self._testMethodName+'.json')).write_text(json.dumps({'root':str(self.root),'events':self.events,'state':self.s._state()},ensure_ascii=False,indent=2,default=str))
 async def call(self,method,p=None,host=False):
  try:r=await self.s.call(method,p or {},host_token=self.s.host_token if host else None)
  except Exception as e:self.events.append({'method':method,'params':p,'error':str(e)});raise
  self.events.append({'method':method,'params':p,'result':r});return r
 async def approve(self,r,choice='y',**fields):
  a=r['approval'];return await self.call('approval.decide',{**{k:a[k] for k in ['request_id','action_hash','request_scope']},'choice':choice,**fields},True)
 async def operate(self,method,p,choice='y'):
  for _ in range(8):
   r=await self.call(method,p)
   if r.get('status')!='waiting_approval':break
   await self.approve(r,choice)
  for _ in range(250):
   if r.get('terminal') or not r.get('run_ref'):break
   r=await self.call('exec.wait',{'run_ref':r['run_ref'],'max_wait_ms':100})
  return r
 async def command(self,code,**args):
  self.index+=1;return await self.operate('exec.start',{'call_id':f'cmd-{self.index}','arguments':{'argv':['/usr/bin/python3','-I','-B','-c',code],'workdir':str(self.A),**args}})
 async def test_nested_runtime_file_and_command_protection(self):
  for mode in ['manual','auto']:
   await self.call('permissions.mode',{'mode':mode},True)
   for method,p in [('files.write',{'path':str(self.R/'new'),'content':'bad','call_id':'blocked-'+mode}),('files.mutation_snapshot',{'path':str(self.R/'original')})]:
    with self.assertRaisesRegex(ValueError,'PROTECTED_RUNTIME_PATH'):await self.call(method,p,method.endswith('snapshot'))
   r=await self.operate('files.write',{'path':str(self.A/mode),'content':'ok','call_id':'normal-'+mode});self.assertEqual(r['status'],'completed')
   r=await self.command(f"from pathlib import Path;Path({str(self.R/'original')!r}).write_text('bad')");self.assertEqual(r['exit_code'],1,r);self.assertIn('Read-only',r['stderr'])
  self.assertEqual((self.R/'original').read_text(),'original');self.assertFalse((self.R/'new').exists())

 async def grant(self,path,access='read',lifetime='request',kind='directory',id=None):
  self.index+=1;p={'call_id':id or f'scope-{self.index}','paths':[{'path':str(path),'access':access,'target_kind':kind,'recursive':kind=='directory'}],'purpose':'synthetic fixture','lifetime':lifetime}
  r=await self.call('permissions.propose',p)
  if r.get('status')=='waiting_approval':await self.approve(r,'session' if lifetime=='session' else 'y');r=await self.call('permissions.propose',p)
  self.assertEqual(r['status'],'completed',r);return r
 async def test_request_session_scope_and_mode(self):
  from agent_core.execution_permissions import path_grants
  await self.grant(self.B,'read','request');await self.grant(self.O,'read_write','session')
  await self.call('permissions.mode',{'mode':'auto','lifetime':'session'},True)
  await self.call('request.revise',{},True)
  self.assertEqual((await self.call('permissions.status',host=True))['approval_mode']['mode'],'auto')
  await self.call('request.begin',{'request_id':'r2','role':'generate'},True)
  self.assertFalse(path_grants(self.s.commands,self.s.task_id,self.B));self.assertTrue(path_grants(self.s.commands,self.s.task_id,self.O,'read_write'))
  self.assertEqual((await self.call('permissions.status',host=True))['approval_mode']['mode'],'auto')
  await self.call('permissions.mode',{'mode':'manual'},True);await self.call('request.begin',{'request_id':'r3','role':'generate'},True)
  self.assertEqual((await self.call('permissions.status',host=True))['approval_mode']['mode'],'manual')
 async def test_grant_subset_identity_and_host_boundary(self):
  p={'call_id':'subset','paths':[{'path':str(self.B),'access':'read_write','target_kind':'directory','recursive':True}]}
  r=await self.call('permissions.propose',p);await self.approve(r,'read_only')
  from agent_core.execution_permissions import path_grants
  self.assertTrue(path_grants(self.s.commands,self.s.task_id,self.B));self.assertFalse(path_grants(self.s.commands,self.s.task_id,self.B,'read_write'))
  p={'call_id':'stale','paths':[{'path':str(self.O),'access':'read_write','target_kind':'directory','recursive':True}]};r=await self.call('permissions.propose',p)
  self.O.rename(self.root/'old-O');self.O.mkdir()
  with self.assertRaisesRegex(ValueError,'TARGET_CHANGED'):await self.approve(r)
  with self.assertRaisesRegex(ValueError,'HOST_CHANNEL_REQUIRED'):await self.call('permissions.mode',{'mode':'auto'})
  with self.assertRaisesRegex(ValueError,'PATH_PROPOSAL_FIELDS_INVALID'):await self.call('permissions.propose',{'call_id':'forged','paths':[],'source':'human'})
 async def test_revoke_coverage_without_grant_resurrection(self):
  from agent_core.execution_permissions import path_grants
  one=await self.grant(self.B,'read','session');two=await self.grant(self.B/'one','read','session','file')
  status=await self.call('permissions.status',host=True);g=two['grants'][0]
  await self.call('permissions.revoke',{'grant_id':g['grant_id'],'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},True)
  covering=path_grants(self.s.commands,self.s.task_id,self.B/'one');self.assertEqual([g['grant_id'] for g in covering],[one['grants'][0]['grant_id']])

 async def test_external_cwd_snapshot_outputs_and_opaque_writes(self):
  await self.grant(self.B,'read');await self.grant(self.O,'read_write')
  r=await self.command(f"from pathlib import Path;assert Path.cwd()==Path({str(self.B)!r});Path({str(self.O/'result')!r}).write_text(Path('one').read_text()+Path('two').read_text())",workdir=str(self.B),write_paths=[str(self.O)])
  self.assertEqual(r['exit_code'],0,r);self.assertEqual((self.O/'result').read_text(),'originaloriginal')
  r=await self.command("from pathlib import Path;Path('one').write_text('bad')",workdir=str(self.B))
  self.assertEqual(r['exit_code'],1,r);self.assertEqual((self.B/'one').read_text(),'original')
  r=await self.command(f"from pathlib import Path;Path({str(self.C/'bad')!r}).write_text('bad')")
  self.assertEqual(r['exit_code'],1,r);self.assertFalse((self.C/'bad').exists())
  r=await self.command("from pathlib import Path;Path('../C/relative').write_text('bad')")
  self.assertEqual(r['exit_code'],1,r);self.assertFalse((self.C/'relative').exists())
  self.assertEqual(str(self.s.cwd),str(self.A))

 async def test_missing_output_file_and_directory(self):
  path=self.O/'new'/'result';p={'path':str(path),'content':'one','call_id':'external-file'}
  r=await self.call('files.write',p);self.assertEqual(r['status'],'waiting_approval');self.assertFalse(path.parent.exists())
  await self.approve(r);self.assertFalse(path.parent.exists())
  r=await self.operate('files.write',p);self.assertEqual(r['status'],'completed',r);self.assertEqual(path.read_text(),'one')
  await self.grant(self.O/'new-dir','read_write')
  r=await self.command(f"from pathlib import Path;Path({str(self.O/'new-dir'/'result')!r}).write_text('ok')",write_paths=[str(self.O/'new-dir')])
  self.assertEqual(r['exit_code'],0,r);self.assertEqual((self.O/'new-dir'/'result').read_text(),'ok')
  # Creation pins the actual directory, including before a command finishes.
  # Replacing the once-missing root must never inherit that identity.
  from agent_core.execution_permissions import path_grants
  self.assertTrue(path_grants(self.s.commands,self.s.task_id,self.O/'new-dir','read_write')[0]['target_anchor'])
  (self.O/'new-dir').rename(self.O/'old-dir')
  self.assertFalse(path_grants(self.s.commands,self.s.task_id,self.O/'new-dir','read_write'))
  (self.O/'new-dir').mkdir()
  self.assertFalse(path_grants(self.s.commands,self.s.task_id,self.O/'new-dir','read_write'))

 async def test_read_snapshot_filters_and_limits(self):
  await self.grant(self.B,'read_write','session')
  (self.B/'.env').write_text('synthetic excluded');(self.B/'nested').mkdir();(self.B/'nested'/'index').write_text('idx')
  r=await self.command("from pathlib import Path;assert not Path('.env').exists();assert Path('nested/index').read_text()=='idx'",workdir=str(self.B))
  self.assertEqual(r['exit_code'],0,r)
  r=await self.command("from pathlib import Path;Path('one').write_text('bad')",workdir=str(self.B),read_paths=[str(self.B)])
  self.assertEqual(r['exit_code'],1,r)
  (self.B/'alias').symlink_to(self.C/'secret')
  r=await self.command('print(1)',workdir=str(self.B));self.assertIn('directory_snapshot_symlink',str(r));self.assertFalse(r.get('execution_started'))
  (self.B/'alias').unlink()
  for i in range(257):(self.B/f'f{i}').touch()
  r=await self.command('print(1)',workdir=str(self.B));self.assertIn('directory_snapshot_limit',str(r));self.assertFalse(r.get('execution_started'))

 async def test_probe_external_rw_grant_is_still_readonly(self):
  await self.grant(self.B,'read_write','session')
  await self.call('request.begin',{'request_id':'r1','role':'consult'},True)
  p={'call_id':'probe','arguments':{'argv':['/usr/bin/python3','-I','-c',"from pathlib import Path;Path('one').write_text('bad')"],'workdir':str(self.B)}}
  r=await self.call('exec.probe',p,True);await self.approve(r)
  r=await self.call('exec.probe',p,True)
  for _ in range(100):
   if r.get('terminal'):break
   r=await self.call('exec.wait',{'run_ref':r['run_ref'],'max_wait_ms':100})
  self.assertEqual(r['exit_code'],1,r);self.assertIn('Read-only',r['stderr']);self.assertEqual((self.B/'one').read_text(),'original')

 async def test_output_permission_does_not_revise_host_task_spec(self):
  from agent_core.pending_input import mutate
  from agent_core.task_spec import ensure_task_spec
  from agent_core.plan_tracker import commit_consultant_candidate_state,canonical_task_spec_for_state
  await self.grant(self.O,'read_write')
  # A synthetic host-owned TaskSpec, persisted by the same canonical record/CAS
  # used by the application; model proposal fields never enter this record.
  spec=ensure_task_spec({'revision':1,'goal':'write results under A/results','output_contracts':[{
   'artifact_id':'output_dir','role':'output_dir','artifact_kind':'directory','canonical_path':str(self.A/'results'),
   'source':'user_explicit','source_ref':'synthetic-host-user-request'}]})
  commit_consultant_candidate_state(self.s.data_root,self.s.task_id,{'task_spec':spec})
  before=canonical_task_spec_for_state(self.s._state())
  r=await self.command(f"from pathlib import Path;Path({str(self.O/'wrong')!r}).write_text('bad')",write_paths=[str(self.O)])
  self.assertIn('OUTPUT_CONTRACT_CONFLICT',str(r));self.assertFalse(r.get('execution_started'));self.assertFalse((self.O/'wrong').exists())
  self.assertEqual(canonical_task_spec_for_state(self.s._state()),before)
  # A separate, explicit host revision changes the output requirement. The
  # permission grant alone above could not revise it.
  before['revision']=2;before['output_contracts'][0]['canonical_path']=str(self.O)
  commit_consultant_candidate_state(self.s.data_root,self.s.task_id,{'task_spec':before})
  r=await self.command(f"from pathlib import Path;Path({str(self.O/'correct')!r}).write_text('ok')",write_paths=[str(self.O)])
  self.assertEqual(r['exit_code'],0,r);self.assertEqual((self.O/'correct').read_text(),'ok')

 async def test_revoke_pending_action_same_call_and_concurrent_reply(self):
  from agent_core import execution_permissions as ep
  granted=await self.grant(self.O,'read_write','session');gid=granted['grants'][0]['grant_id']
  p={'path':str(self.O/'pending'),'content':'must not write','call_id':'pending'}
  r=await self.call('files.write',p);self.assertEqual(r['status'],'waiting_approval')
  # Two independently arriving replies consume the same immutable decision.
  await asyncio.gather(self.approve(r),self.approve(r))
  status=await self.call('permissions.status',host=True)
  await self.call('permissions.revoke',{'grant_id':gid,'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},True)
  row=ep.permission_state(self.s.data_root,self.s.task_id,r['approval']['request_id']);self.assertEqual(row['status'],'cancelled')
  again=await self.call('files.write',p);self.assertEqual(again['status'],'waiting_approval');self.assertFalse((self.O/'pending').exists())
  await self.approve(again,'cancel')
  for identity in ['pending','changed']:
   with self.assertRaisesRegex(ValueError,'REQUEST_PAUSED'):await self.call('files.write',{**p,'call_id':identity,'content':'changed'})
  await self.call('request.resume',{},True)
  self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,self.O,'read_write'))
  fresh=await self.call('files.write',{**p,'call_id':'after-resume'});self.assertEqual(fresh['status'],'waiting_approval')

 async def test_missing_scope_anchor_swap_and_adjacent_prefix(self):
  from agent_core import execution_permissions as ep
  new=self.O/'not-created';r=await self.grant(new,'read_write','session');self.assertFalse(new.exists())
  self.O.rename(self.root/'old-output');self.O.mkdir()
  self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,new,'read_write'))
  p={'path':str(new/'file'),'content':'bad','call_id':'swapped'}
  pending=await self.call('files.write',p);self.assertEqual(pending['status'],'waiting_approval');self.assertFalse(new.exists())
  await self.grant(self.B,'read_write')
  adjacent=self.root/'B-extra';adjacent.mkdir();(adjacent/'one').write_text('hidden')
  self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,adjacent/'one'))
  alias=self.B/'runtime-alias';alias.symlink_to(self.R,target_is_directory=True)
  with self.assertRaisesRegex(ValueError,'PROTECTED_RUNTIME_PATH'):await self.call('files.write',{'path':str(alias/'original'),'content':'bad','call_id':'runtime-alias'})

 async def test_legacy_policy_migration_and_corruption_fail_closed(self):
  from agent_core.pending_input import mutate
  from agent_core import execution_permissions as ep
  read=await self.call('files.read_authorize',{'path':str(self.B/'one'),'call_id':'legacy'},True);await self.approve(read)
  def legacy(state):
   book=state['task_state']['execution_permissions'];book['policy_revision']='execution-permissions-v1'
   for value in [*book['grants'].values(),*book['requests'].values()]:
    value['policy_revision']='execution-permissions-v1';value.pop('lifetime',None)
  mutate(self.s.data_root,self.s.task_id,legacy)
  status=await self.call('permissions.status',host=True)
  self.assertEqual(status['policy_revision'],'execution-permissions-v2');self.assertEqual(status['grants'][0]['lifetime'],'request')
  await self.call('request.begin',{'request_id':'r2','role':'generate'},True);self.assertFalse((await self.call('permissions.status',host=True))['grants'][0]['active'])
  mutate(self.s.data_root,self.s.task_id,lambda state:state['task_state']['execution_permissions'].update(policy_revision='future-unknown'))
  with self.assertRaisesRegex(ValueError,'UNKNOWN_VERSION'):await self.call('permissions.status',host=True)
  # Restore only the synthetic corruption so teardown can capture evidence.
  mutate(self.s.data_root,self.s.task_id,lambda state:state['task_state']['execution_permissions'].update(policy_revision=ep._POLICY_REVISION))
  book=self.s._state()['task_state']['execution_permissions']
  mutate(self.s.data_root,self.s.task_id,lambda state:state['task_state'].pop('execution_permissions'))
  with self.assertRaisesRegex(ValueError,'PERMISSION_BOOK_MISSING'):await self.call('permissions.status',host=True)
  mutate(self.s.data_root,self.s.task_id,lambda state:state['task_state'].update(execution_permissions=[]))
  with self.assertRaisesRegex(ValueError,'INVALID_OR_UNKNOWN_VERSION'):await self.call('permissions.status',host=True)
  mutate(self.s.data_root,self.s.task_id,lambda state:state['task_state'].update(execution_permissions=book))

 async def test_interrupted_directory_creation_pins_identity_before_write(self):
  from agent_core import execution_permissions as ep
  from unittest.mock import patch
  target=self.O/'missing-root';await self.grant(target,'read_write','session')
  p={'path':str(target/'result'),'content':'should not commit','call_id':'interrupted-file'}
  pending=await self.call('files.write',p);await self.approve(pending)
  original=ep.record_created_directory
  def interrupt(path,fd,permit=None):
   original(path,fd,permit)
   if Path(path)==target:raise RuntimeError('SYNTHETIC_CREATE_INTERRUPTION')
  with patch.object(ep,'record_created_directory',side_effect=interrupt):
   with self.assertRaisesRegex(RuntimeError,'SYNTHETIC_CREATE_INTERRUPTION'):await self.call('files.write',p)
  self.assertTrue(target.is_dir());self.assertFalse((target/'result').exists())
  self.assertTrue(ep.path_grants(self.s.commands,self.s.task_id,target,'read_write')[0]['target_anchor'])
  replay=await self.call('files.write',p);self.assertEqual(replay['status'],'reconciliation_required',replay)
  target.rename(self.O/'interrupted-old');target.mkdir()
  self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,target,'read_write'))

 async def test_uri_arguments_and_read_upgrade_use_live_grants(self):
  uri=str(self.B/'one')+'::/resolutions/10000'
  await self.grant(self.B/'one','read',kind='file')
  r=await self.command("import sys;from pathlib import Path;assert sys.argv[1].endswith('::/resolutions/10000');assert Path(sys.argv[1].split('::')[0]).read_text()=='original'",argv=['/usr/bin/python3','-I','-B','-c',"import sys;from pathlib import Path;assert sys.argv[1].endswith('::/resolutions/10000');assert Path(sys.argv[1].split('::')[0]).read_text()=='original'",uri],read_paths=[uri])
  self.assertEqual(r['exit_code'],0,r)
  await self.call('permissions.mode',{'mode':'auto','lifetime':'session'},True)
  p={'path':str(self.B/'one'),'call_id':'upgrade'}
  r=await self.call('files.mutation_snapshot',p,True)
  self.assertEqual(r['status'],'waiting_approval');self.assertEqual(r['approval']['payload']['targets'][0]['access'],'read_write')
  self.assertEqual((self.B/'one').read_text(),'original');await self.approve(r)
  snapshot=await self.call('files.mutation_snapshot',p,True)
  r=await self.call('files.write',{'path':str(self.B/'one'),'content':'changed','expected_sha256':snapshot['sha256'],'call_id':'upgraded-write'})
  self.assertEqual(r['status'],'completed',r);self.assertEqual((self.B/'one').read_text(),'changed')
  outside=await self.call('files.write',{'path':str(self.C/'new'),'content':'bad','call_id':'new-range-auto'})
  self.assertEqual(outside['status'],'waiting_approval');self.assertEqual(outside['approval']['payload']['kind'],'path_access');self.assertFalse((self.C/'new').exists())

 async def test_host_subset_and_exact_snapshot_target_replacement(self):
  from agent_core.execution_permissions import path_grants
  from unittest.mock import patch
  p={'call_id':'subset-paths','paths':[{'path':str(self.B/'one'),'access':'read_write','target_kind':'file'},{'path':str(self.C),'access':'read','target_kind':'directory'}]}
  r=await self.call('permissions.propose',p)
  await self.approve(r,selections=[{'index':0,'access':'read_write','lifetime':'session'}])
  self.assertFalse(path_grants(self.s.commands,self.s.task_id,self.C));self.assertEqual(path_grants(self.s.commands,self.s.task_id,self.B/'one')[0]['lifetime'],'session')
  original=self.s.files._read
  def swap(parent,name,expected_identity=None):
   (self.B/'one').rename(self.B/'old-one');(self.B/'one').write_text('replacement must not be disclosed')
   return original(parent,name,expected_identity)
  with patch.object(self.s.files,'_read',side_effect=swap):
   with self.assertRaisesRegex(ValueError,'TARGET_REPLACED'):await self.call('files.mutation_snapshot',{'path':str(self.B/'one'),'call_id':'snapshot-race'},True)
  self.assertFalse(path_grants(self.s.commands,self.s.task_id,self.B/'one','read_write'))

if __name__=='__main__':unittest.main()
