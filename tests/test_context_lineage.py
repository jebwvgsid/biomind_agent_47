import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.context_lineage import (
    ContextLineageStore,
    SnapshotConflictError,
    build_decision_lineage_bundle,
)
from agent_core.plan_tracker import (
    build_plan_state,
    record_provenance_event,
    save_plan_state,
)
from agent_core.recovery import ExecutionRecord, RecoveryStateStore
from agent_core.router import classify_task_mode
from bio_agents import BioAnalystAgent


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content=self.content)


class ContextLineageSnapshotTests(unittest.TestCase):
    def test_snapshot_preserves_exact_message_roles_and_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            snapshot = store.start_call(
                [
                    SystemMessage(content="system rules"),
                    HumanMessage(content="user request"),
                    AIMessage(content="observable prior answer"),
                ],
                {"node_name": "consultant", "call_kind": "consultant"},
            )
            request = json.loads(Path(snapshot["request_payload_ref"]).read_text(encoding="utf-8"))
            self.assertEqual([row["role"] for row in request["messages"]], ["system", "human", "assistant"])
            self.assertEqual(
                [row["content"] for row in request["messages"]],
                ["system rules", "user request", "observable prior answer"],
            )

    def test_redaction_removes_credentials_but_keeps_paths_and_arguments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            snapshot = store.start_call(
                [HumanMessage(content=(
                    "Authorization: Bearer secret-token\n"
                    "DEEPSEEK_API_KEY=sk-secret-value\n"
                    "/public/home/liusiqi/data/genome.gtf --gtf22"
                ))],
                {
                    "node_name": "generate",
                    "call_kind": "generate",
                    "runtime_audit": {"password": "plain-secret", "env": "bio_gatk"},
                },
            )
            request_text = Path(snapshot["request_payload_ref"]).read_text(encoding="utf-8")
            manifest = json.loads(Path(snapshot["snapshot_ref"]).read_text(encoding="utf-8"))
            self.assertNotIn("secret-token", request_text)
            self.assertNotIn("sk-secret-value", request_text)
            self.assertNotIn("plain-secret", json.dumps(manifest))
            self.assertIn("/public/home/liusiqi/data/genome.gtf", request_text)
            self.assertIn("--gtf22", request_text)
            self.assertTrue(manifest["redaction_manifest"])

    def test_replay_is_idempotent_and_conflicting_explicit_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            metadata = {
                "call_id": "fixed-call",
                "node_name": "planner",
                "call_kind": "planner_initial",
            }
            first = store.start_call([HumanMessage(content="plan this")], metadata)
            second = store.start_call([HumanMessage(content="plan this")], metadata)
            self.assertEqual(first["call_id"], second["call_id"])
            self.assertEqual(len(list(store.root.glob("*/manifest.json"))), 1)
            with self.assertRaises(SnapshotConflictError):
                store.start_call([HumanMessage(content="different prompt")], metadata)

    def test_shared_agent_wrapper_snapshots_the_actual_final_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            agent.llm = _FakeLLM("generated action")
            agent.llm_timeout_seconds = 5
            agent.llm_readonly_timeout_seconds = 5
            agent._latest_llm_call_refs = {}
            messages = [SystemMessage(content="final system"), HumanMessage(content="final user")]
            response = asyncio.run(agent._ainvoke_llm(
                messages,
                stage="generate",
                task_id="task-a",
                task_mode="managed",
                trace_metadata={"call_kind": "generate", "step_id": "step-1"},
            ))
            self.assertEqual(response.content, "generated action")
            snapshots = ContextLineageStore(tmpdir, "task-a").list_snapshots()
            self.assertEqual(len(snapshots), 1)
            request = json.loads(Path(snapshots[0]["request_payload_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(request["messages"], [
                {"role": "system", "content": "final system"},
                {"role": "human", "content": "final user"},
            ])
            self.assertEqual(snapshots[0]["status"], "completed")
            self.assertEqual(agent._latest_llm_call_refs["task-a"][0]["call_kind"], "generate")

    def test_task_router_uses_the_same_snapshot_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = _FakeLLM(json.dumps({
                "task_mode": "managed",
                "retrieval_mode": "kb",
                "confidence": 0.9,
                "reason": "workflow",
            }))
            decision = asyncio.run(classify_task_mode(
                llm,
                "run analysis",
                data_path=tmpdir,
                task_id="task-a",
            ))
            self.assertEqual(decision["task_mode"], "managed")
            snapshot = ContextLineageStore(tmpdir, "task-a").list_snapshots()[0]
            self.assertEqual(snapshot["call_kind"], "task_router")


class DecisionLineageTests(unittest.TestCase):
    def _record_call(self, store, kind, request, response, **metadata):
        snapshot = store.start_call(
            [HumanMessage(content=request)],
            {"node_name": kind, "call_kind": kind, **metadata},
        )
        return store.finish_call(snapshot["call_id"], AIMessage(content=response))

    def test_bundle_proves_wrong_skill_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            self._record_call(
                store,
                "consultant",
                "User asks for compartment calling. Candidates: tad-calling, compartment-calling",
                '{"selected_skills":["genomics/tad-calling"]}',
                selected_skill_ids=["genomics/tad-calling"],
                candidate_skill_ids=["genomics/tad-calling", "genomics/compartment-calling"],
                user_source_refs=[{"text": "请做区室鉴定", "message_id": "u1"}],
            )
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            consult = bundle["segments"]["task_user_consult"]["calls"][0]
            self.assertIn("compartment calling", consult["request"]["messages"][0]["content"])
            self.assertEqual(consult["manifest"]["selected_skill_ids"], ["genomics/tad-calling"])
            self.assertIn("genomics/compartment-calling", consult["manifest"]["candidate_skill_ids"])

    def test_bundle_keeps_planner_initial_and_repair_calls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            self._record_call(store, "planner_initial", "constraint: do not overwrite", "initial plan")
            self._record_call(store, "planner_repair", "repair while preserving constraint", "repaired plan")
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            calls = bundle["segments"]["planner"]["calls"]
            self.assertEqual([row["manifest"]["call_kind"] for row in calls], ["planner_initial", "planner_repair"])
            self.assertIn("do not overwrite", calls[0]["request"]["messages"][0]["content"])

    def test_bundle_records_generated_and_executed_script_difference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generated = root / "generated.sh"
            executed = root / "executed.sh"
            generated.write_text("echo generated\n", encoding="utf-8")
            executed.write_text("echo executed\n", encoding="utf-8")
            plan = build_plan_state(
                "task-a",
                f"- [>] Step 1: run | Action: EXECUTE | Outputs: [{root / 'out.tsv'}] | Done: output exists",
            )
            action = record_provenance_event(plan, "action_bound", {"action": {
                "action_id": "action-a",
                "step_id": "step-1",
                "type": "execute",
                "generated_script_path": str(generated),
            }})
            attempt = record_provenance_event(plan, "attempt_started", {
                "action_id": action["action_id"],
                "executed_script_path": str(executed),
            })
            save_plan_state(tmpdir, "task-a", plan)
            RecoveryStateStore(tmpdir, "task-a").save_execution_record(ExecutionRecord.from_mapping({
                "execution_id": "exec-a",
                "attempt_id": attempt["attempt_id"],
                "action_id": action["action_id"],
                "step_id": "step-1",
                "script_path": str(executed),
                "code": executed.read_text(encoding="utf-8"),
                "exit_code": 0,
                "execution_status": "succeeded",
            }))
            bundle = build_decision_lineage_bundle(tmpdir, "task-a", "step-1", attempt["attempt_id"])
            scripts = bundle["execution_lineage"]["scripts"]
            self.assertNotEqual(scripts[0]["generated_hash"], scripts[0]["executed_hash"])
            self.assertIn("-echo generated", scripts[0]["diff"])
            self.assertIn("+echo executed", scripts[0]["diff"])

    def test_renderer_lineage_does_not_require_generate_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"schema_version": 3, "actions": []}), encoding="utf-8")
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: renderer composite | Action: EXECUTE | Done: manifest actions complete",
            )
            provenance = plan["provenance_state"]
            provenance["manifests"]["manifest-a"] = {
                "manifest_id": "manifest-a",
                "manifest_path": str(manifest_path),
                "renderer_id": "renderer/example",
                "plan_step_id": "step-1",
            }
            save_plan_state(tmpdir, "task-a", plan)
            bundle = build_decision_lineage_bundle(tmpdir, "task-a", "step-1")
            self.assertEqual(bundle["segments"]["generate_renderer"]["calls"], [])
            self.assertEqual(bundle["renderer_lineage"]["manifests"][0]["renderer_id"], "renderer/example")
            self.assertEqual(bundle["trace_status"], "available")

    def test_segment_plan_references_whole_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            self._record_call(store, "consultant", "consult prompt", "consult output")
            self._record_call(store, "planner_initial", "planner prompt", "planner output")
            self._record_call(store, "generate", "generate prompt", "generate output")
            first_event = store.record_event(
                "controller_route",
                {"from_node": "consultant", "to_node": "planner", "step_id": "step-1"},
            )
            second_event = store.record_event(
                "controller_route",
                {"from_node": "consultant", "to_node": "planner", "step_id": "step-1"},
            )
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            self.assertEqual(first_event["event_id"], second_event["event_id"])
            self.assertEqual(len(bundle["controller_lineage"]), 1)
            segment_plan = bundle["segment_plan"]
            self.assertEqual([row["segment"] for row in segment_plan], [
                "task_user_consult", "planner", "generate_renderer", "execute_validate",
            ])
            for row in segment_plan:
                self.assertIn("snapshot_refs", row)
                self.assertIn("hash", row)


if __name__ == "__main__":
    unittest.main()
