"""Actual provider request checks, including public-example admission.

Fixed model replies and inherited offline external preconditions are declared
substitutes. Parser, admission, feedback, graph and review boundaries are real.
"""
import asyncio
from copy import deepcopy
import gzip
import json
import os
from pathlib import Path
import re
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from tests.a1_unified_fix.test_f6_graph import F6Graph
from tests.plan_authority_v2_20260915.test_graph import AuthorityGraph


def save(name, value):
    body = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()
    root = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    (root / ('v2-public-' + name + '.json.gz')).write_bytes(gzip.compress(body, mtime=0))


class CorrectionGraph(AuthorityGraph):
    def __init__(self):
        super().__init__('generic_split')
        self.expected_planner_calls = 2
        self.public_feedback_seen = None

    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'planner':
            return await super().provider(messages, stage=stage, **kwargs)
        self.counts[stage] += 1
        self.requests.append({'stage': stage, 'ordinal': self.counts[stage], 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        if self.counts[stage] == 1:
            # An explicit legacy negative fixture, not a conversion applied to
            # the succeeding v3 candidate. Preserve the actual old reader.
            candidate = F6Graph.plan(self)
            candidate['metadata']['resource_contract_version'] = 2
            candidate['proposed_artifacts'][0]['role_associations'][0]['step_id'] = 'deleted_public_step'
        else:
            assert self.counts[stage] == 2
            feedback = json.loads(next(m.content for m in reversed(messages)
                                      if getattr(m, 'name', None) == 'runtime_observation'))
            issue = feedback['validation_issues'][0]
            assert issue['code'] == 'PLAN_RESOURCE_ROLE_SCOPE_INVALID', issue
            assert issue['actual']['step_id'] == 'deleted_public_step'
            assert issue['machine_evidence']['actual']['step_id'] == 'deleted_public_step'
            assert issue['not_evaluated']
            assert set(feedback['requirements'][0]['legacy_resource_contract_v2']['role_association_item']) == {
                'step_id', 'capability_id', 'role'}
            def walk(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        assert key not in {'task_id', 'plan_id', 'approval_id', 'cas_token', 'attempt_id'}
                        walk(item)
                elif isinstance(value, list):
                    for item in value:
                        walk(item)
            walk(feedback)
            assert not self.executions and not self.approvals
            self.public_feedback_seen = deepcopy(feedback)
            candidate = self.plan()
            assert all('inputs' not in step for step in candidate['steps'])
            assert all('role_associations' not in row for row in candidate['proposed_artifacts'])
        raw = json.dumps(candidate, ensure_ascii=False)
        self.raw_plans.append(raw)
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})


def test_actual_next_request_keeps_legacy_bad_reference_then_executes_v3_plan():
    async def scenario():
        case = CorrectionGraph()
        await case.setup()
        try:
            await case.run()
            assert case.public_feedback_seen is not None
            save('correction-chain', {'feedback_in_actual_second_request': case.public_feedback_seen,
                 'requests': case.requests, 'raw_plans': case.raw_plans,
                 'validator_calls': case.validator_calls, 'counts': case.counts,
                 'commits': case.commits, 'executions': case.executions})
        finally:
            await case.close()
    asyncio.run(scenario())


class PublicExampleGraph(AuthorityGraph):
    async def setup(self):
        await super().setup()
        self.outputs = {}
        self.goal = ('Create one synthetic task-local text record and then inspect whether it can be read. '
                     'Use the public generic example for exactly these obligations. '
                     'No annotation, scientific operation, installation, host-resource discovery or external output is requested. '
                     'This test stops at the real human Plan review boundary.')
        self.example_raw = None
        self.input_patch.stop()
        import bio_agents
        from agent_core.human_io import HumanInputUnavailable
        def wait_for_human(*args, **kwargs):
            self.plan_review_inputs.append({'args': args, 'kwargs': kwargs, 'reply': 'UNAVAILABLE'})
            raise HumanInputUnavailable('No execution approval is supplied by this public-example admission test.')
        self.input_patch = patch.object(bio_agents, 'safe_input', wait_for_human)
        self.input_patch.start()

    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'planner':
            assert stage != 'generate', 'The example must stop at human Plan review.'
            return await super().provider(messages, stage=stage, **kwargs)
        self.counts[stage] += 1
        assert self.counts[stage] == 1
        self.requests.append({'stage': stage, 'ordinal': 1, 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        system = next(m.content for m in messages if m.type == 'system' and '<generic_plan_example' in m.content)
        self.example_raw = re.search(r'<generic_plan_example schema_version="1">\n(.*?)\n</generic_plan_example>', system, re.S).group(1)
        # The exact public example is returned. No added inputs, association,
        # artifact fields, capability or output values are supplied by the test.
        self.raw_plans.append(self.example_raw)
        return AIMessage(content=self.example_raw, response_metadata={'finish_reason': 'stop'})


def test_exact_example_from_actual_planner_request_reaches_real_review():
    async def scenario():
        case = PublicExampleGraph('public_example')
        await case.setup()
        try:
            from agent_core.plan_tracker import load_plan_state
            runner = case.case._runner_state(case.task)
            runner['workflow_routing_schema_version'] = 2
            runner['messages'] = [HumanMessage(content=case.goal)]
            app = case.agent._app_for_task(case.task, runner)
            config = {'configurable': {'thread_id': case.task}, 'recursion_limit': 60}
            async for event in app.astream(runner, config=config, stream_mode='updates'):
                case.events.append(event)
            case.values = app.get_state(config).values
            case.durable = load_plan_state(case.root, case.task)
            assert case.values.get('planner_outcome', {}).get('type') == 'plan_review_ready', case.values.get('planner_outcome')
            assert case.values.get('plan_review_outcome', {}).get('type') == 'approval_pending', case.values.get('plan_review_outcome')
            assert case.plan_review_inputs and case.example_raw
            assert case.counts['planner'] == 1 and case.counts['generate'] == 0
            assert not case.executions and not case.approvals and not case.commits
            assert case.durable['plan_compilation_receipt']['status'] == 'compiled'
            assert case.durable['plan_dataflow_receipt']['status'] == 'compiled'
            save('exact-example-request', {'requests': case.requests, 'exact_example_raw': case.example_raw,
                 'validator_calls': case.validator_calls, 'values': case.values, 'durable': case.durable,
                 'plan_review_inputs': case.plan_review_inputs,
                 'boundary': 'Exact public example admitted from actual request; human review pending, no execution approval or scientific result.'})
        finally:
            await case.close()
    asyncio.run(scenario())
