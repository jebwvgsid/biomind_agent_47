import tempfile
import unittest
from pathlib import Path

import package_manager
from package_manager import PackageManager


class PackageManagerRegistrationTests(unittest.TestCase):
    def make_manager(self, root: str) -> PackageManager:
        package_manager.BIO_SOFTWARE_ROOT = None
        package_manager.BIO_SOURCE_INSTALL_DIR = None
        return PackageManager(work_dir=root)

    def test_register_existing_tool_replaces_default_env_with_env_from_conda_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            pm.register_existing_tool(
                tool_name="cooler",
                env_name="bio_cooler",
                source_path="/data0/stu_liusiqi/anaconda3/envs/hicexplore/bin",
            )

            self.assertEqual(pm.registry["tools"]["cooler"]["env"], "hicexplore")
            self.assertIn("cooler", pm.registry["environments"]["hicexplore"]["bound_tools"])
            shim = Path(tmp) / "bin_shims" / "cooler"
            text = shim.read_text(encoding="utf-8")
            self.assertIn("# Env: hicexplore", text)
            self.assertIn("conda activate hicexplore", text)
            self.assertNotIn("export PATH=\"/data0/stu_liusiqi/anaconda3/envs/hicexplore/bin", text)

    def test_register_existing_tool_rejects_explicit_env_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            with self.assertRaisesRegex(ValueError, "source_path belongs to conda env 'hicexplore'"):
                pm.register_existing_tool(
                    tool_name="cooler",
                    env_name="wrong_env",
                    source_path="/data0/stu_liusiqi/anaconda3/envs/hicexplore/bin",
                )

    def test_register_environment_does_not_downgrade_verified_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            pm.register_environment(
                env_name="hicexplore",
                packages=["hicexplorer"],
                status="verified",
                healthcheck="python -c \"print('OK')\"",
            )

            pm.register_environment(
                env_name="hicexplore",
                packages=["cooler"],
                status="registered",
            )

            record = pm.registry["environments"]["hicexplore"]
            self.assertEqual(record["status"], "verified")
            self.assertEqual(record["packages"], ["hicexplorer", "cooler"])

    def test_merge_registry_data_preserves_environment_skill_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            pm.merge_registry_data({
                "environments": {
                    "hicexplore": {
                        "status": "verified",
                        "packages": ["hicexplorer"],
                        "skill_ids": ["genomics/chromatin/tad-calling"],
                        "healthcheck": "python -c \"print('OK')\"",
                    }
                }
            })

            record = pm.registry["environments"]["hicexplore"]
            self.assertEqual(record["status"], "verified")
            self.assertIn("genomics/chromatin/tad-calling", record["skill_ids"])


if __name__ == "__main__":
    unittest.main()
