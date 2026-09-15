"""Reverse the old F6 fixed-name assumption at real Plan admission only.

Run only in this supplement's new guarded export. No original session, Plan
approval, Generate, scientific process or Commit is loaded or executed.
"""
import asyncio
from copy import deepcopy

import pytest

from tests.a1_unified_fix.test_f6_graph import OPS
from tests.plan_live_review_20260915.test_admission_replay import AdmissionReplay, store


def aliased_public_steps(candidate):
    value = deepcopy(candidate)
    aliases = {'prepare_database': 'runtime_healthcheck'}
    for step in value['steps']:
        original = step['step_id']
        step['step_id'] = aliases.get(original, original)
        step['dependencies'] = [aliases.get(key, key) for key in step['dependencies']]
    for artifact in value['proposed_artifacts']:
        for association in artifact.get('role_associations', []):
            association['step_id'] = aliases.get(association['step_id'], association['step_id'])
    return value


@pytest.mark.parametrize('variant', ['original_f6_names', 'public_step_id_differs_from_operation'])
def test_f6_reverse_variant_reaches_real_plan_review(variant):
    async def scenario():
        f = AdmissionReplay('f6_reverse_' + variant, [], context='f6')
        await f.setup()
        try:
            original = f.plan()
            candidate = original if variant == 'original_f6_names' else aliased_public_steps(original)
            f.replies = [candidate]
            assert [step['operation'] for step in candidate['steps']] == OPS
            assert candidate['steps'][1]['outputs'] == ['plan.config']
            assert candidate['steps'][2]['inputs'] == original['steps'][2]['inputs']
            assert candidate['steps'][3]['inputs'] == original['steps'][3]['inputs']
            assert candidate['proposed_artifacts'][0]['artifact_contract']['resource_kind'] == 'mutable_workspace'
            values, durable = await f.run_admission()
            store(f.mode + '-candidate-and-outcome', {
                'variant': variant, 'raw_candidate': candidate, 'original_f6_candidate': original,
                'planner_outcome': values.get('planner_outcome'),
                'plan_review_outcome': values.get('plan_review_outcome'),
                'compilation_receipt': durable.get('plan_compilation_receipt'),
                'dataflow_receipt': durable.get('plan_dataflow_receipt'),
                'compiled_steps': durable.get('steps'),
                'scope_limit': 'Formal PlanReview admission only; original F6 action() still assumes step_id equals operation.',
            })
            assert f.counts['planner'] == 1, f.rejections
            assert not f.rejections
            assert values['planner_outcome']['type'] == 'plan_review_ready'
            assert values['plan_review_outcome']['type'] == 'approval_pending'
            assert durable['plan_compilation_receipt']['status'] == 'compiled'
            assert durable['plan_dataflow_receipt']['status'] == 'compiled'
            assert len(durable['steps']) == 6
            if variant != 'original_f6_names':
                prepared = next(step for step in durable['steps'] if step['operation'] == 'prepare_database')
                assert prepared['step_id'] == 'runtime_healthcheck'
                build = next(step for step in durable['steps'] if step['operation'] == 'build_database')
                assert 'runtime_healthcheck' in build['dependencies']
            assert f.review_prompts, 'The actual PlanReview reader must reach the unavailable-input boundary'
            assert f.counts['generate'] == 0 and not f.executions and not f.commits
        finally:
            await f.close()
    asyncio.run(scenario())
