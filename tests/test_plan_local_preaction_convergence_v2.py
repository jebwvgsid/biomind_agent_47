from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from agent_core.action_runtime import ActionRuntimeContractError
from agent_core.completion_gate import CompletionGate, DependencyEvidence, GateResult
from agent_core.managed_artifacts import (
    build_approved_visible_artifact_set,
    build_managed_artifact_bindings,
    managed_action_fingerprint,
    materialize_managed_action,
)
from agent_core.node_context import build_node_context
from agent_core.plan_bindings import PlanBindingError
from agent_core.plan_tracker import (
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    canonical_task_spec_for_state,
    current_plan_step_with_index,
    load_plan_state,
    record_provenance_event,
    refresh_state_contract_v2,
    save_plan_state,
)
from agent_core.recovery import (
    PreActionRepairDomain,
    RecoveryDecisionKind,
    RecoveryStateStore,
)
from agent_core.task_spec import create_task_spec
from agent_core.state_contract_store import StateContractStore


ARTIFACT_A = "plan-artifact-a"
ARTIFACT_B = "plan-artifact-b"


def _snapshot(root: str) -> dict:
    return {
        "plan_snapshot_id": "plan-snapshot-test",
        "plan_semantic_hash": "sha256:approved-plan",
        "steps": [
            {
                "step_id": "step-1",
                "title": "produce A",
                "action": "execute",
                "inputs": [],
                "outputs": [ARTIFACT_A],
                "dependencies": [],
                "success_criteria": ["A exists"],
            },
            {
                "step_id": "step-2",
                "title": "consume A",
                "action": "execute",
                "inputs": [ARTIFACT_A],
                "outputs": [],
                "dependencies": ["step-1"],
                "success_criteria": ["A consumed"],
            },
            {
                "step_id": "step-3",
                "title": "unrelated B",
                "action": "execute",
                "inputs": [],
                "outputs": [ARTIFACT_B],
                "dependencies": [],
                "success_criteria": ["B exists"],
            },
        ],
        "resolved_artifact_refs": [
            {
                "step_id": "step-1",
                "relation": "produce",
                "field": "outputs",
                "artifact_ref": {"kind": "authoritative", "identity": ARTIFACT_A},
            },
            {
                "step_id": "step-2",
                "relation": "consume",
                "field": "inputs",
                "artifact_ref": {"kind": "authoritative", "identity": ARTIFACT_A},
            },
            {
                "step_id": "step-3",
                "relation": "produce",
                "field": "outputs",
                "artifact_ref": {"kind": "authoritative", "identity": ARTIFACT_B},
            },
        ],
        "plan_local_artifacts": {
            ARTIFACT_A: {
                "logical_artifact_id": ARTIFACT_A,
                "canonical_path": str(Path(root) / ARTIFACT_A),
                "object_kind": "directory",
                "materialization_policy_id": "managed_intermediate_v1",
                "lifecycle": "plan_local",
                "semantic_view": {"role": "work_dir", "format": "directory"},
            },
            ARTIFACT_B: {
                "logical_artifact_id": ARTIFACT_B,
                "canonical_path": str(Path(root) / ARTIFACT_B),
                "object_kind": "file",
                "materialization_policy_id": "managed_intermediate_v1",
                "lifecycle": "plan_local",
                "semantic_view": {"role": "unrelated", "format": "text"},
            },
        },
    }


def _producer_state(valid: bool = False) -> dict:
    state = {
        "steps": [
            {"step_id": "step-1", "status": "done" if valid else "running"},
            {"step_id": "step-2", "status": "ready"},
            {"step_id": "step-3", "status": "pending"},
        ],
        "provenance_state": {"artifact_heads": {}, "artifacts": {}},
    }
    if valid:
        state["provenance_state"] = {
            "artifact_heads": {"canonical-a": "canonical-a@attempt-1"},
            "artifacts": {
                "canonical-a@attempt-1": {
                    "logical_artifact_id": ARTIFACT_A,
                    "status": "valid",
                    "validation_status": "valid",
                }
            },
        }
    return state


class ApprovedPlanLocalBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snapshot = _snapshot(self.temp.name)
        self.task = {"inputs": [], "output_contracts": []}

    def test_u1_producer_gets_writable_directory_output_binding(self) -> None:
        visible = build_approved_visible_artifact_set(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
        )
        bindings = build_managed_artifact_bindings(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
            visible_artifacts=visible,
        )

        binding = bindings[f"output.{ARTIFACT_A}"]
        self.assertEqual(set(visible), {ARTIFACT_A})
        self.assertEqual(binding.artifact_id, ARTIFACT_A)
        self.assertEqual(binding.direction, "output")
        self.assertEqual(binding.artifact_kind, "directory")
        self.assertEqual(binding.validation_contract_id, "directory_exists")
        self.assertEqual(binding.canonical_path, str(Path(self.temp.name) / ARTIFACT_A))

    def test_u2_consumer_reuses_identity_as_runtime_valid_input(self) -> None:
        state = _producer_state(valid=True)
        visible = build_approved_visible_artifact_set(
            self.task,
            plan_state=state,
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-2",
        )
        binding = build_managed_artifact_bindings(
            self.task,
            plan_state=state,
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-2",
            visible_artifacts=visible,
        )[f"input.{ARTIFACT_A}"]

        self.assertEqual(binding.artifact_id, ARTIFACT_A)
        self.assertEqual(binding.direction, "input")
        self.assertTrue(binding.runtime_available)

    def test_u3_unrelated_ledger_artifact_is_isolated(self) -> None:
        visible = build_approved_visible_artifact_set(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
        )
        self.assertNotIn(ARTIFACT_B, visible)

    def test_u4_true_unknown_and_u5_known_but_unauthorized_are_generate_owned(self) -> None:
        visible = build_approved_visible_artifact_set(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
        )
        bindings = build_managed_artifact_bindings(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
            visible_artifacts=visible,
        )
        for output_id in ("plan-artifact-not-real", ARTIFACT_B):
            with self.subTest(output_id=output_id):
                _, issues = materialize_managed_action(
                    {
                        "type": "execute",
                        "argv": ["mkdir", f"{{output:{output_id}}}"],
                        "produced_outputs": [{"output_id": output_id}],
                    },
                    bindings,
                    approved_visible_artifacts=visible,
                )
                self.assertEqual(issues[0].owner, "GENERATED_ACTION")

    def test_u6_visible_but_missing_projection_is_internal_protocol(self) -> None:
        visible = build_approved_visible_artifact_set(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
        )
        _, issues = materialize_managed_action(
            {
                "type": "execute",
                "argv": ["mkdir", f"{{output:{ARTIFACT_A}}}"],
                "produced_outputs": [{"output_id": ARTIFACT_A}],
            },
            {},
            approved_visible_artifacts=visible,
        )
        self.assertEqual(issues[0].code, "PLAN_ARTIFACT_BINDING_PROJECTION_MISSING")
        self.assertEqual(issues[0].owner, "INTERNAL_PROTOCOL")
        self.assertEqual(issues[0].subowner, "controller_projection")

    def test_u7_directory_kind_reaches_node_context_and_compatibility_projection(self) -> None:
        context = build_node_context(
            node_type="generate",
            task_spec=self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
        )
        output_row = context.llm_payload.to_dict()["task"]["outputs"][0]
        self.assertEqual(output_row["artifact_kind"], "directory")

        plan = build_plan_state("projection", "- [>] Step 1: run | Action: EXECUTE | Done: ok")
        plan["plan_artifacts"] = self.snapshot["plan_local_artifacts"]
        refresh_state_contract_v2(plan)
        projected = plan["current_plan"]["plan_spec"]
        self.assertIn("plan_artifacts", projected)
        self.assertNotIn("plan_local_artifacts", projected)
        self.assertEqual(projected["plan_artifacts"][ARTIFACT_A]["object_kind"], "directory")

    def test_u8_atomic_placeholder_remains_strict(self) -> None:
        visible = build_approved_visible_artifact_set(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
        )
        bindings = build_managed_artifact_bindings(
            self.task,
            plan_state=_producer_state(),
            approved_plan_snapshot=self.snapshot,
            current_step_id="step-1",
            visible_artifacts=visible,
        )
        _, embedded = materialize_managed_action(
            {"argv": [f"{{output:{ARTIFACT_A}}}/foo"]},
            bindings,
            approved_visible_artifacts=visible,
        )
        materialized, valid = materialize_managed_action(
            {"argv": ["mkdir", "$WORK_DIR"], "env": {"WORK_DIR": f"{{output:{ARTIFACT_A}}}"}},
            bindings,
            approved_visible_artifacts=visible,
        )
        self.assertEqual(embedded[0].code, "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED")
        self.assertEqual(valid, [])
        self.assertEqual(materialized["env"]["WORK_DIR"], str(Path(self.temp.name) / ARTIFACT_A))


class PreActionConvergenceTests(unittest.TestCase):

    def _violation(self) -> list[dict]:
        return [{
            "code": "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
            "owner": "GENERATED_ACTION",
            "route": "generate",
            "message": "placeholder must be atomic",
        }]






def workspace_execution_response(messages):
    """External model leaf: explicitly hand user execution intent to the workflow."""
    request = next(str(m.content) for m in reversed(messages) if m.type == "human")
    return AIMessage(content="", tool_calls=[{"name": "request_execution", "id": "offline-managed-handoff",
        "args": {"scope": "managed", "request": request, "authorization_quote": request}}])


class _GraphRouterLLM:
    def bind_tools(self, tools, **kwargs):
        return self

    def bind(self, **_kwargs: Any) -> "_GraphRouterLLM":
        return self

    def model_copy(self, update: Any = None) -> "_GraphRouterLLM":
        del update
        return self

    async def ainvoke(self, _messages: Any, **_kwargs: Any) -> AIMessage:
        if any("You are BioMind, a bioinformatics agent working" in str(m.content) for m in _messages):
            return workspace_execution_response(_messages)
        return AIMessage(content=json.dumps({
            "schema_version": 1,
            "request_kind": "execute",
            "execution_scope": "managed",
            "managed_intent_explicit": True,
            "retrieval_mode": "kb",
            "confidence": 1.0,
            "reason": "plan-local graph integration fixture",
        }))




if __name__ == "__main__":
    unittest.main()
