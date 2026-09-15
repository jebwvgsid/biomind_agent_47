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
import hashlib
import sys
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
        if stage == 'consultant':
            self.counts[stage] += 1
            self.requests.append({'stage': stage, 'ordinal': self.counts[stage], 'messages': [
                {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
            return AIMessage(content=json.dumps({'action_type': 'submit_planning_handoff',
                'summary': self.goal, 'selected_skills': [],
                'task_spec_operations': [{'operation': 'set_goal', 'value': self.goal, 'supporting_evidence': ['U1']}],
                'unresolved_ambiguities': [], 'planner_feedback': None}), response_metadata={'finish_reason': 'stop'})
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


async def run_admission(case):
    from agent_core.plan_tracker import load_plan_state
    runner = case.case._runner_state(case.task)
    runner['workflow_routing_schema_version'] = 2
    runner['messages'] = [HumanMessage(content=case.goal)]
    async def pulse():
        while True:
            await asyncio.sleep(.001)
    case.service = asyncio.create_task(pulse())
    app = case.agent._app_for_task(case.task, runner)
    config = {'configurable': {'thread_id': case.task}, 'recursion_limit': 60}
    async for event in app.astream(runner, config=config, stream_mode='updates'):
        case.events.append(event)
    case.values = app.get_state(config).values
    case.durable = load_plan_state(case.root, case.task)
    assert case.counts['generate'] == 0
    assert not case.executions and not case.approvals and not case.commits


def test_exact_example_from_actual_planner_request_reaches_real_review():
    async def scenario():
        case = PublicExampleGraph('public_example')
        await case.setup()
        try:
            await run_admission(case)
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


class FourthReplyGraph(PublicExampleGraph):
    def __init__(self, stop):
        super().__init__('fourth_stop' if stop else 'fourth_valid')
        self.stop = stop
        self.budgets = []

    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'planner':
            return await super().provider(messages, stage=stage, **kwargs)
        self.counts[stage] += 1
        ordinal = self.counts[stage]
        assert ordinal <= 4, 'A fifth Planner request is forbidden'
        self.requests.append({'stage': stage, 'ordinal': ordinal, 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        system = next(m.content for m in messages if m.type == 'system' and '<generic_plan_example' in m.content)
        self.example_raw = re.search(r'<generic_plan_example schema_version="1">\n(.*?)\n</generic_plan_example>', system, re.S).group(1)
        budget_message = next(m.content for m in reversed(messages)
                              if m.type == 'system' and 'requests_remaining_after_this_reply' in m.content)
        self.budgets.append(budget_message)
        if ordinal == 3:
            feedback = json.loads(next(m.content for m in reversed(messages)
                                      if getattr(m, 'name', None) == 'runtime_observation'))
            unknown = next(row for row in feedback['validation_issues'] if row['code'] == 'UNKNOWN_EXECUTION_CAPABILITY')
            assert unknown['received_id'] == 'framework.unregistered.operation'
            assert 'framework.generic.execute' in unknown['available_capability_ids']
        value = json.loads(self.example_raw)
        value['steps'][0]['step_id'] = 'write_instance_572'
        value['steps'][1]['step_id'] = 'inspect_instance_819'
        value['steps'][1]['dependencies'] = ['write_instance_572']
        # Three actual complete business candidates make successive structural
        # corrections. Admission produces every failure and next-request feedback.
        if ordinal == 3:
            value['steps'][1]['dependencies'] = ['undeclared_dependency_602']
        if ordinal <= 2:
            value['steps'][0]['execution_capability_id'] = 'framework.unregistered.operation'
        if ordinal == 1:
            value['steps'][1]['resource_uses'][0]['resource_ref'] = 'plan.undeclared_record'
        if ordinal == 4 and self.stop:
            from agent_core.plan_contract import planner_stop_wire_example
            value = planner_stop_wire_example()
            feedback = json.loads(next(m.content for m in reversed(messages)
                                      if getattr(m, 'name', None) == 'runtime_observation'))
            value['diagnostic'].update(
                error_codes=[row['code'] for row in feedback['validation_issues']],
                attempted_changes=['Corrected the explicit resource reference.', 'Selected the registered generic capability.'],
                stop_reason='The fixed model declines another candidate with the remaining dependency unresolved.',
                uncertainties=['No execution or scientific result has been established.'],
                unperformed_work=['Create synthetic record', 'Read synthetic record'])
        raw = json.dumps(value, ensure_ascii=False)
        self.raw_plans.append(raw)
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})


def _fourth_reply(stop):
    async def scenario():
        case = FourthReplyGraph(stop)
        await case.setup()
        try:
            await run_admission(case)
            assert case.counts['planner'] == 4
            assert len(case.raw_plans) == len(case.budgets) == 4
            assert '"requests_remaining_after_this_reply": 0' in case.budgets[-1]
            if stop:
                assert case.values['planner_outcome']['type'] == 'terminal', case.values['planner_outcome']
                assert case.values['planner_outcome']['payload']['error_code'] == 'PLANNER_MODEL_STOPPED'
                assert not case.plan_review_inputs
                assert not case.durable.get('steps')
            else:
                assert case.values['planner_outcome']['type'] == 'plan_review_ready', case.values['planner_outcome']
                assert case.values['plan_review_outcome']['type'] == 'approval_pending'
                assert case.plan_review_inputs
                assert case.durable['plan_compilation_receipt']['status'] == 'compiled'
                assert case.durable['plan_dataflow_receipt']['status'] == 'compiled'
            save('fourth-stop' if stop else 'fourth-valid', {
                'requests': case.requests, 'raw_plans': case.raw_plans, 'budgets': case.budgets,
                'validator_calls': case.validator_calls, 'counts': case.counts, 'events': case.events,
                'values': case.values, 'durable': case.durable,
                'boundary': 'Four real Planner dispatches; no fifth, approval or execution; stop is non-success.'})
        finally:
            await case.close()
    asyncio.run(scenario())


def test_fourth_valid_complete_candidate_is_admitted_without_fifth_request():
    _fourth_reply(False)


def test_fourth_formal_stop_is_non_success_without_fifth_request():
    _fourth_reply(True)


class EmptySkillExecutionGraph(PublicExampleGraph):
    async def setup(self):
        await super().setup()
        self.goal = self.goal.replace('This test stops at the real human Plan review boundary.',
            'Complete both operations through human review and the ordinary execution chain.')
        self.input_patch.stop()
        import bio_agents
        def approve(*args, **kwargs):
            self.plan_review_inputs.append({'args': args, 'kwargs': kwargs, 'reply': 'y'})
            return 'y'
        self.input_patch = patch.object(bio_agents, 'safe_input', approve)
        self.input_patch.start()
        self.record_path = None

    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'generate':
            return await super().provider(messages, stage=stage, **kwargs)
        from agent_core.plan_bindings import step_resource_inputs
        self.counts[stage] += 1
        assert self.counts[stage] <= 2
        durable, frozen = self.current_step(approved=True)
        assert frozen['skill_ids'] == []
        assert frozen['execution_capability_id'] == 'framework.generic.execute'
        assert frozen['operation'] == 'execute' and frozen['tool'] == 'python'
        self.requests.append({'stage': stage, 'ordinal': self.counts[stage], 'step_id': frozen['step_id'],
            'messages': [{'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        template = json.loads(next(m.content for m in messages if getattr(m, 'name', '') == 'approved_effect_emission_template'))
        protocol = json.loads(next(m.content for m in messages if getattr(m, 'name', '') == 'current_generate_output_protocol'))
        assert protocol['mode'] == 'script_block_v1'
        source = template['marker'] + '\nfrom pathlib import Path\n'
        facts = {'produces_artifacts': {}, 'produces_observations': {}}
        if frozen['outputs']:
            paths = self.artifact_paths(durable, frozen, template)
            assert len(paths) == 1
            identity, filename = next(iter(paths.items()))
            assert identity in str(messages) and filename in str(messages)
            self.record_path = Path(filename)
            assert self.record_path.is_relative_to(self.root)
            source += 'record = Path(' + repr(filename) + ')\nrecord.parent.mkdir(parents=True, exist_ok=True)\n'
            source += 'record.write_text("SYNTHETIC RECORD\\n")\n'
            facts['produces_artifacts'][identity] = {'path': filename}
        else:
            reads = step_resource_inputs(frozen)
            assert len(reads) == 1
            filename = durable['plan_artifacts'][reads[0]]['canonical_path']
            assert reads[0] in str(messages) and filename in str(messages)
            assert Path(filename) == self.record_path
            source += 'value = Path(' + repr(filename) + ').read_text() == "SYNTHETIC RECORD\\n"\n'
            rows = template['declarations']['produces_observations']
            assert len(rows) == 1
            facts['produces_observations'][rows[0]['identity']] = {'value': '__COMPUTED_VALUE__'}
        literal = repr(facts).replace("'__COMPUTED_VALUE__'", 'value')
        source += 'print(emit_effect_evidence(' + literal + '))\n'
        raw = '<python>\n' + source + '</python>'
        self.provider_candidates.append({'step_id': frozen['step_id'], 'published_mode': protocol['mode'], 'raw_response': raw})
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})

    async def execute(self, path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
        from execution.executor import run_script_path
        path = Path(path)
        approved = path.with_name('approved.sh').read_bytes()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
        assert hashlib.sha256(approved).hexdigest() == self.approvals[-1]['sha256']
        assert path.is_relative_to(self.root)
        clean = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
        boot = sys.modules['__main__']
        boot.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
        boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
        try:
            out, err, rc = await run_script_path(str(path), expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False, env=clean,
                managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            boot.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
            boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
        _, frozen = self.current_step(approved=True)
        self.executions.append({'step_id': frozen['step_id'], 'operation': frozen['operation'],
            'execution_capability_id': frozen['execution_capability_id'], 'attempt_id': path.parent.name,
            'rc': rc, 'stdout': out, 'stderr': err, 'approved_code': approved.decode(),
            'wrapper': path.read_text(), 'approved_sha256': hashlib.sha256(approved).hexdigest(),
            'executed_sha256': expected_sha256})
        return out, err, rc


def test_actual_public_example_empty_skills_executes_two_python_steps_and_commits():
    async def scenario():
        case = EmptySkillExecutionGraph('empty_skill_execute')
        await case.setup()
        try:
            import bio_agents
            from agent_core.plan_tracker import load_plan_state
            runner = case.case._runner_state(case.task)
            runner['workflow_routing_schema_version'] = 2
            runner['messages'] = [HumanMessage(content=case.goal)]
            app = case.agent._app_for_task(case.task, runner)
            config = {'configurable': {'thread_id': case.task}, 'recursion_limit': 90}
            async def pulse():
                while True:
                    await asyncio.sleep(.001)
            case.service = asyncio.create_task(pulse())
            incoming = runner
            with patch.object(bio_agents, 'run_script_path', case.execute):
                for _ in range(8):
                    async for event in app.astream(incoming, config=config, stream_mode='updates'):
                        case.events.append(event)
                        if 'commit_step' in event:
                            case.commits.append(deepcopy(event['commit_step']))
                            case.state_after_commits.append(deepcopy(load_plan_state(case.root, case.task)))
                    snapshot = app.get_state(config)
                    if not snapshot.next:
                        break
                    assert snapshot.tasks and snapshot.tasks[0].interrupts
                    incoming = bio_agents.Command(resume='y')
            case.values = app.get_state(config).values
            case.durable = load_plan_state(case.root, case.task)
            case.terminal = await case.agent._finalize_managed_terminal(case.task, app.get_state(config))
            assert case.values.get('final_audit_outcome', {}).get('type') == 'task_verified', case.values
            assert case.counts['consultant'] == case.counts['planner'] == 1
            assert case.counts['generate'] == 2 and case.counts['recovery'] == 0
            assert len(case.executions) == len(case.approvals) == len(case.commits) == 2
            assert all(x['rc'] == 0 for x in case.executions)
            assert all(x['skill_ids'] == [] for x in case.durable['steps'])
            assert case.record_path.read_text() == 'SYNTHETIC RECORD\n'
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON' in case.executions[1]['stdout']
            assert not case.durable.get('runtime_evidence_state', {}).get('observations')
            save('empty-skills-complete', {'counts': case.counts, 'requests': case.requests,
                'exact_example_raw': case.example_raw, 'raw_plans': case.raw_plans,
                'validator_calls': case.validator_calls, 'executions': case.executions,
                'approvals': case.approvals, 'commits': case.commits,
                'values': case.values, 'durable': case.durable, 'terminal': case.terminal,
                'scope': 'Fixed model writes and reads one new synthetic record. No domain capability or external science-ready evidence is supplied; all formal approvals/execution/evidence/Commit/FinalAudit are real.'})
        finally:
            await case.close()
    asyncio.run(scenario())
