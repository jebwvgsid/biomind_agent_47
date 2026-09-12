"""Chain A: synthetic managed Plan -> real reader process -> Commit -> final gate."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
import pytest


async def managed_check(root, mode, *, negative=False, reject_missing_delivery=False, purpose='validation',
                        reporting=False, answer_failure=False, capture=None):
    import bio_agents
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.plan_tracker import load_plan_state, save_plan_state, canonical_task_spec_for_state
    from tests.ger_fixture_support import configure_protocol
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_cli_model_progress_integration import artifact

    service_writer_callbacks(asyncio.get_running_loop())
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp()
    configure_protocol(case)
    case._configure_one_step_no_output_plan()
    owner = PlainTerminal(io.StringIO(), enabled=mode != 'off', verbosity='verbose' if mode == 'verbose' else 'compact')
    previous = bind_terminal(owner)
    task_id = 'managed-results-synthetic'
    paths = [root / 'input-a.tsv', root / 'input-b.tsv']
    for index, path in enumerate(paths):
        path.write_text('sample\n' if negative and index else 'sample\tvalue\na\t1\n')
    request = '请实际检查这两份小表是否有 value 列并告诉我结论；不生成报告文件，也不启动后续分析。输入：' + ', '.join(map(str, paths))
    if reject_missing_delivery:
        request = '请检查两份输入并交付 report.md，文件必须保留。输入：' + ', '.join(map(str, paths))
        case.agent._prompt_user = lambda *args, **kwargs: '当前计划遗漏了我明确要求的 report.md，请修正必要结果。'
    counts = {'consultant': 0, 'planner': 0, 'generate': 0}
    requests, executions, approvals, events = [], [], [], []
    plan = case._planner_payload()
    plan['metadata']['result_contract_version'] = 1
    step = plan['steps'][0]
    step.update(title='检查两份输入的列', purpose=purpose, success_criteria=['完整读取两份小表并如实报告各自是否有 value 列。'],
                produces_observations=[{'identity': 'checked_columns', 'evidence_contract': {'required_fields': ['value']}}])
    plan['validation_checklist'] = [{'check_id': 'input-scope', 'description': '两份输入均实际检查，缺列也属于有效检查结论，且不继续分析。'}]
    if reporting:
        plan['steps'].append({**deepcopy(step), 'step_id': 'step-reporting',
            'title': '报告已接纳的逐文件检查结论', 'purpose': 'reporting',
            'dependencies': [step['step_id']], 'success_criteria': ['报告前一步已接纳的事实，不再读取输入或重新计算。'],
            'produces_observations': [{'identity': 'reporting_answer', 'evidence_contract': {'required_fields': ['value']}}]})

    async def provider(messages, *, stage, node_context=None, **kwargs):
        assert stage in counts, 'Unexpected model call: ' + stage
        counts[stage] += 1
        assert counts[stage] <= (2 if reporting and stage == 'generate' else 1)
        requests.append({'stage': stage, 'messages': [{'type': m.type, 'name': getattr(m, 'name', ''), 'content': deepcopy(m.content)} for m in messages]})
        if stage == 'consultant':
            payload = {'action_type': 'submit_planning_handoff', 'summary': request,
                       'selected_skills': [case.SKILL_ID], 'task_spec_operations': [
                           {'operation': 'set_goal', 'value': request, 'supporting_evidence': ['U1']}],
                       'unresolved_ambiguities': [], 'planner_feedback': None}
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        if stage == 'planner':
            assert request in '\n'.join(str(m.content) for m in messages)
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        assert any(getattr(m, 'name', '') == 'approved_effect_emission_template' for m in messages)
        if counts['generate'] == 2:
            actual_payload = node_context.llm_payload.to_provider_dict()
            accepted = actual_payload['semantic_history']['accepted_execution_outcomes']
            observation = accepted[0]['effect_evidence']['produces_observations'][0]
            assert observation['identity'] == 'checked_columns'
            assert observation['value'] == {'input-a.tsv': True, 'input-b.tsv': not negative}
            assert accepted[0]['evidence_source'] == 'accepted_typed_step_commit'
            # Assert the actual transmitted request contains the receipt-derived
            # value; never append an observation to messages in this fixture.
            sent = '\n'.join(str(m.content) for m in messages)
            assert 'accepted_execution_outcomes' in sent and 'checked_columns' in sent
            body = "/usr/bin/python3 <<'PY'\nimport json\n" + f"value={observation['value']!r}\n" + (
                "print('BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'schema_version': 1, 'produces_observations': [{'identity': 'reporting_answer', 'value': value}]}))\nPY")
            return AIMessage(content='<shell>' + body + '</shell>')
        body = ("/usr/bin/python3 <<'PY'\nimport json\nfrom pathlib import Path\n" +
                f'paths={list(map(str, paths))!r}\n' +
                "measured={Path(p).name: 'value' in Path(p).read_text().splitlines()[0].split('\\t') for p in paths}\n" +
                "print('检查结论：' + str(measured))\n" +
                "print('BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'schema_version': 1, 'produces_observations': [{'identity': 'checked_columns', 'value': measured}]}))\nPY")
        return AIMessage(content='<shell>' + body + '</shell>')

    case.agent._ainvoke_node_llm = provider
    case.agent._ainvoke_llm = provider
    case.agent.middleware.review_code = lambda code, language, **kw: (approvals.append(code) or 'approve', code)

    async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
        from execution.executor import run_script_path
        actual = Path(path).read_bytes()
        assert hashlib.sha256(actual).hexdigest() == expected_sha256
        allow = sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
        allow.add(str(path))
        try:
            result = await run_script_path(path, expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}, managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            allow.discard(str(path))
        executions.append({'executed_sha256': expected_sha256, 'stdout': result[0], 'stderr': result[1], 'returncode': result[2]})
        return result
    try:
        durable, _, _ = case.agent._persist_user_submission(task_id, request)
        admission = {'schema_version': 1, 'request_kind': 'execute', 'execution_scope': 'managed',
            'managed_intent_explicit': True, 'task_mode': 'managed', 'retrieval_mode': 'kb',
            'confidence': 1.0, 'admission_status': 'managed_execution_admitted',
            'reason': 'Synthetic managed inspection', 'decision_source': 'test_fixture'}
        durable.setdefault('task_state', {})['intent_admission'] = admission
        save_plan_state(case.root, task_id, durable)
        runner = {'messages': [HumanMessage(content=request)], 'task_id': task_id, 'session_id': task_id,
            'project_id': case.agent.default_project_id, 'conversation_evidence': durable['conversation_evidence'],
            'task_spec': canonical_task_spec_for_state(durable), 'task_revision': 0, 'selected_skills': [],
            'resolved_bindings': {}, 'consultation_state': {}, 'planner_feedback': {},
            'request_intent': admission, 'intent_admission': admission, 'managed_intent_admitted': True}
        from contextlib import nullcontext
        failure_patch = patch('agent_core.workflow.final_audit.no_file_final_response',
                              side_effect=ValueError('SYNTHETIC_ANSWER_CONSTRUCTION_FAILED')) if answer_failure else nullcontext()
        with patch.object(bio_agents, 'run_script_path', execute), failure_patch:
            app = case.agent._app_for_task(task_id, runner)
            config = {'configurable': {'thread_id': task_id}, 'recursion_limit': 35}
            stream = app.astream(runner, config=config, stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    if reject_missing_delivery and 'plan_review' in event:
                        break
            finally:
                await stream.aclose()
        await owner.drain(timeout=1)
        saved = load_plan_state(case.root, task_id)
        artifact('managed-observation-chain-last.json', {'events': events, 'calls': counts,
                 'task_state': saved.get('task_state'), 'provenance_state': saved.get('provenance_state')})
        if reject_missing_delivery:
            assert counts == {'consultant': 1, 'planner': 1, 'generate': 0}, events
            assert not approvals and not executions
            assert not saved['task_state'].get('typed_step_commit_receipts')
            assert not saved['task_state'].get('typed_final_audit_receipts')
            review = next(event['plan_review'] for event in events if 'plan_review' in event)
            assert review['plan_review_outcome']['type'] == 'reconsultation_required', review
            assert 'report.md' in json.dumps(review, ensure_ascii=False, default=str)
            artifact('plan-results-omitted-user-delivery.json', {'source': 'synthetic',
                'original_request': request, 'actual_planner_request_contains_original': True,
                'calls': counts, 'execution_count': 0, 'commit_count': 0,
                'actual_review_outcome': review['plan_review_outcome']})
            return {'calls': counts, 'execution_count': 0, 'commit_count': 0,
                    'review_type': review['plan_review_outcome']['type']}
        expected_steps = 2 if reporting else 1
        assert counts == {'consultant': 1, 'planner': 1, 'generate': expected_steps}, events
        assert len(approvals) == len(executions) == expected_steps
        assert executions[0]['returncode'] == 0
        attempt = next(iter(saved['provenance_state']['attempts'].values()))
        approved = Path(attempt['approved_script']['path']).read_bytes()
        generated = Path(attempt['generated_script']['path']).read_bytes()
        assert approved == generated == approvals[0].encode()
        assert hashlib.sha256(approved).hexdigest() == attempt['approved_script']['sha256']
        assert Path(attempt['stdout_path']).read_text() == executions[0]['stdout']
        assert attempt['effect_evidence']['parser_receipt']['raw_stdout_sha256'] == hashlib.sha256(executions[0]['stdout'].encode()).hexdigest()
        assert len(saved['task_state'].get('typed_step_commit_receipts', {})) == expected_steps, events
        from agent_core.plan_contract import plan_uses_explicit_results
        assert plan_uses_explicit_results(saved)
        final = next(value['final_audit_outcome'] for event in events for value in event.values()
                     if isinstance(value, dict) and 'final_audit_outcome' in value)
        if answer_failure:
            assert final['type'] == 'task_incomplete'
            assert final['payload']['failure_owner'] == 'result_delivery'
            assert not saved['task_state'].get('typed_final_audit_receipts')
            assert str(saved['task_state'].get('status')).lower() != 'completed'
            artifact('plan-results-answer-construction-failure.json', {'source': 'synthetic', 'calls': counts,
                     'final_outcome': final, 'commits': expected_steps, 'final_audits': 0})
            return final
        assert len(saved['task_state'].get('typed_final_audit_receipts', {})) == 1, events
        assert final['type'] == 'task_verified'
        assert final['payload']['completion_gate']['classification'] == 'plan_result_evidence_satisfied'
        assert saved['steps'][0]['outputs'] == []
        assert not saved.get('plan_artifacts')
        assert not any('recovery' in event for event in events)
        assert '检查结论' in executions[0]['stdout']
        response = final['payload']['final_response']
        assert response['source'] == 'accepted_typed_observations'
        assert 'input-a.tsv' in response['text'] and 'input-b.tsv' in response['text']
        assert ('"input-b.tsv": false' if negative else '"input-b.tsv": true') in response['text']
        assert response['source_refs']
        interactive = case.agent._interactive_run_result(task_id, 'completed', app.get_state(config))
        assert interactive.reason == response['text'], interactive
        bio_agents.OutputRouter().render_terminal_result(interactive)
        await owner.drain(timeout=1)
        terminal = owner.output.getvalue()
        if mode != 'off':
            assert response['text'] in terminal
        else:
            assert not terminal
        if capture is not None:
            capture(deepcopy(saved))
        assert sorted(p.name for p in root.iterdir()) == ['input-a.tsv', 'input-b.tsv']
        evidence_suffix = '-reporting' if reporting else '-scope' if capture is not None else ''
        artifact('plan-results-managed-' + mode + '-' + str(negative) + '-' + purpose + evidence_suffix + '.json', {
            'source': 'synthetic managed production graph', 'original_request': request,
            'actual_planner_request_contains_original': True, 'approved_plan': plan,
            'mode': mode, 'calls': counts, 'approvals': len(approvals), 'executions': executions,
            'approved_sha256': attempt['approved_script']['sha256'],
            'parser_receipt': attempt['effect_evidence']['parser_receipt'],
            'commit_count': expected_steps, 'final_audit_count': 1,
            'terminal': terminal, 'actual_provider_requests': requests,
            'final_outcome': final, 'graph_nodes': [list(event) for event in events],
            'scope_limit': 'Fresh isolated scratch roots differ; execution wrappers are hash verified independently. Fixed approved body, raw stdout, routes and calls compared across display modes.'})
        # Instrumented execution wrappers include each fresh scratch root. Each
        # is verified above at the real process boundary; compare the fixed
        # approved response and original output bytes across display modes.
        return {'calls': counts, 'executions': [{k: v for k, v in row.items() if k != 'executed_sha256'} for row in executions],
                'approved_sha256': attempt['approved_script']['sha256'], 'approvals': len(approvals),
                'commits': expected_steps, 'final_audits': 1, 'graph_nodes': [list(event) for event in events],
                'answer': response['text'],
                'output_files': [], 'negative_check': negative}
    finally:
        bind_terminal(previous)
        owner.close()
        await case.asyncTearDown()


@pytest.mark.parametrize('negative', [False, True])
def test_managed_no_file_check_to_commit_and_formal_task_end_across_display_modes(tmp_path, negative):
    async def run():
        outcomes = {}
        for mode in ('compact', 'verbose', 'off'):
            outcomes[mode] = await managed_check(tmp_path, mode, negative=negative)
        assert outcomes['compact'] == outcomes['verbose'] == outcomes['off']
    asyncio.run(run())


def test_omitted_user_delivery_is_visible_and_existing_review_prevents_false_completion(tmp_path):
    async def run():
        result = await managed_check(tmp_path, 'off', reject_missing_delivery=True)
        assert result['execution_count'] == result['commit_count'] == 0
    asyncio.run(run())


def test_formal_probe_step_keeps_original_approval_and_commits_its_observation(tmp_path):
    async def run():
        result = await managed_check(tmp_path, 'off', purpose='probe')
        assert result['commits'] == result['final_audits'] == 1
        assert result['calls'] == {'consultant': 1, 'planner': 1, 'generate': 1}
    asyncio.run(run())


def test_answer_construction_failure_does_not_commit_task_completion(tmp_path):
    asyncio.run(managed_check(tmp_path, 'off', answer_failure=True))


@pytest.mark.parametrize('negative', [False, True])
def test_committed_observations_reach_next_reporting_generate_request(tmp_path, negative):
    async def run():
        result = await managed_check(tmp_path, 'off', negative=negative, reporting=True)
        assert result['commits'] == 2
        assert result['calls'] == {'consultant': 1, 'planner': 1, 'generate': 2}
    asyncio.run(run())


def test_only_current_committed_observations_can_be_delivered(tmp_path):
    from agent_core.committed_observations import committed_observation_outcomes, no_file_final_response
    captured = []
    asyncio.run(managed_check(tmp_path, 'off', negative=True, capture=captured.append))
    original = captured[0]
    assert committed_observation_outcomes(original)[0]['observations'][0]['value']['input-b.tsv'] is False
    # Existing accepted execution projections may contain several effect kinds.
    # Adding durable observations must preserve the other explicitly supplied
    # facts, while absent artifact evidence must stay absent.
    from agent_core.node_context import _accepted_execution_outcomes, SemanticHandleMap
    accepted = committed_observation_outcomes(original)[0]
    mixed_raw = {**accepted['scope'], 'exit_code': 0, 'execution_status': 'succeeded',
        'effect_evidence': {
            'produces_bindings': [{'identity': 'fixture_reference', 'value': 'already checked'}],
            'workspace_mutations': [{'identity': 'fixture_workspace_change', 'workspace_ref': 'fixture_workspace',
                                     'mutation': 'annotate', 'value': 'recorded'}],
            'produces_observations': [{'identity': 'checked_columns', 'value': 'unaccepted caller replacement'}],
        }}
    raw_before = deepcopy(mixed_raw)
    step_handles = {accepted['step_id']: 'step.inspection'}
    projected = _accepted_execution_outcomes(original, [mixed_raw], step_handles, SemanticHandleMap())
    assert len(projected) == 1
    assert projected[0]['effect_evidence']['produces_bindings'][0]['identity'] == 'fixture_reference'
    assert projected[0]['effect_evidence']['workspace_mutations'][0]['mutation'] == 'annotate'
    assert projected[0]['effect_evidence']['produces_observations'] == accepted['observations']
    assert 'produced_artifacts' not in projected[0]
    assert mixed_raw == raw_before
    from agent_core.node_context import SemanticHandleBinding
    from agent_core.managed_artifacts import _observed_file_metadata, reconcile_runtime_outputs
    mixed_file = tmp_path / 'mixed-projection-evidence.txt'
    mixed_file.write_text('small synthetic artifact projection evidence\n')
    observed_file = {**_observed_file_metadata(mixed_file),
                     'attempt_id': accepted['scope']['attempt_id'], 'current_attempt_owned': True}
    reconciliation = reconcile_runtime_outputs([{'artifact_id': 'fixture-mixed-result',
        'path': str(mixed_file), 'artifact_kind': 'file', 'format': 'text'}],
        [observed_file], attempt_id=accepted['scope']['attempt_id'], exit_code=0)
    assert reconciliation['status'] == 'accepted'
    artifact_handles = SemanticHandleMap([SemanticHandleBinding('output.fixture_result', 'artifact',
                                                               'fixture-mixed-result', 'output')])
    with_artifact = _accepted_execution_outcomes(original,
        [{**mixed_raw, 'reconciled_outputs': reconciliation['bindings']}], step_handles, artifact_handles)
    assert with_artifact[0]['produced_artifacts'] == ['output.fixture_result']
    assert with_artifact[0]['effect_evidence'] == projected[0]['effect_evidence']
    absent_raw = _accepted_execution_outcomes(original, [], step_handles, SemanticHandleMap())
    assert list(absent_raw[0]['effect_evidence']) == ['produces_observations']
    stale_raw = {**mixed_raw, 'attempt_id': 'another-attempt'}
    assert _accepted_execution_outcomes(original, [stale_raw], step_handles, SemanticHandleMap()) == absent_raw
    for field in ('task_id', 'plan_id', 'plan_version'):
        changed = deepcopy(original)
        changed[field] = 'unrelated-' + field
        assert committed_observation_outcomes(changed) == [], field
        with pytest.raises(ValueError, match='ACCEPTED_OBSERVATION_MISSING'):
            no_file_final_response(changed)
    for field in ('step_id', 'action_id', 'attempt_id', 'validation_id'):
        changed = deepcopy(original)
        consumption = next(iter(changed['task_state']['semantic_completion_consumptions'].values()))
        consumption['scope'][field] = 'unrelated-' + field
        assert committed_observation_outcomes(changed) == [], field
    for record in ('typed_step_commit_receipts', 'semantic_completion_consumptions'):
        changed = deepcopy(original)
        changed['task_state'][record] = {}
        assert committed_observation_outcomes(changed) == []
    changed = deepcopy(original)
    next(iter(changed['provenance_state']['attempts'].values()))['status'] = 'EVIDENCE_COMMITTED'
    assert committed_observation_outcomes(changed) == []
    # Projection never edits the durable typed evidence and keeps valid falsy
    # facts rather than equating them to absent observations.
    before = deepcopy(original)
    for value in (False, 0, None, {}):
        changed = deepcopy(original)
        validation = next(iter(changed['provenance_state']['validations'].values()))
        validation['effect_results']['produces_observations'][0]['value'] = value
        assert committed_observation_outcomes(changed)[0]['observations'][0]['value'] == value
        assert json.dumps(value) in no_file_final_response(changed)['text']
    assert original == before
    changed = deepcopy(original)
    for step in changed['steps']:
        for key in ('produces_observations', 'step_effect'):
            step.pop(key, None)
        step['produces_bindings'] = ['available_reference']
    assert no_file_final_response(changed) is None
