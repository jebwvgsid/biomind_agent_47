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


async def managed_check(root, mode, *, negative=False, reject_missing_delivery=False, purpose='validation'):
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

    async def provider(messages, *, stage, node_context=None, **kwargs):
        assert stage in counts, 'Unexpected model call: ' + stage
        counts[stage] += 1
        assert counts[stage] == 1
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
        with patch.object(bio_agents, 'run_script_path', execute):
            app = case.agent._app_for_task(task_id, runner)
            config = {'configurable': {'thread_id': task_id}, 'recursion_limit': 20}
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
        assert counts == {'consultant': 1, 'planner': 1, 'generate': 1}, events
        assert len(approvals) == len(executions) == 1
        assert executions[0]['returncode'] == 0
        attempt = next(iter(saved['provenance_state']['attempts'].values()))
        approved = Path(attempt['approved_script']['path']).read_bytes()
        generated = Path(attempt['generated_script']['path']).read_bytes()
        assert approved == generated == approvals[0].encode()
        assert hashlib.sha256(approved).hexdigest() == attempt['approved_script']['sha256']
        assert Path(attempt['stdout_path']).read_text() == executions[0]['stdout']
        assert attempt['effect_evidence']['parser_receipt']['raw_stdout_sha256'] == hashlib.sha256(executions[0]['stdout'].encode()).hexdigest()
        assert len(saved['task_state'].get('typed_step_commit_receipts', {})) == 1, events
        assert len(saved['task_state'].get('typed_final_audit_receipts', {})) == 1, events
        from agent_core.plan_contract import plan_uses_explicit_results
        assert plan_uses_explicit_results(saved)
        final = next(value['final_audit_outcome'] for event in events for value in event.values()
                     if isinstance(value, dict) and 'final_audit_outcome' in value)
        assert final['type'] == 'task_verified'
        assert final['payload']['completion_gate']['classification'] == 'plan_result_evidence_satisfied'
        assert saved['steps'][0]['outputs'] == []
        assert not saved.get('plan_artifacts')
        assert not any('recovery' in event for event in events)
        assert '检查结论' in executions[0]['stdout']
        assert sorted(p.name for p in root.iterdir()) == ['input-a.tsv', 'input-b.tsv']
        artifact('plan-results-managed-' + mode + '-' + str(negative) + '-' + purpose + '.json', {
            'source': 'synthetic managed production graph', 'original_request': request,
            'actual_planner_request_contains_original': True, 'approved_plan': plan,
            'mode': mode, 'calls': counts, 'approvals': len(approvals), 'executions': executions,
            'approved_sha256': attempt['approved_script']['sha256'],
            'parser_receipt': attempt['effect_evidence']['parser_receipt'],
            'commit_count': 1, 'final_audit_count': 1,
            'final_outcome': final, 'graph_nodes': [list(event) for event in events],
            'scope_limit': 'Fresh isolated scratch roots differ; execution wrappers are hash verified independently. Fixed approved body, raw stdout, routes and calls compared across display modes.'})
        # Instrumented execution wrappers include each fresh scratch root. Each
        # is verified above at the real process boundary; compare the fixed
        # approved response and original output bytes across display modes.
        return {'calls': counts, 'executions': [{k: v for k, v in row.items() if k != 'executed_sha256'} for row in executions],
                'approved_sha256': attempt['approved_script']['sha256'], 'approvals': len(approvals),
                'commits': 1, 'final_audits': 1, 'graph_nodes': [list(event) for event in events],
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
