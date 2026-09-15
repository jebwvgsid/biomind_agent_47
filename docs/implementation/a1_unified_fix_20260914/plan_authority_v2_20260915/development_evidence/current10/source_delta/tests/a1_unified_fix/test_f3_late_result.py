"""F3/IT09 post-await cancellation for valid, invalid and blocked candidates.

Two barriers, no sleeps or scientific execution. Formal Plan is produced and
approved by existing production graph from a fixed provider. Only new scratch
state is changed through cancellation API / ordinary save_plan_state.
"""
import asyncio
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage

from tests.test_preflight_recovery_audit import save


async def scenario(mode,late_kind):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol
    from agent_core.plan_tracker import load_plan_state,save_plan_state
    from agent_core.workflow.runtime_graph import route_generate_outcome
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    case.SKILL_ID='genomics/snpeff-annotation'
    await case.asyncSetUp()
    waiting,released=asyncio.Event(),asyncio.Event()
    task='fix-f3-late-generate-'+mode+'-'+late_kind
    counts={'consultant':0,'planner':0,'generate':0,'action_approval':0}
    snapshots={};events=[];post_cancel_outcomes=[]
    changer=None
    try:
        capability_id='genomics.snpeff_annotation.check_contig_compatibility'
        configure_protocol(case,execution_capability_id=capability_id)
        case._configure_one_step_no_output_plan(execution_capability_id=capability_id)
        original=case._planner_payload
        def plan():
            value=original();value['steps'][0].update(
              execution_capability_id='genomics.snpeff_annotation.check_contig_compatibility',
              operation='check_contig_compatibility',produces_observations=[{
                'identity':'contigs_compatible','evidence_contract':{'required_fields':['value']}}])
            return value
        case._planner_payload=plan
        def review(code,language,**kwargs):
            counts['action_approval']+=1
            return 'approve',code
        case.agent.middleware.review_code=review
        async def provider(messages,*,stage,node_context=None,**kwargs):
            assert stage in counts,stage
            counts[stage]+=1
            if stage=='consultant':return AIMessage(content=json.dumps(case._consultant_payload(case.root/'unused.md')))
            if stage=='planner':return AIMessage(content=json.dumps(case._planner_payload()))
            assert counts['generate']==1
            snapshots['binding']=node_context.binding.to_dict()
            waiting.set()
            await released.wait()
            if late_kind=='invalid':
                return AIMessage(content=json.dumps({'action_type':'execute','argv':['/usr/bin/printf','synthetic'],'env':[]}))
            if late_kind=='blocked':
                return AIMessage(content=json.dumps({'action_type':'blocked','reason':'No new synthetic evidence to justify another attempt.','missing_evidence':['measurement']}))
            return AIMessage(content=json.dumps({'action_type':'execute' ,'operation':'check_contig_compatibility','language':'bash',
              'code':"printf '%s\\n' 'BIOMIND_EFFECT_EVIDENCE_JSON={\"schema_version\":1,\"produces_observations\":[{\"identity\":\"contigs_compatible\",\"value\":false}]}'",'env':{},'outputs':[]}))
        case.agent._ainvoke_node_llm=provider
        async def mutate_while_awaited():
            await waiting.wait()
            before=load_plan_state(case.root,task)
            snapshots['before']={k:deepcopy(before.get(k)) for k in ('state_generation','plan_id','plan_revision','plan_version','current_step_id','approval_status')}
            if mode=='cancel':
                snapshots['cancellation_receipt']=case.agent._commit_user_cancellation(task,source='audit_generate_provider_await',reason='Explicit synthetic user cancellation during awaited provider')
            else:
                before['task_state']['audit_nonsemantic_observer_note']='Synthetic metadata only; no task/Plan/Skill/user/terminal authority change.'
                save_plan_state(case.root,task,before)
            after=load_plan_state(case.root,task)
            snapshots['after_mutation']={k:deepcopy(after.get(k)) for k in ('state_generation','plan_id','plan_revision','plan_version','current_step_id','approval_status')}
            snapshots['after_mutation']['task_status']=after['task_state'].get('status')
            released.set()
        changer=asyncio.create_task(mutate_while_awaited())
        runner=case._runner_state(task);app=case.agent._app_for_task(task,runner)
        stream=app.astream(runner,config={'configurable':{'thread_id':task},'recursion_limit':20},stream_mode='updates')
        outcome=None
        try:
            async for event in stream:
                events.append(next(iter(event)))
                if 'generate' in event:
                    outcome=event['generate'].get('generate_outcome')
                    if outcome and (mode!='cancel' or outcome['type']=='action_ready'):break # Never dispatch an executable.
                elif outcome is not None:
                    for update in event.values():
                        if isinstance(update,dict):
                            post_cancel_outcomes.extend(deepcopy(v) for k,v in update.items() if k.endswith('_outcome') and isinstance(v,dict))
        finally:await stream.aclose()
        await changer
        durable=load_plan_state(case.root,task)
        pending=durable['task_state'].get('pending_action') or {}
        result={'mode':mode,'counts':counts,'routes_observed':events,'outcome':outcome,
          'next_route':route_generate_outcome({'generate_outcome':outcome}), 'post_cancel_outcomes':post_cancel_outcomes,'barrier':snapshots,
          'durable_status':durable['task_state'].get('status'),'pending_action':pending,
          'provider_claims':durable['task_state'].get('candidate_repair_state',{}).get('provider_claims',{}),
          'candidate_rejection_events':durable['task_state'].get('candidate_repair_state',{}).get('events',{}),
          'attempts':durable.get('provenance_state',{}).get('attempts',{}),
          'loaded_source':{name:hashlib.sha256(Path(__import__(name).__file__).read_bytes()).hexdigest() for name in ('bio_agents',)},
          'fixture_boundary':'fixed provider and Plan yes input; runtime readiness/memory hooks are existing fixture stubs; actual TypedGenerate/parser/CAS/staleness and route; no Execute dispatched'}
        save('f3-late-'+mode+'-'+late_kind,result)
        assert outcome is not None and counts['generate']==1
        assert snapshots['after_mutation']['state_generation']>snapshots['before']['state_generation']
        assert counts['action_approval']==0 and not result['attempts']
        if mode=='cancel':
            assert result['durable_status']=='cancelled'
            assert outcome['type']!='action_ready' and not pending, result
            assert post_cancel_outcomes[-1]['type']=='terminal'
            assert post_cancel_outcomes[-1]['payload']['status']=='cancelled'
            assert counts['planner']==1, 'Cancelled task must not call Planner model again'
        else:
            assert outcome['type']=='action_ready' and pending.get('action_id'),result
            assert result['next_route']=='execute'
    finally:
        if changer is not None and not changer.done():changer.cancel()
        await case.asyncTearDown()


@pytest.mark.parametrize('mode,late_kind',[('cancel','valid'),('cancel','invalid'),('cancel','blocked'),('nonsemantic','valid')])
def test_f3_generate_post_await_boundary(mode,late_kind):
    asyncio.run(asyncio.wait_for(scenario(mode,late_kind),45))
