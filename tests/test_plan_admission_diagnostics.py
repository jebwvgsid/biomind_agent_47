"""Independent Plan errors must reach one correction without partial admission."""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from agent_core.action_runtime import generic_execution_capability
from agent_core.plan_contract import (
    PlannerCandidateError,
    candidate_artifact_ref_keys,
    validate_planner_candidate_deterministically,
)
from agent_core.plan_tracker import record_planning_failure


def resource(ref, role=None):
    return {
        'local_ref': ref,
        'semantic_view': {'role': role or ref.removeprefix('plan.'), 'format': 'text', 'description': 'A declared result'},
        'artifact_contract': {'object_kind': 'file', 'lifecycle': 'plan_local',
                              'materialization_policy_id': 'managed_intermediate_v1'},
    }


def question(*, wrong_binding=False):
    step = {
        'step_id': 'confirm', 'title': 'Confirm the user-owned parameter', 'action': 'ask_user',
        'purpose': 'user_input', 'tool': 'ask_user', 'inputs': [], 'outputs': [],
        'dependencies': [], 'success_criteria': ['The user explicitly confirms the parameter'],
        'skill_ids': [], 'workflow_stage_ids': [], 'batch_id': '', 'risk_level': 'low',
    }
    if wrong_binding:
        step.update(execution_capability_id='framework.generic.execute', operation='execute')
    return step


def candidate():
    return {
        'steps': [question(wrong_binding=True)],
        'proposed_artifacts': [resource('plan.primary'), resource('plan.summary')],
        'validation_checklist': [],
        'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'},
    }


class PlanAdmissionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.handles = {'input.source': 'artifact-source', 'output.primary': 'artifact-primary',
                        'output.summary': 'artifact-summary'}
        self.bindings = {handle: {
            'object_type': 'artifact', 'stable_identity': identity,
            'direction': handle.split('.')[0], 'resource_kind': 'immutable_artifact',
            'metadata': {'role': handle.split('.')[1]},
        } for handle, identity in self.handles.items()}
        generic = generic_execution_capability()
        self.scope = {
            'candidate_scope_id': 'candidate-scope-test', 'authoritative_handles': self.handles,
            'authoritative_bindings': self.bindings,
            'availability_origins': {'artifact-source': 'verified_task_input'},
            'capability_registry': {generic['capability_id']: generic},
            'validate_dataflow': False,
        }

    def test_output_input_conflicts_and_action_mismatch_are_returned_together(self):
        plan = candidate()
        plan['proposed_artifacts'].append(resource('plan.source'))
        original = deepcopy(plan)
        with patch('agent_core.plan_bindings.compile_plan_resource_bindings',
                   side_effect=AssertionError('invalid identities must not be compiled')):
            keys, usage, issues = validate_planner_candidate_deterministically(plan, **self.scope)
        self.assertEqual(keys, {})
        self.assertEqual(usage, [])
        self.assertEqual(plan, original)
        self.assertEqual([i['code'] for i in issues], [
            'TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL', 'TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL',
            'TASK_INPUT_SHADOWED_BY_PLAN_LOCAL', 'STEP_EFFECT_PROTOCOL_INVALID',
        ])
        self.assertEqual(issues[-1]['violation_code'], 'PLAN_ACTION_CAPABILITY_MISMATCH')
        self.assertEqual(issues[-1]['field_path'], '$.steps[0].execution_capability_id')
        durable = record_planning_failure(
            {'task_state': {}, 'steps': []}, validation_issues=issues,
            planning_epoch_id='epoch-test', task_spec_revision_value=1,
            validation_source='planner_candidate_admission', candidate=plan,
        )
        saved = durable['task_state']['planning_failure']['validation_issues']
        self.assertEqual(len(saved), 4)
        outputs = [i['expected_identity']['handle'] for i in saved if i['code'] == 'TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL']
        self.assertEqual(set(outputs), {'output.primary', 'output.summary'})

    def test_bad_declarations_do_not_create_cascading_unknown_reference_errors(self):
        plan = candidate()
        plan['steps'][0]['inputs'] = ['plan.primary', 'plan.summary', 'plan.unknown_a', 'plan.unknown_b']
        _, _, issues = validate_planner_candidate_deterministically(plan, **self.scope)
        unknown = [i['observed_identity']['handle'] for i in issues if i['code'] == 'ARTIFACT_REFERENCE_UNKNOWN']
        self.assertEqual(unknown, ['plan.unknown_a', 'plan.unknown_b'])
        self.assertFalse(any(i['code'] in {'PLAN_ARTIFACT_UNAVAILABLE', 'PLAN_ARTIFACT_PRODUCER_CARDINALITY'} for i in issues))

    def test_duplicates_are_rejected_and_strict_resolver_still_raises(self):
        plan = candidate()
        plan['proposed_artifacts'] = [resource('plan.intermediate'), resource('plan.intermediate'), resource('plan.primary')]
        plan['steps'][0]['inputs'] = ['plan.intermediate']
        _, _, issues = validate_planner_candidate_deterministically(plan, **self.scope)
        self.assertEqual([i['code'] for i in issues], [
            'LOCAL_REF_DUPLICATE', 'TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL', 'STEP_EFFECT_PROTOCOL_INVALID',
        ])
        with self.assertRaises(PlannerCandidateError) as raised:
            candidate_artifact_ref_keys(plan, candidate_scope_id='scope', authoritative_handles=self.handles,
                                        authoritative_bindings=self.bindings)
        self.assertEqual(raised.exception.code, 'LOCAL_REF_DUPLICATE')

    def test_corrected_identity_and_builtin_question_pass_but_dataflow_stays_strict(self):
        plan = candidate()
        plan['proposed_artifacts'] = [resource('plan.intermediate')]
        plan['steps'] = [question(), {
            **question(), 'step_id': 'produce', 'title': 'Produce the declared outputs',
            'action': 'execute', 'purpose': 'analysis', 'tool': 'bash',
            'execution_capability_id': 'framework.generic.execute', 'operation': 'execute',
            'inputs': ['input.source'], 'outputs': ['plan.intermediate', 'output.primary', 'output.summary'],
            'dependencies': ['confirm'],
        }]
        _, _, issues = validate_planner_candidate_deterministically(plan, **self.scope)
        self.assertEqual(issues, [])
        # Identity validity alone must not turn an unproduced intermediate into available input.
        plan['steps'][1]['inputs'].append('plan.intermediate')
        plan['steps'][1]['outputs'].remove('plan.intermediate')
        _, _, issues = validate_planner_candidate_deterministically(plan, **{**self.scope, 'validate_dataflow': True})
        self.assertIn('PLAN_ARTIFACT_UNAVAILABLE', [i['code'] for i in issues])

    def test_malformed_wire_stops_before_identity_and_action_traversal(self):
        plan = candidate()
        plan['steps'] = 'not an array'
        with patch('agent_core.step_effects.independent_step_effect_diagnostics',
                   side_effect=AssertionError('unsafe wire must not reach independent checks')):
            _, _, issues = validate_planner_candidate_deterministically(plan, **self.scope)
        self.assertTrue(issues)
        self.assertTrue(all(i['code'] == 'PLAN_STRUCTURE_INVALID' for i in issues))


if __name__ == '__main__':
    unittest.main()
