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
def test_v05_nested_execute_validation_in_first_provider_request(tmp_path, code):
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
        # The first actual model request includes the producer rejection.
        assert code in json.dumps(wire), wire
        receipt(code, {'status': 'FIXED_AND_VERIFIED', 'producer': 'TypedExecute',
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
            assert 'WORKSPACE_MUTATION_NOT_ATTESTED' in json.dumps(requests[0]['messages'])
            failed = next(e['execute']['execute_outcome'] for e in reversed(f.events)
                          if 'execute' in e and e['execute'].get('execute_outcome', {}).get('type') == 'execution_failed')
            assert failed['payload']['return_code'] == 0
            taskroot = f.root / 'task_outputs' / f.task
            mutation = json.loads((taskroot / 'attempts' / f.executions[-1]['attempt_id'] / 'mutation_intent.json').read_text())
            assert 'postcondition_unsatisfied' in json.dumps(mutation)
            receipt('real-graph-rc0', {'status': 'FIXED_WITH_REAL_ATTESTER',
                'routes': [next(iter(e)) for e in f.events], 'counts': f.counts,
                'execute_calls': len(f.executions), 'commits': len(f.commits),
                'failed_execute': failed, 'mutation_intent': mutation,
                'first_recovery_request': requests[0], 'production_boundaries_mocked': [],
                'external_substitutes': ['provider leaf', 'synthetic runtime observation', 'new synthetic process inputs']})
        finally:
            await f.close()
    asyncio.run(scenario())


def test_v07_v08_rejection_index_priority_unknown_and_omitted_ranges():
    from agent_core.workflow.recovery_projection import _producer_failure_index, _diagnostic_facts
    errors = ["UNKNOWN_FIRST_91"] + ["MISSING_" + str(i) for i in range(40)]
    payload = {"return_code": 0, "status": "exited", "execution_record": {
        "attempt_id": "a", "validation": {"passed": False, "errors": errors},
        "stdout": "irrelevant background" * 100000}}
    index = _producer_failure_index(payload)
    assert index["checks"][0]["code"] == "UNKNOWN_FIRST_91"
    assert index["total_rejecting_checks"] == 41
    assert index["unexpanded_index_range"] == [12, 41]
    assert all(row["observation_status"] == "unknown" for row in index["checks"])
    facts = _diagnostic_facts("execute_outcome", {"payload": payload})
    assert facts[0]["kind"] == "producer_failure_index"
    assert "irrelevant background" not in json.dumps(facts, default=dict)


@pytest.mark.parametrize('kind',['validator_exception','commit_cas','install_failure','preexecution','diagnostic'])
def test_v06_formal_producer_to_first_request(tmp_path,kind):
    async def scenario():
        from tests.test_inspection_workflow import InspectionReviewer
        from agent_core.workflow.early_nodes import TypedInstallSubagent
        from agent_core.workflow.verifier import TypedSupervisor
        from supervisor import SupervisorNode
        from agent_core.plan_contract import StateCASConflict
        f=make_inspection_fixture(tmp_path)
        field='verification_outcome'
        if kind=='validator_exception':
            verifier=SupervisorNode(f.reviewer,output_dir=str(tmp_path/'task_outputs'))
            node=bind_causal_failure_node(TypedSupervisor(data_path=str(tmp_path),verifier=verifier.run).run,
                binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field=field)
            with patch('supervisor.RuleEngine.run',side_effect=ValueError('SYNTHETIC_VALIDATOR_FAULT_73')):
                update=await node(deepcopy(f.state))
            expected='SYNTHETIC_VALIDATOR_FAULT_73'
            assert update[field]['type']=='verification_error'
        elif kind=='commit_cas':
            f.reviewer=InspectionReviewer(accept_first=True)
            accepted=await f.new_supervisor()(deepcopy(f.state));f.state.update(accepted)
            assert accepted[field]['type']=='verified'
            field='commit_step_outcome'
            async def reject_cas(state):
                with patch('agent_core.workflow.commit_step.save_plan_state',side_effect=StateCASConflict('new competing writer')):
                    return await CommitStep(data_path=str(tmp_path)).run(state)
            node=bind_causal_failure_node(reject_cas,binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field=field)
            update=await node(deepcopy(f.state));expected='expected_state_generation'
            assert update[field]['type']=='commit_conflict'
        elif kind=='install_failure':
            field='install_subagent_outcome'
            async def health(_state):return {'runtime_status':{'synthetic':{'ok':False,'error_code':'HEALTH_FAIL_73','message':'bounded fixture health receipt'}}}
            node=bind_causal_failure_node(TypedInstallSubagent(data_path=str(tmp_path),source=health).run,
                binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field=field)
            update=await node(deepcopy(f.state));expected='HEALTH_FAIL_73'
            assert update[field]['type']=='installation_failed'
        else:
            field='execute_outcome'
            async def execution_leaf(_state):
                if kind=='preexecution':return {'pre_execution_rejection':{'violation_owner':'VERIFICATION','violation_code':'DEPENDENCY_NOT_ATTESTED_73'}}
                return {'execution_evidence':{**f.execution,'command_role':'diagnostic','returncode':7}}
            node=bind_causal_failure_node(TypedExecute(executor=execution_leaf,data_path=str(tmp_path)).run,
                binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field=field)
            update=await node(deepcopy(f.state));expected='DEPENDENCY_NOT_ATTESTED_73' if kind=='preexecution' else 'diagnostic_completed'
        result,wire=await capture(f,update)
        assert expected in json.dumps(wire),wire
        receipt('e-owner-'+kind,{'producer_outcome':update[field],'first_request':wire,'model_calls':1,'new_execute_calls':0})
    asyncio.run(scenario())


@pytest.mark.parametrize('kind',['rejected','correction_required','assessment_pending','incomplete_evidence'])
def test_formal_supervisor_variants_and_alias_first_wire(tmp_path,kind):
    async def scenario():
        from agent_core.workflow.verifier import TypedSupervisor
        from tests.test_inspection_workflow import InspectionReviewer
        f=make_inspection_fixture(tmp_path,execution_returncode=7 if kind=='rejected' else 0)
        if kind in {'correction_required','assessment_pending'}:
            class Reviewer(InspectionReviewer):
                async def ainvoke(self,messages,**kwargs):
                    reply=await super().ainvoke(messages,**kwargs);value=json.loads(reply.content)
                    value.update(assessment='CONCERN' if kind=='correction_required' else 'UNKNOWN',disposition=kind)
                    value['issue_dispositions'][0]['disposition']=kind
                    value['concerns']=['FORMAL_REVIEW_73: bounded fixture concern']
                    return AIMessage(content=json.dumps(value))
            f.reviewer=Reviewer()
        if kind=='incomplete_evidence':
            # Invalid *input* revalidation claim is rejected by the real owner;
            # no outcome or stored approval/acceptance is prefilled.
            f.state['recovery_outcome']={'type':'revalidation_required','payload':{'decision_id':'never-admitted'}}
        update=await f.new_supervisor()(deepcopy(f.state))
        assert update['verification_outcome']['type']==kind,update
        assert route_verification_outcome(update)=='recovery'
        result,wire=await capture(f,update)
        expected='REVALIDATION_ORIGINAL_SCOPE_MISMATCH' if kind=='incomplete_evidence' else 'FORMAL_REVIEW_73' if kind!='rejected' else 'return_code'
        assert expected in json.dumps(wire),wire
        # A gate alias is only a source name; it does not rerun either owner.
        receipt('e-supervisor-'+kind,{'source':update,'first_provider_messages':wire,
            'review_calls':len(f.reviewer.requests),'model_calls':1,'new_execute_calls':0,'final_consumer':result})
    asyncio.run(scenario())


def test_formal_triage_resume_keeps_causal_first_request(tmp_path):
    async def scenario():
        from agent_core.workflow.early_nodes import TypedTriage
        from agent_core.workflow.outcomes import ResumeState
        from agent_core.workflow.runtime_graph import route_triage_outcome
        f=make_inspection_fixture(tmp_path);await f.inspect_handoff()
        async def unused(_state):raise AssertionError('resume must not rerun source')
        resume=ResumeState(schema_version=1,type='recovery_pending',summary='existing cause',
            scope={'task_id':f.task_id},payload={'reason_code':'current_causal_failure'}).to_dict()
        state={**f.state,'resume_state':resume}
        update=await TypedTriage(data_path=str(tmp_path),source=unused).run(state)
        assert route_triage_outcome(update)=='recovery'
        result,wire=await capture(f,update)
        assert 'method requirement' in json.dumps(wire)
        receipt('e-triage-resume',{'triage':update,'first_provider_messages':wire,'new_execute_calls':0,'result':result})
    asyncio.run(scenario())


def test_v07_long_background_and_multiple_errors_in_actual_first_wire(tmp_path):
    async def scenario():
        f=make_inspection_fixture(tmp_path)
        errors=['UNREGISTERED_REJECTION_73']+['FAIL_'+str(i) for i in reversed(range(40))]
        async def source(_state):return {'execution_evidence':{**deepcopy(f.execution),
            'stdout':'successful background line\n'*15000,'validation':{'passed':False,'errors':errors}}}
        owner=bind_causal_failure_node(TypedExecute(executor=source,data_path=str(tmp_path)).run,
            binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field='execute_outcome')
        update=await owner(deepcopy(f.state));result,wire=await capture(f,update)
        text=json.dumps(wire)
        assert 'UNREGISTERED_REJECTION_73' in text and 'unexpanded_index_range' in text
        assert text.index('UNREGISTERED_REJECTION_73')<text.index('successful background line')
        receipt('e-long-first-wire',{'first_request':wire,'raw_error_count':41,'raw_stdout_chars':len('successful background line\n')*15000,
            'source_field':'/execution_record/validation/errors','observed':'unknown','result':result})
    asyncio.run(scenario())


def test_formal_diagnostic_validation_receipt_consumer(tmp_path):
    async def scenario():
        from agent_core.recovery import ValidationEvidence
        from agent_core.workflow.verifier import TypedSupervisor
        f=make_inspection_fixture(tmp_path)
        execution={**deepcopy(f.execution),'action_id':'synthetic-diagnostic-action',
            'attempt_id':'synthetic-diagnostic-attempt','command_role':'diagnostic','returncode':7}
        f.state['execution_evidence']=execution
        value=ValidationEvidence.from_mapping({'task_id':f.task_id,'step_id':f.reload()['current_step_id'],
            'action_id':execution['action_id'],'attempt_id':execution['attempt_id'],
            'status':'INVALID','command_role':'diagnostic','errors':['DIAGNOSTIC_RECEIPT_FAILURE_73'],
            'returncode_check':{'passed':False,'returncode':7}}).to_dict()
        async def validation_leaf(_state):return {'validation_evidence':value}
        owner=bind_causal_failure_node(TypedSupervisor(data_path=str(tmp_path),verifier=validation_leaf).run,
            binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field='verification_outcome')
        update=await owner(deepcopy(f.state))
        assert update['verification_outcome']['type']=='diagnostic_evidence_recorded'
        result,wire=await capture(f,update)
        assert 'DIAGNOSTIC_RECEIPT_FAILURE_73' in json.dumps(wire)
        assert not f.reload()['task_state'].get('typed_step_commit_receipts')
        receipt('e-diagnostic-consumer',{'level':'formal adapter consuming isolated ValidationEvidence; no external worker deployed',
            'source':update,'first_request':wire,'result':result,'new_execute_calls':0,'commits':0})
    asyncio.run(scenario())
