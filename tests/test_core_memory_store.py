import tempfile
import unittest
from pathlib import Path

from memory.core_store import CoreMemoryStore


class CoreMemoryStoreTests(unittest.TestCase):
    def test_snapshot_is_frozen_until_explicit_reload_or_new_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_id = "project_alpha"
            store = CoreMemoryStore(Path(tmpdir) / "memory", default_project_id=project_id)

            first = store.read_core(project_id, snapshot_id="session_1")
            store.apply_patch(
                {
                    "user_profile": {"Preferences": ["Prefer complete runnable scripts."]},
                    "projects": {project_id: {"project_root": "/data/project_alpha"}},
                },
                default_project_id=project_id,
            )

            same_session = store.read_core(project_id, snapshot_id="session_1")
            new_session = store.read_core(project_id, snapshot_id="session_2")
            reloaded = store.reload_snapshot(project_id, snapshot_id="session_1").prompt_text()

            self.assertEqual(first, same_session)
            self.assertNotIn("Prefer complete runnable scripts.", same_session)
            self.assertIn("Prefer complete runnable scripts.", new_session)
            self.assertIn("/data/project_alpha", new_session)
            self.assertIn("Prefer complete runnable scripts.", reloaded)

    def test_layout_does_not_create_gt21_project_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            store = CoreMemoryStore(root)

            self.assertFalse((root / "projects" / "GT21.yaml").exists())
            self.assertFalse((root / "projects" / "default.yaml").exists())

            default_snapshot = store.read_core(snapshot_id="session_default")

            self.assertTrue((root / "projects" / "default.yaml").exists())
            self.assertIn("project_id: default", default_snapshot)
            self.assertNotIn("GT21", default_snapshot)

    def test_constructor_default_project_is_used_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            store = CoreMemoryStore(root, default_project_id="project_beta")

            store.apply_patch({"projects": {"": {"project_root": "/data/project_beta"}}})

            snapshot = store.read_core(snapshot_id="session_beta")
            self.assertIn("project_id: project_beta", snapshot)
            self.assertIn("/data/project_beta", snapshot)

    def test_project_facts_are_guarded_to_keep_core_small(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            store = CoreMemoryStore(root, default_project_id="project_alpha")

            store.apply_patch(
                {
                    "projects": {
                        "project_alpha": {
                            "project_root": "/data/project_alpha",
                            "paths": {"gff": "/data/project_alpha.gff3"},
                            "parameters": {"min_q": 20},
                        }
                    }
                }
            )

            snapshot = store.read_core("project_alpha", snapshot_id="session_alpha")
            review = (root / "CORE_REVIEW.md").read_text(encoding="utf-8")
            self.assertIn("project_root", snapshot)
            self.assertNotIn("project_alpha.gff3", snapshot)
            self.assertIn("paths.gff", review)
            self.assertIn("parameters.min_q", review)

    def test_agent_memory_rejects_project_specific_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            store = CoreMemoryStore(root, default_project_id="project_alpha")

            store.apply_patch(
                {
                    "agent_memory": {
                        "Project Notes": ["Alpha dataset lives at /data/alpha.gff3."],
                        "Reusable Fixes": ["Use conda run instead of conda activate."],
                    }
                }
            )

            agent_memory = (root / "AGENT_MEMORY.md").read_text(encoding="utf-8")
            review = (root / "CORE_REVIEW.md").read_text(encoding="utf-8")
            self.assertIn("Use conda run instead of conda activate.", agent_memory)
            self.assertNotIn("Alpha dataset lives", agent_memory)
            self.assertIn("agent_memory.Project Notes", review)

    def test_core_wiki_lint_reports_detailed_core_facts_and_path_conflicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            store = CoreMemoryStore(root, default_project_id="project_alpha")
            project_path = root / "projects" / "project_alpha.yaml"
            project_path.parent.mkdir(parents=True, exist_ok=True)
            project_path.write_text(
                "project_id: project_alpha\n"
                "updated_at: now\n"
                "facts:\n"
                "  paths.gff: \"/old/project_alpha.gff3\"\n",
                encoding="utf-8",
            )

            report = store.lint_project_memory(
                "project_alpha",
                wiki_claims=[
                    {
                        "id": "claim.gff.path",
                        "text": "The project_alpha gff annotation is /new/project_alpha.gff3.",
                        "status": "active",
                        "confidence": "high",
                        "evidence": ["raw:event-1"],
                    }
                ],
            )

            self.assertIn("Core fact should live in Wiki", report)
            self.assertIn("Core/Wiki path conflict", report)


if __name__ == "__main__":
    unittest.main()
