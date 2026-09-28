"""Driver-only fixtures. No Node/model/scientific call and no real canary PASS."""
import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('canary_driver',Path(__file__).with_name('run-canaries.py'))
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


class CanaryDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-canary-driver-only-')
        self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.repo=self.root/'repo';self.runtime=self.repo/'integrations/pi_runtime';self.delivery=self.root/'delivery'
        self.runtime.mkdir(parents=True);self.delivery.mkdir()
        for key,value in {'REPO':self.repo,'RUNTIME':self.runtime,'LEDGER':self.runtime/'.runtime/live-canary-20260926.sqlite'}.items():
            change=patch.object(driver,key,value);change.start();self.addCleanup(change.stop)
        no_process=patch.object(driver.subprocess,'Popen',side_effect=AssertionError('DRIVER_TEST_FORBIDS_PROCESS'))
        no_process.start();self.addCleanup(no_process.stop)

    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value,str) else value)

    def test_runner_lock_excludes_another_open_and_releases_without_touching_ledger(self):
        self.write(driver.LEDGER,'PREEXISTING_LEDGER_BYTES_NOT_A_TEST_DATABASE')
        with self.assertRaisesRegex(RuntimeError,'synthetic runner interruption'):
            with driver.exclusive_execution():
                with self.assertRaisesRegex(driver.CanaryError,'CANARY_RUNNER_ALREADY_ACTIVE'):
                    with driver.exclusive_execution():
                        self.fail('another runner entered')
                raise RuntimeError('synthetic runner interruption')
        with driver.exclusive_execution():
            self.assertEqual(driver.LEDGER.read_text(),'PREEXISTING_LEDGER_BYTES_NOT_A_TEST_DATABASE')
        self.assertTrue(driver.LEDGER.with_suffix('.runner.lock').is_file())

    def test_runner_lock_rejects_symlink_and_does_not_create_ledger(self):
        driver.LEDGER.parent.mkdir(parents=True)
        target=self.root/'must-not-change';target.write_text('unchanged')
        driver.LEDGER.with_suffix('.runner.lock').symlink_to(target)
        with self.assertRaisesRegex(driver.CanaryError,'CANARY_RUNNER_LOCK_UNAVAILABLE'):
            with driver.exclusive_execution():
                self.fail('symlink lock accepted')
        self.assertEqual(target.read_text(),'unchanged')
        self.assertFalse(driver.LEDGER.exists())

    def preflight_fixture(self):
        for name in driver.SOURCE_REQUIREMENTS:self.write(self.repo/name,'synthetic-code-only')
        self.write(self.runtime/'package-lock.json','fixed fixture lock')
        package=self.runtime/'node_modules/@earendil-works/pi-coding-agent'
        self.write(package/'package.json',{'version':'0.87.1'})
        self.write(package/'dist/core/model-runtime.js','synthetic-installed-patch')
        self.write(self.runtime/'compat/pi-0.87.1-observer.patch.json',{'runtimeLabel':'fixture-only',
            'files':[{'path':'dist/core/model-runtime.js','patchedSha256':driver.digest(package/'dist/core/model-runtime.js')}]})
        compiled={}
        for key,name in {'extension_dist_sha256':'extensions/biomind','host_dist_sha256':'host',
                         'usage_observer_dist_sha256':'usage-observer','canary_budget_dist_sha256':'canary-budget',
                         'canary_dispatch_dist_sha256':'canary-dispatch'}.items():
            self.write(self.runtime/'dist'/(name+'.js'),'synthetic compiled '+name)
            compiled[key]=driver.digest(self.runtime/'dist'/(name+'.js'))
        self.write(self.runtime/'.runtime/node/bin/node','THIS IS NOT EXECUTABLE AND MUST NEVER RUN')
        self.write(self.delivery/'SOURCE_MANIFEST.json',[{'path':name,'after_sha256':driver.digest(self.repo/name)} for name in driver.SOURCE_REQUIREMENTS])
        self.write(self.delivery/'RUNTIME_LOCK.json',{'verification':{'clean_directory_reinstall':'PASS',**compiled},
            'lock':{'sha256':driver.digest(self.runtime/'package-lock.json')}})
        self.rows=['case_id\trequirement\trequired_stage\tstatus']+[f'fixture{i}\tREQUIRED\tB3_EXIT\tPASS' for i in range(82)]
        self.write(self.delivery/'ACCEPTANCE_RESULTS.tsv','\n'.join(self.rows)+'\n')
        self.write(self.repo/'registry.json',{'environments':{},'tools':{}})
        return argparse.Namespace(delivery=self.delivery,registry=self.repo/'registry.json',scenario='R1',
            provider='existing-provider',model='existing-model',python=Path(__file__).resolve())

    def test_preflight_missing_gate_and_changed_source_fail_without_ledger_creation(self):
        options=self.preflight_fixture()
        self.assertEqual(driver.preflight(options)['status'],'READY_FOR_EXPLICIT_EXECUTE')
        self.assertFalse(driver.LEDGER.exists())

        self.rows[1]='fixture0\tREQUIRED\tB3_EXIT\tPARTIAL'
        self.write(self.delivery/'ACCEPTANCE_RESULTS.tsv','\n'.join(self.rows)+'\n')
        blocked=driver.preflight(options)
        self.assertIn('ENGINEERING_fixture0_PARTIAL',blocked['blockers'])
        self.assertEqual(blocked['real_provider_sends_in_plan'],0)
        self.write(self.repo/driver.SOURCE_REQUIREMENTS[0],'changed')
        self.assertTrue(any(value.startswith('SOURCE_HASH_MISMATCH_') for value in driver.preflight(options)['blockers']))
        self.assertFalse(driver.LEDGER.exists())

    def test_changed_compiled_dispatch_boundary_blocks_preflight(self):
        options=self.preflight_fixture()
        self.write(self.runtime/'dist/canary-dispatch.js','stale or changed dispatcher implementation')
        blocked=driver.preflight(options)
        self.assertIn('RUNTIME_LOCK_canary_dispatch_dist_sha256_NOT_CURRENT',blocked['blockers'])
        self.assertFalse(driver.LEDGER.exists())

    def test_preflight_r3_requires_declared_tool_and_patched_install(self):
        options=self.preflight_fixture();options.scenario='R3'
        self.assertIn('R3_NO_DECLARED_REGISTERED_EXECUTABLE_PREFIX',driver.preflight(options)['blockers'])
        self.write(self.runtime/'node_modules/@earendil-works/pi-coding-agent/dist/core/model-runtime.js','unpatched')
        self.assertIn('COMPAT_PATCH_NOT_APPLIED_dist/core/model-runtime.js',driver.preflight(options)['blockers'])

    def test_existing_executable_and_prefix_do_not_upgrade_registration_status(self):
        binary=self.root/'fake-prefix/bin/samtools';self.write(binary,'SYNTHETIC_NOT_RUN');binary.chmod(0o700)
        registry=self.root/'registry.json'
        document={'environments':{'fixture':{'status':'registered','prefix':str(binary.parents[1])}},
                  'tools':{'samtools':{'env':'fixture','status':'verified'}}}
        self.write(registry,document)
        self.assertIsNone(driver.registered_tool(registry))
        document['environments']['fixture']['status']='verified';self.write(registry,document)
        self.assertEqual(driver.registered_tool(registry)['name'],'samtools')
        document['tools']['samtools']['status']='active';self.write(registry,document)
        self.assertIsNone(driver.registered_tool(registry))

    def approval(self,payload):
        return {'payload':{'request_id':'actual-fixture-ref','action_hash':'fixture-hash',
            'request_scope':{'request_key':'new-fixture'},'payload':payload}}

    def test_r3_approval_exact_command_directory_and_env_only(self):
        specification={'cwd':self.root,'allowed_command':'registered-tool exact isolated args','tool_info':{'prefix':'/registered/prefix'}}
        frame=self.approval({'kind':'command','cwd':str(self.root),'arguments':{
            'cmd':specification['allowed_command'],'env':{'ENV_PREFIX':'/registered/prefix'}}})
        self.assertTrue(driver.approval_allowed(frame,specification))
        for change in ('env','cmd','cwd','argv','second'):
            bad=deepcopy(frame);current=deepcopy(specification);payload=bad['payload']['payload']
            if change=='env':payload['arguments']['env']['UNREGISTERED_ENV']='not allowed'
            elif change=='cmd':payload['arguments']['cmd']+='; other-operation'
            elif change=='cwd':payload['cwd']='/outside'
            elif change=='argv':payload['arguments']['argv']=['other']
            else:current['approved_proposals']=1
            self.assertFalse(driver.approval_allowed(bad,current),change)

    def test_r2_approval_requires_answer_exact_path_content_and_no_second_proposal(self):
        specification={'cwd':self.root,'allowed_write':self.root/'brief.txt','write_content':'synthetic','question_answered':True}
        frame=self.approval({'kind':'file_write','tool':'write_file','path':str(specification['allowed_write']),
            'arguments':{'path':str(specification['allowed_write']),'content':'synthetic','expected_sha256':''}})
        self.assertTrue(driver.approval_allowed(frame,specification))
        for field,value in [('question_answered',False),('approved_proposals',1)]:
            self.assertFalse(driver.approval_allowed(frame,{**specification,field:value}))
        bad=deepcopy(frame);bad['payload']['payload']['path']=str(self.root/'unapproved.txt')
        self.assertFalse(driver.approval_allowed(bad,specification))
        bad=deepcopy(frame);bad['payload']['payload']['arguments']['content']='changed'
        self.assertFalse(driver.approval_allowed(bad,specification))

    def test_result_rejects_fake_scientific_status_bad_counts_and_nonterminal_receipt(self):
        settled={'type':'event','event':{'type':'agent_settled'}}
        result={'runtime_status':'completed','scientific_success':None,'model_conclusion':'observed conceptual answer'}
        before={'R1':0,'total':0};after={'R1':1,'total':1}
        self.assertEqual(driver.validate_result('R1',{},[settled],result,before,after)['actual_sends'],1)
        for bad_result,bad_after in [({**result,'scientific_success':True},after),(result,{'R1':7,'total':7})]:
            with self.assertRaises(driver.CanaryError):driver.validate_result('R1',{},[settled],bad_result,before,bad_after)
        target=self.root/'tiny.fa.fai';target.write_text('chrSynthetic\t100\t14\t100\t101\n')
        specification={'tool_info':{'name':'samtools'},'targets':[target]}
        frames=[{'type':'event','event':{'type':'tool_execution_start','toolName':'run_command'}},settled]
        result.update(model_conclusion='Observed synthetic output '+str(target),
            execution_facts=[{'run_ref':'real-fixture-run','terminal':True,'exit_code':0}])
        self.assertEqual(driver.validate_result('R3',specification,frames,result,{'R3':0,'total':0},{'R3':2,'total':2})['actual_sends'],2)
        result['execution_facts'][0]['terminal']=False
        with self.assertRaisesRegex(driver.CanaryError,'TERMINAL_RECEIPT'):
            driver.validate_result('R3',specification,frames,result,{'R3':0,'total':0},{'R3':2,'total':2})
        self.assertFalse(driver.LEDGER.exists())


if __name__=='__main__':unittest.main()
