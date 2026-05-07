import json
import tempfile
import unittest
from pathlib import Path

from memory.wiki_memory import WikiMemoryManager


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.responses:
            return _Response(self.responses.pop(0))
        return _Response("{}")


class WikiMemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_layout_raw_redaction_and_large_output_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".biomind" / "memory"
            manager = WikiMemoryManager(None, root, max_inline_chars=80)

            event_id = manager.append_raw_event(
                task_id="session_1",
                session_id="session_1",
                stage="execute",
                event_type="execution_finished",
                messages=[],
                payload={
                    "api_key": "sk-1234567890abcdef",
                    "stdout": "A" * 200,
                },
            )

            self.assertTrue((root / "purpose.md").exists())
            self.assertTrue((root / "schema.md").exists())
            self.assertTrue((root / "wiki" / "index.md").exists())
            raw_file = root / "raw" / "conversations" / "session_1.jsonl"
            raw_text = raw_file.read_text(encoding="utf-8")
            self.assertIn(event_id, raw_text)
            self.assertIn("sk-[REDACTED]", raw_text)
            self.assertNotIn("sk-1234567890abcdef", raw_text)
            self.assertIn("large_text_ref", raw_text)

            record = json.loads(raw_text.strip())
            self.assertEqual(record["project_id"], "default")
            ref_path = Path(record["payload"]["stdout"]["path"])
            self.assertTrue(ref_path.exists())
            self.assertEqual(ref_path.read_text(encoding="utf-8"), "A" * 200)

            pending = json.loads(manager.pending_file.read_text(encoding="utf-8").strip())
            self.assertEqual(pending["project_id"], "default")

    async def test_patch_appends_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Initial method note.",
                            "sources": ["raw:event-1"],
                        },
                        {
                            "op": "merge_section_items",
                            "path": "methods/alpha.md",
                            "section": "Findings",
                            "items": ["Use SCTransform normalization."],
                            "sources": ["raw:event-2"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Second note should append, not overwrite.",
                            "sources": ["raw:event-3"],
                        },
                    ]
                },
                project_id=project_id,
            )

            page = (Path(tmpdir) / "memory" / "wiki" / "methods" / "alpha.md").read_text(encoding="utf-8")
            self.assertIn("Initial method note.", page)
            self.assertIn("Use SCTransform normalization.", page)
            self.assertIn("Second note should append", page)
            self.assertIn('"raw:event-1"', page)
            self.assertIn('"raw:event-2"', page)
            self.assertIn('"raw:event-3"', page)

    async def test_path_traversal_is_rejected_to_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")

            applied = manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "../outside.md",
                            "title": "Unsafe",
                            "content": "Should not be written.",
                        }
                    ]
                },
                project_id="project_alpha",
            )

            self.assertEqual(applied, 0)
            self.assertFalse((Path(tmpdir) / "outside.md").exists())
            review = manager.read_review_queue()
            self.assertIn("Unsafe wiki page path", review)

    async def test_raw_append_is_idempotent_by_event_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            kwargs = {
                "task_id": "session_2",
                "session_id": "session_2",
                "project_id": "project_alpha",
                "stage": "planner",
                "event_type": "planner_event",
                "payload": {"plan": "Run one step."},
            }

            first = manager.append_raw_event(**kwargs)
            second = manager.append_raw_event(**kwargs)

            self.assertEqual(first, second)
            pending_lines = manager.pending_file.read_text(encoding="utf-8").splitlines()
            raw_lines = (manager.conversations_dir / "session_2.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(pending_lines), 1)
            self.assertEqual(len(raw_lines), 1)

    async def test_ingest_pending_applies_llm_patch_and_marks_processed(self):
        patch = {
            "operations": [
                {
                    "op": "create_page",
                    "path": "decisions/use-wiki.md",
                    "title": "Use Wiki Memory",
                    "type": "decision",
                    "content": "BioMind should use a source-traceable wiki layer.",
                    "sources": ["raw:event-placeholder"],
                }
            ],
            "ingest_summary": "Added wiki memory decision.",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM(["analysis", json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            event_id = manager.append_raw_event(
                task_id="session_3",
                session_id="session_3",
                stage="finalize",
                event_type="task_finalized",
                payload={"summary": "Use wiki memory."},
            )
            patch["operations"][0]["sources"] = [f"raw:{event_id}"]

            result = await manager.ingest_pending()

            self.assertIn("Wiki ingest completed.", result)
            self.assertIn(event_id, manager.processed_file.read_text(encoding="utf-8"))
            self.assertEqual(manager.pending_file.read_text(encoding="utf-8").strip(), "")
            page = manager.read_page("decisions/use-wiki.md")
            self.assertIn("source-traceable wiki layer", page)

    async def test_search_and_prompt_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/sctransform.md",
                            "title": "SCTransform Method",
                            "type": "method",
                            "content": "Use SCTransform normalization for single-cell analysis.",
                            "sources": ["raw:event-1"],
                        }
                    ]
                },
                project_id=project_id,
            )

            hits = manager.search("SCTransform normalization", project_id=project_id)
            context = manager.build_prompt_context("session_4", project_id, "SCTransform")

            self.assertEqual(hits[0]["title"], "SCTransform Method")
            self.assertIn("Retrieved Wiki Memory", context)
            self.assertIn("SCTransform normalization", context)

    async def test_constructor_default_project_is_used_when_project_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                None,
                Path(tmpdir) / "memory",
                default_project_id="project_beta",
            )

            manager.append_raw_event(
                task_id="session_beta",
                session_id="session_beta",
                stage="execute",
                event_type="execution_finished",
                payload={"summary": "Beta project note."},
            )
            raw_text = (manager.conversations_dir / "session_beta.jsonl").read_text(encoding="utf-8")
            self.assertEqual(json.loads(raw_text)["project_id"], "project_beta")

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "projects/beta.md",
                            "title": "Beta Project",
                            "type": "project",
                            "content": "Beta project note.",
                            "sources": ["raw:event-1"],
                        }
                    ]
                }
            )

            page = manager.read_page("projects/beta.md")
            self.assertIn('"project_beta"', page)
            self.assertEqual(manager.search("Beta project note")[0]["title"], "Beta Project")

    async def test_legacy_runtime_memory_migration_is_non_destructive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "memory"
            target = Path(tmpdir) / ".biomind" / "memory"
            (legacy / "session_prompts").mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "MEMORY.md").write_text("legacy memory", encoding="utf-8")
            (legacy / "session_prompts" / "a.prompt.md").write_text("legacy prompt", encoding="utf-8")
            (target / "MEMORY.md").write_text("existing memory", encoding="utf-8")

            copied = WikiMemoryManager.migrate_legacy_runtime_memory(legacy, target)

            self.assertIn(str(target / "session_prompts" / "a.prompt.md"), copied)
            self.assertEqual((target / "MEMORY.md").read_text(encoding="utf-8"), "existing memory")
            self.assertEqual((target / "session_prompts" / "a.prompt.md").read_text(encoding="utf-8"), "legacy prompt")


if __name__ == "__main__":
    unittest.main()
