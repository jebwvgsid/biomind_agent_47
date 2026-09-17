"""Unified runtime contracts: real probe compiler, evidence, resolver and approval."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent_core.runtime_probe_contract import build_probe_specification, inspect_declared_runtime, parse_probe_result, probe_argv, PROBE_DRIVER
from agent_core.action_runtime import build_runtime_binding_registry, resolve_action_runtime_bindings, capability_contract_hash, build_action_approval_identity, validate_action_approval_identity, ActionRuntimeContractError
from agent_core.node_context import _compact_planner_runtime_projection
from agent_core.runtime_evidence import make_runtime_evidence, merge_runtime_evidence, runtime_evidence_state, project_runtime_audits, digest
from agent_core.skill_manager import SkillManager
from agent_core.workflow.runtime_evidence_recovery import _not_dispatched

ROOT=Path(__file__).resolve().parents[1]

def _requirements():
    return yaml.safe_load((ROOT/'skills/genomics/wgs-short-read-mapping/SKILL.md').read_text().split('---',2)[1])['runtime']

def _prefix(tmp_path):
    p=tmp_path/'envs'/'chosen';(p/'bin').mkdir(parents=True)
    for name in ('bwa','samtools','picard','java'):
        f=p/'bin'/name;f.write_text('external process fixture\n');f.chmod(0o755)
    return p

def _result(spec, *, unavailable=()):
    lines=[];outcomes={}
    for row in spec['probes']:
        ok=row['output'] not in unavailable
        outcomes[row['output']]={'ok':ok,'reason_code':'' if ok else 'RUNTIME_PROBE_COMMAND_MISMATCH'}
        if ok:lines.append(row['output']+'='+row['value'])
    return '\n'.join(lines+['BIOMIND_PROBE_RESULTS='+json.dumps(outcomes)])

def test_fixed_probe_rejects_shell_install_and_python_code(tmp_path):
    p=_prefix(tmp_path)
    for argv in (['/bin/bash','-c','echo success'],['{prefix}/bin/samtools','faidx','reference.fa'],['/usr/bin/python3','-c','print(1)']):
        r=_requirements();r['inspection_probes'][0]['checks'][0]['argv']=argv
        with pytest.raises(ValueError):build_probe_specification(r,p)

def test_probe_uses_explicit_java_and_isolated_python_preserving_invocation_path(tmp_path):
    p=_prefix(tmp_path);spec=build_probe_specification(_requirements(),p)
    assert spec['environment']['JAVA_HOME']==str(p)
    assert spec['environment']['PATH']==str(p/'bin')+':/usr/bin:/bin'
    assert probe_argv(spec)[:5]==['/usr/bin/python3','-I','-S','-c',PROBE_DRIVER]
    assert spec['probes'][0]['checks'][0]['argv'][0]==str(p/'bin'/'bwa')

def test_partial_probe_does_not_erase_capability_facts_or_invent_coverage(tmp_path):
    p=_prefix(tmp_path);spec=build_probe_specification(_requirements(),p)
    result=parse_probe_result(_result(spec,unavailable=['RESOLVED_SAMTOOLS_COVERAGE_BIN']),spec)
    assert result['coverage']['runtime.samtools.faidx.bin']['availability']=='AVAILABLE'
    assert result['coverage']['runtime.samtools.coverage.bin']['availability']=='UNKNOWN'
    assert 'RESOLVED_SAMTOOLS_COVERAGE_BIN' not in result['resolved_runtime_paths']

@pytest.mark.parametrize("outcomes", [[], {}, {"extra": {"ok": True}}, {"RESOLVED_BWA_INDEX_BIN": True}])
def test_malformed_probe_result_never_becomes_a_runtime_fact(tmp_path, outcomes):
    spec=build_probe_specification(_requirements(),_prefix(tmp_path))
    with pytest.raises(ValueError,match='RUNTIME_PROBE_RESULT_INVALID'):
        parse_probe_result('BIOMIND_PROBE_RESULTS='+json.dumps(outcomes),spec)


def test_evidence_resolver_and_approval_consume_same_checked_launchers(tmp_path):
    p=_prefix(tmp_path);requirements=_requirements();spec=build_probe_specification(requirements,p)
    calls=[]
    def leaf(argv,**kwargs):
        calls.append((argv,kwargs));return SimpleNamespace(returncode=0,stdout=_result(spec),stderr='')
    observed=inspect_declared_runtime(requirements,p,runner=leaf)
    sid='genomics/wgs-short-read-mapping'
    subject={k:digest(k) for k in ('skill_document_digest','skill_contract_digest','environment_fingerprint','inspection_contract_digest')}
    subject.update(skill_id=sid,environment_id='chosen',host_id='offline-host')
    evidence=make_runtime_evidence({'skill_id':sid,'env':'chosen','status':'ready',**observed},task_id='runtime',skill_scope=subject,observed_at='2026-09-17T00:00:00+00:00',coverage=observed['coverage'])
    accepted=merge_runtime_evidence([], [evidence],task_id='runtime',accepted_at='2026-09-17T00:00:00+00:00')
    task={'runtime_evidence':runtime_evidence_state(accepted)}
    audits=project_runtime_audits(task,task_id='runtime',skill_scope=subject,now='2026-09-17T00:00:01+00:00')
    registry=build_runtime_binding_registry(audits)
    meta=yaml.safe_load((ROOT/'skills'/sid/'SKILL.md').read_text().split('---',2)[1])
    contract=next(row for row in meta['generate']['execution_capabilities'] if row['capability_id'].endswith('.prepare_reference'))
    contract={**contract,'selected_skill_id':sid};cid=contract['capability_id']
    def resolve():return resolve_action_runtime_bindings(selected_skill_id=sid,execution_capability_id=cid,approved_capability_contract_hash=capability_contract_hash(contract),capability_registry={cid:contract},ready_runtime_binding_registry=registry,current_action_identity={'action_id':'a','plan_id':'p','step_id':'s'})
    resolved=resolve();values=resolved['effective_runtime_bindings']
    assert values['JAVA_BIN']==str(p/'bin'/'java')
    assert values['PICARD_DICTIONARY_BIN']==str(p/'bin'/'picard')
    assert len(resolved['runtime_binding_manifest']['bindings'])==4
    args=dict(generated_script_sha256=digest('explicit launcher script'),capability_contract_hash_value=capability_contract_hash(contract),artifact_env_manifest_hash=digest({}),execution_policy_manifest_hash=digest({'cwd':str(tmp_path)}),working_directory=str(tmp_path))
    approved=build_action_approval_identity(runtime_env_manifest_hash=resolved['runtime_binding_manifest_hash'],**args)
    registry['runtime.java.bin']['value_digest']=digest('changed Java')
    changed=build_action_approval_identity(runtime_env_manifest_hash=resolve()['runtime_binding_manifest_hash'],**args)
    with pytest.raises(ActionRuntimeContractError):validate_action_approval_identity(approved,changed)
    assert len(calls)==1

def test_planner_keeps_each_skill_unknown_and_declared_checked_paths_separate():
    view=_compact_planner_runtime_projection({'input_audit':[
        {'skill_id':'mapping','status':'ready','declared_env':'bio_wgs','checked_env':'chosen','resolved_runtime_paths':{'RESOLVED_JAVA_BIN':'/chosen/bin/java'}},
        {'skill_id':'calling','status':'unknown','declared_env':'bio_gatk','checked_env':'bio_gatk','reason_code':'inspection_execution_error'}]})
    assert 'runtime_ready' not in view
    assert [row['status'] for row in view['skills']]==['ready','unknown']
    assert view['skills'][0]['declared_environment']=='bio_wgs'
    assert view['skills'][0]['checked_environment']=='chosen'

def test_epoch_alone_cannot_make_dispatched_action_inspectable():
    state={'plan_id':'p','plan_version':'v','runtime_state':{'execution_epoch':9},'provenance_state':{'attempts':{'a':{'attempt_id':'a','step_id':'s','status':'FAILED','finished_at':'now','returncode':1}}}}
    assert not _not_dispatched(state,'s')
    state['task_state']={'recovery_state':{'state_events':[{'event_type':'runtime_generation_prepared','plan_id':'p','plan_version':'v','step_id':'s','execution_epoch':9,'retired_attempt_ids':['a']}]}}
    assert _not_dispatched(state,'s')
    state['provenance_state']['attempts']['new']={'attempt_id':'new','step_id':'s','status':'CREATED'}
    assert not _not_dispatched(state,'s')


def _recovery_fixture(root):
    from agent_core.plan_tracker import (build_plan_state, save_plan_state, load_plan_state, approve_plan_state, record_provenance_event, carry_forward_plan_lifecycle)
    from agent_core.task_spec import create_task_spec, task_spec_revision
    from agent_core.step_effects import canonical_step_effect
    from agent_core.workflow.runtime_evidence_recovery import RuntimeEvidenceRecovery
    from datetime import datetime,timezone
    task_id='postexecution-environment';sid='fixture/runtime'
    old_contract={'capability_id':'fixture.runtime.execute','selected_skill_id':sid,'required_runtime_binding_ids':[],'optional_runtime_binding_ids':[],'required_artifact_binding_ids':[]}
    task=create_task_spec('Choose a key after checking the declared runtime.',task_id=task_id,base_dir=root,project_root=root)
    step={'step_id':'science','title':'Choose key','action':'execute','purpose':'analysis','tool':'python','skill_ids':[sid],
          'workflow_stage_ids':[],'batch_id':'','dependencies':[],'inputs':[],'outputs':[],'produces_bindings':['key'],
          'operation':'choose_key','execution_capability_id':old_contract['capability_id'],'risk_level':'low','success_criteria':['Record chosen key']}
    plan=build_plan_state(task_id,json.dumps({'steps':[step],'validation_checklist':[],'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2'}}),strict=True)
    plan['steps'][0]['capability_contract_hash']=capability_contract_hash(old_contract)
    plan['steps'][0]['step_effect']=canonical_step_effect(plan['steps'][0])
    plan.update(workflow_routing_schema_version=2,plan_commit_requested=True,current_task={'task_spec':task,'task_revision':task_spec_revision(task)},selected_skill_ids=[sid])
    plan['task_state'].update(workflow_routing_schema_version=2,selected_skills=[sid])
    save_plan_state(root,task_id,plan,allow_canonical_task_write=True)
    plan=load_plan_state(root,task_id);approve_plan_state(plan,task_spec_revision(task),approval_source='offline_user');save_plan_state(root,task_id,plan)
    plan=load_plan_state(root,task_id)
    old_resolution=resolve_action_runtime_bindings(selected_skill_id=sid,execution_capability_id=old_contract['capability_id'],approved_capability_contract_hash=capability_contract_hash(old_contract),capability_registry={old_contract['capability_id']:old_contract},ready_runtime_binding_registry={},current_action_identity={'action_id':'old-empty','plan_id':plan['plan_id'],'step_id':'science'})
    action={'action_id':'old-empty','plan_id':plan['plan_id'],'plan_version':plan['plan_version'],'step_id':'science','type':'execute','purpose':'analysis','command_role':'analysis_execution','code':'# failed external process','outputs':[],**old_resolution}
    record_provenance_event(plan,'action_bound',{'action':action})
    attempt=record_provenance_event(plan,'attempt_started',{'action_id':action['action_id'],'step_id':'science'})
    record_provenance_event(plan,'execution_finished',{'attempt_id':attempt['attempt_id'],'execution_record':{'attempt_id':attempt['attempt_id'],'execution_id':'old-execution','action_id':action['action_id'],'step_id':'science','plan_id':plan['plan_id'],'plan_version':plan['plan_version'],'returncode':1,'exit_code':1,'execution_status':'failed','purpose':'analysis','command_role':'analysis_execution'}})
    save_plan_state(root,task_id,plan)
    original=deepcopy(load_plan_state(root,task_id)['provenance_state']['actions']['old-empty'])
    new_contract={**old_contract,'required_runtime_binding_ids':['runtime.tool.bin']}
    previous=load_plan_state(root,task_id);candidate=deepcopy(previous)
    candidate['steps'][0]['capability_contract_hash']=capability_contract_hash(new_contract)
    candidate['steps'][0]['title']='Choose key with the declared runtime'
    candidate=carry_forward_plan_lifecycle(previous,candidate);candidate['plan_commit_requested']=True
    save_plan_state(root,task_id,candidate)
    candidate=load_plan_state(root,task_id);approve_plan_state(candidate,task_spec_revision(task),approval_source='offline_user_revision');save_plan_state(root,task_id,candidate)
    tool=root/'tool';tool.write_text('fixture');tool.chmod(0o755)
    subject={k:digest(k) for k in ('skill_document_digest','skill_contract_digest','environment_fingerprint','inspection_contract_digest')};subject.update(skill_id=sid,environment_id='chosen',host_id='offline-host')
    calls=[]
    async def inspect(request):
        calls.append(request)
        return {'inspection_status':'succeeded','observed_at':datetime.now(timezone.utc).isoformat(),
            'audit':{'skill_id':sid,'env':'chosen','status':'ready','resolved_runtime_paths':{'RESOLVED_TOOL_BIN':str(tool)}},
            'coverage':{'runtime.tool.bin':{'availability':'AVAILABLE','output_name':'RESOLVED_TOOL_BIN','value_kind':'executable_path'}}}
    service=RuntimeEvidenceRecovery(data_path=str(root),scope_provider=lambda _:deepcopy(subject),inspect_runtime=inspect,inspection_contract_provider=lambda _:{'contract_id':'fixed-probe','contract_digest':digest('probe'),'method_digest':subject['inspection_contract_digest'],'permitted':True,'safe_repeat':True,'coverage_binding_ids':['runtime.tool.bin'],'cache_restore_permitted':False})
    return task_id,sid,new_contract,service,original,attempt['attempt_id'],calls


def test_ended_empty_action_requires_new_approved_scope_then_real_discovery(tmp_path):
    import asyncio
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.runtime_evidence_recovery import RuntimeRecoveryError
    task_id,sid,contract,service,original,attempt_id,calls=_recovery_fixture(tmp_path)
    state={'task_id':task_id}
    with pytest.raises(RuntimeRecoveryError,match='EXECUTION_ALREADY_OWNED'):
        service.create_request(state,selected_skill_id=sid,capability_contract=contract,missing_binding_ids=['runtime.tool.bin'],original_error={'code':'MISSING_RUNTIME_FACT'})
    prepared=service.prepare_generation(state,selected_skill_id=sid,capability_contract=contract)
    assert prepared['provenance_state']['actions']['old-empty']==original
    assert original['normalized_action']['runtime_binding_manifest']['bindings']==[]
    assert prepared['provenance_state']['attempts'][attempt_id]['status']=='FAILED'
    epoch=prepared['runtime_state']['execution_epoch']
    replay=service.prepare_generation(state,selected_skill_id=sid,capability_contract=contract)
    assert replay['runtime_state']['execution_epoch']==epoch
    request=service.create_request(state,selected_skill_id=sid,capability_contract=contract,missing_binding_ids=['runtime.tool.bin'],original_error={'code':'MISSING_RUNTIME_FACT'})
    result=asyncio.run(service.recover(state,request))
    assert result['status']=='evidence_ready',result
    after=load_plan_state(tmp_path,task_id)
    assert after['provenance_state']['actions']['old-empty']==original
    assert after['steps'][0]['status'] not in {'passed','completed','done'}
    assert len(calls)==1
    assert not after['task_state'].get('installation_receipt')


def test_live_or_unreconciled_attempt_cannot_open_new_runtime_scope(tmp_path):
    from agent_core.plan_tracker import load_plan_state,save_plan_state
    from agent_core.workflow.runtime_evidence_recovery import RuntimeRecoveryError
    task_id,sid,contract,service,original,aid,calls=_recovery_fixture(tmp_path)
    durable=load_plan_state(tmp_path,task_id)
    durable['provenance_state']['attempts'][aid]['finished_at']=''
    save_plan_state(tmp_path,task_id,durable)
    with pytest.raises(RuntimeRecoveryError,match='PROCESS_NOT_TERMINAL'):
        service.prepare_generation({'task_id':task_id},selected_skill_id=sid,capability_contract=contract)
    assert calls==[]


def test_bubblewrap_permission_failure_is_not_tool_absence():
    import asyncio
    from tests.test_runtime_inspection import RuntimeInspectionTests
    async def check():
        case=RuntimeInspectionTests(methodName='test_nonjava_fact_runs_actual_adapter_and_sandbox_argv')
        case.setUp()
        try:
            case.leaf.code=1
            case.leaf.stderr='bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted'
            result=await case.adapter.inspect(case.request())
            assert result['inspection_status']=='permission_error'
            assert result['reason_code']=='RUNTIME_INSPECTION_SANDBOX_PERMISSION_DENIED'
            assert not result['audit'] and not result['coverage']
            # GWAS S1 preserves only a bounded, sanitized sandbox reason.
            assert result['inspection_error']['reason_detail'] == case.leaf.stderr
            assert len(result['inspection_error']['reason_detail']) <= 384
        finally:
            case.doCleanups()
    asyncio.run(check())
