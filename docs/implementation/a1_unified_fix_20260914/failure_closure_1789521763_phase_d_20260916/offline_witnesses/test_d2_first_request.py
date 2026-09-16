"""Current-behavior witnesses; an observed omission is DEFECT_PRESENT, not FIX PASS.

Run only through run_witnesses.py and the existing A1 guarded child.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from agent_core.plan_tracker import load_plan_state, commit_runtime_evidence_state
from agent_core.runtime_evidence import make_runtime_evidence
from agent_core.workflow.execution import TypedExecute
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.runtime_graph import route_execute_outcome, route_commit_step_outcome
from agent_core.workflow.runtime_graph import route_verification_outcome
from agent_core.workflow.typed_node import profile_typed_node
from agent_core.workflow.outcomes import VerificationOutcome
from agent_core.workflow.verifier import verification_harness_error
from tests.test_inspection_workflow import make_inspection_fixture, _tag_json
from tests.a1_unified_fix.test_f6_graph import F6Graph, SID


def receipt(name, value):
    target = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('d2-' + name + '.json')
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


class CaptureBlocked:
    def __init__(self):
        self.requests = []

    async def ainvoke(self, messages, **kwargs):
        self.requests.append(list(messages))
        seed = _tag_json(list(messages), 'recovery_seed')
        scope = seed['invocation_scope']
        return AIMessage(content='', tool_calls=[{
            'name': 'submit_recovery_directive', 'id': 'phase-d-stop', 'args': {
                'schema_version': 1, 'directive_id': 'phase-d-observed-stop',
                'task_id': scope['task_id'], 'target_step_id': scope['step_id'],
                'action': 'blocked', 'summary': 'Offline observation completed.',
                'reason_code': 'OFFLINE_WITNESS_COMPLETE',
                'evidence_refs': [seed['failure_packet']['source_outcome_ref']],
            }}])


async def capture(fixture, update):
    state = {**deepcopy(fixture.state), **deepcopy(update)}
    model = CaptureBlocked()
    result = await Schema2RecoveryNode(data_path=str(fixture.root), model=model).run(state)
    assert len(model.requests) == 1, result
    wire = [{'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content}
            for m in model.requests[0]]
    return result, wire


@pytest.mark.parametrize('code', ['WORKSPACE_MUTATION_NOT_ATTESTED', 'UNRECOGNIZED_VALIDATOR_CODE_93'])
def test_nested_execute_validation_is_absent_from_first_provider_request(tmp_path, code):
    async def scenario():
        f = make_inspection_fixture(tmp_path)
        execution = {**deepcopy(f.execution), 'returncode': 0,
                     'validation': {'passed': False, 'errors': [code + ': synthetic expected file missing']}}
        async def saved_leaf(state):
            return {'execution_evidence': execution}
        node = bind_causal_failure_node(TypedExecute(executor=saved_leaf, data_path=str(tmp_path)).run,
            binder=CausalFailurePacketBinder(data_path=str(tmp_path)), outcome_field='execute_outcome')
        update = await node(deepcopy(f.state))
        assert update['execute_outcome']['type'] == 'execution_failed'
        assert route_execute_outcome(update) == 'recovery'
        result, wire = await capture(f, update)
        # Current failure witness: desired C2 behavior would invert this assertion.
        assert code not in json.dumps(wire), wire
        receipt(code, {'status': 'DEFECT_PRESENT', 'producer': 'TypedExecute',
            'leaf': 'synthetic already-recorded execution; does not claim attester integration',
            'source_errors': execution['validation']['errors'], 'first_provider_messages': wire,
            'recovery_outcome': result['recovery_outcome'], 'model_calls': 1, 'new_execute_calls': 0})
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['concern', 'missing_attempt', 'malformed_effect', 'commit_rejection'])
def test_real_other_producers_to_first_provider_messages(tmp_path, kind):
    async def scenario():
        options = {'create_attempt': False} if kind == 'missing_attempt' else (
            {'recorded_stdout': 'BIOMIND_EFFECT_EVIDENCE_JSON={broken\n'} if kind == 'malformed_effect' else {})
        f = make_inspection_fixture(tmp_path, **options)
        if kind == 'commit_rejection':
            node = bind_causal_failure_node(CommitStep(data_path=str(tmp_path)).run,
                binder=CausalFailurePacketBinder(data_path=str(tmp_path)), outcome_field='commit_step_outcome')
            update = await node(deepcopy(f.state))
            assert route_commit_step_outcome(update) == 'recovery'
            source = update['commit_step_outcome']
        else:
            handler = profile_typed_node('supervisor', f.new_supervisor(),
                outcome_field='verification_outcome', outcome_contract=VerificationOutcome,
                profiler=None, harness_error_factory=verification_harness_error)
            update = await handler(deepcopy(f.state))
            source = update['verification_outcome']
            if kind == 'missing_attempt':
                # Current no-attempt producer reports an incomplete scope;
                # the real profiling boundary owns framework failure -> END.
                assert source['type'] == 'harness_error', source
                assert route_verification_outcome(update) == '__end__'
                receipt(kind, {'status': 'FRAMEWORK_END_WITNESS', 'producer_outcome': source,
                    'first_provider_messages': [], 'model_calls': 0, 'execute_calls': 0,
                    'reason': 'No attempt -> validator scope mismatch -> profiled harness_error; not Recovery'})
                return
        result, wire = await capture(f, update)
        assert result['recovery_outcome']['type'] == 'blocked', result
        receipt(kind, {'status': 'CURRENT_PRODUCER_WITNESS', 'producer_outcome': source,
            'first_provider_messages': wire, 'model_calls': 1, 'execute_calls': 0,
            'review_calls': len(f.reviewer.requests), 'commits': len(f.reload()['task_state'].get('typed_step_commit_receipts', {}))})
    asyncio.run(scenario())


class MissingPostconditionGraph(F6Graph):
    def __init__(self):
        super().__init__('phase_d_missing_postcondition')

    def action(self, operation, messages):
        action = super().action(operation, messages)
        if operation == 'prepare_database':
            # Safe model-generated synthetic script computes its observation,
            # then omits one required artifact. Actual process and attester run.
            body = action['argv'][-1]
            needle = 'print(emit_effect_evidence('
            assert needle in body
            action['argv'][-1] = body.replace(needle, '(db/"data"/genome/"genes.gtf").unlink()\n' + needle)
        return action


def test_real_graph_rc0_postcondition_failure_enters_recovery_before_supervisor():
    async def scenario():
        import bio_agents
        f = MissingPostconditionGraph()
        await f.setup()
        try:
            runner = f.case._runner_state(f.task)
            runner.update(workflow_routing_schema_version=2, messages=[HumanMessage(content=f.goal)])
            scope = f.agent._runtime_evidence_scope(SID)
            evidence = make_runtime_evidence({'skill_id': SID, 'checked_env': scope['environment_id'], 'status': 'ready',
                'resolved_runtime_paths': {'RESOLVED_JAVA_BIN': str(f.java), 'RESOLVED_SNPEFF_JAR': str(f.jar)}},
                task_id=f.task, skill_scope=scope, observed_at=datetime.now(timezone.utc).isoformat(),
                source_ref='explicit-new-synthetic-observation')
            commit_runtime_evidence_state(f.root, f.task, [evidence])
            app = f.agent._app_for_task(f.task, runner)
            config = {'configurable': {'thread_id': f.task}, 'recursion_limit': 60}
            incoming = runner
            async def service_callbacks():
                # Same scoped event-loop service used by original F6 harness.
                while True:
                    await asyncio.sleep(.001)
            f.service = asyncio.create_task(service_callbacks())
            with patch.object(bio_agents, 'run_script_path', f.execute):
                for _ in range(8):
                    async for event in app.astream(incoming, config=config, stream_mode='updates'):
                        f.events.append(event)
                        if 'commit_step' in event:
                            f.commits.append(deepcopy(event['commit_step']))
                    snap = app.get_state(config)
                    if not snap.next:
                        break
                    assert snap.tasks and snap.tasks[0].interrupts
                    incoming = bio_agents.Command(resume='y')
                else:
                    raise AssertionError('OFFLINE_GRAPH_DID_NOT_TERMINATE')
            f.values, f.durable = app.get_state(config).values, load_plan_state(f.root, f.task)
            assert [e['operation'] for e in f.executions] == ['check_contig_compatibility', 'prepare_database']
            assert [e['rc'] for e in f.executions] == [0, 0]
            assert len(f.commits) == 1
            assert f.counts['reviewer'] == 1, 'prepare must fail before its Supervisor review'
            requests = [r for r in f.requests if r['stage'] == 'recovery']
            assert len(requests) == 1
            assert 'WORKSPACE_MUTATION_NOT_ATTESTED' not in json.dumps(requests[0]['messages'])
            failed = next(e['execute']['execute_outcome'] for e in reversed(f.events)
                          if 'execute' in e and e['execute'].get('execute_outcome', {}).get('type') == 'execution_failed')
            assert failed['payload']['return_code'] == 0
            taskroot = f.root / 'task_outputs' / f.task
            mutation = json.loads((taskroot / 'attempts' / f.executions[-1]['attempt_id'] / 'mutation_intent.json').read_text())
            assert 'postcondition_unsatisfied' in json.dumps(mutation)
            receipt('real-graph-rc0', {'status': 'DEFECT_PRESENT_WITH_REAL_ATTESTER',
                'routes': [next(iter(e)) for e in f.events], 'counts': f.counts,
                'execute_calls': len(f.executions), 'commits': len(f.commits),
                'failed_execute': failed, 'mutation_intent': mutation,
                'first_recovery_request': requests[0], 'production_boundaries_mocked': [],
                'external_substitutes': ['provider leaf', 'synthetic runtime observation', 'new synthetic process inputs']})
        finally:
            await f.close()
    asyncio.run(scenario())
