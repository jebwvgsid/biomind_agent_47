"""Real feedback -> Planner correction -> review -> harmless execution."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage


async def lifecycle_flow(mode='compact', stale_approval=False, stale_provider=False, force_preflight=False):
    import bio_agents
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_cli_model_progress_integration import artifact
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.plan_tracker import load_plan_state, save_plan_state, record_planning_failure
    from agent_core.planner_loop import (plan_review_submission_readiness, classify_planner_lifecycle,
        plan_review_acceptance_readiness, record_plan_review_acceptance)
    service_writer_callbacks(asyncio.get_running_loop())
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp(); configure_protocol(case); case._configure_one_step_no_output_plan()
    owner=PlainTerminal(io.StringIO(),enabled=mode!='off',verbosity=mode if mode=='verbose' else 'compact')
    previous=bind_terminal(owner);task_id='synthetic-feedback-boundary'
    counts={'consultant':0,'planner':0,'generate':0,'planner_plan_impact_review':0};requests=[];reviews=[];executions=[];states={};events=[]
    feedback='只检查并记录合成命令退出状态，不生成文件。'
    async def provider(messages, *, stage, node_context=None, **kw):
        counts[stage]+=1
        requests.append({'stage':stage,'messages':[{'type':m.type,'content':deepcopy(m.content)} for m in messages]})
        if stage=='consultant':
            payload=case._consultant_payload(case.root/'unused.md')
            if counts[stage]>1:
                payload['task_spec_operations']=[{'operation':'set_goal','value':feedback,'supporting_evidence':['U2']}]
            return AIMessage(content=json.dumps(payload))
        if stage=='planner_plan_impact_review':
            durable=load_plan_state(case.root,task_id)
            pending=durable['task_state']['pending_plan_impact_review']
            plan=case._planner_payload();plan['steps'][0]['produces_observations']=['approved_command_succeeded']
            return AIMessage(content=json.dumps({'decision':'FULL_REPLAN',
                'base_task_revision':pending['base_task_revision'],
                'new_task_revision':durable['current_task']['task_revision'],
                'base_plan_revision':durable['plan_revision'],
                'reason':'The user changed the required task goal to a readonly check.',
                'replacement_steps':plan['steps']}))
        if stage=='planner':
            plan=case._planner_payload();plan['metadata']['result_contract_version']=1
            plan['steps'][0]['produces_observations']=['approved_command_succeeded']
            if counts[stage]==2:
                if stale_provider:
                    case.agent._persist_user_submission(task_id,'A newer synthetic input arrived during this provider call.')
                states['before_invalid']=load_plan_state(case.root,task_id)
                assert states['before_invalid']['current_task']['task_revision']==2
                assert len(reviews)==1 and counts['generate']==0 and len(executions)==0
                plan['task_output_materializations']=[{'output_ref':f'output.synthetic{i}','path':f'synthetic{i}.txt',
                    'required':True,'path_policy':'planner_materialized'} for i in range(6)]
            if counts[stage]==3:
                states['after_invalid']=load_plan_state(case.root,task_id)
                assert len(reviews)==1 and counts['generate']==0 and len(executions)==0
                assert not plan_review_submission_readiness(states['after_invalid'])['ready']
                assert not plan_review_acceptance_readiness(states['after_invalid'])['ready']
                assert states['after_invalid']['task_state']['plan_review_submission']==states['v1']['task_state']['plan_review_submission']
                text=json.dumps(requests[-1]['messages'])
                for i in range(6):assert f'$.task_output_materializations[{i}].path_policy' in text
            assert counts[stage]<=3,counts
            return AIMessage(content=json.dumps(plan),response_metadata={'finish_reason':'stop'})
        assert stage=='generate' and counts[stage]==1
        states['generate']=load_plan_state(case.root,task_id)
        assert states['generate']['generated_from_task_revision']==2
        return AIMessage(content="<shell>printf '%s\\n' 'BIOMIND_EFFECT_EVIDENCE_JSON={\"schema_version\":1,\"produces_observations\":[{\"identity\":\"approved_command_succeeded\",\"value\":true}]}'</shell>")
    def prompt(task,stage,text,**kw):
        assert stage=='plan_review'
        durable=load_plan_state(case.root,task_id);reviews.append(deepcopy(durable['task_state']['plan_review_submission']))
        if len(reviews)==1:
            states['v1']=deepcopy(durable)
            # Same revision/new rejected candidate: the actual producer keeps
            # old receipt but its current lifecycle cannot borrow readiness.
            rejected=record_planning_failure(durable,validation_issues=[{'code':'SYNTHETIC_REJECT','field_path':'$.steps'}],
                planning_epoch_id=durable['task_state']['planning_epoch']['planning_epoch_id'],
                task_spec_revision_value=1,validation_source='planner_candidate_admission',candidate={'steps':[]})
            assert rejected['task_state']['plan_review_submission']==durable['task_state']['plan_review_submission']
            assert not plan_review_submission_readiness(rejected)['ready']
            assert classify_planner_lifecycle(rejected)['disposition']=='continue'
            states['same_revision_rejected']=rejected
            return feedback
        if stale_approval:
            case.agent._persist_user_submission(task_id,'A new synthetic task replaces the displayed request.')
            return 'y'
        states['corrected']=deepcopy(durable)
        return 'y'
    async def execute(path, *, expected_sha256, lifecycle_callback=None, **kw):
        from execution.executor import run_script_path
        allow=sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected_sha256
        allow.add(str(path))
        try:
            result=await run_script_path(path,expected_sha256=expected_sha256,lifecycle_callback=lifecycle_callback,
                profile=False,env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'},managed_log_paths=kw.get('managed_log_paths'))
        finally:allow.discard(str(path))
        executions.append({'sha256':expected_sha256,'result':result});return result
    case.agent._ainvoke_node_llm=provider;case.agent._ainvoke_llm=provider;case.agent._prompt_user=prompt
    try:
        runner=case._runner_state(task_id);app=case.agent._app_for_task(task_id,runner)
        config={'configurable':{'thread_id':task_id},'recursion_limit':25}
        with patch.object(bio_agents,'run_script_path',execute):
            stream=app.astream(runner,config=config,stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    if ('execute' in event or (stale_provider and counts['planner']==2 and 'planner' in event) or (force_preflight and 'generate' in event) or (stale_approval and len(reviews)==2 and 'plan_review' in event)):break
            finally:await stream.aclose()
        durable=load_plan_state(case.root,task_id)
        if force_preflight:
            checkpoint=dict(app.get_state(config).values)
            # Deliberately inject an old Plan binding into this synthetic
            # checkpoint to exercise the unmodified last execution guard.
            durable['generated_from_task_revision']=1
            save_plan_state(case.root,task_id,durable)
            with patch.object(bio_agents,'run_script_path',execute):
                preflight=await case.agent._schema2_runtime_node_handlers['execute'](checkpoint)
            assert preflight['execute_outcome']['type']=='stale_result',preflight
            assert executions==[]
            states['preflight']=preflight
            durable=load_plan_state(case.root,task_id)
        artifact('life1-'+mode+'-'+str(stale_approval)+'-'+str(stale_provider)+'-'+str(force_preflight)+'.json',{'counts':counts,'reviews':reviews,
            'executions':executions,'states':states,'events':events,'requests':requests,'final':durable})
        if stale_provider:
            assert counts['generate']==0 and executions==[] and len(reviews)==1
            assert events[-1]['planner']['planner_outcome']['type']=='stale_result',events[-1]
            assert durable['task_state']['plan_review_submission']==states['v1']['task_state']['plan_review_submission']
            return counts
        assert len(reviews)==2,events
        if force_preflight:
            assert all(not a.get('execution_started') for a in durable['provenance_state']['actions'].values())
            return counts
        if stale_approval:
            assert counts['generate']==0 and executions==[]
            assert events[-1]['plan_review']['plan_review_outcome']['type']=='revision_required'
            assert not plan_review_acceptance_readiness(durable)['ready']
            from agent_core.human_io import save_waiting_state
            save_waiting_state(case.root,task_id,stage='plan_review',prompt='Synthetic stale approval',payload={},reason='fixture',timeout_seconds=0)
            resume=case.agent._resolve_schema2_resume_state(task_id,dict(app.get_state(config).values))
            assert resume.type=='planning_ready',resume
        else:
            assert counts=={'consultant':2,'planner':3,'generate':1,'planner_plan_impact_review':1},counts
            assert len(executions)==1 and executions[0]['result'][2]==0,events
            assert reviews[0]['submission_id']!=reviews[1]['submission_id']
            assert reviews[0]['task_revision']==1 and reviews[1]['task_revision']==2
            assert record_plan_review_acceptance(durable)==durable
            # Production checkpoint state was made by the graph. Re-delivering
            # review cannot acquire another human approval or execute again.
            checkpoint=dict(app.get_state(config).values)
            repeated=await case.agent._schema2_early_node_handlers['plan_review'](checkpoint)
            assert len(reviews)==2 and len(executions)==1
            assert repeated['plan_review_outcome']['type']=='approved_for_execution'
        return counts
    finally:
        bind_terminal(previous);owner.close();await case.asyncTearDown()


def test_current_revision_correction_real_review_and_single_execution():
    async def run():
        results=[await lifecycle_flow(mode) for mode in ('compact','verbose','off')]
        assert results[1:]==results[:1]*2
    asyncio.run(run())


def test_stale_approval_is_checked_again_at_commit():
    asyncio.run(lifecycle_flow(stale_approval=True))


def test_late_provider_does_not_replace_current_submission():
    asyncio.run(lifecycle_flow(stale_provider=True))


def test_forced_stale_action_still_cannot_start_at_execute():
    asyncio.run(lifecycle_flow(force_preflight=True))
