"""Offline production transitions from executable steps to human interaction.

Providers and the subprocess leaf are stand-ins. Plan compilation, approval,
Generate, typed routing, interruption, user-turn persistence and Commit are real.
"""
import asyncio
from copy import deepcopy
import hashlib
import gzip
import json
import os
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
import pytest


async def question_graph(*, restart=False, repair_carrier=False, impact_case=''):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

    service_writer_callbacks(asyncio.get_running_loop())
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    from agent_core.workflow.execution import TypedGenerate
    raw_results = []
    typed_run = TypedGenerate.run

    async def observed_run(adapter, state):
        source = adapter.generator
        async def observed_source(value):
            result = await source(value)
            raw_results.append(deepcopy(result))
            return result
        adapter.generator = observed_source
        try:
            return await typed_run(adapter, state)
        finally:
            adapter.generator = source

    with patch.object(TypedGenerate, 'run', observed_run):
        await case.asyncSetUp()
    task = 'interaction-repair' if repair_carrier else 'interaction-restart' if restart else 'interaction-answer'
    if impact_case:
        task += '-' + impact_case
    goal = '离线框架夹具：三次有限观察后询问参数，再继续。不得运行真实输入或科学命令。'
    persist = case.agent._persist_user_submission
    case.agent._persist_user_submission = lambda tid, _: persist(tid, goal)
    requests, executions, events, approvals, states = [], [], [], [], []
    counts = {'consultant': 0, 'planner': 0, 'generate': 0, 'planner_plan_impact_review': 0}
    step_calls = {}
    question = '请确认 S1、S2 的 ploidy；答复前不执行依赖该参数的步骤。'

    def steps():
        rows = []
        for step_id in ['inspect_inputs', 'inspect_runtime', 'inspect_output_dir', 'confirm_ploidy', 'after_answer']:
            ask = step_id == 'confirm_ploidy'
            row = {
                'step_id': step_id, 'title': question if ask else 'synthetic ' + step_id,
                'action': 'ask_user' if ask else 'execute', 'purpose': 'user_input' if ask else 'validation',
                'tool': 'human' if ask else 'bash', 'skill_ids': [],
                'inputs': [], 'outputs': [], 'dependencies': [rows[-1]['step_id']] if rows else [],
                'workflow_stage_ids': [], 'batch_id': '', 'risk_level': 'low',
                'success_criteria': ['收到并记录用户答复。' if ask else '仅保存合成有限观察。'],
            }
            if not ask:
                row['execution_capability_id'] = 'framework.generic.execute'
                row['produces_observations'] = [{'identity': step_id + '_observation', 'evidence_contract': {'required_fields': ['value']}}]
            rows.append(row)
        return rows

    async def provider(messages, *, stage, node_context=None, **kwargs):
        assert stage in counts, stage
        counts[stage] += 1
        requests.append({'stage': stage, 'messages': [{'role': m.type, 'content': m.content} for m in messages]})
        if stage == 'consultant':
            if counts[stage] > 1:
                assert '2倍体' in json.dumps(requests[-1], ensure_ascii=False)
            return AIMessage(content=json.dumps({
                'action_type': 'submit_planning_handoff', 'summary': '离线夹具；只记录已提交事实。',
                'selected_skills': [case.SKILL_ID],
                'task_spec_operations': [{'operation': 'set_goal', 'value': goal, 'supporting_evidence': ['U1']}] if counts[stage] == 1 else [
                    {'operation': 'confirm_decision', 'decision': {'semantic_key': 'analysis.ploidy', 'value': 2}, 'supporting_evidence': ['U2']}],
                'unresolved_ambiguities': [], 'planner_feedback': None,
            }))
        if stage == 'planner_plan_impact_review':
            durable = load_plan_state(case.root, task)
            pending = durable['task_state']['pending_plan_impact_review']
            assert [s['status'] for s in durable['steps'][:4]] == ['passed'] * 4
            replacement = steps()[-1]
            if impact_case == 'invalid_then_valid':
                if counts[stage] == 1:
                    replacement['success_criteria'] = ['unapproved contract change']
                else:
                    assert 'PLAN_IMPACT_PATCH_CONTRACT_CHANGED' in json.dumps(requests[-1])
            return AIMessage(content=json.dumps({
                'decision': 'PATCH_CURRENT_STEP', 'base_task_revision': pending['base_task_revision'],
                'new_task_revision': durable['current_task']['task_revision'],
                'base_plan_revision': durable['plan_revision'],
                'replacement_step': replacement, 'reason': '只更新待执行步骤，保留已完成的有限观察和用户答复。',
            }))
        if stage == 'planner':
            return AIMessage(content=json.dumps({
                'outcome_type': 'SUBMIT_PLAN_FOR_REVIEW', 'steps': steps(), 'proposed_artifacts': [],
                'validation_checklist': [{'check_id': 'synthetic', 'description': '只测试交互和状态。'}],
                'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'},
            }))
        durable = load_plan_state(case.root, task)
        step_id = durable['current_step_id']
        step_calls[step_id] = step_calls.get(step_id, 0) + 1
        if step_id == 'confirm_ploidy':
            return AIMessage(content=json.dumps({'type': 'ask_user', 'question': question}))
        if step_id == 'after_answer':
            assert '2倍体' in json.dumps(requests[-1], ensure_ascii=False)
        body = "# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({'produces_observations': {'" + step_id + "_observation': {'value': True}}}))\n"
        if repair_carrier and step_id == 'inspect_runtime':
            wire = json.dumps(requests[-1], ensure_ascii=False)
            if step_calls[step_id] == 1:
                return AIMessage(content='<shell>PY=/synthetic/bin/python\n"$PY" - <<\'PY\'\n' + body + 'PY\n</shell>')
            if step_calls[step_id] == 2:
                assert 'EFFECT_HELPER_PYTHON_EXECUTABLE_NOT_LITERAL' in wire
                assert 'single-quoted heredoc is present' in wire
                assert 'Keep the approved Bash carrier' in wire
                # Replay the incident's wrong repair to verify that the
                # unchanged approval boundary rejects it with useful feedback.
                return AIMessage(content='<python>' + body + '</python>')
            assert step_calls[step_id] == 3
            assert 'GENERIC_EXECUTION_TOOL_SCOPE_MISMATCH' in wire
            assert 'Changing the approved tool requires a Plan revision' in wire
        return AIMessage(content='<shell>\npython - <<\'PY\'\n' + body + 'PY\n</shell>')

    def approve(code, language, **kwargs):
        approvals.append({'code': code, 'language': language})
        return 'approve', code

    async def execute(path, *, expected_sha256, **kwargs):
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
        state = load_plan_state(case.root, task)
        step_id = state['current_step_id']
        assert step_id != 'confirm_ploidy'
        executions.append(step_id)
        stdout = 'BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'produces_observations': [
            {'identity': step_id + '_observation', 'value': True, 'scope': 'synthetic fixture only'},
        ]}) + '\n'
        for key, value in [('stdout', stdout), ('stderr', '')]:
            logs = kwargs.get('managed_log_paths') or {}
            Path(logs.get(key) or logs.get(key + '_path') or Path(path).parent / (key + '.log')).write_text(value)
        return stdout, '', 0

    try:
        case.agent._ainvoke_node_llm = provider
        case.agent.middleware.review_code = approve
        runner = case._runner_state(task)
        runner['messages'][0].content = goal
        app = case.agent._app_for_task(task, runner)
        config = {'configurable': {'thread_id': task}, 'recursion_limit': 50}
        with patch.object(bio_agents, 'run_script_path', execute):
            async for event in app.astream(runner, config=config, stream_mode='updates'):
                events.append(event)
            paused = app.get_state(config)
            durable = load_plan_state(case.root, task)
            assert paused.tasks and paused.tasks[0].interrupts, events
            assert paused.next == ('ask_user',), paused.next
            assert executions == ['inspect_inputs', 'inspect_runtime', 'inspect_output_dir']
            assert [s['status'] for s in durable['steps'][:3]] == ['passed'] * 3
            assert durable['steps'][3]['status'] not in {'passed', 'done', 'completed'}
            assert not durable['steps'][3]['execution_capability_id']
            before_answers = deepcopy(durable['conversation_evidence'])
            before_requests = len(requests)
            if restart:
                app = bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer)
                async for event in app.astream(None, config=config, stream_mode='updates'):
                    events.append(event)
                assert len(requests) == before_requests
                assert load_plan_state(case.root, task)['conversation_evidence'] == before_answers
            # A draft never enters this API. Only Command(resume=...) submits
            # the answer to the production ask_user interrupt.
            stream = app.astream(bio_agents.Command(resume='2倍体'), config=config, stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    current = load_plan_state(case.root, task)
                    states.append({'node': list(event), 'current_step_id': current.get('current_step_id'),
                                   'steps': current.get('steps'), 'task_state': current.get('task_state')})
                    if impact_case == 'budget' and 'consultant' in event:
                        # Model an exhausted restored epoch for the *current*
                        # TaskSpec. A new user semantic revision legitimately
                        # starts a new epoch under the existing Planner policy.
                        from agent_core.plan_contract import start_planning_epoch
                        from agent_core.task_spec import task_semantic_hash
                        from agent_core.plan_tracker import canonical_task_spec_for_state
                        spec = canonical_task_spec_for_state(current)
                        epoch = start_planning_epoch(task_revision=spec['revision'],
                            task_semantic_identity=task_semantic_hash(spec),
                            base_plan_revision=current['plan_revision'])
                        epoch['planner_calls_total'] = 1
                        current['task_state']['planning_epoch'] = epoch
                        current['task_state'].setdefault('budgets', {})['max_planner_calls_per_epoch'] = 1
                        save_plan_state(case.root, task, current)
                    if 'commit_step' in event and executions[-1:] == ['after_answer']:
                        break
            finally:
                await stream.aclose()
            saved = load_plan_state(case.root, task)
            if impact_case == 'budget':
                assert executions == ['inspect_inputs', 'inspect_runtime', 'inspect_output_dir']
                assert counts['planner_plan_impact_review'] == 0
                assert saved['task_state']['status'] == 'failed'
                assert 'PLANNING_EPOCH_EXHAUSTED' in json.dumps(events, default=str)
                return
            assert executions == ['inspect_inputs', 'inspect_runtime', 'inspect_output_dir', 'after_answer'], events
            assert len(approvals) == 4
            assert counts['generate'] == (7 if repair_carrier else 5)
            assert counts['consultant'] == 2
            assert counts['planner'] == 1
            assert counts['planner_plan_impact_review'] == (2 if impact_case == 'invalid_then_valid' else 1)
            assert saved['task_state']['planning_epoch']['planner_calls_total'] == (2 if impact_case == 'invalid_then_valid' else 1)
            receipt = saved['task_state']['plan_review_submission']
            assert receipt['task_revision'] == 2 and receipt['plan_revision'] == 2
            assert saved['steps'][3]['status'] in {'passed', 'done', 'completed'}
            assert saved['steps'][4]['status'] == 'passed'
            assert saved['conversation_evidence']['user_turn_count'] == 2
            assert any(e.get('ask_user', {}).get('ask_user_outcome', {}).get('type') == 'answer_recorded' for e in events)
    finally:
        Path(os.environ['TMPDIR'], task + '.json').write_text(json.dumps({
            'requests': requests, 'events': events, 'counts': counts, 'executions': executions, 'states': states,
            'raw_generate_results': raw_results,
        }, ensure_ascii=False, default=str, indent=2))
        await case.agent.aclose(timeout_seconds=0.2)
        await case.asyncTearDown()


@pytest.mark.parametrize('restart', [False, True])
def test_three_commits_then_question_answer_and_next_execution(restart):
    asyncio.run(question_graph(restart=restart))


def test_local_carrier_repair_preserves_approved_bash_and_reaches_next_request():
    asyncio.run(question_graph(repair_carrier=True))


@pytest.mark.parametrize('impact_case', ['invalid_then_valid', 'budget'])
def test_plan_impact_keeps_existing_rejection_and_call_limit(impact_case):
    asyncio.run(question_graph(impact_case=impact_case))


@pytest.mark.parametrize('ordinal', range(4))
def test_archived_runtime_carriers_have_precise_materialization_result(ordinal):
    from agent_core.step_effects import declared_effect_emission_contract, materialize_effect_template, EffectHelperError
    from agent_core.controller_audit import parse_executable_script_block
    rows = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/generate_interaction_1789741049.json.gz').read_bytes()))
    response = json.loads(rows[ordinal]['response']['content'])
    block = parse_executable_script_block(response['payload'])
    step = {'step_id': 'inspect_runtime', 'produces_observations': [
        {'identity': 'runtime_availability', 'evidence_contract': {'required_fields': ['value']}},
    ]}
    # The archived carrier is only materialized. Its script is never approved
    # or executed and does not authorize any access to original environments.
    template = declared_effect_emission_contract({'steps': [step]}, 'inspect_runtime')
    if ordinal in {0, 2}:
        with pytest.raises(EffectHelperError, match='EFFECT_HELPER_PYTHON_EXECUTABLE_NOT_LITERAL') as error:
            materialize_effect_template(block.body, template, language=block.language)
        assert 'single-quoted heredoc is present' in error.value.to_dict()['message']
    else:
        result = materialize_effect_template(block.body, template, language=block.language)
        assert '# BIOMIND_EFFECT_HELPER' not in result


async def generate_scope_case(action, capability_id, response, expected_error):
    import bio_agents
    import tests.test_generate_production_context_wiring as fixture
    from agent_core.plan_tracker import load_plan_state
    from tests.test_cli_v2_display import service_writer_callbacks
    service_writer_callbacks(asyncio.get_running_loop())

    case = fixture.ProductionGenerateContextWiringTests(methodName='test_same_basis_keeps_prefix_and_appends_candidate_then_feedback')
    await case.asyncSetUp()
    original = fixture.build_plan_state

    def build(task, plan, **kwargs):
        value = json.loads(plan)
        row = value['steps'][0]
        row.update(action=action, tool='human' if action == 'ask_user' else 'bash',
                   purpose='user_input' if action == 'ask_user' else 'validation',
                   execution_capability_id=capability_id, skill_ids=[])
        return original(task, json.dumps(value), **kwargs)

    calls = []

    async def provider(messages, **kwargs):
        calls.append(messages)
        return AIMessage(content=response)

    try:
        with patch.object(fixture, 'build_plan_state', build):
            state = case._create_task('interaction-scope')
        case.agent._ainvoke_node_llm = provider
        with patch.object(bio_agents, 'resolve_action_runtime_bindings', wraps=bio_agents.resolve_action_runtime_bindings) as resolver:
            result = await case._invoke(state)
        if expected_error:
            assert not calls
            assert expected_error in json.dumps(result, default=str), result
            assert resolver.call_count >= 1
        elif response.startswith('<shell>'):
            # A valid interaction must not grant authority to execute code.
            assert len(calls) == 1
            assert not result.get('pending_action')
            assert not result.get('pending_human_question')
            assert result.get('generate_exit_reason') in {'unrecognized_action', 'script_body_validation_rejected'}
        else:
            assert resolver.call_count == 0
            assert len(calls) == 1
            assert result['pending_human_question']['plan_step_id'] == 'step-generate'
            assert not result['pending_action']
            durable = load_plan_state(case.root, 'interaction-scope')
            assert durable['task_state']['pending_question'] == result['pending_human_question']
            assert not durable['task_state']['pending_action']
    finally:
        await case.asyncTearDown()


@pytest.mark.parametrize('action,capability_id,response,expected_error', [
    ('ask_user', '', '{"type":"ask_user","question":"确认参数？"}', ''),
    ('execute', '', '<shell>printf synthetic</shell>', 'ACTION_CAPABILITY_SCOPE_MISSING'),
    ('ask_user', 'unknown.capability', '{"type":"ask_user","question":"确认参数？"}', 'UNKNOWN_EXECUTION_CAPABILITY'),
    ('execute', 'unknown.capability', '<shell>printf synthetic</shell>', 'UNKNOWN_EXECUTION_CAPABILITY'),
    ('ask_user', '', '<shell>printf synthetic</shell>', ''),
])
def test_actual_generate_preserves_capability_and_action_boundaries(action, capability_id, response, expected_error):
    asyncio.run(generate_scope_case(action, capability_id, response, expected_error))


@pytest.mark.parametrize('failed', [False, True])
def test_impact_uses_planner_provider_and_single_transport_attempt(failed):
    async def scenario():
        from langchain_core.messages import HumanMessage
        from tests.test_generate_production_context_wiring import (
            ProductionGenerateContextWiringTests, _RecordingProvider,
        )
        from tests.test_cli_v2_display import service_writer_callbacks
        service_writer_callbacks(asyncio.get_running_loop())
        case = ProductionGenerateContextWiringTests(methodName='test_same_basis_keeps_prefix_and_appends_candidate_then_feedback')
        await case.asyncSetUp()

        class PlannerProvider(_RecordingProvider):
            async def ainvoke(self, messages, **kwargs):
                if failed:
                    self.calls.append(messages)
                    raise ConnectionError('synthetic offline transport failure')
                return await super().ainvoke(messages, **kwargs)

        provider = PlannerProvider()
        case.agent.planner_llm = provider
        case.agent.planner_llm_sdk_max_retries = 0
        case.agent.llm_max_retries = 9  # Shared-client settings must not apply.
        try:
            case._create_task('impact-provider')
            policy = case.agent._resolve_planner_output_policy()
            call = case.agent._ainvoke_llm_once(
                [HumanMessage(content='Synthetic restricted Plan impact response.')],
                stage='planner_plan_impact_review', task_id='impact-provider',
                trace_metadata={'planner_output_policy': policy},
            )
            if failed:
                with pytest.raises(Exception, match='synthetic offline transport failure'):
                    await call
            else:
                result = await call
                assert result.content == provider.response_text
            assert len(provider.calls) == 1
            assert not case.provider.calls
        finally:
            await case.asyncTearDown()

    asyncio.run(scenario())
