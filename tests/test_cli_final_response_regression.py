"""Real direct answer, Supervisor, and existing final-summary display paths.

Run only via the guarded allowlisted export. All providers are synthetic and
all business stores are newly created temporary fixtures.
"""
import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from tests.cli_model_progress_support import TimedWriter
from tests.test_cli_model_progress_integration import artifact


class AnswerProvider:
    def __init__(self, text):
        self.text = text
        self.requests = []

    async def ainvoke(self, messages, config=None):
        self.requests.append(deepcopy(list(messages)))
        return AIMessage(id='synthetic-direct-response', content=self.text)


def new_case():
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    return PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')


@pytest.mark.parametrize('reply', [
    '这是合成问题的直接回答，数值为 7。',
    '这是另一条直接回答，字段名 public_message 只是示例。',
])
def test_actual_direct_response_stays_readable_without_cpg_protocol(reply):
    async def scenario(mode):
        import bio_agents
        case = new_case()
        await case.asyncSetUp()
        writer = TimedWriter()
        owner = PlainTerminal(writer, enabled=mode != 'off')
        previous = bind_terminal(owner)
        router = bio_agents.output
        router.reset_for_task()
        provider = AnswerProvider(reply)
        case.agent.llm = provider
        finalized = []
        async def finalize(*args, **kwargs):
            finalized.append((args, kwargs))
        case.agent._finalize_task_memory = finalize
        state = {'task_id': 'cli-direct-synthetic', 'messages': [HumanMessage(content='合成问题')],
                 'retrieval_mode': 'none', 'task_mode': 'chat', 'request_intent': {'request_kind': 'question'}}
        try:
            result = await case.agent._schema2_early_sources['direct_response'](state)
            await owner.drain(timeout=1)
            assert result['next_step'] == 'finish'
            assert len(result['messages']) == 1 and result['messages'][0].content == reply
            assert len(provider.requests) == 1
            prompt = str(provider.requests[0][0].content)
            assert 'non-executable response node' in prompt
            assert 'biomind_generate_display_v1' not in prompt
            assert len(finalized) == 1
            assert sum(message.type == 'ai' for message in finalized[0][0][2]) == 1
            public = [item for item in owner.model.items if item.source == 'model_public']
            assert [item.text for item in public] == [reply]
            assert writer.getvalue().count(reply) == (1 if mode == 'plain' else 0)
            if mode == 'off':
                assert writer.getvalue() == ''
            return {'calls': len(provider.requests), 'route': result['next_step'],
                    'response': result['messages'][0].content, 'finalizations': len(finalized)}
        finally:
            owner.close()
            bind_terminal(previous)
            router.reset_for_task()
            await case.asyncTearDown()
    async def run():
        plain, off = await scenario('plain'), await scenario('off')
        assert plain == off
    asyncio.run(run())


def test_actual_supervisor_review_keeps_fact_status_and_one_existing_call(tmp_path):
    async def scenario(mode):
        from supervisor import SupervisorNode
        from tests.test_inspection_workflow import make_inspection_fixture
        fixture = make_inspection_fixture(tmp_path / mode)
        writer = TimedWriter()
        owner = PlainTerminal(writer, enabled=mode != 'off')
        previous = bind_terminal(owner)
        try:
            node = SupervisorNode(fixture.reviewer, output_dir=str(fixture.root / 'task_outputs'))
            result = await node.run(deepcopy(fixture.state))
            await owner.drain(timeout=1)
            evidence = result['validation_evidence']
            assert len(fixture.reviewer.requests) == 1
            assert evidence['status'] == 'VALID'
            assert evidence['llm_advisory_conflict'] is True
            assert not any(item.source == 'model_public' for item in owner.model.items)
            assert '<validation_evidence>' not in writer.getvalue()
            if mode == 'plain':
                assert '局部执行检查：VALID' in writer.getvalue()
                assert '正式完成核验与提交另行判定' in writer.getvalue()
            else:
                assert writer.getvalue() == ''
            assert all('biomind_generate_display_v1' not in str(message.content)
                       for message in fixture.reviewer.requests[0])
            summary = {'calls': len(fixture.reviewer.requests), 'status': evidence['status'],
                       'advisory_conflict': evidence['llm_advisory_conflict'],
                       'message_types': [message.type for message in result['messages']]}
            artifact('cli3-supervisor-' + mode + '.json', {**summary, 'transcript': writer.getvalue()})
            return summary
        finally:
            owner.close()
            bind_terminal(previous)
    async def run():
        assert await scenario('plain') == await scenario('off')
    asyncio.run(run())


class ExistingSummaryProvider:
    def __init__(self):
        self.requests = []

    async def ainvoke(self, messages, config=None):
        self.requests.append(deepcopy(list(messages)))
        assert len(messages) == 2
        facts = json.loads(messages[1].content)
        assert facts['stop_reason']['error_code'] == 'SYNTHETIC_STOP'
        assert 'public_message' not in str(messages[0].content)
        return AIMessage(id='synthetic-existing-summary', content=json.dumps({
            'fact_ids': ['origin', 'stop_reason'],
            'explanation': '旧总结协议的模型自由文字，仅归档，不升级为正式事实。',
        }, ensure_ascii=False))


def test_actual_final_summary_protocol_and_terminal_result_remain_one_shot():
    async def scenario(mode):
        from bio_agents import InteractiveRunResult, OutputRouter
        from agent_core.workflow.managed_stop import finalize_managed_stop, projection
        from tests.test_inspection_workflow import make_inspection_fixture
        case = new_case()
        await case.asyncSetUp()
        fixture = make_inspection_fixture(case.root, task_id='cli-existing-summary', create_attempt=False)
        provider = ExistingSummaryProvider()
        case.agent.terminal_summary_llm = provider
        case.agent.terminal_summary_policy = {'enabled': True, 'timeout_seconds': 1}
        writer = TimedWriter()
        owner = PlainTerminal(writer, enabled=mode != 'off')
        previous = bind_terminal(owner)
        try:
            boundary = {'node': 'generate', 'status': 'failed', 'outcome': {
                'type': 'blocked', 'summary': 'Synthetic candidate was not executed.',
                'scope': {'task_id': fixture.task_id},
                'payload': {'error_code': 'SYNTHETIC_STOP', 'execution_status': 'not_started'}}}
            stop = await finalize_managed_stop(case.agent, fixture.task_id, fixture.state, boundary)
            again = await finalize_managed_stop(case.agent, fixture.task_id, fixture.state, boundary)
            assert stop == again
            assert stop['summary_status'] == 'COMPLETED'
            assert stop['summary_llm_calls'] == 1 and len(provider.requests) == 1
            assert stop['summary_consumption_status'] == 'GROUNDED_FACT_SELECTION'
            projected = projection(stop)
            diagnostic = projected['terminal_diagnostic']
            result = InteractiveRunResult(task_id=fixture.task_id, status=projected['status'],
                stage=diagnostic['stage'], failure_code=diagnostic['error_code'], reason=diagnostic['reason'])
            router = OutputRouter()
            assert router.render_terminal_result(result)
            assert not router.render_terminal_result(result)
            await owner.drain(timeout=1)
            assert all(item.source != 'model_public' for item in owner.model.items)
            if mode == 'plain':
                text = writer.getvalue()
                assert text.count('Terminal result') == 1
                assert 'SYNTHETIC_STOP' in text
                assert 'Status: ' + result.status in text
                assert result.reason and 'Reason: ' + result.reason in text
                assert '旧总结协议的模型自由文字' not in text
            else:
                assert writer.getvalue() == ''
            summary = {'existing_summary_calls': len(provider.requests), 'extra_narration_calls': 0,
                'summary_status': stop['summary_status'], 'status': result.status,
                'failure_code': result.failure_code, 'selected_fact_ids': stop['summary_selected_fact_ids']}
            artifact('cli3-final-summary-' + mode + '.json', {**summary, 'transcript': writer.getvalue()})
            return summary
        finally:
            owner.close()
            bind_terminal(previous)
            await case.asyncTearDown()
    async def run():
        assert await scenario('plain') == await scenario('off')
    asyncio.run(run())
