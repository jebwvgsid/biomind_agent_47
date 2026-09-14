"""Exact env repair progress: real parser/ledger and fresh formal Plan graph.

The provider is fixed; the graph test ends at action admission. No execution,
historical session, science input or authoritative synthetic success is used.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from bio_agents import BioAnalystAgent
from agent_core.workflow.candidate_repair import (
    candidate_progress_assessment, candidate_repair_fingerprint,
)
from tests.a1_unified_fix.test_f3_progress import action, row
from tests.test_preflight_recovery_audit import save


def assess_env(env, prior=()):
    candidate = action(env=env)
    parsed = BioAnalystAgent._extract_structured_action_result(json.dumps(candidate))
    assert not parsed.succeeded
    assert parsed.error['code'] == 'ACTION_SCHEMA_TYPE_ERROR'
    assessment = candidate_progress_assessment(
        candidate=parsed.candidate, representation_targets=parsed.validated_targets,
        binding_facts=None, violations=[parsed.error], prior_rows=prior, contract_ref={})
    return candidate, parsed, assessment


def test_env_key_reordering_is_recurrence_without_false_resolution():
    witnesses = []
    for first_env, next_env, next_field in [
        ({'BAD': 7, 'VALID': 'ok'}, {'VALID': 'ok', 'BAD': 7}, 'env.BAD'),
        ({'BAD': 7, 'VALID': 'ok', 'NEXT': 8},
         {'NEXT': 8, 'VALID': 'ok', 'BAD': 7}, 'env.NEXT'),
    ]:
        first, parsed_first, initial = assess_env(first_env)
        candidate, parsed, actual = assess_env(next_env, [row(first, initial, 1)])
        witnesses.append({'first_error': parsed_first.error, 'next_error': parsed.error,
            'first_targets': list(parsed_first.validated_targets),
            'next_targets': list(parsed.validated_targets), 'assessment': actual,
            'same_full_fingerprint': candidate_repair_fingerprint(first) == candidate_repair_fingerprint(candidate)})
    save('f3-env-key-reordering', witnesses)
    for witness in witnesses:
        assert witness['same_full_fingerprint']
        assert witness['first_error']['field_path'] == 'env.BAD'
        assert witness['next_error']['field_path'] in ('env.BAD', 'env.NEXT')
        assert witness['first_targets'] == witness['next_targets']
        assert not witness['assessment']['progress_proven']
        assert witness['assessment']['basis'] == 'recurrent_rejected_candidate'
        assert witness['assessment']['resolved_targets'] == []


def test_unrelated_valid_env_key_does_not_repair_pending_field():
    first, _, initial = assess_env({'BAD': 7})
    _, parsed, actual = assess_env({'NEW': 'ok', 'BAD': 7}, [row(first, initial, 1)])
    save('f3-env-unrelated-valid-key', {'error': parsed.error, 'assessment': actual})
    assert parsed.error['field_path'] == 'env.BAD'
    assert 'execute.env.NEW' in actual['representation_targets']
    assert not actual['progress_proven']
    assert actual['basis'] == 'no_validated_target_progress'
    assert actual['newly_satisfied_targets'] == []
    assert actual['resolved_targets'] == []


def test_exact_previous_env_value_repair_allows_next_pending_value():
    witnesses = []
    for first_env, next_env, first_field, gained in [
        ({'BAD': 7, 'NEXT': 8}, {'BAD': 'fixed', 'NEXT': 8}, 'env.BAD', ['execute.env.BAD']),
        ({'BAD': 7, 'NEXT': 8, 'OPTIONAL': 'ok'}, {'BAD': 'fixed', 'NEXT': 8},
         'env.BAD', ['execute.env.BAD']),
        ({'BAD': 7, 'NEXT': 8}, {'NEXT': 8}, 'env.BAD', ['removed_optional_field:env.BAD']),
        ({'': 'ok', 'NEXT': 8}, {'VALID': 'ok', 'NEXT': 8}, 'env', ['execute.env_keys']),
    ]:
        first, parsed_first, initial = assess_env(first_env)
        _, parsed, actual = assess_env(next_env, [row(first, initial, 1)])
        witnesses.append({'first_error': parsed_first.error,
            'next_error': parsed.error, 'expected_first_field': first_field,
            'expected_gained': gained,
            'assessment': actual})
    save('f3-env-exact-value-repair', witnesses)
    for witness in witnesses:
        actual = witness['assessment']
        assert witness['first_error']['field_path'] == witness['expected_first_field']
        assert witness['next_error']['field_path'] == 'env.NEXT'
        assert actual['progress_proven']
        assert actual['basis'] == 'validated_representation_target'
        assert actual['newly_satisfied_targets'] == witness['expected_gained']
        assert actual['resolved_targets'] == [witness['expected_first_field']]


def test_existing_progress_row_cannot_buy_retry_from_new_diagnostic_checkpoint():
    candidate, parsed, _ = assess_env({'BAD': 7})
    contract = {'plan_revision': 1}
    old_assessment = candidate_progress_assessment(
        candidate=candidate,
        representation_targets=[target for target in parsed.validated_targets if target != 'execute.env_keys'],
        binding_facts=None, violations=[parsed.error], prior_rows=[], contract_ref=contract)
    # Model the actual pre-checkpoint diagnostic schema only. The real rejected
    # candidate, previous pending field, contract and counters stay unchanged.
    old_assessment.pop('comparison_context_fingerprint', None)
    previous = {**row(candidate, old_assessment, 1), 'contract_ref': contract}
    unchanged_before = deepcopy(previous)
    actual = candidate_progress_assessment(
        candidate=candidate, representation_targets=parsed.validated_targets,
        binding_facts=None, violations=[parsed.error], prior_rows=[previous], contract_ref=contract)
    save('f3-env-existing-progress-row', {'old_row': previous, 'assessment': actual,
        'old_row_unchanged': previous == unchanged_before})
    assert previous == unchanged_before
    assert not actual['progress_proven']
    assert actual['basis'] == 'recurrent_rejected_candidate'


async def last_chance_graph():
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol, generate_until_ready_or_stopped
    from agent_core.plan_tracker import load_plan_state

    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    case.SKILL_ID = 'genomics/snpeff-annotation'
    await case.asyncSetUp()
    task = 'f3-env-exact-last-chance'
    try:
        configure_protocol(case)
        case._configure_one_step_no_output_plan()
        original = case._planner_payload
        def plan():
            payload = original()
            payload['steps'][0].update(
                execution_capability_id='genomics.snpeff_annotation.check_contig_compatibility',
                operation='check_contig_compatibility', produces_observations=[{
                    'identity': 'contigs_compatible', 'evidence_contract': {'required_fields': ['value']}}])
            return payload
        case._planner_payload = plan
        missing = deepcopy(action()); missing.pop('action_type')
        responses = ['{"unclosed":', json.dumps(missing),
            json.dumps(action(env={'BAD': 7, 'NEXT': 8, 'OPTIONAL': 'ok'})),
            json.dumps(action(env={'BAD': 'fixed', 'NEXT': 8})),
            json.dumps(action(env={'BAD': 'fixed', 'NEXT': 'fixed'}))]
        def provider(payload, ordinal):
            assert ordinal <= len(responses)
            return responses[ordinal - 1]
        events, requests, _ = await generate_until_ready_or_stopped(case, task, provider)
        durable = load_plan_state(case.root, task)
        rows = sorted(durable['task_state']['candidate_repair_state']['events'].values(),
            key=lambda item: item['repair_ordinal'])
        outcomes = [update['generate_outcome'] for event in events for update in event.values()
            if isinstance(update, dict) and update.get('generate_outcome')]
        result = {'provider_calls': len(requests), 'rows': rows, 'outcomes': outcomes,
            'pending_action': durable['task_state'].get('pending_action'),
            'last_request_has_pending_env_next': 'env.NEXT' in requests[-1]['_provider_request'],
            'last_request_has_resolved_env_bad': 'env.BAD' in requests[-1]['_provider_request'],
            'loaded_sources': {name: hashlib.sha256(Path(__import__(name, fromlist=['']).__file__).read_bytes()).hexdigest()
                for name in ('bio_agents', 'agent_core.workflow.candidate_repair')},
            'scope': 'real fresh Plan/approval/TypedGenerate/parser/CAS/shared recovery ledger; fixed provider; no Execute'}
        save(task, result)
        assert len(requests) == 5
        assert len(rows) == 4
        assert all(item['progress_proven'] for item in rows)
        assert rows[-1]['policy_receipt']['remaining'] == 0
        assert rows[-1]['progress_assessment']['newly_satisfied_targets'] == ['execute.env.BAD']
        assert rows[-1]['progress_assessment']['resolved_targets'] == ['env.BAD']
        assert rows[-1]['progress_assessment']['pending_targets'] == ['env.NEXT']
        assert outcomes[-1]['type'] == 'action_ready'
        assert result['pending_action']['env'] == {'BAD': 'fixed', 'NEXT': 'fixed'}
        assert result['last_request_has_pending_env_next'] and result['last_request_has_resolved_env_bad']
    finally:
        await case.asyncTearDown()


def test_fully_legal_env_is_admitted_on_last_allowed_response():
    asyncio.run(last_chance_graph())
