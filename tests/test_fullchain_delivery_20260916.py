"""Real PlanTracker/CAS, Recovery reads, Supervisor request and Commit.

External Reviewer and Recovery responses and the science command leaf are
controlled. Files and provenance are real and isolated; no original session.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import (approve_plan_state, build_plan_state, load_plan_state,
    mark_step_verification_pending, record_provenance_event, save_plan_state)
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.step_effects import canonical_step_effect
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.verifier import TypedSupervisor
from agent_core.reproduction import accepted_execution_materials, validate_package_sources
from supervisor import SupervisorNode
from tests.test_inspection_workflow import _tag_json
from tests.test_reliability_evidence_publication import _reopen
from tests.test_fullchain_repair_20260916 import VCF, SUMMARY, summary


def setup(root):
    task_id = 'fullchain-delivery'
    out = root / 'task_outputs' / task_id; out.mkdir(parents=True)
    vcf = root / 'input.vcf'; vcf.write_text(VCF)
    paths = {role: out / name for role, name in [('ann_summary', 'ann_summary.tsv'),
        ('report', 'report.md'), ('reproduce_script', 'reproduce.sh'), ('stage_status', 'stage_status.tsv')]}
    task = create_task_spec('Summarize ANN and deliver a report with independently reproducible tiny fixture.',
        task_id=task_id, base_dir=root, project_root=root, structured_source={
            'input_files': [{'role': 'variant_vcf', 'path': str(vcf), 'format': 'vcf'}],
            'expected_outputs': [{'role': role, 'path': str(path)} for role, path in paths.items()]})
    common = {'action': 'execute', 'purpose': 'analysis', 'tool': 'bash', 'skill_ids': [],
        'workflow_stage_ids': [], 'batch_id': '', 'risk_level': 'low', 'produces_observations': []}
    first = {**common, 'step_id': 'step.summary', 'title': 'Summarize ANN', 'inputs': ['input.variant_vcf'],
        'outputs': ['output.ann_summary'], 'dependencies': [], 'operation': 'summarize_annotations',
        'execution_capability_id': 'fixture.summary', 'success_criteria': ['Produce the three-section ANN summary.']}
    final = {**common, 'step_id': 'step.finalize', 'title': 'Deliver reproducible outputs',
        'inputs': ['output.ann_summary'], 'outputs': ['output.report', 'output.reproduce_script', 'output.stage_status'],
        'dependencies': ['step.summary'], 'operation': 'finalize_outputs', 'execution_capability_id': 'framework.generic.execute',
        'success_criteria': ['Read and validate the three-section summary; include config and helper in the reproduction scope.']}
    plan = build_plan_state(task_id, json.dumps({'steps': [first, final], 'validation_checklist': [],
        'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}), strict=True)
    for step in plan['steps']: step['step_effect'] = canonical_step_effect(step)
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
        current_task={'task_spec': task, 'task_revision': task_spec_revision(task)})
    plan['task_state']['workflow_routing_schema_version'] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source='isolated_fixture_user_approval')
    save_plan_state(root, task_id, plan)
    return task_id, paths


def record_execution(root, task_id, contents):
    durable = load_plan_state(root, task_id)
    step_id = durable['current_step_id']; step = next(r for r in durable['steps'] if r['step_id'] == step_id)
    body = "printf 'fixture command; scientific execution is a recorded stub\\n'\n"
    script = root / 'task_outputs' / task_id / (step_id + '.sh'); script.write_text(body)
    descriptor = {'path': str(script), 'sha256': hashlib.sha256(body.encode()).hexdigest()}
    action = {'action_id': 'action-' + step_id, 'step_id': step_id, 'type': 'execute',
        'command_role': 'analysis_execution', 'purpose': 'analysis', 'plan_version': durable['plan_version'],
        'code': body, 'cwd': str(root), 'argv': ['bash', str(script)], 'env': {'GENOME_VERSION': 'fixture'},
        'outputs': list(step['outputs']), 'expected_step_effect': canonical_step_effect(step)}
    record_provenance_event(durable, 'action_bound', {'action': action, 'raw_action': action})
    attempt = record_provenance_event(durable, 'attempt_started', {'action_id': action['action_id'],
        'step_id': step_id, 'approved_script': descriptor, 'executed_script': descriptor})
    for path, text in contents.items(): path.write_text(text)
    execution = {'schema_version': 1, 'task_id': task_id, 'execution_id': 'execution-' + step_id,
        'step_id': step_id, 'attempt_id': attempt['attempt_id'], 'action_id': action['action_id'],
        'plan_id': durable['plan_id'], 'plan_version': durable['plan_version'], 'exit_code': 0,
        'execution_status': 'succeeded', 'command_role': 'analysis_execution', 'purpose': 'analysis',
        'code': body, 'raw_stdout': 'fixture leaf returned rc0\n', 'raw_stderr': '',
        'produced_artifacts': [str(p) for p in contents], 'declared_outputs': [str(p) for p in contents],
        'step_effect': canonical_step_effect(step), 'declared_step_effect': canonical_step_effect(step),
        'effect_evidence': {'schema_version': 1, 'step_id': step_id, 'action_id': action['action_id'],
            'attempt_id': attempt['attempt_id'], 'status': 'validated'}}
    record_provenance_event(durable, 'execution_finished', {'attempt_id': attempt['attempt_id'], 'execution_record': execution})
    mark_step_verification_pending(durable, step_id=step_id, attempt_id=attempt['attempt_id'])
    save_plan_state(root, task_id, durable)
    return {'task_id': task_id, 'workflow_routing_schema_version': 2, 'validation_level': 'scientific', 'execution_evidence': execution}


class Reviewer:
    def __init__(self, root): self.root, self.requests, self.reports, self.checks = root, [], [], []
    async def ainvoke(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        report = _tag_json(messages, 'completion_report'); self.reports.append(report)
        accepted = report['scope']['step_id'] == 'step.summary'
        reason = 'Read the pending delivery and its exact upstream summary.'
        facts = (report.get('effect_presence') or {}).get('revalidation_inspection_facts') or []
        if report['scope']['step_id'] == 'step.finalize' and facts:
            observations = [r.get('observation') or {} for r in facts]
            contents = [(r.get('result') or {}).get('content', '') for r in observations if not r.get('error')]
            try:
                table = next(s for s in contents if s.startswith('metric\tvalue'))
                self.checks.append(summary.validate(table))
                package = json.loads(next(s for s in contents if s.startswith('{"steps":')))
                catalog = accepted_execution_materials(load_plan_state(self.root, 'fullchain-delivery'), self.root)
                checked = validate_package_sources(package, catalog, required_step_ids=['step.summary'],
                    required_dependencies=['config', 'helper'])
                self.checks.append(checked)
                accepted = checked['status'] == 'source_checks_passed'
                reason = 'Exact read content and dependency/source claims checked.' if accepted else str(checked['errors'])
            except (StopIteration, ValueError) as exc:
                reason = 'Required content check failed: ' + type(exc).__name__ + ':' + str(exc)
        disposition = 'completion_accepted' if accepted else 'inspection_required' if not facts else 'correction_required'
        return AIMessage(content=json.dumps({'report_id': report['report_id'],
            'assessment': 'ACCEPTABLE' if accepted else 'CONCERN', 'disposition': disposition,
            'issue_dispositions': [{'issue_id': report['scope']['validation_id'], 'disposition': disposition, 'explanation': reason}],
            'concerns': [] if accepted else [reason], 'suggestions': [], 'confidence': .95}))


class Reader:
    def __init__(self): self.requests, self.reads = [], []
    async def ainvoke(self, messages, **kwargs):
        self.requests.append(deepcopy(messages)); seed = _tag_json(messages, 'recovery_seed')
        if len(self.requests) == 1:
            refs = [r for r in seed['inspection_manifest'] if r['kind'] == 'artifact' and r['readable']]
            self.refs = [r['ref_id'] for r in refs]
            return AIMessage(content='读取当前交付与已接纳上游汇总。', tool_calls=[
                {'id': 'read-' + str(i), 'name': 'artifact_read', 'args': {'ref_id': r['ref_id']}}
                for i, r in enumerate(refs)])
        self.reads = [json.loads(m.content) for m in messages if m.type == 'tool']
        scope = seed['invocation_scope']
        return AIMessage(content=json.dumps({'schema_version': 1, 'directive_id': 'revalidate-delivery',
            'task_id': scope['task_id'], 'target_step_id': scope['step_id'], 'action': 'revalidate',
            'summary': '用实际读取内容重新核验同一 attempt。', 'reason_code': 'INSPECTION_EVIDENCE_AVAILABLE',
            'instruction': 'Review summary and reproduction dependencies; retain upstream commits; no science rerun.',
            'evidence_refs': self.refs}))


@pytest.mark.parametrize('variant', ['valid', 'bad_summary', 'missing_dependency', 'stale_attempt'])
def test_fullchain_read_review_commit_or_reject(tmp_path, variant):
    async def run():
        task_id, paths = setup(tmp_path)
        reviewer = Reviewer(tmp_path)
        def supervisor():
            node = SupervisorNode(reviewer, output_dir=str(tmp_path / 'task_outputs'))
            typed = TypedSupervisor(data_path=str(tmp_path), verifier=node.run)
            return bind_causal_failure_node(typed.run, binder=CausalFailurePacketBinder(data_path=str(tmp_path)), outcome_field='verification_outcome')
        state = record_execution(tmp_path, task_id, {paths['ann_summary']: SUMMARY if variant != 'bad_summary' else SUMMARY.replace('LOW\t2', 'LOW\t0')})
        state.update(await supervisor()(state))
        assert state['verification_outcome']['type'] in {'verified', 'verified_with_warnings'}, state['verification_outcome']['summary']
        first = await CommitStep(data_path=str(tmp_path)).run(state)
        assert first['commit_step_outcome']['type'] == 'next_step_ready', first
        durable = load_plan_state(tmp_path, task_id); upstream = deepcopy(durable['steps'][0])
        catalog = accepted_execution_materials(durable, tmp_path)
        assert len(catalog['sources']) == 1, catalog
        source = catalog['sources'][0]; original = source['scripts']['approved_script']
        assert original['body'].startswith('printf')
        package = {'steps': [{k: source[k] for k in ('step_id', 'attempt_id', 'commit_id', 'cwd', 'argv')}], 'dependencies': []}
        package['steps'][0].update(body=original['body'], original_body=original['body'], original_sha256=original['sha256'])
        for name in ['config', 'helper']:
            path = tmp_path / name; path.write_text('synthetic dependency\n')
            package['dependencies'].append({'name': name, 'packaged_path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        if variant == 'missing_dependency': (tmp_path / 'config').unlink()
        state = record_execution(tmp_path, task_id, {paths['report']: 'fixture report; science E2E NOT_RUN\n',
            paths['reproduce_script']: json.dumps(package), paths['stage_status']: 'step\tstatus\nsummary\tcompleted\nfinalize\tverification_pending\n'})
        state.update(await supervisor()(state))
        assert state['verification_outcome']['type'] == 'inspection_required', state['verification_outcome']['summary']
        reader = Reader()
        update = await Schema2RecoveryNode(data_path=str(tmp_path), model=reader).run(state)
        assert update['recovery_outcome']['type'] == 'revalidation_required', update['recovery_outcome']
        assert len(reader.requests) == 2 and all(not r.get('error') for r in reader.reads), reader.reads
        state.update(update)
        if variant == 'stale_attempt': state['execution_evidence']['attempt_id'] = 'foreign-attempt'
        state.update(await supervisor()(state))
        before = load_plan_state(tmp_path, task_id)
        committed = await CommitStep(data_path=str(tmp_path)).run(state)
        after = load_plan_state(tmp_path, task_id)
        assert after['steps'][0]['status'] == upstream['status'] == 'passed'
        if variant == 'valid':
            assert committed['commit_step_outcome']['type'] == 'plan_complete', committed
            assert after['steps'][1]['status'] == 'passed'
            assert any(r.get('coverage') == 'summary_internal_consistency_only' for r in reviewer.checks)
        else:
            assert committed['commit_step_outcome']['type'] == 'contract_rejected', committed
            assert after['steps'][1]['status'] != 'passed'
            assert after['task_state'].get('typed_step_commit_receipts') == before['task_state'].get('typed_step_commit_receipts')
        assert len(after['provenance_state']['attempts']) == 2  # zero science redispatch
        evidence = {'variant': variant, 'provider': 'explicit_response_stubs', 'science': 'recorded_execution_leaf',
            'recovery_calls': len(reader.requests), 'science_redispatches': 0, 'commit': committed,
            'read_receipts': reader.reads, 'reviewer_checks': reviewer.checks,
            'reviewer_request_contains_summary': any('variants_total' in str(m.content) for q in reviewer.requests[2:] for m in q),
            'upstream_retained': after['steps'][0]['status']}
        (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / ('delivery-' + variant + '.json')).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    asyncio.run(run())


def test_accepted_execution_catalog_reaches_actual_generate_messages(tmp_path):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol
    async def run():
        task_id, paths = setup(tmp_path)
        reviewer = Reviewer(tmp_path)
        state = record_execution(tmp_path, task_id, {paths['ann_summary']: SUMMARY})
        node = SupervisorNode(reviewer, output_dir=str(tmp_path / 'task_outputs'))
        state.update(await TypedSupervisor(data_path=str(tmp_path), verifier=node.run).run(state))
        state.update(await CommitStep(data_path=str(tmp_path)).run(state))
        case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp()
        try:
            configure_protocol(case)
            case.agent.data_path = str(tmp_path)
            requests = []
            async def provider(messages, **kwargs):
                requests.append(deepcopy(messages))
                return AIMessage(content='<shell>printf "candidate awaiting approval\\n"</shell>')
            case.agent._ainvoke_node_llm = provider
            case.agent._ainvoke_llm = provider
            runnable = case.agent._app_for_task(task_id, state).get_graph().nodes['generate'].data
            result = await runnable.ainvoke(state)
            assert requests, result
            catalog = _tag_json(requests[0], 'accepted_execution_sources')
            assert len(catalog['sources']) == 1
            assert catalog['sources'][0]['scripts']['approved_script']['body'].startswith('printf')
            assert catalog['independent_reproduction'] == 'NOT_VERIFIED'
            (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / 'generate-accepted-source-wire.json').write_text(
                json.dumps({'entry': 'actual generate graph Runnable -> _ainvoke_node_llm response stub',
                    'provider_calls': len(requests), 'catalog': catalog,
                    'first_request': [m.model_dump() for m in requests[0]],
                    'independent_scientific_execution': 'NOT_RUN'}, ensure_ascii=False, indent=2))
            # A lost approved original must not pass by comparing two absent
            # bodies/hashes, even when the package cites a real Commit.
            original = catalog['sources'][0]
            Path(original['scripts']['approved_script']['path']).unlink()
            missing = accepted_execution_materials(load_plan_state(tmp_path, task_id), tmp_path)
            package = {'steps': [{k: original[k] for k in ('step_id', 'attempt_id', 'commit_id', 'cwd', 'argv')}]}
            checked = validate_package_sources(package, missing)
            assert 'REPRODUCTION_SOURCE_UNAVAILABLE' in checked['errors']
        finally:
            await case.asyncTearDown()
    asyncio.run(run())


def test_formally_consumed_stop_retains_actual_episode_counts(tmp_path):
    from agent_core.workflow.managed_stop import build_managed_stop, projection
    from tests.test_inspection_workflow import make_inspection_fixture, ReadThenRevalidateModel
    class StopReader(ReadThenRevalidateModel):
        async def ainvoke(self, messages, **kwargs):
            response = await super().ainvoke(messages, **kwargs)
            if response.content and not response.tool_calls:
                directive = json.loads(response.content)
                directive.update(action='blocked', reason_code='FIXTURE_MODEL_STOP')
                return AIMessage(content=json.dumps(directive))
            return response
    async def run():
        fixture = make_inspection_fixture(tmp_path)
        await fixture.inspect_handoff()
        model = StopReader()
        result = await Schema2RecoveryNode(data_path=str(tmp_path), model=model).run(fixture.state)
        outcome = result['recovery_outcome']
        assert outcome['type'] == 'blocked'
        durable = fixture.reload()
        assert not durable['task_state']['active_failure_packet_ref']
        stop = build_managed_stop(fixture.task_id, result, durable,
            {'node': 'recovery', 'status': 'blocked', 'outcome': outcome})
        facts = projection(stop)['terminal_diagnostic']
        assert facts['recovery_usage']['scope'] == 'last_admitted_episode'
        assert facts['recovery_usage']['turn_count'] == len(model.requests) > 0
        assert facts['recovery_usage']['tool_call_count'] > 0
        assert facts['origin']['failure_type'] == 'inspection_required'
        assert facts['stop_reason']['error_code'] == 'FIXTURE_MODEL_STOP'
        assert 'directive' not in facts['latest_failure']
        (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / 'consumed-stop-counts.json').write_text(
            json.dumps(facts, ensure_ascii=False, indent=2))
    asyncio.run(run())
