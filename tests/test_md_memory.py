import json
import tempfile
import unittest
from pathlib import Path

from memory.core_store import CoreMemoryStore
from memory.md_memory import MarkdownMemoryManager


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, patch):
        self.patch = patch
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return _Response(json.dumps(self.patch))


class MarkdownMemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_event_is_hot_path_and_dream_curates_memory(self):
        patch = {
            "long_term": {
                "User Preferences": ["Prefer Markdown-only memory."],
                "Project Facts": [],
                "Important Paths": ["Workspace root: D:\\work\\BioMind"],
                "Reusable Fixes": [],
                "Important Decisions": [],
                "Deprecated / Superseded": [],
            },
            "task": {
                "Goal": "Build Markdown memory.",
                "Current Status": "Planner completed.",
                "Key Files and Directories": ["memory/MEMORY.md"],
                "Commands and Results": ["Generated task plan."],
                "Errors and Fixes": [],
                "Artifacts": ["memory/tasks/session_1.md"],
                "Open Questions": [],
            },
            "task_status": "running",
            "log_summary": "Observed planner event.",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MarkdownMemoryManager(_FakeLLM(patch), Path(tmpdir) / "memory")
            await manager.observe_event(
                "session_1",
                "planner",
                messages=[],
                payload={"plan_file": "task_outputs/session_1/plan.md"},
            )
            self.assertEqual(manager.llm.calls, [])

            root = Path(tmpdir) / "memory"
            self.assertTrue((root / "MEMORY.md").exists())
            self.assertTrue((root / "TASKS.md").exists())
            self.assertTrue((root / "tasks" / "session_1.md").exists())
            self.assertTrue((root / "logs" / "session_1.md").exists())
            self.assertTrue((root / "history" / "PENDING.md").exists())

            long_term = (root / "MEMORY.md").read_text(encoding="utf-8")
            task_memory = (root / "tasks" / "session_1.md").read_text(encoding="utf-8")
            log_text = (root / "logs" / "session_1.md").read_text(encoding="utf-8")
            task_index = (root / "TASKS.md").read_text(encoding="utf-8")

            self.assertNotIn("Prefer Markdown-only memory.", long_term)
            self.assertIn("plan_file", log_text)
            self.assertIn("session_1", task_index)

            result = await manager.curate_pending(task_id="session_1")
            self.assertIn("Dream completed.", result)
            self.assertEqual(len(manager.llm.calls), 1)

            long_term = (root / "MEMORY.md").read_text(encoding="utf-8")
            task_memory = (root / "tasks" / "session_1.md").read_text(encoding="utf-8")
            dream_log = (root / "dreams" / "DREAM_LOG.md").read_text(encoding="utf-8")
            pending = (root / "history" / "PENDING.md").read_text(encoding="utf-8")

            self.assertIn("Prefer Markdown-only memory.", long_term)
            self.assertIn("Workspace root: D:\\work\\BioMind", long_term)
            self.assertIn("Build Markdown memory.", task_memory)
            self.assertIn("memory/MEMORY.md", task_memory)
            self.assertIn("Observed planner event.", dream_log)
            self.assertNotIn("plan_file", pending)

    async def test_full_log_is_not_default_prompt_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MarkdownMemoryManager(_FakeLLM({}), Path(tmpdir) / "memory")
            manager.initialize_task("session_2", "Do work")
            manager.log_path("session_2").write_text(
                "# Full Process Log: session_2\n\nSECRET_LOG_ONLY\n",
                encoding="utf-8",
            )

            context = manager.build_prompt_context("session_2")
            combined = "\n".join(context.values())

            self.assertIn("Do work", combined)
            self.assertNotIn("SECRET_LOG_ONLY", combined)

    async def test_lexical_long_term_retrieval_returns_matching_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MarkdownMemoryManager(_FakeLLM({}), Path(tmpdir) / "memory")
            manager.apply_patch(
                "session_3",
                {
                    "long_term": {
                        "Project Facts": ["Alpha dataset lives at /data/alpha."],
                        "Reusable Fixes": ["Beta tool needs --force."],
                    },
                    "task": {},
                },
            )

            result = manager.lexical_retrieve_long_term("alpha dataset", top_k=1)

            self.assertIn("Alpha dataset lives", result)
            self.assertNotIn("Beta tool needs", result)

    async def test_dream_restore_reverts_latest_memory_snapshot(self):
        patch = {
            "long_term": {"Project Facts": ["Dream-only fact."]},
            "task": {"Current Status": "Dreamed."},
            "task_status": "running",
            "dream_summary": "Added dream-only fact.",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MarkdownMemoryManager(_FakeLLM(patch), Path(tmpdir) / "memory")
            manager.append_event("session_4", "execute", payload={"stdout": "ok"})

            await manager.curate_pending(task_id="session_4")
            self.assertIn("Dream-only fact.", manager.read_long_term())

            restore_result = manager.restore_latest_snapshot()
            self.assertIn("Restored Markdown memory snapshot", restore_result)
            self.assertNotIn("Dream-only fact.", manager.read_long_term())

    async def test_dream_writes_facts_to_core_memory_for_future_sessions(self):
        project_id = "project_alpha"
        patch = {
            "user_profile": {"Workflow": ["Prefer complete runnable scripts."]},
            "agent_memory": {"Reusable Fixes": ["Use conda run instead of conda activate."]},
            "projects": {
                project_id: {
                    "paths": {"gff": "/data/project_alpha.gff3"},
                    "defaults": {"dmr_skill": "dmr-expression-filter"},
                }
            },
            "long_term": {
                "Project Facts": ["Project Alpha uses dmr-expression-filter for DMR expression filtering."]
            },
            "task": {"Current Status": "Dreamed facts into core memory."},
            "task_status": "completed",
            "dream_summary": "Added durable Project Alpha facts.",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            core_store = CoreMemoryStore(root, default_project_id=project_id)
            manager = MarkdownMemoryManager(_FakeLLM(patch), root, core_store=core_store, core_project_id=project_id)
            manager.append_event("session_5", "finalize", payload={"status": "completed"})

            await manager.curate_pending(task_id="session_5")

            frozen_before = core_store.read_core(project_id, snapshot_id="session_before")
            self.assertIn("Prefer complete runnable scripts.", frozen_before)
            self.assertIn("Use conda run instead of conda activate.", frozen_before)
            self.assertIn("/data/project_alpha.gff3", frozen_before)
            self.assertIn("dmr-expression-filter", frozen_before)


if __name__ == "__main__":
    unittest.main()
