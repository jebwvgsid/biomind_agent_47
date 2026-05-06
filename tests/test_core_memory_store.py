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
                    "projects": {project_id: {"paths": {"gff": "/data/project_alpha.gff3"}}},
                },
                default_project_id=project_id,
            )

            same_session = store.read_core(project_id, snapshot_id="session_1")
            new_session = store.read_core(project_id, snapshot_id="session_2")
            reloaded = store.reload_snapshot(project_id, snapshot_id="session_1").prompt_text()

            self.assertEqual(first, same_session)
            self.assertNotIn("Prefer complete runnable scripts.", same_session)
            self.assertIn("Prefer complete runnable scripts.", new_session)
            self.assertIn("/data/project_alpha.gff3", new_session)
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

            store.apply_patch({"projects": {"": {"paths": {"gff": "/data/project_beta.gff3"}}}})

            snapshot = store.read_core(snapshot_id="session_beta")
            self.assertIn("project_id: project_beta", snapshot)
            self.assertIn("/data/project_beta.gff3", snapshot)


if __name__ == "__main__":
    unittest.main()
