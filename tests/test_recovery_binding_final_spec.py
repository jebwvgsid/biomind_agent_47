from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.active_state import ActiveStateInvariantError, generation_provenance_for_dispatch
from agent_core.diagnostics import _completion_projection, _validation_projection
from agent_core.plan_bindings import (
    PlanBindingError,
    accept_action_binding_candidates,
    accepted_plan_binding,
    invalidate_producer_bindings,
    project_accepted_plan_bindings,
    resolve_action_binding_environment,
    validate_declared_binding_operation,
)
from agent_core.plan_tracker import (
    advance_plan_after_verified_step,
    build_plan_state,
    commit_recovery_bundle,
    load_plan_state,
    record_scoped_evidence,
    rollover_recovery_generation_claim_after_candidate_rejection,
    save_plan_state,
    update_recovery_generation_claim,
)
from agent_core.node_context import build_node_context
from agent_core.task_spec import create_task_spec
from agent_core.recovery import (
    RecoveryAction,
    RecoveryCommitBundle,
    RecoveryDecision,
    RecoveryDecisionDraft,
    RecoveryExecutionContractError,
    RecoveryExecutionEnvelope,
    RecoveryPolicy,
    RecoverySourceScope,
    build_recovery_execution_envelope,
    contextualize_recovery_decision,
    finalize_recovery_decision,
)


def _semantic(reason: str = "repair the generated action") -> RecoveryDecision:
    return RecoveryDecision.from_mapping({
        "schema_version": 5,
        "failure_class": "SCRIPT_BUG",
        "failure_owner": "GENERATED_ACTION",
        "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
        "reason": reason,
        "root_cause": reason,
        "evidence": [reason],
        "confidence": 1.0,
    })


def _scope() -> RecoverySourceScope:
    return RecoverySourceScope(
        task_id="task-a",
        plan_id="plan-a",
        plan_version="plan-v1",
        step_id="step-4",
        failed_action_id="action-4",
        failed_execution_attempt_id="attempt-4",
        source_failure_fingerprint="source-fp-4",
    )


def _final(reason: str = "repair the generated action") -> RecoveryDecision:
    return finalize_recovery_decision(
        _semantic(reason),
        source_scope=_scope(),
        recovery_context_fingerprint="context-fp-4",
        source_node="execute",
        expected_state_generation=3,
    )


def test_semantic_draft_has_no_machine_authority_and_rejects_spoofed_identity():
    draft = RecoveryDecisionDraft.from_mapping({
        "schema_version": 5,
        "failure_class": "SCRIPT_BUG",
        "failure_owner": "GENERATED_ACTION",
        "recommended_action": "PATCH_SCRIPT",
        "reason": "repair",
        "evidence": ["rc=1"],
    })
    assert "task_id" not in draft.to_mapping()
    with pytest.raises(ValueError, match="IGNORE_OR_REJECT_MACHINE_IDENTITY"):
        RecoveryDecisionDraft.from_mapping({"reason": "repair", "decision_id": "spoofed"})


def test_finalized_identity_excludes_prose_and_cannot_be_rebound():
    first = _final("first wording")
    second = _final("different wording")
    assert first.decision_id == second.decision_id
    assert first.incident_id == second.incident_id
    with pytest.raises(RecoveryExecutionContractError, match="FINALIZED_SCOPE_MUTATION"):
        from agent_core.recovery import contextualize_recovery_decision

        contextualize_recovery_decision(first, step_id="step-5")


def test_finalized_decision_envelope_and_scope_are_identical(tmp_path):
    decision = _final()
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root=tmp_path,
    )
    assert decision.decision_id == envelope.recovery_decision_id
    assert decision.source_scope == envelope.source_scope
    with pytest.raises(RecoveryExecutionContractError, match="NOT_FINALIZED"):
        build_recovery_execution_envelope(
            _semantic(),
            recovery_context_id="context-4",
            step_id="step-4",
            task_root=tmp_path,
        )


def test_atomic_bundle_commits_one_active_identity_and_budget_once(tmp_path):
    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    state["plan_id"] = "plan-a"
    state["plan_version"] = "plan-v1"
    state["plan_revision"] = 1
    state["current_step_id"] = "step-4"
    state["steps"][0]["step_id"] = "step-4"
    state["steps"][0]["status"] = "running"
    save_plan_state(tmp_path, "task-a", state)
    current = load_plan_state(tmp_path, "task-a")
    decision = finalize_recovery_decision(
        _semantic(),
        source_scope=_scope(),
        recovery_context_fingerprint="context-fp-4",
        source_node="execute",
        expected_state_generation=current["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root=tmp_path,
    )
    bundle = RecoveryCommitBundle(
        decision,
        envelope,
        {},
        {},
        {"budget_class": decision.consume_budget or ""},
        current["state_generation"],
    )
    receipt = commit_recovery_bundle(tmp_path, "task-a", bundle)
    assert receipt["commit_authority"] == "plan_state_cas"
    committed = load_plan_state(tmp_path, "task-a")
    recovery = committed["task_state"]["recovery_state"]
    assert recovery["active_recovery"]["recovery_id"] == decision.decision_id
    assert recovery["active_recovery_envelope"]["recovery_decision_id"] == decision.decision_id
    duplicate = commit_recovery_bundle(tmp_path, "task-a", bundle)
    assert duplicate["duplicate"] is True


def test_complete_step_bundle_routes_to_generate_without_active_recovery(tmp_path):
    """A completion transition is not a recovery Generate invocation."""
    state = build_plan_state("task-complete", "- [ ] Step 1\n- [ ] Step 2")
    state["plan_id"] = "plan-complete"
    state["plan_version"] = "plan-v1"
    state["plan_revision"] = 1
    state["steps"][0]["step_id"] = "step-1"
    state["steps"][0]["status"] = "running"
    state["steps"][1]["step_id"] = "step-2"
    state["steps"][1]["status"] = "pending"
    state["current_step_id"] = "step-1"
    save_plan_state(tmp_path, "task-complete", state)
    current = load_plan_state(tmp_path, "task-complete")

    semantic = RecoveryPolicy().decide(
        completion_evidence={
            "completion_satisfied": True,
            "task_complete": False,
            "evidence": ["validation-current-step", "completion-gate-passed"],
        },
        action_id="action-1",
        step_id="step-1",
        attempt_id="attempt-1",
    )
    semantic = contextualize_recovery_decision(
        semantic,
        source_node="supervisor",
    )
    decision = finalize_recovery_decision(
        semantic,
        source_scope=RecoverySourceScope(
            task_id="task-complete",
            plan_id="plan-complete",
            plan_version="plan-v1",
            step_id="step-1",
            failed_action_id="action-1",
            failed_execution_attempt_id="attempt-1",
            source_failure_fingerprint="completion-gate-passed",
        ),
        recovery_context_fingerprint="completion-gate-passed",
        source_node="supervisor",
        expected_state_generation=current["state_generation"],
    )
    assert decision.kind == "COMPLETE_STEP"
    assert decision.target_route == "generate"
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="completion-gate-passed",
        step_id="step-1",
        task_root=tmp_path,
    )

    receipt = commit_recovery_bundle(
        tmp_path,
        "task-complete",
        RecoveryCommitBundle(
            decision,
            envelope,
            {},
            {},
            None,
            current["state_generation"],
        ),
    )

    committed = load_plan_state(tmp_path, "task-complete")
    recovery = committed["task_state"]["recovery_state"]
    assert receipt["applied"] is True
    assert committed["steps"][0]["status"] == "passed"
    assert committed["steps"][1]["status"] == "running"
    assert committed["current_step_id"] == "step-2"
    assert recovery["active_recovery"] == {}
    assert recovery["active_recovery_envelope"] == {}
    assert recovery["active_transition"]["decision_id"] == decision.decision_id


def test_complete_task_bundle_accepts_last_completed_step_scope(tmp_path):
    state = build_plan_state("task-final", "- [ ] Final step")
    state["plan_id"] = "plan-final"
    state["plan_version"] = "plan-v1"
    state["plan_revision"] = 1
    state["steps"][0]["step_id"] = "step-final"
    state["steps"][0]["status"] = "passed"
    state["current_step_id"] = ""
    save_plan_state(tmp_path, "task-final", state)
    current = load_plan_state(tmp_path, "task-final")

    semantic = RecoveryPolicy().decide(
        completion_evidence={
            "completion_satisfied": True,
            "task_complete": True,
            "evidence": ["final-completion-gate-passed"],
        },
        action_id="action-final",
        step_id="step-final",
        attempt_id="attempt-final",
    )
    semantic = contextualize_recovery_decision(
        semantic,
        source_node="supervisor",
    )
    decision = finalize_recovery_decision(
        semantic,
        source_scope=RecoverySourceScope(
            task_id="task-final",
            plan_id="plan-final",
            plan_version="plan-v1",
            step_id="step-final",
            failed_action_id="action-final",
            failed_execution_attempt_id="attempt-final",
            source_failure_fingerprint="final-completion-gate-passed",
        ),
        recovery_context_fingerprint="final-completion-gate-passed",
        source_node="supervisor",
        expected_state_generation=current["state_generation"],
    )
    assert decision.kind == "COMPLETE_TASK"

    receipt = commit_recovery_bundle(
        tmp_path,
        "task-final",
        RecoveryCommitBundle(
            decision,
            None,
            {},
            {},
            None,
            current["state_generation"],
        ),
    )

    committed = load_plan_state(tmp_path, "task-final")
    assert receipt["applied"] is True
    assert committed["task_state"]["status"] == "completed"
    assert (
        committed["task_state"]["recovery_state"]["active_transition"][
            "decision_id"
        ]
        == decision.decision_id
    )


def test_strict_active_state_rejects_old_envelope_before_provider():
    decision = _final()
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root="/tmp",
    ).to_dict()
    state = {
        "current_step_id": "step-4",
        "plan_revision": 1,
        "task_state": {"recovery_state": {
            "active_recovery": {
                "recovery_id": "different-active-id",
                "step_id": "step-4",
                "plan_revision": 1,
                "status": "pending",
                "target_route": "generate",
                "envelope_id": envelope["immutable_hash"],
            },
            "active_recovery_envelope": envelope,
        }},
    }
    with pytest.raises(ActiveStateInvariantError, match="ENVELOPE_MISMATCH"):
        generation_provenance_for_dispatch(state, source_route="recovery")


def test_scope_projection_never_uses_prior_step_current_evidence():
    old = {
        "task_id": "task-a", "plan_id": "plan-a", "plan_version": "v1",
        "step_id": "step-3", "action_id": "a3", "attempt_id": "e3",
        "status": "VALID", "passed": True,
    }
    state = {"validation_evidence": old, "completion_gate_result": old}
    expected = {
        "task_id": "task-a", "plan_id": "plan-a", "plan_version": "v1",
        "step_id": "step-4", "action_id": "a4", "execution_attempt_id": "e4",
    }
    assert _validation_projection(state, expected)["status"] == "ABSENT_CURRENT_EVIDENCE"
    assert _completion_projection(state, expected)["classification"] == "ABSENT_CURRENT_EVIDENCE"


def test_step_advance_preserves_history_and_clears_active_refs():
    state = build_plan_state("task-a", "- [ ] Step 1: one\n- [ ] Step 2: two")
    state["current_step_id"] = "step-1"
    state["steps"][0]["status"] = "running"
    record_scoped_evidence(
        state, "validation", {"status": "VALID", "attempt_id": "e1"},
        action_id="a1", execution_attempt_id="e1",
    )
    assert advance_plan_after_verified_step(state, expected_step_id="step-1")
    task_state = state["task_state"]
    assert task_state["validation_history"]
    assert task_state["active_validation_ref"] == {}


def _producer_action(value: str):
    return {
        "action_id": "produce-a",
        "operation": "prepare",
        "produced_bindings": {"stable_name": {"type": "string", "value": value}},
        "binding_env": {"STABLE_NAME": {"source": "produced_binding", "name": "stable_name"}},
    }


def _binding_state():
    return {
        "task_id": "task-a", "plan_id": "plan-a", "plan_version": "v1",
        "plan_revision": 1, "current_step_id": "step-3", "task_state": {},
    }


def test_binding_candidate_acceptance_requires_success_and_preserves_exact_value():
    state = _binding_state()
    action = _producer_action("opaque_A-007")
    record = {"returncode": 1, "step_id": "step-3", "action_id": "produce-a", "attempt_id": "e3"}
    assert not accept_action_binding_candidates(state, action, record, {"passed": False})
    assert accepted_plan_binding(state, "stable_name") is None
    record["returncode"] = 0
    accepted = accept_action_binding_candidates(state, action, record, {"passed": True})
    assert accepted[0].value == "opaque_A-007"


def test_downstream_binding_ref_injects_exact_value_without_shell_parsing():
    state = _binding_state()
    accept_action_binding_candidates(
        state, _producer_action("opaque_A-007"),
        {"returncode": 0, "step_id": "step-3", "action_id": "produce-a", "attempt_id": "e3"},
        {"passed": True},
    )
    consumer = {
        "code": "arbitrary executable representation",
        "consumed_bindings": {"stable_name": {"ref": "plan_binding:stable_name"}},
        "binding_env": {"STABLE_NAME": {"source": "accepted_binding", "name": "stable_name"}},
    }
    resolved = resolve_action_binding_environment(state, consumer)
    assert resolved["environment"]["STABLE_NAME"] == "opaque_A-007"
    consumer["consumed_bindings"]["stable_name"]["ref"] = "plan_binding:wrong"
    with pytest.raises(PlanBindingError, match="BINDING_REFERENCE_MISMATCH"):
        resolve_action_binding_environment(state, consumer)


def test_binding_is_immutable_and_rewind_keeps_stale_history():
    state = _binding_state()
    record = {"returncode": 0, "step_id": "step-3", "action_id": "produce-a", "attempt_id": "e3"}
    accept_action_binding_candidates(state, _producer_action("A"), record, {"passed": True})
    with pytest.raises(PlanBindingError, match="PLAN_BINDING_IMMUTABLE"):
        accept_action_binding_candidates(state, _producer_action("B"), record, {"passed": True})
    assert invalidate_producer_bindings(state, producer_step_id="step-3", reason="rewind") == 1
    assert accepted_plan_binding(state, "stable_name") is None
    assert state["task_state"]["plan_binding_history"][0]["value"] == "A"
    assert state["task_state"]["plan_binding_history"][0]["status"] == "stale"


def test_typed_skill_operation_mismatch_returns_route_free_repair_evidence():
    contract = {"operations": {"consume": {"consumes_bindings": ["stable_name"]}}}
    violations = validate_declared_binding_operation(
        {"operation": "consume", "consumed_bindings": {}}, contract
    )
    assert violations == ({
        "error_code": "BINDING_REFERENCE_MISMATCH",
        "operation": "consume",
        "binding_name": "stable_name",
        "expected_ref": "plan_binding:stable_name",
        "disposition": "REPAIR_REQUIRED",
    },)


def test_binding_module_contains_no_domain_specific_or_shell_extraction_logic():
    source = __import__("pathlib").Path(
        __import__("agent_core.plan_bindings").plan_bindings.__file__
    ).read_text(encoding="utf-8").lower()
    assert "snpeff" not in source
    assert "genome_version" not in source
    assert "generated.sh" not in source
    assert "stable_task_identifier" not in source


def test_generation_claim_lifecycle_and_local_adoption_are_exactly_once(tmp_path):
    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    state.update({
        "plan_id": "plan-a",
        "plan_version": "plan-v1",
        "current_step_id": "step-4",
    })
    state["steps"][0].update({"step_id": "step-4", "status": "running"})
    save_plan_state(tmp_path, "task-a", state)
    current = load_plan_state(tmp_path, "task-a")
    decision = finalize_recovery_decision(
        _semantic(),
        source_scope=_scope(),
        recovery_context_fingerprint="context-fp-4",
        source_node="execute",
        expected_state_generation=current["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root=tmp_path,
    )
    commit_recovery_bundle(
        tmp_path,
        "task-a",
        RecoveryCommitBundle(
            decision, envelope, {}, {}, None, current["state_generation"]
        ),
    )
    claimed = load_plan_state(tmp_path, "task-a")
    claim = claimed["task_state"]["recovery_state"]["generation_claim"]
    target = claim["target_generation_id"]
    assert claim["status"] == "issued"
    assert claim["provider_idempotency_key"] == target
    update_recovery_generation_claim(
        claimed, target_generation_id=target, status="claimed"
    )
    update_recovery_generation_claim(
        claimed, target_generation_id=target, status="provider_completed"
    )
    adopted = update_recovery_generation_claim(
        claimed,
        target_generation_id=target,
        status="action_adopted",
        action_fingerprint="action-fp",
    )
    duplicate = update_recovery_generation_claim(
        claimed,
        target_generation_id=target,
        status="action_adopted",
        action_fingerprint="action-fp",
    )
    assert adopted["changed"] is True
    assert duplicate["duplicate"] is True
    with pytest.raises(ValueError, match="ALREADY_ADOPTED"):
        update_recovery_generation_claim(
            claimed,
            target_generation_id=target,
            status="action_adopted",
            action_fingerprint="different-action",
        )


def test_recovery_candidate_adoption_rejects_missing_active_recovery(tmp_path):
    from bio_agents import BioAnalystAgent

    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    save_plan_state(tmp_path, "task-a", state)
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = tmp_path

    with pytest.raises(
        ValueError,
        match="RECOVERY_GENERATION_ADOPTION_SCOPE_MISMATCH",
    ):
        agent._adopt_recovery_generation_candidate(
            "task-a",
            expected_recovery_id="recovery-missing",
            expected_recovery_envelope_id="envelope-missing",
            expected_target_generation_id="generation-missing",
            candidate={},
            action_fingerprint="candidate-fingerprint",
        )


def test_recovery_candidate_duplicate_adoption_requires_durable_pending_action(
    tmp_path,
):
    from bio_agents import BioAnalystAgent

    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    state.update({
        "plan_id": "plan-a",
        "plan_version": "plan-v1",
        "current_step_id": "step-4",
    })
    state["steps"][0].update({"step_id": "step-4", "status": "running"})
    save_plan_state(tmp_path, "task-a", state)
    current = load_plan_state(tmp_path, "task-a")
    decision = finalize_recovery_decision(
        _semantic(),
        source_scope=_scope(),
        recovery_context_fingerprint="context-fp-4",
        source_node="execute",
        expected_state_generation=current["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root=tmp_path,
    )
    commit_recovery_bundle(
        tmp_path,
        "task-a",
        RecoveryCommitBundle(
            decision, envelope, {}, {}, None, current["state_generation"]
        ),
    )
    durable = load_plan_state(tmp_path, "task-a")
    target = durable["task_state"]["recovery_state"]["generation_claim"][
        "target_generation_id"
    ]
    update_recovery_generation_claim(
        durable,
        target_generation_id=target,
        status="claimed",
    )
    update_recovery_generation_claim(
        durable,
        target_generation_id=target,
        status="provider_completed",
    )
    update_recovery_generation_claim(
        durable,
        target_generation_id=target,
        status="action_adopted",
        action_fingerprint="candidate-fingerprint",
    )
    save_plan_state(tmp_path, "task-a", durable)
    persisted = load_plan_state(tmp_path, "task-a")
    persisted_recovery = persisted["task_state"]["recovery_state"]
    persisted_active = persisted_recovery["active_recovery"]
    candidate = {
        "action_id": "candidate-action",
        "action_hash": "candidate-action-hash",
        "step_id": persisted["current_step_id"],
        "plan_id": persisted["plan_id"],
        "plan_version": persisted["plan_version"],
        "plan_revision": persisted["plan_revision"],
    }
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = tmp_path

    with pytest.raises(
        ValueError,
        match="RECOVERY_GENERATION_ADOPTED_ACTION_MISSING",
    ):
        agent._adopt_recovery_generation_candidate(
            "task-a",
            expected_recovery_id=persisted_active["recovery_id"],
            expected_recovery_envelope_id=persisted_active["envelope_id"],
            expected_target_generation_id=(
                persisted_active["target_generation_id"]
            ),
            candidate=candidate,
            action_fingerprint="candidate-fingerprint",
        )


def test_recovery_candidate_adoption_rejects_superseded_dispatch_target(
    tmp_path,
):
    from bio_agents import BioAnalystAgent

    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    state.update({
        "plan_id": "plan-a",
        "plan_version": "plan-v1",
        "current_step_id": "step-4",
    })
    state["steps"][0].update({"step_id": "step-4", "status": "running"})
    save_plan_state(tmp_path, "task-a", state)
    current = load_plan_state(tmp_path, "task-a")
    decision = finalize_recovery_decision(
        _semantic(),
        source_scope=_scope(),
        recovery_context_fingerprint="context-fp-4",
        source_node="execute",
        expected_state_generation=current["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root=tmp_path,
    )
    commit_recovery_bundle(
        tmp_path,
        "task-a",
        RecoveryCommitBundle(
            decision, envelope, {}, {}, None, current["state_generation"]
        ),
    )
    durable = load_plan_state(tmp_path, "task-a")
    recovery_before = durable["task_state"]["recovery_state"]
    active_before = dict(recovery_before["active_recovery"])
    old_target = active_before["target_generation_id"]
    update_recovery_generation_claim(
        durable,
        target_generation_id=old_target,
        status="claimed",
    )
    update_recovery_generation_claim(
        durable,
        target_generation_id=old_target,
        status="provider_completed",
    )
    rollover = rollover_recovery_generation_claim_after_candidate_rejection(
        durable,
        target_generation_id=old_target,
        rejection_event_id="candidate-rejection-race",
        candidate_fingerprint="candidate-fingerprint-race",
        repair_ordinal=1,
        receipt_ref="state://candidate-repair/receipt-race",
    )
    save_plan_state(tmp_path, "task-a", durable)

    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = tmp_path
    with pytest.raises(
        ValueError,
        match="RECOVERY_GENERATION_ADOPTION_SCOPE_MISMATCH",
    ):
        agent._adopt_recovery_generation_candidate(
            "task-a",
            expected_recovery_id=active_before["recovery_id"],
            expected_recovery_envelope_id=active_before["envelope_id"],
            expected_target_generation_id=old_target,
            candidate={},
            action_fingerprint="late-candidate-fingerprint",
        )

    persisted = load_plan_state(tmp_path, "task-a")
    persisted_recovery = persisted["task_state"]["recovery_state"]
    assert (
        persisted_recovery["generation_claim"]["target_generation_id"]
        == rollover["target_generation_id"]
    )
    assert persisted_recovery["generation_claim"]["status"] == "issued"
    assert (persisted.get("task_state") or {}).get("pending_action") in (
        None,
        {},
    )


def test_candidate_rejection_rolls_generation_claim_without_replacing_recovery_envelope(
    tmp_path,
):
    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    state.update({
        "plan_id": "plan-a",
        "plan_version": "plan-v1",
        "current_step_id": "step-4",
    })
    state["steps"][0].update({"step_id": "step-4", "status": "running"})
    save_plan_state(tmp_path, "task-a", state)
    current = load_plan_state(tmp_path, "task-a")
    decision = finalize_recovery_decision(
        _semantic(),
        source_scope=_scope(),
        recovery_context_fingerprint="context-fp-4",
        source_node="execute",
        expected_state_generation=current["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id="context-4",
        step_id="step-4",
        task_root=tmp_path,
    )
    commit_recovery_bundle(
        tmp_path,
        "task-a",
        RecoveryCommitBundle(
            decision, envelope, {}, {}, None, current["state_generation"]
        ),
    )
    durable = load_plan_state(tmp_path, "task-a")
    recovery_before = durable["task_state"]["recovery_state"]
    active_before = dict(recovery_before["active_recovery"])
    envelope_before = dict(recovery_before["active_recovery_envelope"])
    old_target = active_before["target_generation_id"]
    update_recovery_generation_claim(
        durable, target_generation_id=old_target, status="claimed"
    )
    update_recovery_generation_claim(
        durable, target_generation_id=old_target, status="provider_completed"
    )

    rollover = rollover_recovery_generation_claim_after_candidate_rejection(
        durable,
        target_generation_id=old_target,
        rejection_event_id="candidate-rejection-1",
        candidate_fingerprint="candidate-fp-1",
        repair_ordinal=1,
        receipt_ref="state://candidate-repair/receipt-1",
    )

    recovery_after = durable["task_state"]["recovery_state"]
    active_after = recovery_after["active_recovery"]
    claim_after = recovery_after["generation_claim"]
    old_claim = recovery_after["generation_claim_history"][-1]
    assert rollover["prior_target_generation_id"] == old_target
    assert rollover["target_generation_id"] != old_target
    assert active_after["recovery_id"] == active_before["recovery_id"]
    assert active_after["envelope_id"] == active_before["envelope_id"]
    assert active_after["target_generation_id"] == rollover["target_generation_id"]
    assert recovery_after["active_recovery_envelope"] == envelope_before
    assert old_claim["target_generation_id"] == old_target
    assert old_claim["status"] == "superseded"
    assert old_claim["candidate_rejection_event_id"] == "candidate-rejection-1"
    assert claim_after["status"] == "issued"
    assert claim_after["parent_target_generation_id"] == old_target
    assert claim_after["provider_idempotency_key"] == claim_after["target_generation_id"]
    assert claim_after["receipt_ref"] == "state://candidate-repair/receipt-1"


def test_generation_claim_restart_states_fail_closed_or_resume_same_target():
    state = {
        "task_state": {"recovery_state": {
            "active_recovery": {
                "recovery_id": "recovery-a",
                "target_generation_id": "generation-a",
            },
            "generation_claim": {
                "recovery_id": "recovery-a",
                "target_generation_id": "generation-a",
                "status": "claimed",
            },
        }}
    }
    unknown = update_recovery_generation_claim(
        state,
        target_generation_id="generation-a",
        status="outcome_unknown",
    )
    resumed = update_recovery_generation_claim(
        state,
        target_generation_id="generation-a",
        status="claimed",
    )
    assert unknown["status"] == "outcome_unknown"
    assert resumed["target_generation_id"] == "generation-a"
    with pytest.raises(ValueError, match="TARGET_MISMATCH"):
        update_recovery_generation_claim(
            state,
            target_generation_id="generation-b",
            status="failed",
        )


def test_terminal_chain_preserves_execution_decision_transition_and_blocker(tmp_path):
    from bio_agents import BioAnalystAgent

    state = build_plan_state("task-a", "- [ ] Step 1: repair")
    state.update({
        "plan_id": "plan-a",
        "plan_version": "plan-v1",
        "current_step_id": "step-4",
        "primary_execution_failure": {
            "failure_id": "failure-execute",
            "error_code": "PRIMARY_EXECUTION_FAILED",
            "failure_stage": "execute",
        },
    })
    state["steps"][0].update({"step_id": "step-4", "status": "running"})
    decision = _final()
    state.setdefault("task_state", {}).setdefault("recovery_state", {}).update({
        "last_recovery_decision": decision.to_dict(),
        "active_transition": {
            **decision.to_dict(),
            "status": "pending",
        },
    })
    save_plan_state(tmp_path, "task-a", state)
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = tmp_path
    agent._commit_terminal_internal_protocol_failure(
        "task-a",
        {
            "failure_id": "failure-transition",
            "error_code": "RECOVERY_EXECUTION_ENVELOPE_MISMATCH",
            "failure_stage": "generate.recovery.contract",
            "message": "decision and envelope differ",
        },
    )
    persisted = load_plan_state(tmp_path, "task-a")
    assert persisted["primary_execution_failure"]["error_code"] == (
        "PRIMARY_EXECUTION_FAILED"
    )
    assert persisted["recovery_decision_summary"]["decision_id"] == (
        decision.decision_id
    )
    assert persisted["recovery_transition_failure"]["error_code"] == (
        "RECOVERY_EXECUTION_ENVELOPE_MISMATCH"
    )
    assert persisted["terminal_failure"]["failure_id"] == "failure-transition"


def test_snpeff_skill_declares_typed_producer_build_and_annotation_operations():
    document = (
        Path(__file__).resolve().parents[1]
        / "skills/genomics/snpeff-annotation/SKILL.md"
    ).read_text(encoding="utf-8")
    assert "prepare_database:" in document
    assert "produces_bindings: [genome_version, snpeff_db]" in document
    assert "build_database:" in document
    assert "annotate_variants:" in document
    assert document.count("consumes_bindings: [genome_version, snpeff_db]") >= 2
    assert "owner: generate" in document


def test_typed_execute_parser_preserves_binding_declarations():
    from bio_agents import BioAnalystAgent

    action = BioAnalystAgent._extract_structured_action(json.dumps({
        "action_type": "execute",
        "operation": "prepare_database",
        "code": "printf '%s\\n' \"$GENOME_VERSION\"",
        "produced_bindings": {
            "genome_version": {
                "type": "string", "value": "biomind_bm_remain_annot_007"
            }
        },
        "binding_env": {
            "GENOME_VERSION": {
                "source": "produced_binding", "name": "genome_version"
            }
        },
    }))
    assert action["operation"] == "prepare_database"
    assert action["produced_bindings"]["genome_version"]["value"] == (
        "biomind_bm_remain_annot_007"
    )
    assert action["binding_env"]["GENOME_VERSION"]["source"] == (
        "produced_binding"
    )


def test_downstream_generate_context_projects_exact_accepted_binding():
    state = build_plan_state("task-a", "- [ ] Step 1: consume")
    state["steps"][0]["status"] = "running"
    accept_action_binding_candidates(
        state,
        _producer_action("biomind_bm_remain_annot_007"),
        {
            "returncode": 0,
            "step_id": state["current_step_id"],
            "action_id": "produce-a",
            "attempt_id": "e3",
        },
        {"passed": True},
    )
    context = build_node_context(
        node_type="generate",
        task_spec=create_task_spec("consume binding", task_id="task-a"),
        plan_state=state,
        current_step_id=state["current_step_id"],
    )
    projected = context.llm_payload.to_dict()["current_focus"]["runtime"]
    assert projected["accepted_plan_bindings"][0]["value"] == (
        "biomind_bm_remain_annot_007"
    )


def test_session_1788401254_shaped_replay_preserves_exact_binding_and_recovery_identity(
    tmp_path,
):
    fixture = json.loads((
        Path(__file__).parent
        / "fixtures/session_1788401254_recovery_binding_replay.json"
    ).read_text(encoding="utf-8"))
    state = build_plan_state(
        fixture["task_id"],
        "- [ ] Step 3: prepare\n- [ ] Step 4: annotate",
    )
    state.update({
        "plan_id": fixture["plan_id"],
        "plan_version": fixture["plan_version"],
        "plan_revision": 1,
        "current_step_id": fixture["producer_step_id"],
    })
    state["steps"][0].update({
        "step_id": fixture["producer_step_id"], "status": "running"
    })
    state["steps"][1].update({
        "step_id": fixture["consumer_step_id"], "status": "pending"
    })
    producer = {
        "action_id": fixture["producer_action_id"],
        "operation": "prepare_database",
        "produced_bindings": {
            "genome_version": {
                "type": "string", "value": fixture["accepted_value"]
            },
            "snpeff_db": {"type": "artifact_ref", "value": "artifact-db"},
        },
        "binding_env": {
            "GENOME_VERSION": {
                "source": "produced_binding", "name": "genome_version"
            }
        },
    }
    accept_action_binding_candidates(
        state,
        producer,
        {
            "returncode": 0,
            "step_id": fixture["producer_step_id"],
            "action_id": fixture["producer_action_id"],
            "attempt_id": fixture["producer_attempt_id"],
        },
        {"passed": True},
    )
    assert advance_plan_after_verified_step(
        state, expected_step_id=fixture["producer_step_id"]
    )
    projected = project_accepted_plan_bindings(state)
    exact = next(item for item in projected if item["name"] == "genome_version")
    assert exact["value"] == fixture["accepted_value"]
    consumer = {
        "operation": "annotate_variants",
        "consumed_bindings": {
            "genome_version": {"ref": "plan_binding:genome_version"},
            "snpeff_db": {"ref": "plan_binding:snpeff_db"},
        },
        "binding_env": {
            "GENOME_VERSION": {
                "source": "accepted_binding", "name": "genome_version"
            }
        },
    }
    resolved = resolve_action_binding_environment(state, consumer)
    assert resolved["environment"]["GENOME_VERSION"] == fixture["accepted_value"]
    assert fixture["incorrect_regenerated_value"] not in json.dumps(
        resolved, ensure_ascii=False
    )

    save_plan_state(tmp_path, fixture["task_id"], state)
    current = load_plan_state(tmp_path, fixture["task_id"])
    scope = RecoverySourceScope(
        task_id=fixture["task_id"],
        plan_id=fixture["plan_id"],
        plan_version=fixture["plan_version"],
        step_id=fixture["consumer_step_id"],
        failed_action_id=fixture["failed_action_id"],
        failed_execution_attempt_id=fixture["failed_attempt_id"],
        source_failure_fingerprint=fixture["source_failure_fingerprint"],
    )
    decision = finalize_recovery_decision(
        _semantic("repair the failed annotation action"),
        source_scope=scope,
        recovery_context_fingerprint=fixture["recovery_context_fingerprint"],
        source_node="execute",
        expected_state_generation=current["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision,
        recovery_context_id=fixture["recovery_context_fingerprint"],
        step_id=fixture["consumer_step_id"],
        task_root=tmp_path,
    )
    commit_recovery_bundle(
        tmp_path,
        fixture["task_id"],
        RecoveryCommitBundle(
            decision, envelope, {}, {}, None, current["state_generation"]
        ),
    )
    committed = load_plan_state(tmp_path, fixture["task_id"])
    recovery = committed["task_state"]["recovery_state"]
    assert recovery["active_recovery"]["recovery_id"] == decision.decision_id
    assert recovery["active_recovery_envelope"]["recovery_decision_id"] == decision.decision_id
    assert recovery["authoritative_commit"]["decision"]["decision_id"] == decision.decision_id
