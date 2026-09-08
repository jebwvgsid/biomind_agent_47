"""Production Generate emits its own terminal; graph/outer call summary once."""
import asyncio
from copy import deepcopy
from langgraph.checkpoint.memory import InMemorySaver
from tests import test_generate_session_1788681718_production_path as gen_fixture
from tests.test_common_finalization_workflow import SummaryModel
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.managed_stop import current_managed_stop
from agent_core.plan_tracker import load_plan_state
from execution.profiling import RunProfiler


def test_production_generate_terminal_before_attempt_calls_common_outer():
    async def scenario():
        fixture=gen_fixture.Session1788681718GenerateProductionPathTests(methodName='test_adm_01_saved_candidate_reaches_fake_executor_once')
        await fixture.asyncSetUp()
        try:
            task_id='common-generate-no-attempt'
            state=fixture._approved_generate_state(task_id)
            fixture.provider.calls.clear()
            async def failed_provider(messages,**kwargs):
                fixture.provider.calls.append(list(messages))
                raise ConnectionError('synthetic Generate transport unavailable')
            fixture.provider.ainvoke=failed_provider
            agent=fixture.agent
            model=SummaryModel();agent.terminal_summary_llm=model
            agent.terminal_summary_policy={'enabled':True,'timeout_seconds':1}
            agent.profiler=RunProfiler(fixture.root/'task_outputs');agent.profiler.start_task(task_id)
            agent.state_manager=None
            async def forbidden(state):raise AssertionError('No Execute/Recovery/CommitStep authorized')
            graph=build_schema2_runtime_graph({'generate':agent._schema2_runtime_node_handlers['generate'],**{n:forbidden for n in ('execute','supervisor','recovery','commit_step','final_audit')}}).compile(checkpointer=InMemorySaver())
            config={'configurable':{'thread_id':task_id},'recursion_limit':12}
            agent._app_for_task=lambda _:graph
            async def go(*args,**kwargs):await graph.ainvoke(deepcopy(state),config)
            agent.go=go
            result=await agent._run_agent_interrupt_loop('synthetic Generate protocol failure',task_id)
            durable=load_plan_state(fixture.root,task_id);stop=current_managed_stop(durable)
            assert stop and stop['stage']=='generate',result
            assert not (durable.get('provenance_state') or {}).get('attempts')
            assert len(model.requests)==1
            assert result.failure_code!='TASK_BLOCKED'
            assert graph.get_state(config).values['generate_outcome']['type']=='provider_failed'
        finally:await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(),45))
