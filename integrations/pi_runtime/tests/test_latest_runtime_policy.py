"""Latest W1/W2/W3 policy: real domain and formal owner, synthetic data only."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('latest_domain_fixture',HERE/'test_python_domain.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
from agent_core.skill_runtime_metadata import SkillRuntimeRegistry


class LatestRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t=fixture.DomainTests();await self.t.asyncSetUp()
        self.s=self.t.s;self.root=self.t.root
        self.prefix=self.root/'登记 环境';(self.prefix/'bin').mkdir(parents=True)
        self.loaded=[{'name':'fixture-canonical','file_path':str(self.t.skills/'different-directory/SKILL.md')}]
        self.skill_file=self.t.skills/'.skill_runtime_registry.json'
        self.skill_file.write_text(json.dumps({'skills':{'different-directory':{'env':'fixture','status':'success'}}}))
        self.register({'prefix':str(self.prefix)})

    async def asyncTearDown(self):await self.t.asyncTearDown()

    def register(self,row):
        self.t.registry.write_text(json.dumps({'environments':{'fixture':row},'tools':{}}))

    async def select(self,**params):
        return await self.s.call('environments.select',{'loaded_skills':self.loaded,**params},host_token=self.s.host_token)

    async def test_E01_E04_native_identity_skill_only_reuses_registered_environment(self):
        selected=await self.select(skill_id='fixture-canonical')
        self.assertEqual(selected['availability'],'AVAILABLE')
        self.assertEqual(selected['selected_candidate']['env'],'fixture')
        self.assertEqual(selected['selected_candidate']['prefix'],str(self.prefix))
        self.assertEqual(selected['skill_resolution']['skill_id'],'different-directory')
        self.assertEqual(self.s.skills().get_runtime_registration('fixture-canonical',loaded_skills=self.loaded)['env'],'fixture')

    async def test_E02_relative_basename_and_existing_alias_are_compatible(self):
        folder=self.t.skills/'3d-dna/3d-dna-analysis';folder.mkdir(parents=True)
        (folder/'SKILL.md').write_text('---\nname: chosen-native\ndescription: synthetic\n---\n')
        self.loaded=[{'name':'chosen-native','file_path':str(folder/'SKILL.md')}]
        self.skill_file.write_text(json.dumps({'skills':{'3d-dna/3d-dna-analysis':{'env':'fixture'}}}))
        refs=[]
        for name in ['chosen-native','3d-dna/3d-dna-analysis','3d-dna-analysis','hic-analysis']:
            value=await self.select(skill_id=name);refs.append(value['selected_candidate']['source_ref'])
            self.assertEqual(value['selected_candidate']['env'],'fixture')
        self.assertEqual(len(set(refs)),1)

    async def test_E03_host_selected_native_and_ambiguous_basename(self):
        for group,name in [('one','alpha'),('two','beta')]:
            folder=self.t.skills/group/'shared';folder.mkdir(parents=True)
            (folder/'SKILL.md').write_text('synthetic')
            self.loaded.append({'name':name,'file_path':str(folder/'SKILL.md')})
        self.skill_file.write_text(json.dumps({'skills':{'one/shared':{'env':'fixture'},'two/shared':{'env':'other'}}}))
        selected=await self.select(skill_id='alpha');self.assertEqual(selected['selected_candidate']['env'],'fixture')
        ambiguous=await self.select(skill_id='shared')
        self.assertEqual(ambiguous['reason_code'],'SKILL_RUNTIME_ALIAS_AMBIGUOUS')
        self.assertEqual(set(ambiguous['candidates']),{'one/shared','two/shared'})
        self.skill_file.write_text(json.dumps({'skills':{'one/shared':{'env':'fixture'},'alpha':{'env':'other'}}}))
        conflict=await self.select(skill_id='alpha')
        self.assertEqual(conflict['reason_code'],'SKILL_RUNTIME_REGISTRATION_AMBIGUOUS')

        self.skill_file.write_text(json.dumps({'skills':{'shared':{'env':'fixture'}}}))
        self.assertEqual((await self.select(skill_id='alpha'))['reason_code'],'SKILL_RUNTIME_REGISTRATION_AMBIGUOUS')

    async def test_E05_unknown_and_known_without_registration_are_not_no_requirements(self):
        unknown=await self.select(skill_id='unknown');self.assertEqual(unknown['reason_code'],'SKILL_UNKNOWN')
        self.skill_file.write_text('{"skills":{}}')
        known=await self.select(skill_id='fixture-canonical')
        self.assertEqual(known['reason_code'],'SKILL_RUNTIME_NOT_REGISTERED')
        for row in [unknown,known]:
            self.assertEqual(row['availability'],'UNKNOWN');self.assertFalse(row['install_required'])
        # No current persisted schema marks a Skill explicitly environment-free.
        # Empty metadata is not repurposed as such a declaration.

    async def test_E06_explicit_target_overrides_default_without_skill_requirement(self):
        other=self.root/'other env';other.mkdir()
        self.t.registry.write_text(json.dumps({'environments':{'fixture':{'prefix':str(self.prefix)},'chosen':{'prefix':str(other)}}}))
        for query in [{'skill_id':'fixture-canonical','env_name':'chosen'},{'env_name':'chosen'},
                      {'skill_id':'fixture-canonical','requirements':{'env':'chosen'}}]:
            row=await self.select(**query);self.assertEqual(row['selected_candidate']['env'],'chosen')
            self.assertEqual(row['selected_candidate']['prefix'],str(other))

    async def test_E07_known_version_conflict_unknown_inventory_no_probe(self):
        self.register({'prefix':str(self.prefix),'packages':['abc==1.0']})
        row=await self.select(env_name='fixture',requirements={'packages':['abc>=2']})
        self.assertEqual(row['reason_code'],'REGISTERED_RUNTIME_REQUIREMENT_CONFLICT')
        row=await self.select(env_name='fixture',requirements={'packages':['unknown>=1'],'tools':['not-recorded'],'capabilities':['unknown']})
        self.assertEqual(row['availability'],'AVAILABLE')
        self.assertEqual(row['selected_candidate']['requirements_status'],'unknown')
        self.assertEqual(row['selected_candidate']['unknown_packages'],['unknown>=1'])

        self.register({'prefix':str(self.prefix),'versions':{'abc':'1.0'}})
        row=await self.select(env_name='fixture',requirements={'packages':['abc>=2']})
        self.assertEqual(row['reason_code'],'REGISTERED_RUNTIME_REQUIREMENT_CONFLICT')

    async def test_E08_E09_old_health_failures_and_authoritative_revocation(self):
        for record in [{'prefix':str(self.prefix)}, {'prefix':str(self.prefix),'status':'failed','last_verified_at':'1998-01-01'},
                       {'prefix':str(self.prefix),'status':'invalid','invalidated':True}]:
            self.register(record);before=self.t.registry.read_bytes()
            row=await self.select(skill_id='fixture-canonical')
            self.assertEqual(row['availability'],'AVAILABLE')
            self.assertEqual(row['selected_candidate']['audit']['healthcheck_status'],'not_run')
            self.assertEqual(self.t.registry.read_bytes(),before)
        for status in ['disabled','revoked','removed']:
            self.register({'prefix':str(self.prefix),'status':status})
            self.assertEqual((await self.select(env_name='fixture'))['reason_code'],'REGISTERED_RUNTIME_REVOKED')
        self.register({'prefix':str(self.prefix)})
        self.skill_file.write_text(json.dumps({'skills':{'different-directory':{'env':'fixture','prefix':str(self.root/'other')}}}))
        self.assertEqual((await self.select(skill_id='fixture-canonical'))['reason_code'],'REGISTERED_RUNTIME_LOCATION_AMBIGUOUS')
        self.skill_file.write_text(json.dumps({'skills':{'different-directory':{'env':'fixture','prefix':str(self.prefix)+'/'}}}))
        self.assertEqual((await self.select(skill_id='fixture-canonical'))['availability'],'AVAILABLE')
        self.register({"status":"verified"});self.skill_file.write_text('{"skills":{}}')
        self.assertEqual((await self.select(env_name='fixture'))['reason_code'],'REGISTERED_RUNTIME_LOCATION_UNRESOLVED')

    async def test_E10_passive_query_has_no_process_probe_scan_or_registration_write(self):
        before={p:p.read_bytes() for p in [self.t.registry,self.skill_file]}
        with patch('subprocess.Popen',side_effect=AssertionError('NO_QUERY_PROCESS')), \
             patch('asyncio.create_subprocess_exec',side_effect=AssertionError('NO_QUERY_EXEC')), \
             patch('asyncio.create_subprocess_shell',side_effect=AssertionError('NO_QUERY_SHELL')), \
             patch.object(Path,'rglob',side_effect=AssertionError('NO_SKILL_RESCAN')), \
             patch.object(self.s.registry,'_known_environment_prefixes',side_effect=AssertionError('NO_UNNECESSARY_INVENTORY')):
            row=await self.select(skill_id='fixture-canonical')
        self.assertEqual(row['availability'],'AVAILABLE');self.assertFalse(self.s.active_jobs())
        for p,body in before.items():self.assertEqual(p.read_bytes(),body)

    async def test_E11_missing_registered_prefix_is_exposed_by_real_execution(self):
        missing=self.root/'missing env';self.register({'prefix':str(missing)})
        selected=await self.select(env_name='fixture');self.assertEqual(selected['availability'],'AVAILABLE')
        args={'call_id':'missing-program','arguments':{'argv':[str(missing/'bin/not-here')],'env_name':'fixture','workdir':str(self.root)}}
        try:
            pending=await self.s.call('exec.start',args)
            if pending.get('status')=='waiting_approval':
                await self.t.approve(pending);out=await self.s.call('exec.start',args)
                if out.get('run_ref'):out=await self.t.terminal(out)
            else:out=pending
            self.assertNotEqual(out.get('exit_code'),0)
            self.assertNotEqual(out.get('status'),'completed')
        except (ValueError,FileNotFoundError) as error:self.assertTrue(str(error))
        self.assertFalse(missing.exists())

    async def test_E13_reload_scope_no_model_filepath_or_symlink_escape(self):
        self.assertEqual((await self.select(skill_id='fixture-canonical'))['availability'],'AVAILABLE')
        self.loaded=[{**self.loaded[0],'name':'new-native'}]
        self.assertEqual((await self.select(skill_id='fixture-canonical'))['reason_code'],'SKILL_UNKNOWN')
        self.assertEqual((await self.select(skill_id='new-native'))['availability'],'AVAILABLE')
        with self.assertRaisesRegex(ValueError,'HOST_CHANNEL_REQUIRED'):
            await self.s.call('environments.select',{'skill_id':'new-native','loaded_skills':self.loaded})
        with self.assertRaisesRegex(ValueError,'FIELDS_INVALID'):
            await self.s.call('environments.select',{'skill_id':'new-native','filePath':self.loaded[0]['file_path']})
        outside=self.root/'outside';outside.mkdir();(outside/'SKILL.md').write_text('outside')
        linked=self.t.skills/'link';linked.symlink_to(outside,target_is_directory=True)
        self.loaded=[{'name':'outside','file_path':str(linked/'SKILL.md')}]
        self.assertEqual((await self.select(skill_id='outside'))['reason_code'],'SKILL_UNKNOWN')
        self.loaded=[];self.assertEqual((await self.select(skill_id='new-native'))['reason_code'],'SKILL_UNKNOWN')

    async def test_M01_M02_M03_M05_no_science_gate_and_explicit_business_check(self):
        args,run=await self.t.start('from pathlib import Path;Path("bad.bed").write_text("chr1\\t10\\t2\\n");print("rc zero, bad data")',call='bad-bed')
        run=await self.t.terminal(run)
        self.assertEqual(run['exit_code'],0);self.assertEqual(run['scientific_validation'],'not_assessed')
        self.assertFalse(self.s.active_jobs())
        read=await self.s.call('files.read',{'path':str(self.root/'bad.bed')});self.assertIn('chr1',read['content'])
        _,check=await self.t.start('from pathlib import Path;row=Path("bad.bed").read_text().split();print("interval_invalid",int(row[1])>int(row[2]))',call='requested-check')
        check=await self.t.terminal(check);self.assertIn('interval_invalid True',check['stdout'])
        self.assertEqual(len(self.s.formal._owners),2)
        self.assertFalse((self.root/'report.md').exists());self.assertFalse((self.root/'reproduce.sh').exists())

    async def test_E12_M04_history_error_does_not_force_diagnosis_or_retry(self):
        self.register({'prefix':str(self.prefix),'status':'failed'})
        _,run=await self.t.start('import sys;sys.stderr.write("real failure");sys.exit(7)',call='failed')
        run=await self.t.terminal(run);self.assertEqual(run['exit_code'],7)
        row=await self.select(env_name='fixture');self.assertEqual(row['availability'],'AVAILABLE')
        _,check=await self.t.start('import sys;print(sys.version_info.major)',call='user-requested-check')
        self.assertEqual((await self.t.terminal(check))['exit_code'],0)
        self.assertEqual(len(self.s.formal._owners),2)

    async def test_R01_R02_R03_G01_frozen_script_projection_and_scope(self):
        source=self.root/'实际 脚本.py';body='print("REAL_SCRIPT_V1")\n'+'# retained evidence\n'*800
        source.write_text(body)
        args={'call_id':'frozen-script','arguments':{'script_path':str(source),'script_args':[],'interpreter':sys.executable,'workdir':str(self.root)}}
        pending=await self.s.call('exec.start',args);await self.t.approve(pending)
        run=await self.t.terminal(await self.s.call('exec.start',args));self.assertEqual(run['exit_code'],0)
        source.write_text('print("UNEXECUTED_V2")\n')
        meta=(await self.s.call('exec.status',{'run_ref':run['run_ref'],'record':'command'}))['execution_record']
        parsed=json.loads(meta['content']);self.assertEqual(parsed['cwd'],str(self.root));self.assertNotIn('env',parsed)
        self.assertEqual(parsed['scripts'][0]['original_path'],str(source))
        pages=[];offset=0
        while True:
            page=(await self.s.call('exec.status',{'run_ref':run['run_ref'],'record':'script','offset':offset,'max_chars':700}))['execution_record']
            self.assertEqual(page['status'],'available');pages.append(page['content'])
            if page['next_offset'] is None:break
            self.assertTrue(page['truncated']);offset=page['next_offset']
        self.assertEqual(''.join(pages),body);self.assertNotIn('UNEXECUTED_V2',''.join(pages))
        other=self.t.service('other');await self.t.begin(other,'other','generate')
        with self.assertRaises(ValueError):await other.call('exec.status',{'run_ref':run['run_ref'],'record':'command'})
        with self.assertRaisesRegex(ValueError,'PROTECTED_RUNTIME_PATH'):
            await self.s.call('files.read',{'path':run['receipt_ref']})
        frozen=Path(run['receipt_ref']).parent/'frozen'
        record=self.s.formal._owners[run['run_ref']].result
        script=Path(record['frozen_files'][0]['path']);script.chmod(0o700);script.write_text('changed')
        broken=(await self.s.call('exec.status',{'run_ref':run['run_ref'],'record':'script'}))['execution_record']
        self.assertEqual(broken['reason'],'RUN_SCRIPT_CHANGED')

    async def test_G01_record_projection_redacts_secret_shapes_and_omits_environment(self):
        marker='sk-'+'syntheticsecretmarker000000'
        _,run=await self.t.start('secret='+repr(marker)+';print("nonsecret-output")',call='redacted-record')
        run=await self.t.terminal(run)
        raw=self.s.formal._owners[run['run_ref']].result
        self.assertIn(marker,raw['cmd'])
        projected=(await self.s.call('exec.status',{'run_ref':run['run_ref'],'record':'command'}))['execution_record']
        self.assertTrue(projected['redacted']);self.assertNotIn(marker,projected['content'])
        data=json.loads(projected['content']);self.assertNotIn('env',data);self.assertNotIn('PATH',data)
        self.assertIn('[REDACTED]',projected['content'])
        self.assertIn(marker,raw['cmd'])  # projection must not rewrite the actual receipt

    async def test_R03_missing_script_and_running_record_are_explicit(self):
        _,run=await self.t.start('import time;print("active",flush=True);time.sleep(.5)',call='active-record')
        now=await self.s.call('exec.status',{'run_ref':run['run_ref'],'record':'command'})
        if not now['terminal']:self.assertEqual(now['execution_record']['reason'],'EXECUTION_RECORD_NOT_TERMINAL')
        run=await self.t.terminal(run)
        missing=await self.s.call('exec.status',{'run_ref':run['run_ref'],'record':'script'})
        self.assertEqual(missing['execution_record']['reason'],'FROZEN_SCRIPT_NOT_RECORDED')
        with self.assertRaises(ValueError):await self.s.call('exec.status',{'run_ref':'attempt-'+'0'*24,'record':'command'})


if __name__=='__main__':unittest.main(verbosity=2)
