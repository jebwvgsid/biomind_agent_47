import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
except ModuleNotFoundError:
    langchain_core = types.ModuleType("langchain_core")
    messages_module = types.ModuleType("langchain_core.messages")

    class _BaseMessage:
        def __init__(self, content: str = ""):
            self.content = content

    class SystemMessage(_BaseMessage):
        pass

    class HumanMessage(_BaseMessage):
        pass

    class AIMessage(_BaseMessage):
        pass

    messages_module.SystemMessage = SystemMessage
    messages_module.HumanMessage = HumanMessage
    messages_module.AIMessage = AIMessage
    sys.modules.setdefault("langchain_core", langchain_core)
    sys.modules["langchain_core.messages"] = messages_module

from memory_manager import MemoryManager


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str = "summary"):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return _Response(self.content)


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_prompt_is_adaptive_and_configurable(self):
        llm = _FakeLLM("ok")
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(llm, archive_dir=tmpdir, summary_max_words=750)

            result = await manager._summarize(
                [
                    HumanMessage(content="Run scRNA QC on /data/sample1.rds."),
                    AIMessage(content="<execute>Rscript qc.R --input /data/sample1.rds</execute>"),
                ],
                "task_sc",
            )

        prompt = llm.calls[0][0].content
        required = prompt.split("Domain-specific guidance:", 1)[0]

        self.assertEqual(result, "ok")
        self.assertIn("scientific and bioinformatics analysis tasks", prompt)
        self.assertIn("Task Goal", required)
        self.assertIn("Data and Inputs", required)
        self.assertIn("Methods, Tools, and Environment", required)
        self.assertIn("Metrics, Results, and Observations", required)
        self.assertIn("If the task involves scRNA-seq", prompt)
        self.assertIn("If the task involves chromatin/TAD analysis", prompt)
        self.assertIn("Keep the summary under 750 words.", prompt)
        self.assertNotIn("Reference and Annotation", required)

    async def test_summary_max_words_uses_environment_default(self):
        with patch.dict("os.environ", {"BIOMIND_SUMMARY_MAX_WORDS": "1234"}):
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = MemoryManager(_FakeLLM(), archive_dir=tmpdir)

        self.assertEqual(manager.summary_max_words, 1234)

    async def test_split_messages_prunes_old_arbitrary_system_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(_FakeLLM(), archive_dir=tmpdir, keep_recent_tokens=10_000)

            stable = SystemMessage(content="stable system prompt")
            old_system = SystemMessage(content="Search Result: stale RAG output")
            compression = SystemMessage(content="[Memory Compression #1]\nold summary")
            old_human = HumanMessage(content="old user detail")
            recent_human = HumanMessage(content="recent user detail")

            compressed, archived = manager._split_messages(
                [stable, old_system, compression, old_human, recent_human]
            )

        self.assertIn(stable, compressed)
        self.assertIn(compression, compressed)
        self.assertIn(old_human, compressed)
        self.assertIn(recent_human, compressed)
        self.assertIn(old_system, archived)
        self.assertNotIn(old_system, compressed)

    async def test_split_messages_retains_protected_plan_system_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(_FakeLLM(), archive_dir=tmpdir, keep_recent_tokens=1)

            stable = SystemMessage(content="stable system prompt")
            protected_plan = SystemMessage(content="[Protected Plan]\n- [ ] Step 1: keep this")
            old_human = HumanMessage(content="old " * 80)
            recent_human = HumanMessage(content="recent")

            compressed, archived = manager._split_messages(
                [stable, protected_plan, old_human, recent_human]
            )

        self.assertIn(stable, compressed)
        self.assertIn(protected_plan, compressed)
        self.assertIn(old_human, archived)

    async def test_maybe_compress_archives_and_injects_adaptive_summary(self):
        llm = _FakeLLM("Task Goal\n- compressed summary")
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(
                llm,
                archive_dir=tmpdir,
                max_tokens=50,
                compress_threshold=0.5,
                keep_recent_tokens=8,
                summary_max_words=900,
            )
            messages = [
                SystemMessage(content="stable system prompt"),
                HumanMessage(content="old " * 80),
                HumanMessage(content="recent"),
            ]

            new_messages, compressed = await manager.maybe_compress(messages, "task_1")

            index = json.loads((Path(tmpdir) / "memory_index.json").read_text(encoding="utf-8"))
            archive_exists = Path(index["chunks"][0]["archive_file"]).exists()

        self.assertTrue(compressed)
        self.assertTrue(new_messages[0].content.startswith("[Memory Compression #1]"))
        self.assertIn("Task Goal", new_messages[0].content)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(index["compression_count"], 1)
        self.assertEqual(len(index["chunks"]), 1)
        self.assertTrue(archive_exists)

    async def test_maybe_compress_writes_audit_record_with_prompt_and_summary(self):
        llm = _FakeLLM("Task Goal\n- compressed summary\nNext Action\n- continue")
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(
                llm,
                archive_dir=tmpdir,
                max_tokens=50,
                compress_threshold=0.5,
                keep_recent_tokens=8,
                summary_max_words=700,
            )
            messages = [
                SystemMessage(content="stable system prompt"),
                HumanMessage(content="old /data/sample1.rds " * 80),
                HumanMessage(content="recent"),
            ]

            _new_messages, compressed = await manager.maybe_compress(messages, "task_audit")
            audit_records = manager.read_compression_audit()
            archive_exists = Path(audit_records[0]["archive_file"]).exists()

        self.assertTrue(compressed)
        self.assertEqual(len(audit_records), 1)
        record = audit_records[0]
        self.assertEqual(record["task_id"], "task_audit")
        self.assertIn("scientific and bioinformatics analysis tasks", record["prompt"])
        self.assertIn("Task ID: task_audit", record["prompt"])
        self.assertIn("/data/sample1.rds", record["prompt"])
        self.assertIn("Task Goal", record["summary"])
        self.assertEqual(record["summary_max_words"], 700)
        self.assertGreater(record["total_tokens_before"], record["total_tokens_after"])
        self.assertTrue(archive_exists)

    async def test_compression_audit_can_be_disabled(self):
        llm = _FakeLLM("Task Goal\n- compressed summary")
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(
                llm,
                archive_dir=tmpdir,
                max_tokens=50,
                compress_threshold=0.5,
                keep_recent_tokens=8,
                audit_compressions=False,
            )
            messages = [
                SystemMessage(content="stable system prompt"),
                HumanMessage(content="old " * 80),
                HumanMessage(content="recent"),
            ]

            _new_messages, compressed = await manager.maybe_compress(messages, "task_no_audit")

            self.assertTrue(compressed)
            self.assertEqual(manager.read_compression_audit(), [])
            self.assertFalse((Path(tmpdir) / "compression_audit.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
