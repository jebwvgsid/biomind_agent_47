"""Current-code witnesses. Synthetic files only; provider/config imports forbidden.

Assertions describe observed behavior, including defects, not future acceptance PASS.
Run with the installed BioMind Python and -B. Output contains no ambient env values.
"""
import asyncio
import importlib.abc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/'integrations/pi_runtime')]
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'config' or fullname.startswith('config.') or fullname in {
            'dotenv','bio_agents','agent_core.installer','agent_core.provider_attempt','agent_core.tool_loop'}:
            raise ImportError('AUDIT_FORBIDS_CONFIG_OR_MODEL:' + fullname)
sys.meta_path.insert(0, Guard())
from domain_adapter.service import DomainService
from domain_adapter.registry import RegistryView, registry_path
from agent_core.registered_runtime import registered_runtime_reuse
from package_manager import PackageManager

OBS = {}
class Witnesses(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-audit-v2-')
        self.root=Path(self.tmp.name); self.project=self.root/'project';self.project.mkdir()
        self.prefix=self.root/'envs/rna_fixture';(self.prefix/'bin').mkdir(parents=True)
        exe=self.prefix/'bin/Rscript';exe.write_text('#!/bin/sh\nprintf "SYNTHETIC_RSCRIPT:%s\\n" "$R_LIBS_USER"\n');exe.chmod(0o755)
        self.reg=self.root/'registry.json';self.inventory=self.root/'inventory.txt';self.inventory.write_text(str(self.prefix)+'\n')
        self.reg.write_text(json.dumps({'environments':{'rna_fixture':{'env':'rna_fixture','prefix':str(self.prefix),
            'status':'registered','packages':['R'],'capabilities':['ora'],'env_runner':'env R_LIBS_USER=/synthetic/library Rscript'}},'tools':{'Rscript':{'env':'rna_fixture','status':'registered'}}}))
        self.prefs=self.root/'preferences.md';self.prefs.write_text('synthetic')
        self.services=[];self.s=self.service('first')
        await self.s.call('request.begin',{'request_id':'r1','role':'consult'},host_token=self.s.host_token)

    def service(self, sid):
        s=DomainService(cwd=self.project,data_root=self.root/'state',install_root=ROOT,session_id=sid,
            registry_path=self.reg,environment_inventory=self.inventory,skills_root=self.root/'skills',
            preferences_path=self.prefs,execution_profile='native')
        self.services.append(s);return s

    async def asyncTearDown(self):
        for s in self.services:
            for e in s._install_cancels.values():e.set()
            await s.formal.shutdown()
            if s.active_jobs():await asyncio.gather(*s.active_jobs(),return_exceptions=True)
        self.tmp.cleanup()

    async def terminal(self, value):
        for _ in range(100):
            if value.get('terminal'):return value
            value=await self.s.call('exec.wait',{'run_ref':value['run_ref'],'max_wait_ms':100})
        raise AssertionError('bounded fixture wait exhausted')

    async def test_selection_and_managed_execution(self):
        with patch('subprocess.Popen',side_effect=AssertionError('hidden probe')) as popen:
            first=self.s.select_environment({'tools':['Rscript'],'capabilities':['ora']},env_name='rna_fixture')
            second=self.s.select_environment({'tools':['Rscript'],'capabilities':['ora']},env_name='rna_fixture')
            other=self.service('second').select_environment({},env_name='rna_fixture')
            self.assertEqual(popen.call_count,0)
        self.assertEqual(first['availability'],'AVAILABLE')
        self.assertEqual(first['selected_candidate']['audit']['healthcheck_status'],'not_run')
        self.assertEqual(len(self.s._state()['task_state']['runtime_evidence']['records']),1)
        with patch.dict(os.environ, {'PATH':'/usr/bin:/bin'}, clear=False):
            result=await self.terminal(await self.s.call('exec.start',{'call_id':'run','arguments':{'cmd':'Rscript','workdir':str(self.project)}}))
        self.assertEqual(result.get('exit_code'),127,result)
        with patch.dict(os.environ, {'PATH':'/usr/bin:/bin'}, clear=False):
            result=await self.terminal(await self.s.call('exec.start',{'call_id':'explicit-run','arguments':{'cmd':'Rscript','workdir':str(self.project),'env_name':'rna_fixture'}}))
        self.assertEqual(result.get('exit_code'),0,result)
        output=Path(result['log_refs']['stdout']).read_text()
        self.assertIn('SYNTHETIC_RSCRIPT:',output)
        self.assertNotIn('/synthetic/library',output)
        OBS['selection_managed']={'selection':'AVAILABLE','healthcheck':'not_run','subprocesses_during_selection':0,
            'same_task_records_before_execution':1,'second_session':'AVAILABLE','managed_without_env_name_exit_code':127,
            'managed_with_env_name_exit_code':0,'persisted_selection_automatically_consumed':False,
            'registered_env_runner_applied':False,
            'note':'native run_command needs explicit env_name; prefix then applied; env_runner/R_LIBS_USER not materialized'}
        child=subprocess.run([sys.executable,'-B',str(Path(__file__)),'--read-registry',str(self.reg),str(self.inventory)],capture_output=True,text=True,check=True)
        self.assertEqual(child.stdout.strip(),'AVAILABLE:not_run')
        OBS['selection_managed']['fresh_process']=child.stdout.strip()

    async def test_metadata_limits_and_errors(self):
        v=RegistryView(self.reg,self.inventory)
        unknown=registered_runtime_reuse(v,{'tools':['unrecorded'],'packages':['fgsea']},skill_record={},target_env='rna_fixture')
        self.assertEqual(unknown['availability'],'AVAILABLE');self.assertEqual(unknown['selected_candidate']['requirements_status'],'unknown')
        explicit=registered_runtime_reuse(v,{'prefix':str(self.root/'different')},skill_record={},target_env='rna_fixture')
        self.assertEqual(explicit['selected_candidate']['prefix'],str(self.prefix))
        no_target=registered_runtime_reuse(v,{'tools':['Rscript']},skill_record={})
        self.assertEqual(no_target['reason_code'],'REGISTERED_RUNTIME_TARGET_MISSING')
        missing=RegistryView(self.root/'missing.json',self.inventory)
        self.assertEqual(missing.registry,{'environments':{},'tools':{}})
        self.reg.write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):v.reload()
        pm=PackageManager.__new__(PackageManager);pm.registry_path=self.reg
        self.assertEqual(pm._load_registry(),{'environments':{},'tools':{}})
        self.assertEqual(registry_path(ROOT,environment={}),ROOT/'software_registry.json')
        OBS['metadata_limits']={'unknown_requirement_returns':'AVAILABLE + requirements_status=unknown',
            'requirements_prefix_honored':False,'tool_only_query':'REGISTERED_RUNTIME_TARGET_MISSING',
            'missing_file':'empty view (no file created)','malformed_pi':'JSONDecodeError',
            'malformed_legacy':'empty registry','default_path':'install_root/software_registry.json'}

    async def test_inventory_only_and_ambiguity(self):
        v=RegistryView(self.reg,self.inventory); v.registry={'environments':{},'tools':{}}
        result=registered_runtime_reuse(v,{},skill_record={},target_env='rna_fixture')
        self.assertEqual(result['reason_code'],'REGISTERED_RUNTIME_NOT_FOUND')
        p2=self.root/'other/rna_fixture';p2.mkdir(parents=True)
        self.inventory.write_text(str(self.prefix)+'\n'+str(p2)+'\n')
        v.registry={'environments':{'rna_fixture':{'env':'rna_fixture','status':'registered'}},'tools':{}}
        result=registered_runtime_reuse(v,{},skill_record={},target_env='rna_fixture')
        self.assertEqual(result['reason_code'],'REGISTERED_RUNTIME_LOCATION_AMBIGUOUS')
        OBS['inventory']={'inventory_only':'REGISTERED_RUNTIME_NOT_FOUND','duplicate_name':'REGISTERED_RUNTIME_LOCATION_AMBIGUOUS'}

    async def approved_install(self, call='install'):
        args={'call_id':call,'arguments':{'cmd':'SYNTHETIC_INSTALL','env_name':'installed_fixture',
            'prefix':str(self.prefix),'description':'mock installation','packages':['R'],
            'verification_commands':['SYNTHETIC_VERIFY']}}
        pending=await self.s.call('install.propose',args);a=pending['approval']
        await self.s.call('approval.decide',{**{k:a[k] for k in ['request_id','action_hash','request_scope']},'choice':'y'},host_token=self.s.host_token)
        return args

    async def test_install_success_persistence_and_context(self):
        args=await self.approved_install();seen=[]
        async def fake(code, **kwargs):
            seen.append({'code':code,'env_supplied':bool(kwargs.get('env'))});return ('synthetic','',0)
        with patch('execution.executor._run_native_command',side_effect=fake):
            result=await self.s.call('install.execute',args)
        self.assertEqual(result['status'],'registered')
        fresh=self.service('after-install').select_environment({},env_name='installed_fixture')
        self.assertEqual(fresh['availability'],'AVAILABLE')
        self.assertFalse(any(x['env_supplied'] for x in seen))
        OBS['mock_install']={'status':result['status'],'fresh_session':fresh['availability'],
            'dispatches':seen,'role':'consult (direct backend; Pi UI activation tested separately)',
            'limit':'controlled executor proves wiring, not actual package verification; target env not enforced'}

    async def test_install_failure_and_second_write_failure(self):
        args=await self.approved_install('failed')
        async def failed(*a,**k):return ('','synthetic error',7)
        with patch('execution.executor._run_native_command',side_effect=failed):
            result=await self.s.call('install.execute',args)
        self.assertEqual(result['status'],'execution_failed')
        self.assertNotIn('installed_fixture',json.loads(self.reg.read_text())['environments'])
        args=await self.approved_install('write-failure');writes=0
        original=PackageManager._save_registry
        def save(pm):
            nonlocal writes
            writes+=1
            if writes==2:raise PermissionError('synthetic second write failure')
            original(pm)
        async def success(*a,**k):return ('synthetic','',0)
        with patch('execution.executor._run_native_command',side_effect=success),patch.object(PackageManager,'_save_registry',save):
            with self.assertRaises(PermissionError):await self.s.call('install.execute',args)
        row=json.loads(self.reg.read_text())['environments']['installed_fixture']
        self.assertEqual(row['status'],'verified');self.assertNotIn('prefix',row)
        OBS['mock_failures']={'install_exit_7':result['status'],'failed_install_new_record':False,
            'second_save_failure':'PermissionError; persisted verified row has no prefix',
            'partial_side_effects_rolled_back':False}

    async def test_registration_update_and_lost_update(self):
        a=PackageManager.__new__(PackageManager);a.registry=json.loads(self.reg.read_text());a.registry_path=self.reg
        b=PackageManager.__new__(PackageManager);b.registry=json.loads(self.reg.read_text());b.registry_path=self.reg
        a.register_environment('rna_fixture',packages=['R'])
        self.assertNotIn('prefix',a.registry['environments']['rna_fixture'])
        self.assertNotIn('env_runner',a.registry['environments']['rna_fixture'])
        a.register_environment('writer_a')
        b.register_environment('writer_b')
        persisted=json.loads(self.reg.read_text())
        self.assertNotIn('writer_a',persisted['environments'])
        OBS['registration_writes']={'update_drops':['prefix','env_runner'],'two_stale_writers':'last writer loses other registration',
            'real_install_or_download':False}

    async def test_register_existing_without_install(self):
        pm=PackageManager.__new__(PackageManager);pm.registry={'environments':{},'tools':{}}
        pm.registry_path=self.reg;pm.conda_root=str(self.root);pm.conda_exe=str(self.root/'conda')
        pm.shim_dir=self.root/'shims';pm.shim_dir.mkdir()
        with patch('subprocess.Popen',side_effect=AssertionError('registration probe')) as process:
            pm.register_existing_tool('Rscript','rna_fixture',source_path=str(self.prefix/'bin/Rscript'))
        self.assertEqual(process.call_count,0)
        row=json.loads(self.reg.read_text())
        self.assertEqual(row['tools']['Rscript']['status'],'registered')
        fresh=RegistryView(self.reg,self.inventory)
        selected=registered_runtime_reuse(fresh,{'tools':['Rscript']},skill_record={},target_env='rna_fixture')
        self.assertEqual(selected['availability'],'AVAILABLE')
        OBS['register_existing']={'subprocesses':0,'downloads':0,'new_environment_created':False,
            'temporary_shim_written':True,'authority':'same temporary registry','new_reader':'AVAILABLE',
            'healthcheck':'not_run','limit':'shim generated but not executed; no real software touched'}

if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--read-registry':
        v=RegistryView(sys.argv[2],sys.argv[3]);r=registered_runtime_reuse(v,{},skill_record={},target_env='rna_fixture')
        print(r['availability']+':'+r['selected_candidate']['audit']['healthcheck_status']);sys.exit(0)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Witnesses))
    (Path(__file__).parent/'python_witness.json').write_text(json.dumps({'tests_run':result.testsRun,
        'witness_assertions_passed':result.wasSuccessful(),'observations':OBS,
        'meaning':'reproduces current behavior, including shortcomings; not future acceptance PASS'},ensure_ascii=False,indent=2)+'\n')
    sys.exit(not result.wasSuccessful())
