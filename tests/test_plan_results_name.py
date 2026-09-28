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
from agent_core.managed_artifacts import _observed_file_metadata, reconcile_runtime_outputs
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.workflow.recovery_projection import build_causal_failure_record, project_recovery_seed
from agent_core.workflow.recovery_reception import validate_candidate, directive_schema
from agent_core.workflow.result_resolution import (
    admit_result_resolution, get_result_resolution_request,
    validate_result_organization_action, result_resolution_reconciliation,
)
from agent_core.workflow.stage_contracts import StageContractError
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
                "id": "name-failure-read", "name": "failure_inspect", "args": {}, "type": "tool_call"}], response_metadata={"finish_reason":"tool_calls"})
        assert len(self.requests) == 2, [str(m.content) for m in messages[-3:]]
        # The production projection/tool must make these choices visible;
        # this fake provider does not add records to messages or runtime state.
        wire = json.dumps([str(message.content) for message in messages], ensure_ascii=False)
        for row in self.directive["result_selections"]:
            assert row["artifact_id"] in wire and row["observed_path"] in wire
        return AIMessage(content="结果记录已核对，我会提交当前步骤的局部处理。", tool_calls=[{
            "id": "name-submit", "name": "submit_recovery_directive", "args": deepcopy(self.directive), "type": "tool_call"}], response_metadata={"finish_reason":"tool_calls"})


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
    # An existence-only contract cannot promise compressed-content validity.
    contracts[0]["validation_contract_id"] = "file_nonempty"
    broken = tmp_path / "required-result-0.txt"
    broken.write_bytes(b"\x1f\x8btruncated")
    rows = [{**_observed_file_metadata(broken), "attempt_id": "attempt-1", "current_attempt_owned": True}]
    assert reconcile_runtime_outputs(contracts, rows, attempt_id="attempt-1", exit_code=0)["status"] == "recovery"
    assert reconcile_runtime_outputs(contracts, execution["observed_output_records"], attempt_id="attempt-1", exit_code=1)["status"] == "recovery"




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
        # Acceptance proves this explicit existence contract only. Keep the
        # empty-file content observation truthful instead of promoting it.
        assert receipt['reconciliation']['bindings'][0]['content_valid'] is False
        assert receipt['reconciliation']['bindings'][0]['validation_scope'] == 'existence_and_kind'
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
