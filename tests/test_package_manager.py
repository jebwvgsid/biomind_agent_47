import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import package_manager
from package_manager import PackageManager


class PackageManagerRegistrationTests(unittest.TestCase):
    def make_manager(self, root: str) -> PackageManager:
        package_manager.BIO_SOFTWARE_ROOT = None
        package_manager.BIO_SOURCE_INSTALL_DIR = None
        package_manager.ANACONDA_ROOT = None
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
            self.assertIn('"$CONDA_PREFIX/bin/cooler"', text)
            self.assertIn("could not resolve real executable", text)
            self.assertNotIn('exec cooler "$@"', text)
            self.assertIn('exec "$real_tool" "$@"', text)

    def test_register_existing_tool_rejects_shim_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            shim_dir = Path(tmp) / "agent_shim" / "bin_shims"
            shim_dir.mkdir(parents=True)
            shim_path = shim_dir / "bcftools"
            shim_path.write_text("#!/bin/bash\nexec bcftools \"$@\"\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Refusing to register tool 'bcftools' from shim path"):
                pm.register_existing_tool(
                    tool_name="bcftools",
                    env_name="samtools",
                    source_path=str(shim_path),
                )

    def test_register_existing_tool_rejects_shim_command_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            with self.assertRaisesRegex(ValueError, "Refusing to register shim path as tool command"):
                pm.register_existing_tool(
                    tool_name="/data0/stu_liusiqi/software/agent_shim/bin_shims/bcftools",
                    env_name="samtools",
                    source_path="/data0/stu_liusiqi/anaconda3/envs/samtools/bin",
                )

    def test_manager_repairs_recursive_registered_shim_on_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim_dir = root / "bin_shims"
            shim_dir.mkdir()
            shim = shim_dir / "bcftools"
            shim.write_text(
                "#!/bin/bash\n"
                "# BioMind Shim for bcftools\n"
                "# Env: samtools\n"
                "# 4. Inject Extra Configs (注入 Agent 自动配置的路径)\n"
                "\n"
                "# 5. Execute\n"
                "exec bcftools \"$@\"\n",
                encoding="utf-8",
            )
            (root / "software_registry.json").write_text(
                json.dumps(
                    {
                        "tools": {
                            "bcftools": {
                                "env": "samtools",
                                "env_name": "samtools",
                                "shim_path": str(shim),
                                "source_path": "/data0/stu_liusiqi/anaconda3/envs/samtools/bin",
                            }
                        },
                        "environments": {},
                    }
                ),
                encoding="utf-8",
            )

            pm = self.make_manager(tmp)

            text = shim.read_text(encoding="utf-8")
            self.assertIn("Resolve the real executable explicitly", text)
            self.assertNotIn('exec bcftools "$@"', text)
            self.assertIn('exec "$real_tool" "$@"', text)
            self.assertEqual(pm.registry["tools"]["bcftools"]["shim_version"], "realpath-v2")

    def test_manager_repairs_default_env_shim_mismatch_from_real_executable_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim_dir = root / "bin_shims"
            shim_dir.mkdir()
            shim = shim_dir / "cooler"
            shim.write_text(
                "#!/bin/bash\n"
                "# BioMind Shim for cooler\n"
                "# Env: bio_cooler\n"
                "conda activate bio_cooler\n"
                "# 4. Inject Extra Configs (注入 Agent 自动配置的路径)\n"
                'export PATH="/data0/stu_liusiqi/anaconda3/envs/hicexplore/bin:$PATH"\n'
                "# 5. Execute\n"
                "# Resolve the real executable explicitly. Do not run bare `cooler` here:\n"
                'real_tool_candidates=(\n'
                '    "$CONDA_PREFIX/bin/cooler"\n'
                "    /data0/stu_liusiqi/anaconda3/envs/hicexplore/bin/cooler\n"
                "    /data0/stu_liusiqi/anaconda3/envs/bio_cooler/bin/cooler\n"
                ")\n"
                'exec "$real_tool" "$@"\n',
                encoding="utf-8",
            )
            (root / "software_registry.json").write_text(
                json.dumps(
                    {
                        "tools": {
                            "cooler": {
                                "env": "bio_cooler",
                                "env_name": "bio_cooler",
                                "shim_path": str(shim),
                                "source_path": "/data0/stu_liusiqi/anaconda3/envs/hicexplore/bin",
                            }
                        },
                        "environments": {},
                    }
                ),
                encoding="utf-8",
            )

            pm = self.make_manager(tmp)

            text = shim.read_text(encoding="utf-8")
            self.assertEqual(pm.registry["tools"]["cooler"]["env"], "hicexplore")
            self.assertIn("# Env: hicexplore", text)
            self.assertIn("conda activate hicexplore", text)
            self.assertNotIn("conda activate bio_cooler", text)
            self.assertNotIn('export PATH="/data0/stu_liusiqi/anaconda3/envs/hicexplore/bin', text)

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

    def test_shared_environment_tracks_healthchecks_per_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)
            sommer_check = "conda run -n crop_gp_pro Rscript -e \"library(sommer); library(yaml); cat('OK\\n')\""
            rrblup_check = "conda run -n crop_gp_pro Rscript -e \"library(rrBLUP); cat('OK\\n')\""

            pm.register_environment(
                env_name="crop_gp_pro",
                packages=["sommer", "yaml"],
                skill_id="crop-breeding/crop-gs-sommer-multikernel-gxe",
                language="r",
                healthcheck=sommer_check,
                status="verified",
            )
            pm.register_environment(
                env_name="crop_gp_pro",
                packages=["rrBLUP"],
                skill_id="crop-breeding/crop-gs-rrblup-gblup",
                language="r",
                healthcheck=rrblup_check,
                status="verified",
            )

            record = pm.registry["environments"]["crop_gp_pro"]
            self.assertEqual(record["packages"], ["sommer", "yaml", "rrBLUP"])
            self.assertEqual(
                record["healthchecks_by_skill"]["crop-breeding/crop-gs-sommer-multikernel-gxe"],
                sommer_check,
            )
            self.assertEqual(
                record["healthchecks_by_skill"]["crop-breeding/crop-gs-rrblup-gblup"],
                rrblup_check,
            )

    def test_python_healthcheck_uses_import_module_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.make_manager(tmp)

            command = pm.build_environment_healthcheck(
                env_name="gp_waiter",
                language="python",
                packages=["pytorch", "scikit-learn", "numpy"],
            )

            self.assertIn("import torch, sklearn, numpy", command)
            self.assertNotIn("import pytorch", command)

    def test_configured_anaconda_root_is_used_for_healthchecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conda_bin = root / "anaconda3" / "bin"
            conda_bin.mkdir(parents=True)
            conda_exe = conda_bin / "conda"
            conda_exe.write_text("#!/bin/sh\n", encoding="utf-8")
            package_manager.BIO_SOFTWARE_ROOT = None
            package_manager.BIO_SOURCE_INSTALL_DIR = None
            package_manager.ANACONDA_ROOT = str(root / "anaconda3")

            pm = PackageManager(work_dir=str(root / "work"))
            command = pm._command_with_configured_manager("conda run -n mustache python -m mustache --help")

            self.assertTrue(pm.conda_exe.replace("\\", "/").endswith("anaconda3/bin/conda"))
            self.assertIn("anaconda3/bin/conda run -n mustache", command.replace("\\", "/"))

    def test_mamba_does_not_wrap_readonly_conda_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "anaconda3" / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("conda", "mamba"):
                (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
            pm = self.make_manager(str(root / "work"))
            pm.conda_exe = str(bin_dir / "mamba")

            command = pm._command_with_configured_manager("conda run -n bio_gatk snpEff -version")

            self.assertIn("anaconda3/bin/conda run -n bio_gatk", command.replace("\\", "/"))
            self.assertNotIn("anaconda3/bin/mamba run", command.replace("\\", "/"))

    def test_broken_or_missing_environment_is_not_advertised_or_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pm = self.make_manager(tmp)
            pm.conda_root = root / "anaconda3"
            pm.registry = {
                "tools": {
                    "stale_tool": {"env": "missing_env", "status": "registered"},
                },
                "environments": {
                    "missing_env": {
                        "env": "missing_env",
                        "status": "broken",
                        "packages": ["stale_tool"],
                        "capabilities": ["tool:stale_tool"],
                    }
                },
            }

            self.assertEqual(pm.list_installed_tools(), "None")
            self.assertIsNone(
                pm.find_matching_environment(
                    {"packages": ["stale_tool"], "capabilities": ["tool:stale_tool"]}
                )
            )

    def test_existing_registered_environment_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_dir = root / "anaconda3" / "envs" / "bio_valid"
            env_dir.mkdir(parents=True)
            pm = self.make_manager(tmp)
            pm.conda_root = root / "anaconda3"
            pm.registry = {
                "tools": {"valid_tool": {"env": "bio_valid", "status": "registered"}},
                "environments": {
                    "bio_valid": {
                        "env": "bio_valid",
                        "status": "registered",
                        "packages": ["valid_tool"],
                        "capabilities": ["tool:valid_tool"],
                    }
                },
            }

            self.assertIn("valid_tool", pm.list_installed_tools())
            self.assertEqual(
                pm.find_matching_environment({"packages": ["valid_tool"]})["env"],
                "bio_valid",
            )

    def test_healthcheck_uses_existing_environment_prefix_without_conda_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "anaconda3" / "envs" / "bio_fast" / "bin"
            bin_dir.mkdir(parents=True)
            tool = bin_dir / "fast_tool"
            tool.write_text("#!/bin/sh\nprintf 'FAST_OK\\n'\n", encoding="utf-8")
            tool.chmod(0o755)
            pm = self.make_manager(tmp)
            pm.conda_root = root / "anaconda3"
            pm.registry = {
                "tools": {},
                "environments": {"bio_fast": {"env": "bio_fast", "status": "verified"}},
            }

            ok, detail = pm.verify_environment(
                "bio_fast",
                healthcheck="conda run -n bio_fast fast_tool",
            )

            self.assertTrue(ok, detail)
            self.assertIn("FAST_OK", detail)
            self.assertEqual(pm.registry["environments"]["bio_fast"]["last_healthcheck"], "fast_tool")

            cached = pm.recent_environment_healthcheck(
                "bio_fast",
                "conda run -n bio_fast fast_tool",
            )
            self.assertIn("FAST_OK", cached)

    def test_recent_healthcheck_requires_same_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "anaconda3" / "envs" / "bio_fast" / "bin").mkdir(parents=True)
            pm = self.make_manager(tmp)
            pm.conda_root = root / "anaconda3"
            pm.registry = {
                "tools": {},
                "environments": {
                    "bio_fast": {
                        "env": "bio_fast",
                        "status": "verified",
                        "last_verified_at": datetime.now().isoformat(),
                        "last_healthcheck": "fast_tool --version",
                    }
                },
            }

            self.assertIsNone(
                pm.recent_environment_healthcheck(
                    "bio_fast",
                    "conda run -n bio_fast other_tool --version",
                )
            )

    def test_conda_root_prefers_local_prefix_without_conda_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "conda"
            (root / "bin").mkdir(parents=True)
            (root / "conda-meta").mkdir()
            conda = root / "bin" / "conda"
            conda.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            conda.chmod(0o755)

            pm = PackageManager.__new__(PackageManager)
            pm.conda_exe = str(conda)

            with patch("package_manager.subprocess.run") as run:
                self.assertEqual(pm._get_conda_root(), str(root))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
