"""Actual Consultant/Planner local rounds with synthetic provider and stores."""
import asyncio
from copy import deepcopy
import json
import time
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tests.cli_model_progress_support import TimedWriter
from tests.test_cli_model_progress_integration import artifact


async def cp_scenario(mode, fixture, *, terminal_owner=None, terminal_writer=None):
    import bio_agents
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.plan_tracker import load_plan_state, semantic_plan_hash
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol

    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp()
    configure_protocol(case)
    case._configure_one_step_no_output_plan()
    # State the synthetic effect here: the committed legacy fixture predates
    # the current Plan effect contract; its unrelated dirty edits are excluded.
    original_plan = case._planner_payload
    def synthetic_plan():
        payload = original_plan()
        payload['steps'][0]['produces_observations'] = ['approved_command_succeeded']
        return payload
    case._planner_payload = synthetic_plan
    task_id = 'cli-cp-fixed-task'
    public_text = {
        'consultant': ['现有合成输入已定位，我先读取文件属性。', '输入属性已返回，我会提交现有规划交接。'],
        'planner': ['我先查看已选工具的文档目录，再完成这一步的计划。', '文档目录已返回，计划只要求记录这次输入检查。'],
    }
    counts = {'consultant': 0, 'planner': 0, 'generate': 0}
    requests, responses, dispatches, events = [], [], [], []
    writer = terminal_writer if terminal_writer is not None else TimedWriter()
    owner = terminal_owner if terminal_owner is not None else PlainTerminal(writer, enabled=mode != 'off')
    previous_owner = bind_terminal(owner)
    real_inspect = bio_agents.inspect_resource
    def inspect_spy(*args, **kwargs):
        dispatches.append({'tool': 'inspect_resource', 'at': time.monotonic()})
        return real_inspect(*args, **kwargs)
    real_context = case.agent.skill_access_service.serve_context_request
    def skill_spy(*args, **kwargs):
        dispatches.append({'tool': 'skill_context', 'at': time.monotonic()})
        return real_context(*args, **kwargs)
    case.agent.skill_access_service.serve_context_request = skill_spy

    async def provider(messages, *, stage, **_kwargs):
        assert stage in counts, 'No narration or unexpected model stage: ' + stage
        counts[stage] += 1
        ordinal = counts[stage]
        requests.append({'stage': stage, 'ordinal': ordinal, 'messages': [
            {'type': message.type, 'content': deepcopy(message.content)} for message in messages]})
        if stage == 'consultant':
            assert ordinal <= 2
            payload = ({'action_type': 'inspect_resource', 'resource': str(fixture), 'role': 'input'}
                if ordinal == 1 else case._consultant_payload(case.root / 'unused-report.md'))
            payload['public_message'] = public_text[stage][ordinal - 1]
            wire = json.dumps(payload, ensure_ascii=False)
        elif stage == 'planner':
            assert ordinal <= 2
            payload = ({'decision': 'REQUEST_SKILL_CONTEXT', 'reason': 'Read the selected Skill headings once.',
                'requests': [{'skill_id': case.SKILL_ID, 'operation': 'headings'}]}
                if ordinal == 1 else case._planner_payload())
            payload['public_message'] = public_text[stage][ordinal - 1]
            wire = json.dumps(payload, ensure_ascii=False)
        else:
            assert ordinal == 1
            wire = '<shell>printf "synthetic candidate\\n"</shell>'
        responses.append({'stage': stage, 'ordinal': ordinal, 'wire': wire})
        return AIMessage(id=f'cli-{stage}-{ordinal}', content=wire)
    case.agent._ainvoke_node_llm = provider
    case.agent._ainvoke_llm = provider
    approvals = []
    def reject_unexpected_approval(*args, **kwargs):
        approvals.append(args)
        raise AssertionError('This fixture stops at Generate action_ready before code approval')
    case.agent.middleware.review_code = reject_unexpected_approval
    try:
        with patch.object(bio_agents, 'inspect_resource', inspect_spy):
            runner = case._runner_state(task_id)
            app = case.agent._app_for_task(task_id, runner)
            stream = app.astream(runner, config={'configurable': {'thread_id': task_id}, 'recursion_limit': 20}, stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    if event.get('generate', {}).get('generate_outcome', {}).get('type') == 'action_ready':
                        break
            finally:
                await stream.aclose()
        if mode == 'tty':
            async def drained():
                while owner.history_cursor < len(owner.model.items) or owner.history_flush is not None:
                    await asyncio.sleep(0)
            await asyncio.wait_for(drained(), timeout=2)
        else:
            await owner.drain(timeout=1)
        assert counts == {'consultant': 2, 'planner': 2, 'generate': 1}, str(events)
        assert [entry['tool'] for entry in dispatches] == ['inspect_resource', 'skill_context']
        public = [ticket for ticket in owner.model.items if ticket.kind == 'model_public']
        expected_public = [*public_text['consultant'], *public_text['planner']]
        assert [ticket.text for ticket in public] == expected_public
        if mode in {'plain', 'tty'}:
            for text, dispatch in zip([public_text['consultant'][0], public_text['planner'][0]], dispatches):
                ticket = next(ticket for ticket in public if ticket.text == text)
                assert ticket.times['flush'] <= dispatch['at']
            for text in expected_public:
                assert writer.getvalue().count(text) == 1
            assert '"public_message"' not in writer.getvalue()
        else:
            assert writer.getvalue() == ''
        for request in requests:
            assert not any(row['type'] == 'ai' and row['content'] in expected_public for row in request['messages'])
        for stage, marker in [('consultant', 'resource_observation'), ('planner', 'SKILL_CONTEXT_RESULT')]:
            original = next(response['wire'] for response in responses
                            if response['stage'] == stage and response['ordinal'] == 1)
            next_messages = next(request['messages'] for request in requests
                                 if request['stage'] == stage and request['ordinal'] == 2)
            assert sum(row['type'] == 'ai' and row['content'] == original for row in next_messages) == 1
            assert sum(marker in str(row['content']) for row in next_messages) == 1
        durable = load_plan_state(str(case.root), task_id)
        assert 'public_message' not in json.dumps(durable.get('plan_spec') or {})
        assert 'public_message' not in json.dumps(durable.get('approval_record') or {})
        attempts = (durable.get('provenance_state') or {}).get('attempts', {})
        assert not approvals and not attempts
        result = {'mode': mode, 'calls': counts, 'tools': [entry['tool'] for entry in dispatches],
            'graph_nodes': [list(event) for event in events], 'approvals': 0, 'executions': 0, 'commits': 0,
            'plan_semantic_hash': semantic_plan_hash(durable), 'dispatches': dispatches,
            'public_tickets': [{'text': ticket.text, 'scope': ticket.scope, 'times': ticket.times} for ticket in public],
            'transcript': writer.getvalue(), 'provider_requests': requests, 'provider_responses': responses}
        artifact(f'cli2-nodes-{mode}.json', result)
        return {key: result[key] for key in ('calls', 'tools', 'graph_nodes', 'approvals', 'executions', 'commits', 'plan_semantic_hash')}
    finally:
        bind_terminal(previous_owner)
        if mode != 'tty':
            owner.close()
        await case.asyncTearDown()


def test_cli2_actual_consultant_planner_local_turns_tty_plain_off(tmp_path):
    fixture = tmp_path / 'input.txt'
    fixture.write_text('synthetic input\n')
    async def run():
        plain = await cp_scenario('plain', fixture)
        off = await cp_scenario('off', fixture)
        from agent_core.cli_terminal import Terminal
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from tests.test_cli_model_display import RecordingOutput, service_cross_thread_callbacks
        output = RecordingOutput()
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            owner = Terminal()
            service_cross_thread_callbacks()
            async def business():
                try:
                    return await cp_scenario('tty', fixture, terminal_owner=owner, terminal_writer=output.writer)
                finally:
                    owner.app.exit()
            tasks = []
            def start():
                tasks.append(asyncio.create_task(business()))
            try:
                await owner.app.run_async(pre_run=start, set_exception_handler=False)
                tty = await tasks[0]
            finally:
                owner.closed = True
        assert plain == off == tty
    asyncio.run(run())
