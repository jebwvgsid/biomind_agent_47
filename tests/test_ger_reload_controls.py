import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage


@pytest.mark.parametrize('mode',['cancel_review','changed_after_approval','changed_generated_reference','edited_body'])
def test_control_and_hash_boundaries_do_not_launch(mode):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.execution import TypedExecute
    import bio_agents
    async def scenario():
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp()
        task_id='ger-boundary-'+mode
        try:
            case._configure_one_step_no_output_plan()
            async def provider(_messages,*,stage,**_kwargs):
                if stage=='consultant':return AIMessage(content=json.dumps(case._consultant_payload(case.root/'report.md')))
                if stage in {'planner','planner_structure_repair','planner_repair'}:return AIMessage(content=json.dumps(case._planner_payload()))
                if stage=='generate':return AIMessage(content='<shell>printf "fixture\\n"</shell>')
                raise AssertionError(stage)
            case.agent._ainvoke_node_llm=provider
            runner=case._runner_state(task_id)
            app=case.agent._app_for_task(task_id,runner)
            config={'configurable':{'thread_id':task_id},'recursion_limit':20}
            stream=app.astream(runner,config=config,stream_mode='updates')
            try:
                async for event in stream:
                    if 'generate' in event:break
            finally:await stream.aclose()
            state=app.get_state(config).values
            action=state['pending_action']
            if mode=='cancel_review':
                def cancel(*_args,**_kwargs):raise asyncio.CancelledError
                case.agent.middleware.review_code=cancel
            if mode=='edited_body':
                case.agent.middleware.review_code=lambda *_args,**_kwargs:('edit','printf "edited body\\n"')
            if mode=='changed_generated_reference':
                # Simulate the existing durable generated-script reference
                # lane; this graph's inline candidate has not frozen a file yet.
                path=case.root/'generated.sh'
                original=b'printf "fixture\\n"'
                path.write_bytes(b'printf "changed"\n')
                action['generated_script']={'path':str(path),'sha256':hashlib.sha256(original).hexdigest()}
                state['generate_outcome']['payload']['action']=action
            if mode=='changed_after_approval':
                original=case.agent._persist_script_version
                def persist(*args,**kwargs):
                    reference=original(*args,**kwargs)
                    if args[4]=='approved':Path(reference['path']).write_text('printf "unapproved"\n')
                    return reference
                case.agent._persist_script_version=persist
            with patch.object(bio_agents,'run_script_path',side_effect=AssertionError('must not launch')) as launch:
                call=TypedExecute(executor=case.agent._schema2_runtime_sources['execute'],data_path=str(case.root)).run(dict(state))
                if mode=='cancel_review':
                    with pytest.raises(asyncio.CancelledError):await call
                else:
                    result=await call
                    if mode=='edited_body':
                        assert 'guidance' in str(result),result
                        assert not result.get('pending_action')
                    elif mode=='changed_after_approval':assert 'APPROVED_SCRIPT_HASH_DRIFT' in str(result),result
                    else:assert result['execute_outcome']['type']=='stale_result',result
                assert launch.call_count==0
            durable=load_plan_state(str(case.root),task_id)
            assert not any(a.get('returncode') is not None for a in durable['provenance_state']['attempts'].values())
        finally:await case.asyncTearDown()
    asyncio.run(scenario())
