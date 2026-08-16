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
from agent_core.recovery import RecoveryDecisionKind, RecoveryStateStore
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

    def test_n1_first_rejection_commits_existing_repair_budget(self) -> None:
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
                violations=self._violation(),
                visible_artifacts={},
            )
            guarded = agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": "generate",
                    "pending_recovery_action": {"type": "ACTION_FORMAT_REPAIR"},
                    "recovery_context": context.to_dict(),
                    "recovery_decision": decision.to_dict(),
                },
                "preaction",
            )
            budgets = load_plan_state(root, "preaction")["task_state"]["budgets"]

        self.assertEqual(decision.kind, RecoveryDecisionKind.FORMAT_REPAIR.value)
        self.assertEqual(guarded["next_step"], "generate")
        self.assertEqual(budgets["action_format_repair_count"], 1)
        self.assertEqual(budgets["action_format_repair_by_step"]["step-1"], 1)
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
                violations=self._violation(),
                visible_artifacts={},
            )
            agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": "generate",
                    "pending_recovery_action": {"type": "ACTION_FORMAT_REPAIR"},
                    "recovery_context": first_context.to_dict(),
                    "recovery_decision": first_decision.to_dict(),
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
                violations=self._violation(),
                visible_artifacts={},
            )
            guarded = agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": second_decision.next_route,
                    "pending_recovery_action": {"type": "internal_protocol_error"},
                    "recovery_context": second_context.to_dict(),
                    "recovery_decision": second_decision.to_dict(),
                },
                "preaction",
            )
            budgets = load_plan_state(root, "preaction")["task_state"]["budgets"]

        self.assertEqual(second_decision.kind, RecoveryDecisionKind.ESCALATE.value)
        self.assertEqual(guarded["next_step"], "finish")
        self.assertEqual(budgets["action_format_repair_count"], 1)

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
                violations=self._violation(),
                visible_artifacts={},
            )
            agent._apply_transition_guard(
                "generate",
                {"generate_response_cached": True},
                {
                    "next_step": "generate",
                    "pending_recovery_action": {"type": "ACTION_FORMAT_REPAIR"},
                    "recovery_context": context.to_dict(),
                    "recovery_decision": decision.to_dict(),
                },
                "preaction",
            )
            count = load_plan_state(root, "preaction")["task_state"]["budgets"][
                "action_format_repair_count"
            ]
        self.assertEqual(count, 1)


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
        self.agent.middleware.review_code = lambda code, _language: ("approve", code)

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
                "local_ref": "work_dir",
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
                    "outputs": ["work_dir"],
                    "dependencies": [],
                    "success_criteria": ["the declared directory exists"],
                    "skill_ids": [self.SKILL_ID],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                    "semantic_constraint_ids": [],
                },
                {
                    "step_id": "step-2",
                    "title": "Consume the directory and write the report",
                    "action": "execute",
                    "purpose": "analysis",
                    "tool": "test",
                    "inputs": ["work_dir"],
                    "outputs": ["output.report"],
                    "dependencies": ["step-1"],
                    "success_criteria": ["the report exists and is non-empty"],
                    "skill_ids": [self.SKILL_ID],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                    "semantic_constraint_ids": [],
                },
            ],
            "semantic_claims": [],
            "validation_checklist": [{
                "check_id": "check-plan-local-flow",
                "description": "verify the directory and final report",
            }],
            "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
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

    async def _run_graph(
        self,
        task_id: str,
        generate_action: Any,
        *,
        binding_patch: Any = None,
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
                captures.append(payload)
                return AIMessage(content=json.dumps(generate_action(payload, len(captures))))
            raise AssertionError(f"unexpected stage: {stage}")

        async def fake_run_script(
            _script_path: str,
            *,
            expected_sha256: str = "",
            interpreter: str = "bash",
            lifecycle_callback: Any = None,
        ):
            del expected_sha256, interpreter
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
            durable = load_plan_state(self.root, task_id)
            _, step = current_plan_step_with_index(durable)
            if str((step or {}).get("step_id") or "") == "step-1":
                ledger = dict(durable.get("plan_artifacts") or {})
                self.assertEqual(len(ledger), 1)
                Path(next(iter(ledger.values()))["canonical_path"]).mkdir(parents=True, exist_ok=True)
            else:
                report_path.write_text("plan-local directory consumed\n", encoding="utf-8")
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_EXITED", {"returncode": 0})
            return "stubbed external tool succeeded\n", "", 0

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

    async def test_graph_a_plan_local_directory_happy_path(self) -> None:
        events, captures, report = await self._run_graph(
            "graph-a-plan-local",
            lambda payload, _ordinal: self._execute_action(payload),
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

    async def test_graph_b_atomic_placeholder_repair_then_execute(self) -> None:
        candidate_fingerprints: list[str] = []

        def candidate(payload: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
            action = self._execute_action(payload)
            if ordinal == 1:
                placeholder = self._artifact_row(payload, "output")["placeholder"]
                action["argv"] = [f"{placeholder}/child"]
            elif ordinal == 2:
                placeholder = self._artifact_row(payload, "output")["placeholder"]
                action["env"] = {"WORK_DIR": placeholder}
                action["argv"] = ["/bin/true", "$WORK_DIR/child"]
            candidate_fingerprints.append(managed_action_fingerprint(action))
            return action

        events, captures, _ = await self._run_graph("graph-b-repair", candidate)
        durable = load_plan_state(self.root, "graph-b-repair")
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        visited = [next(iter(event)) for event in events]

        self.assertGreaterEqual(len(captures), 3)
        self.assertNotEqual(candidate_fingerprints[0], candidate_fingerprints[1])
        self.assertEqual(budgets["action_format_repair_by_step"]["step-1"], 1)
        self.assertEqual(visited.count("execute"), 2)
        self.assertNotIn("renderer", visited)
        self.assertIs((durable.get("failure") or {}).get("execution_started"), True)

    async def test_graph_c_same_invalid_candidate_converges(self) -> None:
        def invalid(payload: Mapping[str, Any], _ordinal: int) -> dict[str, Any]:
            action = self._execute_action(payload)
            placeholder = self._artifact_row(payload, "output")["placeholder"]
            action["argv"] = [f"{placeholder}/child"]
            return action

        events, captures, _ = await self._run_graph("graph-c-no-progress", invalid)
        durable = load_plan_state(self.root, "graph-c-no-progress")
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        visited = [next(iter(event)) for event in events]

        self.assertEqual(len(captures), 2)
        self.assertEqual(budgets["action_format_repair_by_step"]["step-1"], 1)
        self.assertNotIn("execute", visited)
        self.assertIn("repair_no_progress", json.dumps(events, default=str))
        self.assertIn(str((durable.get("task_state") or {}).get("status") or ""), {"blocked", "failed"})
        failure = dict(durable.get("failure") or {})
        self.assertEqual(failure.get("error_code"), "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED")
        self.assertEqual(failure.get("owner"), "GENERATED_ACTION")
        self.assertEqual(failure.get("stage"), "pre_action_contract")
        self.assertEqual(failure.get("termination_reason"), "repair_no_progress")
        self.assertIs(failure.get("execution_started"), False)
        self.assertNotIn("internal_protocol_error", json.dumps(events, default=str))
        self.assertNotIn("INCOMPLETE_EVIDENCE", json.dumps(events, default=str))
        self.assertNotIn("RUN_ALLOWED_PROBE", json.dumps(events, default=str))
        self.assertNotEqual(
            str(next((step.get("status") for step in durable.get("steps", []) if step.get("step_id") == "step-1"), "")),
            "diagnosing",
        )

    async def test_graph_d_projection_failure_is_internal_and_budget_neutral(self) -> None:
        events, captures, _ = await self._run_graph(
            "graph-d-projection",
            lambda payload, _ordinal: self._execute_action(payload),
            binding_patch=lambda *_args, **_kwargs: {},
        )
        durable = load_plan_state(self.root, "graph-d-projection")
        budgets = (durable.get("task_state") or {}).get("budgets") or {}
        serialized = json.dumps(events, ensure_ascii=False, default=str)

        self.assertEqual(len(captures), 1)
        self.assertEqual(int(budgets.get("action_format_repair_count") or 0), 0)
        self.assertIn("INTERNAL_CONTRACT_CONFLICT", serialized)
        self.assertIn("controller_projection", serialized)
        self.assertNotIn("renderer", [next(iter(event)) for event in events])


if __name__ == "__main__":
    unittest.main()
