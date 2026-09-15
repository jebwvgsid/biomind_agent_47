"""Bounded legal/illegal variants through the same current Plan admission."""
import asyncio
from copy import deepcopy
import json

import pytest

from tests.plan_live_review_20260915.test_admission_replay import AdmissionReplay, FIXTURES, live_minimum, store


def variant_candidate(name):
    candidate = live_minimum()
    if name == 'distinct_step_ids':
        aliases = {step['step_id']: 'stage_' + str(i + 1) for i, step in enumerate(candidate['steps'])}
        for step in candidate['steps']:
            step['step_id'] = aliases[step['step_id']]
            step['dependencies'] = [aliases[x] for x in step['dependencies']]
        for artifact in candidate['proposed_artifacts']:
            for association in artifact.get('role_associations', []):
                association['step_id'] = aliases[association['step_id']]
    elif name == 'additional_supported_read':
        extra = deepcopy(candidate['steps'][0])
        extra['step_id'] = 'independent_read_check'
        extra['title'] = 'Another explicitly supported read-only input and contig check'
        candidate['steps'][0]['dependencies'] = [extra['step_id']]
        candidate['steps'].insert(0, extra)
    elif name in ('current_input_alias', 'stale_candidate_scope'):
        alias = {'local_ref': 'plan.exact_reference_alias', 'semantic_view': {'role': 'reference_fasta', 'format': 'fasta',
            'description': 'Exact current input reference; no new resource availability asserted'},
            'artifact_contract': {'object_kind': 'file', 'resource_kind': 'immutable_artifact', 'lifecycle': 'plan_local',
                'materialization_policy_id': 'managed_intermediate_v1'},
            'resource_binding': {'resource_ref': 'input.reference_fasta'}}
        if name == 'stale_candidate_scope':
            alias['resource_binding']['candidate_scope_id'] = 'previous-isolated-candidate-scope'
        candidate['proposed_artifacts'].append(alias)
    elif name == 'missing_read_role':
        candidate['proposed_artifacts'][0]['role_associations'].pop(2)
    elif name == 'wrong_existing_step':
        candidate['proposed_artifacts'][0]['role_associations'][0]['step_id'] = 'build_database'
    elif name == 'wrong_parent_resource_kind':
        candidate['proposed_artifacts'][0]['artifact_contract']['resource_kind'] = 'immutable_artifact'
    elif name == 'unknown_capability':
        candidate['steps'][0]['execution_capability_id'] = 'genomics.snpeff_annotation.not_a_declared_capability'
    elif name == 'extra_access_field':
        candidate['proposed_artifacts'][0]['role_associations'][0]['access'] = 'mutate'
    elif name == 'missing_config_effect':
        candidate['steps'][1]['outputs'] = []
    elif name == 'f6_live_output_context':
        candidate = json.loads((FIXTURES / 'f6_passed_candidate.json').read_text())
        # Explicit context transport, not a silent candidate correction: F6
        # originally used five exact outputs; live declares materialized children.
        candidate['task_output_materializations'] = deepcopy(live_minimum()['task_output_materializations'])
        candidate['metadata']['result_contract_version'] = 1
    else:
        raise AssertionError(name)
    return candidate


CASES = [
    ('distinct_step_ids', None),
    ('additional_supported_read', None),
    ('current_input_alias', None),
    ('f6_live_output_context', None),
    ('missing_read_role', 'PLAN_RESOURCE_ROLE_ASSOCIATION_MISSING'),
    ('wrong_existing_step', 'PLAN_RESOURCE_ROLE_SCOPE_INVALID'),
    ('wrong_parent_resource_kind', 'PLAN_RESOURCE_ROLE_SCOPE_INVALID'),
    ('unknown_capability', 'UNKNOWN_EXECUTION_CAPABILITY'),
    ('extra_access_field', 'PLAN_RESOURCE_ROLE_INVALID'),
    ('stale_candidate_scope', 'PLAN_RESOURCE_REFERENCE_STALE_SCOPE'),
    ('missing_config_effect', 'PLAN_STEP_ARTIFACT_EFFECT_UNBOUND'),
]


@pytest.mark.parametrize('name,expected_code', CASES, ids=[x[0] for x in CASES])
def test_current_admission_variant(name, expected_code):
    async def scenario():
        candidate = variant_candidate(name)
        f = AdmissionReplay('variant_' + name, [candidate])
        await f.setup()
        try:
            values, durable = await f.run_admission()
            codes = [[x['code'] for x in d['task_state']['planning_failure']['validation_issues']] for d in f.rejections]
            store('variant-' + name, {'candidate': candidate, 'expected_code': expected_code, 'actual_codes': codes,
                'planner_outcome': values.get('planner_outcome'), 'plan_review_outcome': values.get('plan_review_outcome'),
                'compilation_receipt': durable.get('plan_compilation_receipt'), 'dataflow_receipt': durable.get('plan_dataflow_receipt')})
            if expected_code is None:
                assert f.counts['planner'] == 1 and not codes, codes
                assert values['planner_outcome']['type'] == 'plan_review_ready'
                assert values['plan_review_outcome']['type'] == 'approval_pending', values.get('plan_review_outcome')
                assert durable['plan_compilation_receipt']['status'] == 'compiled'
                assert durable['plan_dataflow_receipt']['status'] == 'compiled'
            else:
                assert f.counts['planner'] == 2 and len(codes) == 1, codes
                assert expected_code in codes[0], codes
                assert values['planner_outcome']['type'] == 'terminal'
                assert not durable.get('steps')
        finally:
            await f.close()
    asyncio.run(scenario())
