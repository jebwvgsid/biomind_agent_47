from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from agent_core.diagnostics import RecoveryContext
from agent_core.middleware import error_recovery_middleware
from agent_core.node_context import build_node_context
from agent_core.task_spec import ensure_task_spec
from bio_agents import BioAnalystAgent
from prompts.manager import PromptManager


def _task_spec(goal: str = "Use the public annotation database.") -> dict:
    return {
        "schema_version": 4,
        "task_id": "task-control-id",
        "revision": 1,
        "goal": goal,
        "inputs": [{
            "input_id": "input-vcf-id",
            "semantic_handle": "input.variant_vcf",
            "role": "variant_vcf",
            "path": "/data/input.vcf",
            "format": "vcf",
            "required": True,
        }],
        "output_contracts": [
            {
                "output_id": "artifact-x",
                "semantic_handle": "output.intermediate_vcf",
                "role": "intermediate_vcf",
                "path": "/work/intermediate.vcf",
                "format": "vcf",
                "required": True,
            },
            {
                "output_id": "annotated-output-id",
                "semantic_handle": "output.annotated_vcf",
                "role": "annotated_vcf",
                "path": "/work/annotated.vcf",
                "format": "vcf",
                "required": True,
            },
        ],
        "requirements": [{
            "constraint_id": "constraint-db",
            "semantic_key": "required_behavior.database_visibility",
            "operator": "eq",
            "value": "public",
            "strength": "hard",
        }],
    }


def _plan() -> dict:
    return {
        "plan_id": "plan-control-id",
        "steps": [
            {
                "step_id": "step-1",
                "title": "Normalize variants",
                "action": "Normalize the input VCF",
                "inputs": ["input-vcf-id"],
                "outputs": ["artifact-x"],
                "dependencies": [],
                "success_criteria": ["Intermediate VCF exists"],
                "status": "completed",
            },
            {
                "step_id": "step-2",
                "title": "Annotate variants",
                "action": "Annotate the normalized VCF",
                "inputs": ["artifact-x"],
                "outputs": ["annotated-output-id"],
                "dependencies": ["step-1"],
                "success_criteria": ["Annotated VCF validates"],
                "status": "running",
            },
        ],
    }


def _conversation() -> list[dict]:
    return [
        {"role": "user", "content": "请使用 custom 数据库并保留原始 VCF。"},
        {"role": "consultant", "content": "请确认输出是否需要压缩？"},
        {"role": "user", "content": "需要压缩，并生成索引。"},
    ]


def _handoff() -> dict:
    return {
        "summary": "Annotate the supplied VCF with the accepted database strategy.",
        "selected_skills": ["genomics/snpeff-annotation"],
        "input_files": ["variant_vcf"],
        "database_strategy": "build from the supplied GTF when needed",
        "rejected_candidates": ["REJECTED CONSULTANT OUTPUT"],
        "request.json": "internal request body",
        "run_metrics": "internal metrics",
        "chain_of_thought": "private reasoning",
        "memory_summary": "[Memory Compression #9]",
    }


def _success_records() -> list[dict]:
    return [{
        "execution_id": "execution-control-id",
        "step_id": "step-1",
        "execution_status": "succeeded",
        "exit_code": 0,
        "produced_artifact_ids": ["artifact-x"],
        "output_reconciliation_status": "verified",
        "raw_stdout": "OLD_STDOUT_FULL_" * 2000,
        "raw_stderr": "OLD_STDERR_FULL_" * 2000,
        "code": "historical command must stay outside semantic history",
    }]


def _context(node_type: str, **overrides):
    values = {
        "node_type": node_type,
        "task_spec": _task_spec(),
        "plan_state": _plan(),
        "current_step_id": "step-2",
        "approved_plan_snapshot": _plan(),
        "conversation_turns": _conversation(),
        "consultant_handoff": _handoff(),
        "execution_records": _success_records(),
        "recovery_decisions": [],
    }
    values.update(overrides)
    return build_node_context(**values)


class NodeContextSemanticContinuityTests(unittest.TestCase):
    def test_knowledge_registry_ignores_open_text_but_keeps_typed_policy(self) -> None:
        task_spec = _task_spec()
        task_spec["requirements"] = [
            {
                "requirement_id": "requirement-1",
                "value": "Keep every biologically relevant annotation.",
                "strength": "hard",
                "source_ref": "U1",
            },
            {
                "constraint_id": "missing-java",
                "semantic_key": "failure_policy.java_missing",
                "operator": "on_condition",
                "value": "block",
                "condition": "java_missing",
                "strength": "hard",
                "source_ref": "U2",
            },
        ]
        task_spec = ensure_task_spec(task_spec)
        plan_state = {
            "schema_version": 10,
            "compatibility": {"source_schema_version": 10},
            "current_task": {
                "task_spec": task_spec,
                "task_revision": task_spec["revision"],
            },
            "selected_skill_ids": [],
            "steps": [],
        }
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/nonexistent-read-only-replay"

        registry = agent._knowledge_claim_registry(
            task_spec["task_id"], {}, plan_state
        )
        task_claims = [
            claim for claim in registry.all()
            if claim.source_type == "task_contract"
        ]

        self.assertEqual(
            [claim.attributes.get("semantic_key") for claim in task_claims],
            ["failure_policy.java_missing"],
        )
        self.assertNotIn(
            "Keep every biologically relevant annotation.",
            "\n".join(claim.proposition for claim in registry.all()),
        )

    def test_finalize_projects_open_text_without_generic_coverage_gap(self) -> None:
        task_spec = _task_spec()
        task_spec["requirements"] = [{
            "requirement_id": "requirement-1",
            "value": "Do not use demo or hidden-answer files.",
            "strength": "hard",
            "source_ref": "U1",
        }]
        task_spec = ensure_task_spec(task_spec)
        agent = BioAnalystAgent.__new__(BioAnalystAgent)

        contract, gaps = agent._consultant_finalize_contract_projection(
            task_spec,
            [],
            fallback_source_id="task-contract:test",
        )

        self.assertEqual(gaps, [])
        self.assertEqual(contract["source_addressable_directives"], [])
        self.assertEqual(contract["highlighted_requirements"], [{
            "text": "Do not use demo or hidden-answer files.",
            "importance": "hard",
            "source_refs": ["U1"],
        }])

    def test_accepted_commit_builds_a_structured_handoff_snapshot(self) -> None:
        handoff = BioAnalystAgent._accepted_consultant_handoff_projection({
            "requirements_summary": "Accepted Consultant result.",
            "selected_skills": ["genomics/snpeff-annotation"],
            "task_spec": _task_spec(),
            "provider_response": "REJECTED INTERNAL RESPONSE",
        })

        self.assertEqual(handoff["goal_interpretation"], _task_spec()["goal"])
        self.assertEqual(handoff["input_files"][0]["role"], "variant_vcf")
        self.assertEqual(
            handoff["required_outputs"][0]["role"], "intermediate_vcf"
        )
        self.assertNotIn("hard_constraints", handoff)
        self.assertNotIn("provider_response", handoff)

        context = build_node_context(
            node_type="planner",
            task_spec=_task_spec(),
            consultant_handoff=handoff,
        )
        self.assertEqual(
            context.llm_payload.to_dict()["task"]["highlighted_constraints"][0]["text"],
            "public",
        )

    def test_planner_receives_raw_user_and_only_accepted_handoff(self) -> None:
        payload = _context(
            "planner",
            approved_plan_snapshot=None,
            runtime={
                "runtime_ready": True,
                "available_capabilities": ["genomics/snpeff-annotation"],
            },
        ).llm_payload.to_provider_dict()
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertIn("请使用 custom 数据库并保留原始 VCF。", rendered)
        self.assertIn("genomics/snpeff-annotation", rendered)
        self.assertIn("variant_vcf", rendered)
        self.assertIn("build from the supplied GTF", rendered)
        self.assertNotIn("REJECTED CONSULTANT OUTPUT", rendered)
        self.assertNotIn(
            "selected_skills",
            payload["semantic_history"]["consultant_handoff"],
        )
        self.assertNotIn(
            "input_files",
            payload["semantic_history"]["consultant_handoff"],
        )

    def test_generate_receives_history_and_one_complete_approved_plan(self) -> None:
        context = _context("generate")
        payload = context.llm_payload.to_provider_dict()
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertIn("需要压缩，并生成索引。", rendered)
        self.assertIn("accepted database strategy", rendered)
        self.assertEqual(
            [row["title"] for row in payload["semantic_history"]["approved_plan"]["steps"]],
            ["Normalize variants", "Annotate variants"],
        )
        self.assertNotIn("plan", payload)
        self.assertNotIn("plan.md", rendered)
        self.assertNotIn("PlanSpec", rendered)
        self.assertEqual(
            payload["current_focus"]["current_step"]["handle"],
            "step.step-2",
        )
        self.assertEqual(
            context.llm_payload.to_dict()["task"]["highlighted_constraints"][0]["text"],
            "public",
        )

    def test_generate_retry_has_byte_identical_stable_prefix(self) -> None:
        first = _context("generate", feedback={"repair": "first"})
        second = _context("generate", feedback={"repair": "second"})
        prompt_one = PromptManager.get_generate_v2_prompt(
            first.llm_payload.to_dict(), capability_instructions="same capability"
        )
        prompt_two = PromptManager.get_generate_v2_prompt(
            second.llm_payload.to_dict(), capability_instructions="same capability"
        )
        prefix_one = prompt_one.split("<current_focus>", 1)[0]
        prefix_two = prompt_two.split("<current_focus>", 1)[0]
        self.assertEqual(prefix_one.encode(), prefix_two.encode())
        self.assertNotEqual(prompt_one, prompt_two)

    def test_planner_keeps_stable_history_and_changes_only_current_focus(self) -> None:
        first = _context("planner", feedback={"correction": "first"})
        second = _context("planner", feedback={"correction": "second"})
        payload_one = json.loads(PromptManager.get_planner_v2_human_prompt(
            first.llm_payload.to_provider_dict()
        ))
        payload_two = json.loads(PromptManager.get_planner_v2_human_prompt(
            second.llm_payload.to_provider_dict()
        ))
        self.assertEqual(
            payload_one["semantic_history"], payload_two["semantic_history"]
        )
        self.assertNotEqual(
            payload_one["current_focus"], payload_two["current_focus"]
        )

    def test_recovery_separates_stable_history_from_current_failure(self) -> None:
        context = _context(
            "recovery",
            runtime={
                "command": "java -jar snpEff.jar",
                "exit_code": 1,
                "expected_java": 21,
                "actual_java": 8,
            },
            feedback={"diagnosis": "class 65 cannot run on class 52"},
            evidence=[{
                "kind": "execution_log",
                "excerpt": "CURRENT_STDERR_FOCUSED",
            }],
        )
        payload = context.llm_payload.to_provider_dict()
        recovery_prompt = PromptManager.get_recovery_v2_prompt(
            context.llm_payload.to_dict()
        )
        stable = json.dumps(payload["semantic_history"], ensure_ascii=False)
        focus = json.dumps(payload["current_focus"], ensure_ascii=False)
        for expected in (
            "请使用 custom 数据库", "accepted database strategy",
            "Use the public annotation database", "Normalize variants",
            "completed", "output.intermediate_vcf",
        ):
            self.assertIn(expected, stable)
        for expected in (
            "java -jar snpEff.jar", "exit_code", "CURRENT_STDERR_FOCUSED",
            "class 65 cannot run on class 52",
        ):
            self.assertIn(expected, focus)
        self.assertNotIn("OLD_STDOUT_FULL_", stable)
        self.assertNotIn("OLD_STDERR_FULL_", stable)
        self.assertEqual(
            context.llm_payload.to_dict()["task"]["highlighted_constraints"][0]["text"],
            "public",
        )
        self.assertIn("may not silently waive", recovery_prompt)
        self.assertNotIn("historical command", stable)

    def test_internal_observability_and_memory_summary_are_absent(self) -> None:
        rendered = _context("generate").llm_payload.to_json()
        for forbidden in (
            "request.json", "internal request body", "run_metrics",
            "internal metrics", "private reasoning", "[Memory Compression #9]",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_raw_and_compiled_semantic_conflict_remain_visible(self) -> None:
        context = _context("planner", approved_plan_snapshot=None)
        prompt = (
            PromptManager.get_planner_v2_system_prompt()
            + PromptManager.get_planner_v2_human_prompt(
                context.llm_payload.to_provider_dict()
            )
        )
        self.assertIn("custom 数据库", prompt)
        self.assertIn("public annotation database", prompt)
        self.assertIn("TASKSPEC_RECONCILIATION_REQUIRED", prompt)

    def test_append_only_conversation_preserves_prior_rows(self) -> None:
        first = _context("planner", approved_plan_snapshot=None)
        appended = [*_conversation(), {"role": "user", "content": "新增澄清。"}]
        second = _context(
            "planner",
            approved_plan_snapshot=None,
            conversation_turns=appended,
        )
        first_rows = first.llm_payload.to_provider_dict()["semantic_history"]["conversation"]
        second_rows = second.llm_payload.to_provider_dict()["semantic_history"]["conversation"]
        self.assertEqual(
            json.dumps(first_rows, ensure_ascii=False).encode(),
            json.dumps(second_rows[:-1], ensure_ascii=False).encode(),
        )
        self.assertEqual(second_rows[-1]["content"], "新增澄清。")


class RecoveryDeterministicBypassTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_prompt_capacity_cannot_block_deterministic_diagnosis(self) -> None:
        class UnexpectedLLM:
            async def ainvoke(self, _messages):
                raise AssertionError("deterministic diagnosis must not call provider")

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>failed</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-127",
                    "attempt_id": "attempt-127",
                    "action_id": "action-127",
                    "step_id": "step-1",
                    "returncode": 127,
                    "execution_status": "failed",
                    "command_role": "primary",
                    "stderr_summary": "tool: command not found",
                },
            }

        with tempfile.TemporaryDirectory() as root, patch.object(
            RecoveryContext,
            "prompt_view",
            side_effect=AssertionError("legacy prompt_view must not gate managed V2"),
        ):
            wrapped = error_recovery_middleware(
                UnexpectedLLM(), data_path=root
            )(execute)
            result = await wrapped({
                "task_id": "deterministic-capacity-bypass",
                "current_step_id": "step-1",
            })

        self.assertIn("recovery_decision", result)


if __name__ == "__main__":
    unittest.main()
