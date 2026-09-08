"""LEN/EXPLAIN real-workflow acceptance; no real models or science runner."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from agent_core.plan_tracker import load_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.early_nodes import TypedPlanner
from tests.planner_truncation_acceptance.harness import (
    Harness, Model, SummaryModel, F, complete_candidate, raw_length,
    recorded_length, receipt, response, run_stop,
)


def length_calls(state):
    return int(state['task_state']['planning_epoch'].get('length_recovery', {}).get('corrective_calls_used', 0))


def assert_unapproved_unexecuted(state):
    assert state.get('approval_status') != 'approved'
    assert not state.get('execution_attempts')
    assert not state.get('provenance_state', {}).get('attempts')


def test_synthetic_complete_fixture_real_plan_review(tmp_path):
    """Fixture-validity probe; itself is not truncation repair acceptance."""
    async def scenario():
        h = Harness(tmp_path, Model([response(complete_candidate())]))
        task = 'offline-complete-candidate-fixture'
        try:
            result = await h.run(h.bootstrap(task))
            state = load_plan_state(tmp_path, task)
            receipt('complete-fixture', {'source': __import__('bio_agents').__file__, 'result': result,
                    'durable': state, 'requests': h.model.calls, 'typed_traces': h.typed_traces})
            assert result['planner_outcome']['type'] == 'plan_review_ready', result
            assert result['plan_review_outcome']['type'] == 'approval_pending', result
            assert len(state['steps']) == 24
            assert state['plan_compilation_receipt']['status'] == 'compiled'
            assert state['plan_dataflow_receipt']['status'] == 'compiled'
            assert len(h.model.calls) == 1
            assert_unapproved_unexecuted(state)
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('first_kind', ['recorded_length', 'parseable_length', 'stop_invalid_json', 'unknown_invalid_json'])
def test_fresh_complete_regeneration_reaches_real_plan_review(tmp_path, first_kind):
    """T01/T06/T07/T09/T10: full new candidate, unchanged frozen materials."""
    async def scenario():
        valid = complete_candidate()
        first = {
            'recorded_length': recorded_length(1),
            'parseable_length': response(valid, 'length'),
            'stop_invalid_json': response('{"outcome_type":"SUBMIT_PLAN_FOR_REVIEW","steps":[ BAD ]}', 'stop'),
            'unknown_invalid_json': response('{"outcome_type":"SUBMIT_PLAN_FOR_REVIEW","steps":[ BAD ]}', None),
        }[first_kind]
        h = Harness(tmp_path, Model([first, response(valid)]))
        task = 'offline-regeneration-' + first_kind
        if first_kind == 'recorded_length':
            h.agent.llm_model_id = 'deepseek-v4-flash'
            h.agent.llm_endpoint_id = 'https://api.deepseek.com'
            h.agent.llm_provider_id = 'openai'
        try:
            initial = h.bootstrap(task)
            h.watch_provider(task)
            result = await h.run(initial)
            state = load_plan_state(tmp_path, task)
            failure = h.provider_states[1]['task_state']['planning_failure']
            context = StateContractStore(tmp_path, task).read_object(failure['context_ref'], expected_type='planner_correction_context')
            record = {'case': first_kind, 'source': __import__('bio_agents').__file__, 'result': result,
                      'durable': state, 'requests': h.model.calls, 'provider_states': h.provider_states,
                      'typed_traces': h.typed_traces, 'context': context, 'manifests': h.manifests(task),
                      'progress_events': h.progress_events, 'model_binds': h.model.binds}
            receipt('recovery-' + first_kind, record)
            assert len(h.model.calls) == 2
            assert result['planner_outcome']['type'] == 'plan_review_ready', result
            assert result['plan_review_outcome']['type'] == 'approval_pending'
            assert len(state['steps']) == 24
            assert not h.provider_states[1].get('steps')
            assert_unapproved_unexecuted(state)
            assert context['raw_candidate'] == first['content']
            # Full System, Consultant handoff Seed and inherited Skill prefix.
            prefix_count = context['fixed_prefix_count']
            assert h.model.calls[0][:prefix_count] == h.model.calls[1][:prefix_count]
            request = '\n'.join(str(m.get('content', '')) for m in h.model.calls[1])
            assert first['content'] in request
            assert 'genomics/wgs-short-read-mapping' in request and 'genomics/wgs-gatk-snp-indel' in request
            assert 'S1' in request and 'S2' in request
            assert state['task_state']['planning_epoch']['planner_calls_total'] == 2
            integrity = failure['response_integrity']
            if first_kind.endswith('length'):
                assert integrity['finish_reason'] == 'length'
                assert failure['validation_issues'][0]['code'] == 'PLANNER_OUTPUT_TRUNCATED'
                assert integrity['parser_diagnostic']['source'] == 'live_planner_envelope_parser'
                assert integrity['parser_diagnostic']['status'] == ('failed' if first_kind == 'recorded_length' else 'passed')
                if first_kind == 'recorded_length':
                    assert integrity['parser_diagnostic']['code'] == 'PLANNER_ENVELOPE_TRANSPORT_INVALID'
                    assert integrity['parser_diagnostic']['expected_type'] == 'single_json_object'
                    assert integrity['parser_diagnostic']['received_type'] == 'mixed_text_or_incomplete_fence'
                    limits = [b['extra_body']['max_tokens'] for b in h.model.binds if 'max_tokens' in b.get('extra_body', {})]
                    assert limits == [16384, 32768], h.model.binds
                    assert failure['remediation_authorization']['kind'] == 'increase_output_limit'
                    assert integrity['resolved_output_policy']['requested_output_limit'] == 16384
                    latest_integrity = state['task_state']['planning_epoch']['call_claim']['response_integrity']
                    assert latest_integrity['resolved_output_policy']['requested_output_limit'] == 32768
                    assert integrity['call_id'] == h.manifests(task)[0]['call_id']
                    assert integrity['provider_attempt_id'] == h.manifests(task)[0]['annotations']['provider_attempt']['attempt_id']
                else:
                    assert failure['remediation_authorization']['kind'] == 'compact_json'
                assert length_calls(state) == 1
                assert 'length' in request and ('完整' in request or 'complete' in request.lower())
                assert failure['occurrence_receipt']['id']
                assert failure['semantic_failure_fingerprint']
                assert state['task_state']['planning_epoch']['length_recovery']['compact_attempted'] is True
            else:
                assert integrity['finish_reason'] == ('stop' if first_kind.startswith('stop') else 'UNKNOWN')
                assert length_calls(state) == 0
                assert any(i['code'] == 'PLANNER_ENVELOPE_JSON_INVALID' for i in failure['validation_issues'])
                assert not any(i['code'] == 'PLANNER_OUTPUT_TRUNCATED' for i in failure['validation_issues'])
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['success', 'provider', 'timeout', 'disabled', 'budget', 'reader', 'store', 'writer'])
def test_repeated_length_real_outer_summary_and_delivery(tmp_path, mode):
    """T01/T11/T13/T18–T20: accurate bounded stop with actual caller/faults."""
    async def scenario():
        record = await run_stop(tmp_path, mode)
        result, diagnostic, stop = record['result'], record['diagnostic'], record['stop']
        before = record['before_outer']
        failure = before['task_state']['planning_failure']
        assert len(record['provider_requests']) == 2
        assert before['task_state']['planning_epoch']['planner_calls_total'] == 2
        assert before['task_state']['planning_epoch']['length_recovery']['corrective_calls_used'] == 1
        assert result['failure_code'] == 'PLANNER_LENGTH_RECOVERY_EXHAUSTED'
        assert result['stage'] == 'planner'
        assert 'PLANNER_NON_PROGRESS_REPLAY' not in json.dumps(record['typed_traces'][-1]['outcome'])
        assert failure['response_integrity']['finish_reason'] == 'length'
        assert failure['primary_failure']['response_integrity']['finish_reason'] == 'length'
        assert_unapproved_unexecuted(before)
        assert not before.get('steps')
        assert diagnostic['execution']['executed'] is False
        assert diagnostic['execution']['attempt_id'] == ''
        assert diagnostic['execution']['validation_status'] == 'not_performed'
        assert diagnostic['planning']['no_approved_plan'] is True
        assert 'length' in result['reason'].lower() or '长度' in result['reason']
        calls = 0 if mode in {'disabled', 'budget', 'store'} else 1
        assert len(record['summary_requests']) == calls
        assert diagnostic['summary_llm_calls'] == calls
        assert diagnostic['summary_status'] == ('SKIPPED' if not calls else 'FAILED' if mode in {'provider', 'timeout'} else 'COMPLETED')
        if calls:
            text = json.dumps(record['summary_requests'], ensure_ascii=False)
            assert 'length' in text and '8192' in text
            assert 'PLANNER_LENGTH_RECOVERY_EXHAUSTED' in text
            assert 'output_policy' in text and 'attempted_remediation' in text
        if mode in record['fault_hits']:
            assert record['fault_hits'][mode] > 0
            code = {'reader': 'PLANNER_CONTEXT_DETAILS_UNAVAILABLE', 'store': 'MANAGED_STOP_PERSIST_FAILED', 'writer': 'REPORT_WRITE_FAILED'}[mode]
            assert any(x['error_code'] == code for x in diagnostic['secondary_causes'])
        if mode == 'store':
            assert diagnostic['persistence_status'] == 'unconfirmed'
            assert not diagnostic['report_paths'] and not stop
        else:
            if mode == 'writer':
                assert stop['report_receipts']['report.md']['status'] == 'failed'
                assert not stop['report_paths']
            else:
                assert stop['report_receipts']['report.md']['status'] == 'created'
                assert stop['report_receipts']['stage_status.tsv']['status'] == 'created'
                report = Path(stop['report_paths']['report.md']).read_text()
                assert '长度' in report or 'length' in report.lower()
            assert stop['report_receipts']['reproduce.sh'] == {'status': 'skipped', 'reason': 'NO_EXECUTION_ATTEMPT'}
        assert not (tmp_path / 'task_outputs' / before['task_id'] / 'reproduce.sh').exists()
    asyncio.run(scenario())


async def first_rejection(h, task):
    """One real typed Planner producer, then stop at its durable node boundary."""
    initial = h.bootstrap(task)
    h.watch_provider(task)
    adapter = TypedPlanner(data_path=str(h.root), source=h.agent._schema2_early_sources['planner'])
    result = await adapter.run(initial)
    durable = load_plan_state(h.root, task)
    assert len(h.model.calls) == 1
    assert result['planner_outcome']['type'] == 'planning_continues', result
    assert durable['task_state']['planning_failure']['response_integrity']['finish_reason'] == 'length'
    return initial, result, durable


def test_correction_context_new_instance_reload_and_real_resume(tmp_path):
    """T08/T09: restart after a real node boundary, not after a claimed call."""
    async def scenario():
        task = 'offline-length-context-reload'
        first = Harness(tmp_path, Model([recorded_length(1)]))
        try:
            initial, update, before = await first_rejection(first, task)
            failure = before['task_state']['planning_failure']
            context = StateContractStore(tmp_path, task).read_object(failure['context_ref'], expected_type='planner_correction_context')
            receipt('reload-before', {'before': before, 'context': context, 'update': update})
            assert length_calls(before) == 0
            assert before['task_state']['planning_epoch']['planner_calls_total'] == 1
        finally:
            await first.close()
        second = Harness(tmp_path, Model([response(complete_candidate())]))
        try:
            result = await second.run(second.input(task))
            after = load_plan_state(tmp_path, task)
            receipt('reload-after', {'result': result, 'durable': after, 'requests': second.model.calls, 'typed_traces': second.typed_traces})
            assert len(second.model.calls) == 1
            assert result['plan_review_outcome']['type'] == 'approval_pending'
            assert length_calls(after) == 1
            assert after['task_state']['planning_epoch']['planner_calls_total'] == 2
            assert after['task_state']['planning_epoch']['planning_epoch_id'] == before['task_state']['planning_epoch']['planning_epoch_id']
            text = '\n'.join(str(m.get('content', '')) for m in second.model.calls[0])
            assert raw_length(1) in text and 'length' in text and '8192' in text
            assert_unapproved_unexecuted(after)
        finally:
            await second.close()
    asyncio.run(scenario())


def test_true_occurrence_replay_idempotence(tmp_path):
    """T12: real first rejection's receipt, unchanged reload, no new producer."""
    async def scenario():
        from agent_core.planner_recovery import complete_planner_claim
        h = Harness(tmp_path, Model([recorded_length(1)]))
        task = 'offline-length-occurrence-replay'
        try:
            initial, update, durable = await first_rejection(h, task)
            stored = deepcopy(durable)
            claim = durable['task_state']['planning_epoch']['call_claim']
            failure = durable['task_state']['planning_failure']
            complete_planner_claim(durable, claim, failure['response_integrity'], outcome='candidate_rejected')
            assert durable == stored
            before = {**initial, 'planner_outcome': update['planner_outcome']}
            adapter = TypedPlanner(data_path=str(tmp_path), source=None)
            outcome = adapter.classify(before, before, durable)
            assert outcome['type'] == 'harness_error'
            assert outcome['payload']['error_code'] == 'PLANNER_NON_PROGRESS_REPLAY'
            assert load_plan_state(tmp_path, task)['state_generation'] == stored['state_generation']
            assert len(h.model.calls) == 1
            assert length_calls(durable) == 0
            assert_unapproved_unexecuted(durable)
            receipt('true-occurrence-replay', {'stored': stored, 'after_idempotent_completion': durable, 'typed_outcome': outcome})
        finally:
            await h.close()
    asyncio.run(scenario())


def test_concurrent_remediation_claim_one_wins_and_restart_does_not_redispatch(tmp_path):
    """T14/T15: actual file CAS, two same-generation claimers, fresh process view."""
    async def scenario():
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from agent_core.planner_recovery import (claim_planner_call, remediation_signature,
                                                select_length_remediation, unsettled_call)
        task = 'offline-length-concurrent-claim'
        first = Harness(tmp_path, Model([recorded_length(1)]))
        try:
            _, _, before = await first_rejection(first, task)
            failure = before['task_state']['planning_failure']
            authorization = failure['remediation_authorization']
            policy = authorization['output_policy']
            assert authorization['status'] == 'available'
            assert remediation_signature(authorization['kind'], policy) == remediation_signature(authorization['kind'], {**policy, 'call_id': 'random-unrelated-id', 'timestamp': 'different', 'extras': {'arbitrary': 'presentation-only'}})
            barrier = threading.Barrier(2)
            snapshots = [load_plan_state(tmp_path, task), load_plan_state(tmp_path, task)]
            def claim(index):
                scope = deepcopy(failure['response_integrity']['scope'])
                scope.update(invocation_id='isolated-concurrent-invocation-' + str(index), candidate_scope_id='isolated-concurrent-candidate-' + str(index))
                barrier.wait(timeout=5)
                try:
                    state, claimed = claim_planner_call(str(tmp_path), task, snapshots[index], scope, policy, authorization)
                    return {'status': 'claimed', 'claim': claimed, 'state': state}
                except Exception as exc:
                    return {'status': 'rejected', 'exception': type(exc).__name__, 'code': str(getattr(exc, 'code', '')), 'detail': str(exc)}
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, [0, 1]))
            after = load_plan_state(tmp_path, task)
            assert [r['status'] for r in results].count('claimed') == 1
            rejected = next(r for r in results if r['status'] == 'rejected')
            assert rejected['exception'] == 'StateCASConflict' or rejected['code'] == 'PLANNER_CALL_RESULT_UNKNOWN', rejected
            assert after['task_state']['planning_epoch']['planner_calls_total'] == 2
            assert length_calls(after) == 1 and unsettled_call(after)
            charged = deepcopy(after)
            policy_checks = []
            variants = [{**policy, 'requested_output_limit': cap, 'wire_output_parameter_value': cap} for cap in [16384, 32768, 16384, 65536]]
            variants.append({**policy, 'requested_output_limit': None, 'wire_output_parameter_key': None, 'wire_output_parameter_value': None, 'provider_capability_source': 'UNKNOWN'})
            for changed in variants:
                decision = select_length_remediation(after, policy, changed)
                assert decision['status'] == 'unavailable'
                assert decision['reason_code'] == 'PLANNER_LENGTH_RECOVERY_EXHAUSTED'
                policy_checks.append({'policy': changed, 'decision': decision})
            assert after == charged
            unchanged = load_plan_state(tmp_path, task)
            assert unchanged['task_state']['planning_epoch']['planner_calls_total'] == 2
            assert length_calls(unchanged) == 1
            assert unchanged['state_generation'] == charged['state_generation']
            assert len(first.model.calls) == 1
            receipt('concurrent-claim', {'before': before, 'results': results, 'after': after, 'policy_cycle_checks': policy_checks, 'durable_after_property_checks': unchanged})
        finally:
            await first.close()
        second = Harness(tmp_path, Model([]))
        second.agent.terminal_summary_llm = SummaryModel()
        second.agent.terminal_summary_policy = {'enabled': False}
        second.agent.state_manager = None
        try:
            result = await second.agent._run_agent_interrupt_loop(before['current_task']['task_spec']['goal'], task, resume_initial=True)
            restored = load_plan_state(tmp_path, task)
            receipt('unknown-claim-resume', {'result': result.__dict__, 'durable': restored, 'requests': second.model.calls})
            assert result.failure_code == 'PLANNER_CALL_RESULT_UNKNOWN', result
            assert second.model.calls == []
            assert restored['task_state']['planning_epoch']['planner_calls_total'] == 2
            assert length_calls(restored) == 1
            assert_unapproved_unexecuted(restored)
        finally:
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['task_revision', 'epoch'])
def test_late_provider_result_cannot_rebind_to_new_scope(tmp_path, change):
    """T16: a real producer call observes an independently committed transition."""
    async def scenario():
        from agent_core.plan_tracker import save_plan_state, commit_consultant_candidate_state
        from agent_core.plan_contract import start_planning_epoch
        task = 'offline-late-planner-' + change
        h = Harness(tmp_path, Model([]))
        initial = h.bootstrap(task)
        advanced = {}
        def transition(model, messages):
            before = load_plan_state(tmp_path, task)
            if change == 'task_revision':
                spec = deepcopy(before['current_task']['task_spec'])
                spec['revision'] += 1
                spec['goal'] += ' Explicit isolated revision from authoritative task owner.'
                current = commit_consultant_candidate_state(tmp_path, task, {'task_spec': spec})
            else:
                current = deepcopy(before)
                old = current['task_state']['planning_epoch']
                current['task_state']['planning_epoch'] = start_planning_epoch(
                    task_revision=current['current_task']['task_revision'],
                    task_semantic_identity=current['current_task']['task_semantic_hash'], base_plan_revision=0,
                    parent_epoch_id=old['planning_epoch_id'], start_reason='isolated-authoritative-new-epoch')
                save_plan_state(tmp_path, task, current)
                current = load_plan_state(tmp_path, task)
            advanced.update(current)
            return recorded_length(1)
        h.model.responses = [transition]
        try:
            adapter = TypedPlanner(data_path=str(tmp_path), source=h.agent._schema2_early_sources['planner'])
            result = await adapter.run(initial)
            final = load_plan_state(tmp_path, task)
            receipt('late-result-' + change, {'advanced': advanced, 'result': result, 'final': final, 'requests': h.model.calls})
            assert len(h.model.calls) == 1
            assert final['current_task']['task_revision'] == advanced['current_task']['task_revision']
            assert final['task_state']['planning_epoch']['planning_epoch_id'] == advanced['task_state']['planning_epoch']['planning_epoch_id']
            assert not final.get('steps')
            failure = final['task_state'].get('planning_failure', {})
            assert failure.get('response_integrity', {}).get('raw_digest') is None
            assert 'stale' in json.dumps(result, default=str).lower(), result
            assert_unapproved_unexecuted(final)
        finally:
            await h.close()
    asyncio.run(scenario())


def test_length_remediation_contract_failure_returns_to_original_admission(tmp_path):
    """T17: a normal-ending correction still has to satisfy real dataflow."""
    async def scenario():
        candidate = json.loads(complete_candidate())
        target = next(s for s in candidate['steps'] if s['step_id'] == 'map_sample_s1')
        target['dependencies'] = []
        h = Harness(tmp_path, Model([recorded_length(1), response(json.dumps(candidate)), response(complete_candidate())]))
        task = 'offline-length-then-semantic-correction'
        try:
            initial = h.bootstrap(task)
            h.watch_provider(task)
            result = await h.run(initial)
            final = load_plan_state(tmp_path, task)
            receipt('length-then-semantic', {'result': result, 'durable': final, 'requests': h.model.calls, 'provider_states': h.provider_states, 'typed_traces': h.typed_traces})
            assert len(h.model.calls) == 3
            failure = h.provider_states[2]['task_state']['planning_failure']
            assert failure['response_integrity']['finish_reason'] == 'stop'
            issues = failure['validation_issues']
            assert len(issues) == 2
            expected_producers = {'plan.reference_indexes': ['prepare_reference'], 'plan.sample_table': ['create_sample_table']}
            assert {i['resource_id']: i['producer_step_ids'] for i in issues} == expected_producers
            for issue in issues:
                assert issue['code'] == 'PLAN_ARTIFACT_UNAVAILABLE'
                assert issue['machine_evidence']['validator_id'] == 'biomind.plan_dataflow_compiler.v1'
                assert issue['machine_evidence']['consumer_step_id'] == 'map_sample_s1'
                assert issue['dependency_ancestors'] == []
                assert issue['accepted_sources'] == []
                assert issue['actual_facts']['source_analysis_status'] == 'complete_sources_rejected'
                assert all(row['reason'] == 'source_step_is_not_an_ancestor' for row in issue['rejected_sources'])
            assert not h.provider_states[2].get('steps')
            assert result['plan_review_outcome']['type'] == 'approval_pending'
            assert final['task_state']['planning_epoch']['planner_calls_total'] == 3
            assert length_calls(final) == 1
            assert_unapproved_unexecuted(final)
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('initiator', ['user', 'parent_task'])
def test_real_outer_cancellation_never_length_recovers_or_summarizes(tmp_path, initiator):
    """T21: external cancellation owner remains authoritative in a new epoch."""
    async def scenario():
        from tests.planner_fix_acceptance.test_planner_cancellation_workflow import PartialCancellationModel
        model = PartialCancellationModel(initiator)
        h = Harness(tmp_path, model)
        task = 'offline-truncation-cancel-' + initiator
        initial = h.bootstrap(task)
        model.agent, model.task_id = h.agent, task
        summary = SummaryModel()
        h.agent.terminal_summary_llm = summary
        h.agent.terminal_summary_policy = {'enabled': True}
        try:
            caller = asyncio.create_task(h.agent._run_agent_interrupt_loop(initial['task_spec']['goal'], task, resume_initial=True))
            model.cancel_target = caller
            with pytest.raises(asyncio.CancelledError):
                await caller
            state = load_plan_state(tmp_path, task)
            receipt('cancellation-' + initiator, {'durable': state, 'calls': model.calls, 'summary_calls': summary.calls, 'progress': h.progress_events})
            assert state['task_state']['cancellation_receipt']['initiator_kind'] == initiator
            assert summary.calls == []
            assert len(model.calls) == 1
            assert length_calls(state) == 0
            assert_unapproved_unexecuted(state)
        finally:
            await h.close()
    asyncio.run(scenario())


def test_normal_complete_plan_outer_waits_for_approval_without_failure_summary(tmp_path):
    """T21 HITL: no length correction or terminal failure explanation."""
    async def scenario():
        h = Harness(tmp_path, Model([response(complete_candidate())]))
        task = 'offline-normal-plan-hitl'
        initial = h.bootstrap(task)
        summary = SummaryModel()
        h.agent.terminal_summary_llm = summary
        h.agent.terminal_summary_policy = {'enabled': True}
        h.agent.state_manager = None
        try:
            result = await h.agent._run_agent_interrupt_loop(initial['task_spec']['goal'], task, resume_initial=True)
            state = load_plan_state(tmp_path, task)
            receipt('normal-plan-hitl', {'result': result.__dict__, 'durable': state, 'requests': h.model.calls, 'summary_requests': summary.calls})
            assert result.status == 'waiting_user', result
            assert len(h.model.calls) == 1 and summary.calls == []
            assert length_calls(state) == 0
            assert_unapproved_unexecuted(state)
        finally:
            await h.close()
    asyncio.run(scenario())


def test_response_received_then_process_loss_never_refunds_or_redispatches(tmp_path):
    """T15: model returned, durable correction write lost, fresh real outer stops."""
    async def scenario():
        from unittest.mock import patch
        from agent_core.planner_recovery import unsettled_call
        class SimulatedProcessLoss(BaseException):
            pass
        task = 'offline-length-result-persistence-loss'
        first = Harness(tmp_path, Model([recorded_length(1), recorded_length(2)]))
        try:
            initial, update, before = await first_rejection(first, task)
            actual_put = StateContractStore.put_object
            hits = []
            def fail(store, object_type, value):
                if object_type == 'planner_correction_context':
                    hits.append({'type': object_type, 'raw': value.get('raw_candidate')})
                    raise SimulatedProcessLoss('isolated loss after provider result before durable correction acceptance')
                return actual_put(store, object_type, value)
            with patch.object(StateContractStore, 'put_object', fail):
                adapter = TypedPlanner(data_path=str(tmp_path), source=first.agent._schema2_early_sources['planner'])
                with pytest.raises(SimulatedProcessLoss):
                    await adapter.run({**initial, **update})
            unknown = load_plan_state(tmp_path, task)
            assert hits and hits[0]['raw'] == raw_length(2)
            assert len(first.model.calls) == 2
            assert unsettled_call(unknown)
            assert unknown['task_state']['planning_epoch']['planner_calls_total'] == 2
            assert length_calls(unknown) == 1
            receipt('process-loss-before-reload', {'durable': unknown, 'requests': first.model.calls, 'write_fault_hits': hits})
        finally:
            await first.close()
        second = Harness(tmp_path, Model([]))
        second.agent.terminal_summary_policy = {'enabled': False}
        second.agent.state_manager = None
        try:
            result = await second.agent._run_agent_interrupt_loop(before['current_task']['task_spec']['goal'], task, resume_initial=True)
            after = load_plan_state(tmp_path, task)
            receipt('process-loss-after-reload', {'result': result.__dict__, 'durable': after, 'requests': second.model.calls})
            assert result.failure_code == 'PLANNER_CALL_RESULT_UNKNOWN'
            assert second.model.calls == []
            assert after['task_state']['planning_epoch']['planner_calls_total'] == 2
            assert length_calls(after) == 1
            assert_unapproved_unexecuted(after)
        finally:
            await second.close()
    asyncio.run(scenario())


def test_old_correction_context_does_not_inherit_current_provider_metadata(tmp_path):
    """T08 compatibility: legacy fixture is explicitly metadata-free, never guessed."""
    async def scenario():
        from agent_core.plan_tracker import save_plan_state
        from agent_core.planner_context import load_correction_context
        h = Harness(tmp_path, Model([recorded_length(1)]))
        task = 'offline-legacy-correction-context'
        try:
            _, _, state = await first_rejection(h, task)
            store = StateContractStore(tmp_path, task)
            failure = state['task_state']['planning_failure']
            source = store.read_object(failure['context_ref'], expected_type='planner_correction_context')
            legacy = deepcopy(source)
            legacy['schema_version'] = 1
            for key in ('response_integrity', 'occurrence_receipt', 'attempted_remediation', 'response_metadata'):
                legacy.pop(key, None)
            ref = store.put_object('planner_correction_context', legacy)
            state['task_state']['planning_failure']['context_ref'] = ref
            save_plan_state(tmp_path, task, state)
            current = load_plan_state(tmp_path, task)
            capabilities = h.manager.get_execution_capability_registry(current['task_state']['selected_skills'])
            loaded = load_correction_context(str(tmp_path), task, current,
                task_spec=current['current_task']['task_spec'], capabilities=capabilities)
            receipt('legacy-context', {'legacy_context': legacy, 'loaded': loaded, 'current_failure': current['task_state']['planning_failure']})
            assert loaded is not None
            assert loaded['raw_candidate'] == raw_length(1)
            assert loaded.get('response_integrity', {}).get('finish_reason', 'UNKNOWN') == 'UNKNOWN'
            assert loaded.get('response_integrity', {}).get('usage_source', 'UNKNOWN') == 'UNKNOWN'
        finally:
            await h.close()
    asyncio.run(scenario())


def test_original_syntax_cause_and_first_later_truncation_both_survive_common_stop(tmp_path):
    """The oldest cause is never rewritten just because length occurred later."""
    async def scenario():
        bad = '{"outcome_type":"SUBMIT_PLAN_FOR_REVIEW","steps":[ BAD ]}'
        rows = [response(bad), recorded_length(1), response(bad), response(bad + '\n')]
        record = await run_stop(tmp_path, responses=rows, evidence_case='syntax-length-syntax')
        assert len(record['provider_requests']) == 4
        assert record['result']['failure_code'] == 'PLANNER_REPEATED_FAILURE'
        failure = record['before_outer']['task_state']['planning_failure']
        assert failure['primary_failure']['response_integrity']['finish_reason'] == 'stop'
        assert any(x['code'] == 'PLANNER_ENVELOPE_JSON_INVALID' for x in failure['primary_failure']['validation_issues'])
        assert failure['response_integrity']['finish_reason'] == 'stop'
        assert length_calls(record['before_outer']) == 1
        assert len(record['summary_requests']) == 1
        facts = json.loads(record['summary_requests'][0][1]['content'])
        assert facts['response_integrity']['origin']['finish_reason'] == 'stop'
        assert facts['response_integrity']['latest']['finish_reason'] == 'stop'
        assert facts['first_truncation']['finish_reason'] == 'length'
        assert '首个截断' in record['result']['reason'] or 'length' in record['result']['reason']
        assert_unapproved_unexecuted(record['before_outer'])
    asyncio.run(scenario())


def test_same_real_terminal_reentered_through_outer_keeps_single_summary(tmp_path):
    """T12: same actual checkpoint terminal is consumed through outer a second time."""
    async def scenario():
        record = await run_stop(tmp_path, evidence_case='outer-idempotence', reenter_outer=True)
        reentry = record['outer_reentry']
        assert reentry['calls_before'] == {'planner': 2, 'summary': 1}
        assert reentry['calls_after'] == reentry['calls_before']
        assert reentry['result']['failure_code'] == 'PLANNER_LENGTH_RECOVERY_EXHAUSTED'
        before, after = reentry['before'], reentry['after']
        old_stop = before['task_state']['managed_run_stop']
        new_stop = after['task_state']['managed_run_stop']
        assert old_stop['stop_id'] == new_stop['stop_id']
        assert before['task_state']['planning_epoch']['planner_calls_total'] == after['task_state']['planning_epoch']['planner_calls_total'] == 2
        assert length_calls(before) == length_calls(after) == 1
        assert old_stop['summary_llm_calls'] == new_stop['summary_llm_calls'] == 1
        assert_unapproved_unexecuted(before)
        assert_unapproved_unexecuted(after)
    asyncio.run(scenario())
