"""Current protocol in real scoped provider calls, with historical error retained."""
import asyncio
from copy import deepcopy
import io
import json
from langchain_core.messages import AIMessage
from agent_core.plan_contract import PLAN_RESULT_PATH_POLICIES, structured_plan_candidate_diagnostics
from tests.test_plan_result_contract_v1 import candidate


def test_path_policies_remain_distinct_and_legacy_is_rejected():
    from agent_core.plan_contract import plan_result_contracts
    for policy in sorted(PLAN_RESULT_PATH_POLICIES):
        plan = candidate(required=True)
        plan['proposed_artifacts'][0]['artifact_contract']['path_policy'] = policy
        assert structured_plan_candidate_diagnostics(plan) == []
        assert plan_result_contracts(plan)['plan.result']['path_policy'] == policy
    plan['proposed_artifacts'][0]['artifact_contract']['path_policy'] = 'planner_materialized'
    issues = structured_plan_candidate_diagnostics(plan)
    assert any(i['field_path'].endswith('.path_policy') for i in issues)
    assert plan['proposed_artifacts'][0]['artifact_contract']['path_policy'] == 'planner_materialized'


async def protocol_flow():
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_cli_model_progress_integration import artifact
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.plan_tracker import load_plan_state
    service_writer_callbacks(asyncio.get_running_loop())
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp(); configure_protocol(case); case._configure_one_step_no_output_plan()
    owner = PlainTerminal(io.StringIO(), enabled=False); previous = bind_terminal(owner)
    requests=[]; counts={'consultant':0, 'planner':0, 'generate':0}
    task_id='synthetic-protocol-correction'
    async def provider(messages, *, stage, **kwargs):
        counts[stage]+=1
        requests.append({'stage':stage,'messages':[{'type':m.type,'content':deepcopy(m.content)} for m in messages]})
        if stage=='consultant':return AIMessage(content=json.dumps(case._consultant_payload(case.root/'unused.md')))
        assert stage=='planner', counts
        plan=case._planner_payload(); plan['metadata']['result_contract_version']=1
        plan['steps'][0]['produces_observations']=['approved_command_succeeded']
        if counts['planner']==1:
            plan['task_output_materializations']=[{'output_ref':f'output.synthetic{i}', 'path':f'synthetic{i}.txt',
                'required':True,'path_policy':'planner_materialized'} for i in range(6)]
        return AIMessage(content=json.dumps(plan), response_metadata={'finish_reason':'stop'})
    case.agent._ainvoke_node_llm=provider; case.agent._ainvoke_llm=provider
    try:
        runner=case._runner_state(task_id); app=case.agent._app_for_task(task_id,runner)
        stream=app.astream(runner,config={'configurable':{'thread_id':task_id},'recursion_limit':12},stream_mode='updates')
        events=[]
        try:
            async for event in stream:
                events.append(event)
                if 'planner' in event:break
        finally:await stream.aclose()
        artifact('pf1-protocol.json',{'requests':requests,'counts':counts,'events':events,'durable':load_plan_state(case.root,task_id)})
        assert counts=={'consultant':1,'planner':2,'generate':0},counts
        assert events[-1]['planner']['planner_outcome']['type']=='plan_review_ready',events[-1]
        planner=[r for r in requests if r['stage']=='planner']
        for row in planner:
            system='\n'.join(str(m['content']) for m in row['messages'] if m['type']=='system')
            assert 'Current Plan result path_policy values are: managed_exact, model_selectable.' in system
            assert 'For path_policy=planner_materialized' not in system
        corrected=json.dumps(planner[1]['messages'])
        for i in range(6):assert f'$.task_output_materializations[{i}].path_policy' in corrected
        assert 'planner_materialized' in corrected and 'managed_exact or model_selectable' in corrected
        now=load_plan_state(case.root,task_id)
        assert not (now.get('provenance_state') or {}).get('attempts')
    finally:
        bind_terminal(previous);owner.close();await case.asyncTearDown()


def test_final_provider_messages_one_local_correction_for_six_fields():
    asyncio.run(protocol_flow())
