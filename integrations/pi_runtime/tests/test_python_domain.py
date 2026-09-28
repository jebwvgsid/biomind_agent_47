"""Production formal permission/owner/executor policy on synthetic data only.

No executor replacement, no test-only launch policy, no production credentials.
Formal execution is the existing explicitly approved host boundary, not an OS sandbox.
"""
import asyncio
import importlib.abc
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode=True
RUNTIME=Path(__file__).resolve().parents[1]
REPO=RUNTIME.parents[1]
sys.path[:0]=[str(RUNTIME),str(REPO)]


class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='config' or fullname.startswith('config.') or fullname in {
            'bio_agents','agent_core.provider_attempt','agent_core.tool_loop','agent_core.installer','agent_core.memory_maintenance',
            'agent_core.workflow.runtime_graph','agent_core.workflow.pi_rpc','dotenv'}:
            raise ImportError('TEST_FORBIDS_OLD_MODEL_OR_CONFIG:'+fullname)


sys.meta_path.insert(0,Guard())
from domain_adapter.service import DomainService,DomainError
from agent_core import execution_permissions as permissions


class DomainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-pi-domain-')
        self.root=Path(self.temp.name);self.services=[]
        self.preferences=self.root/'preferences.md';self.preferences.write_text('Chinese concise fixture A')
        self.registry=self.root/'registry.json';self.registry.write_text('{"environments":{},"tools":{}}')
        self.skills=self.root/'skills';(self.skills/'different-directory'/'scripts').mkdir(parents=True)
        (self.skills/'different-directory'/'SKILL.md').write_text('---\nname: fixture-canonical\ndescription: Synthetic fixture skill\nruntime:\n  default_env: fixture\n---\nUse scripts/f.py\n')
        (self.skills/'different-directory'/'scripts'/'f.py').write_text('print("fixture")\n')
        self.s=self.service('main')
        await self.begin(self.s,'first','generate')

    def service(self,session):
        s=DomainService(cwd=self.root,data_root=self.root/'data',install_root=REPO,session_id=session,
            skills_root=self.skills,preferences_path=self.preferences,registry_path=self.registry,
            memory_root=self.root/'memory',wiki_root=self.root/'wiki',history_enabled=True)
        self.services.append(s);return s

    async def asyncTearDown(self):
        for s in self.services:
            for event in s._install_cancels.values():event.set()
            await s.formal.shutdown()
            if s.active_jobs():await asyncio.gather(*s.active_jobs(),return_exceptions=True)
        self.temp.cleanup()

    async def begin(self,s,request,role):
        return await s.call('request.begin',{'request_id':request,'role':role},host_token=s.host_token)

    async def approve(self,pending,choice='y',s=None,**extra):
        s=s or self.s;a=pending['approval']
        return await s.call('approval.decide',{**{k:a[k] for k in ('request_id','action_hash','request_scope')},
            'choice':choice,**extra},host_token=s.host_token)

    async def start(self,code,call='call1',s=None):
        s=s or self.s
        args={'call_id':call,'arguments':{'argv':[sys.executable,'-I','-u','-c',code],'workdir':str(self.root)}}
        pending=await s.call('exec.start',args);self.assertEqual(pending['status'],'waiting_approval')
        await self.approve(pending,s=s)
        return args,await s.call('exec.start',args)

    async def terminal(self,result,s=None):
        s=s or self.s
        for _ in range(100):
            if result.get('terminal'):return result
            result=await s.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
        self.fail('synthetic process did not reach terminal')

    async def test_formal_executor_once_receipt_log_and_replay(self):
        code='from pathlib import Path; p=Path("count"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("fixture-receipt")'
        args,result=await self.start(code)
        result=await self.terminal(result)
        self.assertEqual(result['exit_code'],0);self.assertIn('fixture-receipt',result['stdout'])
        self.assertTrue(Path(result['receipt_ref']).is_file());self.assertTrue(Path(result['log_refs']['stdout']).is_file())
        again=await self.s.call('exec.start',args)
        self.assertEqual(again['run_ref'],result['run_ref']);self.assertEqual((self.root/'count').read_text(),'x')
        self.assertNotIn('env',again);self.assertEqual(again['scientific_validation'],'not_assessed')

    async def test_pi_command_description_is_metadata_before_permission_and_formal_dispatch(self):
        args={'call_id':'described-command','arguments':{'argv':[sys.executable,'-I','-u','-c',
            'from pathlib import Path;Path("described-output").write_text("real execution");print("described receipt")'],
            'workdir':str(self.root),'description':'Describe the synthetic job; this is not an executor argument.'}}
        pending=await self.s.call('exec.start',args)
        self.assertEqual(pending['status'],'waiting_approval')
        self.assertNotIn('description',pending['approval']['payload']['arguments'])
        self.assertFalse((self.root/'described-output').exists())
        await self.approve(pending)
        result=await self.terminal(await self.s.call('exec.start',args))
        self.assertEqual(result['exit_code'],0)
        self.assertIn('described receipt',result['stdout'])
        self.assertEqual((self.root/'described-output').read_text(),'real execution')
        self.assertEqual((await self.s.call('exec.start',args))['run_ref'],result['run_ref'])

    async def test_real_log_cursor_pages_reconstruct_complete_file(self):
        _,started=await self.start('import sys;sys.stdout.write("x"*130000);sys.stderr.write("fixture-stderr")','large-log')
        terminal=await self.terminal(started)
        self.assertEqual(len(Path(terminal['log_refs']['stdout']).read_text()),130000)
        self.assertLessEqual(len(terminal['stdout']),12000)
        cursor=None;pieces=[]
        for _ in range(30):
            reply=await self.s.call('exec.read_log',{'run_ref':terminal['run_ref'],'after_cursor':cursor})
            self.assertIsInstance(reply['next_cursor'],str)
            pieces.append(reply['stdout_delta']);cursor=reply['next_cursor']
            if not any(reply['unread_log_bytes'].values()):break
        self.assertEqual(''.join(pieces),Path(terminal['log_refs']['stdout']).read_text())

    async def test_abort_wait_cancel_target_and_eof_preserves_owner(self):
        _,result=await self.start('import time; print("running",flush=True);time.sleep(1)')
        waiter=asyncio.create_task(self.s.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':60000}))
        await asyncio.sleep(.03);waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiter
        self.assertTrue((await self.s.call('session.lifecycle',{}))['active_jobs'])
        # Disconnect waits for the existing process; it does not cancel it.
        await self.s.disconnected()
        end=await self.s.call('exec.status',{'run_ref':result['run_ref']});self.assertEqual(end['exit_code'],0)
        _,other=await self.start('import time;time.sleep(3)','cancelJob')
        stopped=await self.s.call('exec.cancel',{'run_ref':other['run_ref'],'reason':'fixture user cancel'})
        self.assertTrue(stopped.get('terminal'));self.assertTrue(stopped.get('cleanup_confirmed'))
        self.assertNotEqual(stopped.get('exit_code'),0)

    async def test_wrong_scope_and_unknown_reconnect_do_not_launch(self):
        args,result=await self.start('import time;time.sleep(2)')
        stranger=self.service('stranger');await self.begin(stranger,'stranger','generate')
        denied=await stranger.call('exec.cancel',{'run_ref':result['run_ref']})
        self.assertEqual(denied['status'],'not_run')
        reconnected=self.service('main')
        unknown=await reconnected.call('exec.start',args)
        self.assertEqual(unknown['status'],'reconciliation_unknown')
        self.assertFalse(unknown.get('owner_available',False))
        await self.s.call('exec.cancel',{'run_ref':result['run_ref']})

    async def test_host_capability_scope_blank_deny_and_no_model_forgery(self):
        args={'call_id':'denied','arguments':{'argv':[sys.executable,'-I','-c','print("must not run")']}}
        pending=await self.s.call('exec.start',args)
        with self.assertRaises(DomainError):await self.s.call('approval.decide',{'request_id':pending['approval']['request_id'],'choice':'y'})
        blank=await self.approve(pending,choice='');self.assertEqual(blank['status'],'pending')
        await self.approve(pending,choice='n');denied=await self.s.call('exec.start',args)
        self.assertFalse(denied['execution_started']);self.assertFalse(self.s.active_jobs())
        with self.assertRaises(DomainError):await self.s.call('exec.start',{'call_id':'forged','arguments':{'argv':['true'],'approved':True}})
        changed={**pending['approval'],'action_hash':'wrong'}
        with self.assertRaises(DomainError):await self.approve({'approval':changed})

    async def test_file_permission_real_write_replay_and_secret_refusal(self):
        args={'call_id':'write1','path':str(self.root/'result.txt'),'content':'real domain write\n','expected_sha256':''}
        pending=await self.s.call('files.write',args);await self.approve(pending)
        written=await self.s.call('files.write',args);self.assertEqual(written['status'],'completed')
        again=await self.s.call('files.write',args);self.assertTrue(again['replayed'])
        read=await self.s.call('files.read',{'path':str(self.root/'result.txt')});self.assertEqual(read['content'],'real domain write\n')
        offset=await self.s.call('files.read',{'path':str(self.root/'result.txt'),'offset':5});self.assertEqual(offset['content'],'domain write\n')
        with self.assertRaises(ValueError):await self.s.call('files.read',{'path':str(REPO/'.env.api_keys')})

    async def test_registry_reuse_without_package_manager_or_healthcheck(self):
        prefix=self.root/'env';prefix.mkdir()
        self.registry.write_text(json.dumps({'environments':{'fixture':{'status':'verified','prefix':str(prefix),'packages':['abc'],'language':'python'}},'tools':{}}))
        selected=await self.s.call('environments.select',{'env_name':'fixture','requirements':{'packages':['abc']}})
        self.assertEqual(selected['decision'],'registry_reused')
        self.assertEqual(selected['selected_candidate']['audit']['healthcheck_status'],'not_run')
        self.assertFalse(self.s.active_jobs())
        listed=await self.s.call('environments.select',{'env_name':'fixture','requirements':['abc']})
        self.assertEqual(listed['availability'],'AVAILABLE')

    async def test_registered_environment_survives_silence_timeout_cancel_and_nondependency_failure(self):
        prefix=self.root/'env';prefix.mkdir()
        original=json.dumps({'environments':{'fixture':{'status':'verified','prefix':str(prefix),'language':'python'}},'tools':{}})
        self.registry.write_text(original)
        scenarios=[('silent','import time;time.sleep(.1)',None),
                   ('failure','import sys;sys.stderr.write("ordinary synthetic input error");sys.exit(4)',None),
                   ('timeout','import time;time.sleep(2)',150),('cancel','import time;time.sleep(2)',None)]
        for name,code,timeout in scenarios:
            args={'call_id':'registration-'+name,'arguments':{'argv':[sys.executable,'-I','-u','-c',code],'env_name':'fixture'}}
            if timeout:args['arguments']['timeout_ms']=timeout
            pending=await self.s.call('exec.start',args);await self.approve(pending)
            started=await self.s.call('exec.start',args)
            if name=='cancel':result=await self.s.call('exec.cancel',{'run_ref':started['run_ref']})
            else:result=await self.terminal(started)
            if name=='silent':self.assertEqual(result['exit_code'],0)
            elif name=='failure':self.assertEqual(result['exit_code'],4)
            else:self.assertNotEqual(result.get('exit_code'),0)
            self.assertEqual(self.registry.read_text(),original)
            self.assertFalse(self.s._state().get('task_state',{}).get('runtime_invalidations'))
            self.assertEqual((await self.s.call('environments.select',{'env_name':'fixture'}))['availability'],'AVAILABLE')

    async def test_two_session_jobs_cancel_target_preserves_other_and_rejects_wrong_scope(self):
        other=self.service('parallel-session');await self.begin(other,'other-request','generate')
        _,first=await self.start('import time;time.sleep(2)','first-parallel')
        _,second=await self.start('import time;time.sleep(.7);print("other job survived")','second-parallel',s=other)
        denied=await other.call('exec.cancel',{'run_ref':first['run_ref']})
        self.assertEqual(denied['status'],'not_run')
        cancelled=await self.s.call('exec.cancel',{'run_ref':first['run_ref']})
        self.assertTrue(cancelled['cleanup_confirmed'])
        self.assertTrue((await other.call('session.lifecycle',{}))['active_jobs'])
        terminal=await self.terminal(second,s=other)
        self.assertEqual(terminal['exit_code'],0);self.assertIn('other job survived',terminal['stdout'])

    async def test_completed_receipt_restores_in_new_service_without_relaunch(self):
        args,result=await self.start('from pathlib import Path;p=Path("restored-count");p.write_text(p.read_text()+"x" if p.exists() else "x");print("durable terminal")','restorable')
        terminal=await self.terminal(result)
        restarted=self.service('main')
        restored=await restarted.call('exec.start',args)
        self.assertTrue(restored['terminal']);self.assertEqual(restored['exit_code'],0)
        self.assertEqual(restored['run_ref'],terminal['run_ref']);self.assertEqual(restored['receipt_ref'],terminal['receipt_ref'])
        self.assertEqual((self.root/'restored-count').read_text(),'x')
        self.assertFalse(restarted.active_jobs())

    async def test_original_file_scope_grant_reuses_only_covered_paths_and_request(self):
        granted=self.root/'authorized-output';granted.mkdir();outside=self.root/'separate-output';outside.mkdir()
        first={'call_id':'grant-first','path':str(granted/'a.txt'),'content':'a'}
        pending=await self.s.call('files.write',first)
        self.assertIn('scope',pending['approval']['allowed_decisions']);await self.approve(pending,'scope')
        self.assertEqual((await self.s.call('files.write',first))['status'],'completed')
        second={'call_id':'grant-second','path':str(granted/'b.txt'),'content':'b'}
        self.assertEqual((await self.s.call('files.write',second))['status'],'completed')
        third={'call_id':'outside-grant','path':str(outside/'c.txt'),'content':'c'}
        self.assertEqual((await self.s.call('files.write',third))['status'],'waiting_approval')
        self.assertFalse((outside/'c.txt').exists())
        await self.begin(self.s,'new-request','generate')
        fourth={'call_id':'new-scope','path':str(granted/'d.txt'),'content':'d'}
        self.assertEqual((await self.s.call('files.write',fourth))['status'],'waiting_approval')
        self.assertFalse((granted/'d.txt').exists())

    async def test_leaf_runtime_never_imports_old_graph_or_scans_conda(self):
        from unittest.mock import patch
        from agent_core import workflow
        self.assertNotIn('agent_core.workflow.runtime_graph',sys.modules)
        self.assertNotIn('agent_core.workflow.pi_rpc',sys.modules)
        with patch('agent_core.command_execution._runtime_locations',side_effect=AssertionError('conda scan')):
            other=self.service('no-scan');other.commands._refresh_runtime()
        # The domain remains usable after the old StepWorker implementation and
        # its lazy exports are physically retired.
        self.assertNotIn('StepRequest', workflow.__all__)
        with self.assertRaises(AttributeError):
            getattr(workflow, 'StepRequest')
        self.assertFalse((REPO/'agent_core/workflow/step_worker.py').exists())

    async def test_skill_alias_source_layout_and_refresh_preferences(self):
        # The model catalog/body reader now belongs to Pi. Domain retains only
        # persisted environment evidence and explicit host read authorization.
        with self.assertRaises(DomainError):
            await self.s.call('skills.list', {})
        with self.assertRaises(DomainError):
            await self.s.call('skills.read', {'skill_id':'fixture-canonical'})
        before=await self.s.call('context.snapshot',{})
        self.preferences.write_text('Chinese concise fixture B')
        after=await self.s.call('context.snapshot',{});self.assertNotEqual(before['revision'],after['revision'])
        self.preferences.write_text('');empty=await self.s.call('context.snapshot',{})
        self.assertEqual(empty['preferences']['status'],'empty')

    async def test_skill_resources_pagination_and_runtime_preserved(self):
        # Native line paging is covered by native-skills-read.test.ts. The domain
        # only grants exact stat facts, never a second character-pagination API.
        skill_dir=self.skills/'different-directory'
        (self.skills/'.skill_runtime_registry.json').write_text(json.dumps({'skills':{
            'different-directory':{'env':'fixture','status':'success'}}}))
        self.assertEqual(self.s.skills().get_runtime_registration('fixture-canonical')['env'],'fixture')
        result=await self.s.call('files.read_authorize',{'path':str(skill_dir/'scripts/f.py'),
            'trusted_skill_dirs':[str(skill_dir)]},host_token=self.s.host_token)
        self.assertEqual(result['path'],str(skill_dir/'scripts/f.py'))
        self.assertNotIn('content',result)
        self.assertNotIn('sha256',result)

    async def test_skill_resource_escape_and_missing_are_real_errors(self):
        outside=self.root.parent/(self.root.name+'-outside.md');outside.write_text('synthetic outside')
        skill_dir=self.skills/'different-directory'
        (skill_dir/'references-link').symlink_to(outside)
        try:
            for value in [str(outside),str(skill_dir/'references-link')]:
                with self.assertRaises((ValueError,OSError)):
                    await self.s.call('files.read_authorize',{'path':value,'trusted_skill_dirs':[str(skill_dir)]},host_token=self.s.host_token)
            with self.assertRaises(FileNotFoundError):
                await self.s.call('files.read_authorize',{'path':str(skill_dir/'missing.md'),'trusted_skill_dirs':[str(skill_dir)]},host_token=self.s.host_token)
        finally:
            outside.unlink()

    async def test_memory_write_read_forget_and_history_disable(self):
        args={'call_id':'memory1','operation':'create','path':'methods/fixture.md','content':'Synthetic reusable finding'}
        pending=await self.s.call('memory.write',args);await self.approve(pending)
        written=await self.s.call('memory.write',args);self.assertEqual(written['status'],'written')
        ref=written['written'][0]['source_ref']
        read=await self.s.call('memory.read',{'source_ref':ref});self.assertIn('Synthetic reusable finding',read['content'])
        found=await self.s.call('memory.search',{'query':'Synthetic reusable','source':'wiki'})
        self.assertTrue(found['hits'])
        self.assertIn('hits',await self.s.call('memory.search',{'query':'','source':'projects'}))
        self.assertIn(ref,(await self.s.call('context.snapshot',{}))['history_sources'])
        forget={'call_id':'forget','operation':'forget','path':ref.removeprefix('wiki:'),'expected_hash':read['content_hash']}
        pending=await self.s.call('memory.write',forget);await self.approve(pending)
        self.assertEqual((await self.s.call('memory.write',forget))['status'],'written')
        self.assertIn(ref,(await self.s.call('context.snapshot',{}))['revoked_sources'])
        self.assertEqual((await self.s.call('memory.read',{'source_ref':ref}))['status'],'unavailable')
        disabled=await self.s.call('settings.update',{'history_enabled':False},host_token=self.s.host_token)
        self.assertIn(ref,disabled['revoked_sources'])
        self.assertEqual((await self.s.call('memory.read',{'source_ref':ref}))['status'],'disabled')
        self.assertTrue(disabled['preferences']['body'])

    async def test_core_preference_write_uses_single_source_and_version(self):
        snapshot=self.s.preference()
        args={'call_id':'preference','target':'preference','operation':'replace','content':'Updated single preference source',
              'expected_hash':snapshot['source_hash']}
        pending=await self.s.call('memory.write',args);await self.approve(pending)
        result=await self.s.call('memory.write',args);self.assertEqual(result['status'],'written')
        self.assertEqual(Path(result['absolute_path']),self.preferences)
        self.assertEqual(self.s.preference()['body'],'Updated single preference source')
        self.assertTrue((await self.s.call('memory.write',args))['replayed'])
        self.assertFalse(any('Updated single preference source' in p.read_text() for p in (self.root/'wiki').rglob('*.md')))

    async def test_install_edit_stale_reply_execute_verify_register_and_replay(self):
        prefix=self.root/'fixture-env';prefix.mkdir()
        args={'call_id':'install1','arguments':{'cmd':'printf old > install-marker','env_name':'fixture',
            'prefix':str(prefix),'verification_commands':['test -d '+str(prefix)],'packages':['synthetic-only']}}
        pending=await self.s.call('install.propose',args)
        edited=await self.approve(pending,'edit',replacement={'cmd':'printf edited > install-marker'})
        self.assertEqual(edited['status'],'waiting_approval')
        with self.assertRaises(DomainError):await self.approve(pending)
        await self.approve(edited)
        self.assertEqual((await self.approve(edited))['status'],'allowed')
        installed=await self.s.call('install.execute',args)
        self.assertEqual(installed['status'],'registered');self.assertEqual((self.root/'install-marker').read_text(),'edited')
        again=await self.s.call('install.execute',args);self.assertTrue(again['replayed'])
        self.assertEqual(json.loads(self.registry.read_text())['environments']['fixture']['status'],'verified')

    async def test_install_denial_unknown_claim_and_cancel(self):
        args={'call_id':'installDenied','arguments':{'cmd':'touch NEVER','env_name':'fixture'}}
        pending=await self.s.call('install.propose',args);await self.approve(pending,'n')
        result=await self.s.call('install.execute',args);self.assertFalse(result['execution_started']);self.assertFalse((self.root/'NEVER').exists())
        args={'call_id':'installUnknown','arguments':{'cmd':'touch NEVER2','env_name':'fixture'}}
        pending=await self.s.call('install.propose',args);await self.approve(pending)
        row=permissions.permission_state(self.s.data_root,self.s.task_id,pending['approval']['request_id'])
        permissions.claim_permission(self.s.data_root,self.s.task_id,row,'simulated_lost_execution')
        self.assertEqual((await self.s.call('install.execute',args))['status'],'reconciliation_required')
        self.assertFalse((self.root/'NEVER').exists())
        args={'call_id':'installCancelled','arguments':{'cmd':'touch NEVER3','env_name':'fixture'}}
        pending=await self.s.call('install.propose',args);await self.approve(pending,'cancel')
        self.assertFalse((await self.s.call('install.execute',args))['execution_started'])
        self.assertFalse((self.root/'NEVER3').exists())

    async def test_optional_maintenance_real_outbox_no_python_model_and_replay(self):
        sources=[{'source_ref':'pi:fixture:user:1','kind':'user','content':'Reusable public synthetic observation'}]
        with self.assertRaises(DomainError):await self.s.call('memory.maintenance.prepare',{'sources':sources,'consent':True})
        pending=await self.s.call('memory.maintenance.prepare',{'sources':sources},host_token=self.s.host_token)
        self.assertEqual(pending['status'],'needs_confirmation')
        ready=await self.s.call('memory.maintenance.prepare',{'sources':sources,'consent':True},host_token=self.s.host_token)
        self.assertEqual(ready['status'],'ready')
        patch={'operations':[{'op':'create_page','path':'methods/maintained.md','title':'Synthetic increment',
                             'content':'Bound to actual public source.','sources':['pi:fixture:user:1']}]}
        with self.assertRaises(ValueError):
            await self.s.call('memory.maintenance.apply',{'outbox_id':ready['outbox_id'],'patch':{'operations':[
                {'op':'create_page','path':'methods/forbidden.md','sources':['invented-source']}] }},host_token=self.s.host_token)
        args={'outbox_id':ready['outbox_id'],'patch':patch}
        written=await self.s.call('memory.maintenance.apply',args,host_token=self.s.host_token)
        self.assertEqual(written['status'],'written')
        replay=await self.s.call('memory.maintenance.apply',args,host_token=self.s.host_token);self.assertTrue(replay['replayed'])
        self.assertEqual((await self.s.call('memory.maintenance.prepare',{'sources':sources,'consent':True},host_token=self.s.host_token))['status'],'no_increment')
        await self.s.call('settings.update',{'history_enabled':False},host_token=self.s.host_token)
        self.assertEqual((await self.s.call('memory.maintenance.apply',args,host_token=self.s.host_token))['status'],'disabled')

    async def test_maintenance_cancel_fence_and_journal_retry(self):
        await self.s.call('settings.update',{'memory_mode':'auto'},host_token=self.s.host_token)
        ready=await self.s.call('memory.maintenance.prepare',{'sources':[{'source_ref':'pi:fixture:tool:1','kind':'tool_result','content':'No-op synthetic result'}]},host_token=self.s.host_token)
        await self.s.call('request.cancel',{},host_token=self.s.host_token)
        result=await self.s.call('memory.maintenance.apply',{'outbox_id':ready['outbox_id'],'patch':{'operations':[]}},host_token=self.s.host_token)
        self.assertEqual(result['status'],'cancelled')
        outbox=self.s.data_root/'task_outputs'/self.s.task_id/'wiki_lifecycle'/'outbox'/(ready['outbox_id']+'.json')
        self.assertEqual(json.loads(outbox.read_text())['status'],'prepared')

    async def test_maintenance_write_then_lost_ack_replays_journal_once(self):
        ready=await self.s.call('memory.maintenance.prepare',{'consent':True,'sources':[
            {'source_ref':'pi:fixture:lost-ack','kind':'tool_result','content':'Public known synthetic fact'}]},host_token=self.s.host_token)
        args={'outbox_id':ready['outbox_id'],'patch':{'operations':[{'op':'create_page','path':'methods/once.md',
            'title':'Once only','content':'Exactly one journal application.','sources':['pi:fixture:lost-ack']}]}}
        real_apply=self.s.wiki().apply_patch_result
        def lose_ack(*a,**k):
            real_apply(*a,**k)
            raise OSError('synthetic acknowledgement loss after actual Wiki write')
        self.s.wiki().apply_patch_result=lose_ack
        result=await self.s.call('memory.maintenance.apply',args,host_token=self.s.host_token)
        self.assertEqual(result['status'],'outcome_unknown')
        self.s.wiki().apply_patch_result=real_apply
        replay=await self.s.call('memory.maintenance.apply',{**args,'patch':{'operations':[]}},host_token=self.s.host_token)
        self.assertIn(replay['status'],{'written','noop'})
        text=next((self.root/'wiki').rglob('once.md')).read_text()
        self.assertEqual(text.count('Exactly one journal application.'),1)

    async def test_install_waiter_abort_preserves_owner_then_target_cancel(self):
        prefix=self.root/'cancel-env';prefix.mkdir()
        args={'call_id':'active-install','arguments':{'cmd':'sleep 3; printf late > install-must-not-finish','env_name':'cancel-fixture',
            'prefix':str(prefix),'verification_commands':['test -d '+str(prefix)]}}
        pending=await self.s.call('install.propose',args);await self.approve(pending)
        waiting=asyncio.create_task(self.s.call('install.execute',args));await asyncio.sleep(.05)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiting
        self.assertTrue(self.s.active_jobs())
        with self.assertRaisesRegex(DomainError,'ACTIVE_JOB'):await self.begin(self.s,'forbidden-new-request','consult')
        await self.s.call('exec.cancel',{'run_ref':'install:active-install'})
        await self.s.disconnected()
        result=await self.s.call('install.execute',args)
        self.assertEqual(result['status'],'cancelled');self.assertTrue(result['replayed'])
        self.assertFalse((self.root/'install-must-not-finish').exists())

    async def test_explicit_archive_and_current_scope_preserve_existing_store(self):
        from memory.session_archive import SessionArchive
        self.s.wiki();path=self.root/'synthetic-archive.sqlite';archive=SessionArchive(path)
        rowid=archive.append_event('fixture-old','fixture-session','generate','tool_result','Synthetic archived observation',project_id=self.s.project_id)
        self.s.archive_path=path
        found=await self.s.call('memory.search',{'source':'archive','query':'Synthetic','scope':'current'})
        self.assertEqual(found['hits'][0]['source_ref'],'session_archive:'+str(rowid))
        read=await self.s.call('memory.read',{'source_ref':found['hits'][0]['source_ref'],'scope':'current'})
        self.assertIn('archived observation',read['content'])
        self.assertEqual(self.s.requested_scope('unassigned'),'default')
        self.assertEqual((await self.s.call('context.snapshot',{}))['project_id'],self.s.project_id)

    async def test_real_dependency_receipt_invalidates_registration_not_silence(self):
        prefix=self.root/'env';prefix.mkdir()
        self.registry.write_text(json.dumps({'environments':{'fixture':{'status':'verified','prefix':str(prefix),'language':'python'}},'tools':{}}))
        args={'call_id':'dependency','arguments':{'argv':[sys.executable,'-I','-c','import missing_pi_synthetic_module_1029'], 'env_name':'fixture'}}
        pending=await self.s.call('exec.start',args);await self.approve(pending)
        result=await self.terminal(await self.s.call('exec.start',args))
        self.assertNotEqual(result['exit_code'],0)
        selected=await self.s.call('environments.select',{'env_name':'fixture'})
        self.assertEqual(selected['decision'],'execution_dependency_failed')
        self.assertEqual(selected['invalidation']['missing_dependency'],'missing_pi_synthetic_module_1029')

    async def test_product_formal_permission_required_before_dispatch(self):
        production=self.service('actual-product');await self.begin(production,'permission','generate')
        result=await production.call('exec.start',{'call_id':'probe','arguments':{'argv':['/usr/bin/true']}})
        self.assertEqual(result['status'],'waiting_approval');self.assertFalse(result['execution_started'])
        self.assertFalse(production.active_jobs())

    async def test_current_request_fence_before_any_late_side_effect(self):
        args={'call_id':'late-file','path':str(self.root/'late.txt'),'content':'must not write','active_request_id':'first'}
        pending=await self.s.call('files.write',args);await self.approve(pending)
        await self.begin(self.s,'next-request','generate')
        with self.assertRaises(DomainError):await self.s.call('files.write',args)
        self.assertFalse((self.root/'late.txt').exists())

    async def test_manifest_actual_100_to_101_unicode_space_and_unbuffered_argv(self):
        folder=self.root/'合成 数据';folder.mkdir();manifest=folder/'样本 清单.tsv'
        manifest.write_text('sample\n'+''.join(f'sample{i}\n' for i in range(100)))
        code='from pathlib import Path; p=Path("样本 清单.tsv"); print(len(p.read_text().splitlines())-1)'
        for count in (100,101):
            if count==101:manifest.write_text(manifest.read_text()+'sample100\n')
            args={'call_id':f'manifest-{count}','arguments':{'argv':[sys.executable,'-I','-u','-c',code],'workdir':str(folder)}}
            pending=await self.s.call('exec.start',args);await self.approve(pending)
            result=await self.terminal(await self.s.call('exec.start',args))
            self.assertEqual(result['stdout'].strip(),str(count));self.assertEqual(result['exit_code'],0)
        self.assertFalse(self.s._state().get('steps'))

    async def test_preference_missing_empty_unreadable_and_agents_escape(self):
        fallback=self.root/'memory'/'USER_PROFILE.md';fallback.parent.mkdir(exist_ok=True);fallback.write_text('fallback preference')
        self.preferences.unlink();missing=self.s.preference()
        self.assertEqual(missing['source_kind'],'core_fallback')
        self.preferences.write_text('');self.assertEqual(self.s.preference()['status'],'empty')
        self.preferences.unlink();self.preferences.mkdir()
        unreadable=self.s.preference();self.assertEqual(unreadable['status'],'unreadable');self.assertNotEqual(unreadable['source_kind'],'core_fallback')
        with tempfile.TemporaryDirectory(prefix='pi-agents-outside-') as outside:
            file=Path(outside)/'AGENTS.md';file.write_text('outside sentinel')
            (self.root/'AGENTS.md').symlink_to(file)
            context=self.s.context_snapshot();self.assertNotIn('agents',context)
            self.assertNotIn('outside sentinel',json.dumps(context))


if __name__=='__main__':unittest.main(verbosity=2)
