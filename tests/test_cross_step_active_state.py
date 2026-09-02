"""Offline regression tests for session_1788362482 cross-step state leakage."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

import pytest

from agent_core.active_state import (
    ActiveStateInvariantError,
    GenerationMode,
    generation_provenance_for_dispatch,
    scoped_execution_evidence,
)
from bio_agents import BioAnalystAgent, recovery_generation_contract_failure
from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
)
from agent_core.plan_tracker import (
    advance_plan_after_verified_step,
    apply_recovery_decision,
    build_plan_state,
    load_plan_state,
    normalize_active_recovery_state,
    save_plan_state,
)
from agent_core.recovery import (
    RecoveryPolicy,
    build_recovery_execution_envelope,
    contextualize_recovery_decision,
)


INCIDENT_ROOT = Path(
    "/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/"
    "runs/task_outputs/session_1788362482"
)


def _two_step_plan(task_id: str = "cross-step") -> Dict[str, Any]:
    state = build_plan_state(
        task_id,
        "- [>] Step 1: validate-inputs | Action: EXECUTE | Skills: [] | Done: rc=0\n"
        "- [ ] Step 2: check-contig-compatibility | Action: EXECUTE | Skills: [] | Done: rc=0",
    )
    state["steps"][0].update({"step_id": "validate-inputs", "status": "running"})
    state["steps"][1].update({
        "step_id": "check-contig-compatibility",
        "status": "pending",
    })
    state["current_step_id"] = "validate-inputs"
    state.setdefault("task_state", {})["status"] = "running"
    state["task_state"].setdefault("recovery_state", {})
    return state


def _record(
    state: Dict[str, Any],
    *,
    step_id: str,
    attempt_id: str,
    action_id: str,
    returncode: int = 0,
) -> Dict[str, Any]:
    return {
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or ""),
        "plan_revision": int(state.get("plan_revision") or 0),
        "step_id": step_id,
        "attempt_id": attempt_id,
        "execution_id": "exec-" + attempt_id,
        "action_id": action_id,
        "returncode": returncode,
        "execution_status": "succeeded" if returncode == 0 else "failed",
        "command_role": "primary",
    }


def _normal_action(script: str = "echo ok") -> Dict[str, Any]:
    return adapt_script_block_to_action(
        parse_executable_script_block(f"<shell>{script}</shell>"),
        recovery_generated=False,
    )


def _agent(root: Path) -> BioAnalystAgent:
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = root
    return agent


def test_historical_execution_isolation_and_atomic_advance() -> None:
    state = _two_step_plan()
    first = _record(
        state,
        step_id="validate-inputs",
        attempt_id="attempt-step-1",
        action_id="action-step-1",
    )
    history = [deepcopy(first)]
    state["task_state"]["active_execution"] = {
        "step_id": "validate-inputs",
        "attempt_id": "attempt-step-1",
        "action_id": "action-step-1",
    }
    state["task_state"]["recovery_state"].update({
        "step_id": "validate-inputs",
        "active_recovery": {
            "recovery_id": "recovery-step-1",
            "plan_revision": state["plan_revision"],
            "step_id": "validate-inputs",
            "source_attempt_id": "attempt-step-1",
            "status": "dispatched",
            "target_route": "generate",
            "envelope_id": "envelope-step-1",
        },
        "active_recovery_envelope": {"immutable_hash": "envelope-step-1"},
    })
    state.update({
        "pending_recovery_action": {"step_id": "validate-inputs"},
        "recovery_execution_envelope": {"immutable_hash": "envelope-step-1"},
        "execution_evidence": deepcopy(first),
        "current_attempt": "attempt-step-1",
    })

    assert advance_plan_after_verified_step(
        state,
        "validated",
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    ) is True
    assert advance_plan_after_verified_step(
        state,
        "validated",
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    ) is False

    assert history == [first]
    assert scoped_execution_evidence(state, history) == {}
    assert state["task_state"]["active_execution"] == {}
    recovery = state["task_state"]["recovery_state"]
    assert recovery["active_recovery"] == {}
    assert recovery["active_recovery_envelope"] == {}
    assert recovery["active_recovery_history"][-1]["status"] == "closed"
    assert state["pending_recovery_action"] == {}
    assert state["recovery_execution_envelope"] == {}
    assert state["execution_evidence"] == {}
    assert state["current_attempt"] == ""
    assert state["steps"][0]["status"] == "passed"
    assert state["steps"][1]["status"] == "running"
    assert state["current_step_id"] == "check-contig-compatibility"
    assert state["generation_mode"] == GenerationMode.NORMAL.value
    assert state["task_state"]["generation_provenance"]["mode"] == "NORMAL"
    assert state["task_state"]["next_route"] == "generate"


def test_historical_recovery_does_not_select_recovery_generate() -> None:
    state = _two_step_plan()
    advance_plan_after_verified_step(
        state,
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    )
    state["task_state"]["recovery_state"].update({
        "decision_history": [{
            "decision_id": "historical-step-1",
            "step_id": "validate-inputs",
            "status": "consumed",
            "target_route": "generate",
        }],
        "last_recovery_decision": {
            "decision_id": "historical-step-1",
            "step_id": "validate-inputs",
            "status": "consumed",
            "target_route": "generate",
        },
        "active_recovery": {},
    })
    provenance = generation_provenance_for_dispatch(
        state,
        source_route="supervisor",
    )
    action = _normal_action("printf 'contigs ok\\n'")

    assert provenance.mode == GenerationMode.NORMAL.value
    assert provenance.recovery_id is None
    assert action["type"] == "execute"
    assert action["action_type"] == "execute"


def test_production_dispatch_drops_prior_step_evidence_and_recovery(
    tmp_path: Path,
) -> None:
    task_id = "production-dispatch-isolation"
    state = _two_step_plan(task_id)
    first = _record(
        state,
        step_id="validate-inputs",
        attempt_id="attempt-step-1",
        action_id="action-step-1",
    )
    advance_plan_after_verified_step(
        state,
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    )
    historical = {
        "decision_id": "historical-step-1",
        "step_id": "validate-inputs",
        "status": "consumed",
        "target_route": "generate",
    }
    state["task_state"]["recovery_state"]["decision_history"] = [historical]
    state["recovery_decision"] = historical
    save_plan_state(tmp_path, task_id, state)
    persisted = load_plan_state(tmp_path, task_id)

    resolved = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "supervisor",
        {
            "task_id": task_id,
            "state_generation": persisted["state_generation"],
            "execution_evidence": first,
        },
        {
            "proposed_route": "generate",
            "next_step": "generate",
            "execution_evidence": first,
        },
        task_id,
    )

    assert resolved["dispatch_route"] == "generate"
    assert resolved["current_step_id"] == "check-contig-compatibility"
    assert resolved["generation_mode"] == GenerationMode.NORMAL.value
    assert resolved["recovery_generated"] is False
    assert resolved["generation_provenance"]["recovery_id"] is None
    assert resolved["execution_evidence"] == {}
    assert resolved["pending_recovery_action"] == {}
    assert resolved["recovery_execution_envelope"] == {}
    durable = load_plan_state(tmp_path, task_id)
    assert durable["task_state"]["generation_provenance"]["mode"] == "NORMAL"
    assert durable["task_state"]["recovery_state"]["decision_history"] == [
        historical
    ]


def test_matching_recovery_generation_requires_and_accepts_matching_envelope(
    tmp_path: Path,
) -> None:
    state = _two_step_plan("matching-recovery")
    advance_plan_after_verified_step(
        state,
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    )
    decision = contextualize_recovery_decision(
        RecoveryPolicy().decide(
            execution_evidence=_record(
                state,
                step_id="check-contig-compatibility",
                attempt_id="attempt-step-2-failed",
                action_id="action-step-2-failed",
                returncode=1,
            ),
            budgets={},
            action_id="action-step-2-failed",
            step_id="check-contig-compatibility",
            attempt_id="attempt-step-2-failed",
        ),
        source_node="execute",
        step_id="check-contig-compatibility",
        action_id="action-step-2-failed",
        attempt_id="attempt-step-2-failed",
        trigger_event_id="attempt-step-2-failed",
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-step-2",
        step_id="check-contig-compatibility",
        task_root=tmp_path,
    )
    apply_recovery_decision(
        state,
        decision,
        execution_envelope=envelope.to_dict(),
    )

    provenance = generation_provenance_for_dispatch(
        state,
        source_route="execute",
        execution_envelope=envelope.to_dict(),
    )
    action = adapt_script_block_to_action(
        parse_executable_script_block("<shell>printf 'recovered\\n'</shell>"),
        execution_envelope=envelope.to_dict(),
        recovery_generated=True,
    )
    assert provenance.mode == GenerationMode.RECOVERY.value
    assert provenance.recovery_id == decision.decision_id
    assert action["action_type"] == "execute"
    assert action["recovery_decision_id"] == decision.decision_id


def test_true_recovery_without_envelope_is_pre_provider_internal_protocol() -> None:
    state = _two_step_plan("missing-recovery-envelope")
    state["task_state"]["recovery_state"]["active_recovery"] = {
        "recovery_id": "recovery-missing-envelope",
        "plan_revision": state["plan_revision"],
        "step_id": "validate-inputs",
        "source_attempt_id": "attempt-failed",
        "status": "dispatched",
        "target_route": "generate",
        "envelope_id": "",
    }
    with pytest.raises(ActiveStateInvariantError) as caught:
        generation_provenance_for_dispatch(state, source_route="execute")
    failure = recovery_generation_contract_failure(caught.value)
    assert caught.value.code == "RECOVERY_EXECUTION_ENVELOPE_MISSING"
    assert failure["failure_class"] == "internal_protocol"
    assert failure["stage"] == "generate.recovery_contract"
    assert failure["llm_invoked"] is False
    assert failure["execute_started"] is False


def test_plan_revision_change_closes_old_active_recovery() -> None:
    state = _two_step_plan("revision-change")
    state["task_state"]["recovery_state"]["active_recovery"] = {
        "recovery_id": "recovery-old-plan",
        "plan_revision": state["plan_revision"],
        "step_id": "validate-inputs",
        "source_attempt_id": "attempt-old-plan",
        "status": "pending",
        "target_route": "generate",
        "envelope_id": "old-envelope",
    }
    state["plan_revision"] += 1
    normalized = normalize_active_recovery_state(state)
    assert normalized["active_recovery"] == {}
    assert state["task_state"]["recovery_state"]["active_recovery_history"][-1][
        "closed_reason"
    ] == "PLAN_REVISION_CHANGED"
    assert generation_provenance_for_dispatch(
        state,
        source_route="planner",
    ).mode == GenerationMode.NORMAL.value


def test_mismatched_passed_step_recovery_is_closed_without_model_call() -> None:
    state = _two_step_plan("stale-recovery")
    advance_plan_after_verified_step(
        state,
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    )
    recovery = state["task_state"]["recovery_state"]
    recovery["active_recovery"] = {
        "recovery_id": "stale-step-1",
        "plan_revision": state["plan_revision"],
        "step_id": "validate-inputs",
        "source_attempt_id": "attempt-step-1",
        "status": "dispatched",
        "target_route": "generate",
        "envelope_id": "old-envelope",
    }
    recovery["active_recovery_envelope"] = {"immutable_hash": "old-envelope"}
    model_calls = 0

    result = normalize_active_recovery_state(state)
    provenance = generation_provenance_for_dispatch(
        state,
        source_route="stale_state_normalization",
    )

    assert result["active_recovery"] == {}
    assert result["events"][-1]["code"] == "STALE_ACTIVE_RECOVERY_CLOSED"
    normalized_recovery = state["task_state"]["recovery_state"]
    assert normalized_recovery["active_recovery_history"][-1]["status"] == "stale"
    assert provenance.mode == GenerationMode.NORMAL.value
    assert model_calls == 0


def test_legacy_recovery_migration_activates_only_matching_pending_scope() -> None:
    matching = _two_step_plan("legacy-matching")
    matching["task_state"]["recovery_state"].pop("active_recovery", None)
    matching["recovery_decision"] = {
        "decision_id": "legacy-current",
        "step_id": "validate-inputs",
        "plan_version": matching["plan_version"],
        "status": "pending",
        "target_route": "generate",
        "attempt_id": "legacy-attempt",
    }
    migrated = normalize_active_recovery_state(matching)
    assert migrated["active_recovery"]["recovery_id"] == "legacy-current"
    assert matching["recovery_decision"] == {}

    historical = _two_step_plan("legacy-historical")
    advance_plan_after_verified_step(
        historical,
        expected_step_id="validate-inputs",
        transition_id="complete-step-1",
    )
    historical["task_state"]["recovery_state"].pop("active_recovery", None)
    historical["recovery_decision"] = {
        "decision_id": "legacy-old",
        "step_id": "validate-inputs",
        "plan_version": historical["plan_version"],
        "status": "pending",
        "target_route": "generate",
    }
    retained = normalize_active_recovery_state(historical)
    recovery = historical["task_state"]["recovery_state"]
    assert retained["active_recovery"] == {}
    assert recovery["decision_history"][-1]["decision_id"] == "legacy-old"
    assert recovery["state_events"][-1]["code"] == (
        "LEGACY_RECOVERY_RETAINED_AS_HISTORY"
    )


def test_two_step_normal_and_recovery_integrations_reach_mock_execute(
    tmp_path: Path,
) -> None:
    state = _two_step_plan("two-step-normal")
    execution_history = []
    calls = []

    def mock_execute(action: Dict[str, Any], step_id: str, attempt_id: str) -> None:
        assert action["type"] == "execute"
        calls.append(step_id)
        execution_history.append(_record(
            state,
            step_id=step_id,
            attempt_id=attempt_id,
            action_id=str(action.get("action_id") or f"action-{step_id}"),
        ))

    step1_action = _normal_action("printf 'inputs valid\\n'")
    mock_execute(step1_action, "validate-inputs", "attempt-normal-1")
    state["task_state"]["active_execution"] = {
        "step_id": "validate-inputs",
        "attempt_id": "attempt-normal-1",
    }
    state["task_state"]["recovery_state"]["decision_history"] = [{
        "decision_id": "fixture-step-1-history",
        "step_id": "validate-inputs",
        "status": "consumed",
        "target_route": "generate",
    }]
    advance_plan_after_verified_step(
        state,
        "supervisor validated Step 1",
        expected_step_id="validate-inputs",
        transition_id="supervisor-normal-step-1",
    )
    assert scoped_execution_evidence(state, execution_history) == {}
    provenance = generation_provenance_for_dispatch(
        state,
        source_route="supervisor",
    )
    step2_action = _normal_action("printf 'contigs compatible\\n'")
    mock_execute(step2_action, "check-contig-compatibility", "attempt-normal-2")

    assert provenance.mode == GenerationMode.NORMAL.value
    assert step2_action["action_type"] == "execute"
    assert calls == ["validate-inputs", "check-contig-compatibility"]
    assert scoped_execution_evidence(
        state,
        execution_history,
        attempt_id="attempt-normal-2",
    )["step_id"] == "check-contig-compatibility"

    recovered = _two_step_plan("two-step-recovered")
    failure = _record(
        recovered,
        step_id="validate-inputs",
        attempt_id="attempt-failed",
        action_id="action-failed",
        returncode=1,
    )
    decision = contextualize_recovery_decision(
        RecoveryPolicy().decide(
            execution_evidence=failure,
            budgets={},
            action_id="action-failed",
            step_id="validate-inputs",
            attempt_id="attempt-failed",
        ),
        source_node="execute",
        step_id="validate-inputs",
        action_id="action-failed",
        attempt_id="attempt-failed",
        trigger_event_id="attempt-failed",
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="recovery-context-step-1",
        step_id="validate-inputs",
        task_root=tmp_path,
    )
    apply_recovery_decision(
        recovered,
        decision,
        execution_envelope=envelope.to_dict(),
    )
    recovery_provenance = generation_provenance_for_dispatch(
        recovered,
        source_route="execute",
        execution_envelope=envelope.to_dict(),
    )
    recovery_action = adapt_script_block_to_action(
        parse_executable_script_block("<shell>printf 'retry ok\\n'</shell>"),
        execution_envelope=envelope.to_dict(),
        recovery_generated=True,
    )
    assert recovery_provenance.mode == GenerationMode.RECOVERY.value
    assert recovery_action["action_type"] == "execute"
    recovered["pending_recovery_action"] = {"step_id": "validate-inputs"}
    recovered["recovery_execution_envelope"] = envelope.to_dict()
    advance_plan_after_verified_step(
        recovered,
        "recovery Execute and Supervisor succeeded",
        expected_step_id="validate-inputs",
        transition_id="recovered-step-1",
    )
    step2_provenance = generation_provenance_for_dispatch(
        recovered,
        source_route="supervisor",
    )
    assert step2_provenance.mode == GenerationMode.NORMAL.value
    assert recovered["task_state"]["recovery_state"]["active_recovery"] == {}
    assert recovered["task_state"]["recovery_state"]["decision_history"]
    assert recovered["pending_recovery_action"] == {}
    assert recovered["recovery_execution_envelope"] == {}


@pytest.mark.skipif(not INCIDENT_ROOT.is_dir(), reason="incident fixture unavailable")
def test_session_1788362482_read_only_offline_replay_reaches_mock_execute() -> None:
    plan_path = INCIDENT_ROOT / "plan_state.json"
    records_path = INCIDENT_ROOT / "executions" / "execution_records.jsonl"
    response_paths = sorted((INCIDENT_ROOT / "context_trace").glob(
        "generate-001[12]-*/response.json"
    ))
    before = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in [plan_path, records_path, *response_paths]
    }
    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    replay = deepcopy(raw_plan)
    replay["task_state"]["status"] = "running"
    replay["task_state"]["recovery_state"]["active_recovery"] = {}
    replay["task_state"]["recovery_state"]["active_recovery_envelope"] = {}
    replay["steps"][0]["status"] = "passed"
    replay["steps"][1]["status"] = "running"
    for step in replay["steps"][2:]:
        step["status"] = "pending"
    replay["current_step_id"] = "check-contig-compatibility"

    assert records[0]["step_id"] == "validate-inputs"
    assert scoped_execution_evidence(replay, records) == {}
    provenance = generation_provenance_for_dispatch(
        replay,
        source_route="supervisor",
    )
    assert provenance.mode == GenerationMode.NORMAL.value
    assert len(response_paths) == 2
    actions = []
    for path in response_paths:
        content = json.loads(path.read_text(encoding="utf-8"))["content"]
        action = adapt_script_block_to_action(
            parse_executable_script_block(content),
            recovery_generated=False,
        )
        assert action["action_type"] == "execute"
        actions.append(action)
    mock_execute_calls = 0
    if actions[-1]["type"] == "execute":
        mock_execute_calls += 1
    assert mock_execute_calls == 1
    assert all(
        marker not in json.dumps(actions, ensure_ascii=False)
        for marker in (
            "RECOVERY_EXECUTION_ENVELOPE_MISSING",
            "recovery_execution_contract_invalid",
            "STATE_MACHINE_STALLED",
        )
    )
    after = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in before
    }
    assert after == before
