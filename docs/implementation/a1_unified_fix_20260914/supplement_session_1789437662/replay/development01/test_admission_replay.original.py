"""Read-only supplemental Plan replay; fixed external replies, real admission.

Only the guarded exporter may run this module. Historical business replies are
data; no historical state is loaded and no Plan is approved or executed.
"""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from tests.a1_unified_fix.test_f6_graph import F6Graph, SID

FIXTURES = Path(__file__).parent / 'fixtures'


def raw_round(n):
    return (FIXTURES / f'live_round_{n}.txt').read_text()


def live_minimum():
    value = json.loads(raw_round(3))
    value['proposed_artifacts'][0]['role_associations'][0]['step_id'] = 'runtime_healthcheck'
    return value


def store(name, value):
    root = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    body = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()
    path = root / ('plan-review-' + name + '.json.gz')
    path.write_bytes(gzip.compress(body, mtime=0))
    return {'path': str(path), 'raw_sha256': hashlib.sha256(body).hexdigest()}


class AdmissionReplay(F6Graph):
    def __init__(self, name, replies, *, context='live'):
        super().__init__('plan-review-' + name)
        self.replies = list(replies)
        self.context = context
        self.validator_calls = []
        self.rejections = []
        self.raw_deliveries = []
        self.review_prompts = []

    async def setup(self):
        await super().setup()
        self.public = json.loads((FIXTURES / 'live_public_context.json').read_text())
        self.output_root = self.root / '隔离声明' / 'Part2.vairant_annotation' / 'medium'
        if self.context == 'live':
            # Preserve the scientific obligations as planning text. All input
            # paths and source authority are newly established by the handoff.
            self.goal = '\n'.join([
                self.public['task_spec']['goal'],
                self.public['consultation_handoff']['summary'],
                *[x['text'] for x in self.public['task_spec']['highlighted_constraints']],
                'SUPPLEMENTAL OFFLINE PLAN ADMISSION ONLY. The following paths are new synthetic declarations; scientific execution and installation are forbidden in this replay.',
                'variant_vcf=' + str(self.vcf), 'reference_fasta=' + str(self.reference),
                'annotation_gtf=' + str(self.annotation), 'output_dir=' + str(self.output_root),
            ])
        from agent_core.human_io import HumanInputUnavailable
        def no_approval(*args, **kwargs):
            self.review_prompts.append({'args': args, 'kwargs': kwargs})
            raise HumanInputUnavailable('New isolated Plan is awaiting human approval; this review authorizes no execution.')
        self.agent._prompt_user = no_approval
        actual = self.agent._planner_observed_call
        def observed(tid, observation, phase, operation, *args, **kwargs):
            if phase not in ('validate_wire_and_bind', 'normalize_handles', 'compile_resource_bindings',
                    'compile_step_effects', 'compile_dataflow', 'validate_candidate',
                    'compile_step_effects_final', 'compile_dataflow_final', 'normalize_plan_contracts'):
                return actual(tid, observation, phase, operation, *args, **kwargs)
            row = {'ordinal': self.counts['planner'], 'phase': phase,
                   'args': deepcopy(args), 'kwargs': deepcopy(kwargs)}
            self.validator_calls.append(row)
            try:
                result = actual(tid, observation, phase, operation, *args, **kwargs)
            except Exception as error:
                row['exception'] = {'type': type(error).__name__, 'message': str(error)}
                raise
            row['result'] = deepcopy(result)
            if phase == 'validate_wire_and_bind':
                from agent_core.plan_bindings import capability_workspace_roles
                candidate = args[0]
                steps = {x['step_id']: x for x in candidate['steps']}
                predicates = []
                for parent in candidate.get('proposed_artifacts', []):
                    for association in parent.get('role_associations', []):
                        step = steps.get(association['step_id'], {})
                        cap = kwargs['capability_registry'].get(association['capability_id'], {})
                        roles = capability_workspace_roles(cap)
                        kind = parent.get('artifact_contract', {}).get('resource_kind', 'immutable_artifact')
                        predicates.append({'parent_local_ref': parent['local_ref'], 'parent_resource_kind': kind,
                            'association': association, 'lookup_found': bool(step), 'lookup_step': step,
                            'selected_skill_id': cap.get('selected_skill_id'), 'roles': roles,
                            'failed_conditions': {
                                'step_capability_mismatch': step.get('execution_capability_id') != association['capability_id'],
                                'selected_skill_not_in_step': cap.get('selected_skill_id') not in (step.get('skill_ids') or []),
                                'unknown_role': association['role'] not in roles,
                                'resource_kind_mismatch': association['role'] in roles and roles[association['role']]['resource_kind'] != kind}})
                row['exact_role_predicates_from_actual_arguments'] = predicates
            return result
        self.agent._planner_observed_call = observed
        progress = self.agent._planner_progress
        def rejected(tid, event, payload):
            result = progress(tid, event, payload)
            if event == 'candidate_rejected':
                from agent_core.plan_tracker import load_plan_state
                self.rejections.append(deepcopy(load_plan_state(self.root, self.task)))
            return result
        self.agent._planner_progress = rejected

    async def provider(self, messages, *, stage, **kwargs):
        assert stage in ('consultant', 'planner'), 'No Generate or scientific boundary permitted'
        if stage == 'consultant' and self.context == 'f6':
            return await super().provider(messages, stage=stage, **kwargs)
        self.counts[stage] += 1
        self.requests.append({'stage': stage, 'ordinal': self.counts[stage], 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        if stage == 'consultant':
            operations = [{'operation': 'set_goal', 'value': self.goal, 'supporting_evidence': ['U1']}]
            for role, path in [('variant_vcf', self.vcf), ('reference_fasta', self.reference), ('annotation_gtf', self.annotation)]:
                operations.append({'operation': 'upsert_input', 'input': {'role': role, 'path': str(path), 'required': True}, 'supporting_evidence': ['U1']})
            for constraint in self.public['task_spec']['highlighted_constraints']:
                operations.append({'operation': 'upsert_constraint', 'constraint': {'value': constraint['text'],
                    'source_text': constraint['text'], 'strength': 'required'}, 'supporting_evidence': ['U1']})
            for declared in self.public['task_spec']['outputs']:
                output = {k: deepcopy(declared[k]) for k in ('output_id', 'role', 'artifact_kind', 'format', 'required', 'path_policy', 'root_ref') if k in declared}
                if output['output_id'] == 'output_dir':
                    output['canonical_path'] = str(self.output_root)
                operations.append({'operation': 'upsert_output_contract', 'output_contract': output, 'supporting_evidence': ['U1']})
            value = {'action_type': 'submit_planning_handoff', 'summary': self.public['consultation_handoff']['summary'],
                'selected_skills': [SID], 'task_spec_operations': operations, 'unresolved_ambiguities': [], 'planner_feedback': None}
            raw = json.dumps(value, ensure_ascii=False)
        else:
            index = self.counts[stage] - 1
            if index < len(self.replies):
                value = self.replies[index]
                raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            else:
                assert index == len(self.replies), 'Unexpected extra model call'
                from agent_core.plan_contract import planner_stop_wire_example
                value = planner_stop_wire_example()
                value['diagnostic']['stop_reason'] = 'Fixed offline provider has no further supplied candidate; this is a formal non-success stop.'
                raw = json.dumps(value, ensure_ascii=False)
            self.raw_deliveries.append(raw)
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})

    async def run_admission(self):
        from agent_core.plan_tracker import load_plan_state
        runner = self.case._runner_state(self.task)
        runner['workflow_routing_schema_version'] = 2
        runner['messages'] = [HumanMessage(content=self.goal)]
        async def pulse():
            while True:
                await asyncio.sleep(.001)
        self.service = asyncio.create_task(pulse())
        app = self.agent._app_for_task(self.task, runner)
        config = {'configurable': {'thread_id': self.task}, 'recursion_limit': 60}
        async for event in app.astream(runner, config=config, stream_mode='updates'):
            self.events.append(event)
        self.values = app.get_state(config).values
        self.durable = load_plan_state(self.root, self.task)
        assert not self.durable.get('execution_attempts')
        assert self.durable.get('approval_status') != 'approved'
        assert self.counts['generate'] == self.counts['reviewer'] == self.counts['recovery'] == 0
        assert not self.executions and not self.commits and not self.approvals
        return self.values, self.durable

    async def close(self):
        store(self.mode, {'context': self.context, 'goal': self.goal, 'counts': self.counts, 'requests': self.requests,
            'raw_deliveries': self.raw_deliveries, 'validator_calls': self.validator_calls, 'rejections': self.rejections,
            'events': self.events, 'values': getattr(self, 'values', {}), 'durable': getattr(self, 'durable', {}),
            'review_prompts': self.review_prompts, 'receptions': self.receptions,
            'external_substitutes': ['fixed model leaf', 'new input/path declarations', 'unavailable human approval reader',
                'inherited Consultant external preflight observer; no ready database seeded'],
            'not_run': ['Generate', 'code approval', 'Execute', 'Commit', 'scientific tools', 'real model/API']})
        await self.agent.aclose()
        if hasattr(self, 'service'):
            self.service.cancel()
            try:
                await self.service
            except asyncio.CancelledError:
                pass
        await self.case.asyncTearDown()
        self.prepare_patch.stop()
        self.compaction_patch.stop()


def test_live_three_candidates_and_formal_stop():
    async def scenario():
        f = AdmissionReplay('live_sequence', [raw_round(i) for i in range(1, 5)])
        await f.setup()
        try:
            values, durable = await f.run_admission()
            assert f.counts['planner'] == 4
            assert f.raw_deliveries == [raw_round(i) for i in range(1, 5)]
            errors = [[x['code'] for x in d['task_state']['planning_failure']['validation_issues']] for d in f.rejections]
            assert errors == [['PLAN_RESOURCE_ROLE_READ_MISSING'], ['ACTION_CAPABILITY_SCOPE_MISSING',
                'ACTION_CAPABILITY_SCOPE_MISSING', 'PLAN_STEP_ARTIFACT_EFFECT_UNBOUND'], ['PLAN_RESOURCE_ROLE_SCOPE_INVALID']], errors
            assert values['planner_outcome']['type'] == 'planning_stopped', values.get('planner_outcome')
            assert not durable.get('steps')
        finally:
            await f.close()
    asyncio.run(scenario())


def test_minimum_one_field_full_admission():
    async def scenario():
        candidate = live_minimum()
        original = json.loads(raw_round(3))
        restored = deepcopy(candidate)
        restored['proposed_artifacts'][0]['role_associations'][0]['step_id'] = 'prepare_database'
        assert restored == original, 'Exactly one public reference changed; no obligations or other fields changed'
        f = AdmissionReplay('minimum', [candidate])
        await f.setup()
        try:
            values, durable = await f.run_admission()
            assert f.counts['planner'] == 1, f.rejections
            assert values['planner_outcome']['type'] == 'plan_review_ready', values.get('planner_outcome')
            assert values['plan_review_outcome']['type'] == 'approval_pending', values.get('plan_review_outcome')
            assert durable['plan_compilation_receipt']['status'] == 'compiled'
            assert durable['plan_dataflow_receipt']['status'] == 'compiled'
            assert len(durable['steps']) == 6
        finally:
            await f.close()
    asyncio.run(scenario())
