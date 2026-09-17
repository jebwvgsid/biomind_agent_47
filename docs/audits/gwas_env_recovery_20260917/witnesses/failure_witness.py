"""Current-behavior witnesses, NOT production repairs or scientific replay.

Run through tests/offline_managed_bootstrap.py in an explicit /tmp source
snapshot. FAILURE_SNAPSHOT points at failure_capture.py's exact JSON copies.
The only executor substitute supplies recorded process output plus the real
reconciler's validation result; no process, provider, state writer or installer
is invoked. Production reconciliation, TypedExecute, causal-packet builder,
managed-stop scope projection and CLI text all run unchanged.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path


def inputs():
    root = Path(os.environ["FAILURE_SNAPSHOT"])
    state = json.loads((root / "plan_state.json").read_text())
    record = json.loads((root / "execution_records.jsonl").read_text().splitlines()[0])
    return root, state, record


def result(name, data):
    root = Path(os.environ["FAILURE_RESULTS"])
    root.mkdir(parents=True, exist_ok=True)
    (root / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def test_recorded_empty_sample_is_rejected_by_actual_reconciler():
    from agent_core.managed_artifacts import reconcile_runtime_outputs
    _, state, record = inputs()
    attempt = state["provenance_state"]["attempts"][record["attempt_id"]]
    original = record["runtime_output_reconciliation"]
    actual = reconcile_runtime_outputs(attempt["managed_output_contracts"], original["observed_candidates"],
        attempt_id=record["attempt_id"], exit_code=record["exit_code"])
    assert actual == original
    assert actual["status"] == "recovery" and actual["exit_code"] == 0
    assert [row["code"] for row in actual["violations"]] == ["OBSERVED_ARTIFACT_CONTENT_INVALID"]
    result("reconciliation", {"matched_recorded_result": actual == original, "result": actual})


def test_actual_typed_and_causal_functions_reproduce_rc_zero_misclassification():
    from agent_core.managed_artifacts import reconcile_runtime_outputs
    from agent_core.workflow.execution import TypedExecute
    from agent_core.workflow.recovery_projection import build_causal_failure_record
    _, state, recorded = inputs()
    attempt = state["provenance_state"]["attempts"][recorded["attempt_id"]]
    rejection = reconcile_runtime_outputs(attempt["managed_output_contracts"],
        recorded["runtime_output_reconciliation"]["observed_candidates"], attempt_id=recorded["attempt_id"], exit_code=recorded["exit_code"])
    evidence = deepcopy(recorded)
    # ExecutionRecord serialization omits the graph-local validation field.
    # Recreate that producer field from the unchanged real reconciliation,
    # with the same shape/code present in the recorded producer_failure_index.
    evidence["returncode"] = recorded["exit_code"]
    evidence["validation"] = {"passed": False,
        "errors": [row["code"] + ": " + row["message"] for row in rejection["violations"]]}
    async def recorded_executor(_state):
        return {"execution_evidence": evidence}
    invocation = {"task_id": state["task_id"], "plan_id": state["plan_id"], "plan_version": state["plan_version"],
        "current_step_id": state["current_step_id"], "task_mode": "managed"}
    typed = asyncio.run(TypedExecute(executor=recorded_executor).run(invocation))["execute_outcome"]
    assert typed["type"] == "execution_failed"
    assert typed["payload"]["return_code"] == 0
    assert typed["payload"]["execution_record"]["failure_code"] == "OBSERVED_ARTIFACT_CONTENT_INVALID"
    assert "error_code" not in typed["payload"] and not evidence.get("error_code")
    causal = build_causal_failure_record("execute_outcome", typed, state)
    packet = causal["failure_packet"]
    assert packet["error_code"] == "EXECUTION_FAILED"
    index = next(row["metadata"] for row in packet["diagnostic_facts"] if row["kind"] == "producer_failure_index")
    assert index["validation_acceptance"] is False
    assert index["process"]["return_code"] == 0
    assert any(row["code"] == "OBSERVED_ARTIFACT_CONTENT_INVALID" for row in index["checks"])
    result("causal_mapping", {"typed_type": typed["type"], "typed_summary": typed["summary"],
        "returncode": 0, "producer_failure_code": evidence["failure_code"], "failure_packet": packet,
        "boundary": "Recorded executor return; real reconciler, TypedExecute and build_causal_failure_record. No execution/Commit."})


def test_actual_current_stop_and_cli_keep_delivery_but_mislabel_original_failure():
    from agent_core.cli_lifecycle import terminal_text
    from agent_core.workflow.managed_stop import current_managed_stop, projection, report_delivery_status
    _, state, _ = inputs()
    stop = current_managed_stop(state)
    assert stop and stop["origin"]["returncode"] == 0
    projected = projection(stop)
    text = terminal_text(projected["status"], projected["terminal_diagnostic"])
    assert "脚本执行失败，退出码 0" in text
    assert "RECOVERY_SEED_CAPACITY_EXCEEDED" in text
    assert "chars=32097" not in text and "limit=32000" not in text
    assert "调查累计次数：未知" in text
    assert stop["stop_reason"]["model_turns"] == stop["stop_reason"]["tool_calls"] == 0
    assert report_delivery_status(stop["report_receipts"]) == "complete"
    assert projected["status"] == "failed" and stop["scientific_completion"] == "not_completed"
    assert not state["task_state"].get("typed_step_commit_receipts")
    for path in stop["report_paths"].values():
        assert path in text
    result("terminal_projection", {"current_stop_id": stop["stop_id"], "status": projected["status"],
        "origin": stop["origin"], "stop_reason": stop["stop_reason"], "recovery_usage": stop["recovery_usage"],
        "scientific_completion": stop["scientific_completion"], "delivery": report_delivery_status(stop["report_receipts"]),
        "cli_text": text, "boundary": "Pure current receipt projection; no writer or original-session access."})
