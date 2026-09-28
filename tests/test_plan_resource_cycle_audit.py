"""Defect witnesses only. Production FIX NOT IMPLEMENTED.

Run only through the guarded /tmp export. Original sessions are never loaded.
External model and host discovery are inert fixtures; parser, admission,
effects, feedback, durable claims, typed routing and review eligibility are real.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import os
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

AUDIT = Path(__file__).resolve().parents[1] / 'docs/audits/session_1789369423_plan_resource_cycle'
SID = 'genomics/snpeff-annotation'

def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

def raw(number):
    return json.loads((AUDIT / f'candidate_{number:02d}.json').read_text())

def proposed_c(variant='C'):
    """Compact public candidate; preserves every required result and ordered mutation."""
    c = raw(3)
    for a in c['proposed_artifacts']:
        a['semantic_view']['description'] = 'New task local resource; no existing resource is claimed.'
        a['artifact_contract'].pop('source_refs', None)
    for s in c['steps']:
        s['title'] = s['step_id']
    if variant == 'role':
        for s in c['steps']:
            for m in s.get('workspace_mutations', []): m['workspace_ref'] = 'artifact.snpeff_data_dir'
    elif variant == 'inherited':
        for s in c['steps']: s.pop('workspace_mutations', None)
    elif variant == 'renamed':
        c = json.loads(json.dumps(c).replace('plan.snpeff_data_dir', 'plan.alternate_workspace'))
    elif variant == 'new_protocol_example':
        c['metadata'].pop('result_contract_version', None)
        c['task_output_materializations']=[{k:v for k,v in x.items() if k in ['output_ref','path']} for x in c['task_output_materializations']]
        for a in c['proposed_artifacts']:
            a['artifact_contract'] = {k:v for k,v in a['artifact_contract'].items() if k in ['object_kind','lifecycle','resource_kind','materialization_policy_id']}
    return c

def save(name, value):
    root = Path(os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']); root.mkdir(parents=True, exist_ok=True)
    (root / (name+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')


def issues(result, index=-1):
    return result['states'][index]['task_state']['planning_failure']['validation_issues']

def codes(result, index=-1): return [x['code'] for x in issues(result,index)]






def readonly_candidate():
    c=raw(3); c['proposed_artifacts']=[];c['task_output_materializations']=[];c['steps']=c['steps'][:1]
    return c




def test_claim_replay_and_reload():
    from agent_core.plan_tracker import build_plan_state, save_plan_state, load_plan_state
    from agent_core.plan_contract import start_planning_epoch
    from agent_core.planner_recovery import claim_planner_call, complete_planner_claim, PlannerCallError
    import tempfile
    root=Path(tempfile.mkdtemp(prefix='reload-state-',dir=os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']))
    task='new-synthetic-budget-reload'
    state=build_plan_state(task,'')
    state['task_state']['planning_epoch']=start_planning_epoch(task_revision=0,task_semantic_identity='',base_plan_revision=0)
    save_plan_state(root,task,state);state=load_plan_state(root,task)
    scope={'task_id':task,'task_revision':0,'planning_epoch_id':state['task_state']['planning_epoch']['planning_epoch_id'],'invocation_id':'audit-call','candidate_scope_id':'audit-scope'}
    state,claim=claim_planner_call(root,task,state,scope,{})
    with pytest.raises(PlannerCallError,match='unsettled|prior claimed'):
        claim_planner_call(root,task,load_plan_state(root,task),scope,{})
    integrity={'scope':scope,'raw_digest':'fixed-raw-digest'}
    complete_planner_claim(state,claim,integrity,outcome='audit_result')
    before=deepcopy(state)
    complete_planner_claim(state,claim,integrity,outcome='audit_result')
    assert state==before
    save_plan_state(root,task,state)
    reloaded=load_plan_state(root,task)
    assert reloaded['task_state']['planning_epoch']['planner_calls_total']==1
    save('reload_claim',{'root':str(root),'task_id':task,'scope':scope,'claim':claim,'integrity':integrity,'counter':1})

def test_reload_claim_in_fresh_process():
    """Run separately after test_claim_replay_and_reload with the same results root."""
    from agent_core.plan_tracker import load_plan_state
    from agent_core.planner_recovery import complete_planner_claim
    receipt=Path(os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS'])/'reload_claim.json'
    if not receipt.exists(): pytest.skip('Run the claim writer first; no original session fallback')
    d=json.loads(receipt.read_text());state=load_plan_state(d['root'],d['task_id'])
    complete_planner_claim(state,d['claim'],d['integrity'],outcome='audit_result')
    assert state['task_state']['planning_epoch']['planner_calls_total']==1
    save('fresh_process_reload',{'pid':os.getpid(),'counter':1,'epoch':state['task_state']['planning_epoch']['planning_epoch_id']})

def test_formal_path_text_parser_and_admission_seams():
    """Exact original path spelling; no loader or filesystem access to those paths."""
    from agent_core.node_context import build_node_context, resolve_plan_proposal_handles
    from agent_core.plan_contract import parse_planner_outcome_envelope, validate_planner_candidate_deterministically
    from agent_core.plan_bindings import compile_plan_resource_bindings
    from agent_core.plan_tracker import build_plan_state, normalize_plan_contracts
    from agent_core.step_effects import compile_plan_step_effects, compile_plan_dataflow
    from agent_core.skill_manager import SkillManager
    task={'schema_version':1,'task_id':'new-formal-path-declaration-only', **json.loads((AUDIT/'task_declarations.json').read_text())}
    ctx=build_node_context(node_type='planner',task_spec=task,plan_state={},state_generation=0)
    authority={k:v for k,v in ctx.binding.semantic_handle_map.items() if v.get('object_type')=='artifact'}
    handles={k:v['stable_identity'] for k,v in authority.items()}
    origins={v['stable_identity']:'verified_task_input' if v.get('direction')=='input' else 'not_yet_available' for v in authority.values()}
    registry=SkillManager(str(AUDIT.parents[2]/'skills')).get_execution_capability_registry([SID])
    c=parse_planner_outcome_envelope(json.dumps(proposed_c()),allow_legacy_plan=False).plan_candidate_body()
    kw={'candidate_scope_id':ctx.binding.candidate_scope_id,'authoritative_handles':handles,'availability_origins':origins,'authoritative_bindings':authority,'require_declared_references':True}
    refs,usage,errors=validate_planner_candidate_deterministically(c,**kw,capability_ids=tuple(registry),validate_dataflow=False)
    assert not errors
    bindings=compile_plan_resource_bindings(c,**kw)
    resolved=resolve_plan_proposal_handles(c,ctx.binding)
    state=build_plan_state('new-formal-path-declaration-only',json.dumps(resolved),strict=True)
    state['artifact_usage']=usage
    state,receipt,normalization_errors=normalize_plan_contracts(state,renderer_records=[],output_contracts=task['output_contracts'],task_spec=task)
    compiled,effect_receipt,effect_errors=compile_plan_step_effects(state,registry,allow_legacy_untyped=False,resource_bindings=bindings)
    flowed,flow_receipt,flow_errors=compile_plan_dataflow(compiled,registry,resource_bindings=bindings,compile_effect_contracts=False,validate_dependencies=True,allow_legacy_untyped=False)
    assert [x['violation_code'] for x in effect_errors]==['STEP_EFFECT_CAPABILITY_MISMATCH']*2
    assert [x['code'] for x in flow_errors].count('PLAN_RESOURCE_BINDING_MISSING')==2
    save('formal_path_seams',{'candidate':c,'actual_binding':authority,'parser_identity_errors':errors,'bindings':bindings,'effect_errors':effect_errors,'flow_errors':flow_errors,'normalization_errors':normalization_errors,
        'meaning':'Native ordered admission seams with original path text; complete graph entry is separately witnessed on synthetic paths'})
