"""Offline current-attempt diagnostic reads; no execution, provider or acceptance."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_tools import (
    build_recovery_inspection_registry, RecoveryLocalToolDispatcher, RecoveryToolError,
)
from tests.test_recovery_agent_loop import _seed_fixture


def fixture(tmp_path):
    _, seed = _seed_fixture()
    seed = replace(seed, current_step={**dict(seed.current_step), "inputs": ["annotated_vcf"],
        "outputs": ["ann_summary"]}, dependency_outcomes=({"step_id": "upstream",
            "status": "completed", "summary": "Previously accepted upstream fixture."},))
    packet = seed.failure_packet
    output = tmp_path / "旧目录" / "ann_summary.tsv"
    output.parent.mkdir(); output.write_text("old bytes, production not established\n")
    upstream = output.with_name("annotated.vcf"); upstream.write_text("accepted upstream fixture\n")
    state = {"task_id": packet.task_id, "plan_id": seed.plan_spec_ref.plan_id,
        "plan_version": seed.plan_spec_ref.plan_version, "current_step_id": packet.step_id,
        "plan_revision": 1, "current_plan": {"plan_spec": {}}, "task_state": {},
        "current_task": {"task_spec": {"revision": 1, "goal": "Diagnose one failed summary.", "inputs": [],
            "output_contracts": [{"artifact_id": name, "output_id": name, "role": name,
                "required": True, "path_policy": "planner_materialized"}
                for name in ("ann_summary", "annotated_vcf")]}},
        "task_output_materializations": [{"output_ref": name, "output_id": name,
            "canonical_path": str(path), "path": str(path)}
            for name, path in (("ann_summary", output), ("annotated_vcf", upstream))]}
    record = {"artifact_version_id": "summary@expected", "logical_artifact_id": "ann_summary",
        "producer_step_id": packet.step_id, "producer_attempt_id": "", "path": str(output),
        "status": "expected", "validation_status": "unknown"}
    accepted = {"artifact_version_id": "upstream@committed", "logical_artifact_id": "annotated_vcf",
        "producer_step_id": "upstream", "producer_attempt_id": "upstream-attempt", "path": str(upstream),
        "status": "valid", "validation_status": "valid", "consumer_step_ids": [packet.step_id]}
    attempt = {"attempt_id": packet.attempt_ref, "plan_id": state["plan_id"],
        "plan_version": state["plan_version"], "step_id": packet.step_id,
        "execution_record_id": "exec-current", "started_at": "recorded-start", "finished_at": "recorded-end",
        "returncode": 1, "action_id": "action-current", "action_hash": "a" * 64,
        "approved_script": {"path": str(tmp_path / "approved.py"), "sha256": "a" * 64},
        "executed_script": {"path": str(tmp_path / "executed.py"), "sha256": "b" * 64},
        "managed_output_contracts": [{"artifact_id": "ann_summary", "semantic_handle": "output.ann_summary",
            "expected_path": str(output), "required": True}],
        "managed_output_snapshot_before": {str(output): {"exists": True}}}
    state["provenance_state"] = {"artifacts": {"summary@expected": record, "upstream@committed": accepted},
        "artifact_heads": {"ann_summary": "summary@expected", "annotated_vcf": "upstream@committed"},
        "attempts": {packet.attempt_ref: attempt}}
    archive = PhaseArchiveStore(tmp_path)
    authority = tmp_path / "task_outputs" / packet.task_id / "plan_state.json"
    authority.parent.mkdir(parents=True)
    return seed, state, archive, authority, output


def dispatcher(seed, state, archive, authority):
    authority.write_text(json.dumps(state))
    registry = build_recovery_inspection_registry(seed, state, phase_store=archive)
    return RecoveryLocalToolDispatcher(seed=seed, durable=state, registry=registry, phase_store=archive)


def test_expected_output_is_diagnostic_only_and_upstream_stays_formal(tmp_path):
    seed, state, archive, authority, output = fixture(tmp_path)
    tools = dispatcher(seed, state, archive, authority)
    before = authority.read_bytes()
    manifest = next(row for row in tools.registry.provider_manifest() if row["ref_id"] == "artifact:ann_summary")
    assert manifest["readable"] and manifest["scope"]["diagnostic_only"] and manifest["scope"]["source_uncertain"]
    for tool in ("artifact.inspect", "artifact.read"):
        result = tools.dispatch_full(tool, {"ref_id": "artifact:ann_summary"})
        assert result["diagnostic_only"] and result["source_uncertain"]
        assert result["formal_consumption_allowed"] is False
        assert result["diagnostic_read"]["production_by_current_attempt"] == "NOT_ESTABLISHED"
        assert result["diagnostic_read"]["existed_before_attempt"] is True
    assert "old bytes" in tools.dispatch_full("artifact.read", {"ref_id": "artifact:ann_summary"})["content"]
    accepted = tools.dispatch_full("artifact.inspect", {"ref_id": "artifact:annotated_vcf"})
    assert accepted["validation_status"] == "valid" and "diagnostic_only" not in accepted
    assert authority.read_bytes() == before and output.read_text().startswith("old bytes")
    assert state["provenance_state"]["artifacts"]["summary@expected"]["status"] == "expected"


@pytest.mark.parametrize("change", ["not_executed", "unapproved", "wrong_attempt", "wrong_plan", "wrong_step",
    "different_contract_path", "different_head_path", "foreign_producer", "ambiguous_head"])
def test_diagnostic_admission_rejects_missing_authority(tmp_path, change):
    seed, state, archive, authority, _ = fixture(tmp_path)
    provenance = state["provenance_state"]
    attempt = provenance["attempts"][seed.failure_packet.attempt_ref]
    record = provenance["artifacts"]["summary@expected"]
    if change == "not_executed": attempt["execution_record_id"] = ""
    elif change == "unapproved": attempt["approved_script"] = {}
    elif change == "wrong_attempt": attempt["attempt_id"] = "foreign"
    elif change == "wrong_plan": attempt["plan_version"] = "foreign"
    elif change == "wrong_step": attempt["step_id"] = "foreign"
    elif change == "different_contract_path": attempt["managed_output_contracts"][0]["expected_path"] = str(tmp_path / "other")
    elif change == "different_head_path": record["path"] = str(tmp_path / "other")
    elif change == "foreign_producer": record["producer_attempt_id"] = "foreign"
    else:
        provenance["artifacts"]["second"] = {**record, "artifact_version_id": "second"}
        provenance["artifact_heads"]["second"] = "second"
    tools = dispatcher(seed, state, archive, authority)
    manifest = next(row for row in tools.registry.provider_manifest() if row["ref_id"] == "artifact:ann_summary")
    assert not manifest["readable"]
    with pytest.raises(RecoveryToolError): tools.dispatch_full("artifact.read", {"ref_id": "artifact:ann_summary"})


@pytest.mark.parametrize("change", ["current_step", "head", "execution", "approval", "contract", "materialization", "incident"])
def test_read_rechecks_current_attempt_and_authority(tmp_path, change):
    seed, state, archive, authority, _ = fixture(tmp_path)
    tools = dispatcher(seed, state, archive, authority)
    fresh = deepcopy(state); attempt = fresh["provenance_state"]["attempts"][seed.failure_packet.attempt_ref]
    if change == "current_step": fresh["current_step_id"] = "next"
    elif change == "head": fresh["provenance_state"]["artifact_heads"].pop("ann_summary")
    elif change == "execution": attempt["execution_record_id"] = "exec-different"
    elif change == "approval": attempt["approved_script"]["sha256"] = "c" * 64
    elif change == "contract": attempt["managed_output_contracts"][0]["expected_path"] = str(tmp_path / "other")
    elif change == "materialization": fresh["task_output_materializations"][0]["canonical_path"] = str(tmp_path / "other")
    else: fresh["task_state"]["active_failure_packet_ref"] = "failure:new-incident"
    authority.write_text(json.dumps(fresh))
    with pytest.raises(RecoveryToolError, match="ARTIFACT_AUTHORITY_STALE"):
        tools.dispatch_full("artifact.read", {"ref_id": "artifact:ann_summary"})


def test_missing_output_is_a_read_failure_not_a_created_result(tmp_path):
    seed, state, archive, authority, output = fixture(tmp_path)
    tools = dispatcher(seed, state, archive, authority)
    output.unlink()
    with pytest.raises(RecoveryToolError, match="ARTIFACT_FILE_NOT_FOUND"):
        tools.dispatch_full("artifact.inspect", {"ref_id": "artifact:ann_summary"})
    assert not output.exists()


def test_unpublished_location_never_becomes_downstream_input(tmp_path):
    seed, state, archive, authority, _ = fixture(tmp_path)
    prior_step = seed.failure_packet.step_id
    packet = replace(seed.failure_packet, step_id="downstream", attempt_ref="next-attempt")
    seed = replace(seed, failure_packet=packet,
        current_step={**dict(seed.current_step), "step_id": "downstream", "inputs": ["ann_summary"], "outputs": []},
        dependency_outcomes=({"step_id": prior_step, "status": "completed",
            "summary": "A dependency label cannot substitute for an accepted artifact version."},))
    state["current_step_id"] = "downstream"
    tools = dispatcher(seed, state, archive, authority)
    with pytest.raises(RecoveryToolError, match="ARTIFACT_VERSION_SCOPE_MISMATCH"):
        tools.dispatch_full("artifact.read", {"ref_id": "artifact:ann_summary"})
