"""Actual Generate approval, tiny process and CommitStep display invariants."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest
from langchain_core.messages import AIMessage
from tests.cli_model_progress_support import TimedWriter
from tests.test_cli_model_progress_integration import artifact

async def node_scenario(mode, shared_fixture, *, generate_envelope, execute_to_commit=True, terminal_owner=None, first_kind='invalid_wrapper'):
    import hashlib
    import sys
    from unittest.mock import patch
    import bio_agents
    import execution.script_validation as script_validation
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.plan_tracker import load_plan_state, semantic_plan_hash
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol

    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp()
    configure_protocol(case)
    case._configure_one_step_no_output_plan()
    task_id = 'cli-progress-fixed-task'
    original_plan = case._planner_payload
    def plan():
        value = original_plan()
        value['steps'][0]['produces_observations'] = [{'identity': 'validation_observation',
            'evidence_contract': {'required_fields': ['value']}}]
        value['steps'][0]['success_criteria'] = ['Record the actual local input existence check as a structured observation.']
        next_step = deepcopy(value['steps'][0])
        next_step.update(step_id='step-next', dependencies=['step-direct'], title='Next stage must not run')
        value['steps'].append(next_step)
        return value
    case._planner_payload = plan
    public_text = {
        'consultant': ['现有合成输入已定位，我先读取文件属性。', '输入属性已返回，我会提交现有规划交接。'],
        'planner': ['我先查看已选工具的文档目录，再完成这一步的计划。', '文档目录已返回，计划只要求记录这次输入检查。'],
        'generate': ['我准备生成这一步的候选脚本，仍需原语法检查和批准。', '上个候选包装被拒绝，我会按原协议重新给出脚本。'],
    }
    if first_kind == 'skill':
        public_text['generate'][0] = '候选脚本生成前，我先确认所选工具文档的标题。'
    counts = {'consultant': 0, 'planner': 0, 'generate': 0}
    requests, responses, dispatches, events, syntax_inputs, approvals, executions = [], [], [], [], [], [], []
    syntax_receipts = []
    boundaries = []
    writer = TimedWriter()
    owner = terminal_owner if terminal_owner is not None else PlainTerminal(writer, enabled=mode != 'off')
    previous_owner = bind_terminal(owner)
    if mode == 'tty':
        from tests.test_cli_model_display import complete_fake_review
        original_prompt = case.agent._prompt_user
        case.agent._prompt_user = lambda *args, **kwargs: complete_fake_review(owner, original_prompt, *args, **kwargs)
    real_inspect = bio_agents.inspect_resource
    def inspect_spy(*args, **kwargs):
        dispatches.append({'tool': 'inspect_resource', 'at': time.monotonic()})
        return real_inspect(*args, **kwargs)
    real_context = case.agent.skill_access_service.serve_context_request
    def skill_spy(*args, **kwargs):
        dispatches.append({'tool': 'skill_context', 'at': time.monotonic()})
        return real_context(*args, **kwargs)
    case.agent.skill_access_service.serve_context_request = skill_spy
    real_syntax = script_validation.check_script_body
    def syntax_spy(code, language, **kwargs):
        boundaries.append({'boundary': 'syntax', 'at': time.monotonic(), 'generate_ordinal': counts['generate']})
        syntax_inputs.append(code)
        receipt = real_syntax(code, language, **kwargs)
        syntax_receipts.append(deepcopy(receipt))
        return receipt
    def review(code, _language, **_kwargs):
        boundaries.append({'boundary': 'approval', 'at': time.monotonic()})
        approvals.append(code)
        return 'approve', code
    case.agent.middleware.review_code = review

    async def provider(messages, *, stage, node_context=None, **_kwargs):
        assert stage in counts, 'No narration or unexpected model stage: ' + stage
        counts[stage] += 1
        ordinal = counts[stage]
        requests.append({'stage': stage, 'ordinal': ordinal, 'messages': [
            {'type': message.type, 'content': deepcopy(message.content)} for message in messages]})
        if stage == 'consultant':
            assert ordinal <= 2
            payload = ({'action_type': 'inspect_resource', 'resource': str(shared_fixture), 'role': 'input'}
                if ordinal == 1 else case._consultant_payload(case.root / 'unused-report.md'))
            payload['public_message'] = public_text[stage][ordinal - 1]
            wire = json.dumps(payload, ensure_ascii=False)
        elif stage == 'planner':
            assert ordinal <= 2
            payload = ({'decision': 'REQUEST_SKILL_CONTEXT', 'reason': 'Read the selected Skill headings once.',
                'requests': [{'skill_id': case.SKILL_ID, 'operation': 'headings'}]}
                if ordinal == 1 else plan())
            payload['public_message'] = public_text[stage][ordinal - 1]
            wire = json.dumps(payload, ensure_ascii=False)
        else:
            assert ordinal <= (2 if execute_to_commit else 1)
            if ordinal == 2:
                assert not approvals and not executions
            if execute_to_commit and ordinal == 1:
                payload = ('<shell>true</shell>' if first_kind == 'known_length' else
                           '<shell><script language="bash">true</script></shell>')
                if first_kind == 'wrapped_skill':
                    payload = json.dumps({'decision': 'REQUEST_SKILL_CONTEXT',
                        'reason': 'This native decision cannot become an envelope script.',
                        'requests': [{'skill_id': case.SKILL_ID, 'operation': 'headings'}]})
                if first_kind == 'syntax_error':
                    payload = '<shell>if then\nfi</shell>'
            elif execute_to_commit:
                template = json.loads(next(message.content for message in messages
                    if getattr(message, 'name', '') == 'approved_effect_emission_template'))
                body = ("/usr/bin/python3 <<'PY'\n" + template['python_template'] +
                    '\nfrom pathlib import Path\n' + f'exists=Path({str(shared_fixture)!r}).is_file()\n' +
                    "print(emit_effect_evidence({'produces_observations':{'validation_observation':{'value':{'exists':exists}}}}))\nPY")
                payload = '<shell>' + body + '</shell>'
            else:
                payload = '<shell>printf "synthetic candidate\\n"</shell>'
            wire = (json.dumps({'response_type': 'biomind_generate_display_v1',
                'public_message': public_text[stage][ordinal - 1], 'payload': payload}, ensure_ascii=False)
                if generate_envelope else payload)
            if ordinal == 1 and first_kind == 'invalid_transport':
                # Both tags would be tempting regex fallbacks in the old path.
                wire = json.dumps({'response_type': 'biomind_generate_display_v1',
                    'public_message': public_text[stage][0], 'payload': '<shell>true</shell>',
                    'alternate_payload': '<execute>true</execute>'}, ensure_ascii=False)
            if ordinal == 1 and first_kind == 'skill':
                business = {'decision': 'REQUEST_SKILL_CONTEXT', 'reason': 'Inspect selected headings before candidate creation.',
                    'requests': [{'skill_id': case.SKILL_ID, 'operation': 'headings'}]}
                if generate_envelope:
                    business['public_message'] = public_text['generate'][0]
                wire = json.dumps(business, ensure_ascii=False)
        responses.append({'stage': stage, 'ordinal': ordinal, 'wire': wire})
        metadata = {'finish_reason': 'length'} if stage == 'generate' and ordinal == 1 and first_kind == 'known_length' else {}
        return AIMessage(id=f'cli-{stage}-{ordinal}', content=wire, response_metadata=metadata)
    case.agent._ainvoke_node_llm = provider
    case.agent._ainvoke_llm = provider

    async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
        from execution.executor import run_script_path
        boundaries.append({'boundary': 'process', 'at': time.monotonic()})
        raw = Path(path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected_sha256
        allowed = sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
        allowed.add(str(path))
        try:
            stdout, stderr, rc = await run_script_path(path, expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}, managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            allowed.discard(str(path))
        executions.append({'sha256': expected_sha256, 'stdout': stdout, 'stderr': stderr, 'rc': rc})
        return stdout, stderr, rc
    try:
        with patch.object(bio_agents, 'inspect_resource', inspect_spy), patch.object(bio_agents, 'run_script_path', execute), patch.object(script_validation, 'check_script_body', syntax_spy):
            runner = case._runner_state(task_id)
            app = case.agent._app_for_task(task_id, runner)
            stream = app.astream(runner, config={'configurable': {'thread_id': task_id}, 'recursion_limit': 30}, stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    if execute_to_commit and 'commit_step' in event:
                        break
                    if not execute_to_commit and event.get('generate', {}).get('generate_outcome', {}).get('type') == 'action_ready':
                        break
            finally:
                await stream.aclose()
        if mode != 'tty':
            await owner.drain(timeout=1)
        assert counts == {'consultant': 2, 'planner': 2, 'generate': 2 if execute_to_commit else 1}, str(events)
        assert [entry['tool'] for entry in dispatches] == ['inspect_resource', 'skill_context'] + (['skill_context'] if first_kind == 'skill' else [])
        public = [ticket for ticket in owner.model.items if ticket.kind == 'model_public']
        expected_public = [*public_text['consultant'], *public_text['planner']]
        if generate_envelope:
            expected_public += public_text['generate'][1:] if first_kind in {'known_length', 'invalid_transport'} else public_text['generate']
        assert [ticket.text for ticket in public] == expected_public
        if mode in {'plain', 'tty'}:
            for text, dispatch in zip([public_text['consultant'][0], public_text['planner'][0]], dispatches):
                ticket = next(ticket for ticket in public if ticket.text == text)
                assert ticket.times['flush'] <= dispatch['at']
            if generate_envelope:
                last_public = next(ticket for ticket in public if ticket.text == public_text['generate'][1])
                assert last_public.times['flush'] <= min(item['at'] for item in boundaries
                    if item.get('generate_ordinal', 2) == 2)
                if first_kind == 'syntax_error':
                    first_public = next(ticket for ticket in public if ticket.text == public_text['generate'][0])
                    assert first_public.times['flush'] <= boundaries[0]['at']
                if first_kind == 'skill':
                    skill_public = next(ticket for ticket in public if ticket.text == public_text['generate'][0])
                    assert skill_public.times['flush'] <= dispatches[2]['at']
            if mode == 'plain':
                for text in expected_public:
                    assert writer.getvalue().count(text) == 1
                assert '"public_message"' not in writer.getvalue()
        elif mode == 'off':
            assert writer.getvalue() == ''
        for request in requests:
            assert not any(row['type'] == 'ai' and row['content'] in expected_public for row in request['messages'])
        first_generate = next(row['wire'] for row in responses if row['stage'] == 'generate')
        second_generate = next(row['messages'] for row in requests if row['stage'] == 'generate' and row['ordinal'] == 2)
        assert sum(row['type'] == 'ai' and row['content'] == first_generate for row in second_generate) == 1
        if first_kind == 'skill':
            assert sum('SKILL_CONTEXT_RESULT' in str(row['content']) for row in second_generate) == 1
        validator_feedback = []
        for row in second_generate:
            try:
                value = json.loads(row['content'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get('kind') == 'generate_validator_feedback':
                validator_feedback.append(value['feedback']['action_repair'])
        assert len(validator_feedback) == (0 if first_kind == 'skill' else 1)
        if validator_feedback:
            feedback = validator_feedback[0]
            assert len(feedback['exact_violations']) == 1
            violation = feedback['exact_violations'][0]
            if first_kind == 'syntax_error':
                assert feedback['repair_stage'] == 'script_body_validation'
                assert violation['code'] == 'GENERATE_SCRIPT_SYNTAX_INVALID'
                assert feedback['validation_source'] == 'execution.script_validation.check_script_body'
                # The existing script-artifact feedback projection changes the
                # provider field_path and drops syntax_check. Preserve that
                # observed record; use the actual checker receipt as evidence.
                assert syntax_receipts[0]['execution_started'] is False
                assert syntax_receipts[0]['status'] == 'invalid'
                assert syntax_receipts[0]['checker_returncode'] != 0
            else:
                assert feedback['exact_error']['code'] == 'GENERATE_SCRIPT_BLOCK_INVALID'
                assert feedback['exact_error']['stage'] == feedback['repair_stage'] == 'script_block'
                assert violation['field_path'] == 'response'
                assert violation['failure_stage'] == 'pre_execution_candidate_admission'
        durable = load_plan_state(str(case.root), task_id)
        assert 'public_message' not in json.dumps(durable.get('plan_spec') or {})
        attempts = list((durable.get('provenance_state') or {}).get('attempts', {}).values())
        approved_bytes = []
        raw_receipts = []
        if execute_to_commit:
            assert len(approvals) == len(executions) == len(attempts) == 1
            assert len(durable['task_state']['typed_step_commit_receipts']) == 1
            assert executions[0]['rc'] == 0
            assert not any('recovery' in event for event in events)
            for attempt in attempts:
                approved = Path(attempt['approved_script']['path']).read_bytes()
                generated = Path(attempt['generated_script']['path']).read_bytes()
                assert approved == generated == approvals[0].encode()
                assert approved.decode() in syntax_inputs
                assert hashlib.sha256(approved).hexdigest() == attempt['approved_script']['sha256']
                raw = Path(attempt['stdout_path']).read_bytes()
                receipt = attempt['effect_evidence']['parser_receipt']
                assert hashlib.sha256(raw).hexdigest() == receipt['raw_stdout_sha256']
                assert raw.decode() == executions[0]['stdout']
                approved_bytes.append(approved.decode())
                raw_receipts.append({'sha256': receipt['raw_stdout_sha256'], 'emission_count': receipt['emission_count']})
        else:
            assert not approvals and not executions and not attempts
        result = {'mode': mode, 'first_kind': first_kind, 'generate_envelope': generate_envelope, 'calls': counts,
            'tools': [entry['tool'] for entry in dispatches], 'graph_nodes': [list(event) for event in events],
            'approvals': len(approvals), 'executions': len(executions),
            'commits': len(durable['task_state'].get('typed_step_commit_receipts', {})),
            'plan_semantic_hash': semantic_plan_hash(durable), 'approved_bytes': approved_bytes,
            'raw_receipts': raw_receipts, 'repair_event_count': len(durable['task_state'].get('candidate_repair_state', {}).get('events', [])),
            'dispatches': dispatches, 'boundaries': boundaries, 'validator_feedback': validator_feedback,
            'syntax_receipts': syntax_receipts,
            'public_tickets': [{'text': ticket.text, 'scope': ticket.scope, 'times': ticket.times} for ticket in public],
            'transcript': writer.getvalue(), 'provider_requests': requests, 'provider_responses': responses}
        artifact(f"cli{'3' if execute_to_commit else '2'}-nodes-{mode}-envelope-{generate_envelope}-{first_kind}.json", result)
        return {key: result[key] for key in ('calls', 'tools', 'graph_nodes', 'approvals', 'executions', 'commits',
            'plan_semantic_hash', 'approved_bytes', 'raw_receipts', 'repair_event_count', 'validator_feedback')}
    finally:
        bind_terminal(previous_owner)
        if mode != 'tty':
            owner.close()
        await case.asyncTearDown()



import asyncio
import concurrent.futures
import io
import threading
import time

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import Terminal


class RecordedTerminalOutput(DummyOutput):
    def __init__(self):
        self.text = io.StringIO()
        self.events = []

    def write(self, data):
        self.text.write(data)
        self.events.append({'event': 'write', 'at': time.monotonic(), 'text': data})

    def flush(self):
        self.events.append({'event': 'flush', 'at': time.monotonic()})


def service_restricted_loop(loop):
    # Test-only callback service, including asyncio's executor shutdown; the
    # restricted shell can suppress cross-thread self-pipe wakeups. This never
    # completes a ticket, claims visibility, or delays for user reading.
    def pulse():
        loop.call_later(.001, pulse)
    pulse()


async def run_actual_tty(scenario, *, completion_budget=2):
    """Run ``await scenario(owner)`` with the production owner/thread topology.

    Returns (scenario_result, owner, recorded_output). The scenario retains its
    existing fake provider/tool/approval setup. It must not block for human
    input: use the already-authorized synthetic approval boundary.
    """
    output = RecordedTerminalOutput()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        owner = Terminal()
        previous = bind_terminal(owner)
        loop = asyncio.get_running_loop()
        service_restricted_loop(loop)
        completion = concurrent.futures.Future()

        def worker():
            async def invoke():
                service_restricted_loop(asyncio.get_running_loop())
                return await scenario(owner)
            try:
                result = asyncio.run(invoke())
            except BaseException as exc:
                completion.set_exception(exc)
            else:
                completion.set_result(result)

        thread = threading.Thread(target=worker, name='synthetic-cli-runtime', daemon=True)
        result = None
        failure = None

        async def finish():
            nonlocal result, failure
            try:
                result = await asyncio.wait_for(asyncio.shield(
                    asyncio.wrap_future(completion)), completion_budget)
                # Drain already queued display copies before closing the app.
                # Tools never wait here; the synthetic business run is done.
                owner._changed()
                deadline = time.monotonic() + .5
                while owner.history_flush is not None:
                    await asyncio.wait_for(asyncio.shield(owner.history_flush),
                                           max(.001, deadline - time.monotonic()))
            except BaseException as exc:
                failure = exc
            finally:
                if owner.app.is_running:
                    owner.app.exit()

        watcher = None
        def start():
            nonlocal watcher
            thread.start()
            watcher = asyncio.create_task(finish())

        try:
            await owner.app.run_async(pre_run=start, set_exception_handler=False)
            await watcher
            if failure is not None:
                raise failure
            return result, owner, output
        finally:
            owner.closed = True
            bind_terminal(previous)



def test_cli3_legacy_process_fixture_preserves_existing_effect_contract(tmp_path):
    fixture = tmp_path / 'legacy-input.txt'
    fixture.write_text('synthetic input\n')
    result = asyncio.run(node_scenario('off', fixture, generate_envelope=False))
    assert result['commits'] == result['executions'] == result['approvals'] == 1


def test_cli3_actual_generated_envelope_approval_process_commit_equal_old_tty_plain_off(tmp_path):
    fixture = tmp_path / 'input.txt'
    fixture.write_text('synthetic input\n')
    async def run():
        new_plain = await node_scenario('plain', fixture, generate_envelope=True)
        new_off = await node_scenario('off', fixture, generate_envelope=True)
        old_off = await node_scenario('off', fixture, generate_envelope=False)
        new_tty, owner, output = await run_actual_tty(
            lambda terminal: node_scenario('tty', fixture, generate_envelope=True, terminal_owner=terminal),
            completion_budget=60)
        public = [ticket for ticket in owner.model.items if ticket.kind == 'model_public']
        assert len(public) == 6
        assert all('flush' in ticket.times for ticket in public)
        assert 'biomind_generate_display_v1' not in output.text.getvalue()
        assert all(output.text.getvalue().count(ticket.text) == 1 for ticket in public)
        artifact('cli3-actual-tty-output.json', {'transcript': output.text.getvalue(),
            'writer_events': output.events,
            'tickets': [{'kind': t.kind, 'scope': t.scope, 'text': t.text, 'times': t.times} for t in owner.model.items]})
        assert new_plain == new_off == old_off == new_tty
    asyncio.run(run())


def test_cli3_generate_skill_early_return_uses_real_context_tool(tmp_path):
    fixture = tmp_path / 'skill-input.txt'
    fixture.write_text('synthetic input\n')
    result = asyncio.run(node_scenario('plain', fixture, generate_envelope=True, first_kind='skill'))
    assert result['calls']['generate'] == 2
    assert result['tools'] == ['inspect_resource', 'skill_context', 'skill_context']


def test_cli3_complete_json_known_length_keeps_original_candidate_repair(tmp_path):
    fixture = tmp_path / 'length-input.txt'
    fixture.write_text('synthetic input\n')
    result = asyncio.run(node_scenario('plain', fixture, generate_envelope=True, first_kind='known_length'))
    assert result['repair_event_count'] == 1
    assert result['calls']['generate'] == 2


@pytest.mark.parametrize('first_kind', ['invalid_transport', 'wrapped_skill'])
def test_cli3_invalid_transport_cannot_execute_embedded_fallback_tags(tmp_path, first_kind):
    fixture = tmp_path / 'conflict-input.txt'
    fixture.write_text('synthetic input\n')
    result = asyncio.run(node_scenario('plain', fixture, generate_envelope=True, first_kind=first_kind))
    assert result['repair_event_count'] == 1
    assert result['calls']['generate'] == 2


def test_cli3_actual_bash_syntax_failure_repairs_before_approval_or_execution(tmp_path):
    fixture = tmp_path / 'syntax-input.txt'
    fixture.write_text('synthetic input\n')
    async def run():
        result = await node_scenario('plain', fixture, generate_envelope=True, first_kind='syntax_error')
        legacy = await node_scenario('off', fixture, generate_envelope=False, first_kind='syntax_error')
        assert result == legacy
        return result
    result = asyncio.run(run())
    assert result['repair_event_count'] == 1
    assert result['calls']['generate'] == 2
