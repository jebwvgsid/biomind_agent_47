"""Explicit synthetic receipts through Recovery admission and local publishing.

No input reader or registered scientific helper is invoked here.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_core.action_runtime import build_execution_policy_manifest, resolve_preflight_evidence_binding
from agent_core.managed_artifacts import managed_input_signature, _observed_file_metadata
from agent_core.step_effects import emit_observation
from agent_core.workflow.recovery_projection import build_causal_failure_record, project_recovery_seed
from agent_core.workflow.recovery_reception import validate_candidate
from agent_core.workflow.result_resolution import (
    preflight_result_candidate, admit_result_resolution, get_result_resolution_request,
    result_resolution_reconciliation, register_result_resolution,
)
from agent_core.workflow.stage_contracts import StageContractError
from tests.test_plan_results_name import name_fixture, tiny_copy

HANDLE = 'genomics/wgs-short-read-mapping#scripts/inspect_wgs_inputs.py'


def synthetic_receipt(root):
    """Write a labelled stand-in for the helper's published receipt schema."""
    root.mkdir(parents=True)
    out, err = root/'item-001.stdout.log', root/'item-001.stderr.log'
    value = {'operation':'fastq_prefix', 'synthetic':True, 'prefix_valid':True, 'full_scientific_validation':False}
    out.write_text(emit_observation('input_1', value) + '\n'); err.write_text('')
    check = {'label':'synthetic', 'status':'returned', 'passed':True, 'scope':'fastq_prefix_only',
        'finding':{'passed':True, 'value':value, 'scope':'fastq_prefix_only'},
        'raw':{'stdout':str(out),'stderr':str(err)}, 'returncode':0}
    result = {'status':'returned','total':1,'returned':1,'passed':1,'unstarted':0,'checks':[check]}
    (root/'checks.json').write_text(json.dumps({'version':1, **result}))
    return emit_observation('validation_observation', {**result,'scope':'synthetic_prefix_only',
        'full_file_validated':False,'all_checks_passed':True}) + '\n'


def fixture(root):
    durable, state, directive = name_fixture(root/'delivery', 'copy_to_exact')
    execution = state['execute_outcome']['payload']['execution_record']
    contract = execution['action_contract']['controller_output_bindings'][0]
    contract.update(expected_path=str(root/'delivery/00.input_check'), artifact_kind='directory',format='directory',validation_contract_id='directory_exists')
    aid, attempt = execution['action_id'], execution['attempt_id']
    policy = build_execution_policy_manifest(action_identity={'action_id':aid,'plan_id':durable['plan_id'],'step_id':'step-1'},
        working_directory=str(root),language='bash',skill_resources=[{'handle':HANDLE}],evidence_root=str(root/'attempts'))
    binding = {**policy['evidence_binding'],'attempt_id':attempt,'action_id':aid,
        'policy_manifest_hash':policy['manifest_hash'],'canonical_path':str(root/'attempts'/attempt/'preflight')}
    stdout = synthetic_receipt(Path(binding['canonical_path']))
    source_input = root/'synthetic-input.txt';source_input.write_text('synthetic content\n')
    action = {'controller_output_bindings':[contract], 'execution_policy_manifest':policy,
        'materialized_artifacts':{'input.synthetic':str(source_input),'output.result':contract['expected_path']},
        'concrete_artifact_bindings':[{'direction':'input','path':str(source_input)}]}
    execution.update(stdout=stdout,raw_stdout=stdout,action_contract=action,preflight_evidence_binding=binding,
        managed_input_signature=managed_input_signature(action),validation_before_output_reconciliation={'passed':True},
        declared_step_effect={'produces_artifacts':[contract['artifact_id']], 'produces_observations':[{'identity':'validation_observation','evidence_contract':{'required_fields':['value']}}]})
    durable['provenance_state']['actions'][aid].update(normalized_action=deepcopy(action),controller_output_bindings=[contract])
    durable['provenance_state']['attempts'][attempt].update(attempt_sequence=1,status='EVIDENCE_COMMITTED',managed_input_signature=execution['managed_input_signature'])
    # The historical checkpoint has only a parent-directory observation. The
    # new reception obtains the candidate from the exact execution binding.
    execution['observed_output_records']=[{**_observed_file_metadata(root/'delivery'),'attempt_id':attempt,'current_attempt_owned':True}]
    directive['result_selections']=[{'artifact_id':contract['artifact_id'],'observed_path':binding['canonical_path'],'operation':'copy_to_exact'}]
    refresh_packet(durable,state)
    return durable,state,directive


def refresh_packet(durable,state):
    source=state['execute_outcome']
    record=build_causal_failure_record('execute_outcome',source,durable,task_id=durable['task_id'])
    durable['task_state']['causal_failure_packets']={record['packet_ref']:record}
    durable['task_state']['active_failure_packet_ref']=record['packet_ref']
    state.update(active_failure_packet_ref=record['packet_ref'],failure_packet=record['failure_packet'],failure_source=record['failure_source'])


def admit(durable,state,directive):
    return admit_result_resolution(seed=project_recovery_seed(state,durable).recovery_seed,
        durable=durable,directive=validate_candidate(directive),state=state)


def test_preflight_receipt_is_visible_in_real_seed_and_selectable(tmp_path):
    durable,state,directive=fixture(tmp_path)
    packet=project_recovery_seed(state,durable).recovery_seed.to_dict()
    assert directive['result_selections'][0]['observed_path'] in json.dumps(packet)
    request=admit(durable,state,directive)
    assert request['source_attempt_id']=='attempt-1'
    assert request['selections'][0]['observed_record']['full_file_validated'] is False
    assert 'inspect_wgs_inputs.py' not in request['approved_code']
    assert not Path(request['selections'][0]['target_path']).exists()


def test_actual_local_publication_keeps_original_attempt_and_reenters(tmp_path):
    async def run():
        durable,state,directive=fixture(tmp_path)
        request=admit(durable,state,directive)
        durable['task_state']['typed_recovery_receipts']={directive['directive_id']:{'result_resolution_request':request}}
        original=deepcopy(request['source_execution'])
        local=await tiny_copy(tmp_path,request,durable)
        receipt=result_resolution_reconciliation(request,local)
        assert receipt['source_attempt_id']=='attempt-1' and receipt['local_attempt_id']=='local-attempt-1'
        assert receipt['reconciliation']['status']=='accepted'
        target=Path(request['selections'][0]['target_path'])
        before={p.name:p.read_bytes() for p in target.iterdir()}
        again=await tiny_copy(tmp_path,request,durable)
        assert result_resolution_reconciliation(request,again)==receipt
        assert before=={p.name:p.read_bytes() for p in target.iterdir()}
        assert request['source_execution']==original
        assert json.loads((target/'checks.json').read_text())['checks'][0]['raw']['stdout'].startswith(str(tmp_path/'attempts/attempt-1'))
    asyncio.run(run())


@pytest.mark.parametrize('bad',['parent','missing_raw','extra_file','input_changed','superseded','receipt_changed','binding_changed','unknown_target','wrong_observation','plan_revision','approval_changed'])
def test_unacceptable_evidence_never_becomes_a_formal_output(tmp_path,bad):
    durable,state,directive=fixture(tmp_path)
    execution=state['execute_outcome']['payload']['execution_record']
    root=Path(execution['preflight_evidence_binding']['canonical_path'])
    if bad=='parent':directive['result_selections'][0]['observed_path']=str(tmp_path/'delivery')
    elif bad=='missing_raw':(root/'item-001.stdout.log').unlink()
    elif bad=='extra_file':(root/'unclaimed').write_text('unknown')
    elif bad=='input_changed':(tmp_path/'synthetic-input.txt').write_text('changed')
    elif bad=='superseded':durable['provenance_state']['attempts']['attempt-2']={'attempt_sequence':2,'step_id':'step-1','action_id':'another-action'}
    elif bad=='receipt_changed':(root/'checks.json').write_text('{}')
    elif bad=='binding_changed':execution['preflight_evidence_binding']['attempt_id']='another-attempt'
    elif bad=='unknown_target':(tmp_path/'delivery/00.input_check').mkdir()
    elif bad=='wrong_observation':execution['raw_stdout']=execution['stdout']='no structured observation'
    elif bad in {'plan_revision','approval_changed'}:
        request=admit(durable,state,directive)
        durable['task_state']['typed_recovery_receipts']={directive['directive_id']:{'result_resolution_request':request}}
        if bad=='plan_revision':durable['plan_revision']=int(durable.get('plan_revision') or 0)+1
        else:durable['approval_record_ref']='replacement-approval'
        with pytest.raises(StageContractError):get_result_resolution_request(durable,directive['directive_id'])
        return
    with pytest.raises((StageContractError,ValueError,OSError)):
        admit(durable,state,directive)
    assert not durable['task_state'].get('result_resolution_receipts')


@pytest.mark.parametrize('interruption',['partial','symlink','unknown_target','after_rename'])
def test_partial_directory_publication_resumes_only_its_owned_staging(tmp_path,interruption):
    import hashlib
    async def run():
        durable,state,directive=fixture(tmp_path)
        request=admit(durable,state,directive)
        durable['task_state']['typed_recovery_receipts']={directive['directive_id']:{'result_resolution_request':request}}
        row=request['selections'][0]
        spec={k:row[k] for k in ('observed_path','target_path','decision_id','artifact_id')}
        spec['files']=row['observed_record']['evidence_files']
        target=Path(row['target_path'])
        stage=target.parent/('.biomind-publish-'+hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:24])
        (stage/'pending').mkdir(parents=True)
        (stage/'owner.json').write_text(json.dumps(spec))
        (stage/'pending/checks.json').write_text('interrupted partial copy')
        if interruption=='symlink':
            import shutil
            shutil.rmtree(stage/'pending')
            outside=tmp_path/'outside';outside.mkdir()
            (stage/'pending').symlink_to(outside,target_is_directory=True)
            with pytest.raises(AssertionError):await tiny_copy(tmp_path,request,durable)
            assert not list(outside.iterdir()) and not target.exists()
            return
        if interruption in {'unknown_target','after_rename'}:
            import shutil
            pending=stage/'pending'
            shutil.rmtree(pending)
            shutil.copytree(row['observed_path'],pending)
            if interruption=='after_rename':
                st=pending.stat()
                (stage/'placement.json').write_text(json.dumps({'device':st.st_dev,'inode':st.st_ino}))
            pending.rename(target)
            if interruption=='unknown_target':
                with pytest.raises(AssertionError):await tiny_copy(tmp_path,request,durable)
                assert target.exists() and not (stage/'published.json').exists()
                return
        local=await tiny_copy(tmp_path,request,durable)
        assert result_resolution_reconciliation(request,local)['reconciliation']['status']=='accepted'
        assert not (stage/'pending').exists() and (stage/'published.json').exists()
    asyncio.run(run())


def test_seed_compares_prior_injected_binding_with_current_missing_declaration(tmp_path):
    from agent_core.workflow.recovery_projection import _helper_binding_facts
    durable,state,_=fixture(tmp_path)
    execution=deepcopy(state['execute_outcome']['payload']['execution_record'])
    previous=durable['provenance_state']['actions'][execution['action_id']]['normalized_action']
    previous.update(skill_resource_refs=[HANDLE],formal_skill_resources=[{'handle':HANDLE}])
    original=deepcopy(execution['preflight_evidence_binding'])
    directory=Path(original['canonical_path']).parent
    (directory/'preflight_evidence_binding.json').write_text(json.dumps(original))
    durable['provenance_state']['attempts'][execution['attempt_id']]['executed_script_path']=str(directory/'executed.sh')
    durable['task_state']['typed_recovery_receipts']={'retry':{'outcome':{'scope':{
        'action_id':execution['action_id'],'attempt_id':execution['attempt_id']}}}}
    execution.update(action_contract={},action_id='new-action',attempt_id='new-attempt',recovery_decision_id='retry',preflight_evidence_binding={})
    facts=_helper_binding_facts(execution,durable)
    assert facts[0]['metadata']['previous']['injected_evidence_directory']==original['canonical_path']
    assert facts[0]['metadata']['previous']['evidence_binding_declared'] is True
    assert facts[0]['metadata']['current']['helper_handles']==()
    assert facts[0]['metadata']['current']['injected_evidence_directory']==''


def archived_receipt(root):
    """Rebase historical receipt paths into a fixture; never inspect inputs."""
    import gzip
    source=json.loads(gzip.decompress((Path(__file__).parent/'fixtures/preflight_1789734892_receipt.json.gz').read_bytes()))
    old=source['source_attempt_root']+'/preflight'
    root.mkdir(parents=True)
    for name,row in source['files'].items():
        if name.startswith('preflight/'):
            (root/Path(name).name).write_text(row['content'].replace(old,str(root)))
    return source['files']['stdout.log']['content'].replace(old,str(root))


def test_archived_five_checks_form_an_explicit_finite_scope_candidate(tmp_path):
    import shutil
    durable,state,directive=fixture(tmp_path)
    execution=state['execute_outcome']['payload']['execution_record']
    root=Path(execution['preflight_evidence_binding']['canonical_path'])
    shutil.rmtree(root)  # owned synthetic fixture only
    execution['stdout']=execution['raw_stdout']=archived_receipt(root)
    refresh_packet(durable,state)
    request=admit(durable,state,directive)
    observed=request['selections'][0]['observed_record']
    assert len(observed['evidence_files'])==11 and observed['full_file_validated'] is False
    assert 'inspect_wgs_inputs.py' not in request['approved_code']
