"""Audit witnesses: real downstream mechanics, fixed provider/science stand-ins.

No existing test or production assertion is changed. These are not FIX PASS.
Run only through export_and_run.py and its existing process/resource guards.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage


def write(name, value):
    path = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('a1-' + name + '.json')
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def mechanism(mode):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.runtime_graph import build_schema2_production_graph
    from agent_core.workflow.managed_stop import terminal_boundary, finalize_managed_stop
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.test_preflight_recovery_audit import PYTHON
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    workspace_mode = mode == 'workspace_prepare'
    sid = 'genomics/snpeff-annotation' if workspace_mode else 'genomics/wgs-short-read-mapping'
    case.SKILL_ID = sid
    await case.asyncSetUp()
    root, agent = case.root, case.agent
    task = 'a1-downstream-' + mode
    counter = root / 'isolated-mechanism-counter.txt'
    report = root / 'report.md'
    goal = ('Run six explicitly synthetic mechanism steps. Each new tiny Python process reads the previous counter, '
            'asserts its predecessor, and increments it. This substitutes all scientific computation and proves no '
            'scientific property. Stop if no further grounded action is available. Optional report: ' + str(report))
    if workspace_mode:
        reference, annotation = root / 'new-reference.fasta', root / 'new-annotation.gtf'
        reference.write_text('>synthetic\nACGT\n')
        annotation.write_text('synthetic\tfixture\tgene\t1\t4\t.\t+\t.\tgene_id "synthetic";\n')
        goal = ('Prepare one isolated synthetic snpEff workspace layout and config; no real database build or scientific tools. '
                'Explicit genome version synthetic_v1. Reference ' + str(reference) + '; annotation ' + str(annotation)
                + '; optional diagnostic report ' + str(report))
    persist = agent._persist_user_submission
    agent._persist_user_submission = lambda tid, _prompt: persist(tid, goal)
    counts = {'consultant': 0, 'planner': 0, 'generate': 0, 'reviewer': 0}
    approvals, executions, events, requests, commit_updates = [], [], [], [], []

    async def provider(messages, *, stage, **kwargs):
        counts[stage] += 1
        requests.append({'stage': stage, 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        if stage == 'consultant':
            inputs = ([{'operation': 'upsert_input', 'input': {'role': role, 'path': str(path), 'required': True},
                        'supporting_evidence': ['U1']} for role, path in [('reference_fasta', reference), ('annotation_gtf', annotation)]]
                      if workspace_mode else [])
            return AIMessage(content=json.dumps({'action_type': 'submit_planning_handoff',
                'summary': 'Explicit synthetic mechanism, no scientific claim.', 'selected_skills': [sid],
                'task_spec_operations': [*inputs, {'operation': 'set_goal', 'value': goal, 'supporting_evidence': ['U1']},
                    {'operation': 'upsert_output_contract', 'output_contract': {'output_id': 'report', 'role': 'report',
                        'artifact_kind': 'file', 'path': str(report), 'required': False}, 'supporting_evidence': ['U1']}],
                'unresolved_ambiguities': [], 'planner_feedback': None}))
        if stage == 'planner':
            if workspace_mode:
                cap = agent.skill_manager.get_execution_capability_registry([sid])['genomics.snpeff_annotation.prepare_database']
                artifacts = [
                    {'local_ref': 'plan.db', 'semantic_view': {'role': 'snpeff_data_dir', 'format': 'directory', 'description': 'New isolated mutable layout'},
                     'artifact_contract': {'object_kind': 'directory', 'resource_kind': 'mutable_workspace', 'lifecycle': 'plan_local', 'materialization_policy_id': 'managed_intermediate_v1'},
                     'role_associations': [{'step_id': 'prepare_database', 'capability_id': cap['capability_id'], 'role': 'artifact.snpeff_data_dir'}]},
                    {'local_ref': 'plan.config', 'semantic_view': {'role': 'snpeff_config', 'format': 'text', 'description': 'New synthetic config'},
                     'artifact_contract': {'object_kind': 'file', 'lifecycle': 'plan_local', 'materialization_policy_id': 'managed_intermediate_v1'}}]
                step = {'step_id': 'prepare_database', 'title': 'Prepare synthetic layout', 'action': 'execute', 'purpose': 'analysis',
                    'tool': sid, 'skill_ids': [sid], 'execution_capability_id': cap['capability_id'], 'operation': 'prepare_database',
                    'inputs': ['input.reference_fasta', 'input.annotation_gtf'], 'outputs': ['plan.config'], 'dependencies': [],
                    'batch_id': '', 'workflow_stage_ids': [], 'risk_level': 'low', 'success_criteria': ['Actual new layout and config exist; no scientific claim.'],
                    'produces_bindings': ['genome_version', 'snpeff_db'], 'produces_observations': ['database_layout_prepared'],
                    'workspace_mutations': [{**deepcopy(row), 'workspace_ref': 'plan.db'} for row in cap['workspace_mutations']]}
                return AIMessage(content=json.dumps({'outcome_type': 'SUBMIT_PLAN_FOR_REVIEW', 'proposed_artifacts': artifacts,
                    'steps': [step], 'validation_checklist': [{'check_id': 'layout', 'description': 'Real bounded layout attestation.'}],
                    'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}))
            steps = []
            for i in range(1, 7):
                steps.append({'step_id': 'mechanism-' + str(i), 'title': 'Synthetic mechanism ' + str(i),
                    'action': 'execute', 'purpose': 'validation', 'tool': sid, 'skill_ids': [sid],
                    'execution_capability_id': 'genomics.wgs.short.read.mapping.inspect', 'operation': 'inspect',
                    'inputs': [], 'outputs': [], 'dependencies': [] if i == 1 else ['mechanism-' + str(i-1)],
                    'batch_id': '', 'workflow_stage_ids': [], 'risk_level': 'low',
                    'success_criteria': ['Record only the observed isolated counter transition; no scientific conclusion.'],
                    'produces_observations': [{'identity': 'validation_observation', 'evidence_contract': {'required_fields': ['value']}}]})
            return AIMessage(content=json.dumps({'outcome_type': 'SUBMIT_PLAN_FOR_REVIEW', 'proposed_artifacts': [],
                'steps': steps, 'validation_checklist': [{'check_id': 'synthetic', 'description': 'Six actual counter transitions.'}],
                'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}))
        assert stage == 'generate'
        if workspace_mode:
            from agent_core.plan_tracker import load_approved_plan_snapshot
            from agent_core.plan_bindings import public_binding_requirements
            from agent_core.workspace_attestation import project_current_action_workspace_bindings
            durable = load_plan_state(root, task)
            snapshot = load_approved_plan_snapshot(root, task, state=durable)
            step = snapshot['steps'][0]
            cap = agent.skill_manager.get_execution_capability_registry([sid])[step['execution_capability_id']]
            frozen = step['generate_binding_input_contract']
            binding = frozen['binding_contract']
            catalog = project_current_action_workspace_bindings(plan_state=durable, approved_plan_snapshot=snapshot,
                current_step=step, capability_contract=cap, binding_rule=binding['operations']['prepare_database'],
                derived_artifact_layout=binding.get('derived_artifact_layout'))
            handle = catalog[0]['plan_handle']
            assert handle in str(messages), 'Model uses an actually published catalog handle.'
            artifact_id = durable['steps'][0]['outputs'][0]
            config_path = durable['plan_artifacts'][artifact_id]['canonical_path']
            assert artifact_id in str(messages) and config_path in str(messages), 'Use only actually published artifact identity/path.'
            # Independent legal downstream input: a complete script publishing
            # its actual values in the advertised effect JSON wire. This does
            # not exercise/fix structured-action helper-marker expansion.
            source = 'import json, os\nfrom pathlib import Path\n'
            source += 'db=Path(os.environ["SNPEFF_DB"]); genome=os.environ["GENOME_VERSION"]\n'
            source += '(db/"data"/"genomes").mkdir(parents=True,exist_ok=True)\n(db/"data"/genome).mkdir(exist_ok=True)\n'
            source += '(db/"data"/"genomes"/(genome+".fa")).write_bytes(Path(' + repr(str(reference)) + ').read_bytes())\n'
            source += '(db/"data"/genome/"genes.gtf").write_bytes(Path(' + repr(str(annotation)) + ').read_bytes())\n'
            source += 'config=Path(' + repr(config_path) + '); config.parent.mkdir(parents=True,exist_ok=True); config.write_text(genome+".genome : synthetic fixture\\n")\n'
            source += 'observed=all(p.is_file() and p.stat().st_size>0 for p in [config,db/"data"/"genomes"/(genome+".fa"),db/"data"/genome/"genes.gtf"])\n'
            source += 'print("BIOMIND_EFFECT_EVIDENCE_JSON="+json.dumps({"schema_version":1,"produces_artifacts":[{"identity":' + repr(artifact_id) + ',"path":str(config)}],"produces_observations":[{"identity":"database_layout_prepared","value":observed}]}))\n'
            body = shlex.quote(PYTHON) + " -I -S - <<'PY'\n" + source + 'PY\n'
            return AIMessage(content=json.dumps({'type': 'execute', 'language': 'bash', 'operation': 'prepare_database', 'code': body,
                'produced_bindings': {'genome_version': {'type': 'string', 'value': 'synthetic_v1'},
                                     'snpeff_db': {'type': 'artifact_ref', 'value': {'handle': handle}}}}), response_metadata={'finish_reason': 'stop'})
        i = int(load_plan_state(root, task)['current_step_id'].rsplit('-', 1)[1])
        if mode == 'voluntary_stop' and i == 2:
            return AIMessage(content=json.dumps({'type': 'blocked', 'reason': 'One real synthetic transition completed; no new grounded action is available.'}))
        template = json.loads(next(m.content for m in messages if getattr(m, 'name', '') == 'approved_effect_emission_template'))
        source = template['marker'] + '\nfrom pathlib import Path\n'
        source += 'p=Path(' + repr(str(counter)) + ')\nprevious=int(p.read_text()) if p.exists() else 0\n'
        source += 'assert previous == ' + str(i-1) + '\np.write_text(str(previous+1))\n'
        source += "print(emit_effect_evidence({'produces_observations':{'validation_observation':{'value':{'previous':previous,'current':int(p.read_text()),'scientific_stub':True}}}}))\n"
        return AIMessage(content='<shell>' + shlex.quote(PYTHON) + " -I -S - <<'PY'\n" + source + 'PY\n</shell>',
                         response_metadata={'finish_reason': 'stop'})

    agent._ainvoke_node_llm = provider
    agent.supervisor_node.use_llm_review = True
    class Reviewer:
        async def ainvoke(self, messages, **kwargs):
            counts['reviewer'] += 1
            seed = next(m.content for m in messages if getattr(m, 'name', '') == 'supervisor_advisory_seed')
            completion = json.loads(seed.split('<completion_report>\n', 1)[1].split('\n</completion_report>', 1)[0])
            payload = {'report_id': completion['report_id'], 'assessment': 'ACCEPTABLE', 'disposition': 'completion_accepted',
                'issue_dispositions': [{'issue_id': issue, 'disposition': 'completion_accepted',
                    'explanation': 'Actual bounded synthetic counter observation only.'}
                    for issue in completion['semantic_review_requirements']['allowed_issue_ids']],
                'confidence': 0.9, 'concerns': [], 'suggestions': []}
            requests.append({'stage': 'reviewer', 'report': completion, 'response': payload})
            return AIMessage(content=json.dumps(payload), response_metadata={'finish_reason': 'stop'})
    agent.supervisor_node._llm_reviewer.llm = Reviewer()
    del agent.middleware.review_code
    agent.middleware.auto_approve_safe = False
    real_review = agent.middleware.review_code
    def approve(code, *args, **kwargs):
        result = real_review(code, *args, **kwargs)
        if result[0] == 'approve':
            approvals.append({'sha256': hashlib.sha256(code.encode()).hexdigest(), 'code': code})
        return result
    agent.middleware.review_code = approve

    async def execute(path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
        from execution.executor import run_script_path
        path = Path(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
        approved = path.with_name('approved.sh').read_bytes()
        assert hashlib.sha256(approved).hexdigest() == approvals[-1]['sha256']
        clean = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
        if workspace_mode:
            clean.update({key: env[key] for key in ('GENOME_VERSION', 'SNPEFF_DB')})
        bootstrap = sys.modules['__main__']
        bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
        bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
        try:
            out, err, rc = await run_script_path(str(path), expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False, env=clean,
                managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
            bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
        executions.append({'attempt': path.parent.name, 'stdout': out, 'stderr': err, 'rc': rc,
                           'approved_sha256': hashlib.sha256(approved).hexdigest(), 'executed_sha256': expected_sha256})
        return out, err, rc

    try:
        runner = case._runner_state(task)
        runner['workflow_routing_schema_version'] = bio_agents.WORKFLOW_ROUTING_SCHEMA_TYPED
        app = agent._app_for_task(task, runner)
        config = {'configurable': {'thread_id': task}, 'recursion_limit': 65}
        incoming = runner
        with patch.object(bio_agents, 'run_script_path', execute):
            for _ in range(8):
                async for event in app.astream(incoming, config=config, stream_mode='updates'):
                    events.append(event)
                    if 'commit_step' in event:
                        commit_updates.append(deepcopy(event['commit_step']))
                snapshot = app.get_state(config)
                if not snapshot.next:
                    break
                assert snapshot.tasks and snapshot.tasks[0].interrupts
                incoming = bio_agents.Command(resume='y')
        values = app.get_state(config).values
        durable = load_plan_state(root, task)
        commits = durable['task_state'].get('typed_step_commit_receipts', {})
        if mode in {'six_success', 'workspace_prepare'}:
            expected = 1 if workspace_mode else 6
            assert len(commits) == len(executions) == len(approvals) == expected
            assert [e['rc'] for e in executions] == [0] * expected
            assert values['final_audit_outcome']['type'] == 'task_verified'
            assert durable['task_state']['status'] == 'completed'
            if workspace_mode:
                assert len(durable['task_state']['accepted_plan_bindings']) == 2
            else:
                assert counter.read_text() == '6'
            # The first acknowledged CAS must not undo the sixth step on replay.
            assert len(commit_updates) == expected
        else:
            assert len(commits) == len(executions) == 1
            assert values['generate_outcome']['type'] == 'blocked'
            assert values['generate_outcome']['payload']['reason_code'] == 'GENERATE_STOP_REQUESTED'
            boundary = terminal_boundary(values)
            assert boundary['status'] == 'blocked'
            agent.terminal_summary_policy = {'enabled': False}
            stopped = await finalize_managed_stop(agent, task, values, boundary)
            assert stopped['report_receipts']['report.md']['status'] == 'created'
            assert not load_plan_state(root, task)['task_state'].get('candidate_repair_state', {}).get('events')
        write(mode, {'witness_pass': True, 'fix_pass': False, 'counts': counts, 'executions': executions,
                     'approvals': approvals, 'routes': [next(iter(e)) for e in events], 'commits': commits,
                     'values': values, 'durable': load_plan_state(root, task), 'requests': requests,
                     'scope': ('Formal prepare_database capability, actual isolated layout and mutation attestation; no real database build.' if workspace_mode else
                               'Six-step mechanism chain, repeated formal inspect capability; scientific computation replaced by new counter processes.'),
                     'success_report_scope': 'FinalAudit factual completion; optional physical report not produced by successful graph.' if mode != 'voluntary_stop' else 'Real managed-stop report publication.',
                     'fixture_limits': 'Explicit managed triage admission, fixed model responses, disabled session-memory/compression and runtime scientific readiness stand-ins; not real CLI or science E2E.'})
    finally:
        write(mode + '-progress', {'counts': counts, 'routes': [next(iter(e)) for e in events],
            'executions': executions, 'events': events, 'requests': requests})
        await case.asyncTearDown()


@pytest.mark.parametrize('mode', ['six_success', 'voluntary_stop', 'workspace_prepare'])
def test_actual_six_step_or_voluntary_stop(mode):
    asyncio.run(mechanism(mode))
