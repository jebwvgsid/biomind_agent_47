from copy import deepcopy
import pytest
from agent_core.plan_bindings import normalize_public_action_bindings, PlanBindingError


def invocation():
 row={'canonical_resource_id':'artifact.other_db','resource_kind':'mutable_workspace','binding_revision':7,
  'binding_id':'fixed-resource','plan_handle':'plan.other_db','binding_digest':'digest7','operation_scope':{'operation':'prepare'}}
 rule={'produces_bindings':['choice','db'],'consumes_bindings':[], 'binding_types':{'choice':'string','db':'artifact_ref'},
  'artifact_binding_requirements':{'db':{'canonical_resource_id':'artifact.other_db','resource_kind':'mutable_workspace'}},
  'required_binding_env':{'DB_PATH':{'source':'produced_binding','name':'db'}}}
 frozen={'protocol':'public_handle_v2','operation':'prepare','binding_contract':{'operations':{'prepare':rule}}}
 action={'type':'execute','operation':'prepare','produced_bindings':{'choice':{'type':'string','value':'semantic'},
  'db':{'type':'artifact_ref','value':{'handle':'plan.other_db'}}}}
 return action,frozen,row


def test_generic_capability_exact_revision_and_raw_integrity():
 raw,frozen,row=invocation();old=deepcopy(raw)
 value,receipt=normalize_public_action_bindings(raw,frozen=frozen,scope={'task_id':'task-a'},workspace_bindings=[row])
 assert raw==old and value['produced_bindings']['db']['value']['binding_revision']==7
 assert value['binding_env']==frozen['binding_contract']['operations']['prepare']['required_binding_env']
 assert receipt['raw_digest'] != receipt['normalized_digest'] and receipt['raw_candidate']==old


@pytest.mark.parametrize('mutation,code',[
 ('unknown','BINDING_HANDLE_UNKNOWN'),('mixed','BINDING_INPUT_PROTOCOL_MIXED'),('unsupported','BINDING_INPUT_PROTOCOL_UNSUPPORTED'),
 ('ambiguous','BINDING_HANDLE_AMBIGUOUS'),('env','BINDING_ENV_CONTRACT_MISMATCH'),('cross_operation','BINDING_HANDLE_UNKNOWN')])
def test_rejections(mutation,code):
 action,frozen,row=invocation();rows=[row]
 if mutation=='unknown':action['produced_bindings']['db']['value']['handle']='other-task-handle'
 if mutation=='mixed':action['produced_bindings']['db']['value']['binding_revision']=7
 if mutation=='unsupported':action['binding_input_protocol']='v999'
 if mutation=='ambiguous':rows.append(deepcopy(row))
 if mutation=='env':action['binding_env']={'DB_PATH':{'source':'accepted_binding','name':'db'}}
 if mutation=='cross_operation':row['operation_scope']['operation']='other'
 with pytest.raises(PlanBindingError) as e:normalize_public_action_bindings(action,frozen=frozen,scope={},workspace_bindings=rows)
 assert e.value.code==code


def test_legacy_is_not_repaired_or_migrated():
 action,frozen,row=invocation();action['produced_bindings']['db']['value']={'canonical_resource_id':'artifact.other_db','resource_kind':'mutable_workspace','binding_revision':999}
 value,receipt=normalize_public_action_bindings(action,frozen=frozen,scope={},workspace_bindings=[row])
 assert value==action and 'binding_env' not in value and receipt['protocol']=='legacy_full_v1'
 value,receipt=normalize_public_action_bindings(action,frozen={},scope={},workspace_bindings=[row])
 assert value==action and not receipt


@pytest.mark.parametrize('field', ['task_id', 'plan_revision', 'step_id', 'execution_capability_id'])
def test_raw_public_scope_cannot_be_lost_by_parser(field):
 action,frozen,row=invocation()
 public=deepcopy(action);public[field]='foreign'
 with pytest.raises(PlanBindingError) as error:
  normalize_public_action_bindings(action,frozen=frozen,scope={'task_id':'task-a'},workspace_bindings=[row],raw_public_candidate=public)
 assert error.value.code=='BINDING_PUBLIC_SCOPE_MISMATCH'
