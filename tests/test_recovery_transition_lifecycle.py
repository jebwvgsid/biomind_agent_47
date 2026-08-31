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
    RecoveryTransitionStatus,
    contextualize_recovery_decision,
    parse_recovery_judge_decision,
    recovery_route_from_state,
)
from bio_agents import BioAnalystAgent
from supervisor import SupervisorNode


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
    return contextualize_recovery_decision(
        RecoveryDecision.from_mapping(value),
        action_id=action_id,
        attempt_id=attempt_id,
        step_id="step-1",
        source_node="execute",
        step_attempt=step_attempt,
        trigger_event_id=attempt_id,
    )


def _retry_decision(evidence: Dict[str, Any], *, step_attempt: int = 1) -> RecoveryDecision:
    decision = RecoveryPolicy().decide(
        execution_evidence=evidence,
        budgets={},
        action_id=str(evidence["action_id"]),
        attempt_id=str(evidence["attempt_id"]),
        step_id="step-1",
    )
    assert decision.kind == RecoveryDecisionKind.RETRY_CURRENT_ACTION.value
    return contextualize_recovery_decision(
        decision,
        action_id=str(evidence["action_id"]),
        attempt_id=str(evidence["attempt_id"]),
        step_id="step-1",
        source_node="execute",
        step_attempt=step_attempt,
        trigger_event_id=str(evidence["attempt_id"]),
    )


def _agent(root: Path) -> BioAnalystAgent:
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = root
    return agent


def _resolve_execute(
    root: Path,
    task_id: str,
    evidence: Dict[str, Any],
    *,
    proposed_route: str = "supervisor",
    recovery_application: Dict[str, Any] | None = None,
    progress_guard_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    update: Dict[str, Any] = {
        "proposed_route": proposed_route,
        "next_step": proposed_route,
        "execution_evidence": evidence,
    }
    if recovery_application is not None:
        update["recovery_application"] = recovery_application
    if progress_guard_result is not None:
        update["progress_guard_result"] = progress_guard_result
    return _agent(root)._resolve_and_commit_effective_transition(
        "execute",
        {
            "task_id": task_id,
            "current_step_id": "step-1",
            "execution_evidence": evidence,
        },
        update,
        task_id,
    )


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


def test_b_recovery_transition_is_consumed_once_and_not_reapplied(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-b-one-shot"
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-accepted",
        attempt_id="attempt-accepted",
        returncode=0,
        result_path=result_path,
        result_content="##fileformat=VCFv4.2\n# accepted\n",
    )
    application = apply_recovery_decision(
        state, _accepted_decision(incident_fixture)
    )
    save_plan_state(tmp_path, task_id, state)

    first = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        recovery_application=application,
    )
    assert first["effective_route"] == "supervisor"
    assert first["override_source"] == "recovery_transition"

    consumed_state = load_plan_state(tmp_path, task_id)
    recovery_state = consumed_state["task_state"]["recovery_state"]
    assert recovery_state["active_transition"] == {}
    assert recovery_state["last_consumed_transition"]["status"] == "consumed"
    assert recovery_state["last_consumed_transition"]["decision_id"] == application["decision_id"]
    consumed = [
        item
        for item in recovery_state["transition_history"]
        if item.get("decision_id") == application["decision_id"]
        and item.get("status") == "consumed"
    ]
    assert len(consumed) == 1

    later = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "generate",
        {"task_id": task_id, "current_step_id": "step-1"},
        {"proposed_route": "execute", "next_step": "execute"},
        task_id,
    )
    assert later["effective_route"] == "execute"
    assert later["override_decision_id"] == ""
    assert later["override_source"] != "recovery_transition"


def test_c_old_generate_decision_cannot_override_newer_execute_success(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-c-stale"
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    old_evidence = _record_attempt(
        state,
        action_id="action-old",
        attempt_id="attempt-old",
        returncode=1,
        stderr_summary="old recoverable failure",
    )
    old_decision = _retry_decision(old_evidence)
    assert old_decision.target_route == "generate"
    application = apply_recovery_decision(state, old_decision)
    newer_evidence = _record_attempt(
        state,
        action_id="action-new",
        attempt_id="attempt-new",
        returncode=0,
        result_path=result_path,
        result_content="##fileformat=VCFv4.2\n# newer success\n",
    )
    save_plan_state(tmp_path, task_id, state)

    resolved = _resolve_execute(tmp_path, task_id, newer_evidence)
    assert resolved["proposed_route"] == "supervisor"
    assert resolved["effective_route"] == "supervisor"
    assert resolved["override_source"] == "stale_recovery_transition_ignored"
    assert resolved["override_decision_id"] == application["decision_id"]

    persisted = load_plan_state(tmp_path, task_id)
    recovery_state = persisted["task_state"]["recovery_state"]
    assert recovery_state["active_transition"] == {}
    assert recovery_state["last_ineligible_transition"]["reason"] == "SUPERSEDED_BY_NEWER_EXECUTION"
    assert any(
        item.get("decision_id") == application["decision_id"]
        and item.get("status") == "superseded"
        for item in recovery_state["transition_history"]
    )


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


def test_e_integration_accept_validate_complete_and_start_next_step(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = str(incident_fixture["task_id"])
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    route_trace = ["Generate", "Execute", "Recovery"]
    evidence = _record_attempt(
        state,
        action_id="action-accepted",
        attempt_id="attempt-accepted",
        returncode=0,
        result_path=result_path,
        result_content="##fileformat=VCFv4.2\n# canonical accepted result\n",
        stderr_summary=str(incident_fixture["incident_evidence"]["stderr_summary"]),
    )
    application = apply_recovery_decision(
        state, _accepted_decision(incident_fixture)
    )
    save_plan_state(tmp_path, task_id, state)

    routed = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        recovery_application=application,
    )
    route_trace.append("Supervisor")
    assert routed["effective_route"] == "supervisor"
    assert "Generate" not in route_trace[3:]

    supervised = load_plan_state(tmp_path, task_id)
    validation = record_provenance_event(
        supervised,
        "validation_recorded",
        {
            "attempt_id": evidence["attempt_id"],
            "validation": {
                "validation_id": "validation-accepted",
                "status": "valid",
                "checks": ["nonempty annotated VCF", "canonical result accepted"],
            },
        },
    )
    gate = CompletionGate.validate_step_evidence(supervised, "step-1")
    route_trace.append("CompletionGate")
    assert gate.completion_satisfied, gate.to_dict()
    assert gate.validation_ids == [validation["validation_id"]]

    completion_decision = RecoveryPolicy().decide(
        completion_evidence={
            **gate.to_dict(),
            "task_complete": False,
            "evidence": [f"validation_id={validation['validation_id']}"],
        },
        action_id=evidence["action_id"],
        step_id="step-1",
        attempt_id=evidence["attempt_id"],
    )
    completion_decision = contextualize_recovery_decision(
        completion_decision,
        action_id=evidence["action_id"],
        step_id="step-1",
        attempt_id=evidence["attempt_id"],
        source_node="supervisor",
        step_attempt=1,
        trigger_event_id=evidence["attempt_id"],
    )
    assert completion_decision.kind == RecoveryDecisionKind.COMPLETE_STEP.value
    completion_application = apply_recovery_decision(supervised, completion_decision)
    route_trace.append("step-2")

    statuses = _status_by_step(supervised)
    assert statuses == {"step-1": "passed", "step-2": "running"}
    assert route_trace == incident_fixture["expected_route_trace"]
    forbidden = incident_fixture["forbidden_route_trace"]
    assert route_trace != forbidden
    recovery_state = supervised["task_state"]["recovery_state"]
    assert recovery_state["last_consumed_transition"]["decision_id"] == application["decision_id"]
    assert recovery_state["last_consumed_transition"]["status"] == "consumed"
    active = dict(recovery_state.get("active_transition") or {})
    assert active.get("decision_id") != application["decision_id"]

    # The completion transition itself remains pending across persistence even
    # though applying it advanced current_step_id.  It is then consumed once by
    # the sole effective-route resolver before step 2 is dispatched.
    save_plan_state(tmp_path, task_id, supervised)
    persisted_completion = load_plan_state(tmp_path, task_id)
    assert (
        persisted_completion["task_state"]["recovery_state"]["active_transition"]["decision_id"]
        == completion_application["decision_id"]
    )
    completion_route = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "supervisor",
        {"task_id": task_id, "current_step_id": "step-1"},
        {
            "proposed_route": "generate",
            "next_step": "generate",
            "current_step_id": "step-1",
            "execution_evidence": evidence,
            "recovery_decision": completion_decision.to_dict(),
            "recovery_application": completion_application,
        },
        task_id,
    )
    assert completion_route["dispatch_route"] == "generate"
    assert completion_route["recovery_transition_consumed"] is True
    consumed_completion = load_plan_state(tmp_path, task_id)["task_state"]["recovery_state"]
    assert consumed_completion["active_transition"] == {}
    assert consumed_completion["last_consumed_transition"]["decision_id"] == completion_application["decision_id"]


def test_f_progress_guard_observes_effective_override_route(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-f-effective-route"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-retry",
        attempt_id="attempt-retry",
        returncode=1,
    )
    application = apply_recovery_decision(state, _retry_decision(evidence))
    save_plan_state(tmp_path, task_id, state)

    resolved = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        proposed_route="supervisor",
        recovery_application=application,
        progress_guard_result=_matching_progress_guard(
            evidence, classification="no_progress", should_block=False
        ),
    )

    assert resolved["proposed_route"] == "supervisor"
    assert resolved["pre_guard_effective_route"] == "generate"
    assert resolved["effective_route"] == "generate"
    assert resolved["override_source"] == "recovery_transition"
    assert resolved["progress_guard_result"]["scope_matches"] is True
    assert resolved["progress_guard_result"]["observed_route"] == "generate"


def test_g_exhausted_progress_is_a_hard_block_and_dispatches_finish(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-g-hard-block"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-exhausted",
        attempt_id="attempt-exhausted",
        returncode=1,
    )
    save_plan_state(tmp_path, task_id, state)

    resolved = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        proposed_route="generate",
        progress_guard_result=_matching_progress_guard(
            evidence, classification="exhausted", should_block=True
        ),
    )

    assert resolved["effective_route"] == "blocked"
    assert resolved["dispatch_route"] == "finish"
    assert resolved["next_step"] == "finish"
    assert resolved["route_resolution"]["block_reason_code"] == "RECOVERY_NO_PROGRESS_EXHAUSTED"
    assert BioAnalystAgent._dispatch_effective_route(resolved, "execute") == "finish"
    persisted = load_plan_state(tmp_path, task_id)
    assert persisted["task_state"]["status"] == "blocked"
    assert _status_by_step(persisted) == {
        "step-1": "blocked",
        "step-2": "skipped_upstream_blocked",
    }


def test_h_no_progress_block_never_completes_or_advances_plan(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-h-no-advance"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-no-advance",
        attempt_id="attempt-no-advance",
        returncode=1,
    )
    save_plan_state(tmp_path, task_id, state)

    resolved = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        progress_guard_result=_matching_progress_guard(
            evidence, classification="exhausted", should_block=True
        ),
    )
    persisted = load_plan_state(tmp_path, task_id)
    statuses = _status_by_step(persisted)

    assert resolved["effective_route"] == "blocked"
    assert "passed" not in statuses.values()
    assert "running" not in statuses.values()
    assert statuses["step-1"] == "blocked"
    assert statuses["step-2"] == "skipped_upstream_blocked"
    assert persisted["current_step_id"] != "step-2"


def test_i_legacy_generate_override_is_superseded_by_newer_success(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-i-legacy-resume"
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    _record_attempt(
        state,
        action_id="action-accepted",
        attempt_id="attempt-accepted",
        returncode=0,
        result_path=result_path,
        result_content="##fileformat=VCFv4.2\n# old canonical result\n",
    )
    newer = _record_attempt(
        state,
        action_id="action-newer-success",
        attempt_id="attempt-newer-success",
        returncode=0,
        result_path=result_path,
        result_content="##fileformat=VCFv4.2\n# newer canonical result\n",
    )
    legacy = dict(incident_fixture["legacy_accepted_decision"])
    recovery_state = dict(state["task_state"].get("recovery_state") or {})
    recovery_state.update(
        {
            "last_recovery_decision": legacy,
            "decision_history": [dict(legacy)],
            "active_transition": {},
        }
    )
    state["task_state"]["recovery_state"] = recovery_state
    state["task_state"]["last_recovery_decision"] = dict(legacy)
    save_plan_state(tmp_path, task_id, state)

    resolved = _resolve_execute(tmp_path, task_id, newer)
    assert resolved["effective_route"] == "supervisor"
    assert resolved["dispatch_route"] == "supervisor"
    assert resolved["override_source"] == "stale_recovery_transition_ignored"
    assert resolved["effective_route"] != "generate"

    persisted = load_plan_state(tmp_path, task_id)
    recovery_state = persisted["task_state"]["recovery_state"]
    assert recovery_state["active_transition"] == {}
    assert recovery_state["last_ineligible_transition"]["reason"] == "SUPERSEDED_BY_NEWER_EXECUTION"
    assert any(
        item.get("status") == "superseded"
        and item.get("superseded_reason") == "SUPERSEDED_BY_NEWER_EXECUTION"
        for item in recovery_state["transition_history"]
    )
    assert recovery_state["decision_history"][0]["next_route"] == "generate"


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
    decision = _retry_decision(latest_failure, step_attempt=2)
    application = apply_recovery_decision(state, decision)

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
    application = apply_recovery_decision(state, _retry_decision(evidence))

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
    application = apply_recovery_decision(state, _retry_decision(evidence))
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


def test_execute_early_return_does_not_reuse_state_execution_evidence(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-resolver-no-stale-evidence"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="action-prior-failure",
        attempt_id="attempt-prior-failure",
        returncode=1,
    )
    application = apply_recovery_decision(state, _retry_decision(evidence))
    save_plan_state(tmp_path, task_id, state)

    resolved = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "execute",
        {
            "task_id": task_id,
            "current_step_id": "step-1",
            "execution_evidence": evidence,
        },
        {
            "proposed_route": "supervisor",
            "next_step": "supervisor",
        },
        task_id,
    )

    assert resolved["effective_route"] == "supervisor"
    assert resolved["override_source"] == "stale_recovery_transition_ignored"
    persisted = load_plan_state(tmp_path, task_id)
    ineligible = persisted["task_state"]["recovery_state"]["last_ineligible_transition"]
    assert ineligible["decision_id"] == application["decision_id"]
    assert ineligible["reason"] == "CURRENT_EVENT_MISSING"


def test_final_step_completion_transition_preserves_final_audit_route(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-final-step-audit-route"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    state["steps"][0]["status"] = "passed"
    state["steps"][1]["status"] = "passed"
    state["current_step_id"] = "step-2"
    decision_id = "decision-final-step-complete"
    transition = {
        "schema_version": 5,
        "decision_id": decision_id,
        "idempotency_key": decision_id,
        "decision_owner": "RecoveryPolicy",
        "kind": "COMPLETE_STEP",
        "disposition": "COMPLETE_STEP",
        "status": "pending",
        "step_id": "step-2",
        "step_attempt": 0,
        "source_node": "supervisor",
        "trigger_node": "supervisor",
        "attempt_id": "completion-event-step-2",
        "trigger_event_id": "completion-event-step-2",
        "next_route": "generate",
        "target_route": "generate",
        "reason_code": "completion_contract_satisfied",
        "reason": "CompletionGate accepted the final step.",
        "evidence": ["validation_id=final-step"],
    }
    state["task_state"]["recovery_state"] = {
        "schema_version": 4,
        "phase": "pending_dispatch",
        "status": "COMPLETE_STEP",
        "step_id": "step-2",
        "active_transition": dict(transition),
        "last_recovery_decision": dict(transition),
        "transition_history": [],
        "decision_history": [dict(transition)],
        "decision_audit": [{
            "decision_id": decision_id,
            "apply_status": "applied",
        }],
        "applied_decision_ids": [decision_id],
    }
    state["task_state"]["last_recovery_decision"] = dict(transition)
    save_plan_state(tmp_path, task_id, state)

    resolved = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "supervisor",
        {"task_id": task_id, "current_step_id": "step-2"},
        {
            "proposed_route": "generate",
            "next_step": "supervisor_final_audit",
            "current_step_id": "step-2",
            "final_audit_requested": True,
            "transition_guard": "complete_step_requires_final_audit",
            "recovery_decision": dict(transition),
            "recovery_application": {
                "applied": True,
                "decision_id": decision_id,
            },
        },
        task_id,
    )

    assert resolved["pre_guard_effective_route"] == "generate"
    assert resolved["effective_route"] == "supervisor_final_audit"
    assert resolved["dispatch_route"] == "supervisor_final_audit"
    assert resolved["override_source"] == "controller_completion_route"
    persisted = load_plan_state(tmp_path, task_id)
    assert persisted["task_state"]["recovery_state"]["active_transition"] == {}


def test_plan_patch_fuse_is_applied_after_recovery_route_override(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-plan-patch-effective-route-guard"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    decision_id = "decision-plan-patch"
    transition = {
        "schema_version": 5,
        "decision_id": decision_id,
        "idempotency_key": decision_id,
        "decision_owner": "RecoveryPolicy",
        "kind": "PLAN_PATCH_REQUEST",
        "disposition": "PLAN_PATCH",
        "status": "pending",
        "step_id": "step-1",
        "step_attempt": 0,
        "source_node": "generate",
        "trigger_node": "generate",
        "attempt_id": "plan-patch-event",
        "trigger_event_id": "plan-patch-event",
        "next_route": "planner",
        "target_route": "planner",
        "reason_code": "plan_patch_requested",
        "reason": "Planner patch proposed no semantic change.",
        "evidence": ["same_semantic_plan_hash=true"],
    }
    state["task_state"]["recovery_state"] = {
        "schema_version": 4,
        "phase": "pending_dispatch",
        "status": "PLAN_PATCH_REQUEST",
        "step_id": "step-1",
        "active_transition": dict(transition),
        "last_recovery_decision": dict(transition),
        "transition_history": [],
        "decision_history": [dict(transition)],
    }
    state["task_state"]["last_recovery_decision"] = dict(transition)
    save_plan_state(tmp_path, task_id, state)

    resolved = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "generate",
        {"task_id": task_id, "current_step_id": "step-1"},
        {
            "proposed_route": "planner",
            "next_step": "generate",
            "current_step_id": "step-1",
            "transition_guard": "repeated_no_op_plan_patch_fused",
            "recovery_decision": dict(transition),
            "recovery_application": {
                "applied": True,
                "decision_id": decision_id,
            },
        },
        task_id,
    )

    assert resolved["pre_guard_effective_route"] == "planner"
    assert resolved["effective_route"] == "generate"
    assert resolved["override_source"] == "controller_plan_patch_guard"
    assert resolved["progress_guard_result"]["observed_route"] == "generate"


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
    application = apply_recovery_decision(
        state,
        _retry_decision(primary, step_attempt=1),
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


def test_plain_continue_previews_then_crash_resume_consumes_accept_once(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-crash-resume-accept"
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="resume-action",
        attempt_id="resume-attempt",
        returncode=0,
        result_path=result_path,
    )
    application = apply_recovery_decision(
        state,
        _accepted_decision(
            incident_fixture,
            action_id="resume-action",
            attempt_id="resume-attempt",
        ),
    )
    save_plan_state(tmp_path, task_id, state)

    agent = _agent(tmp_path)
    agent.state_manager = None
    agent.default_project_id = "test-project"
    agent._get_or_create_stable_prompt = lambda *_args: {
        "prompt": "",
        "core_memory_loaded_at": "",
    }
    agent._runtime_status_for_task = lambda *_args, **_kwargs: {}

    preview = agent._continue_control_state(task_id)
    assert preview["action"] == "resume"
    assert preview["resume_audit"]["resume_reason"] == "pending_recovery_transition_preview"
    still_pending = load_plan_state(tmp_path, task_id)["task_state"]["recovery_state"]
    assert still_pending["active_transition"]["decision_id"] == application["decision_id"]

    runner = agent._build_resume_runner_state(task_id)
    assert runner["resolved_resume_node"] == "supervisor"
    assert runner["execution_evidence"]["attempt_id"] == evidence["attempt_id"]
    assert runner["reconciliation_evidence"]["decision_id"] == application["decision_id"]
    assert runner["reconciliation_evidence"]["attempt_id"] == evidence["attempt_id"]
    assert runner["recovery_decision"] == {}
    assert runner["recovery_phase"] is None
    consumed = load_plan_state(tmp_path, task_id)["task_state"]["recovery_state"]
    assert consumed["active_transition"] == {}
    assert consumed["last_consumed_transition"]["decision_id"] == application["decision_id"]
    assert consumed["last_consumed_transition"]["status"] == "consumed"


def test_supervisor_reconciliation_is_evidence_not_route_authority(tmp_path: Path) -> None:
    node = SupervisorNode(None, output_dir=str(tmp_path), use_llm_review=False)
    result = asyncio.run(node.run({
        "task_id": "supervisor-reconciliation",
        "current_step_id": "step-1",
        "execution_evidence": {
            "execution_id": "exec-1",
            "attempt_id": "attempt-1",
            "action_id": "action-1",
            "step_id": "step-1",
            "returncode": 0,
            "execution_status": "succeeded",
            "command_role": "primary",
            "command": "true",
            "stdout_summary": "validated output",
            "stderr_summary": "",
        },
        "reconciliation_evidence": {
            "decision_id": "decision-accept",
            "attempt_id": "attempt-1",
            "step_id": "step-1",
            "status": "consumed",
            "reason": "the observed anomaly was non-fatal",
            "evidence": ["canonical artifact exists"],
        },
    }))
    validation = result["validation_evidence"]
    assert validation["status"] == "VALID"
    assert any(
        "decision_id=decision-accept" in item
        for item in validation["info"]
    )
    assert "next_step" not in result


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


def test_persisted_route_conflict_is_derived_from_typed_disposition(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-typed-disposition-route"
    state, result_path = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="typed-action",
        attempt_id="typed-attempt",
        returncode=0,
        result_path=result_path,
    )
    application = apply_recovery_decision(
        state,
        _accepted_decision(
            incident_fixture,
            action_id="typed-action",
            attempt_id="typed-attempt",
        ),
    )
    recovery = state["task_state"]["recovery_state"]
    recovery["active_transition"]["target_route"] = "generate"
    recovery["active_transition"]["next_route"] = "generate"
    recovery["last_recovery_decision"]["target_route"] = "generate"
    recovery["last_recovery_decision"]["next_route"] = "generate"
    save_plan_state(tmp_path, task_id, state)

    normalized = load_plan_state(tmp_path, task_id)
    active = normalized["task_state"]["recovery_state"]["active_transition"]
    assert active["disposition"] == "ACCEPT_EXECUTION_FOR_VERIFICATION"
    assert active["target_route"] == "supervisor"
    assert active["route_conflict_payload"]["target_route"] == "generate"

    resolved = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        recovery_application=application,
    )
    assert resolved["effective_route"] == "supervisor"
    assert resolved["dispatch_route"] == "supervisor"
    assert resolved["recovery_transition_consumed"] is True


def test_typed_accept_anomaly_marks_verification_pending_even_when_rc_nonzero(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    task_id = "test-typed-accept-nonzero"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    evidence = _record_attempt(
        state,
        action_id="anomaly-action",
        attempt_id="anomaly-attempt",
        returncode=1,
        stderr_summary="a non-fatal wrapper anomaly",
    )
    decision = _accepted_decision(
        incident_fixture,
        action_id="anomaly-action",
        attempt_id="anomaly-attempt",
    ).to_dict()
    state["steps"][0]["status"] = "retry_pending"
    state["task_state"]["recovery_state"] = {
        "schema_version": 4,
        "phase": "pending_dispatch",
        "status": "ACCEPT_EXECUTION_FOR_VERIFICATION",
        "active_transition": dict(decision),
        "last_recovery_decision": dict(decision),
        "decision_history": [dict(decision)],
        "transition_history": [],
        "applied_decision_ids": [decision["decision_id"]],
    }
    save_plan_state(tmp_path, task_id, state)

    migrated = load_plan_state(tmp_path, task_id)
    assert _status_by_step(migrated)["step-1"] == "verification_pending"
    assert migrated["task_state"]["resume_route_hint"] == "supervisor"
    resolved = _resolve_execute(
        tmp_path,
        task_id,
        evidence,
        recovery_application={
            "applied": True,
            "decision_id": decision["decision_id"],
            "transition_status": "pending",
        },
    )
    assert resolved["effective_route"] == "supervisor"
    assert resolved["recovery_transition_consumed"] is True
    assert _status_by_step(load_plan_state(tmp_path, task_id))["step-1"] == "verification_pending"


def test_applied_complete_step_with_persisted_route_is_not_replayed(
    tmp_path: Path,
    incident_fixture: Dict[str, Any],
) -> None:
    """A split legacy checkpoint must not dispatch COMPLETE_STEP twice.

    The applied completion has already advanced to step 2 and its effective
    route is durable, but an old pending projection survived alongside it.
    Migration must treat the persisted route as proof of prior consumption.
    """
    task_id = "test-complete-step-route-already-persisted"
    state, _ = _new_plan(tmp_path, incident_fixture, task_id=task_id)
    state["steps"][0]["status"] = "passed"
    state["steps"][1]["status"] = "running"
    state["current_step_id"] = "step-2"

    decision_id = "decision-complete-step-already-routed"
    transition = {
        "schema_version": 5,
        "decision_id": decision_id,
        "idempotency_key": decision_id,
        "decision_owner": "RecoveryPolicy",
        "kind": "COMPLETE_STEP",
        "disposition": "COMPLETE_STEP",
        "status": "pending",
        "step_id": "step-1",
        "step_attempt": 1,
        "source_node": "supervisor",
        "trigger_node": "supervisor",
        "attempt_id": "attempt-complete-step",
        "trigger_event_id": "attempt-complete-step",
        "next_route": "generate",
        "target_route": "generate",
        "reason_code": "completion_contract_satisfied",
        "reason": "CompletionGate accepted step 1.",
        "evidence": ["validation_id=validation-step-1"],
    }
    route_record = {
        "task_id": task_id,
        "route_sequence": 1,
        "route_event_id": "route-complete-step-already-persisted",
        "source_node": "supervisor",
        "proposed_route": "generate",
        "pre_guard_effective_route": "generate",
        "effective_route": "generate",
        "dispatch_route": "generate",
        "override_decision_id": decision_id,
        "override_source": "recovery_transition",
        "current_step_id": "step-1",
        "current_attempt": "attempt-complete-step",
    }
    task_state = state["task_state"]
    task_state["recovery_state"] = {
        "schema_version": 4,
        "phase": "pending_dispatch",
        "status": "COMPLETE_STEP",
        "active_transition": dict(transition),
        "last_recovery_decision": dict(transition),
        "decision_history": [dict(transition)],
        "transition_history": [],
        "decision_audit": [{
            "decision_id": decision_id,
            "apply_status": "applied",
        }],
        "applied_decision_ids": [decision_id],
    }
    task_state["last_recovery_decision"] = dict(transition)
    task_state["route_sequence"] = 1
    task_state["last_route_resolution"] = dict(route_record)
    task_state["route_resolution_history"] = [dict(route_record)]
    save_plan_state(tmp_path, task_id, state)

    migrated = load_plan_state(tmp_path, task_id)
    recovery = migrated["task_state"]["recovery_state"]
    assert recovery["active_transition"] == {}
    assert recovery["last_consumed_transition"]["decision_id"] == decision_id
    assert recovery["last_consumed_transition"]["status"] == "consumed"
    assert (
        recovery["last_consumed_transition"]["route_event_id"]
        == route_record["route_event_id"]
    )
    assert recovery["last_consumed_transition"]["dispatched_route"] == "generate"
    assert len([
        item for item in recovery["transition_history"]
        if item.get("decision_id") == decision_id
        and item.get("status") == "consumed"
    ]) == 1
    assert len([
        item for item in recovery["decision_audit"]
        if item.get("decision_id") == decision_id
        and item.get("apply_status") == "consumed"
    ]) == 1

    # A later Supervisor result may route normally, but cannot consume or
    # attach authority from the already-persisted completion transition.
    later = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "supervisor",
        {"task_id": task_id, "current_step_id": "step-1"},
        {
            "proposed_route": "generate",
            "next_step": "generate",
            "current_step_id": "step-1",
            "recovery_decision": dict(transition),
            "recovery_application": {
                "applied": True,
                "decision_id": decision_id,
            },
        },
        task_id,
    )
    assert later["recovery_transition_consumed"] is False
    assert later["override_decision_id"] == ""

    persisted = load_plan_state(tmp_path, task_id)
    matching_routes = [
        item for item in persisted["task_state"]["route_resolution_history"]
        if item.get("override_decision_id") == decision_id
    ]
    assert [item["route_event_id"] for item in matching_routes] == [
        route_record["route_event_id"]
    ]
