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

async def scenario(candidates, name, *, late_cancel=False, readonly=False):
    import bio_agents
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    case.SKILL_ID = SID
    await case.asyncSetUp()
    # Restore production validation methods overridden by the broad older fixture.
    for method in ['_ensure_skill_runtime_ready','_selected_skill_env_runner_violation','_workflow_future_skill_violation']:
        delattr(case.agent, method)
    task = 'synthetic-resource-cycle-'+name
    declarations = json.loads((AUDIT/'task_declarations.json').read_text())
    # The graph's Consultant inspects paths before model dispatch. Give that
    # separate boundary nonexistent synthetic paths; never relax the read guard.
    for x in declarations['inputs']:
        x['canonical_path']=str(case.root/'输入 中文'/Path(x['canonical_path']).name)
    declarations['output_contracts'][0]['canonical_path']=str(case.root/'结果 新根'/'medium')
    paths = [x['canonical_path'] for x in declarations['inputs']]
    # Declaration strings are preserved and never opened. All state stays in /tmp.
    goal = 'Structure audit only: check contigs, prepare a NEW task-local workspace, build in it, annotate and summarize. Inputs read only: '+', '.join(paths)
    goal += '; Output root '+declarations['output_contracts'][0]['canonical_path']+'; required outputs annotated_vcf ann_summary report reproduce_script stage_status.'
    if readonly:
        declarations['output_contracts']=[]
        goal='Read only contig compatibility observation, no files requested. Inputs: '+', '.join(paths)
    persist = case.agent._persist_user_submission
    case.agent._persist_user_submission = lambda tid,_text:persist(tid,goal)
    operations = [{'operation':'set_goal','value':goal,'supporting_evidence':['U1']}]
    for x in declarations['inputs']:
        operations.append({'operation':'upsert_input','input':{'role':x['role'],'path':x['canonical_path'],'required':True},'supporting_evidence':['U1']})
    for x in declarations['output_contracts']:
        row={k:v for k,v in x.items() if k in ['output_id','role','artifact_kind','canonical_path','root_ref','path_policy','required','user_visible']}
        operations.append({'operation':'upsert_output_contract','output_contract':row,'supporting_evidence':['U1']})
    counts={'consultant':0,'planner':0}; requests=[]; states=[]; calls=[]; events=[]
    final_states=[]; external_stop=[]
    progress=case.agent._planner_progress
    def progress_observer(tid, event, payload):
        progress(tid,event,payload)
        if event=='candidate_rejected':
            states.append(deepcopy(load_plan_state(case.root,task)))
            if counts['planner']==len(candidates) and not late_cancel:
                external_stop.append('after durable rejection, before next dispatch')
                raise asyncio.CancelledError('external bounded audit ceiling')
    case.agent._planner_progress=progress_observer
    capture = case.agent._planner_observed_call
    def observe(tid, observation, phase, operation, *args, **kwargs):
        result = capture(tid, observation, phase, operation, *args, **kwargs)
        # Exact actual arguments to resource/effect/admission validators in this synthetic run.
        if phase in ['validate_wire_and_bind','compile_resource_bindings','compile_step_effects','compile_dataflow','validate_candidate','normalize_plan_contracts']:
            calls.append({'ordinal':counts['planner'],'phase':phase,'args':deepcopy(args),'kwargs':deepcopy(kwargs),'result':deepcopy(result)})
        return result
    case.agent._planner_observed_call=observe
    async def provider(messages, *, stage, **kwargs):
        assert stage in counts, 'No Generate, scientific execution or extra external stage allowed: '+stage
        if stage=='consultant':
            counts[stage]+=1
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'Structure only synthetic task.','selected_skills':[SID],'task_spec_operations':operations,'unresolved_ambiguities':[],'planner_feedback':None}))
        index=counts['planner']
        assert index < len(candidates), 'External bounded witness ceiling; not autonomous convergence'
        counts[stage]+=1
        requests.append([{'type':m.type,'name':getattr(m,'name',None),'content':m.content} for m in messages])
        if late_cancel:
            durable=load_plan_state(case.root,task)
            case.agent._commit_user_cancellation(task, source='audit', reason='Synthetic cancellation before a late response')
        return AIMessage(content=json.dumps(candidates[index]), response_metadata={'finish_reason':'stop'})
    case.agent._ainvoke_node_llm=provider
    def forbidden(*a, **k): raise AssertionError('Approval/execution forbidden in audit')
    case.agent._prompt_user=forbidden
    try:
        runner=case._runner_state(task)
        app=bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer, interrupt_after=['planner'])
        async def consume():
            for index in range(len(candidates)):
                stream=app.astream(runner if index==0 else None,config={'configurable':{'thread_id':task},'recursion_limit':60},stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        if 'planner' in event:
                            d=load_plan_state(case.root,task)
                            final_states.append(deepcopy(d))
                finally: await stream.aclose()
        try: await asyncio.wait_for(consume(),45)
        except asyncio.CancelledError:
            assert external_stop, 'Unexpected cancellation'
        if not final_states: final_states.append(deepcopy(load_plan_state(case.root,task)))
        result={'counts':counts,'states':states,'requests':requests,'validator_calls':calls,'events':events,
            'final_states':final_states,'external_stop':external_stop,
            'production_source':'exact export; no validator mocked','stop':'external bounded witness ceiling',
            'registry':case.agent.skill_manager.get_execution_capability_registry([SID])}
        save(name,result)
        return result
    finally: await case.asyncTearDown()

def issues(result, index=-1):
    return result['states'][index]['task_state']['planning_failure']['validation_issues']

def codes(result, index=-1): return [x['code'] for x in issues(result,index)]

def test_original_loop_and_actual_feedback():
    r=asyncio.run(scenario([raw(i) for i in range(1,8)],'original'))
    assert r['counts']['planner']==7
    for i in range(7):
        expected='PLAN_RESOURCE_REFERENCE_NOT_DECLARED' if i in [1,3,5] else 'STEP_EFFECT_PROTOCOL_INVALID'
        assert expected in codes(r,i)
        state=r['states'][i]
        assert not state['steps'] and not state['plan_artifacts']
        assert not state.get('current_approval')
        assert state['task_state']['planning_epoch']['planner_calls_total']==i+1
        if i<6:
            received=[]
            for m in r['requests'][i+1]:
                try: v=json.loads(m['content'])
                except (ValueError,TypeError):continue
                if isinstance(v,dict) and v.get('code')=='PLANNER_CANDIDATE_REJECTED':received.append(v)
            assert received and [x['code'] for x in received[-1]['validation_issues']]==codes(r,i)
    assert len({s['task_state']['planning_epoch']['planning_epoch_id'] for s in r['states']})==1
    assert r['states'][-1]['task_state']['planning_failure']['repeat_count']==0

@pytest.mark.parametrize('variant',['C','role','inherited','renamed','new_protocol_example'])
def test_public_c_expression_gap(variant):
    c=proposed_c(variant)
    r=asyncio.run(scenario([c],variant))
    assert not r['states'][-1]['steps'] and not r['states'][-1]['plan_artifacts']
    assert 'PLAN_RESOURCE_BINDING_MISSING' in codes(r)
    if variant in ['C','renamed','new_protocol_example']:
        assert codes(r).count('STEP_EFFECT_PROTOCOL_INVALID')==2
    assert [s['step_id'] for s in c['steps']][:4]==['check_contig_compatibility','prepare_database','build_database','annotate_variants']

def test_same_candidate_reentry_is_not_new_progress():
    renamed=raw(4)
    for step in renamed['steps']:step['title']='标题变化 '+step['title']
    renamed['public_message']='Different explanation, identical formal resource error.'
    r=asyncio.run(scenario([raw(4),renamed,raw(4),raw(4)],'same'))
    assert r['counts']['planner']==2
    assert [s['task_state']['planning_failure']['repeat_count'] for s in r['states']]==[0,1]
    assert not r['external_stop']
    assert r['final_states'][-1]['task_state']['planning_failure']['terminal_diagnostic']['error_code']=='PLANNER_REPEATED_FAILURE'

def test_compiled_candidate_uuid_is_spurious_structure_progress():
    r=asyncio.run(scenario([raw(3)]*3,'repeated_B'))
    failures=[s['task_state']['planning_failure'] for s in r['states']]
    assert len(failures)==3 and r['external_stop']
    assert len({f['issue_fingerprint'] for f in failures})==1
    assert len({f['candidate_correction_signature'] for f in failures})==3
    assert [f['repeat_count'] for f in failures]==[0,0,0]

def test_late_cancel_response():
    r=asyncio.run(scenario([proposed_c()], 'late_cancel',late_cancel=True))
    assert r['counts']['planner']==1
    assert not r['states'][-1]['steps'] and not r['states'][-1]['plan_artifacts']
    assert r['states'][-1]['task_state']['status']=='cancelled'
    assert r['validator_calls'], 'Defect witness: rejected late response still ran validators'

def readonly_candidate():
    c=raw(3); c['proposed_artifacts']=[];c['task_output_materializations']=[];c['steps']=c['steps'][:1]
    return c

def test_readonly_capability_positive_control():
    r=asyncio.run(scenario([readonly_candidate()],'readonly',readonly=True))
    s=r['final_states'][-1]
    assert s['steps'] and not r['states']
    assert s['current_approval']['status']=='pending'
    assert not r['external_stop']

def test_late_valid_candidate_does_not_gain_approval():
    r=asyncio.run(scenario([readonly_candidate()],'late_valid',readonly=True,late_cancel=True))
    s=r['final_states'][-1]
    assert s['task_state']['status']=='cancelled'
    assert s['current_approval']['status']=='pending'
    assert s['steps'], 'Defect witness: valid late candidate is persisted after cancellation'
    # Record whether admission persists a cancelled candidate; do not assert a fix.
    save('late_valid_boundary', {'candidate_persisted':bool(s['steps']),'approval_status':s['current_approval']['status'],'validator_phases':[x['phase'] for x in r['validator_calls']]})

def test_rejection_keeps_old_plan_without_review_eligibility():
    from agent_core.plan_tracker import record_planning_failure
    from agent_core.planner_loop import plan_review_submission_readiness, plan_review_acceptance_readiness
    r=asyncio.run(scenario([readonly_candidate()],'old_plan',readonly=True))
    old=r['final_states'][-1]
    rejected=record_planning_failure(old, validation_issues=[{'code':'PLAN_RESOURCE_REFERENCE_NOT_DECLARED','field_path':'$.proposed_artifacts[1].resource_binding'}],
        planning_epoch_id=old['task_state']['planning_epoch']['planning_epoch_id'], task_spec_revision_value=1,
        validation_source='planner_candidate_admission',candidate=raw(4))
    assert rejected['steps']==old['steps']
    assert rejected['task_state']['plan_review_submission']==old['task_state']['plan_review_submission']
    assert not plan_review_submission_readiness(rejected)['ready']
    assert not plan_review_acceptance_readiness(rejected)['ready']
    save('old_plan_readiness',{'submission':plan_review_submission_readiness(rejected),'acceptance':plan_review_acceptance_readiness(rejected),'old_plan_preserved':True})

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
