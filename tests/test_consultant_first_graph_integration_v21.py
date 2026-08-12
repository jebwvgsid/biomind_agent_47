from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

import bio_agents
from agent_core.completion_gate import CompletionGate
from agent_core.diagnostics import build_recovery_context
from agent_core.plan_tracker import (
    CanonicalTaskStateError,
    canonical_task_spec_for_state,
    load_plan_state,
)
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import task_spec_revision


SKILL_ID = "sequence/search/local-blast"


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
            "task_mode": "simple_execute",
            "retrieval_mode": "none",
            "confidence": 0.99,
            "reason": "integration test lightweight route",
        }))


class _NoopGraph:
    async def astream(self, _state: Any, config: Any = None):
        if False:  # pragma: no cover - keeps this an async generator
            yield config


def _planner_plan(input_path: str, output_path: str, *, two_steps: bool = False) -> str:
    steps = []
    if two_steps:
        steps.append({
            "step_id": "step-1",
            "title": "Validate the corrected FASTA binding",
            "action": "execute",
            "purpose": "validation",
            "tool": "python",
            "inputs": [input_path],
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
        "inputs": [input_path],
        "outputs": [output_path],
        "dependencies": ["step-1"] if two_steps else [],
        "success_criteria": ["report exists and is non-empty"],
        "skill_ids": [SKILL_ID],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
        "semantic_constraint_ids": [],
    })
    return json.dumps({
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
        }

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
            if stage in {"planner", "planner_structure_repair", "planner_repair"}:
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

    async def test_h_snpeff_scenario_keeps_paths_and_resolves_genome_version(self) -> None:
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
        consultant_payload = json.dumps(seen["consultant"], ensure_ascii=False)
        for value in (str(vcf), str(fasta), str(gtf), str(outdir), "U1"):
            self.assertIn(value, consultant_payload)
        durable = load_plan_state(self.root, task_id)
        self.assertEqual(
            durable["task_state"]["selected_skills"],
            ["genomics/snpeff-annotation"],
        )
        self.assertTrue(
            durable["task_state"]["resolved_bindings"]
            ["genomics/snpeff-annotation"]["arguments"]["genome_version"]
        )
        self.assertFalse(durable["task_state"].get("pending_question"))
        input_paths = {
            item.get("canonical_path")
            for item in canonical_task_spec_for_state(durable).get("inputs", [])
        }
        self.assertTrue({str(vcf), str(fasta), str(gtf)}.issubset(input_paths))

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
