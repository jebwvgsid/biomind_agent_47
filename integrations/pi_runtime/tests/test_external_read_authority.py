"""A authority tests: synthetic files, sealed simulated host choices, no provider."""
import asyncio
import importlib.abc
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
RUNTIME=Path(__file__).resolve().parents[1]
REPO=RUNTIME.parents[1]
sys.path[:0]=[str(RUNTIME),str(REPO)]

class NoProvider(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='config' or fullname.startswith('config.') or fullname in {
                'bio_agents','agent_core.provider_attempt','agent_core.tool_loop','dotenv'}:
            raise ImportError('SYNTHETIC_TEST_FORBIDS_CONFIG_OR_PROVIDER:'+fullname)
sys.meta_path.insert(0,NoProvider())
from domain_adapter.service import DomainService, DomainError,DomainError
from agent_core import execution_permissions as ep
from agent_core.pending_input import mutate

class ExternalReadAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-external-read-')
        self.root=Path(self.temp.name)
        self.project=self.root/'project';self.project.mkdir()
        self.inputs=self.root/'inputs';self.inputs.mkdir()
        self.leaf=self.inputs/'leaf.bin';self.leaf.write_bytes(b'leaf\x00binary')
        self.bud=self.inputs/'bud.txt';self.bud.write_text('bud fixture\n')
        registry=self.root/'registry.json';registry.write_text('{"environments":{},"tools":{}}')
        preferences=self.root/'preferences.md';preferences.write_text('synthetic preferences')
        self.skills=self.root/'skills';(self.skills/'trusted'/'scripts').mkdir(parents=True)
        (self.skills/'trusted'/'SKILL.md').write_text('---\nname: synthetic\ndescription: Test only\n---\n')
        (self.skills/'trusted'/'scripts'/'a.py').write_text('print("fixture")\n')
        self.s=DomainService(cwd=self.project,data_root=self.root/'records',install_root=REPO,
            session_id='synthetic-session',registry_path=registry,skills_root=self.skills,
            preferences_path=preferences,memory_root=self.root/'memory',wiki_root=self.root/'wiki')
        await self.begin('request-1')

    async def asyncTearDown(self):
        await self.s.formal.shutdown()
        self.temp.cleanup()

    async def begin(self,request,role='consult'):
        return await self.s.call('request.begin',{'request_id':request,'role':role},host_token=self.s.host_token)

    async def stat(self,path=None,call='read-1',**extra):
        return await self.s.call('files.read_authorize',{'path':str(path or self.leaf),'call_id':call,**extra},host_token=self.s.host_token)

    async def decide(self,pending,choice):
        a=pending['approval']
        return await self.s.call('approval.decide',{'request_id':a['request_id'],'action_hash':a['action_hash'],
            'request_scope':a['request_scope'],'choice':choice},host_token=self.s.host_token)

    async def test_exact_file_vs_explicit_directory_scope_sequential(self):
        before=self.s.scope()
        p=await self.stat();self.assertEqual(p['status'],'waiting_approval')
        self.assertEqual(p['approval']['payload']['target_kind'],'file')
        await self.decide(p,'y')
        first=await self.stat();self.assertTrue(first['read_only_source'])
        self.assertNotIn('write_path',first)
        second=await self.stat(self.bud,'bud');self.assertEqual(second['status'],'waiting_approval')
        await self.decide(second,'scope')
        (self.inputs/'third').write_text('third')
        third=await self.stat(self.inputs/'third','third');self.assertEqual(third['status'],'completed')
        self.assertEqual(before,self.s.scope());self.assertEqual(self.s.commands.project_for_task(self.s.task_id),self.project)

    async def test_prior_pending_rechecked_against_later_directory_grant(self):
        first=await self.stat(self.leaf,'leaf')
        second=await self.stat(self.bud,'bud')
        await self.decide(second,'scope')
        result=await self.stat(self.leaf,'leaf');self.assertEqual(result['status'],'completed')
        self.assertEqual(ep.permission_state(self.s.data_root,self.s.task_id,first['approval']['request_id'])['source'],'rule')

    async def test_denial_survives_new_tool_call_description_and_directory_grant(self):
        second=await self.stat(self.bud,'bud')
        first=await self.stat(self.leaf,'leaf');await self.decide(first,'n')
        self.assertEqual((await self.decide(second,'scope'))['status'],'cancelled')
        self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])
        for method in ('files.stat','files.read','files.read_authorize'):
            with self.subTest(method=method):
                with self.assertRaisesRegex(DomainError,'REQUEST_PAUSED'):
                    await self.s.call(method,{'path':str(self.leaf),'call_id':'alternate-'+method},
                        host_token=self.s.host_token if method=='files.read_authorize' else None)

    async def test_consult_generate_same_request_and_new_request_session_do_not_inherit(self):
        p=await self.stat();await self.decide(p,'scope')
        await self.begin('request-1','generate')
        self.assertEqual((await self.stat(self.bud,'generate'))['status'],'completed')
        await self.begin('request-2','generate')
        self.assertEqual((await self.stat(self.bud,'new'))['status'],'waiting_approval')
        mutate(self.s.data_root,self.s.task_id,lambda s:s['task_state'].update(pi_session_id='other-session'))
        self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])

    async def test_revoke_invalidates_human_rule_and_new_calls_preserving_history(self):
        p=await self.stat();await self.decide(p,'scope')
        self.assertEqual((await self.stat(self.bud,'rule'))['status'],'completed')
        status=await self.s.call('permissions.status',{},host_token=self.s.host_token)
        grant=status['grants'][0]
        result=await self.s.call('permissions.revoke',{'grant_id':grant['grant_id'],
            'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},host_token=self.s.host_token)
        self.assertTrue(result['revoked'])
        self.assertEqual(ep.permission_state(self.s.data_root,self.s.task_id,p['approval']['request_id'])['status'],'cancelled')
        for path,call in ((self.leaf,'read-1'),(self.bud,'rule')):
            with self.assertRaisesRegex(ep.PermissionDenied,'CANCELLED'):
                await self.stat(path,call)
        self.assertEqual((await self.stat(self.bud,'new'))['status'],'waiting_approval')
        self.assertEqual(len(ep.active_read_grants(self.s.data_root,self.s.task_id,include_inactive=True)),1)

    async def test_blank_null_cancel_never_grant_and_host_only(self):
        p=await self.stat()
        for choice in ('',None,'   ','details','unoffered'):
            result=await self.decide(p,choice);self.assertFalse(result.get('approved',False))
            self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])
        await self.decide(p,'cancel')
        with self.assertRaisesRegex(DomainError,'REQUEST_PAUSED'): await self.stat()
        with self.assertRaises(DomainError):
            await self.s.call('files.read_authorize',{'path':str(self.leaf),'trusted_skill_dirs':[str(self.inputs)]})
        with self.assertRaises(DomainError):
            await self.s.call('permissions.revoke',{'grant_id':'fake'})

    async def test_skill_stat_and_read_same_host_source_and_fake_directory_not_trusted(self):
        target=self.skills/'trusted'/'scripts'/'a.py'
        trusted={'trusted_skill_dirs':[str(self.skills/'trusted')]}
        receipt=await self.stat(target,'skill',**trusted)
        self.assertEqual(receipt['status'],'completed');self.assertTrue(receipt['read_only_source'])
        read=self.s.files.read({'path':str(target)},task_id=self.s.task_id,trusted_skill_dirs=trusted['trusted_skill_dirs'])
        self.assertIn('fixture',read['content']);self.assertTrue(read['read_only_source'])
        self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])
        fake=self.inputs/'trusted';fake.mkdir();(fake/'a.py').write_text('not a skill')
        self.assertEqual((await self.stat(fake/'a.py','fake',**trusted))['status'],'waiting_approval')

    async def test_fresh_stat_no_binary_decode_or_hash_and_read_cannot_write(self):
        with self.leaf.open('wb') as f:f.truncate(2*1024**3)
        p=await self.stat();await self.decide(p,'y')
        with patch.object(Path,'read_bytes',side_effect=AssertionError('no content hash in stat')):
            result=await self.stat();self.assertEqual(result['bytes'],2*1024**3)
        with self.leaf.open('wb') as f:f.write(b'new\x00bytes')
        result=await self.stat();self.assertEqual(result['bytes'],9)
        with self.assertRaisesRegex(ValueError,'PATH_WRITE_PERMISSION_REQUIRED'):
            self.s.files.write({'path':str(self.leaf),'content':'bad','expected_sha256':''},task_id=self.s.task_id)

    async def test_symlink_hardlink_and_anchor_replacement_rejected(self):
        p=await self.stat();await self.decide(p,'y')
        old=self.leaf.with_name('old');self.leaf.rename(old);self.leaf.write_bytes(b'replaced')
        with self.assertRaises(ep.PermissionDenied): await self.stat()
        linked=self.inputs/'symlink';linked.symlink_to(self.bud)
        pending=await self.stat(linked,'sym')
        self.assertEqual(pending['status'],'waiting_approval');self.assertEqual(pending['approval']['payload']['path'],str(self.bud))
        hard=self.inputs/'hard';os.link(self.bud,hard)
        with self.assertRaises(ep.PermissionDenied): await self.stat(hard,'hard')

    async def test_directory_declaration_y_is_exact_directory_not_parent(self):
        try: ep.prepare_file_read(self.s.files,str(self.inputs),task_id=self.s.task_id,call_id='directory')
        except ep.PermissionPending as exc: row=exc.request
        else:self.fail('expected pending')
        self.assertEqual(set(row['allowed_decisions']),{'y','n','session','directory_session'});self.assertTrue(row['payload']['recursive'])
        ep.record_human_decision(self.s.data_root,self.s.task_id,
            ep._Decision(row['request_id'],row['action_hash'],'y',ep._SEAL))
        self.assertEqual((await self.stat(self.bud,'inside'))['status'],'completed')
        sibling=self.root/'sibling.txt';sibling.write_text('outside')
        self.assertEqual((await self.stat(sibling,'sibling'))['status'],'waiting_approval')

    async def test_status_is_query_and_stale_revoke_does_not_mutate_grant(self):
        before=ep.read_permission_status(self.s.commands,self.s.task_id)
        self.assertEqual(before['grants'],[])
        p=await self.stat();await self.decide(p,'scope')
        status=ep.read_permission_status(self.s.commands,self.s.task_id)
        with self.assertRaises(ep.PermissionDenied):
            ep.revoke_read_grant(self.s.commands,self.s.task_id,status['grants'][0]['grant_id'],expected_scope={})
        self.assertEqual(len(ep.active_read_grants(self.s.data_root,self.s.task_id)),1)

    async def test_command_subidentity_projection_freeze_and_revoke(self):
        await self.begin('request-1','generate')
        args={'cmd':'true','read_paths':[str(self.leaf)]}
        try:ep.command_payload(self.s.commands,args,self.s.task_id,call_id='same-call')
        except ep.PermissionPending as exc: row=exc.request
        else:self.fail('missing input approval')
        ep.record_human_decision(self.s.data_root,self.s.task_id,ep._Decision(row['request_id'],row['action_hash'],'y',ep._SEAL))
        command_id=ep.derive_permission_call_id('same-call','command')
        payload=ep.command_payload(self.s.commands,args,self.s.task_id,call_id=command_id)
        command=ep.request_permission(self.s.data_root,self.s.task_id,command_id,payload)
        self.assertNotEqual(command['request_id'],row['request_id'])
        self.assertNotIn('read_paths',payload['arguments'])
        ep.record_human_decision(self.s.data_root,self.s.task_id,ep._Decision(command['request_id'],command['action_hash'],'y',ep._SEAL))
        permit=ep.claim_permission(self.s.data_root,self.s.task_id,command,'synthetic-owner')
        ep.validate_permit(permit)
        # Formal final recheck receives the original owner id, while domain
        # approval received its command child id. They must reuse read rows.
        from agent_core.plan_tracker import load_plan_state
        before=load_plan_state(self.s.data_root,self.s.task_id)['task_state']['execution_permissions']['requests']
        self.assertEqual(ep.command_payload(self.s.commands,args,self.s.task_id,call_id='same-call'),payload)
        after=load_plan_state(self.s.data_root,self.s.task_id)['task_state']['execution_permissions']['requests']
        self.assertEqual(set(before),set(after))
        ep.revoke_read_grant(self.s.commands,self.s.task_id,row['request_id'])
        with self.assertRaises(ep.PermissionDenied):ep.validate_permit(permit)

    async def test_unrelated_grant_does_not_change_existing_command_or_install_projection(self):
        p=await self.stat();await self.decide(p,'y')
        args={'cmd':'true'}
        before={}
        for call in ('command-freeze','install:install-freeze'):
            projection=ep.command_access_projection(self.s.commands,args,self.s.task_id,call_id=call)
            identity=call if call.startswith('install:') else ep.derive_permission_call_id(call,'command')
            ep.request_permission(self.s.data_root,self.s.task_id,identity,{'kind':'installation' if call.startswith('install:') else 'command',
                'access_projection':projection,'human_required':True})
            before[call]=projection
        other=self.root/'other';other.mkdir();target=other/'input';target.write_text('other')
        p=await self.stat(target,'other');await self.decide(p,'scope')
        for call in before:
            self.assertEqual(ep.command_access_projection(self.s.commands,args,self.s.task_id,call_id=call),before[call])

    async def test_status_does_not_create_context_or_ledger(self):
        binding=self.s.data_root/'task_outputs'/self.s.task_id/'command_inspection'/'project_context.json'
        existed=binding.exists()
        before={str(p):p.stat().st_mtime_ns for p in self.s.data_root.rglob('*') if p.is_file()}
        ep.read_permission_status(self.s.commands,self.s.task_id)
        after={str(p):p.stat().st_mtime_ns for p in self.s.data_root.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertEqual(binding.exists(),existed)

    async def test_external_script_pending_before_content_hash_and_denial_not_projected(self):
        script=self.inputs/'analysis.py';script.write_text('print("synthetic")')
        args={'script_path':str(script),'interpreter':'python'}
        with patch('agent_core.execution_permissions._authorized_source_hash',side_effect=AssertionError('read before approval')):
            with self.assertRaises(ep.PermissionPending):
                ep.command_payload(self.s.commands,args,self.s.task_id,call_id='script')
        allowed=await self.stat(self.bud,'allow')
        denied=await self.stat(self.leaf,'deny');await self.decide(denied,'n')
        self.assertEqual((await self.decide(allowed,'scope'))['status'],'cancelled')
        with self.assertRaisesRegex(ep.PermissionDenied,'FILE_READ_PERMISSION_(DENIED|CANCELLED)'):
            ep.command_access_projection(self.s.commands,{'cmd':'true','read_paths':[str(self.leaf)]},self.s.task_id,call_id='cross-tool')
        # Neither an unrelated directory grant nor a denied file belongs in pwd.
        projection=ep.command_access_projection(self.s.commands,{'cmd':'pwd'},self.s.task_id,call_id='unrelated')
        self.assertEqual(projection['reads'],[])

    async def test_directory_grant_projects_two_files_and_revocation_still_invalidates(self):
        p=await self.stat();await self.decide(p,'scope')
        before=set(self.s._state()['task_state']['execution_permissions']['grants'])
        args={'cmd':'true','read_paths':[str(self.leaf),str(self.bud)]}
        projection=ep.command_access_projection(self.s.commands,args,self.s.task_id,call_id='two-files')
        self.assertEqual({g['root'] for g in projection['reads']},{str(self.leaf),str(self.bud)})
        self.assertTrue(all(g['target_kind']=='file' and not g['recursive'] for g in projection['reads']))
        self.assertEqual(set(self.s._state()['task_state']['execution_permissions']['grants']),before)
        ep.validate_read_grants(self.s.data_root,self.s.task_id,projection)
        ep.revoke_read_grant(self.s.commands,self.s.task_id,p['approval']['request_id'])
        with self.assertRaisesRegex(ep.PermissionDenied,'REVOKED_OR_CHANGED'):
            ep.validate_read_grants(self.s.data_root,self.s.task_id,projection)

    async def test_projected_file_replacement_and_private_child_are_rejected(self):
        p=await self.stat();await self.decide(p,'scope')
        projection=ep.command_access_projection(self.s.commands,{'cmd':'true','read_paths':[str(self.leaf)]},self.s.task_id,call_id='anchored')
        self.leaf.rename(self.inputs/'old-leaf');self.leaf.write_bytes(b'replacement')
        with self.assertRaisesRegex(ep.PermissionDenied,'REVOKED_OR_CHANGED'):
            ep.validate_read_grants(self.s.data_root,self.s.task_id,projection)
        private=self.inputs/'.env.private';private.write_text('synthetic only')
        with self.assertRaisesRegex(ValueError,'PRIVATE_FILE_EXCLUDED'):
            ep.command_access_projection(self.s.commands,{'cmd':'true','read_paths':[str(private)]},self.s.task_id,call_id='private')

    async def test_directory_stat_reports_metadata_without_listing_or_reapproval(self):
        data=self.project/'data';data.mkdir()
        project=await self.s.call('files.stat',{'path':str(data)})
        self.assertEqual(project['kind'],'directory')
        p=await self.stat();await self.decide(p,'scope')
        result=await self.s.call('files.stat',{'path':str(self.inputs),'call_id':'directory-stat'})
        self.assertEqual(result['kind'],'directory')
        self.assertEqual(result['inode'],self.inputs.stat().st_ino)
        self.assertNotIn('content',result)

    async def test_cancel_request_is_not_grant_revocation(self):
        p=await self.stat();await self.decide(p,'scope')
        ep.cancel_permission(self.s.data_root,self.s.task_id,p['approval']['request_id'])
        with self.assertRaisesRegex(ep.PermissionDenied,'CANCELLED'):await self.stat()
        self.assertEqual((await self.stat(self.bud,'new-read'))['status'],'completed')
        grants=ep.active_read_grants(self.s.data_root,self.s.task_id)
        self.assertEqual(len(grants),1);self.assertFalse(grants[0]['revoked'])

    async def test_project_switch_and_directory_anchor_replacement_do_not_inherit(self):
        from dataclasses import replace
        p=await self.stat();await self.decide(p,'scope')
        switched=self.root/'other-project';switched.mkdir()
        self.s.commands.context=replace(self.s.commands.context,project_dir=switched)
        with self.assertRaisesRegex(ep.PermissionDenied,'PERMISSION_STORAGE_OWNER_MISMATCH'):await self.stat(self.bud,'other-project')
        self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])
        self.s.commands.context=replace(self.s.commands.context,project_dir=self.project)
        self.s.commands.project_for_task(self.s.task_id)
        self.inputs.rename(self.root/'old-inputs');self.inputs.mkdir();self.bud.write_text('new directory')
        self.assertEqual((await self.stat(self.bud,'new-directory-anchor'))['status'],'waiting_approval')

    async def test_content_read_reuses_permission_but_never_old_content(self):
        p=await self.stat(self.bud,'content');await self.decide(p,'y')
        first=await self.s.call('files.read',{'path':str(self.bud),'call_id':'content-read'})
        self.assertEqual(first['content'],'bud fixture\n');self.assertTrue(first['read_only_source'])
        self.bud.write_text('updated contents\n')
        second=await self.s.call('files.read',{'path':str(self.bud),'call_id':'content-read'})
        self.assertEqual(second['content'],'updated contents\n')
        self.assertNotEqual(second['sha256'],first['sha256']);self.assertNotIn('write_path',second)

    async def test_same_session_shared_ledger_different_project_cannot_reuse_grant(self):
        p=await self.stat();await self.decide(p,'scope')
        other_project=self.root/'other-domain-project';other_project.mkdir()
        other=DomainService(cwd=other_project,data_root=self.s.data_root,install_root=REPO,
            session_id=self.s.session_id,registry_path=self.s.registry.path,skills_root=self.skills,
            preferences_path=self.root/'preferences.md',memory_root=self.root/'memory2',wiki_root=self.root/'wiki2')
        try:
            with self.assertRaisesRegex(DomainError,'PERMISSION_STORAGE_OWNER_MISMATCH'):
                await other.call('request.begin',{'request_id':'request-1','role':'consult'},host_token=other.host_token)
        finally: await other.formal.shutdown()
        # Back in the original owner/root and still the original request, the
        # originally approved anchor is reusable; the other project gained none.
        self.assertEqual((await self.stat(self.bud,'original-domain'))['status'],'completed')

    async def test_policy_revision_invalidates_read_grant(self):
        p=await self.stat();await self.decide(p,'scope')
        mutate(self.s.data_root,self.s.task_id,lambda s:s['task_state']['execution_permissions'].update(policy_revision='synthetic-next-policy'))
        with self.assertRaisesRegex(ep.PermissionDenied,'UNKNOWN_VERSION'):ep.active_read_grants(self.s.data_root,self.s.task_id)
        with self.assertRaises(ep.PermissionDenied):await self.stat(self.bud,'policy-drift')

    async def test_finished_request_and_request_cancel_invalidate_grants(self):
        p=await self.stat();await self.decide(p,'scope')
        scope=self.s.scope()
        mutate(self.s.data_root,self.s.task_id,lambda s:s['task_state'].update(request_finish={'scope':scope,'status':'completed'}))
        self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])
        with self.assertRaises(ep.PermissionDenied):await self.stat(self.bud,'finished')
        await self.begin('request-next')
        p=await self.stat(self.bud,'next');await self.decide(p,'y')
        await self.s.call('request.cancel',{},host_token=self.s.host_token)
        self.assertEqual(ep.active_read_grants(self.s.data_root,self.s.task_id),[])
        with self.assertRaises(ep.PermissionDenied):await self.stat(self.bud,'cancelled-request')

    async def test_authorized_source_open_rejects_changed_symlink(self):
        p=await self.stat(self.bud,'source');await self.decide(p,'y')
        with ep.open_authorized_source(self.s.files,self.bud,self.s.task_id,'formal-source') as stream:
            self.assertEqual(stream.read(),b'bud fixture\n')
        self.bud.rename(self.inputs/'old-bud.txt');self.bud.symlink_to(self.leaf)
        with self.assertRaises((ep.PermissionDenied,ep.PermissionPending,OSError)):
            with ep.open_authorized_source(self.s.files,self.bud,self.s.task_id,'formal-source') as stream:
                self.fail('changed symbolic source opened')

    async def test_formal_directory_snapshot_reuses_a_grant(self):
        from agent_core.command_execution import FormalProtectionError
        p=await self.stat();await self.decide(p,'scope')
        projection=ep.command_access_projection(self.s.commands,{'cmd':'true','read_paths':[str(self.inputs)]},self.s.task_id,call_id='directory-formal')
        self.assertEqual(len(projection['directory_snapshots']),1)
        self.assertTrue(all(r['target_kind']=='file' for r in projection['reads']))
        self.assertEqual(len(ep.active_read_grants(self.s.data_root,self.s.task_id)),1)
        self.assertEqual((await self.stat(self.bud,'a-still-available'))['status'],'completed')

if __name__=='__main__': unittest.main(verbosity=2)
