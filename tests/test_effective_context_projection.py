import asyncio
import json
import tempfile
import unittest

from langchain_core.messages import AIMessage, HumanMessage

from agent_core.diagnostics import build_recovery_context
from agent_core.completion_gate import _text_for_output_root_discovery
from agent_core.plan_tracker import build_plan_state, save_plan_state
from agent_core.task_spec import (
    apply_user_feedback,
    create_task_spec,
    effective_task_contract,
    format_effective_user_facts_context,
    resolve_effective_user_facts,
)
from bio_agents import BioAnalystAgent
from prompts.manager import PromptManager
from supervisor import _effective_declared_requirements


class _CaptureLLM:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content='{"ok":true}')


def _seven_fact_spec(task_id="task-a"):
    spec = create_task_spec(
        "输入 /data/a.vcf；species=maize；threads=8；不得猜测物种数据库；输出目录: /out/a",
        task_id=task_id,
    )
    spec = apply_user_feedback(
        spec,
        "确认选择 custom_mode，sample_a compared_with sample_b",
        kind="consultant_answer",
        source_task_id=task_id,
    )
    return spec


class EffectiveContextProjectionTests(unittest.TestCase):
    def test_shared_projection_is_captured_by_managed_prompts(self):
        spec = _seven_fact_spec()
        context = format_effective_user_facts_context(spec, audience="consultant", max_chars=7000)
        self.assertIn("[EFFECTIVE USER FACTS]", context)
        self.assertIn("[USER SOURCE REFERENCES]", context)
        for value in ("/data/a.vcf", "maize", "threads", "不得猜测物种数据库", "/out/a"):
            self.assertIn(value, context)

        consultant = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="none",
            skills_summary="skill/a",
            rag_history=[],
            task_spec_context=context,
        )
        planner = PromptManager.get_detailed_planner_prompt(
            user_msg="run",
            task_spec_context=context,
            consultant_summary="selected skill/a",
            runtime_audit_context="ready",
            skills_summary="skill/a",
            workflow_handoff="none",
        )
        repair = PromptManager.get_plan_repair_prompt(
            {"allowed_step_ids": ["step-1"], "violations": ["missing field"]},
            effective_facts_context=context,
            selected_skill_context="skill/a",
            renderer_route="renderer/a",
        )
        fake = _CaptureLLM()

        async def capture():
            for prompt in (consultant, planner, repair):
                await fake.ainvoke([HumanMessage(content=prompt)])

        asyncio.run(capture())
        self.assertEqual(len(fake.calls), 3)
        for call in fake.calls:
            prompt = call[0].content
            self.assertIn("/data/a.vcf", prompt)
            self.assertIn("不得猜测物种数据库", prompt)
        self.assertIn("[CONSULT HANDOFF]", planner)
        self.assertIn("[SELECTED SKILL CONTRACT]", planner)
        self.assertIn("[RUNTIME AUDIT]", planner)
        self.assertIn("[WORKFLOW STATE]", planner)
        self.assertIn("Repair scope: schema/format/missing fields/references only", repair)

    def test_generate_projection_uses_current_step_and_effective_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: use B | Action: EXECUTE | Skills: [] | Inputs: [/data/b.mcool] "
                "| Outputs: [result.tsv] | Done: result.tsv exists",
            )
            save_plan_state(tmpdir, "task-a", plan)
            spec = create_task_spec(
                "输入 /data/a.vcf 和 /data/b.mcool；输出目录: /out；不得覆盖输入",
                task_id="task-a",
            )
            projection = json.loads(agent._generate_task_spec_projection(
                {"task_id": "task-a", "task_spec": spec},
                [],
            ))

        paths = {item["locator"] for item in projection["active_artifacts"]}
        self.assertIn("/data/b.mcool", paths)
        self.assertIn("/out", paths)
        self.assertNotIn("/data/a.vcf", paths)
        self.assertTrue(projection["active_constraints"])
        self.assertNotIn("user_directives", projection)

    def test_recovery_level_one_contains_same_effective_fact_revision(self):
        spec = _seven_fact_spec()
        resolved = resolve_effective_user_facts(spec)
        context = build_recovery_context(
            {
                "task_id": "task-a",
                "task_spec": spec,
                "current_step_id": "step-1",
                "pending_action": {"action_id": "action-1"},
            },
            {"execution_id": "exec-1", "step_id": "step-1", "returncode": 1},
            plan_state={"task_id": "task-a", "steps": [{"step_id": "step-1", "status": "running"}]},
        )
        projection = context.effective_user_facts
        self.assertEqual(projection["effective_facts_revision"], resolved["effective_facts_revision"])
        self.assertIn("/data/a.vcf", json.dumps(projection, ensure_ascii=False))

    def test_task_boundary_prevents_archive_or_other_task_override(self):
        task_a = create_task_spec("species=peanut", task_id="task-a")
        task_b = create_task_spec("species=maize", task_id="task-b")
        facts_a = format_effective_user_facts_context(task_a, audience="consultant")
        facts_b = format_effective_user_facts_context(task_b, audience="consultant")
        self.assertIn("peanut", facts_a)
        self.assertNotIn("peanut", facts_b)
        self.assertIn("maize", facts_b)

    def test_effective_contract_excludes_superseded_output_directory(self):
        spec = create_task_spec("输出目录: /out/old", task_id="task-a")
        spec = apply_user_feedback(
            spec,
            "输出目录改为 /out/new",
            kind="consultant_answer",
            source_task_id="task-a",
        )
        contract = effective_task_contract(spec)
        output_dirs = [item["path"] for item in contract["outputs"] if item["role"] == "output_dir"]
        self.assertEqual(output_dirs, ["/out/new"])
        self.assertNotIn("/out/old", json.dumps(contract, ensure_ascii=False))
        gate_context = _text_for_output_root_discovery({"task_spec": spec}, "")
        self.assertIn("/out/new", gate_context)
        self.assertNotIn("/out/old", gate_context)

    def test_supervisor_requirements_use_active_input_binding(self):
        spec = create_task_spec("输入 /data/old.vcf", task_id="task-a")
        spec = apply_user_feedback(
            spec,
            "把 /data/old.vcf 换成 /data/new.vcf",
            kind="consultant_answer",
            source_task_id="task-a",
        )
        requirements = _effective_declared_requirements({"task_spec": spec})
        encoded = json.dumps(requirements, ensure_ascii=False)
        self.assertIn("/data/new.vcf", encoded)
        self.assertNotIn("/data/old.vcf", encoded)


if __name__ == "__main__":
    unittest.main()
