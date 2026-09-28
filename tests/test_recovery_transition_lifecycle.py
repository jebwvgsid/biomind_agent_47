"""Regression coverage for session_1787797284 recovery transition routing.

The tests exercise the durable PlanTracker and the controller's effective-route
commit path directly.  No LLM, shell command, or snpEff behavior participates.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from agent_core.completion_gate import CompletionGate
from agent_core.diagnostics import ProgressTracker
from agent_core.plan_tracker import (
    apply_recovery_decision,
    build_plan_state,
    clear_step_scoped_recovery,
    load_plan_state,
    migrate_plan_state,
    record_provenance_event,
    recovery_transition_eligibility,
    save_plan_state,
)
from agent_core.recovery import (
    RecoveryAction,
    RecoveryDecision,
    RecoveryDecisionKind,
    RecoveryDisposition,
    RecoveryPolicy,
    RecoverySourceScope,
    RecoveryTransitionStatus,
    build_recovery_execution_envelope,
    contextualize_recovery_decision,
    finalize_recovery_decision,
    parse_recovery_judge_decision,
    recovery_route_from_state,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "session_1787797284_recovery_transition_replay.json"
)


@pytest.fixture(scope="module")
def incident_fixture() -> Dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _new_plan(
    root: Path,
    fixture: Dict[str, Any],
    *,
    task_id: str,
    first_status: str = "retry_pending",
) -> Tuple[Dict[str, Any], Path]:
    result_path = root / task_id / "annotated.vcf"
    plan_text = str(fixture["plan_markdown"]).replace("{RESULT}", str(result_path))
    state = build_plan_state(task_id, plan_text)
    state["steps"][0]["status"] = first_status
    state["steps"][1]["status"] = "pending"
    state["current_step_id"] = "step-1"
    state.setdefault("task_state", {})["status"] = "running"
    save_plan_state(root, task_id, state)
    return load_plan_state(root, task_id), result_path


def _record_attempt(
    state: Dict[str, Any],
    *,
    action_id: str,
    attempt_id: str,
    returncode: int,
    result_path: Path | None = None,
    result_content: str = "",
    stderr_summary: str = "",
) -> Dict[str, Any]:
    record_provenance_event(
        state,
        "action_bound",
        {
            "action": {
                "action_id": action_id,
                "step_id": "step-1",
                "type": "execute",
                "command_role": "primary",
                "action_hash": f"hash-{action_id}",
            }
        },
    )
    record_provenance_event(
        state,
        "attempt_started",
        {
            "action_id": action_id,
            "attempt_id": attempt_id,
            "step_id": "step-1",
        },
    )
    produced_artifacts = []
    if result_path is not None and returncode == 0:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(result_content or "##fileformat=VCFv4.2\n", encoding="utf-8")
        produced_artifacts.append(str(result_path))
    record_provenance_event(
        state,
        "execution_finished",
        {
            "attempt_id": attempt_id,
            "execution_record": {
                "execution_id": f"exec-{attempt_id}",
                "exit_code": returncode,
                "execution_status": "succeeded" if returncode == 0 else "failed",
                "command_role": "primary",
                "produced_artifacts": produced_artifacts,
                "stderr_summary": stderr_summary,
            },
        },
    )
    return {
        "execution_id": f"exec-{attempt_id}",
        "attempt_id": attempt_id,
        "action_id": action_id,
        "step_id": "step-1",
        "returncode": returncode,
        "execution_status": "succeeded" if returncode == 0 else "failed",
        "command_role": "primary",
        "produced_artifacts": produced_artifacts,
        "stderr_summary": stderr_summary,
    }


def _accepted_decision(
    fixture: Dict[str, Any],
    state: Dict[str, Any],
    *,
    action_id: str = "action-accepted",
    attempt_id: str = "attempt-accepted",
    step_attempt: int = 1,
) -> RecoveryDecision:
    value = dict(fixture["legacy_accepted_decision"])
    value.update(
        {
            "action_id": action_id,
            "attempt_id": attempt_id,
            "step_id": "step-1",
            "source_node": "execute",
        }
    )
    decision = contextualize_recovery_decision(
        RecoveryDecision.from_mapping(value),
        action_id=action_id,
        attempt_id=attempt_id,
        step_id="step-1",
        source_node="execute",
        step_attempt=step_attempt,
        trigger_event_id=attempt_id,
    )
    return _finalize_for_state(state, decision)


def _retry_decision(
    state: Dict[str, Any],
    evidence: Dict[str, Any],
    *,
    step_attempt: int = 1,
) -> RecoveryDecision:
    decision = RecoveryPolicy().decide(
        execution_evidence=evidence,
        budgets={},
        action_id=str(evidence["action_id"]),
        attempt_id=str(evidence["attempt_id"]),
        step_id="step-1",
    )
    assert decision.kind == RecoveryDecisionKind.RETRY_CURRENT_ACTION.value
    decision = contextualize_recovery_decision(
        decision,
        action_id=str(evidence["action_id"]),
        attempt_id=str(evidence["attempt_id"]),
        step_id="step-1",
        source_node="execute",
        step_attempt=step_attempt,
        trigger_event_id=str(evidence["attempt_id"]),
    )
    return _finalize_for_state(state, decision, evidence=evidence)


def _finalize_for_state(
    state: Dict[str, Any],
    decision: RecoveryDecision,
    *,
    evidence: Dict[str, Any] | None = None,
) -> RecoveryDecision:
    record = dict(evidence or {})
    record.setdefault("step_id", decision.step_id)
    record.setdefault("action_id", decision.action_id)
    record.setdefault("attempt_id", decision.attempt_id)
    return finalize_recovery_decision(
        decision,
        source_scope=RecoverySourceScope.from_state_and_record(state, record),
        recovery_context_fingerprint=str(
            record.get("failure_fingerprint") or f"context:{decision.attempt_id}"
        ),
        source_node=decision.source_node or "execute",
        expected_state_generation=int(state.get("state_generation") or 0),
    )


def _apply_finalized(state: Dict[str, Any], decision: RecoveryDecision) -> Dict[str, Any]:
    envelope = None
    if decision.target_route == "generate":
        envelope = build_recovery_execution_envelope(
            decision,
            recovery_context_id=decision.recovery_context_fingerprint,
            step_id=decision.step_id,
            task_root="/tmp",
        ).to_dict()
    return apply_recovery_decision(state, decision, execution_envelope=envelope)






def _matching_progress_guard(
    evidence: Dict[str, Any],
    *,
    classification: str,
    should_block: bool,
) -> Dict[str, Any]:
    attempt_id = str(evidence["attempt_id"])
    return {
        "classification": classification,
        "should_block": should_block,
        "reason": "deterministic no-progress fixture",
        "source_node": "execute",
        "step_id": "step-1",
        "attempt_id": attempt_id,
        "trigger_event_id": attempt_id,
    }


def _status_by_step(state: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(step["step_id"]): str(step["status"])
        for step in state.get("steps", [])
    }


def test_a_accepted_execute_reconciliation_routes_to_supervisor_not_generate(
    incident_fixture: Dict[str, Any],
) -> None:
    decision = RecoveryDecision.from_mapping(
        dict(incident_fixture["legacy_accepted_decision"])
    )

    assert decision.kind == RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value
    assert decision.disposition == RecoveryDisposition.ACCEPT_EXECUTION_FOR_VERIFICATION.value
    assert decision.status == RecoveryTransitionStatus.PENDING.value
    assert decision.target_route == "supervisor"
    assert decision.next_route == "supervisor"
    assert recovery_route_from_state(
        {"recovery_decision": decision.to_dict()}, "execute"
    ) == "supervisor"
    assert decision.kind != RecoveryDecisionKind.COMPLETE_STEP.value

    # Generic CONTINUE is a legacy input only.  It is normalized to a
    # Supervisor-owned protocol path, never a second Generate route contract.
    ordinary_continue = RecoveryDecision.from_mapping(
        {
            "schema_version": 3,
            "kind": "CONTINUE",
            "recommended_action": "CONTINUE",
            "verdict": "continue",
            "action_type": "pass_with_warning",
            "reason": "No blocking condition.",
            "root_cause": "No blocking condition.",
            "evidence": [],
            "source_node": "supervisor",
        }
    )
    assert ordinary_continue.next_route == "supervisor"
    assert ordinary_continue.disposition == RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value






def test_d_successful_execute_sets_verification_pending_without_passing_step(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    state, result_path = _new_plan(
        tmp_path, incident_fixture, task_id="test-d-post-execute"
    )
    assert state["steps"][0]["status"] == "retry_pending"

    evidence = _record_attempt(
        state,
        action_id="action-success",
        attempt_id="attempt-success",
        returncode=0,
        result_path=result_path,
        result_content="##fileformat=VCFv4.2\n# success awaits validation\n",
    )

    statuses = _status_by_step(state)
    assert statuses["step-1"] == "verification_pending"
    assert statuses["step-1"] != "passed"
    assert statuses["step-2"] == "pending"
    assert state["task_state"]["verification_pending"] == pytest.approx(
        {
            "step_id": "step-1",
            "attempt_id": evidence["attempt_id"],
            "recorded_at": state["task_state"]["verification_pending"]["recorded_at"],
        }
    )
    assert state["task_state"]["resume_route_hint"] == "supervisor"












def test_recovery_judge_cannot_complete_a_step() -> None:
    payload = {
        "schema_version": 2,
        "failure_class": "VALIDATOR_FALSE_POSITIVE",
        "error_is_real": False,
        "primary_execution_succeeded": True,
        "evidence_sufficient": True,
        "confidence": 0.99,
        "recommended_action": RecoveryAction.COMPLETE_STEP.value,
        "rerun_required": False,
        "consume_retry_budget": False,
        "user_notification_required": False,
        "reason": "The judge believes the output is valid.",
        "evidence": ["canonical result exists"],
    }

    with pytest.raises(ValueError, match="may not complete a step or task"):
        parse_recovery_judge_decision(json.dumps(payload))


def test_exhausted_progress_does_not_cross_step_boundaries() -> None:
    tracker = ProgressTracker()
    first = tracker.evaluate(
        attempt_id="step-1-attempt-1",
        stage_id="step-1",
        action_type="inspect_log",
        command="cat run.log",
        new_evidence_ids=[],
    )
    exhausted = tracker.evaluate(
        attempt_id="step-1-attempt-2",
        stage_id="step-1",
        action_type="inspect_log",
        command="cat run.log",
        new_evidence_ids=[],
    )
    next_step = tracker.evaluate(
        attempt_id="step-2-attempt-1",
        stage_id="step-2",
        action_type="inspect_log",
        command="cat run.log",
        new_evidence_ids=[],
    )

    assert first.no_progress_streak == 1
    assert exhausted.classification == "exhausted"
    assert exhausted.should_block is True
    assert exhausted.step_id == "step-1"
    assert next_step.step_id == "step-2"
    assert next_step.no_progress_streak == 1
    assert next_step.classification != "exhausted"
    assert next_step.should_block is False


def test_migration_does_not_treat_an_older_success_as_newer_than_trigger(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-migration-monotonic-order"
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    _record_attempt(
        state,
        action_id="action-z-old-success",
        attempt_id="attempt-z-old-success",
        returncode=0,
        result_path=result_path,
    )
    latest_failure = _record_attempt(
        state,
        action_id="action-a-latest-failure",
        attempt_id="attempt-a-latest-failure",
        returncode=1,
    )
    # The production clock is second-granular.  Equal timestamps must retain
    # provenance insertion order rather than sorting opaque attempt IDs.
    for attempt in state["provenance_state"]["attempts"].values():
        attempt["created_at"] = "2026-08-27T00:00:00"
        attempt["started_at"] = "2026-08-27T00:00:00"
        attempt["finished_at"] = "2026-08-27T00:00:00"
    decision = _retry_decision(state, latest_failure, step_attempt=2)
    application = _apply_finalized(state, decision)

    migrated, _ = migrate_plan_state(state, task_id)
    recovery_state = migrated["task_state"]["recovery_state"]
    active = recovery_state["active_transition"]

    assert active["decision_id"] == application["decision_id"]
    assert active["status"] == "pending"
    assert not any(
        item.get("decision_id") == application["decision_id"]
        and item.get("superseded_reason") == "SUPERSEDED_BY_NEWER_EXECUTION"
        for item in recovery_state.get("transition_history", [])
    )
    assert _status_by_step(migrated)["step-1"] == "retry_pending"


def test_missing_current_event_fails_closed_instead_of_replaying_old_route(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-current-event-required"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-old-event",
        attempt_id="attempt-old-event",
        returncode=1,
    )
    application = _apply_finalized(state, _retry_decision(state, evidence))

    eligibility = recovery_transition_eligibility(
        state,
        source_node="execute",
        current_step_id="step-1",
        current_attempt_id="",
    )

    assert eligibility["eligible"] is False
    assert eligibility["decision_id"] == application["decision_id"]
    assert eligibility["status"] == "expired"
    assert eligibility["reason"] == "CURRENT_EVENT_MISSING"
    assert state["task_state"]["recovery_state"]["active_transition"] == {}


def test_completed_step_expiration_cannot_be_promoted_back_to_active(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-expired-transition-not-revived"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-expire",
        attempt_id="attempt-expire",
        returncode=1,
    )
    application = _apply_finalized(state, _retry_decision(state, evidence))
    clear_step_scoped_recovery(state, "step-1")

    first = recovery_transition_eligibility(
        state,
        source_node="execute",
        current_step_id="step-1",
        current_attempt_id="attempt-expire",
    )
    second = recovery_transition_eligibility(
        state,
        source_node="execute",
        current_step_id="step-1",
        current_attempt_id="attempt-expire",
    )

    recovery_state = state["task_state"]["recovery_state"]
    assert first["eligible"] is False
    assert second["eligible"] is False
    assert recovery_state["active_transition"] == {}
    assert recovery_state["last_recovery_decision"]["decision_id"] == application["decision_id"]
    assert recovery_state["last_recovery_decision"]["status"] == "expired"








def test_legacy_acceptance_migration_is_source_and_result_scoped(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    accepted_task = "test-legacy-accepted-migration"
    accepted, result_path = _new_plan(
        tmp_path, incident_fixture, task_id=accepted_task
    )
    _record_attempt(
        accepted,
        action_id="legacy-action",
        attempt_id="legacy-attempt",
        returncode=0,
        result_path=result_path,
    )
    legacy = dict(incident_fixture["legacy_accepted_decision"])
    legacy.update({
        "action_id": "legacy-action",
        "attempt_id": "legacy-attempt",
        "step_id": "step-1",
        "source_node": "execute",
    })
    accepted["task_state"]["recovery_state"] = {
        "schema_version": 3,
        "active_transition": dict(legacy),
        "decision_history": [],
        "transition_history": [],
    }
    save_plan_state(tmp_path, accepted_task, accepted)

    migrated = load_plan_state(tmp_path, accepted_task)
    recovery = migrated["task_state"]["recovery_state"]
    active = recovery["active_transition"]
    assert active["kind"] == "ACCEPT_EXECUTION_FOR_VERIFICATION"
    assert active["target_route"] == "supervisor"
    assert active["step_attempt"] == 1
    assert _status_by_step(migrated)["step-1"] == "verification_pending"
    assert migrated["task_state"]["resume_route_hint"] == "supervisor"
    assert active["legacy_payload"]["kind"] == "CONTINUE"
    assert active["legacy_payload"]["next_route"] == "generate"
    assert recovery["decision_history"][0]["migration_original_payload"] is True

    incompatible = RecoveryDecision.from_mapping({
        **legacy,
        "kind": "",
        "source_node": "supervisor",
        "primary_execution_succeeded": False,
        "error_is_real": True,
    })
    assert incompatible.kind == RecoveryDecisionKind.ESCALATE.value
    assert incompatible.disposition == RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value
    assert incompatible.target_route == "supervisor"


def test_primary_attempt_ordinal_ignores_diagnostic_probe(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-primary-attempt-ordinal"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    probe = _record_attempt(
        state,
        action_id="probe-action",
        attempt_id="probe-attempt",
        returncode=1,
    )
    state["provenance_state"]["attempts"][probe["attempt_id"]]["command_role"] = "diagnostic"
    primary = _record_attempt(
        state,
        action_id="primary-action",
        attempt_id="primary-attempt",
        returncode=1,
    )
    application = _apply_finalized(
        state,
        _retry_decision(state, primary, step_attempt=1),
    )
    save_plan_state(tmp_path, task_id, state)
    persisted = load_plan_state(tmp_path, task_id)

    eligibility = recovery_transition_eligibility(
        persisted,
        source_node="execute",
        current_step_id="step-1",
        current_attempt_id="primary-attempt",
        newly_committed_decision_id=application["decision_id"],
    )
    assert eligibility["eligible"] is True
    assert eligibility["transition"]["step_attempt"] == 1






def test_unbound_success_decision_from_supervisor_becomes_generate_continuation() -> None:
    unbound = RecoveryPolicy().decide(
        execution_evidence={"returncode": 0, "command_role": "primary"},
        validation_evidence={"status": "valid"},
        action_id="action-1",
        step_id="step-1",
        attempt_id="attempt-1",
    )
    assert unbound.kind == RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value

    bounded = contextualize_recovery_decision(
        unbound,
        source_node="supervisor",
    )
    assert bounded.kind == RecoveryDecisionKind.RETRY_CURRENT_ACTION.value
    assert bounded.disposition == RecoveryDisposition.RETRY_GENERATE.value
    assert bounded.target_route == "generate"

    incompatible_unbound = RecoveryDecision.from_mapping({
        "schema_version": 3,
        "failure_class": "CONTRACT_DRIFT",
        "failure_owner": "ARTIFACT_CONTRACT",
        "failure_type": "legacy_unbound_reconcile",
        "recommended_action": "RECONCILE_AND_CONTINUE",
        "primary_execution_succeeded": False,
        "error_is_real": True,
        "evidence_sufficient": True,
        "confidence": 1.0,
        "rerun_required": False,
        "consume_retry_budget": False,
        "user_notification_required": False,
        "reason": "legacy payload has no accepted Execute",
        "root_cause": "legacy payload has no accepted Execute",
        "evidence": ["returncode=1"],
    })
    rejected = contextualize_recovery_decision(
        incompatible_unbound,
        source_node="supervisor",
    )
    assert rejected.kind == RecoveryDecisionKind.ESCALATE.value
    assert rejected.disposition == RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value
    assert rejected.target_route == "supervisor"






