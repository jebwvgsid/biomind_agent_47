"""Offline routing tests for pre-provider Generate prompt failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.node_context import ControlMetadataLeakError
from agent_core.plan_tracker import (
    build_plan_state,
    load_plan_state,
    save_plan_state,
)
from bio_agents import (
    BioAnalystAgent,
    GeneratePromptAssemblyFailure,
    assemble_generate_prompt_deterministically,
    enforce_runtime_state_invariants,
    final_audit_entry_eligible,
    pre_llm_internal_protocol_failure,
    prompt_assembly_failure_records,
    runtime_state_invariant_violations,
)


def _new_plan(root: Path, task_id: str) -> None:
    state = build_plan_state(
        task_id,
        "- [>] Step 1: Assemble Generate request | Action: EXECUTE | "
        "Skills: [] | Done: request reaches provider\n"
        "- [ ] Step 2: Execute | Action: EXECUTE | Skills: [] | "
        "Done: output exists",
    )
    state["current_step_id"] = str(state["steps"][0]["step_id"])
    state.setdefault("task_state", {})["status"] = "running"
    save_plan_state(root, task_id, state)


def _agent(root: Path) -> BioAnalystAgent:
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = root
    return agent


def test_control_metadata_leak_is_terminal_before_provider_or_execute() -> None:
    counts = {"assembly": 0, "provider": 0, "execute": 0, "final_audit": 0}

    def builder():
        counts["assembly"] += 1
        raise ControlMetadataLeakError(
            "LLM_CONTROL_METADATA_LEAK: prohibited control metadata at "
            "$LLMPayload.current_focus.current_step.capability_contract_hash"
        )

    with pytest.raises(GeneratePromptAssemblyFailure) as caught:
        assemble_generate_prompt_deterministically(builder)

    primary, secondary = prompt_assembly_failure_records(caught.value)
    terminal = enforce_runtime_state_invariants({
        "task_status": "blocked",
        "next_step": "finish",
        "pending_recovery_action": {},
        "primary_failure": primary,
    })

    assert counts == {
        "assembly": 1,
        "provider": 0,
        "execute": 0,
        "final_audit": 0,
    }
    assert primary["error_code"] == "LLM_CONTROL_METADATA_LEAK"
    assert primary["failure_class"] == "internal_protocol"
    assert primary["failure_stage"] == "generate.prompt_assembly"
    assert primary["retryable"] is False
    assert primary["exception_evidence"]["control_metadata_paths"] == [
        "$LLMPayload.current_focus.current_step.capability_contract_hash"
    ]
    assert secondary == []
    assert terminal["next_step"] == "finish"
    assert terminal["pending_recovery_action"] == {}
    assert runtime_state_invariant_violations(terminal) == []
    assert pre_llm_internal_protocol_failure(terminal) is True
    assert final_audit_entry_eligible(terminal) is False


def test_other_assembly_error_gets_exactly_one_controller_rebuild() -> None:
    calls = []

    def builder():
        calls.append(len(calls) + 1)
        raise RuntimeError(f"assembly-{len(calls)}")

    with pytest.raises(GeneratePromptAssemblyFailure) as caught:
        assemble_generate_prompt_deterministically(builder)

    primary, secondary = prompt_assembly_failure_records(caught.value)
    assert calls == [1, 2]
    assert primary["message"] == "assembly-1"
    assert primary["deterministic_rebuild_attempted"] is True
    assert [item["message"] for item in secondary] == ["assembly-2"]
    assert secondary[0]["caused_by_failure_id"] == primary["failure_id"]


def test_pending_recovery_and_terminal_status_never_coexist() -> None:
    normalized_recovery = enforce_runtime_state_invariants({
        "task_status": "running",
        "next_step": "finish",
        "pending_recovery_action": {
            "type": "diagnostic_probe",
            "reason": "needs deterministic evidence",
        },
        "final_audit_requested": True,
    })
    assert normalized_recovery["task_status"] == "diagnosing"
    assert normalized_recovery["next_step"] == "supervisor"
    assert normalized_recovery["final_audit_requested"] is False
    assert runtime_state_invariant_violations(normalized_recovery) == []

    normalized_terminal = enforce_runtime_state_invariants({
        "task_status": "blocked",
        "next_step": "supervisor_final_audit",
        "pending_recovery_action": {"type": "internal_protocol_error"},
        "final_audit_requested": True,
    })
    assert normalized_terminal["next_step"] == "finish"
    assert normalized_terminal["pending_recovery_action"] == {}
    assert normalized_terminal["final_audit_requested"] is False
    assert runtime_state_invariant_violations(normalized_terminal) == []


def test_terminal_commit_preserves_primary_and_blocks_current_step(
    tmp_path: Path,
) -> None:
    task_id = "prompt-terminal"
    _new_plan(tmp_path, task_id)
    agent = _agent(tmp_path)
    leak = ControlMetadataLeakError(
        "LLM_CONTROL_METADATA_LEAK: prohibited control metadata at "
        "$LLMPayload.semantic_history.approved_plan.steps[0].capability_contract_hash"
    )
    first_failure = GeneratePromptAssemblyFailure(leak)
    primary, secondary = prompt_assembly_failure_records(first_failure)

    guarded = agent._apply_transition_guard(
        "generate",
        {"task_id": task_id},
        {
            "next_step": "finish",
            "pending_recovery_action": {},
            "terminal_internal_protocol_failure": {
                "primary_failure": primary,
                "secondary_failures": secondary,
            },
        },
        task_id,
    )
    normalized = enforce_runtime_state_invariants(
        guarded,
        durable_task_status="blocked",
    )
    resolved = agent._resolve_and_commit_effective_transition(
        "generate",
        {"task_id": task_id},
        normalized,
        task_id,
    )

    persisted = load_plan_state(tmp_path, task_id)
    assert resolved["dispatch_route"] == "finish"
    assert resolved["pending_recovery_action"] == {}
    assert resolved["final_audit_requested"] is False
    assert persisted["task_state"]["status"] == "blocked"
    assert persisted["primary_failure"]["error_code"] == "LLM_CONTROL_METADATA_LEAK"
    assert persisted["failure"]["error_code"] == "LLM_CONTROL_METADATA_LEAK"
    assert persisted["task_state"]["recovery_state"]["active_transition"] == {}
    current = next(
        step
        for step in persisted["steps"]
        if str(step.get("step_id")) == str(persisted["current_step_id"])
    )
    assert current["status"] == "blocked"
    assert current["internal_protocol_blocked"] is True
    assert current["failure_stage"] == "generate.prompt_assembly"

    later = dict(primary)
    later.update({
        "failure_id": "failure-later",
        "error_code": "NO_SUCCESSFUL_EXECUTE_EVIDENCE",
        "message": "secondary final-audit symptom",
    })
    committed_primary, committed_secondary, _ = (
        agent._commit_terminal_internal_protocol_failure(
            task_id,
            later,
        )
    )
    assert committed_primary["error_code"] == "LLM_CONTROL_METADATA_LEAK"
    assert [item["error_code"] for item in committed_secondary] == [
        "NO_SUCCESSFUL_EXECUTE_EVIDENCE"
    ]


def test_supervisor_cannot_route_to_itself_without_revision_progress(
    tmp_path: Path,
) -> None:
    task_id = "supervisor-self-loop"
    _new_plan(tmp_path, task_id)
    resolved = _agent(tmp_path)._resolve_and_commit_effective_transition(
        "supervisor",
        {"task_id": task_id, "task_revision": 0},
        {"next_step": "supervisor", "proposed_route": "supervisor"},
        task_id,
    )
    assert resolved["dispatch_route"] == "finish"
    assert resolved["effective_route"] == "blocked"
    assert resolved["recovery_status"] == "SUPERVISOR_SELF_LOOP_BLOCKED"
    assert resolved["transition_guard"] == "supervisor_self_loop_blocked"
