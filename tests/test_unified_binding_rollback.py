"""R1: real persistence, deterministic validation, Commit and exact consumption.

Run with tests/offline_managed_bootstrap.py in an explicitly exported candidate.
Only the external process result and optional model reviewer are fixture inputs.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_bindings import (
    PlanBindingError, accept_action_binding_candidates, accepted_plan_binding,
    reconcile_rollback_binding_history, resolve_action_binding_environment,
)
from agent_core.plan_tracker import (
    _apply_upstream_step_rollback, apply_recovery_decision, approve_plan_state, build_plan_state,
    load_plan_state, mark_step_verification_pending, record_provenance_event,
    save_plan_state, plan_state_path,
)
from agent_core.step_effects import canonical_step_effect
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.verifier import TypedSupervisor
from supervisor import SupervisorNode


def _setup(root):
    task_id = "unified-binding-rollback"
    task = create_task_spec("Choose three opaque keys and consume the final key.", task_id=task_id,
                            base_dir=root, project_root=root)
    steps = []
    for name, deps in (("d", []), ("a", []), ("b", ["a"]), ("c", ["b"])):
        steps.append({"step_id": name, "title": "Choose key " + name, "action": "execute",
                      "purpose": "analysis", "tool": "python", "skill_ids": [],
                      "workflow_stage_ids": [], "batch_id": "",
                      "dependencies": deps, "inputs": [], "outputs": [],
                      "produces_bindings": [name + "_key"], "operation": "choose_key",
                      "execution_capability_id": "fixture.choose_key", "risk_level": "low",
                      "success_criteria": ["Record the chosen opaque key."]})
    plan = build_plan_state(task_id, json.dumps({"steps": steps, "validation_checklist": [],
            "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    for step in plan["steps"]:
        step["step_effect"] = canonical_step_effect(step)
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
                current_task={"task_spec": task, "task_revision": task_spec_revision(task)})
    plan["task_state"]["workflow_routing_schema_version"] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="isolated_fixture_user_approval")
    save_plan_state(root, task_id, plan)
    return task_id


class _Reviewer:
    async def ainvoke(self, messages, **kwargs):
        import re
        content = "\n".join(str(item.content) for item in messages)
        report = json.loads(re.search(r"<completion_report>\s*(.*?)\s*</completion_report>", content, re.S)[1])
        return AIMessage(content=json.dumps({"report_id": report["report_id"], "assessment": "ACCEPTABLE",
            "disposition": "completion_accepted", "issue_dispositions": [], "concerns": [],
            "suggestions": [], "confidence": .99}))


def _commit(root, task_id, value):
    async def run():
        plan = load_plan_state(root, task_id)
        step_id = plan["current_step_id"]
        step = next(item for item in plan["steps"] if item["step_id"] == step_id)
        action = {"action_id": "action-" + step_id + "-" + value, "step_id": step_id,
                  "plan_id": plan["plan_id"], "plan_version": plan["plan_version"],
                  "type": "execute", "purpose": "analysis", "command_role": "analysis_execution",
                  "operation": "choose_key", "code": "# external executor fixture\n",
                  "outputs": [], "produced_bindings": {step_id + "_key": {"type": "string", "value": value}},
                  "expected_step_effect": canonical_step_effect(step)}
        plan["task_state"]["pending_action"] = deepcopy(action)
        record_provenance_event(plan, "action_bound", {"action": action})
        attempt = record_provenance_event(plan, "attempt_started", {"action_id": action["action_id"], "step_id": step_id})
        execution = {"task_id": task_id, "execution_id": "execution-" + action["action_id"],
                     "plan_id": plan["plan_id"], "plan_version": plan["plan_version"],
                     "step_id": step_id, "action_id": action["action_id"], "attempt_id": attempt["attempt_id"],
                     "exit_code": 0, "returncode": 0, "execution_status": "succeeded",
                     "command_role": "analysis_execution", "purpose": "analysis", "raw_stdout": "",
                     "raw_stderr": "", "declared_outputs": [], "produced_artifacts": [],
                     "produced_bindings": deepcopy(action["produced_bindings"]),
                     "step_effect": canonical_step_effect(step), "declared_step_effect": canonical_step_effect(step)}
        record_provenance_event(plan, "execution_finished", {"attempt_id": attempt["attempt_id"], "execution_record": execution})
        mark_step_verification_pending(plan, step_id=step_id, attempt_id=attempt["attempt_id"])
        save_plan_state(root, task_id, plan)
        supplied = {"task_id": task_id, "workflow_routing_schema_version": 2,
                    "validation_level": "scientific", "pending_action": action, "execution_evidence": execution}
        verifier = TypedSupervisor(data_path=str(root), verifier=SupervisorNode(
            _Reviewer(), output_dir=str(root / "task_outputs")).run)
        supplied.update(await verifier.run(supplied))
        assert supplied["verification_outcome"]["type"] in {"verified", "verified_with_warnings"}, supplied
        committed = await CommitStep(data_path=str(root)).run(supplied)
        assert committed["commit_step_outcome"]["type"] in {"next_step_ready", "plan_complete"}, committed
        return committed
    return asyncio.run(run())


def _old_commits(root):
    task_id = _setup(root)
    for value in ("d-old", "a-old", "b-old"):
        _commit(root, task_id, value)
    return task_id, load_plan_state(root, task_id)


def test_rollback_reload_commit_and_consume_exact_new_identity(tmp_path):
    task_id, state = _old_commits(tmp_path)
    old = {name: accepted_plan_binding(state, name + "_key") for name in "dab"}
    unrelated = deepcopy(state["steps"][0])
    decision = {"identity_finalized": True, "decision_owner": "RecoveryPolicy",
                "decision_id": "fixture-finalized-rollback", "kind": "ROLLBACK_UPSTREAM_STEP",
                "step_id": "c", "failed_step_id": "c", "rollback_to_step_id": "a",
                "task_id": task_id, "plan_id": state["plan_id"], "plan_version": state["plan_version"],
                "next_route": "supervisor", "reason": "Explicit synthetic upstream dependency failure"}
    result = apply_recovery_decision(state, decision)
    assert result["affected_step_ids"] == ["a", "b", "c"]
    assert set(result["invalidated_binding_ids"]) == {old["a"].binding_id, old["b"].binding_id}
    save_plan_state(tmp_path, task_id, state)
    state = load_plan_state(tmp_path, task_id)
    assert accepted_plan_binding(state, "a_key") is None
    assert accepted_plan_binding(state, "b_key") is None
    assert state["steps"][0] == unrelated
    assert accepted_plan_binding(state, "d_key") == old["d"]
    assert apply_recovery_decision(state, decision)["duplicate"]
    save_plan_state(tmp_path, task_id, state)
    _commit(tmp_path, task_id, "a-new")
    _commit(tmp_path, task_id, "b-new")
    state = load_plan_state(tmp_path, task_id)
    new = accepted_plan_binding(state, "b_key")
    assert new.value == "b-new" and new.revision == 2 and new.binding_id != old["b"].binding_id
    assert apply_recovery_decision(state, decision)["duplicate"]
    assert accepted_plan_binding(state, "b_key") == new
    assert reconcile_rollback_binding_history(state) is False
    resolved = resolve_action_binding_environment(state, {
        "consumed_bindings": {"b_key": {"ref": "plan_binding:b_key"}},
        "binding_env": {"B_KEY": {"source": "accepted_binding", "name": "b_key"}},
    })
    assert resolved["environment"] == {"B_KEY": "b-new"}
    assert new.binding_id in json.dumps(resolved) and old["b"].binding_id not in json.dumps(resolved)
    with pytest.raises(PlanBindingError, match="PLAN_BINDING_IMMUTABLE"):
        accept_action_binding_candidates(state, {"produced_bindings": {"b_key": {"type": "string", "value": "unapproved-change"}}},
                                         {"step_id": "b", "returncode": 0}, {"status": "valid"})


def test_persisted_residue_repaired_and_old_event_preserves_later_commit(tmp_path):
    task_id, state = _old_commits(tmp_path)
    old_history = deepcopy(state["task_state"]["plan_binding_history"])
    old_index = deepcopy(state["task_state"]["accepted_plan_bindings"])
    transition = _apply_upstream_step_rollback(state, {"failed_step_id": "c", "rollback_to_step_id": "a"})
    # A pre-fix checkpoint has the real rollback state plus the bug's remaining
    # descendant acceptance. Historical audit is explicit, not recomputed DAG.
    event = {"decision_id": "legacy-rollback", "decision_kind": "ROLLBACK_UPSTREAM_STEP",
             "apply_status": "applied", "plan_id": state["plan_id"], "plan_version": state["plan_version"],
             "plan_revision": state["plan_revision"], "state_generation": state["state_generation"],
             "affected_steps": transition["affected_step_ids"]}
    state["task_state"].setdefault("recovery_state", {})["decision_audit"] = [event]
    save_plan_state(tmp_path, task_id, state)
    # Write only this isolated fixture's legacy runtime residue after a real
    # save, so the read boundary (not just save migration) must repair it.
    path = plan_state_path(tmp_path, task_id)
    checkpoint = json.loads(path.read_text())
    checkpoint["task_state"]["plan_binding_history"] = old_history
    checkpoint["task_state"]["accepted_plan_bindings"] = old_index
    path.write_text(json.dumps(checkpoint))
    state = load_plan_state(tmp_path, task_id)
    assert accepted_plan_binding(state, "a_key") is None and accepted_plan_binding(state, "b_key") is None
    assert json.loads(path.read_text())["task_state"]["accepted_plan_bindings"] == {"d_key": old_index["d_key"]}
    assert accepted_plan_binding(state, "d_key").value == "d-old"
    _commit(tmp_path, task_id, "a-new")
    _commit(tmp_path, task_id, "b-new")
    state = load_plan_state(tmp_path, task_id)
    binding = accepted_plan_binding(state, "b_key")
    assert binding.value == "b-new" and binding.revision == 2
    assert reconcile_rollback_binding_history(state) is False
    save_plan_state(tmp_path, task_id, state)
    assert accepted_plan_binding(load_plan_state(tmp_path, task_id), "b_key") == binding


@pytest.mark.parametrize("missing", ["plan_revision", "state_generation"])
def test_ambiguous_legacy_history_diagnosed_without_revoking_acceptance(tmp_path, missing):
    task_id, state = _old_commits(tmp_path)
    binding = accepted_plan_binding(state, "b_key")
    event = {"decision_id": "ambiguous", "decision_kind": "ROLLBACK_UPSTREAM_STEP", "apply_status": "applied",
             "plan_id": state["plan_id"], "plan_version": state["plan_version"], "plan_revision": state["plan_revision"],
             "state_generation": state["state_generation"], "affected_steps": ["b"]}
    event.pop(missing)
    state["task_state"].setdefault("recovery_state", {})["decision_audit"] = [event]
    assert reconcile_rollback_binding_history(state)
    assert accepted_plan_binding(state, "b_key") == binding
    assert state["task_state"]["plan_binding_reconciliation_diagnostics"][0]["code"].endswith("UNVERIFIABLE")


def test_actual_limited_rollback_and_plan_scope_are_respected(tmp_path):
    task_id, state = _old_commits(tmp_path)
    unrelated = accepted_plan_binding(state, "b_key")
    foreign = deepcopy(state["task_state"]["plan_binding_history"][1])
    foreign.update(binding_id="foreign-binding", producer_plan_id="another-plan")
    state["task_state"]["plan_binding_history"].append(foreign)
    result = _apply_upstream_step_rollback(state, {
        "failed_step_id": "c", "rollback_to_step_id": "a", "invalidate_descendants": False,
    })
    assert result["affected_step_ids"] == ["a", "c"]
    assert accepted_plan_binding(state, "b_key") == unrelated
    assert state["task_state"]["plan_binding_history"][-1] == foreign
