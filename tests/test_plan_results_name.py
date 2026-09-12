"""NAME typed choices through actual Recovery and the guarded tiny executor.

These focused checks supplement the managed graph/Commit/downstream chain;
their initial receipt is synthetic and is not an original business session.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys

import pytest
from langchain_core.messages import AIMessage

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from agent_core.managed_artifacts import _observed_file_metadata, reconcile_runtime_outputs
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import build_causal_failure_record, project_recovery_seed
from agent_core.workflow.recovery_reception import validate_candidate, directive_schema
from agent_core.workflow.recovery_runtime import RecoveryRuntime
from agent_core.workflow.result_resolution import (
    admit_result_resolution, get_result_resolution_request,
    validate_result_organization_action, result_resolution_reconciliation,
)
from agent_core.workflow.runtime_graph import route_recovery_outcome
from agent_core.workflow.stage_contracts import StageContractError
from tests.test_cli_v2_recovery_route import MemoryStore
from tests.test_recovery_projection import _durable, _execute_failure


def name_fixture(root: Path, operation="accept_actual", *, companions=1):
    """Exact synthetic contracts + real small files + causal production packet."""
    root.mkdir(parents=True, exist_ok=True)
    durable = _durable()
    durable["plan_document"] = {"metadata": {"result_contract_version": 1}}
    source = _execute_failure()
    source["payload"]["return_code"] = 0
    execution = source["payload"]["execution_record"]
    execution.update(returncode=0, stdout="synthetic original command completed\n", stderr="",
                     declared_step_effect={"produces_artifacts": []})
    contracts, observed, selections = [], [], []
    for index in range(companions):
        actual = root / f"software-result-{index}.txt"
        actual.write_text(f"sample-A filtered-stage value-{index}\n")
        expected = root / f"required-result-{index}.txt"
        artifact_id = f"result-{index}"
        contracts.append({"artifact_id": artifact_id, "semantic_handle": "output." + artifact_id,
            "expected_path": str(expected), "artifact_kind": "file", "format": "text",
            "required": True, "validation_contract_id": "file_exists",
            "path_policy": "model_selectable" if operation == "accept_actual" else "managed_exact",
            "result_contract_version": 1})
        observed.append({**_observed_file_metadata(actual), "attempt_id": "attempt-1", "current_attempt_owned": True})
        selections.append({"artifact_id": artifact_id, "observed_path": str(actual), "operation": operation})
        execution["declared_step_effect"]["produces_artifacts"].append(artifact_id)
    execution["observed_output_records"] = observed
    execution["action_contract"] = {"controller_output_bindings": contracts}
    execution["runtime_output_reconciliation"] = reconcile_runtime_outputs(contracts, observed, attempt_id="attempt-1", exit_code=0)
    durable["provenance_state"] = {"attempts": {"attempt-1": {"attempt_id": "attempt-1", "action_id": "action-1", "step_id": "step-1"}},
        "actions": {"action-1": {"action_id": "action-1", "controller_output_bindings": contracts}}}
    record = build_causal_failure_record("execute_outcome", source, durable, task_id="task-recovery")
    durable["task_state"]["causal_failure_packets"] = {record["packet_ref"]: record}
    durable["task_state"]["active_failure_packet_ref"] = record["packet_ref"]
    state = {"task_id": "task-recovery", "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"], "failure_source": record["failure_source"], "execute_outcome": source}
    directive = {"schema_version": 1, "directive_id": "name-selection-1", "task_id": "task-recovery",
        "action": "revalidate" if operation == "accept_actual" else "organize_result", "summary": "当前样本和阶段一致，按已检查结果作局部处理。",
        "reason_code": "RESULT_LOCATION_DIFFERENCE", "target_step_id": "step-1", "instruction": "保留原计算结果，仅按已选位置接纳或复制到批准交付名。",
        "evidence_refs": ["execution-1"], "result_selections": selections}
    return durable, state, directive


class SelectionProvider:
    def __init__(self, directive):
        self.directive = directive
        self.requests = []

    def bind_tools(self, schemas):
        self.schemas = schemas
        return self

    async def ainvoke(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        if len(self.requests) == 1:
            return AIMessage(content="我先核对本次执行的结果记录。", tool_calls=[{
                "id": "name-failure-read", "name": "failure_inspect", "args": {}, "type": "tool_call"}])
        assert len(self.requests) == 2, [str(m.content) for m in messages[-3:]]
        # The production projection/tool must make these choices visible;
        # this fake provider does not add records to messages or runtime state.
        wire = json.dumps([str(message.content) for message in messages], ensure_ascii=False)
        for row in self.directive["result_selections"]:
            assert row["artifact_id"] in wire and row["observed_path"] in wire
        return AIMessage(content="结果记录已核对，我会提交当前步骤的局部处理。", tool_calls=[{
            "id": "name-submit", "name": "submit_recovery_directive", "args": deepcopy(self.directive), "type": "tool_call"}])


async def tiny_copy(root, request, durable):
    """Actual executor bytes and exit; no shell beyond the registered fixture."""
    from execution.executor import run_script_path
    action = {"code": request["approved_code"], "recovery_decision_id": request["decision_id"]}
    validate_result_organization_action(action, durable)
    script = root / "approved-local-copy.sh"
    script.write_text(action["code"])
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    allowed = sys.modules["__main__"].OFFLINE_ALLOWED_SCRIPT_PATHS
    allowed.add(str(script))
    try:
        stdout, stderr, rc = await run_script_path(str(script), expected_sha256=digest, profile=False,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
    finally:
        allowed.discard(str(script))
    assert rc == 0, stderr
    return {**action, "attempt_id": "local-attempt-1", "returncode": rc,
            "stdout": stdout, "stderr": stderr, "approved_sha256": digest}


@pytest.mark.parametrize("operation", ["accept_actual", "copy_to_exact"])
def test_actual_recovery_selection_and_local_executor_keep_scope_and_original_evidence(tmp_path, operation):
    async def run():
        from tests.test_cli_v2_display import service_writer_callbacks
        service_writer_callbacks(asyncio.get_running_loop())
        values = []
        for mode in ("compact", "verbose", "off"):
            root = tmp_path / mode
            durable, state, directive = name_fixture(root, operation, companions=2)
            raw = deepcopy(state["execute_outcome"])
            owner = PlainTerminal(io.StringIO(), enabled=mode != "off", verbosity="verbose" if mode == "verbose" else "compact")
            previous = bind_terminal(owner)
            store = MemoryStore(durable)
            provider = SelectionProvider(directive)
            node = Schema2RecoveryNode(data_path=str(root), model=provider, phase_store=PhaseArchiveStore(str(root)),
                evidence_reader=RecoveryEvidenceReader(str(root)), loader=store.load, saver=store.save)
            try:
                result = await node.run(deepcopy(state))
                expected_route = "supervisor" if operation == "accept_actual" else "generate"
                assert route_recovery_outcome(result) == expected_route, result
                request = get_result_resolution_request(store.value, directive["directive_id"])
                assert request["source_attempt_id"] == "attempt-1"
                assert request["source_execution"] == raw["payload"]["execution_record"]
                local = await tiny_copy(root, request, store.value) if operation == "copy_to_exact" else None
                receipt = result_resolution_reconciliation(request, local)
                assert receipt["reconciliation"]["status"] == "accepted"
                assert len(receipt["reconciliation"]["bindings"]) == 2
                assert all(row["required"] is True for row in request["contracts"])
                # Re-entry reads the admitted receipt and rechecks existing
                # results. It neither charges again nor copies a second time.
                saves = len(store.saved)
                replay = await RecoveryRuntime(data_path=str(root), loader=store.load, saver=store.save).run(
                    {**state, "recovery_directive": directive})
                assert replay == result and len(store.saved) == saves
                assert result_resolution_reconciliation(request, local) == receipt
                assert state["execute_outcome"] == raw
                assert len(provider.requests) == 2
                budget = store.value["task_state"].get("typed_recovery_budget", {})
                values.append({"route": expected_route, "provider_calls": 2,
                    "local_processes": int(local is not None), "budget": list(budget.values()),
                    "bindings": len(receipt["reconciliation"]["bindings"]), "source_attempt": receipt["source_attempt_id"]})
                await owner.drain(timeout=1)
            finally:
                bind_terminal(previous)
                owner.close()
        assert values[0] == values[1] == values[2]
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["old_attempt", "wrong_logical", "outside", "changed_content", "exact_accept", "target_conflict", "input_path", "wrong_format", "scientific_failure"])
def test_name_admission_rejects_untrusted_or_conflicting_choices(tmp_path, bad):
    operation = "copy_to_exact" if bad == "target_conflict" else "accept_actual"
    durable, state, directive = name_fixture(tmp_path, operation)
    selection = directive["result_selections"][0]
    execution = state["execute_outcome"]["payload"]["execution_record"]
    if bad == "old_attempt":
        execution["observed_output_records"][0]["attempt_id"] = "old-attempt"
    elif bad == "wrong_logical":
        selection["artifact_id"] = "another-sample-output"
    elif bad == "outside":
        selection["observed_path"] = str(tmp_path.parent / "unrelated.txt")
    elif bad == "changed_content":
        Path(selection["observed_path"]).write_text("wrong sample and stage\n")
    elif bad == "input_path":
        durable["provenance_state"]["actions"]["action-1"]["concrete_artifact_bindings"] = [
            {"direction": "input", "expected_path": selection["observed_path"]}]
    elif bad == "wrong_format":
        Path(selection["observed_path"]).write_text('{"different_object": true}')
        execution["observed_output_records"][0] = {**_observed_file_metadata(Path(selection["observed_path"])),
            "attempt_id": "attempt-1", "current_attempt_owned": True}
        durable["provenance_state"]["actions"]["action-1"]["controller_output_bindings"][0]["format"] = "vcf"
    elif bad == "scientific_failure":
        execution["validation_before_output_reconciliation"] = {"passed": False,
            "errors": ["Approved sample/stage validation rejected the original computation."]}
    elif bad == "exact_accept":
        durable["provenance_state"]["actions"]["action-1"]["controller_output_bindings"][0]["path_policy"] = "managed_exact"
    else:
        (tmp_path / "required-result-0.txt").write_text("different existing result\n")
    seed = project_recovery_seed(state, durable).recovery_seed
    with pytest.raises(StageContractError):
        admit_result_resolution(seed=seed, durable=durable, directive=validate_candidate(directive), state=state)


def test_name_schema_is_typed_and_cannot_carry_code_or_route(tmp_path):
    _, _, directive = name_fixture(tmp_path)
    assert directive_schema()["properties"]["result_selections"]["items"]["additionalProperties"] is False
    directive["result_selections"][0]["code"] = "arbitrary computation"
    with pytest.raises(ValueError):
        validate_candidate(directive)


def test_new_reconciliation_never_guesses_unique_or_ambiguous_name(tmp_path):
    durable, state, _ = name_fixture(tmp_path)
    execution = state["execute_outcome"]["payload"]["execution_record"]
    contracts = execution["action_contract"]["controller_output_bindings"]
    observed = execution["observed_output_records"]
    unique = reconcile_runtime_outputs(contracts, observed, attempt_id="attempt-1", exit_code=0)
    assert unique["status"] == "recovery" and not unique["bindings"]
    assert unique["violations"][0]["code"] == "OUTPUT_SELECTION_REQUIRED"
    other = tmp_path / "second-candidate.txt"
    other.write_text("same format is not the same sample\n")
    observed.append({**_observed_file_metadata(other), "attempt_id": "attempt-1", "current_attempt_owned": True})
    ambiguous = reconcile_runtime_outputs(contracts, observed, attempt_id="attempt-1", exit_code=0)
    assert len(ambiguous["observed_candidates"]) == 2 and not ambiguous["bindings"]


def test_exact_name_with_bad_content_and_nonzero_exit_stays_failure(tmp_path):
    _, state, _ = name_fixture(tmp_path, "copy_to_exact")
    execution = state["execute_outcome"]["payload"]["execution_record"]
    contracts = execution["action_contract"]["controller_output_bindings"]
    broken = tmp_path / "required-result-0.txt"
    broken.write_bytes(b"\x1f\x8btruncated")
    rows = [{**_observed_file_metadata(broken), "attempt_id": "attempt-1", "current_attempt_owned": True}]
    assert reconcile_runtime_outputs(contracts, rows, attempt_id="attempt-1", exit_code=0)["status"] == "recovery"
    assert reconcile_runtime_outputs(contracts, execution["observed_output_records"], attempt_id="attempt-1", exit_code=1)["status"] == "recovery"


@pytest.mark.parametrize('operation,mode,downstream', [(op, mode, True) for op in ('accept_actual', 'copy_to_exact') for mode in ('compact', 'verbose', 'off')] + [(op, 'off', False) for op in ('accept_actual', 'copy_to_exact')])
def test_managed_name_resolution_commits_original_computation_and_feeds_downstream(tmp_path, operation, mode, downstream):
    async def run():
        import bio_agents
        from unittest.mock import patch
        from langchain_core.messages import HumanMessage
        from agent_core.plan_tracker import load_plan_state, save_plan_state, canonical_task_spec_for_state
        from tests.ger_fixture_support import configure_protocol
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.test_cli_v2_display import service_writer_callbacks
        from tests.test_cli_model_progress_integration import artifact
        service_writer_callbacks(asyncio.get_running_loop())
        case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp()
        configure_protocol(case)
        owner = PlainTerminal(io.StringIO(), enabled=mode != 'off', verbosity='verbose' if mode == 'verbose' else 'compact')
        previous = bind_terminal(owner)
        task_id = 'name-managed-synthetic'
        request_text = '生成一个微型样本过滤结果，保留结果文件，再实际读取它报告行数。' + ('交付名必须为 report.md。' if operation == 'copy_to_exact' else '实际文件名可以由模型依据本轮结果选择。')
        plan = case._planner_payload()
        plan['metadata']['result_contract_version'] = 1
        declaration = plan['proposed_artifacts'][0]
        declaration['local_ref'] = 'plan.filtered_table'
        declaration['semantic_view'] = {'role': 'filtered_table', 'format': 'text', 'description': 'Synthetic sample A filtered stage table'}
        declaration['artifact_contract'].update(object_kind='file', required=True,
            path_policy='managed_exact' if operation == 'copy_to_exact' else 'model_selectable')
        if operation == 'copy_to_exact': declaration['artifact_contract']['path'] = 'report.md'
        first, second = plan['steps']
        first.update(title='生成微型过滤表', tool='fixture', outputs=['plan.filtered_table'], success_criteria=['当前样本过滤结果实际存在。'])
        second.update(title='读取结果报告行数', tool='fixture', inputs=['plan.filtered_table'], outputs=[], purpose='validation',
            produces_observations=[{'identity': 'result_rows', 'evidence_contract': {'required_fields': ['value']}}],
            success_criteria=['实际读取上一步接纳路径并报告结果行数。'])
        if not downstream:
            plan['steps'] = [first]
        plan['validation_checklist'] = [{'check_id': 'filtered-result', 'description': '保留正确样本和阶段的结果。'}]
        counts = {'consultant': 0, 'planner': 0, 'generate': 0}
        captures, approvals, executions, events = [], [], [], []
        expected_path = actual_path = logical_id = ''
        selection_provider = SelectionProvider({})
        async def provider(messages, *, stage, node_context=None, **kwargs):
            nonlocal expected_path, actual_path, logical_id
            if stage == 'recovery':
                import re
                evidence = re.search(r'exec-attempt_[a-z0-9]+', '\n'.join(str(m.content) for m in messages))
                assert evidence, 'Current execution evidence must be visible to the Recovery provider'
                selection_provider.directive['evidence_refs'] = [evidence.group(0)]
                return await selection_provider.ainvoke(messages, **kwargs)
            assert stage in counts, 'Unexpected provider stage ' + stage
            counts[stage] += 1
            captures.append({'stage': stage, 'messages': [str(m.content) for m in messages]})
            if stage == 'consultant':
                assert counts[stage] == 1
                return AIMessage(content=json.dumps({'action_type': 'submit_planning_handoff', 'summary': request_text,
                    'selected_skills': [case.SKILL_ID], 'task_spec_operations': [{'operation': 'set_goal', 'value': request_text,
                    'supporting_evidence': ['U1']}], 'unresolved_ambiguities': [], 'planner_feedback': None}))
            if stage == 'planner':
                assert counts[stage] == 1, 'No revised plan is needed for an accepted local result'
                return AIMessage(content=json.dumps(plan))
            payload = node_context.llm_payload.to_dict()
            runtime = (payload.get('current_focus') or {}).get('runtime') or {}
            environment = runtime.get('artifact_environment') or runtime.get('current_action_artifact_bindings') or []
            assert any(getattr(m, 'name', '') == 'approved_effect_emission_template' for m in messages)
            if counts[stage] == 1:
                row = next(r for r in environment if str(r.get('semantic_handle') or '').startswith('output.'))
                expected_path = row.get('expected_path') or row.get('path')
                logical_id = row['semantic_handle'].removeprefix('output.')
                actual_path = str(Path(expected_path).parent / 'software-filtered.txt')
                durable = load_plan_state(case.root, task_id)
                selection_provider.directive = {'schema_version': 1, 'directive_id': 'managed-name-1', 'task_id': task_id,
                    'action': 'organize_result' if operation == 'copy_to_exact' else 'revalidate',
                    'summary': '本轮样本和过滤阶段符合要求，只处理结果位置。', 'reason_code': 'RESULT_LOCATION_DIFFERENCE',
                    'target_step_id': durable['current_step_id'], 'instruction': '保留原主计算，只接纳已核对的结果或复制到指定交付名。',
                    'evidence_refs': ['current-execution'], 'result_selections': [{'artifact_id': logical_id,
                        'observed_path': actual_path, 'operation': operation}]}
                body = "set -e\n/usr/bin/python3 <<'PY'\nfrom pathlib import Path\nimport json\n" + \
                    f"# Approved output location: {expected_path}\np=Path({actual_path!r})\np.parent.mkdir(parents=True,exist_ok=True)\np.write_text('sample\\tvalue\\nA\\t1\\n')\n" + \
                    "print('BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'schema_version':1,'produces_artifacts':[" + \
                    repr({'identity': logical_id, 'path': actual_path}) + "]}))\nPY"
            else:
                assert counts[stage] == 2
                row = next(r for r in environment if str(r.get('semantic_handle') or '').startswith('input.'))
                path = row.get('expected_path') or row.get('path')
                assert path == (actual_path if operation == 'accept_actual' else expected_path), row
                assert str(path) in '\n'.join(str(m.content) for m in messages)
                body = "/usr/bin/python3 <<'PY'\nimport json\nfrom pathlib import Path\n" + \
                    f"n=len(Path({path!r}).read_text().splitlines())-1\nassert n==1\n" + \
                    "print('BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'schema_version':1,'produces_observations':[{'identity':'result_rows','value':n}]}))\nPY"
            return AIMessage(content='<shell>\n'+body+'\n</shell>')
        case.agent._ainvoke_node_llm = provider
        case.agent._ainvoke_llm = provider
        case.agent.middleware.review_code = lambda code, language, **kw: (approvals.append(code) or 'approve', code)
        async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
            from execution.executor import run_script_path
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
            allow = sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
            allow.add(str(path))
            try:
                result = await run_script_path(path, expected_sha256=expected_sha256, lifecycle_callback=lifecycle_callback,
                    profile=False, env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}, managed_log_paths=kwargs.get('managed_log_paths'))
            finally: allow.discard(str(path))
            executions.append({'stdout':result[0], 'stderr':result[1], 'returncode':result[2], 'code':Path(path).read_text()})
            return result
        try:
            durable, _, _ = case.agent._persist_user_submission(task_id, request_text)
            admission = {'schema_version':1,'request_kind':'execute','execution_scope':'managed','managed_intent_explicit':True,
                'task_mode':'managed','retrieval_mode':'kb','confidence':1.0,'admission_status':'managed_execution_admitted',
                'reason':'Synthetic local file fixture','decision_source':'test_fixture'}
            durable.setdefault('task_state', {})['intent_admission'] = admission
            save_plan_state(case.root, task_id, durable)
            runner = {'messages':[HumanMessage(content=request_text)],'task_id':task_id,'session_id':task_id,
                'project_id':case.agent.default_project_id,'conversation_evidence':durable['conversation_evidence'],
                'task_spec':canonical_task_spec_for_state(durable),'task_revision':0,'selected_skills':[],
                'resolved_bindings':{},'consultation_state':{},'planner_feedback':{},'request_intent':admission,
                'intent_admission':admission,'managed_intent_admitted':True}
            with patch.object(bio_agents, 'run_script_path', execute):
                app = case.agent._app_for_task(task_id, runner)
                stream = app.astream(runner, config={'configurable':{'thread_id':task_id},'recursion_limit':25}, stream_mode='updates')
                try:
                    async for event in stream: events.append(event)
                finally: await stream.aclose()
            saved = load_plan_state(case.root, task_id)
            compact_events = [{node: {k:v for k,v in value.items() if k.endswith('_outcome')} for node,value in event.items()} for event in events]
            artifact('name-managed-'+operation+'-'+mode+'-'+str(downstream)+'.json', {'source':'isolated synthetic production graph','events':compact_events,
                'calls':counts,'recovery_calls':len(selection_provider.requests),'executions':executions,'approvals':len(approvals),
                'result_receipts':saved['task_state'].get('result_resolution_receipts',{})})
            assert counts == {'consultant':1,'planner':1,'generate':2 if downstream else 1}, compact_events
            assert len(selection_provider.requests) == 2
            assert len(executions) == len(approvals) == 1 + int(downstream) + int(operation == 'copy_to_exact')
            assert len(saved['task_state'].get('typed_step_commit_receipts',{})) == 1 + int(downstream), compact_events
            assert len(saved['task_state'].get('typed_final_audit_receipts',{})) == 1, compact_events
            receipt = saved['task_state']['result_resolution_receipts']['managed-name-1']
            attempts = saved['provenance_state']['attempts']
            source_attempt = attempts[receipt['source_attempt_id']]
            assert Path(source_attempt['stdout_path']).read_text() == executions[0]['stdout']
            assert Path(source_attempt['approved_script']['path']).read_text() == approvals[0]
            assert hashlib.sha256(approvals[0].encode()).hexdigest() == source_attempt['approved_script']['sha256']
            assert source_attempt['effect_evidence']['parser_receipt']['raw_stdout_sha256'] == hashlib.sha256(executions[0]['stdout'].encode()).hexdigest()
            head = next(saved['provenance_state']['artifacts'][ref] for ref in saved['provenance_state']['artifact_heads'].values() if saved['provenance_state']['artifacts'][ref].get('logical_artifact_id') == logical_id)
            assert head['producer_attempt_id'] == receipt['source_attempt_id']
            assert Path(actual_path).read_text() == 'sample\tvalue\nA\t1\n'
            if not downstream:
                from agent_core.workflow.outcomes import CommitStepOutcome
                from agent_core.workflow.result_resolution import read_committed_result_resolution
                commit = CommitStepOutcome.from_mapping(next(e['commit_step']['commit_step_outcome'] for e in events if 'commit_step' in e))
                request, held, view = read_committed_result_resolution(saved, 'managed-name-1', commit, data_path=str(case.root))
                forged = deepcopy(saved)
                token = commit.to_dict()['payload']['semantic_consumption_ref']
                forged['task_state']['semantic_completion_consumptions'][token]['scope']['attempt_id'] = 'unrelated-completed-attempt'
                with pytest.raises(StageContractError, match='RESULT_RESOLUTION_COMMIT_CONSUMPTION_MISMATCH'):
                    read_committed_result_resolution(forged, 'managed-name-1', commit, data_path=str(case.root))
                if operation == 'copy_to_exact':
                    forged = deepcopy(saved)
                    forged['provenance_state']['attempts'][held['local_attempt_id']]['completion_commit_ref'] = 'another-commit'
                    with pytest.raises(StageContractError, match='RESULT_LOCAL_ORGANIZATION_NOT_COMMITTED'):
                        read_committed_result_resolution(forged, 'managed-name-1', commit, data_path=str(case.root))
            if operation == 'copy_to_exact':
                assert Path(expected_path).read_bytes() == Path(actual_path).read_bytes()
                assert receipt['local_attempt_id'] and receipt['local_attempt_id'] != receipt['source_attempt_id']
                local_attempt = attempts[receipt['local_attempt_id']]
                assert source_attempt['status'] == local_attempt['status'] == 'COMPLETED'
                assert local_attempt['computational_source_attempt_id'] == receipt['source_attempt_id']
                assert local_attempt['completion_commit_ref'] in saved['task_state']['typed_step_commit_receipts']
                assert Path(local_attempt['stdout_path']).read_text() == executions[1]['stdout']
                assert Path(local_attempt['approved_script']['path']).read_text() == approvals[1]
                for attempt in (source_attempt, local_attempt):
                    validation = saved['provenance_state']['validations'][attempt['validation_evidence_ref']]
                    assert validation['attempt_id'] == attempt['attempt_id'] and validation['action_id'] == attempt['action_id']
                    assert validation['status'] in ('VALID', 'VALID_WITH_WARNINGS')
            if not downstream:
                changed = Path(expected_path if operation == 'copy_to_exact' else actual_path)
                changed.write_text('changed after the committed verification')
                with pytest.raises(StageContractError, match='RESULT_REGISTERED_COPY_CHANGED|RESULT_SELECTED_FILE_CHANGED'):
                    read_committed_result_resolution(saved, 'managed-name-1', commit, data_path=str(case.root))
        finally:
            bind_terminal(previous)
            owner.close()
            await case.asyncTearDown()
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['accept_actual', 'copy_to_exact'])
@pytest.mark.parametrize('generic_format', ['text', 'file', 'data', 'output'])
def test_explicit_file_exists_allows_empty_name_resolution_without_accepting_broken_format(tmp_path, operation, generic_format):
    async def run():
        root = tmp_path / 'empty'
        durable, state, directive = name_fixture(root, operation)
        durable['provenance_state']['actions']['action-1']['controller_output_bindings'][0]['format'] = generic_format
        actual = Path(directive['result_selections'][0]['observed_path'])
        actual.write_bytes(b'')
        execution = state['execute_outcome']['payload']['execution_record']
        execution['observed_output_records'][0] = {**_observed_file_metadata(actual), 'attempt_id':'attempt-1', 'current_attempt_owned':True}
        seed = project_recovery_seed(state, durable).recovery_seed
        parsed = validate_candidate(directive)
        request = admit_result_resolution(seed=seed, durable=durable, directive=parsed, state=state)
        durable['task_state'].setdefault('typed_recovery_receipts',{})[directive['directive_id']] = {'result_resolution_request':request}
        local = await tiny_copy(root, request, durable) if operation == 'copy_to_exact' else None
        receipt = result_resolution_reconciliation(request, local)
        assert receipt['reconciliation']['status'] == 'accepted'
        assert receipt['reconciliation']['bindings'][0]['content_valid'] is True
        assert execution['observed_output_records'][0]['content_valid'] is False
    asyncio.run(run())


def test_local_copy_conflict_and_response_loss_never_overwrite_or_register_early(tmp_path):
    async def run():
        from execution.executor import run_script_path
        durable, state, directive = name_fixture(tmp_path, 'copy_to_exact', companions=2)
        request = admit_result_resolution(seed=project_recovery_seed(state, durable).recovery_seed, durable=durable,
            directive=validate_candidate(directive), state=state)
        durable['task_state'].setdefault('typed_recovery_receipts',{})[directive['directive_id']] = {'result_resolution_request':request}
        target = Path(request['selections'][1]['target_path'])
        target.write_text('different existing result')
        script = tmp_path / 'conflicting-local-copy.sh'; script.write_text(request['approved_code'])
        allow = sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS; allow.add(str(script))
        try:
            stdout, stderr, rc = await run_script_path(str(script), expected_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                profile=False, env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'})
        finally: allow.discard(str(script))
        assert rc != 0
        assert target.read_text() == 'different existing result'
        with pytest.raises(StageContractError, match='RESULT_LOCAL_EXECUTION_NOT_CONFIRMED'):
            result_resolution_reconciliation(request, {'attempt_id':'local-1','returncode':rc,'code':request['approved_code'],
                'recovery_decision_id':request['decision_id'],'stdout':stdout,'stderr':stderr})
        assert not durable['task_state'].get('result_resolution_receipts')
        # The fixture created this conflict. After a user resolves it, a
        # resumed admitted action checks its already copied first companion.
        target.unlink()
        first = Path(request['selections'][0]['target_path']); before = first.stat().st_mtime_ns
        local = await tiny_copy(tmp_path, request, durable)
        receipt = result_resolution_reconciliation(request, local)
        assert receipt['reconciliation']['status'] == 'accepted'
        assert first.stat().st_mtime_ns == before
        all_before = [Path(r['target_path']).stat().st_mtime_ns for r in request['selections']]
        again = await tiny_copy(tmp_path, request, durable)
        assert result_resolution_reconciliation(request, again) == receipt
        assert [Path(r['target_path']).stat().st_mtime_ns for r in request['selections']] == all_before
    asyncio.run(run())
