"""Request-scoped real Domain metadata/launch wiring; synthetic programs only."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch,AsyncMock
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1])]
from test_external_access_integration import DomainService,DomainError,REPO
from package_manager import PackageManager

class EnvironmentBinding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='biomind-binding-b-');self.root=Path(self.tmp.name)
        self.registry=self.root/'registry.json';self.registry.write_text('{"environments":{},"tools":{}}')
        self.prefs=self.root/'preferences.md';self.prefs.write_text('fixture')
        self.inventory=self.root/'inventory.txt';self.inventory.write_text('')
        self.kw=dict(cwd=self.root,data_root=self.root/'data',install_root=REPO,registry_path=self.registry,
            preferences_path=self.prefs,skills_root=self.root/'skills',environment_inventory=self.inventory,execution_profile='native')
        self.services=[];self.s=self.service('one');await self.begin('request-one')
    def service(self,name):
        s=DomainService(**self.kw,session_id=name);self.services.append(s);return s
    async def begin(self,name):return await self.s.call('request.begin',{'request_id':name,'role':'consult'},host_token=self.s.host_token)
    async def asyncTearDown(self):
        for s in self.services:
            await s.formal.shutdown()
            await s.disconnected()
        self.tmp.cleanup()
    def tool(self,name,tool='Rscript'):
        prefix=self.root/name;(prefix/'bin').mkdir(parents=True)
        p=prefix/'bin'/tool;p.write_text('#!/bin/sh\nprintf "%s:%s:%s\\n" "'+name+'" "$CONDA_PREFIX" "$BIO_TEST_LIBRARY"\n/bin/sh -c \'printf "child:%s\\n" "$BIO_TEST_LIBRARY"\'\n');p.chmod(0o755)
        return prefix
    async def register(self,name,**extra):
        tool=extra.pop('tool','Rscript');prefix=self.tool(name,tool)
        result=await self.s.call('environments.register',{'call_id':'register-'+name,'env_name':name,'prefix':str(prefix),
            'tools':[tool],'runtime_env':{'BIO_TEST_LIBRARY':name+'-library'},**extra})
        self.assertEqual(result['status'],'registered',result);return prefix,result
    async def select(self,**p):return await self.s.call('environments.select',p)
    async def terminal(self,r):
        for _ in range(100):
            if r.get('terminal'):return r
            if not r.get('run_ref'):return r
            r=await self.s.call('exec.wait',{'run_ref':r['run_ref'],'max_wait_ms':100})
        self.fail(str(r))
    async def test_E01_E03_zero_probe_select_dedup_implicit_managed_binding(self):
        prefix,_=await self.register('rna')
        with patch('subprocess.Popen',side_effect=AssertionError('selection probe')):
            one=await self.select(requirements={'tools':['Rscript']})
            with patch.object(self.s.registry,'reload',side_effect=AssertionError('repeated query reloaded registry')), patch('domain_adapter.service.registered_runtime_reuse',side_effect=AssertionError('repeated query enumerated registry')):
                two=await self.select(requirements={'tools':['Rscript']})
                reuse=await self.select()
        self.assertEqual(one['binding_ref'],two['binding_ref']);self.assertEqual(reuse['binding_ref'],one['binding_ref'])
        self.assertEqual(len(self.s._state()['task_state']['runtime_evidence']['records']),1)
        r=await self.terminal(await self.s.call('exec.start',{'call_id':'implicit','arguments':{'argv':['Rscript'],'workdir':str(self.root)}}))
        self.assertEqual(r.get('exit_code'),0,r);self.assertIn('rna:'+str(prefix),r['stdout']);self.assertIn('child:rna-library',r['stdout'])
        self.assertEqual(r['runtime_binding']['binding_ref'],one['binding_ref'])
    async def test_E02_fresh_process_and_session_same_authority_no_check(self):
        prefix,_=await self.register('fresh')
        code="import sys;sys.path.insert(0,sys.argv[1]);from domain_adapter.registry import RegistryView;from agent_core.registered_runtime import registered_runtime_reuse;v=RegistryView(sys.argv[2]);r=registered_runtime_reuse(v,{'tools':['Rscript']},skill_record={});print(r['availability'],r['selected_candidate']['prefix'])"
        proc=subprocess.run([sys.executable,'-B','-c',code,str(REPO/'integrations/pi_runtime'),str(self.registry)],env={**os.environ,'PYTHONPATH':str(REPO)},capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr);self.assertIn('AVAILABLE '+str(prefix),proc.stdout)
        self.s=self.service('second');await self.begin('fresh-session')
        choice=await self.select(requirements={'tools':['Rscript']});self.assertEqual(choice['availability'],'AVAILABLE')
        r=await self.terminal(await self.s.call('exec.start',{'call_id':'new','arguments':{'argv':['Rscript']}}));self.assertEqual(r.get('exit_code'),0,r)
    async def test_N08_two_scopes_parallel_and_new_request_isolation(self):
        rna,_=await self.register('r');py,_=await self.register('p',tool='python3')
        r=await self.select(env_name='r',runtime_scope='R');p=await self.select(env_name='p',runtime_scope='Python')
        ambiguous=await self.s.call('exec.start',{'call_id':'ambiguous','arguments':{'argv':['Rscript']}})
        self.assertIn('RUNTIME_BINDING_REQUIRED',ambiguous['error'])
        async def run(choice,cid):return await self.terminal(await self.s.call('exec.start',{'call_id':cid,'arguments':{'argv':['Rscript' if cid=='r' else 'python3'],'binding_ref':choice['binding_ref']}}))
        results=[await run(r,'r'),await run(p,'p')]
        self.assertIn('r:'+str(rna),results[0]['stdout']);self.assertIn('p:'+str(py),results[1]['stdout'])
        await self.begin('request-two')
        stale=await self.s.call('exec.start',{'call_id':'stale','arguments':{'argv':['Rscript'],'binding_ref':r['binding_ref']}})
        self.assertEqual(stale['status'],'blocked')
        host=await self.terminal(await self.s.call('exec.start',{'call_id':'host','arguments':{'cmd':'printf "%s" "${BIO_TEST_LIBRARY-unset}"'}}))
        self.assertEqual(host['stdout'],'unset');self.assertNotIn('BIO_TEST_LIBRARY',os.environ)
    async def test_N09_explicit_constraints_reach_actual_launch(self):
        await self.register('one');choice=await self.select(env_name='one')
        for req in ({'prefix':'/wrong'},{'env':'other'},{'backend':'remote'},{'host':'foreign-host'}):
            r=await self.s.call('exec.start',{'call_id':'conflict-'+str(req),'arguments':{'argv':['Rscript'],'binding_ref':choice['binding_ref'],'requirements':req}})
            self.assertEqual(r['status'],'blocked',r);self.assertFalse(r['execution_started'])
        conflict=await self.select(env_name='one',requirements={'env':'other'});self.assertNotEqual(conflict['availability'],'AVAILABLE')
    async def test_E08_same_name_other_prefix_preserves_identity_and_refuses_foreign_scope(self):
        prefix,_=await self.register('same-name');other=self.tool('other-prefix')
        before=self.registry.read_bytes()
        with patch('subprocess.Popen',side_effect=AssertionError('metadata must not execute')):
            result=await self.s.call('environments.register',{'call_id':'same-name-other-prefix','env_name':'same-name',
                'prefix':str(other),'tools':['Rscript']})
            self.assertEqual(result['status'],'registration_failed',result)
            self.assertIn('CONFLICT',result['error']);self.assertEqual(self.registry.read_bytes(),before)
        self.s=self.service('same-name-fresh');await self.begin('same-name-fresh-request')
        for constraint in ({'prefix':str(other)},{'backend':'remote'},{'host':'foreign-node'}):
            rejected=await self.select(env_name='same-name',requirements=constraint)
            self.assertNotEqual(rejected['availability'],'AVAILABLE',rejected)
        selected=await self.select(env_name='same-name')
        run=await self.terminal(await self.s.call('exec.start',{'call_id':'stable-identity','arguments':{
            'argv':['Rscript'],'binding_ref':selected['binding_ref']}}))
        self.assertEqual(run['exit_code'],0,run);self.assertIn('same-name:'+str(prefix),run['stdout'])
        self.assertNotIn(str(other),run['stdout'])
    async def test_E04_I01_N12_metadata_registration_idempotent_and_no_claimed_verification(self):
        prefix=self.tool('inventory');self.inventory.write_text(str(prefix)+'\n')
        r=await self.select(env_name='inventory');self.assertNotEqual(r['availability'],'AVAILABLE')
        args={'call_id':'reg','env_name':'inventory','prefix':str(prefix),'tools':['Rscript'],'packages':['fgsea'],'source_note':'user declaration'}
        with patch('subprocess.Popen',side_effect=AssertionError('registration probe')):
            first=await self.s.call('environments.register',args)
            old=self.registry.read_bytes();second=await self.s.call('environments.register',args)
        self.assertEqual(first['operation'],'created');self.assertEqual(second['operation'],'unchanged');self.assertEqual(old,self.registry.read_bytes())
        self.assertNotIn('last_verified_at',first['record']);self.assertEqual(first['record']['evidence']['strength'],'declared_not_verified')
        for field in ['verified','registry_path','source','execution_profile','host_token']:
            with self.assertRaises((ValueError,DomainError)):
                await self.s.call('environments.register',{**args,field:True})
        with self.assertRaisesRegex(DomainError,'HOST_CHANNEL'):
            await self.s.call('environments.register_observed',{**args,'observation':{}})
    async def test_E09_errors_keep_authority_and_no_write(self):
        for raw in ('{broken','[]','{"schema_version":999}'):
            self.registry.write_text(raw)
            result=await self.select(env_name='rna')
            self.assertEqual(result['availability'],'UNKNOWN',result);self.assertFalse(result['install_required'])
            self.assertEqual(result['registration_sources']['environment_registry'],str(self.registry))
            self.assertEqual(self.registry.read_text(),raw)
        self.registry.unlink();result=await self.select(env_name='rna');self.assertIn('UNINITIALIZED',result['reason_code']);self.assertFalse(self.registry.exists())
    async def test_bound_bash_launch_preserves_requested_shell_and_does_not_probe(self):
        prefix,_=await self.register('bash');choice=await self.select(env_name='bash')
        with patch('subprocess.Popen',side_effect=AssertionError('launch resolution probe')):
            prepared=await self.s.call('environments.launch',{'binding_ref':choice['binding_ref'],'command':'Rscript; printf "%s" "$BASH_VERSION"','shell':'/bin/bash','base_path':'/usr/bin:/bin'},host_token=self.s.host_token)
        process=subprocess.run(['/bin/bash','-c',prepared['command']],env={**os.environ,**prepared['env']},capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr);self.assertIn('bash:'+str(prefix),process.stdout);self.assertIn('5.',process.stdout)
    async def test_N15_partial_tool_mapping_keeps_precise_environment(self):
        prefix=self.tool('partial')
        with patch.object(PackageManager,'register_existing_tool',side_effect=OSError('synthetic tool mapping fault')):
            result=await self.s.call('environments.register',{'call_id':'partial','env_name':'partial','prefix':str(prefix),'tools':['Rscript']})
        self.assertEqual(result['status'],'partial');self.assertEqual(result['record']['prefix'],str(prefix));self.assertEqual(result['installation_status'],'not_run')
    async def test_E15_lookup_does_not_repeat_failed_scope(self):
        with patch('domain_adapter.registry.bounded_location_lookup',new_callable=AsyncMock,return_value={'status':'not_found','candidates':[]}) as locate:
            one=await self.s.call('environments.locate',{'requirements':{'tools':['missing']}})
            two=await self.s.call('environments.locate',{'requirements':{'tools':['missing']}})
        self.assertEqual(locate.call_count,1);self.assertTrue(two['replayed']);self.assertFalse(one['install_required'])

    async def test_protected_metadata_registration_requires_existing_write_authority(self):
        # Put the authority outside this protected project; it is metadata,
        # but still not a model-granted write capability.
        project=self.root/'protected-project';project.mkdir()
        kw={**self.kw,'cwd':project,'data_root':project/'data','execution_profile':'protected'}
        self.s=DomainService(**kw,session_id='proper-protected');self.services.append(self.s);await self.begin('proper')
        prefix=self.tool('metadata-only');before=self.registry.read_bytes()
        result=await self.s.call('environments.register',{'call_id':'blocked-meta','env_name':'metadata-only','prefix':str(prefix),'tools':['Rscript']})
        self.assertEqual(result['status'],'waiting_approval',result);self.assertEqual(before,self.registry.read_bytes())

if __name__=='__main__':unittest.main(verbosity=2)
