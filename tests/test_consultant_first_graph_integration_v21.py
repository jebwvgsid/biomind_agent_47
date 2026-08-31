from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph.message import add_messages

import bio_agents
from agent_core.completion_gate import CompletionGate
from agent_core.diagnostics import build_recovery_context
from agent_core.plan_tracker import (
    CanonicalTaskStateError,
    approve_plan_state,
    canonical_task_spec_for_state,
    load_approved_plan_snapshot,
    load_current_plan_snapshot,
    load_plan_state,
    save_plan_state,
)
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import (
    create_task_spec,
    merge_task_spec,
    task_spec_revision,
)


SKILL_ID = "sequence/search/local-blast"
SNPEFF_SKILL_ID = "genomics/snpeff-annotation"


class _RouterLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def bind(self, **_kwargs: Any) -> "_RouterLLM":
        return self

    def model_copy(self, update: Any = None) -> "_RouterLLM":
        return self

    async def ainvoke(self, messages: Any, **_kwargs: Any) -> AIMessage:
        text = "\n".join(str(getattr(item, "content", item)) for item in messages)
        self.calls.append(text)
        return AIMessage(content=json.dumps({
            "schema_version": 1,
            "request_kind": "execute",
            "execution_scope": "managed",
            "managed_intent_explicit": True,
            "retrieval_mode": "kb",
            "confidence": 0.99,
            "reason": "integration test managed execution",
        }))


class _NoopGraph:
    async def astream(self, _state: Any, config: Any = None):
        if False:  # pragma: no cover - keeps this an async generator
            yield config


def _planner_plan(input_path: str, output_path: str, *, two_steps: bool = False) -> str:
    # Paths remain arguments for the surrounding TaskSpec assertions. Planner
    # candidates themselves use only model-visible semantic handles.
    del input_path, output_path
    steps = []
    if two_steps:
        steps.append({
            "step_id": "step-1",
            "title": "Validate the corrected FASTA binding",
            "action": "execute",
            "purpose": "validation",
            "tool": "python",
            "inputs": ["input.reference_fasta"],
            "outputs": [],
            "dependencies": [],
            "success_criteria": ["the corrected FASTA is readable"],
            "skill_ids": [SKILL_ID],
            "workflow_stage_ids": [],
            "batch_id": "",
            "risk_level": "low",
            "semantic_constraint_ids": [],
        })
    steps.append({
        "step_id": "step-2" if two_steps else "step-1",
        "title": "Write the requested report",
        "action": "execute",
        "purpose": "analysis",
        "tool": "python",
        "inputs": ["input.reference_fasta"],
        "outputs": ["output.report"],
        "dependencies": ["step-1"] if two_steps else [],
        "success_criteria": ["report exists and is non-empty"],
        "skill_ids": [SKILL_ID],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
        "semantic_constraint_ids": [],
    })
    return json.dumps({
        "outcome_type": "PLAN_CANDIDATE",
        "steps": steps,
        "semantic_claims": [],
        "validation_checklist": [{
            "check_id": "check-report",
            "description": "verify report output",
        }],
        "metadata": {
            "schema_version": 1,
            "planner_contract": "structured_plan_v1",
        },
    })


def _planner_v2_plan() -> str:
    return json.dumps({
        "outcome_type": "PLAN_CANDIDATE",
        "steps": [{
            "step_id": "step-1",
            "title": "Write the requested report",
            "action": "execute",
            "purpose": "analysis",
            "tool": "python",
            "inputs": ["input.reference_fasta"],
            "outputs": ["output.report"],
            "dependencies": [],
            "success_criteria": ["report exists and is non-empty"],
            "skill_ids": [SKILL_ID],
            "workflow_stage_ids": [],
            "batch_id": "",
            "risk_level": "low",
        }],
        "validation_checklist": [{
            "check_id": "check-report",
            "description": "verify report output",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    })


def _planner_v2_ordering_only_plan() -> str:
    return json.dumps({
        "outcome_type": "PLAN_CANDIDATE",
        "steps": [
            {
                "step_id": "step-1",
                "title": "Validate the admitted reference input",
                "action": "execute",
                "purpose": "validation",
                "tool": "python",
                "inputs": ["input.reference_fasta"],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["reference input is readable"],
                "skill_ids": [SNPEFF_SKILL_ID],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
            },
            {
                "step_id": "step-2",
                "title": "Write the requested report",
                "action": "execute",
                "purpose": "analysis",
                "tool": "python",
                "inputs": ["input.reference_fasta"],
                "outputs": ["output.report"],
                "dependencies": ["step-1"],
                "success_criteria": ["report exists and is non-empty"],
                "skill_ids": [SNPEFF_SKILL_ID],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
            },
        ],
        "validation_checklist": [{
            "check_id": "check-report",
            "description": "verify report output",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    })


class ConsultantFirstGraphIntegrationV21Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="biomind-v21-graph-"))
        self.router = _RouterLLM()
        self.patches = [
            patch.object(bio_agents, "init_chat_model", return_value=self.router),
            patch.object(bio_agents, "BIOMIND_MEMORY_DIR", str(self.root / "memory")),
            patch.object(bio_agents, "BIOMIND_WIKI_ENABLED", False),
        ]
        for item in self.patches:
            item.start()
        self.agent = bio_agents.BioAnalystAgent(
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

    async def asyncTearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        # LangGraph runs synchronous route callbacks in the loop's default
        # executor.  Explicitly retire that test-local executor so
        # IsolatedAsyncioTestCase never carries an idle worker into loop close.
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None

    def _finish_payload(
        self,
        *,
        input_path: str,
        output_path: str,
        input_turn: str = "U1",
        planner_feedback: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "action_type": "finish",
            "summary": "The semantic task contract is complete.",
            "selected_skills": [SKILL_ID],
            "task_spec_operations": [
                {
                    "operation": "set_goal",
                    "value": "Run local BLAST with the supplied FASTA and create the requested report",
                    "supporting_evidence": [input_turn],
                },
                {
                    "operation": "upsert_input",
                    "input": {
                        "role": "reference_fasta",
                        "path": input_path,
                        "required": True,
                    },
                    "supporting_evidence": [input_turn],
                },
                {
                    "operation": "upsert_output_contract",
                    "output_contract": {
                        "role": "report",
                        "path": output_path,
                        "required": True,
                    },
                    "supporting_evidence": [input_turn],
                },
            ],
            "unresolved_ambiguities": [],
            "planner_feedback": planner_feedback,
        }
        return json.dumps(payload)

    def _runner_state(self, task_id: str, prompt: str) -> dict[str, Any]:
        durable, _, _ = self.agent._persist_user_submission(task_id, prompt)
        intent_admission = {
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
        task_state["intent_admission"] = intent_admission
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
            "request_intent": intent_admission,
            "intent_admission": intent_admission,
            "managed_intent_admitted": True,
        }

    def _ready_planner_state(
        self,
        task_id: str,
        *,
        input_path: Path,
        output_path: Path,
        skill_id: str = SKILL_ID,
    ) -> dict[str, Any]:
        task_spec = merge_task_spec(
            create_task_spec(
                "Use the supplied FASTA and write the requested report",
                task_id=task_id,
            ),
            {
                "inputs": [{
                    "artifact_id": "artifact-reference-fasta",
                    "semantic_handle": "input.reference_fasta",
                    "role": "reference_fasta",
                    "canonical_path": str(input_path),
                    "required": True,
                }],
                "output_contracts": [{
                    "artifact_id": "artifact-report",
                    "semantic_handle": "output.report",
                    "role": "report",
                    "canonical_path": str(output_path),
                    "required": True,
                }],
            },
            source="user_explicit",
            reason="planner_snapshot_reentry_fixture",
        )
        save_plan_state(
            self.root,
            task_id,
            {
                "task_id": task_id,
                "task_spec": task_spec,
                "steps": [],
                "state_generation": 0,
            },
        )
        consultation_state = {
            "phase": "ready_to_plan",
            "consultation_status": "ready",
            "completion_decision": "ready_to_finalize",
            "completion_reason": "test_fixture_complete",
            "resolved_selected_skill_ids": [skill_id],
            "final_contract": {
                "summary": "The managed task contract is complete.",
                "selected_skills": [skill_id],
                "constraints": [],
            },
        }
        return {
            "messages": [HumanMessage(content=str(task_spec.get("goal") or ""))],
            "task_id": task_id,
            "session_id": task_id,
            "project_id": self.agent.default_project_id,
            "task_mode": "managed",
            "task_spec": task_spec,
            "task_revision": task_spec_revision(task_spec),
            "selected_skills": [skill_id],
            "selected_skills_raw": [skill_id],
            "skill_selection_source": "test_fixture",
            "consultation_state": consultation_state,
            "requirements_summary": "The managed task contract is complete.",
            "resolved_bindings": {},
            "runtime_audit": [],
        }

    async def _seed_pending_candidate(
        self,
        task_id: str,
        *,
        input_path: Path,
        output_path: Path,
        skill_id: str = SKILL_ID,
        planner_payload: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        planner_state = self._ready_planner_state(
            task_id,
            input_path=input_path,
            output_path=output_path,
            skill_id=skill_id,
        )

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            del node_context
            if stage == "planner":
                return AIMessage(content=planner_payload or _planner_v2_plan())
            raise AssertionError(f"unexpected stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        planner_result = await self.agent._graph_node_handlers["planner"](
            planner_state
        )
        self.assertEqual(planner_result["next_step"], "plan_review")
        next_state = dict(planner_state)
        next_state.update({
            key: value for key, value in planner_result.items() if key != "messages"
        })
        next_state["messages"] = add_messages(
            list(planner_state.get("messages") or []),
            list(planner_result.get("messages") or []),
        )
        durable = load_plan_state(self.root, task_id)
        self.assertTrue((durable.get("current_plan") or {}).get("plan_snapshot_ref"))
        self.assertEqual((durable.get("current_approval") or {}).get("status"), "pending")
        return next_state, durable

    async def _run_through_planner(
        self,
        task_id: str,
        *,
        input_path: str,
        output_path: str,
        two_steps: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        prompt = f"Run local BLAST using {input_path} and write {output_path}."
        runner = self._runner_state(task_id, prompt)
        captured: dict[str, Any] = {"consultant": [], "planner": []}

        async def scripted(messages: Any, *, node_context: Any, stage: str, **_kwargs: Any):
            captured.setdefault(stage, []).append({
                "messages": [str(getattr(item, "content", item)) for item in messages],
                "payload": node_context.llm_payload.to_dict(),
            })
            if stage == "consultant":
                return AIMessage(content=self._finish_payload(
                    input_path=input_path,
                    output_path=output_path,
                ))
            if stage == "planner":
                return AIMessage(content=_planner_plan(
                    input_path,
                    output_path,
                    two_steps=two_steps,
                ))
            raise AssertionError(f"unexpected stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        events: list[dict[str, Any]] = []

        async def consume() -> None:
            stream = self.agent.app.astream(
                runner,
                config={"configurable": {"thread_id": task_id}},
                stream_mode="updates",
                interrupt_after=["planner"],
            )
            try:
                async for event in stream:
                    events.append(event)
            finally:
                await stream.aclose()

        await asyncio.wait_for(consume(), timeout=30)
        return events, captured

    async def test_a_e_g_full_bootstrap_lightweight_route_and_planner_wrapper(self) -> None:
        task_id = "graph-aeg"
        input_path = self.root / "A.fasta"
        output_path = self.root / "out-a" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        events, captured = await self._run_through_planner(
            task_id,
            input_path=str(input_path),
            output_path=str(output_path),
        )

        self.assertEqual([next(iter(item)) for item in events[:3]], [
            "triage", "consultant", "planner",
        ])
        self.assertEqual(events[0]["triage"]["task_mode"], "managed")
        self.assertEqual(events[0]["triage"]["next_step"], "consultant")
        self.assertTrue(events[1]["consultant"]["canonical_commit"]["succeeded"])
        self.assertIn(str(input_path), json.dumps(captured["consultant"][0], ensure_ascii=False))
        self.assertIn("U1", json.dumps(captured["consultant"][0], ensure_ascii=False))
        durable = load_plan_state(self.root, task_id)
        task_spec = canonical_task_spec_for_state(durable)
        self.assertEqual(task_spec_revision(task_spec), 1)
        self.assertEqual(
            events[1]["consultant"]["task_spec"],
            task_spec,
        )
        self.assertTrue(durable.get("steps"), "Planner did not persist a PlanSpec")
        self.assertNotIn("TypeError", json.dumps(events, ensure_ascii=False, default=str))

    async def test_a2_select_requires_load_then_reenters_and_routes_to_planner(self) -> None:
        task_id = "graph-select-load-select"
        input_path = self.root / "load-first.fasta"
        output_path = self.root / "out-load-first" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        runner = self._runner_state(
            task_id,
            f"Run local BLAST using {input_path} and write {output_path}.",
        )
        self.agent._consultant_runtime_seed_evidence = (
            lambda selected, _task_id: (
                [{"skill_id": skill_id, "status": "ready"} for skill_id in selected],
                {
                    skill_id: {"status": "ready", "source": "test_runtime_probe"}
                    for skill_id in selected
                },
                "",
            )
        )
        select_payload = json.loads(self._finish_payload(
            input_path=str(input_path),
            output_path=str(output_path),
        ))
        select_payload["action_type"] = "select_skill"
        consultant_calls = 0

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            nonlocal consultant_calls
            del node_context
            if stage == "consultant":
                consultant_calls += 1
                if consultant_calls == 1:
                    return AIMessage(content=json.dumps(select_payload))
                if consultant_calls == 2:
                    return AIMessage(content=json.dumps({
                        "action_type": "load_skill_doc",
                        "skill_id": SKILL_ID,
                        "task_hypothesis": "Run documented local BLAST",
                    }))
                if consultant_calls == 3:
                    return AIMessage(content=json.dumps(select_payload))
                raise AssertionError(
                    "Consultant made an extra provider call after the "
                    "load/select recovery sequence"
                )
            if stage == "planner":
                return AIMessage(content=_planner_plan(
                    str(input_path),
                    str(output_path),
                ))
            raise AssertionError(f"unexpected stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        events: list[dict[str, Any]] = []
        stream = self.agent.app.astream(
            runner,
            config={"configurable": {"thread_id": task_id}},
            stream_mode="updates",
            interrupt_after=["planner"],
        )
        with patch.object(
            bio_agents,
            "resolve_formal_upstream_artifacts",
            return_value={
                "relevant": False,
                "status": "not_applicable",
                "formal_candidates": [],
            },
        ):
            try:
                async for event in stream:
                    events.append(event)
            finally:
                await stream.aclose()

        consultant_updates = [
            event["consultant"]
            for event in events
            if "consultant" in event
        ]
        self.assertEqual(
            consultant_calls,
            3,
            json.dumps(
                [
                    {
                        "next_step": item.get("next_step"),
                        "rejection": item.get("consultant_commit_rejection"),
                        "decision": item.get("skill_decision_result"),
                    }
                    for item in consultant_updates
                ],
                ensure_ascii=False,
                default=str,
            ),
        )
        self.assertEqual(
            consultant_updates[0]["skill_decision_result"]["code"],
            "SKILL_DOC_REQUIRED",
        )
        self.assertEqual(
            consultant_updates[0]["consultant_pending_transition"][
                "required_action"
            ],
            "LOAD_SKILL_DOC",
        )
        self.assertEqual(
            consultant_updates[0]["consultant_pending_transition"]["skill_id"],
            SKILL_ID,
        )
        self.assertEqual(
            consultant_updates[1]["consultant_pending_transition"],
            {},
        )
        self.assertEqual(consultant_updates[2]["next_step"], "planner")
        self.assertTrue(consultant_updates[2]["canonical_commit"]["succeeded"])
        self.assertTrue(any("planner" in event for event in events))

    async def test_b_empty_graph_projection_cannot_shadow_committed_task(self) -> None:
        task_id = "graph-b-shadow"
        input_path = self.root / "shadow.fasta"
        output_path = self.root / "out-shadow" / "report.md"
        input_path.write_text(">chr1\nA\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._runner_state(task_id, f"Run local BLAST using {input_path}; write {output_path}")
        binding_context: dict[str, Any] = {}

        async def scripted(messages: Any, *, node_context: Any, stage: str, **_kwargs: Any):
            binding_context["payload"] = node_context.llm_payload.to_dict()
            return AIMessage(content=self._finish_payload(
                input_path=str(input_path),
                output_path=str(output_path),
            ))

        self.agent._ainvoke_node_llm = scripted
        stale_graph_state = self._runner_state(
            task_id,
            "The durable chain already exists; preserve it.",
        )
        stale_graph_state["task_spec"] = {}
        update = await self.agent._graph_node_handlers["consultant"](stale_graph_state)
        durable = canonical_task_spec_for_state(load_plan_state(self.root, task_id))
        self.assertTrue(durable)
        self.assertEqual(update["task_spec"], durable)
        self.assertEqual(update["task_revision"], 1)
        self.assertIn("U1", json.dumps(binding_context, ensure_ascii=False))
        self.assertIn("U2", json.dumps(binding_context, ensure_ascii=False))

    async def test_c_plan_review_feedback_routes_through_consultant_then_planner(self) -> None:
        task_id = "graph-c-review"
        first_input = self.root / "review-A.fasta"
        second_input = self.root / "review-B2.fasta"
        output_path = self.root / "out-review" / "report.md"
        first_input.write_text(">chr1\nA\n", encoding="utf-8")
        second_input.write_text(">chr1\nB\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await self._run_through_planner(
            task_id,
            input_path=str(first_input),
            output_path=str(output_path),
        )
        config = {"configurable": {"thread_id": task_id}}
        snapshot_state = dict(self.agent.app.get_state(config).values)
        self.agent._prompt_user = lambda *_args, **_kwargs: (
            f"FASTA 改成 {second_input}，并且第二步提前"
        )
        review_result = await self.agent._graph_node_handlers["plan_review"](
            snapshot_state
        )
        self.assertEqual(review_result["next_step"], "consultant")
        turns = StateContractStore(self.root, task_id).read_conversation_evidence(
            load_plan_state(self.root, task_id)["conversation_evidence"]
        )
        self.assertEqual([turn["turn_id"] for turn in turns], ["U1", "U2"])
        self.assertEqual(
            task_spec_revision(canonical_task_spec_for_state(load_plan_state(self.root, task_id))),
            1,
            "Plan Review must not parse free text into TaskSpec itself",
        )

        seen: dict[str, Any] = {}

        async def scripted(messages: Any, *, node_context: Any, stage: str, **_kwargs: Any):
            seen.setdefault(stage, []).append(node_context.llm_payload.to_dict())
            if stage == "consultant":
                return AIMessage(content=self._finish_payload(
                    input_path=str(second_input),
                    output_path=str(output_path),
                    input_turn="U2",
                    planner_feedback={
                        "instructions": ["Move the second step earlier in the replanned sequence."],
                        "supporting_evidence": ["U2"],
                    },
                ))
            return AIMessage(content=_planner_plan(
                str(second_input), str(output_path), two_steps=True
            ))

        self.agent._ainvoke_node_llm = scripted
        consultant_state = dict(snapshot_state)
        consultant_state.update({
            key: value for key, value in review_result.items() if key != "messages"
        })
        consultant_state["messages"] = add_messages(
            list(snapshot_state.get("messages") or []),
            list(review_result.get("messages") or []),
        )
        consultant_result = await self.agent._graph_node_handlers["consultant"](
            consultant_state
        )
        self.assertEqual(consultant_result["next_step"], "planner")
        self.assertIn("U1", json.dumps(seen["consultant"][0], ensure_ascii=False))
        self.assertIn("U2", json.dumps(seen["consultant"][0], ensure_ascii=False))
        durable_after_consultant = load_plan_state(self.root, task_id)
        updated = canonical_task_spec_for_state(durable_after_consultant)
        self.assertEqual(task_spec_revision(updated), 2)
        fasta_paths = [
            item.get("canonical_path")
            for item in updated.get("inputs", [])
            if item.get("role") == "reference_fasta"
        ]
        self.assertEqual(fasta_paths, [str(second_input)])

        planner_state = dict(consultant_state)
        planner_state.update({
            key: value for key, value in consultant_result.items() if key != "messages"
        })
        planner_state["messages"] = add_messages(
            consultant_state["messages"],
            list(consultant_result.get("messages") or []),
        )
        planner_result = await self.agent._graph_node_handlers["planner"](
            planner_state
        )
        self.assertIn("planner", seen)
        self.assertIn(
            "Move the second step earlier",
            json.dumps(seen["planner"][0], ensure_ascii=False),
        )
        self.assertEqual(planner_result["next_step"], "plan_review")
        feedback = load_plan_state(self.root, task_id)["task_state"]["planner_feedback"]
        self.assertEqual(feedback["status"], "applied")

    async def test_pending_candidate_reentry_does_not_load_approved_snapshot(self) -> None:
        task_id = "graph-pending-candidate-reentry"
        input_path = self.root / "pending.fasta"
        output_path = self.root / "out-pending" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        planner_state, _durable = await self._seed_pending_candidate(
            task_id,
            input_path=input_path,
            output_path=output_path,
        )

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            del node_context
            if stage == "planner":
                return AIMessage(content=_planner_v2_plan())
            raise AssertionError(f"unexpected stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        with patch.object(bio_agents, "load_approved_plan_snapshot") as loader:
            result = await self.agent._graph_node_handlers["planner"](planner_state)

        loader.assert_not_called()
        self.assertEqual(result["next_step"], "plan_review")

    async def test_approved_snapshot_reentry_loads_approved_snapshot(self) -> None:
        task_id = "graph-approved-snapshot-reentry"
        input_path = self.root / "approved.fasta"
        output_path = self.root / "out-approved" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        planner_state, durable = await self._seed_pending_candidate(
            task_id,
            input_path=input_path,
            output_path=output_path,
        )
        task_spec = canonical_task_spec_for_state(durable)
        approve_plan_state(
            durable,
            task_spec_revision(task_spec),
            review_event_id="review-approved-reentry",
        )
        save_plan_state(self.root, task_id, durable)
        approved = load_plan_state(self.root, task_id)
        self.assertEqual((approved.get("current_approval") or {}).get("status"), "approved")

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            del node_context
            if stage == "planner":
                return AIMessage(content=_planner_v2_plan())
            raise AssertionError(f"unexpected stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        real_loader = bio_agents.load_approved_plan_snapshot
        with patch.object(
            bio_agents,
            "load_approved_plan_snapshot",
            wraps=real_loader,
        ) as loader:
            result = await self.agent._graph_node_handlers["planner"](planner_state)

        loader.assert_called_once()
        self.assertEqual(result["next_step"], "plan_review")

    async def test_approved_snapshot_loader_remains_strict_for_pending_candidate(self) -> None:
        task_id = "graph-pending-loader-strict"
        input_path = self.root / "strict.fasta"
        output_path = self.root / "out-strict" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _planner_state, durable = await self._seed_pending_candidate(
            task_id,
            input_path=input_path,
            output_path=output_path,
        )

        with self.assertRaisesRegex(
            ValueError,
            "APPROVED_PLAN_SNAPSHOT_NOT_APPROVED",
        ):
            load_approved_plan_snapshot(
                self.root,
                task_id,
                state=durable,
            )

    async def test_plan_review_unknown_output_reference_reenters_pending_candidate(self) -> None:
        task_id = "graph-plan-review-pending-roundtrip"
        input_path = self.root / "roundtrip.fasta"
        output_path = self.root / "out-roundtrip" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        graph_state, durable = await self._seed_pending_candidate(
            task_id,
            input_path=input_path,
            output_path=output_path,
        )
        durable["task_output_materializations"] = [{
            "output_ref": "unknown.output.artifact",
            "path": "unknown.txt",
        }]
        save_plan_state(self.root, task_id, durable)
        pending = load_plan_state(self.root, task_id)
        self.assertTrue((pending.get("current_plan") or {}).get("plan_snapshot_ref"))
        self.assertEqual((pending.get("current_approval") or {}).get("status"), "pending")

        self.agent._prompt_user = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocking structural issue must route before human review")
        )
        review_result = await self.agent._graph_node_handlers["plan_review"](
            graph_state
        )
        self.assertEqual(review_result["next_step"], "planner")
        self.assertIn(
            "TASK_OUTPUT_REFERENCE_UNKNOWN",
            {str(item.get("code") or "") for item in review_result["validation_issues"]},
        )
        review_issue = next(
            item
            for item in review_result["validation_issues"]
            if item.get("code") == "TASK_OUTPUT_REFERENCE_UNKNOWN"
        )
        for field in (
            "field_path", "affected_step_ids", "affected_artifact",
            "affected_output", "expected_identity", "observed_identity",
            "machine_evidence",
        ):
            self.assertIn(field, review_issue)
        self.assertNotIn("message", review_issue)
        self.assertNotIn("owner", review_issue)
        self.assertNotIn("route", review_issue)

        reentry_state = dict(graph_state)
        reentry_state.update({
            key: value for key, value in review_result.items() if key != "messages"
        })
        reentry_state["messages"] = add_messages(
            list(graph_state.get("messages") or []),
            list(review_result.get("messages") or []),
        )

        observed_planner_payload: dict[str, Any] = {}
        observed_planner_call: dict[str, Any] = {}

        async def scripted(
            _messages: Any,
            *,
            node_context: Any,
            stage: str,
            **_kwargs: Any,
        ) -> AIMessage:
            if stage == "planner":
                observed_planner_payload.update(
                    node_context.llm_payload.to_provider_dict()
                )
                observed_planner_call.update({
                    "messages": list(_messages),
                    "temperature": _kwargs.get("temperature"),
                })
                return AIMessage(content=_planner_v2_plan())
            raise AssertionError(f"unexpected stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        real_current_loader = load_current_plan_snapshot
        with (
            patch.object(bio_agents, "load_approved_plan_snapshot") as loader,
            patch.object(
                bio_agents,
                "load_current_plan_snapshot",
                wraps=real_current_loader,
            ) as current_loader,
        ):
            planner_result = await self.agent._graph_node_handlers["planner"](reentry_state)

        loader.assert_not_called()
        current_loader.assert_called_once()
        self.assertEqual(len(observed_planner_call["messages"]), 2)
        self.assertIsInstance(observed_planner_call["messages"][0], SystemMessage)
        self.assertIsInstance(observed_planner_call["messages"][1], HumanMessage)
        self.assertEqual(observed_planner_call["temperature"], 0.1)
        self.assertEqual(
            json.loads(observed_planner_call["messages"][1].content),
            observed_planner_payload,
        )
        self.assertNotIn(
            "Create the intermediate summary",
            observed_planner_call["messages"][0].content,
        )
        self.assertEqual(
            observed_planner_payload["semantic_history"]["plan_snapshot"]
            ["snapshot_status"],
            "pending",
        )
        self.assertEqual(
            observed_planner_payload["current_focus"]["planning"]["mode"],
            "correct_previous_plan",
        )
        self.assertIn(
            "TASK_OUTPUT_REFERENCE_UNKNOWN",
            {
                str(item.get("code") or "")
                for item in observed_planner_payload["current_focus"]["planning"]
                ["violations"]
            },
        )
        self.assertEqual(planner_result["next_step"], "plan_review")
        final_state = load_plan_state(self.root, task_id)
        self.assertNotEqual((final_state.get("task_state") or {}).get("status"), "failed")

    async def test_plan_review_accepts_snpeff_same_skill_ordering_only_dependency(
        self,
    ) -> None:
        task_id = "graph-snpeff-ordering-only"
        input_path = self.root / "ordering-only.fasta"
        output_path = self.root / "out-ordering-only" / "report.md"
        input_path.write_text(">chr1\nACGT\n", encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        graph_state, _durable = await self._seed_pending_candidate(
            task_id,
            input_path=input_path,
            output_path=output_path,
            skill_id=SNPEFF_SKILL_ID,
            planner_payload=_planner_v2_ordering_only_plan(),
        )

        self.agent._prompt_user = lambda *_args, **_kwargs: "y"
        review_result = await self.agent._graph_node_handlers["plan_review"](
            graph_state
        )

        self.assertEqual(review_result["next_step"], "generate")
        durable = load_plan_state(self.root, task_id)
        self.assertEqual(
            (durable.get("current_approval") or {}).get("status"),
            "approved",
        )

    async def test_d_go_appends_u1_u2_u3_on_one_task(self) -> None:
        task_id = "graph-d-turns"
        real_app = self.agent.app
        self.agent.app = _NoopGraph()
        try:
            await self.agent.go("first instruction", task_id=task_id)
            await self.agent.go("second instruction", task_id=task_id)
            await self.agent.go("third instruction", task_id=task_id)
        finally:
            self.agent.app = real_app
        durable = load_plan_state(self.root, task_id)
        turns = StateContractStore(self.root, task_id).read_conversation_evidence(
            durable["conversation_evidence"]
        )
        self.assertEqual([turn["turn_id"] for turn in turns], ["U1", "U2", "U3"])
        self.assertEqual(
            [turn["content"] for turn in turns],
            ["first instruction", "second instruction", "third instruction"],
        )

    async def test_h_snpeff_scenario_keeps_paths_without_renderer_binding(self) -> None:
        task_id = "graph-h-snpeff"
        data_dir = self.root / "snpeff-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        vcf = data_dir / "all.filtered.snp.vcf"
        fasta = data_dir / "genome.fasta"
        gtf = data_dir / "genome.gtf"
        outdir = self.root / "snpeff-output"
        vcf.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n", encoding="utf-8")
        fasta.write_text(">chr1\nACGT\n", encoding="utf-8")
        gtf.write_text('chr1\ttest\tgene\t1\t4\t.\t+\t.\tgene_id "g1";\n', encoding="utf-8")
        outdir.mkdir(parents=True, exist_ok=True)
        prompt = (
            "请对这个重测序 SNP VCF 做功能注释，参考基因组和 GTF 已在同一 data 目录。\n"
            f"VCF: {vcf}\nFASTA: {fasta}\nGTF: {gtf}\n输出目录: {outdir}\n"
            "最终至少生成 report.md 和 reproduce.sh。"
        )
        runner = self._runner_state(task_id, prompt)
        seen: dict[str, Any] = {}
        payload = {
            "action_type": "finish",
            "summary": "Use the canonical snpEff annotation renderer.",
            "selected_skills": ["genomics/snpeff-annotation"],
            "task_spec_operations": [
                {"operation": "set_goal", "value": "Annotate the supplied SNP VCF with the supplied FASTA and GTF", "supporting_evidence": ["U1"]},
                {"operation": "upsert_input", "input": {"role": "variant_vcf", "path": str(vcf), "required": True}, "supporting_evidence": ["U1"]},
                {"operation": "upsert_input", "input": {"role": "reference_fasta", "path": str(fasta), "required": True}, "supporting_evidence": ["U1"]},
                {"operation": "upsert_input", "input": {"role": "annotation_gtf", "path": str(gtf), "required": True}, "supporting_evidence": ["U1"]},
                {"operation": "upsert_output_contract", "output_contract": {"role": "output_dir", "path": str(outdir), "required": True}, "supporting_evidence": ["U1"]},
                {"operation": "upsert_output_contract", "output_contract": {"role": "report", "path": str(outdir / "report.md"), "required": True}, "supporting_evidence": ["U1"]},
                {"operation": "upsert_output_contract", "output_contract": {"role": "reproduce_script", "path": str(outdir / "reproduce.sh"), "required": True}, "supporting_evidence": ["U1"]},
            ],
            "unresolved_ambiguities": [],
            "planner_feedback": None,
        }

        async def scripted(messages: Any, *, node_context: Any, stage: str, **_kwargs: Any):
            seen[stage] = {
                "messages": [str(getattr(item, "content", item)) for item in messages],
                "payload": node_context.llm_payload.to_dict(),
            }
            return AIMessage(content=json.dumps(payload))

        self.agent._ainvoke_node_llm = scripted
        events: list[dict[str, Any]] = []

        async def consume() -> None:
            stream = self.agent.app.astream(
                runner,
                config={"configurable": {"thread_id": task_id}},
                stream_mode="updates",
                interrupt_after=["consultant"],
            )
            try:
                async for event in stream:
                    events.append(event)
            finally:
                await stream.aclose()

        await asyncio.wait_for(consume(), timeout=30)
        self.assertEqual(events[0]["triage"]["next_step"], "consultant")
        self.assertTrue(events[1]["consultant"]["canonical_commit"]["succeeded"])
        self.assertEqual(events[1]["consultant"]["next_step"], "planner")
        self.assertEqual(events[1]["consultant"]["effective_route"], "planner")
        self.assertEqual(events[1]["consultant"]["dispatch_route"], "planner")
        consultant_payload = json.dumps(seen["consultant"], ensure_ascii=False)
        for value in (str(vcf), str(fasta), str(gtf), str(outdir), "U1"):
            self.assertIn(value, consultant_payload)
        durable = load_plan_state(self.root, task_id)
        self.assertEqual(
            durable["task_state"]["selected_skills"],
            ["genomics/snpeff-annotation"],
        )
        self.assertNotIn(
            "genomics/snpeff-annotation",
            durable["task_state"].get("resolved_bindings", {}),
        )
        self.assertFalse(durable["task_state"].get("pending_question"))
        input_paths = {
            item.get("canonical_path")
            for item in canonical_task_spec_for_state(durable).get("inputs", [])
        }
        self.assertTrue({str(vcf), str(fasta), str(gtf)}.issubset(input_paths))

    async def test_i_snpeff_generate_only_full_graph_lifecycle(self) -> None:
        task_id = "graph-i-snpeff-generate-only"
        data_dir = self.root / "snpeff-full-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        vcf = data_dir / "variants.vcf"
        fasta = data_dir / "reference.fasta"
        gtf = data_dir / "annotation.gtf"
        outdir = self.root / "snpeff-full-output"
        outdir.mkdir(parents=True, exist_ok=True)
        vcf.write_text(
            "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n",
            encoding="utf-8",
        )
        fasta.write_text(">chr1\nACGT\n", encoding="utf-8")
        gtf.write_text(
            'chr1\ttest\tgene\t1\t4\t.\t+\t.\tgene_id "g1";\n',
            encoding="utf-8",
        )
        output_paths = {
            "annotated_vcf": outdir / "annotated.vcf",
            "ann_summary": outdir / "ann_summary.tsv",
            "report": outdir / "report.md",
            "reproduce_script": outdir / "reproduce.sh",
        }
        prompt = (
            "请执行重测序 SNP 功能注释。\n"
            f"VCF: {vcf}\nFASTA: {fasta}\nGTF: {gtf}\n输出目录: {outdir}"
        )
        runner = self._runner_state(task_id, prompt)
        consultant_payload = {
            "action_type": "finish",
            "summary": "The canonical snpEff task is complete.",
            "selected_skills": ["genomics/snpeff-annotation"],
            "task_spec_operations": [
                {
                    "operation": "set_goal",
                    "value": "Annotate the supplied SNP VCF with the supplied FASTA and GTF",
                    "supporting_evidence": ["U1"],
                },
                *[
                    {
                        "operation": "upsert_input",
                        "input": {"role": role, "path": str(path), "required": True},
                        "supporting_evidence": ["U1"],
                    }
                    for role, path in (
                        ("variant_vcf", vcf),
                        ("reference_fasta", fasta),
                        ("annotation_gtf", gtf),
                    )
                ],
                {
                    "operation": "upsert_output_contract",
                    "output_contract": {
                        "role": "output_dir",
                        "artifact_kind": "directory",
                        "path": str(outdir),
                        "required": True,
                    },
                    "supporting_evidence": ["U1"],
                },
                *[
                    {
                        "operation": "upsert_output_contract",
                        "output_contract": {
                            "output_id": output_id,
                            "role": output_id,
                            "artifact_kind": "file",
                            "path": str(path),
                            "required": True,
                        },
                        "supporting_evidence": ["U1"],
                    }
                    for output_id, path in output_paths.items()
                ],
            ],
            "unresolved_ambiguities": [],
            "planner_feedback": None,
        }
        plan_payload = {
            "outcome_type": "PLAN_CANDIDATE",
            "steps": [{
                "step_id": "step-1",
                "title": "Annotate variants and produce all approved outputs",
                "action": "execute",
                "purpose": "analysis",
                "tool": "snpEff",
                "inputs": [
                    "input.variant_vcf",
                    "input.reference_fasta",
                    "input.annotation_gtf",
                ],
                "outputs": [f"output.{output_id}" for output_id in output_paths],
                "dependencies": [],
                "success_criteria": ["all four approved outputs are non-empty"],
                "skill_ids": ["genomics/snpeff-annotation"],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
                "semantic_constraint_ids": [],
            }],
            "semantic_claims": [],
            "validation_checklist": [{
                "check_id": "all-outputs",
                "description": "verify the four approved outputs",
            }],
            "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
        }
        generate_calls = 0

        async def scripted(messages: Any, *, node_context: Any, stage: str, **_kwargs: Any):
            nonlocal generate_calls
            if stage == "consultant":
                return AIMessage(content=json.dumps(consultant_payload))
            if stage == "planner":
                return AIMessage(content=json.dumps(plan_payload))
            if stage == "generate":
                generate_calls += 1
                if generate_calls == 1:
                    return AIMessage(content=json.dumps({
                        "action_type": "execute",
                        "action_kind": "primary",
                        "purpose": "analysis",
                        "argv": [
                            "/bin/true",
                            *[f"{{output:{output_id}}}" for output_id in output_paths],
                        ],
                        "env": {},
                        "outputs": [f"output.{output_id}" for output_id in output_paths],
                        "produced_outputs": [
                            {
                                "output_id": output_id,
                                "path": f"{{output:{output_id}}}",
                            }
                            for output_id in output_paths
                        ],
                        "expected_evidence": [
                            f"artifact.output.{output_id}" for output_id in output_paths
                        ],
                    }))
                return AIMessage(content=json.dumps({
                    "action_type": "solution",
                    "answer": "All four approved snpEff output contracts were produced.",
                }))
            raise AssertionError(f"unexpected LLM stage: {stage}")

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
            for output_id, path in output_paths.items():
                path.write_text(f"generated {output_id}\n", encoding="utf-8")
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_EXITED", {"returncode": 0})
            return "generated four outputs\n", "", 0

        self.agent._ainvoke_node_llm = scripted
        self.agent._prompt_user = lambda *_args, **_kwargs: "y"
        self.agent._ensure_skill_runtime_ready = lambda *_args, **_kwargs: (True, "")
        self.agent._selected_skill_env_runner_violation = lambda *_args, **_kwargs: ""
        self.agent._workflow_future_skill_violation = lambda *_args, **_kwargs: ""
        self.agent.middleware.review_code = lambda code, _language: ("approve", code)
        events: list[dict[str, Any]] = []

        async def consume() -> None:
            with patch.object(bio_agents, "run_script_path", new=fake_run_script):
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

        await asyncio.wait_for(consume(), timeout=30)
        visited = [next(iter(event)) for event in events]
        self.assertIn("consultant", visited)
        self.assertIn("planner", visited)
        self.assertIn("plan_review", visited)
        self.assertIn("generate", visited)
        self.assertIn("execute", visited)
        self.assertIn("supervisor", visited)
        self.assertIn("supervisor_final_audit", visited)
        self.assertNotIn("renderer", visited)
        self.assertGreaterEqual(generate_calls, 1)
        self.assertTrue(all(path.is_file() and path.stat().st_size > 0 for path in output_paths.values()))
        durable = load_plan_state(self.root, task_id)
        self.assertEqual((durable.get("task_state") or {}).get("status"), "completed")
        self.assertEqual(
            set(durable["steps"][0]["outputs"]),
            set(output_paths),
        )
        canonical_task = canonical_task_spec_for_state(durable)
        output_dir_contract = next(
            row for row in canonical_task.get("output_contracts", [])
            if row.get("output_id") == "output_dir"
        )
        self.assertEqual(output_dir_contract.get("canonical_path"), str(outdir))
        self.assertFalse(any(
            step.get("renderer_composite") or step.get("renderer_invocation_contract")
            for step in durable.get("steps", [])
        ))
        provenance = durable.get("provenance_state") or {}
        artifact_records = [
            record for record in (provenance.get("artifacts") or {}).values()
            if isinstance(record, dict)
            and str(record.get("status") or "") == "valid"
        ]
        self.assertEqual(
            {str(record.get("output_id") or "") for record in artifact_records},
            set(output_paths),
        )

    def test_f_v2_corruption_fails_closed_despite_valid_legacy_alias(self) -> None:
        valid_legacy = {
            "schema_version": 4,
            "revision": 1,
            "goal": "legacy alias must not revive the task",
        }
        corrupt = {
            "schema_version": 8,
            "task_id": "graph-f-corrupt",
            "current_task": {"task_spec": None, "task_revision": 1},
            "task_spec": valid_legacy,
            "steps": [],
        }
        with self.assertRaises(CanonicalTaskStateError):
            canonical_task_spec_for_state(corrupt)
        with self.assertRaises(CanonicalTaskStateError):
            build_recovery_context(corrupt, {})
        result = CompletionGate.validate_final(
            self.root,
            "graph-f-corrupt",
            corrupt,
            "",
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.classification, "canonical_task_state_corrupt")


if __name__ == "__main__":
    unittest.main()
