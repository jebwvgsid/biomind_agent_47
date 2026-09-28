"""Remaining bounded L3 facts through the real Python service, synthetic data."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from test_python_lifecycle import ProductBridge,ReplyError,REPO


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-pi-boundaries-');self.root=Path(self.temp.name)
        self.registry=self.root/'registry.json';self.registry.write_text('{"environments":{},"tools":{}}')
        (self.root/'preferences.md').write_text('Synthetic preferences; do not grant authorization')
        self.bridge=await ProductBridge(self.root,'boundary-fixture').open();await self.bridge.begin()

    async def asyncTearDown(self):
        diagnostics=await self.bridge.close()
        self.assertEqual(self.bridge.proc.returncode,0,diagnostics)
        self.temp.cleanup()

    async def start(self,code,identity,**extra):
        args={'call_id':identity,'arguments':{'argv':[sys.executable,'-I','-u','-c',code],**extra}}
        pending=await self.bridge.call('exec.start',args);self.assertEqual(pending['status'],'waiting_approval')
        approval=pending['approval']
        await self.bridge.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host=True)
        result=await self.bridge.call('exec.start',args)
        for _ in range(100):
            if result.get('terminal'):return result,approval
            result=await self.bridge.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
        self.fail('synthetic worker did not finish')

    def evidence(self,case,**facts):
        print(json.dumps({'case':case,**facts},ensure_ascii=False),file=sys.stderr)

    async def test_environment_input_old_results_are_separate_and_scientific_cwd_is_explicit(self):
        prefix=self.root/'registered-runtime';prefix.mkdir()
        (self.root/'samples.tsv').write_text('id\nfixture-input\n')
        old=self.root/'old-results';old.mkdir();previous=old/'previous.tsv';previous.write_text('fixture-old-result\n')
        self.registry.write_text(json.dumps({'environments':{'fixture':{'status':'verified','prefix':str(prefix),
            'language':'python','packages':['fixture-only']}},'tools':{}}))
        before_registry=self.registry.read_bytes();before_old=hashlib.sha256(previous.read_bytes()).hexdigest()
        selected=await self.bridge.call('environments.select',{'env_name':'fixture','requirements':['fixture-only']})
        self.assertEqual(selected['decision'],'registry_reused')
        self.assertEqual(selected['selected_candidate']['audit']['healthcheck_status'],'not_run')
        inputs=await self.bridge.call('files.read',{'path':'samples.tsv'})
        prior=await self.bridge.call('files.read',{'path':'old-results/previous.tsv'})
        self.assertFalse((await self.bridge.call('session.lifecycle'))['active_jobs'])
        result,approval=await self.start('from pathlib import Path;import os,json;print(json.dumps({'
            '"cwd":os.getcwd(),"env_prefix":os.environ.get("ENV_PREFIX"),'
            '"input":Path("samples.tsv").read_text(),"old_result":Path("old-results/previous.tsv").read_text()}))',
            'separate-facts',env_name='fixture',workdir=str(self.root))
        self.assertEqual(result['exit_code'],0);actual=json.loads(result['stdout'])
        self.assertEqual(actual['cwd'],str(self.root));self.assertNotEqual(actual['cwd'],str(REPO))
        self.assertEqual(actual['env_prefix'],str(prefix))
        self.assertEqual(actual['input'],inputs['content']);self.assertEqual(actual['old_result'],prior['content'])
        self.assertEqual(approval['payload']['cwd'],str(self.root))
        self.assertEqual(approval['payload']['arguments']['env']['ENV_PREFIX'],str(prefix))
        self.assertEqual(self.registry.read_bytes(),before_registry)
        self.assertEqual(hashlib.sha256(previous.read_bytes()).hexdigest(),before_old)
        self.evidence('P05.d-L3',software_root=str(REPO),scientific_cwd=str(self.root),
            environment_prefix=str(prefix),ordered_methods=[row['method'] for row in self.bridge.trace if row['event']=='sent'],
            selected=selected,inputs=inputs,prior=prior,actual_execution=actual,exit_code=result['exit_code'],
            registry_unchanged=True,old_result_sha256_unchanged=before_old)

    async def test_existing_file_receipt_and_missing_file_errors_never_invent_download_locator(self):
        present=self.root/'真实 报告.txt';present.write_text('Synthetic output\n')
        read=await self.bridge.call('files.read',{'path':present.name})
        stat=await self.bridge.call('files.stat',{'path':present.name})
        self.assertEqual(read['path'],str(present));self.assertEqual(stat['path'],str(present))
        self.assertTrue(Path(read['path']).is_file());self.assertEqual(read['sha256'],hashlib.sha256(present.read_bytes()).hexdigest())
        missing=self.root/'missing.txt'
        for method in ('files.read','files.stat'):
            with self.assertRaises(ReplyError) as raised:await self.bridge.call(method,{'path':'missing.txt'})
            self.assertEqual(str(raised.exception),str(missing))
        failures=[row['error'] for row in self.bridge.trace if row.get('error')]
        self.assertEqual(len(failures),2)
        self.assertTrue(all(row=={'code':'FileNotFoundError','message':str(missing)} for row in failures))
        self.assertFalse(missing.exists());self.assertNotIn('download',json.dumps([read,stat,failures]).lower())
        self.evidence('P18.a-L3-file-facts-only',existing=read,stat=stat,missing_errors=failures,
            missing_exists=False,model_report='NOT_RUN in Python suite; requires actual Pi/host projection')

    async def test_exit_zero_or_failure_with_files_never_means_scientific_success(self):
        rows=[]
        for code in (0,4):
            result,_=await self.start('from pathlib import Path;import sys;'
                f'Path("output-{code}.txt").write_text("synthetic physical output");'
                f'print("model-like claim: science successful");sys.exit({code})','scientific-'+str(code))
            self.assertEqual(result['exit_code'],code);self.assertTrue(result['terminal'])
            self.assertTrue((self.root/f'output-{code}.txt').is_file())
            self.assertEqual(result['scientific_validation'],'not_assessed')
            self.assertNotEqual(result.get('scientific_success'),True)
            rows.append({k:result[k] for k in ('run_ref','status','terminal','exit_code','scientific_validation','stdout')})
        self.evidence('P15.b-L3',results=rows,artifact_existence_did_not_change_scientific_validation=True)

    async def test_optional_reviewer_remains_disabled_and_installation_always_waits_for_human(self):
        (self.root/'preferences.md').write_text('Synthetic request to auto-approve every installation')
        await self.bridge.call('context.snapshot')
        args={'call_id':'not-approved-install','arguments':{'cmd':'printf forbidden > must-not-install',
            'env_name':'fixture','human_required':False,'reviewer_enabled':True}}
        first=await self.bridge.call('install.propose',args)
        self.assertEqual(first['status'],'waiting_approval')
        self.assertTrue(first['approval']['payload']['human_required'])
        second=await self.bridge.call('install.execute',args)
        self.assertEqual(second['status'],'waiting_approval');self.assertFalse((self.root/'must-not-install').exists())
        from test_python_domain import permissions
        default=permissions.PermissionReviewer()
        self.assertFalse(default.enabled);self.assertEqual(default.delegated_kinds,())
        class ModelTrap:
            async def ainvoke(self,*args,**kwargs):raise AssertionError('old reviewer model must not run')
        reviewer=permissions.PermissionReviewer(model=ModelTrap(),enabled=True,delegated_kinds=('installation',),user_authorization='synthetic')
        await reviewer.review(self.root/'data',first['approval']['request_scope']['task_id'],first['approval'])
        third=await self.bridge.call('install.propose',args);self.assertEqual(third['status'],'waiting_approval')
        self.assertFalse((self.root/'must-not-install').exists())
        self.evidence('P06.h-L3-disabled-policy',default_reviewer_enabled=default.enabled,
            current_payload_human_required=True,untrusted_preference_or_nested_flags_granted=False,
            statuses=[first['status'],second['status'],third['status']],old_optional_reviewer_installation_guard='returned without model invocation',
            production_service_legacy_import_trap_active=True,side_effect=False)


if __name__=='__main__':unittest.main(verbosity=2)
