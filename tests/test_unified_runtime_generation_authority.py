"""Actual durable retry authority: receipts cannot retire later executions."""
import asyncio
from copy import deepcopy

import pytest

from agent_core.action_runtime import stable_digest
from agent_core.plan_tracker import load_plan_state, save_plan_state, record_provenance_event
from agent_core.workflow.runtime_evidence_recovery import (
    RuntimeRecoveryError, runtime_generation_source_scope,
)
from tests.test_unified_runtime_preparation import _recovery_fixture


def _failed_attempt(plan, name):
    action = {"action_id": name, "step_id": "science", "plan_id": plan["plan_id"],
        "plan_version": plan["plan_version"], "execution_epoch": plan["runtime_state"]["execution_epoch"],
        "type": "execute", "purpose": "analysis", "code": "# fixture external failure", "outputs": []}
    record_provenance_event(plan, "action_bound", {"action": action})
    attempt = record_provenance_event(plan, "attempt_started", {"action_id": name, "step_id": "science"})
    execution = {"task_id": plan["task_id"], "plan_id": plan["plan_id"],
        "plan_version": plan["plan_version"], "step_id": "science", "action_id": name,
        "attempt_id": attempt["attempt_id"], "execution_id": name + "-execution",
        "returncode": 1, "exit_code": 1, "execution_status": "failed", "purpose": "analysis"}
    record_provenance_event(plan, "execution_finished", {"attempt_id": attempt["attempt_id"], "execution_record": execution})
    return execution


def _current_failure(tmp_path):
    tid, sid, contract, service, original, old_aid, calls = _recovery_fixture(tmp_path)
    plan = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    execution = _failed_attempt(plan, "second-failure")
    save_plan_state(tmp_path, tid, plan)
    return tid, sid, contract, service, load_plan_state(tmp_path, tid), execution


def _installation(plan, sid, contract):
    request = {"request_id": "install-work-order", "attempt_id": "install-attempt",
        "target_env": "chosen", "target_prefix": "/tmp/chosen",
        "selected_skills": [sid], "capabilities": [contract["capability_id"]],
        "generation_source_scope": runtime_generation_source_scope(plan)}
    receipt = {**deepcopy(request), "request_hash": stable_digest(request), "status": "available"}
    plan["task_state"].update(installation_request=request, installation_receipt=receipt)


@pytest.mark.parametrize("authority", ["stale_install", "stale_recovery"])
def test_historical_available_or_completed_retry_cannot_retire_later_failure(tmp_path, authority):
    tid, sid, contract, service, plan, execution = _current_failure(tmp_path)
    if authority == "stale_install":
        plan["task_state"]["installation_request"] = {"request_id": "new-unapproved", "target_env": "newtarget"}
        plan["task_state"]["installation_receipt"] = {"status": "available", "request_id": "old-request",
            "target_env": "oldtarget", "selected_skills": [sid], "capabilities": [contract["capability_id"]]}
    else:
        plan["task_state"]["recovery_state"]["active_recovery"] = {"recovery_id": "stale-r",
            "step_id": "science", "status": "completed", "plan_revision": -1, "source_attempt_id": "old"}
        plan["task_state"]["typed_recovery_receipts"] = {"stale-r": {"directive": {
            "action": "retry_step", "evidence_refs": ["stale:other"]}}}
    save_plan_state(tmp_path, tid, plan)
    with pytest.raises(RuntimeRecoveryError, match="GENERATION_AUTHORITY_REQUIRED"):
        service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    after = load_plan_state(tmp_path, tid)
    assert after["runtime_state"]["execution_epoch"] == plan["runtime_state"]["execution_epoch"]
    assert after["provenance_state"]["attempts"][execution["attempt_id"]]["status"] == "EVIDENCE_COMMITTED"


@pytest.mark.parametrize("field", ["request_id", "attempt_id", "target_env", "target_prefix",
    "request_hash", "generation_source_scope", "source_attempt_ids", "approved_plan_snapshot_ref"])
def test_available_receipt_requires_exact_work_order_and_frozen_scientific_source(tmp_path, field):
    tid, sid, contract, service, plan, _ = _current_failure(tmp_path)
    _installation(plan, sid, contract)
    if field in {"source_attempt_ids", "approved_plan_snapshot_ref"}:
        request = plan["task_state"]["installation_request"]
        request["generation_source_scope"][field] = ["other-attempt"] if field == "source_attempt_ids" else "other-snapshot"
        plan["task_state"]["installation_receipt"]["generation_source_scope"] = deepcopy(request["generation_source_scope"])
        plan["task_state"]["installation_receipt"]["request_hash"] = stable_digest(request)
    else:
        plan["task_state"]["installation_receipt"][field] = {} if field == "generation_source_scope" else "mismatch"
    save_plan_state(tmp_path, tid, plan)
    with pytest.raises(RuntimeRecoveryError, match="GENERATION_AUTHORITY_REQUIRED"):
        service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)


def test_current_installation_can_retire_once_preserving_actions_and_not_cover_next_failure(tmp_path):
    tid, sid, contract, service, plan, _ = _current_failure(tmp_path)
    _installation(plan, sid, contract)
    actions = deepcopy(plan["provenance_state"]["actions"])
    save_plan_state(tmp_path, tid, plan)
    accepted = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    assert accepted["provenance_state"]["actions"] == actions
    assert accepted["runtime_state"]["execution_epoch"] == plan["runtime_state"]["execution_epoch"] + 1
    assert service.prepare_generation({"task_id": tid}, selected_skill_id=sid,
        capability_contract=contract)["runtime_state"]["execution_epoch"] == accepted["runtime_state"]["execution_epoch"]
    _failed_attempt(accepted, "third-failure")
    save_plan_state(tmp_path, tid, accepted)
    with pytest.raises(RuntimeRecoveryError, match="GENERATION_AUTHORITY_REQUIRED"):
        service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)


def _admit_retry(tmp_path, tid, plan, execution):
    from agent_core.workflow.outcomes import ExecuteOutcome
    from agent_core.workflow.recovery_projection import build_causal_failure_record
    from agent_core.workflow.recovery_runtime import RecoveryRuntime
    outcome = ExecuteOutcome(schema_version=1, type="execution_failed", summary="Fixture ended failure",
        scope={key: execution[key] for key in ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id")},
        payload={"execution_record": execution, "action_id": execution["action_id"],
            "attempt_id": execution["attempt_id"], "return_code": 1, "failure_fingerprint": "fixture-failure", "progress_snapshot": {}},
        evidence_refs=(execution["execution_id"],)).to_dict()
    record = build_causal_failure_record("execute_outcome", outcome, plan, task_id=tid)
    plan["task_state"].update(causal_failure_packets={record["packet_ref"]: record},
        active_failure_packet_ref=record["packet_ref"])
    save_plan_state(tmp_path, tid, plan)
    state = {"task_id": tid, "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"], "failure_source": record["failure_source"],
        "recovery_archive_ref": "phase-archive:offline-fixture",
        "recovery_directive": {"schema_version": 1, "directive_id": "current-retry", "task_id": tid,
            "action": "retry_step", "target_step_id": "science", "summary": "Inspect the runtime then regenerate",
            "reason_code": "RUNTIME_PREPARATION_REQUIRED", "instruction": "Acquire declared runtime facts",
            "evidence_refs": [execution["execution_id"]]}}
    result = asyncio.run(RecoveryRuntime(data_path=str(tmp_path)).run(state))
    assert result["recovery_outcome"]["type"] == "candidate_repair_required", result
    return load_plan_state(tmp_path, tid)


def test_formal_adopted_recovery_can_retire_once_but_not_a_later_attempt(tmp_path):
    tid, sid, contract, service, plan, execution = _current_failure(tmp_path)
    plan = _admit_retry(tmp_path, tid, plan, execution)
    actions = deepcopy(plan["provenance_state"]["actions"])
    accepted = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    assert accepted["provenance_state"]["actions"] == actions
    assert accepted["task_state"]["recovery_state"]["state_events"][-1]["authority"] == "recovery"
    _failed_attempt(accepted, "third-failure")
    save_plan_state(tmp_path, tid, accepted)
    with pytest.raises(RuntimeRecoveryError, match="GENERATION_AUTHORITY_REQUIRED"):
        service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)


@pytest.mark.parametrize("mutation", ["completed", "source_attempt", "envelope_hash", "directive_digest"])
def test_typed_recovery_checks_adopted_scope_and_integrity(tmp_path, mutation):
    tid, sid, contract, service, plan, execution = _current_failure(tmp_path)
    plan = _admit_retry(tmp_path, tid, plan, execution)
    recovery = plan["task_state"]["recovery_state"]
    if mutation == "completed": recovery["active_recovery"]["status"] = "completed"
    elif mutation == "source_attempt": recovery["active_recovery"]["source_attempt_id"] = "other"
    elif mutation == "envelope_hash": recovery["active_recovery_envelope"]["immutable_hash"] = "tampered"
    else: plan["task_state"]["typed_recovery_receipts"]["current-retry"]["directive_digest"] = "tampered"
    save_plan_state(tmp_path, tid, plan)
    with pytest.raises(RuntimeRecoveryError, match="GENERATION_AUTHORITY_REQUIRED"):
        service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
