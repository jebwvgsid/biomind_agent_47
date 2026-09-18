"""Real Skill inspection -> reconcile/Commit -> next provider -> Q refusal.

Only provider/human approval, the controlled process-launch harness and unused
memory observation are isolated. Plan/phase/helper binding/reconciliation and
Commit remain production code. All inputs are synthetic and local to tmp.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

SID = 'population-genetics/gwas-vcf-prep-qc'
INSPECT = 'population.genetics.gwas.vcf.prep.qc.inspect_inputs'
PREPARE = 'population.genetics.gwas.vcf.prep.qc.prepare_q_covariates'


def test_real_gwas_observation_is_consumed_after_commit_and_unknown_q_refuses_output(tmp_path):
    import bio_agents
    from langchain_core.messages import AIMessage
    from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
    from agent_core.skill_resource_binding import resource_env
    from execution.executor import run_script_path
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

    async def scenario():
        case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        case.SKILL_ID = SID
        await case.asyncSetUp()
        task = 'gwas-input-observation-joint'
        counts = {'consultant': 0, 'planner': 0, 'generate': 0}
        requests, approvals, executions, events = [], [], [], []
        output = Path(os.environ.get('BIOMIND_CLI_PROGRESS_ARTIFACTS', str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        try:
            for name in ('_consultant_runtime_seed_evidence', '_consultant_runtime_preflight', '_ensure_skill_runtime_ready',
                         '_selected_skill_env_runner_violation', '_workflow_future_skill_violation'):
                delattr(case.agent, name)
            vcf = case.root / 'synthetic.vcf'
            phenotype = case.root / 'synthetic.phenotype.tsv'
            q = case.root / 'synthetic.Q'
            tfam = case.root / 'synthetic.tfam'
            q_output = case.root / 'must-not-be-created.covariates.tsv'
            vcf.write_text('##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n1\t1\t.\tA\tC\t.\tPASS\t.\tGT\t0/0\t0/1\n')
            phenotype.write_text('S1\t11\nS2\t22\n')
            q.write_text('0.1 0.2 0.7\n0.4 0.5 0.1\n')
            tfam.write_text('F1 S1 0 0 0 -9\nF2 S2 0 0 0 -9\n')
            inputs = [('vcf', vcf), ('phenotype', phenotype), ('q_matrix', q), ('target_tfam', tfam)]
            before_inputs = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for _, p in inputs}
            goal = ('Inspect these synthetic inputs without a report file; preserve all phenotype rows and unknown Q correspondence. '
                    'Prepare Q covariates only after established mapping, otherwise refuse output: '
                    + ', '.join(str(p) for _, p in inputs) + '; target ' + str(q_output))
            persist = case.agent._persist_user_submission
            case.agent._persist_user_submission = lambda tid, _prompt: persist(tid, goal)

            def handoff():
                operations = [{'operation': 'set_goal', 'value': goal, 'supporting_evidence': ['U1']}]
                operations += [{'operation': 'upsert_input', 'input': {'role': role, 'path': str(path), 'required': True}, 'supporting_evidence': ['U1']}
                    for role, path in inputs]
                operations.append({'operation': 'upsert_output_contract', 'output_contract': {'output_id': 'q_covariates',
                    'role': 'q_covariates', 'artifact_kind': 'file', 'path': str(q_output), 'required': True}, 'supporting_evidence': ['U1']})
                return {'action_type': 'submit_planning_handoff', 'summary': 'Inspect structure; Q preparation retains its identity precondition.',
                    'selected_skills': [SID], 'task_spec_operations': operations, 'unresolved_ambiguities': [], 'planner_feedback': None}

            class ProviderLeaf:
                max_tokens = 16000
                model_name = 'offline-gwas-structure'
                def bind_tools(self, tools, **kwargs):
                    return self
                def bind(self, **kwargs):
                    return self
                def model_copy(self, update=None):
                    return self
                async def ainvoke(self, messages, config=None, **kwargs):
                    stage = (config or {}).get('tags', [''])[-1]
                    if stage == "workspace_agent":
                        from tests.test_plan_local_preaction_convergence_v2 import workspace_execution_response
                        return workspace_execution_response(messages)
                    if stage in {'', 'router'}:
                        return AIMessage(content=json.dumps({'schema_version': 1, 'request_kind': 'execute', 'execution_scope': 'managed',
                            'managed_intent_explicit': True, 'retrieval_mode': 'kb', 'confidence': 1.0, 'reason': 'Explicit isolated input validation'}))
                    assert stage in counts, stage
                    counts[stage] += 1
                    requests.append({'stage': stage, 'messages': [{'type': m.type, 'content': deepcopy(m.content)} for m in messages]})
                    if stage == 'consultant':
                        assert counts[stage] == 1
                        return AIMessage(content=json.dumps(handoff()))
                    if stage == 'planner':
                        assert counts[stage] == 1
                        task_spec = canonical_task_spec_for_state(load_plan_state(case.root, task))
                        handles = ['input.' + row['role'] for row in task_spec['inputs']]
                        common = {'action': 'execute', 'purpose': 'validation', 'inputs': handles, 'batch_id': '',
                            'workflow_stage_ids': [], 'skill_ids': [SID], 'risk_level': 'low'}
                        steps = [{**common, 'step_id': 'check-input-structure', 'title': 'Observe sample structure and unknown Q mapping',
                            'operation': 'inspect_inputs', 'tool': INSPECT, 'execution_capability_id': INSPECT,
                            'outputs': [], 'dependencies': [], 'produces_observations': [{'identity': 'gwas_input_structure',
                                'evidence_contract': {'required_fields': ['value']}}],
                            'success_criteria': ['Publish scoped sample identity/header and Q mapping observation; unknown Q is an admissible finding.']},
                            {**common, 'step_id': 'validate-q-before-output', 'title': 'Q identity guard must refuse an unresolved mapping',
                            'operation': 'prepare_q_covariates', 'tool': PREPARE, 'execution_capability_id': PREPARE,
                            'outputs': ['output.q_covariates'], 'dependencies': ['check-input-structure'],
                            'success_criteria': ['Only version-validated explicit row correspondence permits nonempty Q covariate output.']}]
                        return AIMessage(content=json.dumps({'outcome_type': 'SUBMIT_PLAN_FOR_REVIEW', 'proposed_artifacts': [], 'steps': steps,
                            'validation_checklist': [{'check_id': 'q-identity', 'description': 'Preserve the unresolved mapping and reject unwarranted output.'}],
                            'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}))
                    wire = json.dumps(requests[-1], ensure_ascii=False)
                    assert 'inspect_inputs.py' in wire and 'sort_phenotype.py' in wire
                    if counts[stage] == 1:
                        assert not executions
                        handle = SID + '#scripts/inspect_inputs.py'
                        command = shlex.quote(sys.executable) + ' "${' + resource_env(handle) + '}"'
                        command += ' --vcf ' + shlex.quote(str(vcf)) + ' --phenotype ' + shlex.quote(str(phenotype))
                        command += ' --q-matrix ' + shlex.quote(str(q)) + ' --observation-id gwas_input_structure'
                    else:
                        assert counts[stage] == 2
                        assert len(executions) == 1 and executions[0]['returncode'] == 0
                        durable = load_plan_state(case.root, task)
                        assert len(durable['task_state'].get('typed_step_commit_receipts', {})) == 1
                        assert 'Q_SAMPLE_MAPPING_UNKNOWN' in wire, wire[-12000:]
                        assert 'association_using_q' in wire and 'q_covariate_preparation' in wire
                        assert 'gwas_input_structure' in wire and 'GA0001' not in wire  # only these synthetic inputs
                        history = next(json.loads(match.group(1)) for message in requests[-1]['messages']
                            for match in [re.search(r'<semantic_history>\s*(.*?)\s*</semantic_history>', str(message['content']), re.S)] if match)
                        accepted = next(row for row in history['accepted_execution_outcomes'] if row['step'] == 'step.check-input-structure')
                        assert accepted['evidence_source'] == 'accepted_typed_step_commit'
                        received = accepted['effect_evidence']['produces_observations'][0]['value']
                        actual = json.loads(executions[0]['stdout'].split('=', 1)[1])['produces_observations'][0]['value']
                        assert received['q_mapping'] == actual['q_mapping']
                        assert received['phenotype']['ids'] == ['S1', 'S2']
                        for name in ['vcf', 'phenotype', 'q_matrix']:
                            assert received[name]['file_identity'] == actual[name]['file_identity']
                        assert received['vcf']['file_identity']['identity_scope'] == 'header_only'
                        assert received['phenotype']['file_identity']['sha256'] == before_inputs[str(phenotype)]
                        assert received['q_matrix']['file_identity']['sha256'] == before_inputs[str(q)]
                        assert received['q_matrix']['file_identity']['identity_scope'] == 'full_file'
                        # Deliberately adverse provider candidate tests the real helper guard.
                        # It must not turn equal row counts into an accepted output.
                        handle = SID + '#scripts/sort_phenotype.py'
                        command = shlex.quote(sys.executable) + ' "${' + resource_env(handle) + '}"'
                        command += ' --tfam ' + shlex.quote(str(tfam)) + ' --phenotype ' + shlex.quote(str(q))
                        command += ' --input-kind q_covariates --trait 1,2,3 --out-emmax-covar ' + shlex.quote(str(q_output))
                    return AIMessage(content='<shell skill_resources="' + handle + '">\n' + command + '\n</shell>',
                        response_metadata={'finish_reason': 'stop'})
            model = ProviderLeaf()
            case.agent.llm = case.agent.planner_llm = case.agent.generate_llm = model

            def approve(code, language, **kwargs):
                provenance = kwargs['provenance']
                policy = next(row['content'] for row in provenance['sections'] if isinstance(row['content'], dict) and 'policy' in row['content'])
                runtime_manifest = next(row['content'] for row in provenance['sections'] if row['risk_source'] == 'RUNTIME_ENV')
                assert runtime_manifest['bindings'] == [], 'The standard-library helper must not require a scientific runtime binding'
                assert len(policy['skill_resources']) == 2
                approvals.append({'code': code, 'policy': deepcopy(policy), 'sha256': hashlib.sha256(code.encode()).hexdigest()})
                return 'approve', code
            case.agent.middleware.review_code = approve

            async def execute(path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
                assert env is not None
                assert Path(path).with_name('approved.sh').read_bytes() == approvals[-1]['code'].encode()
                assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
                for resource in approvals[-1]['policy']['skill_resources']:
                    assert env[resource['environment_variable']] == resource['canonical_path']
                    assert hashlib.sha256(Path(resource['canonical_path']).read_bytes()).hexdigest() == resource['content_hash']
                # Keep the production-derived environment byte-for-byte. This parent
                # process was started with env -i and synthetic config only.
                launch_env = deepcopy(env)
                bootstrap = sys.modules['__main__']
                bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
                bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = launch_env
                actual_shell = asyncio.create_subprocess_shell
                launches = []
                async def observe_shell(command, **options):
                    assert options['env'] == env
                    launches.append({'argv': shlex.split(command), 'cwd': options.get('cwd') or os.getcwd(), 'env': deepcopy(options['env'])})
                    return await actual_shell(command, **options)
                try:
                    with patch.object(asyncio, 'create_subprocess_shell', observe_shell):
                        stdout, stderr, rc = await run_script_path(path, expected_sha256=expected_sha256,
                            interpreter=kwargs.get('interpreter', 'bash'), lifecycle_callback=lifecycle_callback,
                            profile=False, env=launch_env, managed_log_paths=kwargs.get('managed_log_paths'))
                finally:
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
                executions.append({'returncode': rc, 'stdout': stdout, 'stderr': stderr, 'launches': launches,
                    'approved_sha256': approvals[-1]['sha256'], 'executed_sha256': expected_sha256})
                return stdout, stderr, rc

            runner = case._runner_state(task)
            runner['messages'][0].content = goal
            config = {'configurable': {'thread_id': task}, 'recursion_limit': 25}
            with patch.object(bio_agents, 'run_script_path', execute), patch.object(bio_agents, 'init_chat_model', return_value=model):
                app = case.agent._app_for_task(task, runner)
                stream = app.astream(runner, config=config, stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        if len(executions) == 2 and 'execute' in event:
                            break
                finally:
                    await stream.aclose()
            durable = load_plan_state(case.root, task)
            assert counts == {'consultant': 1, 'planner': 1, 'generate': 2}, counts
            assert len(executions) == len(approvals) == 2
            assert executions[0]['returncode'] == 0
            observed = json.loads(executions[0]['stdout'].split('=', 1)[1])['produces_observations'][0]['value']
            assert observed['phenotype']['row_count'] == 2 and observed['phenotype']['ids'] == ['S1', 'S2']
            assert observed['phenotype']['header_present'] is False
            assert observed['q_mapping']['status'] == 'unknown'
            assert observed['vcf_phenotype_alignment']['alignment'] == 'identical'
            assert executions[1]['returncode'] == 2
            assert json.loads(executions[1]['stderr'])['error']['code'] == 'Q_SAMPLE_MAPPING_UNKNOWN'
            assert not q_output.exists()
            commits = durable['task_state'].get('typed_step_commit_receipts', {})
            assert len(commits) == 1
            assert next(row for row in durable['steps'] if row['step_id'] == 'check-input-structure')['outputs'] == []
            assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for _, p in inputs} == before_inputs
            (output / 'gwas-input-observation-joint.json').write_text(json.dumps({'counts': counts, 'requests': requests,
                'events': events, 'approvals': approvals, 'executions': executions, 'commit_receipts': commits,
                'runtime_evidence': durable['task_state'].get('runtime_evidence'), 'actual_observation': observed,
                'q_output_created': q_output.exists(), 'input_hashes': before_inputs,
                'scope': 'Synthetic inputs; real Plan/Skill/helper/binding/phase/provider-adapter/approval/reconcile/Commit. Provider and human decision leaves replaced; actual approved scripts executed with production-provided env. Deliberately adverse second provider response tests helper fail-closed, not scientific model behavior.'}, ensure_ascii=False, indent=2, default=str))
        finally:
            (output / 'gwas-input-observation-joint-debug.json').write_text(json.dumps({'counts': counts,
                'requests': requests, 'events': events, 'approvals': approvals, 'executions': executions}, ensure_ascii=False, indent=2, default=str))
            await case.asyncTearDown()
    asyncio.run(scenario())
