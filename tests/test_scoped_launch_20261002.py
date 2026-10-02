"""B: scoped immutable bindings and real synthetic launches, entirely offline."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO/'integrations/pi_runtime/tests'),str(REPO)]
from test_python_domain import DomainService,permissions
from agent_core.pending_input import mutate
from agent_core.request_state import request_scope
from agent_core.runtime_evidence import (make_runtime_evidence,merge_runtime_evidence,runtime_evidence_state,
    registered_execution_binding,project_registered_runtime_scopes,validate_runtime_evidence,RuntimeEvidenceError,digest)
from agent_core.runtime_launch import prepare_runtime_launch,RuntimeLaunchError
from agent_core.action_runtime import freeze_execution_environment,registered_execution_environment
from agent_core.command_execution import protected_path_roots,FormalProtectionError,formal_protected_spawn


def bind(state,prefix,*,scope='r-analysis',name='fixture',legacy=False,extra=None):
    launch={'prefix':str(prefix),'env':name,'tools':['Rscript'],'source_ref':'fixture:'+name,**(extra or {})}
    audit={'skill_id':scope,'env':name,'status':'ready','resolved_runtime_paths':{'RESOLVED_ENV_PREFIX':str(prefix)}}
    stamp=datetime.now(timezone.utc).isoformat()
    context={'skill_id':scope,'environment_id':name,'host_id':'synthetic-host',
             **{k:digest(k+str(prefix)) for k in ('skill_document_digest','skill_contract_digest','environment_fingerprint','inspection_contract_digest')}}
    evidence=make_runtime_evidence(audit,task_id=state['task_id'],skill_scope=context,observed_at=stamp,
        producer='registered_runtime',inspection_status='not_run',source_ref=launch['source_ref'])
    task=state['task_state'];old=task.get('runtime_evidence',{}).get('records',[])
    kwargs={} if legacy else {'request_scope':request_scope(state),'launch_description':launch,'requirements':{'tools':['Rscript']}}
    records=merge_runtime_evidence(old,[evidence],task_id=state['task_id'],**kwargs)
    task['runtime_evidence']=runtime_evidence_state(records)
    return records[-1]['evidence_id']


class BindingContracts(unittest.TestCase):
    def setUp(self):
        self.state={'task_id':'task','authoritative_user_turn_revision':1,
                    'task_state':{'pi_request_id':'request-one','current_request_key':'request-one'}}

    def test_request_isolation_and_old_records_need_new_selection(self):
        ref=bind(self.state,'/synthetic/prefix')
        self.assertEqual(registered_execution_binding(self.state)['binding_ref'],ref)
        self.state['task_state'].update(pi_request_id='request-two',current_request_key='request-two')
        self.state['authoritative_user_turn_revision']=2
        self.assertIsNone(registered_execution_binding(self.state))
        self.assertEqual(project_registered_runtime_scopes(self.state['task_state'],task_id='task'),([],[]))
        with self.assertRaisesRegex(RuntimeEvidenceError,'NOT_IN_CURRENT_REQUEST'):
            registered_execution_binding(self.state,binding_ref=ref)
        bind(self.state,'/synthetic/old',legacy=True)
        self.assertIsNone(registered_execution_binding(self.state))

    def test_multiple_scopes_require_reference_even_same_prefix(self):
        one=bind(self.state,'/same/prefix',scope='r-analysis')
        two=bind(self.state,'/same/prefix',scope='python-analysis')
        with self.assertRaisesRegex(RuntimeEvidenceError,'RUNTIME_BINDING_REQUIRED'):
            registered_execution_binding(self.state)
        self.assertEqual(registered_execution_binding(self.state,runtime_scope='r-analysis')['binding_ref'],one)
        self.assertEqual(registered_execution_binding(self.state,binding_ref=two)['scope'],'python-analysis')
        with self.assertRaisesRegex(RuntimeEvidenceError,'SCOPE_CONFLICT'):
            registered_execution_binding(self.state,binding_ref=two,runtime_scope='r-analysis')

    def test_binding_digest_and_explicit_constraints_cannot_drift(self):
        ref=bind(self.state,'/synthetic/prefix')
        with self.assertRaisesRegex(RuntimeEvidenceError,'REQUIREMENT_CONFLICT'):
            registered_execution_binding(self.state,binding_ref=ref,requirements={'prefix':'/wrong/prefix'})
        record=deepcopy(self.state['task_state']['runtime_evidence']['records'][0])
        record['acceptance']['launch_description']['prefix']='/tampered'
        with self.assertRaisesRegex(RuntimeEvidenceError,'DIGEST_MISMATCH'):validate_runtime_evidence(record)
        with self.assertRaisesRegex(RuntimeEvidenceError,'REQUIREMENT_CONFLICT'):
            registered_execution_binding(self.state,requirements={'env':'one','env_name':'two'})

    def test_selection_is_no_probe_and_legacy_non_pi_projection_retained(self):
        bind(self.state,'/synthetic/prefix',legacy=True)
        self.state['task_state'].pop('pi_request_id')
        with patch('subprocess.Popen',side_effect=AssertionError('binding probe forbidden')):
            rows,_=project_registered_runtime_scopes(self.state['task_state'],task_id='task')
        self.assertEqual(rows[0]['resolved_runtime_paths']['RESOLVED_ENV_PREFIX'],'/synthetic/prefix')

    def test_runner_entry_contract_and_environment_overrides(self):
        record={'prefix':'/target/python','env_runner':'/launcher/conda run -n python python'}
        with self.assertRaisesRegex(RuntimeLaunchError,'RUNNER_ENTRY_CONFLICT'):
            prepare_runtime_launch(record,'ls',base_env={'PATH':'/usr/bin:/bin'})
        with self.assertRaisesRegex(RuntimeLaunchError,'RUNNER_ENTRY_CONFLICT'):
            prepare_runtime_launch(record,['Rscript','-e','1'],base_env={'PATH':'/usr/bin:/bin'})
        projection=prepare_runtime_launch(record,['python','script.py'],base_env={'PATH':'/usr/bin:/bin'})
        self.assertEqual(projection['command'][:4],['/launcher/conda','run','-p','/target/python'])
        with self.assertRaisesRegex(RuntimeLaunchError,'OVERRIDE_CONFLICT'):
            prepare_runtime_launch({'prefix':'/target/python','runtime_env':{'LD_LIBRARY_PATH':'/exact'}},['python'],extra_env={'LD_LIBRARY_PATH':'/other'})
        with self.assertRaisesRegex(RuntimeLaunchError,'EXPLICIT_ENTRY_CONFLICT'):
            prepare_runtime_launch({'prefix':'/target/r','tools':['Rscript']},['/host/Rscript'])

    def test_frozen_prepared_path_is_not_duplicated(self):
        record={'prefix':'/target/r','runtime_env':{'LIB_FIXTURE':'exact'}}
        prepared=prepare_runtime_launch(record,'echo x',base_env={'PATH':'/usr/bin:/bin'})
        frozen=freeze_execution_environment({'PATH':'/host/bin'},prepared['env'])
        self.assertEqual(frozen['PATH'].split(':').count('/target/r/bin'),1)
        self.assertNotIn('LIB_FIXTURE',os.environ)

    def test_runner_only_and_unknown_selected_requirements_remain_usable(self):
        projection=prepare_runtime_launch({'entries':{'Rscript':'/exact/Rscript'},
            'runtime_env':{'LIB_FIXTURE':'exact'},'runner_argv':['/exact/wrapper','{command}']},
            ['Rscript','-e','1'],base_env={'PATH':'/usr/bin:/bin'})
        self.assertEqual(projection['command'],['/exact/wrapper','/exact/Rscript','-e','1'])
        with self.assertRaisesRegex(RuntimeLaunchError,'SHELL_ENTRY_MAPPING_UNSUPPORTED'):
            prepare_runtime_launch({'entries':{'Rscript':'/exact/Rscript'}},'Rscript -e 1',base_env={'PATH':'/usr/bin:/bin'})
        ref=bind(self.state,'/synthetic/prefix',extra={'packages':['known'],'versions':{'known':'1.0'}})
        self.assertIsNotNone(registered_execution_binding(self.state,binding_ref=ref,requirements={'packages':['unknown>=3']}))
        self.assertIsNotNone(registered_execution_binding(self.state,requirements={'env':'fixture','packages':['unknown>=3']}))
        self.assertIsNone(registered_execution_binding(self.state,requirements={'packages':['new-unrelated']}))
        with self.assertRaisesRegex(RuntimeEvidenceError,'REQUIREMENT_CONFLICT'):
            registered_execution_binding(self.state,binding_ref=ref,requirements={'versions':{'known':'>=2'}})


class RealScopedLaunch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='biomind-scope-');self.root=Path(self.tmp.name)
        self.project=self.root/'project';self.project.mkdir();self.registry=self.root/'registry.json'
        self.registry.write_text('{"environments":{},"tools":{}}');self.prefs=self.root/'prefs';self.prefs.write_text('fixture')
        self.kw=dict(cwd=self.project,data_root=self.project/'.biomind/data',install_root=REPO,
            session_id='scope-native',registry_path=self.registry,preferences_path=self.prefs,skills_root=self.root/'skills')
        self.s=DomainService(**self.kw,execution_profile='native')
        await self.s.call('request.begin',{'request_id':'r1','role':'generate'},host_token=self.s.host_token)
        self.first=self.root/'first';self.second=self.root/'second'
        for prefix,label in [(self.first,'FIRST'),(self.second,'SECOND')]:
            (prefix/'bin').mkdir(parents=True)
            path=prefix/'bin/Rscript';path.write_text('#!/bin/sh\n/bin/sh -c \'printf "'+label+'|%s|%s\\n" "$ENV_PREFIX" "$LIB_FIXTURE"\'\n');path.chmod(0o755)

    async def asyncTearDown(self):
        await self.s.disconnected();self.tmp.cleanup()

    def binding(self,prefix,scope):
        return mutate(self.s.data_root,self.s.task_id,lambda state:bind(state,prefix,scope=scope,name=scope,extra={'runtime_env':{'LIB_FIXTURE':scope}}))

    async def run_bound(self,ref,call):
        selected=registered_execution_binding(self.s._state(),binding_ref=ref)
        launched=prepare_runtime_launch(selected['launch'],['Rscript'],base_env={'PATH':'/usr/bin:/bin'})
        args={'argv':launched['command'],'env':launched['env'],'_runtime_binding':{
            'binding_ref':ref,'scope':selected['scope'],'launch':launched['launch']}}
        r=await self.s.formal.start(args,task_id=self.s.task_id,call_id=call,stage='pi_native',request_scope=self.s.scope(),yield_time_ms=10)
        for _ in range(100):
            if r.get('terminal'):break
            r=await self.s.call('exec.wait',{'run_ref':r['run_ref'],'max_wait_ms':100})
        self.assertEqual(r.get('exit_code'),0,r)
        return r

    async def test_native_bindings_reach_real_child_and_parallel_launches_are_isolated(self):
        first=self.binding(self.first,'first');second=self.binding(self.second,'second')
        # Keep the existing observer's single active job rule. Parallel shell
        # calls use the same shared launch projection and existing executor.
        one=await self.run_bound(first,'one');two=await self.run_bound(second,'two')
        self.assertIn('FIRST|'+str(self.first)+'|first',Path(one['log_refs']['stdout']).read_text())
        self.assertIn('SECOND|'+str(self.second)+'|second',Path(two['log_refs']['stdout']).read_text())
        receipt=json.loads(Path(one['receipt_ref']).read_text())
        self.assertEqual(receipt['runtime_binding']['binding_ref'],first)
        self.assertEqual(receipt['env']['PATH'].split(':').count(str(self.first/'bin')),1)
        self.assertFalse(self.s._state()['task_state']['execution_permissions']['requests'])
        self.assertNotIn('LIB_FIXTURE',os.environ)
        from execution.executor import _run_native_command
        async def concurrent(ref):
            selected=registered_execution_binding(self.s._state(),binding_ref=ref)
            projection=prepare_runtime_launch(selected['launch'],['Rscript'],base_env={'PATH':'/usr/bin:/bin'})
            return await _run_native_command(projection['command'],check_intent=lambda:None,cwd=self.project,
                env=projection['env'],profile=False,display_streams=False)
        a,b=await asyncio.gather(concurrent(first),concurrent(second))
        self.assertIn('FIRST|'+str(self.first)+'|first',a[0]);self.assertEqual(a[2],0)
        self.assertIn('SECOND|'+str(self.second)+'|second',b[0]);self.assertEqual(b[2],0)

    async def test_protected_registered_external_prefix_does_not_grant_directory(self):
        await self.s.disconnected()
        self.s=DomainService(**{**self.kw,'session_id':'scope-protected'},execution_profile='protected')
        await self.s.call('request.begin',{'request_id':'r1','role':'generate'},host_token=self.s.host_token)
        self.binding(self.first,'first')
        self.registry.write_text(json.dumps({'environments':{'first':{'prefix':str(self.first),'status':'verified'}},'tools':{}}))
        self.s.registry.reload()
        with self.assertRaisesRegex(FormalProtectionError,'runtime_prefix_permission_required'):
            protected_path_roots(self.s.commands,self.s.task_id,{'env':{'ENV_PREFIX':str(self.first)}})
        params={'call_id':'protected-real','arguments':{'argv':['Rscript']}}
        blocked=await self.s.call('exec.start',params)
        self.assertEqual(blocked['status'],'stage_blocked',blocked)
        # Merely having accepted selection must not grant ordinary file reads.
        roots=protected_path_roots(self.s.commands,self.s.task_id)['runtime']
        self.assertNotIn(self.first,roots)
        args={'path':str(self.first),'access':'read','target_kind':'directory','recursive':True}
        try:
            permissions.prepare_path_access(self.s.commands,self.s.task_id,[args],call_id='runtime-directory',purpose='explicit synthetic runtime')
        except permissions.PermissionPending as exc:row=exc.request
        await self.s.call('approval.decide',{**{k:row[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host_token=self.s.host_token)
        facts=protected_path_roots(self.s.commands,self.s.task_id,{'env':{'ENV_PREFIX':str(self.first)}})
        self.assertIn(self.first,facts['runtime']);self.assertEqual(len(facts['runtime_grants']),1)
        pending=await self.s.call('exec.start',params)
        self.assertEqual(pending['status'],'waiting_approval',pending)
        approval=pending['approval']
        await self.s.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host_token=self.s.host_token)
        result=await self.s.call('exec.start',params)
        for _ in range(100):
            if result.get('terminal'):break
            result=await self.s.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
        self.assertEqual(result.get('exit_code'),0,result)
        receipt=json.loads(Path(result['receipt_ref']).read_text())
        self.assertEqual(receipt['runtime_binding']['scope'],'first')
        self.assertIn('FIRST|'+str(self.first)+'|first',Path(result['log_refs']['stdout']).read_text())
        grant_id=facts['runtime_grants'][0]['grant_id']
        permissions.revoke_read_grant(self.s.data_root,self.s.task_id,grant_id)
        with self.assertRaisesRegex(FormalProtectionError,'runtime_prefix_permission_required'):
            protected_path_roots(self.s.commands,self.s.task_id,{'env':{'ENV_PREFIX':str(self.first)}})


if __name__=='__main__':unittest.main()
