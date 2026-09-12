"""Synthetic stage-purpose regression; no scientific programs or live provider."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
import pytest


async def _stage_flow(root, reporting_step, reporting_purpose, *, empty_report=False):
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
    owner = PlainTerminal(io.StringIO(), enabled=False)
    previous = bind_terminal(owner)
    task_id = 'purpose-stage-synthetic'
    source = root / 'tiny.tsv'
    source.write_text('sample\tvalue\na\t1\n')
    request = f'实际检查小表 {source} 的列并写 report.md。仅检查与报告，不运行科学分析。'
    if empty_report:
        request = f'实际检查小表 {source} 的列；同时创建名为 report.md 的空文件作为用户要求的存在标记，不向空文件写内容。'
    plan = case._planner_payload()
    plan['metadata']['result_contract_version'] = 1
    plan['proposed_artifacts'] = [{
        'local_ref': 'plan.report',
        'semantic_view': {'role': 'report', 'format': 'text', 'description': '用户要求的检查报告'},
        'artifact_contract': {'object_kind': 'file', 'lifecycle': 'plan_local',
            'materialization_policy_id': 'managed_intermediate_v1', 'required': True,
            'path_policy': 'managed_exact', 'path': 'report.md', 'validator': 'file_exists'}}]
    first, final = plan['steps']
    first.update(step_id='00.input_check', title='检查小表的真实列', purpose='validation',
        inputs=[], outputs=[], dependencies=[],
        produces_observations=[{'identity': 'checked_columns', 'evidence_contract': {'required_fields': ['value']}}],
        success_criteria=['实际读取一份小表并记录列检查结论'])
    final.update(step_id=reporting_step, title='保存已检查事实报告', purpose=reporting_purpose,
        inputs=[], outputs=['plan.report'], dependencies=['00.input_check'],
        success_criteria=['根据已接纳的检查结果保存 report.md'])
    if empty_report:
        final.update(title='创建用户要求的空文件存在标记', success_criteria=['report.md 存在，按要求保持零字节'])
    plan['validation_checklist'] = [{'check_id': 'report', 'description': 'report.md 如实包含实际检查结论'}]
    counts = {'consultant': 0, 'planner': 0, 'generate': 0}
    approvals, executions, events, actual_contexts = [], [], [], []
    expected = {'tiny.tsv': {'value_column_present': True, 'rows_read': 1}}

    async def provider(messages, *, stage, node_context=None, **kwargs):
        assert stage in counts, 'Unexpected model stage: ' + stage
        counts[stage] += 1
        if stage == 'consultant':
            assert counts[stage] == 1
            return AIMessage(content=json.dumps({'action_type': 'submit_planning_handoff',
                'summary': request, 'selected_skills': [case.SKILL_ID],
                'task_spec_operations': [{'operation': 'set_goal', 'value': request, 'supporting_evidence': ['U1']}],
                'unresolved_ambiguities': [], 'planner_feedback': None}, ensure_ascii=False))
        if stage == 'planner':
            assert counts[stage] == 1
            assert request in '\n'.join(str(message.content) for message in messages)
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        assert counts['generate'] <= 2
        payload = node_context.llm_payload.to_dict()
        actual_contexts.append(deepcopy(payload))
        if counts['generate'] == 1:
            body = ("/usr/bin/python3 <<'PY'\nimport json\nfrom pathlib import Path\n" +
                f'p=Path({str(source)!r})\n' +
                "lines=p.read_text().splitlines()\n" +
                "value={p.name: {'value_column_present': 'value' in lines[0].split('\\t'), 'rows_read': len(lines)-1}}\n" +
                "print('BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'schema_version': 1, 'produces_observations': [{'identity': 'checked_columns', 'value': value}]}))\nPY")
            return AIMessage(content='<shell>' + body + '</shell>')
        outcomes = (payload.get('semantic_history') or {}).get('accepted_execution_outcomes') or []
        observed = [row for outcome in outcomes for row in
                    (outcome.get('effect_evidence') or {}).get('produces_observations', [])]
        assert any(row.get('identity') == 'checked_columns' and row.get('value') == expected for row in observed), payload
        assert any(outcome.get('evidence_source') == 'accepted_typed_step_commit' for outcome in outcomes)
        runtime = (payload.get('current_focus') or {}).get('runtime', {})
        env_rows = runtime.get('artifact_environment') or runtime.get('current_action_artifact_bindings') or []
        outputs = [row for row in env_rows if str(row.get('semantic_handle') or '').startswith('output.')]
        assert len(outputs) == 1, runtime
        output = outputs[0]
        env_name = str(output.get('env_var') or '')
        expected_path = str(output.get('expected_path') or output.get('path') or '')
        assert env_name or expected_path, output
        reference = '"$' + env_name + '"' if env_name else shlex.quote(expected_path)
        body = 'mkdir -p "$(dirname ' + reference + ')"\n'
        if empty_report:
            body += ': > ' + reference + "\nprintf '%s\\n' '-rw-r--r-- 1 owner group 0 Sep 12 00:00 report.md'"
        else:
            body += "printf '%s\\n' " + shlex.quote(json.dumps(expected, ensure_ascii=False)) + ' > ' + reference
        return AIMessage(content='<shell>' + body + '</shell>')

    case.agent._ainvoke_node_llm = provider
    case.agent._ainvoke_llm = provider
    case.agent.middleware.review_code = lambda code, language, **kw: (approvals.append(code) or 'approve', code)

    async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
        from execution.executor import run_script_path
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
        allow = sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
        allow.add(str(path))
        try:
            result = await run_script_path(path, expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}, managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            allow.discard(str(path))
        executions.append({'sha256': expected_sha256, 'stdout': result[0], 'stderr': result[1], 'returncode': result[2]})
        return result

    try:
        durable, _, _ = case.agent._persist_user_submission(task_id, request)
        admission = {'schema_version': 1, 'request_kind': 'execute', 'execution_scope': 'managed',
            'managed_intent_explicit': True, 'task_mode': 'managed', 'retrieval_mode': 'kb',
            'confidence': 1.0, 'admission_status': 'managed_execution_admitted',
            'reason': 'Synthetic purpose stage flow', 'decision_source': 'test_fixture'}
        durable.setdefault('task_state', {})['intent_admission'] = admission
        save_plan_state(case.root, task_id, durable)
        runner = {'messages': [HumanMessage(content=request)], 'task_id': task_id, 'session_id': task_id,
            'project_id': case.agent.default_project_id, 'conversation_evidence': durable['conversation_evidence'],
            'task_spec': canonical_task_spec_for_state(durable), 'task_revision': 0, 'selected_skills': [],
            'resolved_bindings': {}, 'consultation_state': {}, 'planner_feedback': {},
            'request_intent': admission, 'intent_admission': admission, 'managed_intent_admitted': True}
        with patch.object(bio_agents, 'run_script_path', execute):
            app = case.agent._app_for_task(task_id, runner)
            async for event in app.astream(runner, config={'configurable': {'thread_id': task_id}, 'recursion_limit': 32}, stream_mode='updates'):
                events.append(event)
        await owner.drain(timeout=1)
        saved = load_plan_state(case.root, task_id)
        artifact('purpose-stage-final-evidence-' + reporting_purpose + '-' + str(empty_report) + '.json', {
            'source': 'synthetic production graph', 'calls': counts, 'executions': executions,
            'steps': saved['steps'], 'provenance': saved['provenance_state'],
            'typed_outcomes': [{node: {key: value for key, value in update.items() if key.endswith('_outcome') or key == 'validation_evidence'}
                               for node, update in event.items() if isinstance(update, dict)} for event in events],
            'task_state': {key: value for key, value in saved['task_state'].items() if key.startswith('typed_')}})
        assert counts == {'consultant': 1, 'planner': 1, 'generate': 2}
        assert len(approvals) == len(executions) == 2
        assert all(row['returncode'] == 0 for row in executions)
        assert len(saved['task_state'].get('typed_step_commit_receipts', {})) == 2, events
        outcome = next(value['final_audit_outcome'] for event in events for value in event.values()
                       if isinstance(value, dict) and 'final_audit_outcome' in value)
        assert outcome['type'] == 'task_verified', events
        assert not any('recovery' in event for event in events)
        assert saved['steps'][0]['outputs'] == []
        report_row = next(iter(saved['plan_artifacts'].values()))
        report = Path(report_row['canonical_path'])
        assert report.name == 'report.md'
        if empty_report:
            assert report.read_bytes() == b''
        else:
            assert json.loads(report.read_text()) == expected
        assert sorted(p.name for p in root.iterdir()) == ['tiny.tsv']
        artifact('plan-results-stage-purpose-' + reporting_purpose + '-' + str(empty_report) + '.json', {
            'source': 'synthetic production managed graph', 'plan': plan,
            'calls': counts, 'approvals': len(approvals), 'executions': executions,
            'accepted_observation_in_actual_generate_request': actual_contexts[1]['semantic_history']['accepted_execution_outcomes'],
            'graph_nodes': [list(event) for event in events], 'final_outcome': outcome,
            'report_content': report.read_text(), 'commit_count': 2,
            'scope_limit': 'Only tiny input read and report write; stage names model WGS handoff, no scientific task.'})
    finally:
        bind_terminal(previous)
        owner.close()
        await case.asyncTearDown()


@pytest.mark.parametrize('step,purpose', [('06.reporting', 'record_failure'), ('05.summary', 'validation')])
def test_observation_reaches_actual_reporting_request_and_formal_file_is_accepted(tmp_path, step, purpose):
    asyncio.run(_stage_flow(tmp_path, step, purpose))


def test_approved_zero_byte_file_exists_result_finishes_with_stdout_warning(tmp_path):
    asyncio.run(_stage_flow(tmp_path, '05.summary', 'validation', empty_report=True))


@pytest.mark.parametrize('negative', [False, True])
def test_actual_runtime_stop_keeps_science_incomplete_even_with_status_reports(negative):
    from tests.test_runtime_recovery_reporting import RuntimeRecoveryReportingTests
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.runtime_recovery_reporting import finalize_runtime_recovery_result
    from tests.test_cli_model_progress_integration import artifact

    async def run():
        case = RuntimeRecoveryReportingTests(methodName='test_negative_requires_durable_complete_evidence')
        await case.asyncSetUp()
        try:
            case.initialize()
            if negative:
                case.probe_result = {'inspection_status': 'succeeded', 'observed_at': datetime.now(timezone.utc).isoformat(),
                    'audit': {'skill_id': 'fixture/generic', 'env': 'testenv', 'status': 'ready'},
                    'coverage': {'runtime.widget.bin': {'availability': 'UNAVAILABLE', 'reason_code': 'complete_search_absent',
                        'conclusion_complete': True, 'value_kind': 'executable_path'}}}
            result = await case.recovered()
            before = load_plan_state(case.root, case.task_id)
            final = finalize_runtime_recovery_result(case.root, case.task_id, result)
            after = load_plan_state(case.root, case.task_id)
            assert final['resolution_disposition'] == ('dependency_blocked' if negative else 'unresolved'), final
            assert final['runtime_recovery_result']['scientific_goal_completed'] is False
            assert final['runtime_recovery_result']['summary_llm_calls'] == 0
            assert before['steps'] == after['steps'] and before['provenance_state'] == after['provenance_state']
            assert set(final['report_paths']) == {'report.md', 'reproduce.sh', 'stage_status.tsv'}
            assert not (case.task_root / 'annotated.vcf').exists()
            assert '科学目标完成：否' in Path(final['report_paths']['report.md']).read_text()
            artifact('plan-results-real-stop-' + str(negative) + '.json', {
                'source': 'synthetic durable runtime recovery, stub inspection',
                'result': final, 'provenance_unchanged': True, 'steps_unchanged': True,
                'scope_limit': 'Status report generation does not complete the unexecuted scientific stage.'})
        finally:
            await case.asyncTearDown()
            case.doCleanups()
    asyncio.run(run())


@pytest.mark.parametrize('marker,approved,check,contents,stderr,expected_status', [
    (True, True, 'file_exists', b'', '', 'VALID_WITH_WARNINGS'),
    (True, True, 'file_nonempty', b'', '', 'INVALID'),
    (True, True, 'file_exists', b'actual nonempty content', '', 'VALID_WITH_WARNINGS'),
    (False, True, 'file_exists', b'', '', 'INVALID'),
    (True, False, 'file_exists', b'', '', 'INVALID'),
    (True, True, 'file_exists', b'actual content', 'rscript: command not found', 'INVALID'),
])
def test_supervisor_uses_approved_result_validator_not_stdout_filename(
        tmp_path, marker, approved, check, contents, stderr, expected_status):
    from supervisor import SupervisorNode
    from agent_core.plan_tracker import build_plan_state, save_plan_state
    from tests.test_plan_result_contract_v1 import state_with_observation

    result_path = tmp_path / 'result.txt'
    result_path.write_bytes(contents)
    durable = build_plan_state('synthetic-results', '- [>] Step 1: Inspect | Action: EXECUTE | Done: check actual file\n')
    durable.update({key: value for key, value in state_with_observation(tmp_path).items()
                    if key not in {'current_task', 'task_state', 'schema_version'}})
    durable['approval_status'] = 'approved' if approved else 'pending'
    if not marker:
        durable['plan_document']['metadata'].pop('result_contract_version')
    durable['provenance_state']['actions']['action-1'] = {
        'action_id': 'action-1', 'step_id': 'inspect', 'plan_id': 'plan-results',
        'plan_version': 'pv1', 'status': 'bound', 'normalized_action': {'step_id': 'inspect'}}
    save_plan_state(tmp_path, 'synthetic-results', durable)
    stdout = '-rw-r--r-- 1 owner group 0 Sep 12 00:00 result.txt\n'
    evidence = {'task_id': 'synthetic-results', 'step_id': 'inspect', 'action_id': 'action-1',
        'attempt_id': 'attempt-1', 'execution_id': 'execution-1', 'execution_status': 'succeeded',
        'command_role': 'primary', 'returncode': 0, 'raw_stdout': stdout, 'raw_stderr': stderr,
        'command': '/usr/bin/python3 synthetic_check.py'}
    runner = {'task_id': 'synthetic-results', 'execution_evidence': evidence,
        'verification_checks': [{'check_type': check, 'target': str(result_path), 'required': True}]}
    node = SupervisorNode(None, output_dir=str(tmp_path / 'task_outputs'), use_llm_review=False)
    result = asyncio.run(node.run(runner))
    validation = result['validation_evidence']
    assert validation['status'] == expected_status, validation
    assert evidence['raw_stdout'] == stdout and result_path.read_bytes() == contents
    if marker and approved:
        assert not any('Required-looking' in str(row) for row in validation.get('errors', []))
    if stderr:
        assert any('not found' in str(row).lower() or 'missing' in str(row).lower()
                   for row in validation.get('errors', [])), validation


@pytest.mark.parametrize('marker,materialized', [(True, False), (False, True)])
def test_allow_probe_producer_is_only_a_legacy_compatibility_flag(tmp_path, marker, materialized):
    from agent_core.plan_tracker import ensure_provenance_state, record_provenance_event
    from tests.test_plan_result_contract_v1 import state_with_observation

    state = state_with_observation(tmp_path, required=True, with_file=True)
    if not marker:
        state['plan_document']['metadata'].pop('result_contract_version')
    provenance = ensure_provenance_state(state)
    provenance['attempts']['attempt-1']['produced_artifact_ids'] = []
    provenance['attempts']['attempt-1']['artifact_version_ids'] = []
    artifact_id = state['steps'][0]['output_artifact_ids'][0]
    definition = provenance['artifacts'][provenance['artifact_heads'][artifact_id]]
    definition['allow_probe_producer'] = True
    path = Path(definition['path'])
    path.write_text('a diagnostic-produced file, not an approved Plan execution\n')
    record_provenance_event(state, 'execution_finished', {
        'attempt_id': 'attempt-1', 'execution_record': {
            'execution_id': 'probe-1', 'command_role': 'diagnostic', 'purpose': 'probe',
            'step_id': 'inspect', 'exit_code': 0, 'produced_artifacts': [str(path)]}})
    assert (artifact_id + '@attempt-1' in state['provenance_state']['artifacts']) is materialized


@pytest.mark.parametrize('artifact_failed', [False, True])
def test_warning_acceptance_preserves_individual_artifact_failure_and_downstream_visibility(tmp_path, artifact_failed):
    from agent_core.plan_tracker import ensure_provenance_state, record_provenance_event
    from agent_core.managed_artifacts import _validated_runtime_realization
    from tests.test_plan_result_contract_v1 import state_with_observation

    state = state_with_observation(tmp_path, required=False, with_file=True)
    provenance = ensure_provenance_state(state)
    artifact_id = state['steps'][0]['output_artifact_ids'][0]
    head = provenance['artifact_heads'][artifact_id]
    row = provenance['artifacts'][head]
    row.update(status='materialized', producer_attempt_id='attempt-1')
    Path(row['path']).write_bytes(b'')
    provenance['attempts']['attempt-1']['artifact_version_ids'] = [head]
    validation = {'validation_id': 'warning-validation', 'status': 'VALID_WITH_WARNINGS',
        'warnings': ['terminal evidence mentions a zero-byte file'],
        'artifact_results': [{'path': row['path'], 'satisfied': False}] if artifact_failed else []}
    record_provenance_event(state, 'validation_recorded', {'attempt_id': 'attempt-1', 'validation': validation})
    assert provenance['validations']['warning-validation']['status'] == 'VALID_WITH_WARNINGS'
    accepted = _validated_runtime_realization(state, 'plan.result')
    assert accepted == ('' if artifact_failed else row['path'])
    assert (row['status'] == 'valid') is not artifact_failed
