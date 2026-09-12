"""Synthetic checkpoint only: production interactive continue -> go -> real graph."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch
from langchain_core.messages import AIMessage
import pytest


async def stopped_checkpoint(case, task_id):
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from agent_core.workflow.early_nodes import TypedConsultant
    from agent_core.workflow.managed_stop import build_managed_stop, _save
    runner = case._runner_state(task_id)
    durable = load_plan_state(case.root, task_id)
    import bio_agents
    # Reproduce the historical source behavior through its actual shared tool:
    # only disable the two new fixes while constructing this synthetic checkpoint.
    with patch.object(bio_agents, 'close_consultant_skill_turn', side_effect=lambda state, decision, update: update), \
         patch('agent_core.workflow.consultant_resume.enter_consultant', side_effect=lambda state: state):
        update = await TypedConsultant(data_path=case.root, source=case.agent._schema2_early_sources['consultant']).run(runner)
    outcome = update['consultant_outcome']
    durable = load_plan_state(case.root, task_id)
    assert durable['task_state']['skill_access_state']['continuations']
    assert not durable['task_state'].get('consultation_state')
    assert outcome['type'] == 'harness_error'
    boundary = {'node': 'consultant', 'status': 'failed', 'outcome': outcome}
    stop = build_managed_stop(task_id, runner, durable, boundary)
    # Old checkpoints predate source-entry receipts; lifecycle remains absent.
    stop.pop('consultant_invocation_id', None)
    stop.update(persistence_status='committed', reports_status='finalized')
    _save(case.root, stop, loader=load_plan_state, saver=save_plan_state)
    runner.update(consultant_outcome=outcome, managed_run_boundary=boundary,
                  terminal_diagnostic={'source_node': 'consultant', 'error_code': 'CONSULTATION_LIFECYCLE_CONTRADICTION'})
    app = case.agent._app_for_task(task_id, runner)
    await app.aupdate_state({'configurable': {'thread_id': task_id}}, runner, as_node='consultant')
    return runner, stop


async def continue_case(*, new_failure):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.managed_stop import current_managed_stop, projection
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_cli_model_progress_integration import artifact
    from tests.test_consultant_skill_handoff import SEARCH
    service_writer_callbacks(asyncio.get_running_loop())
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp()
    counts = {'consultant': 0, 'summary': 0, 'executions': 0}
    requests = []
    failure_reasons = []
    mark_failure = case.agent._mark_plan_current_blocked
    def observe_failure(task_id, reason):
        failure_reasons.append(reason)
        return mark_failure(task_id, reason)
    case.agent._mark_plan_current_blocked = observe_failure
    async def provider(messages, *, stage='', **kwargs):
        assert stage == 'consultant', stage
        counts['consultant'] += 1
        assert counts['consultant'] <= 2
        requests.append([{'type': m.type, 'content': m.content} for m in messages])
        if counts['consultant'] == 2:
            if new_failure:
                raise ValueError('SYNTHETIC_NEW_PROVIDER_FAILURE')
            payload = case._consultant_payload(case.root / 'report.md')
            payload.update(action_type='ask_user', question='Which synthetic report format do you want?',
                           requested_slots=['report_format'])
            return AIMessage(content=json.dumps(payload))
        return AIMessage(content=json.dumps(SEARCH))
    async def summary(*args, **kwargs):
        assert kwargs.get('stage') == 'terminal_summary'
        counts['summary'] += 1
        assert counts['summary'] <= 1
        return AIMessage(content='{"fact_ids": ["stop_reason"]}')
    case.agent._ainvoke_node_llm = provider
    case.agent._ainvoke_llm = summary
    case.agent._sync_runtime_registries_at_startup = lambda: None
    answers = iter(['continue', 'continue', '/exit'])
    async def prompt(task_id, stage, *args, **kwargs):
        if stage == 'consultant_question':
            from agent_core.human_io import HumanInputEOF
            raise HumanInputEOF('Synthetic unanswered question')
        assert stage == 'interactive_prompt', stage
        return next(answers)
    case.agent._aprompt_user = prompt
    task_id = 'session_1700000001'
    try:
        runner, old = await stopped_checkpoint(case, task_id)
        assert counts == {'consultant': 1, 'summary': 0, 'executions': 0}
        historical_requests = deepcopy(requests)
        requests.clear()
        counts['consultant'] = 0
        artifact('consultant-before-continue.json', load_plan_state(case.root, task_id))
        assert case.agent._continue_control_state(task_id)['action'] == 'resume'
        before = load_plan_state(case.root, task_id)
        assert current_managed_stop(before)['stop_id'] == old['stop_id']
        assert before['task_state']['status'] == 'failed'  # preview did not clear stop
        case.agent._managed_terminal_results = {task_id: projection(old)}
        with patch.object(bio_agents.time, 'time', return_value=1700000001):
            await case.agent.interactive_mode()
        saved = load_plan_state(case.root, task_id)
        current = current_managed_stop(saved)
        artifact('consultant-resume-check-' + str(new_failure) + '.json', {'requests': requests, 'saved': saved, 'counts': counts})
        assert counts == {'consultant': 2, 'summary': int(new_failure), 'executions': 0}, counts
        if new_failure:
            assert current and current['stop_id'] != old['stop_id'], saved['task_state']
            assert current['stop_reason']['error_code'] == 'CONSULTANT_PROVIDER_FAILED', current
            assert 'SYNTHETIC_NEW_PROVIDER_FAILURE' in current['stop_reason']['detail']
            assert current['summary_status'] == 'COMPLETED', current
            assert saved['task_state']['managed_run_stop_history'][old['stop_id']] == old
            assert saved['task_state']['consultant_invocation']['invocation_id'] == current['consultant_invocation_id']
            assert case.agent._continue_control_state(task_id)['action'] == 'terminal'
            assert len(failure_reasons) == 1 and 'SYNTHETIC_NEW_PROVIDER_FAILURE' in failure_reasons[0]
        else:
            assert not current, current
            assert saved['task_state']['managed_run_stop'] == old
            assert case.agent._continue_control_state(task_id)['action'] == 'waiting_user'
            snapshot = case.agent._app_for_task(task_id).get_state({'configurable': {'thread_id': task_id}})
            assert case.agent._terminal_status_from_snapshot(task_id, snapshot) == 'waiting_user'
            result = case.agent._interactive_run_result(task_id, 'waiting_user', snapshot)
            assert 'CONSULTATION_LIFECYCLE_CONTRADICTION' not in result.reason
        assert not (saved.get('provenance_state') or {}).get('attempts')
        artifact('consultant-production-continue-' + ('new-failure' if new_failure else 'waiting-user') + '.json', {
            'source': 'synthetic checkpoint and production interactive_mode',
            'historical_component_calls': {'consultant': 1, 'executions': 0},
            'historical_provider_requests': historical_requests, 'calls': counts, 'old_stop': old, 'new_stop': current, 'provider_requests': requests,
            'entry_receipt': saved['task_state']['consultant_invocation'], 'history_preserved': True,
            'actual_source_failure_reasons': failure_reasons})
    finally:
        await case.asyncTearDown()


@pytest.mark.parametrize('new_failure', [False, True])
def test_production_continue_reenters_and_preserves_historical_stop(new_failure):
    asyncio.run(continue_case(new_failure=new_failure))


@pytest.mark.parametrize('change', ['cancelled', 'completed', 'blocked', 'new_fault', 'lifecycle', 'execution'])
def test_non_legacy_failures_are_not_eligible(change):
    # Pure eligibility check on synthetic facts; graph acceptance is tested above.
    from agent_core.workflow.consultant_resume import legacy_handoff_stop
    from agent_core.workflow.managed_stop import _scope
    from agent_core.workflow.inspection_stop import _authority
    durable = {'task_id': 'synthetic', 'task_state': {'status': 'failed', 'skill_access_state': {'continuations': {'synthetic': {'caller_node': 'consultant', 'request_kind': 'SEARCH_SKILLS', 'status': 'served'}}}}}
    stop = {'producer': 'managed_run_stop_v1', 'persistence_status': 'committed',
            'scope': _scope(durable), 'authority': _authority(durable), 'stop_id': 'synthetic-old',
            'boundary': {'node': 'consultant', 'outcome': {'type': 'harness_error', 'payload': {
                'error_code': 'CONSULTATION_LIFECYCLE_CONTRADICTION', 'semantic_state': {'phase': ''}}}}}
    durable['task_state']['managed_run_stop'] = stop
    assert legacy_handoff_stop(durable)
    if change in {'cancelled', 'completed', 'blocked'}:
        durable['task_state']['status'] = change
    elif change == 'new_fault':
        stop['boundary']['outcome']['payload']['error_code'] = 'CONSULTANT_AGENT_NO_PROGRESS'
    elif change == 'lifecycle':
        durable['task_state']['consultation_state'] = {'phase': 'invalid'}
    else:
        durable['provenance_state'] = {'attempts': {'synthetic': {'status': 'failed'}}}
    assert not legacy_handoff_stop(durable)


def test_current_stop_scope_is_not_cleared_for_other_owners():
    from agent_core.workflow.managed_stop import current_managed_stop, _scope
    from agent_core.workflow.inspection_stop import _authority
    durable = {'task_id': 'synthetic', 'task_state': {'status': 'blocked'}}
    stop = {'producer': 'managed_run_stop_v1', 'persistence_status': 'committed',
            'scope': _scope(durable), 'authority': _authority(durable), 'stop_id': 'synthetic-recovery',
            'boundary': {'node': 'recovery'}}
    durable['task_state']['managed_run_stop'] = stop
    durable['task_state']['consultant_invocation'] = {'invocation_id': 'different-source-entry'}
    assert current_managed_stop(durable) == stop


def test_provider_diagnostic_requires_current_invocation():
    from agent_core.workflow.early_nodes import TypedConsultant
    node = TypedConsultant(data_path='/unused-synthetic', source=None)
    state = {'task_id': 'synthetic', 'terminal_diagnostic': {
        'source_node': 'consultant', 'error_code': 'CONSULTANT_PROVIDER_FAILED',
        'detail': 'old provider failure', 'consultant_invocation_id': 'old-entry'}}
    durable = {'task_id': 'synthetic', 'task_state': {'status': 'blocked',
        'consultant_invocation': {'invocation_id': 'new-entry'}}}
    result = node.classify({}, state, durable)
    assert result['payload']['reason_code'] == 'consultation_terminal'
    assert 'old provider failure' not in json.dumps(result)


def test_missing_prompt_toolkit_falls_back_without_repeating_runtime(monkeypatch):
    import io
    import agent_core.cli_terminal as module
    from agent_core.cli_display import terminal
    from tests.test_cli_v2_display import service_writer_callbacks
    class Writer(io.StringIO):
        def isatty(self):
            return True
    writer, notices, calls = Writer(), io.StringIO(), []
    monkeypatch.setattr(module.sys, 'stdout', writer)
    monkeypatch.setattr(module.sys, 'stderr', notices)
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setenv('BIOMIND_UI', '')
    def missing(**kwargs):
        raise ModuleNotFoundError('synthetic missing prompt_toolkit')
    monkeypatch.setattr(module, 'Terminal', missing)
    async def runtime():
        service_writer_callbacks(asyncio.get_running_loop())
        calls.append(terminal().mode)
    asyncio.run(module.run_terminal(runtime))
    assert calls == ['plain']
    assert 'requirements-cli.txt' in notices.getvalue()
