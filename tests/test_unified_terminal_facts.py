"""Exact stopped-step ownership and current diagnostic facts, no live session."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from agent_core.cli_lifecycle import delivery_text, terminal_text
from agent_core.plan_tracker import build_plan_state, load_plan_state, save_plan_state
from agent_core.workflow.managed_stop import (
    _project_current_stop_facts, _save, build_managed_stop, current_managed_stop, projection,
)


def fixture(tmp_path):
    task_id = "terminal-facts"
    durable = build_plan_state(task_id,
        "- [x] Step 1: Input check | Action: EXECUTE | Skills: [] | Done: accepted input\n"
        "- [>] Step 2: Prepare reference | Action: EXECUTE | Skills: [] | Done: verified indexes\n"
        "- [ ] Step 3: Mapping | Action: EXECUTE | Skills: [] | Done: verified mapping\n")
    before, current, later = durable["steps"]
    before["status"], current["status"], later["status"] = "passed", "running", "pending"
    durable["current_step_id"] = current["step_id"]
    scope = {"task_id": task_id, "plan_id": durable["plan_id"], "plan_version": durable["plan_version"],
             "step_id": current["step_id"], "attempt_id": "attempt-reference", "episode_id": "episode-reference"}
    packet = {**scope, "attempt_ref": scope["attempt_id"], "failure_id": "failed-reference", "failure_type": "execution_failed",
        "failure_owner": "GENERATED_ACTION", "source": "execute_outcome", "error_code": "EXECUTION_FAILED", "summary": "Recorded failed execution",
        "diagnostic_facts": [{"kind": "return_code", "metadata": {"return_code": 1}}]}
    durable["task_state"].update(active_failure_packet_ref="packet-reference", active_execution={}, current_attempt="",
        causal_failure_packets={"packet-reference": {"source_kind": "execute_outcome", "source_fingerprint": scope["episode_id"],
            "failure_packet": packet, "failure_source": {"type": "execution_failed", "payload": {}}}})
    durable["provenance_state"]["attempts"][scope["attempt_id"]] = {**scope, "action_id": "action-reference",
        "status": "EVIDENCE_COMMITTED", "returncode": 1, "execution_record_id": "exec-reference"}
    durable["provenance_state"]["actions"]["action-reference"] = {**scope, "action_id": "action-reference", "normalized_action": {
        "runtime_binding_manifest": {"bindings": []}, "runtime_binding_manifest_hash": "selected-runtime-manifest"}}
    save_plan_state(tmp_path, task_id, durable)
    durable = load_plan_state(tmp_path, task_id)
    boundary = {"node": "recovery", "status": "blocked", "outcome": {"type": "blocked", "scope": scope,
        "summary": "Formal recovery stopped", "payload": {"error_code": "RECOVERY_NO_PROGRESS_LIMIT"}}}
    stdout = "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({"explicit_side_effects": [
        {"identity": "create_reference_indexes", "status": "blocked", "missing_commands": ["picard"]}]}) + "\n"
    values = {"execute_outcome": {"payload": {"execution_record": {**scope, "returncode": 1, "raw_stdout": stdout}}}}
    stop = build_managed_stop(task_id, values, durable, boundary)
    return durable, stop, [row["step_id"] for row in durable["steps"]]


def test_post_execution_stop_saves_exact_step_and_preserves_other_steps_and_attempt(tmp_path):
    durable, stop, ids = fixture(tmp_path)
    before = deepcopy(durable["provenance_state"])
    _save(tmp_path, stop, loader=load_plan_state, saver=save_plan_state)
    after = load_plan_state(tmp_path, durable["task_id"])
    assert after["task_state"]["status"] == "blocked"
    assert [row["status"] for row in after["steps"]] == ["passed", "blocked", "pending"]
    assert after["provenance_state"] == before
    assert stop["execution"]["step_status"] == "blocked"
    assert next(row for row in stop["stages"] if row["step_id"] == ids[1])["status"] == "blocked"


@pytest.mark.parametrize("change", ["new_attempt", "step_changed", "plan_changed", "attempt_wrong_plan", "running_attempt", "active_owner", "committed", "commit_receipt"])
def test_terminal_writer_never_stops_unowned_or_committed_step(tmp_path, change):
    durable, stop, ids = fixture(tmp_path)
    attempt = durable["provenance_state"]["attempts"][stop["scope"]["attempt_id"]]
    if change == "new_attempt":
        durable["provenance_state"]["attempts"]["new-attempt"] = {**attempt, "attempt_id": "new-attempt", "status": "RUNNING"}
    elif change == "step_changed":
        durable["current_step_id"] = ids[2]
    elif change == "plan_changed":
        stop["scope"]["plan_version"] = "stale-version"
    elif change == "attempt_wrong_plan":
        attempt["plan_id"] = "another-plan"
    elif change == "running_attempt":
        attempt["status"] = "RUNNING"
    elif change == "active_owner":
        durable["task_state"]["active_execution"] = {"step_id": ids[1], "attempt_id": attempt["attempt_id"]}
        durable["task_state"]["current_attempt"] = attempt["attempt_id"]
    elif change == "commit_receipt":
        durable["task_state"]["typed_step_commit_receipts"] = {"commit": {"outcome": {"payload": {
            "completed_step_id": ids[1], "commit_id": "valid-commit", "semantic_consumption_ref": "consumption"}}}}
        durable["task_state"]["semantic_completion_consumptions"] = {"consumption": {"commit_id": "valid-commit", "scope": deepcopy(stop["scope"])}}
    else:
        durable["steps"][1]["status"] = "passed"
    save_plan_state(tmp_path, durable["task_id"], durable)
    before = load_plan_state(tmp_path, durable["task_id"])
    if change in {"new_attempt", "step_changed", "plan_changed"}:
        with pytest.raises(ValueError, match="MANAGED_STOP_STALE_SCOPE"):
            _save(tmp_path, stop, loader=load_plan_state, saver=save_plan_state)
    else:
        _save(tmp_path, stop, loader=load_plan_state, saver=save_plan_state)
    after = load_plan_state(tmp_path, durable["task_id"])
    assert after["steps"] == before["steps"]
    assert after["provenance_state"] == before["provenance_state"]


@pytest.mark.parametrize("case,resolved", [("later_same", True), ("earlier_same", False), ("different_ref", False), ("different_episode", False), ("mutable_path", False)])
def test_only_later_same_immutable_evidence_resolves_historical_read_error(tmp_path, case, resolved):
    durable, stop, _ = fixture(tmp_path)
    ref = "/mutable/file" if case == "mutable_path" else "inspection:sha256:" + "a" * 64
    failure = {"request_id": "bad-read", "source_ref": ref, "tool": "step.inspect", "error": {"code": "INSPECTION_REF_KIND_MISMATCH"}}
    success = {"request_id": "good-read", "source_ref": "inspection:sha256:" + "b" * 64 if case == "different_ref" else ref,
               "tool": "evidence.read", "error": {}, "content_sha256": "c" * 64}
    stop["inspection_failures"] = [{"tool_call_id": "bad-read", "error_code": failure["error"]["code"], "evidence_ref": ref}]
    original = deepcopy(stop["inspection_failures"])
    durable["task_state"]["typed_recovery_receipts"] = {"decision": {
        "source_fingerprint": "other-episode" if case == "different_episode" else stop["scope"]["episode_id"],
        "causal_observation_receipts": [success, failure] if case == "earlier_same" else [failure, success]}}
    _project_current_stop_facts(durable, stop)
    assert stop["inspection_failures"] == original
    assert bool(stop["resolved_inspection_failures"]) is resolved
    assert bool(stop["current_inspection_failures"]) is not resolved
    text = terminal_text("blocked", projection(stop)["terminal_diagnostic"])
    assert ("恢复取证问题" not in text) is resolved
    if resolved:
        assert stop["resolved_inspection_failures"][0]["resolved_by"]["request_id"] == "good-read"
        assert "错误记录仍保留" in text


def test_cli_reports_exact_observed_command_gap_without_claiming_global_absence(tmp_path):
    durable, stop, _ = fixture(tmp_path)
    projected = projection(stop)["terminal_diagnostic"]
    text = terminal_text("blocked", projected)
    assert "missing_commands=[picard]" in text
    assert "该次脚本报告" in text and "正式 runtime bindings 为空" in text
    assert "已有环境是否能提供所需能力仍待验证" in text
    assert "未安装" not in text
    assert "验证已声明的环境与所需命令" in text
    stop["execution"]["stdout"] = "a model says picard missing"
    _project_current_stop_facts(durable, stop)
    assert "missing_commands" not in stop["runtime_diagnostics"]


def test_delivery_paths_require_current_successful_receipt(tmp_path):
    protected, current = str(tmp_path / "report.md"), str(tmp_path / "terminal" / "report.md")
    facts = {"required_output_status": "conflict", "report_paths": {"report.md": protected},
        "report_receipts": {"report.md": {"status": "failed", "reason": "RUNTIME_REPORT_EXISTING_FILE_PROTECTED"}},
        "internal_diagnostic_delivery": {"status": "complete", "paths": {"report.md": current},
            "receipts": {"report.md": {"status": "created", "sha256": "real-publication-hash"}}}}
    text = delivery_text(facts)
    assert "目标文件受保护，未覆盖" in text and current in text
    assert "本次报告 report.md：" not in text
    facts["internal_diagnostic_delivery"]["receipts"]["report.md"]["status"] = "failed"
    assert current not in delivery_text(facts)


def test_recorded_incident_replay_uses_real_ordered_receipts_without_reopening_session():
    fixture = json.loads((Path(__file__).parent / "fixtures/unified_terminal_recorded.json").read_text())
    stop, durable = fixture["stop"], fixture["durable"]
    original = deepcopy(stop["inspection_failures"])
    _project_current_stop_facts(durable, stop)
    text = terminal_text("blocked", projection(stop)["terminal_diagnostic"])
    assert "missing_commands=[picard]" in text
    assert "正式 runtime bindings 为空" in text
    assert "恢复取证问题" not in text
    assert len(stop["resolved_inspection_failures"]) == 1
    assert stop["inspection_failures"] == original
    assert stop["internal_diagnostic_delivery"]["paths"]["report.md"] in text
    assert "目标文件受保护，未覆盖" in text


def test_real_terminal_graph_preserves_old_report_and_exposes_new_internal_report(tmp_path):
    from tests.test_common_finalization_workflow import run_execution_case
    async def scenario():
        paths = []
        def inject(_agent, fixture):
            path = tmp_path / "task_outputs" / fixture.task_id / "report.md"
            path.write_text("Preserve preexisting user report.\n")
            paths.append((path, hashlib.sha256(path.read_bytes()).hexdigest()))
        fixture, agent, graph, config, recovery, summary, result, _ = await run_execution_case(
            tmp_path, summary_policy={"enabled": False}, inject=inject)
        durable = fixture.reload()
        stop = current_managed_stop(durable)
        current = next(row for row in durable["steps"] if row["step_id"] == stop["scope"]["step_id"])
        assert current["status"] == "blocked"
        assert stop["internal_diagnostic_delivery"]["status"] == "complete"
        internal = stop["internal_diagnostic_delivery"]["paths"]["report.md"]
        assert Path(internal).is_file()
        assert hashlib.sha256(paths[0][0].read_bytes()).hexdigest() == paths[0][1]
        assert internal in terminal_text("blocked", projection(stop)["terminal_diagnostic"])
        assert not summary.requests
        await agent._finalize_managed_terminal(fixture.task_id, graph.get_state(config))
        assert current_managed_stop(fixture.reload())["stop_id"] == stop["stop_id"]
    asyncio.run(scenario())
