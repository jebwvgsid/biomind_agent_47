from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

import bio_agents
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
    refresh_state_contract_v2,
    save_plan_state,
)
from agent_core.recovery import (
    PreActionRepairDomain,
    RecoveryDecisionKind,
    RecoveryStateStore,
)
from agent_core.task_spec import create_task_spec
from bio_agents import BioAnalystAgent


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
    def _durable_agent(self, root: str) -> BioAnalystAgent:
        task = create_task_spec("run one managed action", task_id="preaction")
        plan = build_plan_state(
            "preaction",
            "- [>] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
        )
        bind_plan_to_task_revision(plan, task["revision"])
        plan["current_task"] = {"task_spec": task}
        approve_plan_state(plan, task["revision"])
        save_plan_state(root, "preaction", plan)
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = root
        return agent

    def _violation(self) -> list[dict]:
        return [{
            "code": "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
            "owner": "GENERATED_ACTION",
            "route": "generate",
            "message": "placeholder must be atomic",
        }]

    def _finalize(self, agent, context, decision):
        finalized, envelope = agent._finalize_controller_recovery_decision(
            "preaction",
            decision,
            source_node="generate",
            source_record={
                "step_id": context.step_id,
                "action_id": context.action_id,
                "attempt_id": context.attempt_id,
                "failure_fingerprint": context.error_fingerprint,
            },
            recovery_context_fingerprint=context.error_fingerprint,
        )
        context.recovery_decision = finalized.to_dict()
        return finalized, envelope

    def test_n1_first_semantic_rejection_commits_corrective_generate_budget(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent = self._durable_agent(root)
            plan = load_plan_state(root, "preaction")
            candidate = {"type": "execute", "argv": ["{output:a}/foo"]}
            fingerprint = managed_action_fingerprint(candidate)
            context, decision = agent._managed_preaction_recovery(
                task_id="preaction",
                plan_state=plan,
                approved_plan_snapshot={"plan_snapshot_id": "snapshot", "steps": []},
                step_id="step-1",
                candidate=candidate,
                candidate_fingerprint=fingerprint,
                previous_candidate_fingerprint="",
                repair_domain=PreActionRepairDomain.GENERATED_SEMANTIC.value,
                violations=self._violation(),
                visible_artifacts={},
            )
            decision, envelope = self._finalize(agent, context, decision)
            guarded = agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": "generate",
                    "pending_recovery_action": {"type": "same_step_repair"},
                    "recovery_context": context.to_dict(),
                    "recovery_decision": decision.to_dict(),
                    "recovery_execution_envelope": envelope,
                },
                "preaction",
            )
            budgets = load_plan_state(root, "preaction")["task_state"]["budgets"]

        self.assertEqual(decision.kind, RecoveryDecisionKind.RETRY_CURRENT_ACTION.value)
        self.assertEqual(guarded["next_step"], "generate")
        self.assertEqual(int(budgets.get("action_format_repair_count") or 0), 0)
        self.assertEqual(budgets["generate_attempts_by_step"]["step-1"], 1)
        self.assertEqual(budgets["invalid_action_hashes_by_step"]["step-1"][fingerprint], 1)

    def test_n2_equivalent_second_rejection_converges_without_increment(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent = self._durable_agent(root)
            candidate = {"type": "execute", "argv": ["{output:a}/foo"]}
            fingerprint = managed_action_fingerprint(candidate)
            first_context, first_decision = agent._managed_preaction_recovery(
                task_id="preaction",
                plan_state=load_plan_state(root, "preaction"),
                approved_plan_snapshot={"plan_snapshot_id": "snapshot", "steps": []},
                step_id="step-1",
                candidate=candidate,
                candidate_fingerprint=fingerprint,
                previous_candidate_fingerprint="",
                repair_domain=PreActionRepairDomain.GENERATED_SEMANTIC.value,
                violations=self._violation(),
                visible_artifacts={},
            )
            first_decision, first_envelope = self._finalize(
                agent, first_context, first_decision
            )
            agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": "generate",
                    "pending_recovery_action": {"type": "same_step_repair"},
                    "recovery_context": first_context.to_dict(),
                    "recovery_decision": first_decision.to_dict(),
                    "recovery_execution_envelope": first_envelope,
                },
                "preaction",
            )
            second_context, second_decision = agent._managed_preaction_recovery(
                task_id="preaction",
                plan_state=load_plan_state(root, "preaction"),
                approved_plan_snapshot={"plan_snapshot_id": "snapshot", "steps": []},
                step_id="step-1",
                candidate=candidate,
                candidate_fingerprint=fingerprint,
                previous_candidate_fingerprint=fingerprint,
                repair_domain=PreActionRepairDomain.GENERATED_SEMANTIC.value,
                violations=self._violation(),
                visible_artifacts={},
            )
            second_decision, second_envelope = self._finalize(
                agent, second_context, second_decision
            )
            guarded = agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": second_decision.next_route,
                    "pending_recovery_action": {"type": "internal_protocol_error"},
                    "recovery_context": second_context.to_dict(),
                    "recovery_decision": second_decision.to_dict(),
                    "recovery_execution_envelope": second_envelope,
                },
                "preaction",
            )
            budgets = load_plan_state(root, "preaction")["task_state"]["budgets"]

        self.assertEqual(second_decision.kind, RecoveryDecisionKind.ESCALATE.value)
        self.assertEqual(guarded["next_step"], "supervisor")
        self.assertEqual(int(budgets.get("action_format_repair_count") or 0), 0)
        self.assertEqual(budgets["generate_attempts_by_step"]["step-1"], 1)

    def test_n4_cached_source_still_commits_and_n5_invalid_cache_is_not_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent = self._durable_agent(root)
            candidate = {"type": "execute", "argv": ["{output:a}/foo"]}
            fingerprint = managed_action_fingerprint(candidate)
            identity = {
                "approved_plan_snapshot_id": "snapshot",
                "task_semantic_hash": "task-hash",
                "step_id": "step-1",
                "failure_stage": "pre_action_contract",
                "error_fingerprint": "error",
                "candidate_fingerprint": "prior",
                "repair_ordinal": 1,
            }
            store = RecoveryStateStore(root, "preaction")
            store.save_llm_result(
                "context",
                "generate:FORMAT_REPAIR",
                raw_response="cached",
                cache_identity=identity,
                candidate_fingerprint=fingerprint,
            )
            self.assertTrue(store.load_llm_result(
                "context",
                "generate:FORMAT_REPAIR",
                cache_identity=identity,
            ))
            self.assertEqual(store.load_llm_result(
                "context",
                "generate:FORMAT_REPAIR",
                cache_identity=identity,
                invalid_candidate_fingerprints=[fingerprint],
            ), {})

            plan = load_plan_state(root, "preaction")
            context, decision = agent._managed_preaction_recovery(
                task_id="preaction",
                plan_state=plan,
                approved_plan_snapshot={"plan_snapshot_id": "snapshot", "steps": []},
                step_id="step-1",
                candidate=candidate,
                candidate_fingerprint=fingerprint,
                previous_candidate_fingerprint="",
                repair_domain=PreActionRepairDomain.GENERATED_SEMANTIC.value,
                violations=self._violation(),
                visible_artifacts={},
            )
            decision, envelope = self._finalize(agent, context, decision)
            agent._apply_transition_guard(
                "generate",
                {"generate_response_cached": True},
                {
                    "next_step": "generate",
                    "pending_recovery_action": {"type": "same_step_repair"},
                    "recovery_context": context.to_dict(),
                    "recovery_decision": decision.to_dict(),
                    "recovery_execution_envelope": envelope,
                },
                "preaction",
            )
            budgets = load_plan_state(root, "preaction")["task_state"]["budgets"]
        self.assertEqual(int(budgets.get("action_format_repair_count") or 0), 0)
        self.assertEqual(budgets["generate_attempts_by_step"]["step-1"], 1)


class _GraphRouterLLM:
    def bind(self, **_kwargs: Any) -> "_GraphRouterLLM":
        return self

    def model_copy(self, update: Any = None) -> "_GraphRouterLLM":
        del update
        return self

    async def ainvoke(self, _messages: Any, **_kwargs: Any) -> AIMessage:
        return AIMessage(content=json.dumps({
            "schema_version": 1,
            "request_kind": "execute",
            "execution_scope": "managed",
            "managed_intent_explicit": True,
            "retrieval_mode": "kb",
            "confidence": 1.0,
            "reason": "plan-local graph integration fixture",
        }))


class PlanLocalRealGraphTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the production graph wrappers with only external I/O stubbed."""

    SKILL_ID = "sequence/search/local-blast"

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="biomind-plan-local-graph-")
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(bio_agents, "init_chat_model", return_value=_GraphRouterLLM()),
            patch.object(bio_agents, "BIOMIND_MEMORY_DIR", str(self.root / "memory")),
            patch.object(bio_agents, "BIOMIND_WIKI_ENABLED", False),
            patch.object(bio_agents, "ENABLE_ROOT_CAUSE_ANALYZER", False),
        ]
        for item in self.patches:
            item.start()
        self.agent = BioAnalystAgent(
            data_path=str(self.root),
            enable_persistence=False,
            use_llm_supervisor=False,
        )

        async def no_observe(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def no_compress(messages: Any, _task_id: str):
            return messages, False

        self.agent._observe_session_memory = no_observe
        self.agent._finalize_task_memory = no_observe
        self.agent.memory_manager.maybe_compress = no_compress
        self.agent._consultant_runtime_seed_evidence = (
            lambda *_args, **_kwargs: ([], {}, "")
        )
        self.agent._consultant_runtime_preflight = (
            lambda *_args, **_kwargs: ([], "")
        )
        self.agent._prompt_user = lambda *_args, **_kwargs: "y"
        self.agent._ensure_skill_runtime_ready = lambda *_args, **_kwargs: (True, "")
        self.agent._selected_skill_env_runner_violation = lambda *_args, **_kwargs: ""
        self.agent._workflow_future_skill_violation = lambda *_args, **_kwargs: ""
        self.agent.middleware.review_code = (
            lambda code, _language, **_kwargs: ("approve", code)
        )

    async def asyncTearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None
        self.temp.cleanup()

    def _runner_state(self, task_id: str) -> dict[str, Any]:
        prompt = "Create an approved plan-local work directory, consume it, and write the report."
        durable, _, _ = self.agent._persist_user_submission(task_id, prompt)
        admission = {
            "schema_version": 1,
            "request_kind": "execute",
            "execution_scope": "managed",
            "managed_intent_explicit": True,
            "task_mode": "managed",
            "retrieval_mode": "kb",
            "confidence": 1.0,
            "admission_status": "managed_execution_admitted",
            "reason": "test fixture explicit managed execution",
            "decision_source": "test_fixture",
        }
        task_state = dict(durable.get("task_state") or {})
        task_state["intent_admission"] = admission
        durable["task_state"] = task_state
        save_plan_state(self.root, task_id, durable)
        return {
            "messages": [HumanMessage(content=prompt)],
            "task_id": task_id,
            "session_id": task_id,
            "project_id": self.agent.default_project_id,
            "conversation_evidence": dict(durable.get("conversation_evidence") or {}),
            "task_spec": canonical_task_spec_for_state(durable),
            "task_revision": 0,
            "selected_skills": [],
            "resolved_bindings": {},
            "consultation_state": {},
            "planner_feedback": {},
            "request_intent": admission,
            "intent_admission": admission,
            "managed_intent_admitted": True,
        }

    def _consultant_payload(self, report_path: Path) -> dict[str, Any]:
        return {
            "action_type": "finish",
            "summary": "The managed task contract is complete.",
            "selected_skills": [self.SKILL_ID],
            "task_spec_operations": [
                {
                    "operation": "set_goal",
                    "value": "Create and consume a typed plan-local directory, then write a report",
                    "supporting_evidence": ["U1"],
                },
                {
                    "operation": "upsert_output_contract",
                    "output_contract": {
                        "output_id": "report",
                        "role": "report",
                        "artifact_kind": "file",
                        "path": str(report_path),
                        "required": True,
                    },
                    "supporting_evidence": ["U1"],
                },
            ],
            "unresolved_ambiguities": [],
            "planner_feedback": None,
        }

    def _planner_payload(self) -> dict[str, Any]:
        return {
            "outcome_type": "PLAN_CANDIDATE",
            "proposed_artifacts": [{
                "local_ref": "plan.work_dir",
                "semantic_view": {
                    "role": "work_dir",
                    "format": "directory",
                    "description": "typed plan-local working directory",
                },
                "artifact_contract": {
                    "object_kind": "directory",
                    "lifecycle": "plan_local",
                    "materialization_policy_id": "managed_intermediate_v1",
                },
            }],
            "steps": [
                {
                    "step_id": "step-1",
                    "title": "Create the plan-local directory",
                    "action": "execute",
                    "purpose": "analysis",
                    "tool": "mkdir",
                    "inputs": [],
                    "outputs": ["plan.work_dir"],
                    "dependencies": [],
                    "success_criteria": ["the declared directory exists"],
                    "skill_ids": [self.SKILL_ID],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                },
                {
                    "step_id": "step-2",
                    "title": "Consume the directory and write the report",
                    "action": "execute",
                    "purpose": "analysis",
                    "tool": "test",
                    "inputs": ["plan.work_dir"],
                    "outputs": ["output.report"],
                    "dependencies": ["step-1"],
                    "success_criteria": ["the report exists and is non-empty"],
                    "skill_ids": [self.SKILL_ID],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                },
            ],
            "validation_checklist": [{
                "check_id": "check-plan-local-flow",
                "description": "verify the directory and final report",
            }],
            "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"},
        }

    @staticmethod
    def _artifact_row(payload: Mapping[str, Any], direction: str) -> dict[str, Any]:
        field = "outputs" if direction == "output" else "inputs"
        for row in ((payload.get("task") or {}).get(field) or []):
            if str(row.get("handle") or "").startswith(f"{direction}.plan-artifact-"):
                return dict(row)
        raise AssertionError(f"no plan-local {direction} row: {payload}")

    @staticmethod
    def _execute_action(
        payload: Mapping[str, Any],
        *,
        argv_prefix: list[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        output_rows = list(((payload.get("task") or {}).get("outputs") or []))
        placeholders = [str(row.get("placeholder") or "") for row in output_rows]
        input_rows = list(((payload.get("task") or {}).get("inputs") or []))
        input_placeholders = [str(row.get("placeholder") or "") for row in input_rows]
        return {
            "action_type": "execute",
            "action_kind": "primary",
            "purpose": "analysis",
            "argv": list(argv_prefix or ["/bin/true"]) + input_placeholders + placeholders,
            "env": dict(env or {}),
            "outputs": [str(row.get("handle") or "") for row in output_rows],
            "produced_outputs": [
                {
                    "output_id": str(row.get("handle") or "").partition(".")[2],
                    "path": str(row.get("placeholder") or ""),
                }
                for row in output_rows
            ],
            "expected_evidence": [
                "artifact." + str(row.get("handle") or "") for row in output_rows
            ],
        }

    @staticmethod
    def _script_action(
        payload: Mapping[str, Any],
        *,
        extra_lines: list[str] | None = None,
    ) -> str:
        """Produce the current script_block_v1 ABI while using every binding."""
        runtime = dict((payload.get("current_focus") or {}).get("runtime") or {})
        environment = [
            dict(item)
            for item in runtime.get("artifact_environment", []) or []
            if isinstance(item, Mapping)
        ]
        lines = list(extra_lines or [])
        for row in environment:
            env_name = str(row.get("env_var") or "")
            handle = str(row.get("semantic_handle") or "")
            reference = f'"${env_name}"'
            if handle.startswith("input."):
                lines.append(f"test -e {reference}")
            elif str(row.get("artifact_kind") or "") == "directory":
                lines.extend([
                    f"mkdir -p {reference}",
                    f': > "${env_name}/.prepared"',
                ])
            else:
                lines.extend([
                    f'mkdir -p "$(dirname "${env_name}")"',
                    f'printf "prepared\\n" > "${env_name}"',
                ])
        lines.append("true")
        return "<shell>\n" + "\n".join(lines) + "\n</shell>"

    async def _run_graph(
        self,
        task_id: str,
        generate_action: Any,
        *,
        binding_patch: Any = None,
        command_calls: list[str] | None = None,
        command_results: list[tuple[str, str, int]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path]:
        report_path = self.root / task_id / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        runner = self._runner_state(task_id)
        captures: list[dict[str, Any]] = []

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            if stage == "consultant":
                return AIMessage(content=json.dumps(self._consultant_payload(report_path)))
            if stage in {"planner", "planner_structure_repair", "planner_repair"}:
                return AIMessage(content=json.dumps(self._planner_payload()))
            if stage == "generate":
                payload = node_context.llm_payload.to_dict()
                payload["_provider_request"] = "\n".join(
                    str(getattr(message, "content", "") or "")
                    for message in _messages
                )
                captures.append(payload)
                generated = generate_action(payload, len(captures))
                return AIMessage(content=(
                    generated if isinstance(generated, str) else json.dumps(generated)
                ))
            raise AssertionError(f"unexpected stage: {stage}")

        async def fake_run_script(
            _script_path: str,
            *,
            expected_sha256: str = "",
            interpreter: str = "bash",
            lifecycle_callback: Any = None,
            env: Mapping[str, str] | None = None,
        ):
            del expected_sha256, interpreter, env
            if command_calls is not None:
                command_calls.append(_script_path)
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
            result = (
                command_results.pop(0)
                if command_results
                else ("stubbed external tool succeeded\n", "", 0)
            )
            durable = load_plan_state(self.root, task_id)
            _, step = current_plan_step_with_index(durable)
            if result[2] == 0:
                if str((step or {}).get("step_id") or "") == "step-1":
                    ledger = dict(durable.get("plan_artifacts") or {})
                    self.assertEqual(len(ledger), 1)
                    Path(next(iter(ledger.values()))["canonical_path"]).mkdir(parents=True, exist_ok=True)
                else:
                    report_path.write_text("plan-local directory consumed\n", encoding="utf-8")
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_EXITED", {"returncode": result[2]})
            return result

        self.agent._ainvoke_node_llm = scripted
        events: list[dict[str, Any]] = []
        patches = [patch.object(bio_agents, "run_script_path", new=fake_run_script)]
        if binding_patch is not None:
            patches.append(patch.object(
                bio_agents,
                "build_managed_artifact_bindings",
                new=binding_patch,
            ))
        for item in patches:
            item.start()
        try:
            async def consume() -> None:
                stream = self.agent.app.astream(
                    runner,
                    config={
                        "configurable": {"thread_id": task_id},
                        "recursion_limit": 40,
                    },
                    stream_mode="updates",
                )
                try:
                    async for event in stream:
                        events.append(event)
                finally:
                    await stream.aclose()

            await asyncio.wait_for(consume(), timeout=40)
        finally:
            for item in reversed(patches):
                item.stop()
        return events, captures, report_path

    def _configure_one_step_no_output_plan(self, *, execution_capability_id: str | None = None) -> None:
        original_planner = self._planner_payload
        original_consultant = self._consultant_payload
        capability_registry = self.agent.skill_manager.get_execution_capability_registry(
            [self.SKILL_ID]
        )
        fixture_capabilities = [key for key, contract in capability_registry.items()
                                if contract.get("selected_skill_id") == self.SKILL_ID]
        if execution_capability_id is not None:
            self.assertIn(execution_capability_id, fixture_capabilities)
            fixture_capabilities = [execution_capability_id]
        self.assertEqual(len(fixture_capabilities), 1)
        capability_id = fixture_capabilities[0]

        def one_step_plan() -> dict[str, Any]:
            payload = original_planner()
            payload["proposed_artifacts"] = []
            payload["steps"] = [{
                "step_id": "step-direct",
                "title": "Run one approved command",
                "action": "execute",
                "purpose": "analysis",
                "tool": "test",
                "inputs": [],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["the approved command exits"],
                "skill_ids": [self.SKILL_ID],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
                "execution_capability_id": capability_id,
            }]
            return payload

        def no_output_consultant(_report_path: Path) -> dict[str, Any]:
            payload = original_consultant(_report_path)
            payload["task_spec_operations"] = [
                payload["task_spec_operations"][0]
            ]
            return payload

        self._planner_payload = one_step_plan
        self._consultant_payload = no_output_consultant

    async def test_execute_initializes_one_authoritative_attempt_state_before_binding_resolution(
        self,
    ) -> None:
        task_id = "execute-attempt-state-ordering"
        command_calls: list[str] = []
        initialization_calls: list[dict[str, Any]] = []
        resolver_calls: list[dict[str, Any]] = []
        original_initialize = self.agent._initialize_execute_attempt_state
        original_resolver = bio_agents.resolve_action_binding_environment

        def initialize_spy(observed_task_id: str) -> dict[str, Any]:
            initialized = original_initialize(observed_task_id)
            initialization_calls.append(initialized)
            return initialized

        def resolver_spy(
            plan_state: Mapping[str, Any],
            action: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.assertIs(plan_state, initialization_calls[-1])
            self.assertEqual(plan_state["plan_id"], action["plan_id"])
            self.assertEqual(plan_state["plan_version"], action["plan_version"])
            self.assertEqual(plan_state["current_step_id"], action["step_id"])
            resolver_calls.append(dict(action))
            return original_resolver(plan_state, action)

        self._configure_one_step_no_output_plan()
        with patch.object(
            self.agent,
            "_initialize_execute_attempt_state",
            side_effect=initialize_spy,
        ), patch.object(
            bio_agents,
            "resolve_action_binding_environment",
            side_effect=resolver_spy,
        ):
            events, captures, _report = await self._run_graph(
                task_id,
                lambda payload, _ordinal: self._script_action(payload),
                command_calls=command_calls,
            )

        self.assertEqual(len(initialization_calls), 1)
        self.assertEqual(len(resolver_calls), 1)
        self.assertEqual(len(command_calls), 1)
        self.assertNotIn("UnboundLocalError", json.dumps(events, default=str))
        self.assertEqual(len(captures), 1)

    async def test_unexpected_execute_preparation_exception_is_terminal_not_recovery(
        self,
    ) -> None:
        task_id = "execute-preparation-internal-error"
        command_calls: list[str] = []
        self._configure_one_step_no_output_plan()

        with patch.object(
            bio_agents,
            "resolve_action_binding_environment",
            side_effect=RuntimeError("synthetic internal failure secret=not-durable"),
        ):
            events, captures, _report = await self._run_graph(
                task_id,
                lambda payload, _ordinal: self._script_action(payload),
                command_calls=command_calls,
            )

        durable = load_plan_state(self.root, task_id)
        failure = dict(durable.get("terminal_failure") or durable.get("failure") or {})
        records = RecoveryStateStore(self.root, task_id).load_execution_records()
        self.agent.profiler.finish_task(task_id, "blocked")
        metrics = self.agent.profiler.snapshot(task_id)

        self.assertEqual(len(captures), 1)
        self.assertEqual(command_calls, [])
        self.assertEqual(records, [])
        self.assertEqual(
            int(
                ((durable.get("task_state") or {}).get("budgets") or {}).get(
                    "recovery_judge_count"
                )
                or 0
            ),
            0,
        )
        self.assertEqual(failure["error_code"], "EXECUTE_PREPARATION_INTERNAL_ERROR")
        self.assertEqual(failure["owner"], "CONTROLLER")
        self.assertEqual(failure["node"], "execute")
        self.assertEqual(failure["phase"], "binding_resolution")
        self.assertEqual(failure["exception_type"], "RuntimeError")
        self.assertIs(failure["command_started"], False)
        self.assertIs(failure["execution_started"], False)
        self.assertEqual(failure["step_id"], "step-direct")
        self.assertTrue(failure["action_id"])
        self.assertNotIn("secret=not-durable", json.dumps(durable, default=str))
        self.assertEqual(metrics["summary"]["commands"], 0)
        self.assertEqual(metrics["summary"]["command_execution_seconds"], 0)
        self.assertEqual(
            metrics["failure"]["error_code"],
            "EXECUTE_PREPARATION_INTERNAL_ERROR",
        )
        self.assertEqual(metrics["failure"]["owner"], "CONTROLLER")
        self.assertNotIn("TASK_FAILED", json.dumps(events, default=str))

    async def test_graph_interrupt_pauses_without_harness_failure_and_resumes_once(
        self,
    ) -> None:
        task_id = "execute-approval-graph-interrupt"
        command_calls: list[str] = []
        captures: list[dict[str, Any]] = []
        report_path = self.root / f"{task_id}.txt"
        runner = self._runner_state(task_id)
        self._configure_one_step_no_output_plan()

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            if stage == "consultant":
                return AIMessage(content=json.dumps(self._consultant_payload(report_path)))
            if stage in {"planner", "planner_structure_repair", "planner_repair"}:
                return AIMessage(content=json.dumps(self._planner_payload()))
            if stage == "generate":
                payload = node_context.llm_payload.to_dict()
                captures.append(payload)
                return AIMessage(content=self._script_action(payload))
            raise AssertionError(f"unexpected stage: {stage}")

        async def fake_run_script(
            script_path: str,
            *,
            expected_sha256: str = "",
            interpreter: str = "bash",
            lifecycle_callback: Any = None,
            env: Mapping[str, str] | None = None,
        ):
            del expected_sha256, interpreter, env
            command_calls.append(script_path)
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
                await lifecycle_callback("PROCESS_EXITED", {"returncode": 0})
            return "approved command ran once\n", "", 0

        self.agent._ainvoke_node_llm = scripted
        # Use the production HITL implementation and force even a safe fixture
        # through LangGraph interrupt/resume.
        del self.agent.middleware.review_code
        self.agent.middleware.auto_approve_safe = False
        config = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": 40,
        }
        paused_events: list[dict[str, Any]] = []
        resumed_events: list[dict[str, Any]] = []

        with patch.object(bio_agents, "run_script_path", new=fake_run_script):
            async for event in self.agent.app.astream(
                runner,
                config=config,
                stream_mode="updates",
            ):
                paused_events.append(event)

            paused_snapshot = self.agent.app.get_state(config)
            paused_durable = load_plan_state(self.root, task_id)
            self.assertTrue(paused_snapshot.tasks[0].interrupts)
            self.assertEqual(command_calls, [])
            self.assertEqual(
                RecoveryStateStore(self.root, task_id).load_execution_records(),
                [],
            )
            self.assertNotEqual(
                str((paused_durable.get("failure") or {}).get("error_code") or ""),
                "EXECUTE_PREPARATION_INTERNAL_ERROR",
            )

            async for event in self.agent.app.astream(
                bio_agents.Command(resume="y"),
                config=config,
                stream_mode="updates",
            ):
                resumed_events.append(event)

        self.assertEqual(len(captures), 1)
        self.assertEqual(len(command_calls), 1)
        self.assertTrue(any("__interrupt__" in event for event in paused_events))
        self.assertNotIn(
            "EXECUTE_PREPARATION_INTERNAL_ERROR",
            json.dumps([paused_events, resumed_events], default=str),
        )

    async def test_format_repair_reentry_reaches_initialized_execute_once(self) -> None:
        task_id = "execute-format-repair-reentry"
        command_calls: list[str] = []
        initialization_calls: list[dict[str, Any]] = []
        resolver_calls: list[dict[str, Any]] = []
        original_initialize = self.agent._initialize_execute_attempt_state
        original_resolver = bio_agents.resolve_action_binding_environment
        self._configure_one_step_no_output_plan()

        def candidate(payload: Mapping[str, Any], ordinal: int) -> str:
            if ordinal == 1:
                return "\n".join([
                    "```json",
                    "{",
                    '  "action_type": "execute",',
                    '  "action_kind": "primary",',
                    '  "purpose": "analysis",',
                    '  "argv": [',
                    '    "bash",',
                    '    "-lc",',
                    '    "true",',
                    '    "env": {"OUT": "/tmp/not-a-binding"}',
                    "  ]",
                    "}",
                    "```",
                ])
            return self._script_action(payload)

        def initialize_spy(observed_task_id: str) -> dict[str, Any]:
            initialized = original_initialize(observed_task_id)
            initialization_calls.append(initialized)
            return initialized

        def resolver_spy(
            plan_state: Mapping[str, Any],
            action: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.assertIs(plan_state, initialization_calls[-1])
            self.assertEqual(plan_state["current_step_id"], action["step_id"])
            resolver_calls.append(dict(action))
            return original_resolver(plan_state, action)

        with patch.object(
            self.agent,
            "_initialize_execute_attempt_state",
            side_effect=initialize_spy,
        ), patch.object(
            bio_agents,
            "resolve_action_binding_environment",
            side_effect=resolver_spy,
        ):
            events, captures, _report = await self._run_graph(
                task_id,
                candidate,
                command_calls=command_calls,
            )

        serialized = json.dumps(events, default=str)
        self.assertEqual(len(captures), 2)
        self.assertEqual(len(initialization_calls), 1)
        self.assertEqual(len(resolver_calls), 1)
        self.assertEqual(len(command_calls), 1)
        self.assertIn("GENERATE_SCRIPT_BLOCK_INVALID", serialized)
        self.assertNotIn("UnboundLocalError", serialized)
        self.assertNotIn("EXECUTE_PREPARATION_INTERNAL_ERROR", serialized)

    async def test_plan_binding_error_retains_controlled_corrective_generate_path(
        self,
    ) -> None:
        task_id = "execute-controlled-plan-binding-error"
        command_calls: list[str] = []
        resolver_calls: list[str] = []
        original_resolver = bio_agents.resolve_action_binding_environment
        self._configure_one_step_no_output_plan()

        def resolver(
            plan_state: Mapping[str, Any],
            action: Mapping[str, Any],
        ) -> dict[str, Any]:
            resolver_calls.append(str(action.get("action_id") or ""))
            if len(resolver_calls) == 1:
                raise PlanBindingError(
                    "PLAN_BINDING_REFERENCE_MISSING",
                    binding_name="fixture.binding",
                )
            return original_resolver(plan_state, action)

        with patch.object(
            bio_agents,
            "resolve_action_binding_environment",
            side_effect=resolver,
        ):
            events, captures, _report = await self._run_graph(
                task_id,
                lambda payload, _ordinal: self._script_action(payload),
                command_calls=command_calls,
            )

        serialized = json.dumps(events, default=str)
        self.assertEqual(len(resolver_calls), 2)
        self.assertEqual(len(captures), 2)
        self.assertEqual(len(command_calls), 1)
        self.assertIn("BINDING_REFERENCE_MISMATCH", serialized)
        self.assertIn("CORRECTIVE_GENERATE", serialized)
        self.assertNotIn("EXECUTE_PREPARATION_INTERNAL_ERROR", serialized)

    async def test_started_nonzero_command_keeps_execution_record_and_recovery_path(
        self,
    ) -> None:
        task_id = "execute-real-command-failure"
        command_calls: list[str] = []
        command_results = [
            ("", "fixture tool exited nonzero", 7),
            ("stubbed retry succeeded\n", "", 0),
        ]
        self._configure_one_step_no_output_plan()

        events, captures, _report = await self._run_graph(
            task_id,
            lambda payload, _ordinal: self._script_action(payload),
            command_calls=command_calls,
            command_results=command_results,
        )

        records = RecoveryStateStore(self.root, task_id).load_execution_records()
        failed_records = [
            record for record in records
            if int(record.get("returncode") or record.get("exit_code") or 0) == 7
        ]
        execute_updates = [
            dict(event.get("execute") or {})
            for event in events
            if isinstance(event, Mapping) and isinstance(event.get("execute"), Mapping)
        ]
        recovery_eligible = any(
            bool((update.get("recovery_trigger") or {}).get("required"))
            for update in execute_updates
        )

        self.assertGreaterEqual(len(captures), 1)
        self.assertGreaterEqual(len(command_calls), 1)
        self.assertTrue(failed_records, json.dumps(records, default=str))
        self.assertTrue(recovery_eligible, json.dumps(execute_updates, default=str))
        self.assertNotIn(
            "EXECUTE_PREPARATION_INTERNAL_ERROR",
            json.dumps(events, default=str),
        )

    async def test_graph_a_plan_local_directory_happy_path(self) -> None:
        events, captures, report = await self._run_graph(
            "graph-a-plan-local",
            lambda payload, _ordinal: self._script_action(payload),
        )
        visited = [next(iter(event)) for event in events]
        producer = self._artifact_row(captures[0], "output")
        consumer = self._artifact_row(captures[1], "input")
        producer_id = producer["placeholder"].removeprefix("{output:").removesuffix("}")
        consumer_id = consumer["placeholder"].removeprefix("{input:").removesuffix("}")

        self.assertEqual(producer_id, consumer_id)
        self.assertEqual(producer["artifact_kind"], "directory")
        self.assertEqual(consumer["availability"], "runtime_valid")
        durable = load_plan_state(self.root, "graph-a-plan-local")
        self.assertEqual(
            visited.count("execute"),
            2,
            json.dumps({
                "steps": durable.get("steps"),
                "plan_artifacts": durable.get("plan_artifacts"),
                "artifact_heads": (durable.get("provenance_state") or {}).get("artifact_heads"),
                "artifacts": (durable.get("provenance_state") or {}).get("artifacts"),
            }, ensure_ascii=False, default=str),
        )
        self.assertNotIn("renderer", visited)
        self.assertTrue(report.is_file())
        self.assertNotIn("GENERATE_OUTPUT_ID_UNKNOWN", json.dumps(events, default=str))

    async def test_graph_b_raw_placeholder_and_audit_findings_are_diagnostic(self) -> None:
        def candidate(payload: Mapping[str, Any], _ordinal: int) -> str:
            placeholder = str(
                (((payload.get("task") or {}).get("outputs") or [{}])[0]).get("placeholder")
                or "{output:example}"
            )
            return self._script_action(
                payload,
                extra_lines=[f'echo "example {placeholder}/child" >/dev/null'],
            )

        events, captures, _ = await self._run_graph("graph-b-repair", candidate)
        durable = load_plan_state(self.root, "graph-b-repair")
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        visited = [next(iter(event)) for event in events]

        self.assertEqual(len(captures), 2)
        self.assertEqual(int(budgets.get("action_format_repair_count") or 0), 0)
        self.assertEqual(dict(budgets.get("generate_attempts_by_step") or {}), {})
        self.assertEqual(visited.count("execute"), 2)
        self.assertNotIn("renderer", visited)
        serialized = json.dumps(events, default=str)
        self.assertIn("MANAGED_PLACEHOLDER_IN_FREE_SOURCE", serialized)
        self.assertIn("OUTPUT_EVIDENCE_SUPPRESSION", serialized)
        self.assertNotIn("<action_repair>", str(captures[1].get("_provider_request") or ""))
        self.assertEqual(str((durable.get("task_state") or {}).get("status") or ""), "completed")

    async def test_graph_e_malformed_script_block_repair_is_visible_to_provider(self) -> None:
        def candidate(payload: Mapping[str, Any], ordinal: int) -> str:
            if ordinal == 1:
                placeholder = self._artifact_row(payload, "output")["placeholder"]
                return "\n".join([
                    "```json",
                    "{",
                    '  "action_type": "execute",',
                    '  "action_kind": "primary",',
                    '  "purpose": "analysis",',
                    '  "argv": [',
                    '    "bash",',
                    '    "-lc",',
                    '    "test -n \\"$OUT\\"",',
                    '    "env": {',
                    f'      "OUT": {json.dumps(placeholder)}',
                    "    }",
                    "  ]",
                    "}",
                    "```",
                ])
            if ordinal == 2:
                return self._script_action(payload)
            return self._script_action(payload)

        events, captures, _ = await self._run_graph("graph-e-json-repair", candidate)
        durable = load_plan_state(self.root, "graph-e-json-repair")
        visited = [next(iter(event)) for event in events]

        self.assertGreaterEqual(len(captures), 3)
        repair_request = str(captures[1].get("_provider_request") or "")
        self.assertIn("<semantic_history>", repair_request)
        self.assertIn("<capability_instructions>", repair_request)
        self.assertIn("sequence/search/local-blast", repair_request)
        self.assertIn("Create the plan-local directory", repair_request)
        self.assertIn(self._artifact_row(captures[1], "output")["placeholder"], repair_request)
        self.assertIn("<action_repair>", repair_request)
        self.assertIn('"repair_stage": "script_block"', repair_request)
        self.assertIn('"code": "GENERATE_SCRIPT_BLOCK_INVALID"', repair_request)
        self.assertIn("one <shell>, <python>, or <r> block is required", repair_request)
        self.assertIn('"previous_candidate"', repair_request)
        self.assertIn("Repair the previous generated action instead of regenerating", repair_request)
        self.assertIn("repair_previous_action", repair_request)
        self.assertNotIn("regenerate_script", repair_request)
        self.assertEqual(visited.count("execute"), 2)
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        self.assertEqual(budgets["action_format_repair_by_step"]["step-1"], 1)
        self.assertEqual(dict(budgets.get("generate_attempts_by_step") or {}), {})
        self.assertNotEqual(
            str((durable.get("task_state") or {}).get("status") or ""),
            "failed",
            json.dumps({
                "task_state": durable.get("task_state"),
                "failure": durable.get("failure"),
                "primary_failure": durable.get("primary_failure"),
                "steps": durable.get("steps"),
            }, ensure_ascii=False, default=str),
        )

    async def test_graph_f_session_1787622048_raw_literals_reach_execute(self) -> None:
        scripts: list[str] = []
        task_id = "session-1787622048-prepare-snpeff-config"

        def candidate(payload: Mapping[str, Any], ordinal: int) -> str:
            runtime = dict((payload.get("current_focus") or {}).get("runtime") or {})
            environment = [
                dict(item)
                for item in runtime.get("artifact_environment", []) or []
                if isinstance(item, Mapping)
            ]
            task = dict(payload.get("task") or {})
            artifacts = {
                str(item.get("handle") or ""): dict(item)
                for item in [
                    *(task.get("inputs") or []),
                    *(task.get("outputs") or []),
                ]
                if isinstance(item, Mapping)
            }
            lines: list[str] = []
            for index, row in enumerate(environment):
                env_name = str(row.get("env_var") or "")
                handle = str(row.get("semantic_handle") or "")
                target = f"ARTIFACT_{index}"
                durable = load_plan_state(self.root, task_id)
                ledger = dict(durable.get("plan_artifacts") or {})
                artifact_id = handle.partition(".")[2]
                literal_path = str(
                    (ledger.get(artifact_id) or {}).get("canonical_path")
                    or (artifacts.get(handle) or {}).get("required_basename")
                    or "/invalid/literal-fallback"
                )
                lines.append(f'{target}="${{{env_name}:-{literal_path}}}"')
                if handle.startswith("input."):
                    lines.append(f'test -e "${target}"')
                elif str(row.get("artifact_kind") or "") == "directory":
                    lines.append(f'mkdir -p "${target}"')
                    lines.append(f': > "${target}/.prepared"')
                else:
                    lines.append(f'mkdir -p "$(dirname "${target}")"')
                    lines.append(f'printf "prepared\\n" > "${target}"')
            lines.extend([
                "cat <<EOF",
                "Custom genome from provided genome.fasta and genome.gtf",
                "EOF",
            ])
            lines.append("true")
            script = "<shell>\n" + "\n".join(lines) + "\n</shell>"
            scripts.append(script)
            return script

        events, captures, _ = await self._run_graph(
            task_id,
            candidate,
        )
        durable = load_plan_state(self.root, task_id)
        visited = [next(iter(event)) for event in events]

        self.assertEqual(len(captures), 2)
        self.assertEqual(visited.count("execute"), 2)
        self.assertIn("Custom genome from provided genome.fasta and genome.gtf", scripts[0])
        self.assertIn(":-", scripts[0])
        repair_request = str(captures[1].get("_provider_request") or "")
        self.assertNotIn("<action_repair>", repair_request)
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        self.assertEqual(int(budgets.get("action_format_repair_count") or 0), 0)
        self.assertEqual(dict(budgets.get("generate_attempts_by_step") or {}), {})
        self.assertIn("MANAGED_LITERAL_IN_FREE_SOURCE", json.dumps(events, default=str))
        self.assertEqual(
            str((durable.get("task_state") or {}).get("status") or ""),
            "completed",
        )
        self.assertFalse(durable.get("failure"))

    async def test_graph_c_same_malformed_representation_converges(self) -> None:
        def invalid(_payload: Mapping[str, Any], _ordinal: int) -> str:
            return "not-an-executable-script-block"

        events, captures, _ = await self._run_graph("graph-c-no-progress", invalid)
        durable = load_plan_state(self.root, "graph-c-no-progress")
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        visited = [next(iter(event)) for event in events]

        self.assertEqual(len(captures), 2)
        self.assertEqual(budgets["action_format_repair_by_step"]["step-1"], 1)
        self.assertEqual(dict(budgets.get("generate_attempts_by_step") or {}), {})
        self.assertNotIn("execute", visited)
        self.assertIn("GENERATE_SCRIPT_BLOCK_INVALID", json.dumps(events, default=str))
        self.assertIn(str((durable.get("task_state") or {}).get("status") or ""), {"blocked", "failed"})
        self.assertNotIn("CORRECTIVE_GENERATE", json.dumps(events, default=str))

    def test_graph_d_projection_failure_is_internal_and_budget_neutral(self) -> None:
        visible = {
            "lost-output": {
                "direction": "output",
                "aliases": ["output.lost"],
                "row": {"artifact_id": "lost-output"},
                "runtime_available": True,
            }
        }
        _, violations = materialize_managed_action(
            {"argv": ["tool", "{output:lost}"]},
            {},
            approved_visible_artifacts=visible,
        )

        self.assertEqual(violations[0].code, "PLAN_ARTIFACT_BINDING_PROJECTION_MISSING")
        self.assertEqual(violations[0].owner, "INTERNAL_PROTOCOL")
        self.assertEqual(violations[0].route, "internal_reproject")


if __name__ == "__main__":
    unittest.main()
