"""Approved Plan -> Generate -> approval -> real shell -> reconcile -> Gate/Commit.

Model replies/approval and environment discovery are explicit fixture leaves.
All managed provenance is emitted by production Execute, never by the test.
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


def test_owned_candidate_reuse_rejects_foreign_changed_inputs_and_explicit_freshness(tmp_path):
    from agent_core.managed_artifacts import (capture_managed_output_snapshot, discover_current_attempt_outputs,
        discover_owned_unchanged_outputs, reconcile_runtime_outputs, prepare_approved_output_parents)
    target = tmp_path / 'new-parent' / 'result.txt'
    contract = {'artifact_id': 'r', 'semantic_handle': 'output.r', 'expected_path': str(target),
        'artifact_kind': 'file', 'validation_contract_id': 'file_exists', 'freshness': 'owned_candidate'}
    assert not target.parent.exists()
    assert prepare_approved_output_parents([contract]) == [str(target.parent)]
    assert not target.exists()
    before = capture_managed_output_snapshot([contract]); target.write_text('')
    after = capture_managed_output_snapshot([contract])
    observed = discover_current_attempt_outputs(before, after, attempt_id='origin')
    assert reconcile_runtime_outputs([contract], observed, attempt_id='origin', exit_code=0)['status'] == 'accepted'
    prior = dict(attempt_id='origin', step_id='s', plan_id='p', plan_version='v', returncode=0,
        managed_input_signature={'in': 'unchanged'}, managed_output_contracts=[contract],
        managed_output_snapshot_after=after, observed_output_records=observed)
    state = {'task_id': 'one', 'plan_id': 'p', 'plan_version': 'v', 'provenance_state': {'attempts': {'origin': prior}}}
    from agent_core.plan_tracker import semantic_plan_hash
    prior['managed_reuse_scope'] = {'task_id': 'one', 'task_revision': None, 'plan_semantic_hash': semantic_plan_hash(state)}
    def reuse(contracts=None, inputs=None):
        return discover_owned_unchanged_outputs(state, contracts or [contract], after,
            capture_managed_output_snapshot([contract]), attempt_id='retry', step_id='s',
            input_signature=inputs or {'in': 'unchanged'})
    accepted = reuse(); assert len(accepted) == 1 and not accepted[0]['current_attempt_owned']
    assert accepted[0]['source_attempt_id'] == 'origin'
    reconciled = reconcile_runtime_outputs([contract], accepted, attempt_id='retry', exit_code=0)
    assert reconciled['bindings'][0]['realization'] == 'revalidated_existing'
    assert not reuse(inputs={'in': 'changed'})
    prior['status']='invalidated'; assert not reuse(); prior.pop('status')
    state['current_task']={'task_revision':2}; assert not reuse(); state.pop('current_task')
    assert not reuse([{**contract, 'freshness': 'current_attempt'}])
    state['plan_version'] = 'new'; assert not reuse(); state['plan_version'] = 'v'
    prior['observed_output_records'] = []; assert not reuse(); prior['observed_output_records'] = observed
    target.write_text('foreign mutation'); assert not reuse()
    foreign = tmp_path / 'foreign'; foreign.mkdir(); (tmp_path / 'link').symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match='SYMLINK'):
        prepare_approved_output_parents([{**contract, 'expected_path': str(tmp_path / 'link' / 'result')}])


def test_reuse_tracks_upstream_directory_members_and_rejects_partial_snapshots(tmp_path):
    from agent_core.managed_artifacts import managed_input_signature, _bounded_reuse_signature
    upstream = tmp_path / 'upstream'; upstream.mkdir(); child = upstream / 'member'; child.write_text('old')
    output = tmp_path / 'new-result'
    action = {'materialized_artifacts': {'output.prior': str(upstream), 'output.current': str(output)},
              'controller_output_bindings': [{'expected_path': str(output)}]}
    before = managed_input_signature(action)
    assert set(before) == {'output.prior'} and before['output.prior']['complete']
    child.write_text('new')
    assert managed_input_signature(action) != before
    assert not _bounded_reuse_signature(upstream, max_entries=1)['complete']
    assert not _bounded_reuse_signature(upstream, max_depth=0)['complete']
    (upstream / 'alias').symlink_to(child)
    assert not managed_input_signature(action)['output.prior']['complete']


def test_frozen_review_level_changes_plan_hash_and_skills_do_not_request_review():
    from agent_core.plan_contract import approved_step_validation_level
    from agent_core.plan_tracker import semantic_plan_hash
    state = {'steps': [{'step_id': 's', 'title': 'x', 'action': 'execute', 'status': 'ready', 'skill_ids': ['domain']}], 'current_step': 0}
    assert approved_step_validation_level(state, 's') == 'basic'
    before = semantic_plan_hash(state)
    state['steps'][0]['validation_level'] = 'scientific'
    assert approved_step_validation_level(state, 's') == 'scientific'
    assert semantic_plan_hash(state) != before


@pytest.mark.parametrize("kind", ["directory", "file", "fresh"])
def test_actual_planner_execute_retry_reuses_unchanged_directory_and_downstream_commits(tmp_path, kind):
    async def scenario():
        import bio_agents
        from agent_core.plan_tracker import load_plan_state, current_plan_step_with_index
        from agent_core.workflow.recovery_node import Schema2RecoveryNode
        from execution.executor import run_script_path
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.ger_fixture_support import configure_protocol
        case = PlanLocalRealGraphTests(methodName='test_graph_a_plan_local_directory_happy_path')
        await case.asyncSetUp(); configure_protocol(case)
        task = 'owned-retry-full-graph'; events=[]; requests=[]; executions=[]; approvals=[]
        case.agent.supervisor_node.use_llm_review = True
        async def unexpected_review(*args, **kw):
            raise AssertionError('No scientific review was declared by the Plan.')
        case.agent.supervisor_node._llm_reviewer.review = unexpected_review
        original = case._planner_payload
        def plan():
            p = original()
            p['steps'][0]['validation_level']='basic'
            if kind == 'file':
                p['proposed_artifacts'][0]['artifact_contract']['object_kind'] = 'file'
                p['proposed_artifacts'][0]['semantic_view']['format'] = 'text'
            if kind == 'fresh': p['proposed_artifacts'][0]['artifact_contract']['freshness'] = 'current_attempt'
            p['steps'][0]['produces_observations'] = [{'identity': 'measured', 'evidence_contract': {'required_fields': ['value', 'scope']}}]
            return p
        case._planner_payload = plan
        async def provider(messages, *, stage, node_context=None, **kw):
            if stage == 'consultant':
                return AIMessage(content=json.dumps(case._consultant_payload(case.root / 'report.md')))
            if stage.startswith('planner'):
                return AIMessage(content=json.dumps(case._planner_payload()))
            assert stage == 'generate', stage
            payload = node_context.llm_payload.to_dict(); requests.append(payload)
            plan_state = load_plan_state(case.root, task); _, step = current_plan_step_with_index(plan_state)
            envs = (payload['current_focus']['runtime'].get('artifact_environment') or
                    payload['current_focus']['runtime'].get('current_action_artifact_bindings') or [])
            lines = ['set -e']
            for row in envs:
                path = ('"$' + row['env_var'] + '"') if row.get('env_var') else shlex.quote(row.get('expected_path') or row['path'])
                if row['semantic_handle'].startswith('input.'):
                    lines.append('test -e ' + path)
                elif row['artifact_kind'] == 'directory':
                    lines.append(('mkdir ' if not executions else 'test -d ') + path)
                elif step['step_id'] == 'step-1':
                    lines.append((': > ' if not executions else 'test -f ') + path)
                else:
                    lines.append("printf 'downstream consumed\\n' > " + path)
            if step['step_id'] == 'step-1':
                fact = {'identity': 'measured', 'value': True}
                if executions: fact['scope'] = 'current-inputs'
                lines.append("printf '%s\\n' " + shlex.quote('BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'produces_observations': [fact]})))
            return AIMessage(content='<shell>\n'+'\n'.join(lines)+'\n</shell>', response_metadata={'finish_reason': 'stop'})
        class RecoveryLeaf:
            def bind_tools(self, tools, **kwargs): return self
            async def ainvoke(self, messages, **kw):
                assert len(executions) == 1
                return AIMessage(content='', tool_calls=[{'name': 'submit_recovery_directive', 'id': 'repair', 'args': {
                    'schema_version': 1, 'directive_id': 'repair-observation', 'task_id': task,
                    'action': 'retry_step', 'target_step_id': 'step-1', 'reason_code': 'SCRIPT_BUG',
                    'summary': 'Required observation scope missing.',
                    'instruction': 'Keep the owned existing directory unchanged. Emit measured with actual value and required scope.'}}],
                    response_metadata={'finish_reason': 'tool_calls'})
        node = Schema2RecoveryNode(data_path=str(case.root), model=RecoveryLeaf(), phase_store=case.agent.phase_archive_store,
            skill_service=case.agent.skill_access_service, event_sink=case.agent._schema2_recovery_event_sink,
            provider_invoke=case.agent._schema2_recovery_provider_invoke)
        case.agent._schema2_runtime_node_handlers['recovery'] = node.run
        case.agent._schema2_node_handlers['recovery'] = node.run
        case.agent.schema2_app = bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer)
        case.agent._ainvoke_node_llm = provider
        def approve(code, language, **kw):
            approvals.append(code)
            return ('approve_backup_replace' if kind == 'file' and executions else 'approve'), code
        case.agent.middleware.review_code = approve
        async def execute(path, *, expected_sha256, lifecycle_callback=None, env=None, **kw):
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
            durable = load_plan_state(case.root, task)
            if not executions:
                ledger = list(durable['plan_artifacts'].values()); assert len(ledger) == 1
                output = Path(ledger[0]['canonical_path'])
                assert output.parent.is_dir() and not output.exists(), 'Runtime creates the parent, never the leaf.'
            bootstrap = sys.modules['__main__']
            bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
            bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = deepcopy(env)
            try:
                result = await run_script_path(path, expected_sha256=expected_sha256, interpreter=kw.get('interpreter', 'bash'),
                    lifecycle_callback=lifecycle_callback, profile=False, env=env, managed_log_paths=kw.get('managed_log_paths'))
            finally:
                bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path)); bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
            executions.append({'stdout': result[0], 'stderr': result[1], 'returncode': result[2]}); return result
        try:
            runner = case._runner_state(task)
            with patch.object(bio_agents, 'run_script_path', execute):
                app = case.agent._app_for_task(task, runner)
                stream = app.astream(runner, config={'configurable': {'thread_id': task}, 'recursion_limit': 35}, stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        durable = load_plan_state(case.root, task)
                        (tmp_path / 'graph.json').write_text(json.dumps({'events': events, 'durable': durable, 'requests': requests, 'executions': executions}, default=str))
                        if len(durable['task_state'].get('typed_step_commit_receipts') or {}) == 2: break
                        if kind == 'fresh' and len(executions) == 2 and 'execute' in event: break
                finally: await stream.aclose()
            durable = load_plan_state(case.root, task)
            if kind == 'fresh':
                assert len(executions) == len(approvals) == 2
                assert 'OUTPUT_MISSING' in json.dumps(events[-1])
                assert not durable['task_state'].get('typed_step_commit_receipts')
                return
            assert durable['steps'][0]['validation_level'] == 'basic'
            assert len(executions) == len(approvals) == 3, (executions, [next(iter(e)) for e in events], str(events[-1])[-3000:])
            assert all(row['returncode'] == 0 for row in executions)
            assert len(durable['task_state']['typed_step_commit_receipts']) == 2
            attempts = list(durable['provenance_state']['attempts'].values())
            first, retry = [row for row in attempts if row['step_id'] == 'step-1']
            path = next(iter(durable['plan_artifacts'].values()))['canonical_path']
            assert first['managed_output_snapshot_after'][path] == retry['managed_output_snapshot_after'][path]
            reused = next(row for row in retry['observed_output_records'] if row['path'] == path)
            assert reused['source_attempt_id'] == first['attempt_id'] and reused['current_attempt_revalidated']
            artifacts = list(durable['provenance_state']['artifacts'].values())
            assert any(a.get('producer_attempt_id') == first['attempt_id'] and a.get('acceptance_attempt_id') == retry['attempt_id'] for a in artifacts)
            assert (case.root / 'report.md').read_text() == 'downstream consumed\n'
        finally: await case.asyncTearDown()
    asyncio.run(scenario())


def test_reference_helper_emits_approved_plan_local_identity_without_alias_rewriting(tmp_path, monkeypatch, capsys):
    from tests.test_recovery50000_reference_helper import helper, _request, _fake_command
    from agent_core.managed_artifacts import approved_artifact_emission_identities
    from agent_core.step_effects import structured_effect_parse_receipt
    req = _request(tmp_path); calls=[]; monkeypatch.setattr(helper, '_command', _fake_command(calls))
    req['artifact_ref']='output.plan-artifact-bundle'; req['evidence_artifact_ref']='output.plan-artifact-evidence'
    effect = {'produces_artifacts': ['plan-artifact-bundle','plan-artifact-evidence'],
        'produces_observations': ['reference_bundle_valid'], 'explicit_side_effects': ['create_reference_indexes']}
    bindings = [{'artifact_id': key, 'semantic_handle': 'output.'+key} for key in effect['produces_artifacts']]
    for key, value in req['environment'].items(): monkeypatch.setenv(key, value)
    monkeypatch.setenv('BIOMIND_ARTIFACT_IDENTITIES_JSON', json.dumps(approved_artifact_emission_identities(bindings, effect)))
    argv=[]
    for key,value in [('source',req['source']),('output',req['output']),('work-dir',req['work_dir']),
            ('reference-ref',req['reference_ref']),('artifact-ref',req['artifact_ref']),
            ('evidence-artifact-ref',req['evidence_artifact_ref']),*req['tools'].items()]:argv += ['--'+key,str(value)]
    assert helper.main(argv) == 0
    stdout=capsys.readouterr().out
    receipt=structured_effect_parse_receipt(stdout,effect)
    assert not receipt['issues'], receipt
    assert receipt['emission_count'] == 1 and calls == ['bwa','fai','dictionary']
    # The parser remains strict for unrelated/undeclared access references.
    assert structured_effect_parse_receipt(stdout.replace('plan-artifact-bundle','foreign'),effect)['issues']


def test_reference_scope_reads_nested_plan_local_role(tmp_path):
    from agent_core.genomics_reference_bundle import _formal_paths, REFERENCE_BUNDLE_FINAL_URI_PARSER_ID
    from agent_core.task_spec import create_task_spec
    source=tmp_path/'source.fa';source.write_text('>x\nAC\n')
    task=create_task_spec('Prepare reference',task_id='role',base_dir=tmp_path,project_root=tmp_path,
        structured_source={'input_files':[{'role':'reference_fasta','path':str(source),'format':'fasta'}]})
    rows={key:{'logical_artifact_id':key,'canonical_path':str(tmp_path/key),'object_kind':'directory',
        'resource_kind':'immutable_artifact','semantic_view':{'role':role,'format':'directory'}}
        for key,role in [('bundle','reference_indexes'),('evidence','reference_preparation_evidence')]}
    step={'step_id':'s','inputs':['input.reference_fasta'],'outputs':list(rows),
        'produces_artifacts':list(rows),'produces_observations':[{'identity':'reference_bundle_valid',
        'predicate':{'predicate_id':'reference','operator':'boolean_equals','expected':True,
            'parser_id':REFERENCE_BUNDLE_FINAL_URI_PARSER_ID,'failure_disposition':'correction_required','missing_disposition':'inspection_required'}}]}
    plan={'task_id':'role','steps':[step],'plan_artifacts':rows,'current_task':{'task_spec':task}}
    paths=_formal_paths(plan,step)
    assert paths[2:] == ('bundle',tmp_path/'bundle')


def test_all_shared_clients_and_reservations_default_to_50000():
    async def scenario():
        import bio_agents
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        case=PlanLocalRealGraphTests(methodName='test_graph_a_plan_local_directory_happy_path')
        await case.asyncSetUp()
        try:
            calls=bio_agents.init_chat_model.call_args_list
            assert len(calls) >= 4
            assert all(call.kwargs['max_tokens']==50000 for call in calls)
            assert case.agent.generate_requested_output_limits == {'max_tokens':50000}
            assert case.agent._schema2_phase_compactor.policy.output_reserve_tokens==50000
            assert bio_agents.CONSULTANT_FINALIZE_MAX_OUTPUT_TOKENS==50000
        finally:await case.asyncTearDown()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage',['consultant','planner','generate','supervisor','context_compaction','terminal_summary'])
def test_actual_sdk_wire_shared_50000(stage,tmp_path):
    from tests.planner_truncation_acceptance.test_planner_output_policy import wire_call
    body,response,receipt,policy=asyncio.run(wire_call(stage=stage,existing={'max_tokens':50000},trace_root=tmp_path))
    assert body['max_tokens']==50000 and 'max_completion_tokens' not in body
    if stage=='planner':assert policy['input_reserve_used']==50000


def test_planner_local_reference_helper_through_execute_and_commit(tmp_path):
    """Real helper and validators; only model/approval/tool execution leaves substituted."""
    async def scenario():
        import base64, contextlib, io
        import bio_agents
        from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
        from agent_core.skill_resource_binding import resource_env
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.test_recovery50000_reference_helper import helper, _fake_command, REAL_BWA_FIXTURE
        case=PlanLocalRealGraphTests(methodName='test_graph_a_plan_local_directory_happy_path')
        await case.asyncSetUp()
        task='planner-reference-helper'; sid='genomics/wgs-short-read-mapping'
        source=case.root/'source.fa';source.write_bytes(base64.b64decode(REAL_BWA_FIXTURE['reference.fa']))
        tools={}
        for name in ('bwa','samtools','picard','java'):
            p=case.root/name;p.write_text('#!/bin/sh\nexit 99\n');p.chmod(0o700);tools[name]=str(p)
        handle=sid+'#scripts/prepare_reference.py'; counts={};events=[]; executed=[];native=[]
        goal='Prepare a reference bundle in new task-local directories for this exact FASTA: '+str(source)
        persist=case.agent._persist_user_submission
        case.agent._persist_user_submission=lambda tid,_:persist(tid,goal)
        registry=case.agent.skill_manager.get_execution_capability_registry([sid])
        cap=registry['genomics.wgs.short.read.mapping.prepare_reference']
        async def provider(messages,*,stage,node_context=None,**kwargs):
            counts[stage]=counts.get(stage,0)+1
            if stage=='consultant':
                return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'Prepare isolated reference.',
                    'selected_skills':[sid],'task_spec_operations':[
                        {'operation':'set_goal','value':goal,'supporting_evidence':['U1']},
                        {'operation':'upsert_input','input':{'role':'reference_fasta','path':str(source),'format':'fasta','required':True},'supporting_evidence':['U1']}],
                    'unresolved_ambiguities':[],'planner_feedback':None}))
            if stage.startswith('planner'):
                return AIMessage(content=json.dumps({'outcome_type':'SUBMIT_PLAN_FOR_REVIEW','proposed_artifacts':[
                    {'local_ref':'plan.'+role,'semantic_view':{'role':role,'format':'directory','description':role},
                     'artifact_contract':{'object_kind':'directory','lifecycle':'plan_local','materialization_policy_id':'managed_intermediate_v1'}}
                    for role in ['reference_indexes','reference_preparation_evidence']],
                    'steps':[{'step_id':'prepare','title':'Prepare reference bundle','action':'execute','purpose':'analysis',
                        'tool':'bash','operation':'execute','execution_capability_id':'framework.generic.execute','skill_ids':[sid],
                        'inputs':['input.reference_fasta'],'outputs':['plan.reference_indexes','plan.reference_preparation_evidence'],
                        'dependencies':[],'risk_level':'low','workflow_stage_ids':[],'batch_id':'','success_criteria':['Verify prepared reference bundle.'],
                        'produces_observations':deepcopy(cap['produces_observations']),
                        'explicit_side_effects':deepcopy(cap['explicit_side_effects'])}],
                    'validation_checklist':[],'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2'}}))
            assert stage=='generate' and counts[stage]==1,stage
            payload=node_context.llm_payload.to_dict()
            rows=payload['current_focus']['runtime']['current_action_artifact_bindings']
            args={'source':str(source),'reference-ref':'input.reference_fasta',**tools}
            for row in rows:
                if row['direction']=='output':
                    flag='output' if row['role']=='reference_indexes' else 'work-dir'
                    args[flag]=row['expected_path'];args['artifact-ref' if flag=='output' else 'evidence-artifact-ref']=row['semantic_handle']
                    assert not Path(row['expected_path']).parent.exists()
            command=shlex.quote(sys.executable)+' "${'+resource_env(handle)+'}"'+''.join(' --'+key+' '+shlex.quote(value) for key,value in args.items())
            return AIMessage(content='<shell skill_resources="'+handle+'">\n'+command+'\n</shell>',response_metadata={'finish_reason':'stop'})
        async def execution_leaf(path,*,expected_sha256,lifecycle_callback=None,env=None,**kwargs):
            # The externally executed shell/Python adapter is the explicit leaf;
            # all helper logic, publication, reconciliation and Commit are real.
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected_sha256
            approved=Path(path).with_name('approved.sh').read_text().strip()
            argv=shlex.split(approved)
            assert argv[0]==sys.executable and argv[1]=='${'+resource_env(handle)+'}'
            assert json.loads(env['BIOMIND_ARTIFACT_IDENTITIES_JSON'])
            if lifecycle_callback:await lifecycle_callback('PROCESS_STARTED',{'pid':1,'pgid':1})
            out,err=io.StringIO(),io.StringIO()
            with patch.dict(os.environ,env),patch.object(helper,'_command',_fake_command(native)),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                rc=helper.main(argv[2:])
            if lifecycle_callback:await lifecycle_callback('PROCESS_EXITED',{'returncode':rc})
            executed.append({'rc':rc,'stdout':out.getvalue(),'stderr':err.getvalue()})
            return out.getvalue(),err.getvalue(),rc
        case.agent._ainvoke_node_llm=provider
        try:
            runner=case._runner_state(task)
            with patch.object(bio_agents,'run_script_path',execution_leaf):
                app=case.agent._app_for_task(task,runner)
                stream=app.astream(runner,config={'configurable':{'thread_id':task},'recursion_limit':18},stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event);durable=load_plan_state(case.root,task)
                        (tmp_path/'reference-graph.json').write_text(json.dumps({'events':events,'plan':durable,'executed':executed},default=str))
                        if durable['task_state'].get('typed_step_commit_receipts'):break
                finally:await stream.aclose()
            durable=load_plan_state(case.root,task)
            assert counts=={'consultant':1,'planner':1,'generate':1}, (counts,str(events[-1])[-2000:])
            assert len(executed)==1 and executed[0]['rc']==0,executed
            assert native==['bwa','fai','dictionary']
            assert len(durable['task_state'].get('typed_step_commit_receipts') or {})==1,str(events[-1])[-3000:]
            assert len(durable['plan_artifacts'])==2
            attempt=next(iter(durable['provenance_state']['attempts'].values()))
            assert attempt['effect_evidence']['parser_receipt']['status']=='parsed'
            assert len(attempt['artifact_version_ids'])==2
        finally:await case.asyncTearDown()
    asyncio.run(scenario())


def test_standalone_budget_fallbacks_reserve_the_same_output_cap(monkeypatch):
    import types
    from supervisor import LLMReviewer
    from agent_core.workflow.context_compaction import ContextBudgetPolicy
    from agent_core.workflow.recovery_runtime import configured_recovery_budget_policy
    monkeypatch.setitem(sys.modules, 'config.config', types.SimpleNamespace())
    assert LLMReviewer._default_context_policy(ContextBudgetPolicy).output_reserve_tokens == 50000
    monkeypatch.setenv('BIOMIND_RECOVERY_OUTPUT_LIMIT_TOKENS', '100000')
    policy = configured_recovery_budget_policy()
    assert policy.requested_output_limit_tokens == policy.output_reserve_tokens == 50000


def test_freshness_is_semantic_and_survives_task_output_normalization(tmp_path):
    from agent_core.plan_tracker import semantic_plan_hash, plan_semantic_payload, normalize_task_output_materializations
    from tests.test_plan_result_contract_v1 import candidate
    plan = candidate(output=False)
    plan['plan_artifacts'] = {'result': {'object_kind': 'file'}}
    original = semantic_plan_hash(plan)
    assert 'artifact_freshness' not in plan_semantic_payload(plan)
    plan['plan_artifacts']['result']['freshness'] = 'current_attempt'
    assert semantic_plan_hash(plan) != original
    assert plan_semantic_payload(plan)['artifact_freshness'] == {'result': 'current_attempt'}
    plan['task_output_materializations'] = [{'output_ref': 'output.report', 'path': 'report.md',
        'required': False, 'path_policy': 'managed_exact', 'validator': 'file_exists', 'freshness': 'current_attempt'}]
    spec = {'output_contracts': [
        {'output_id': 'result-report', 'semantic_handle': 'output.report', 'artifact_kind': 'file', 'required': True},
        {'output_id': 'root', 'role': 'output_dir', 'artifact_kind': 'directory', 'path': str(tmp_path)}]}
    normalized, issues = normalize_task_output_materializations(plan, spec)
    assert not issues
    assert normalized['task_output_materializations'][0]['freshness'] == 'current_attempt'
