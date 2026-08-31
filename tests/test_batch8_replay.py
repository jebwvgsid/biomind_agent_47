import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.context_lineage import (
    ContextLineageStore,
    build_decision_lineage_bundle,
    dump_context_lineage,
    dump_effective_user_facts,
)
from agent_core.plan_tracker import build_plan_state, load_plan_state, save_plan_state
from agent_core.task_spec import (
    apply_user_feedback,
    create_task_spec,
    resolve_effective_user_facts,
)
from memory_manager import MemoryManager


class _SummaryLLM:
    async def ainvoke(self, messages):
        return AIMessage(content="bounded compression summary")


def _persist_spec(root: str, task_id: str, spec):
    plan = build_plan_state(
        task_id,
        "- [>] Step 1: analyze | Action: EXECUTE | Done: validated output exists",
    )
    plan["current_task"] = {"task_spec": spec}
    save_plan_state(root, task_id, plan)
    return plan


def _call(store, kind, request, response, **metadata):
    snapshot = store.start_call(
        [HumanMessage(content=request)],
        {"node_name": kind, "call_kind": kind, **metadata},
    )
    store.finish_call(snapshot["call_id"], AIMessage(content=response))
    return snapshot


class Batch8ReplayTests(unittest.TestCase):
    def test_seven_round_facts_remain_equal_to_dumped_effective_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = create_task_spec("输入 /data/a.mcool；sample=18l", task_id="seven")
            for text in (
                "输出目录 /out/a",
                "resolution=10000",
                "不得覆盖输入",
                "使用 balanced matrix",
                "确认 sample=18l",
                "优先生成 PDF",
            ):
                spec = apply_user_feedback(
                    spec,
                    text,
                    kind="consultant_answer",
                    source_task_id="seven",
                )
            _persist_spec(tmpdir, "seven", spec)
            expected = resolve_effective_user_facts(spec)
            dumped = dump_effective_user_facts(tmpdir, "seven")
            self.assertEqual(dumped["effective_facts_revision"], expected["effective_facts_revision"])
            self.assertEqual(dumped["active_artifacts"], expected["active_artifacts"])
            self.assertEqual(dumped["active_constraints"], expected["active_constraints"])

    def test_gt21_tasks_are_isolated_and_explicit_inheritance_is_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_a = create_task_spec(
                "利用 /data 对18l进行TAD calling，输出到 /out/a",
                task_id="task-a",
            )
            _persist_spec(tmpdir, "task-a", task_a)
            _call(
                ContextLineageStore(tmpdir, "task-a"),
                "task_router",
                "利用 /data 对18l进行TAD calling，输出到 /out/a",
                "managed",
            )

            task_b = create_task_spec("请帮我出图", task_id="task-b")
            task_b = apply_user_feedback(
                task_b,
                "使用 /out/a/tads.bed 作为上一个任务输出",
                kind="consultant_answer",
                source_task_id="task-a",
            )
            plan_b = _persist_spec(tmpdir, "task-b", task_b)
            plan_b["inherited_refs"] = [{"source_task_id": "task-a", "artifact": "/out/a/tads.bed"}]
            save_plan_state(tmpdir, "task-b", plan_b)

            task_c = create_task_spec(
                "请对175l进行区室鉴定，输入 /data/175l.mcool",
                task_id="task-c",
            )
            _persist_spec(tmpdir, "task-c", task_c)

            facts_b = dump_effective_user_facts(tmpdir, "task-b")
            facts_c = dump_effective_user_facts(tmpdir, "task-c")
            self.assertIn("/out/a/tads.bed", json.dumps(facts_b, ensure_ascii=False))
            self.assertNotIn("18l", json.dumps(facts_c, ensure_ascii=False))
            self.assertNotIn("tad_calling", json.dumps(facts_c, ensure_ascii=False).lower())
            bundle_b = build_decision_lineage_bundle(tmpdir, "task-b")
            self.assertEqual(bundle_b["scope"]["explicit_inherited_refs"][0]["source_task_id"], "task-a")
            self.assertEqual(bundle_b["inherited_lineage"][0]["source_task_id"], "task-a")
            self.assertIn(
                "18l",
                bundle_b["inherited_lineage"][0]["user_and_consult_calls"][0]["request"]["messages"][0]["content"],
            )

    def test_fault_replay_keeps_each_decision_layer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "fault")
            _call(store, "task_router", "user goal", "managed")
            _call(store, "consultant", "candidate skills", "selected wrong skill")
            _call(store, "planner_initial", "correct handoff", "wrong plan")
            _call(store, "planner_repair", "preserve constraint", "constraint lost")
            _call(store, "generate", "correct step", "wrong generated script", step_id="step-1")
            _call(store, "supervisor_reviewer", "execution evidence", "incorrect concern", step_id="step-1")
            store.record_event("controller_route", {
                "from_node": "generate", "to_node": "execute", "step_id": "step-1",
            })
            _persist_spec(tmpdir, "fault", create_task_spec("输入 /data/x", task_id="fault"))
            bundle = build_decision_lineage_bundle(tmpdir, "fault", "step-1")
            self.assertEqual(
                [row["manifest"]["call_kind"] for row in bundle["segments"]["planner"]["calls"]],
                ["planner_initial", "planner_repair"],
            )
            self.assertEqual(bundle["segments"]["generate_renderer"]["calls"][0]["manifest"]["call_kind"], "generate")
            self.assertEqual(bundle["segments"]["execute_validate"]["calls"][0]["manifest"]["call_kind"], "supervisor_reviewer")
            self.assertEqual(bundle["controller_lineage"][0]["payload"]["to_node"], "execute")

    def test_compression_does_not_change_facts_or_snapshot_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = create_task_spec("输入 /data/a.vcf；不得猜测数据库", task_id="compressed")
            plan = _persist_spec(tmpdir, "compressed", spec)
            store = ContextLineageStore(tmpdir, "compressed")
            _call(store, "consultant", "X" * 120000, "selected skill/a")
            before_facts = dump_effective_user_facts(tmpdir, "compressed")
            before = build_decision_lineage_bundle(tmpdir, "compressed")

            manager = MemoryManager(
                _SummaryLLM(),
                archive_dir=str(Path(tmpdir) / "archives"),
                max_tokens=100,
                compress_threshold=0.5,
                keep_recent_tokens=20,
            )
            _, compressed = asyncio.run(manager.maybe_compress([
                SystemMessage(content="stable system"),
                HumanMessage(content="old " + "Y" * 10000),
                AIMessage(content="middle " + "Z" * 5000),
                HumanMessage(content="latest answer"),
            ], "compressed"))
            self.assertTrue(compressed)

            plan = load_plan_state(tmpdir, "compressed")
            plan["task_state"]["compression_summary"] = "compressed old messages"
            plan["task_state"]["messages"] = [{"role": "system", "content": "summary only"}]
            save_plan_state(tmpdir, "compressed", plan)

            after_facts = dump_effective_user_facts(tmpdir, "compressed")
            after = build_decision_lineage_bundle(tmpdir, "compressed")
            self.assertEqual(before_facts["effective_facts_revision"], after_facts["effective_facts_revision"])
            self.assertEqual(
                before["segment_plan"][0]["hash"],
                after["segment_plan"][0]["hash"],
            )
            self.assertTrue(Path(after["segments"]["task_user_consult"]["calls"][0]["manifest"]["request_payload_ref"]).is_file())

    def test_legacy_checkpoint_does_not_claim_exact_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_spec = {
                "task_id": "legacy",
                "goal": "legacy goal",
                "user_directives": [{"text": "不得覆盖输入", "source_task_id": "legacy"}],
            }
            _persist_spec(tmpdir, "legacy", legacy_spec)
            bundle = build_decision_lineage_bundle(tmpdir, "legacy")
            self.assertEqual(bundle["trace_status"], "legacy_unavailable")
            self.assertTrue(bundle["reconstructed"])
            self.assertTrue(bundle["not_exact"])
            self.assertEqual(bundle["segments"]["task_user_consult"]["calls"], [])

    def test_debug_dumps_are_read_only_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = create_task_spec("输入 /data/a.vcf；token=ordinary_parameter", task_id="debug")
            _persist_spec(tmpdir, "debug", spec)
            store = ContextLineageStore(tmpdir, "debug")
            _call(store, "consultant", "API_KEY=sk-secret-value /data/a.vcf", "selected skill/a")
            facts_text = dump_effective_user_facts(tmpdir, "debug", as_json=True)
            lineage_text = dump_context_lineage(tmpdir, "debug", as_json=True)
            self.assertNotIn("sk-secret-value", lineage_text)
            self.assertIn("/data/a.vcf", lineage_text)
            self.assertIn("effective_facts_revision", facts_text)
            self.assertNotIn("api_key", facts_text.lower())

    def test_unrelated_task_snapshots_never_enter_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _call(ContextLineageStore(tmpdir, "task-a"), "consultant", "task A", "A")
            _call(ContextLineageStore(tmpdir, "task-b"), "consultant", "task B", "B")
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            payload = json.dumps(bundle, ensure_ascii=False)
            self.assertIn("task A", payload)
            self.assertNotIn("task B", payload)


if __name__ == "__main__":
    unittest.main()
