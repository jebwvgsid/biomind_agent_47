import unittest
import tempfile
from pathlib import Path

import package_manager
from agent_core.skill_manager import SkillManager
from package_manager import PackageManager


class SkillManagerSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        cls.manager = SkillManager(str(repo_root / "skills"))

    def test_parse_selected_skill_ids_cleans_quotes_and_commas(self):
        text = (
            "FINISH\n"
            "[SELECTED_SKILLS: `scrna/ingest/data-download`, \"scrna/qc/filter\"]"
        )
        self.assertEqual(
            self.manager.parse_selected_skill_ids(text),
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )

    def test_parse_selected_skill_ids_supports_chinese_commas_and_semicolons(self):
        text = "[SELECTED_SKILLS: (scrna/ingest/data-download)，- scrna/qc/filter；None]"
        self.assertEqual(
            self.manager.parse_selected_skill_ids(text),
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )

    def test_parse_selected_skill_ids_supports_connectors(self):
        english = "[SELECTED_SKILLS: `scrna/ingest/data-download` and (scrna/qc/filter)]"
        chinese = "[SELECTED_SKILLS: scrna/ingest/data-download 和 scrna/qc/filter]"
        expected = ["scrna/ingest/data-download", "scrna/qc/filter"]
        self.assertEqual(self.manager.parse_selected_skill_ids(english), expected)
        self.assertEqual(self.manager.parse_selected_skill_ids(chinese), expected)

    def test_parse_selected_skill_ids_keeps_miswritten_skill_name_for_validation(self):
        text = "[SELECTED_SKILLS: - `scRNA QC Filter`]"
        self.assertEqual(
            self.manager.parse_selected_skill_ids(text),
            ["scRNA QC Filter"],
        )
        self.assertIn(
            "Unknown skill id: scRNA QC Filter",
            self.manager.validate_selected_skills(["scRNA QC Filter"]),
        )

    def test_validate_scrna_canonical_skill_ids(self):
        selected = ["scrna/ingest/data-download", "scrna/qc/filter"]
        self.assertEqual(self.manager.validate_selected_skills(selected, max_results=3), [])
        self.assertEqual(
            self.manager.normalize_selected_skills(selected, max_results=3),
            selected,
        )

    def test_catalog_exposes_workflow_matching_metadata(self):
        catalog = self.manager.get_skill_catalog_prompt(["scrna/qc/filter"])

        self.assertIn("task_types: single_cell_rnaseq", catalog)
        self.assertIn("stage_tags: qc_filter", catalog)
        self.assertIn("inputs:", catalog)
        self.assertIn("outputs:", catalog)

    def test_extract_skills_prefers_active_skills_block(self):
        plan = (
            "[SELECTED_SKILLS: scrna/preprocess/basic]\n"
            "[ACTIVE_SKILLS]\n"
            "scrna/ingest/data-download\n"
            "scrna/qc/filter\n"
            "[/ACTIVE_SKILLS]\n"
        )

        self.assertEqual(
            self.manager.extract_skills_from_plan(plan),
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )
        self.assertEqual(
            self.manager.extract_future_skills_from_plan(
                "[FUTURE_SKILLS]\nscrna/preprocess/basic\n[/FUTURE_SKILLS]"
            ),
            ["scrna/preprocess/basic"],
        )

    def test_stage_scoring_uses_structured_metadata(self):
        stage = {
            "stage": "qc_filter",
            "inputs": ["raw_seurat_object"],
            "outputs": ["qc_filtered_seurat_object"],
        }

        score = self.manager.score_skill_for_stage(
            "scrna/qc/filter",
            stage,
            workflow_task_types=["single_cell_rnaseq"],
        )

        self.assertEqual(score["role"], "required")
        self.assertGreaterEqual(score["score"], 70)
        self.assertIn("stage_tags match", score["reasons"])

    def test_stage_scoring_forbids_when_not_to_use_hits(self):
        stage = {
            "stage": "qc_filter",
            "description": "Bulk RNA-seq count table QC",
        }

        score = self.manager.score_skill_for_stage(
            "scrna/qc/filter",
            stage,
            workflow_task_types=["bulk_rnaseq"],
        )

        self.assertTrue(score["forbidden"])
        self.assertEqual(score["role"], "forbidden")

    def test_validate_scrna_v2_canonical_skill_ids(self):
        selected = [
            "scrna/preprocess/basic",
            "scrna/cluster/basic",
            "scrna/markers/find",
            "scrna/annotate/basic",
            "scrna/de/contrast",
        ]
        self.assertEqual(self.manager.validate_selected_skills(selected, max_results=10), [])
        self.assertEqual(
            self.manager.normalize_selected_skills(selected, max_results=10),
            selected,
        )

    def test_match_scrna_preprocess_skill(self):
        matched = self.manager.match_skills("请继续做 scRNA 标准化 preprocess 和高变基因筛选", max_results=3)
        self.assertIn("scrna/preprocess/basic", matched)

    def test_match_scrna_cluster_skill(self):
        matched = self.manager.match_skills("继续做 PCA UMAP clustering 聚类分析", max_results=3)
        self.assertIn("scrna/cluster/basic", matched)

    def test_match_scrna_marker_skill(self):
        matched = self.manager.match_skills("找 cluster markers 和 marker 基因", max_results=3)
        self.assertIn("scrna/markers/find", matched)

    def test_match_scrna_annotation_skill(self):
        matched = self.manager.match_skills("做单细胞细胞类型注释 annotation 或 SingleR", max_results=3)
        self.assertIn("scrna/annotate/basic", matched)

    def test_match_scrna_de_skill(self):
        matched = self.manager.match_skills("做 ctrl vs stim 的 differential expression 差异表达", max_results=3)
        self.assertIn("scrna/de/contrast", matched)

    def test_match_tad_boundary_change_skill(self):
        matched = self.manager.match_skills("请做TAD边界改变统计，比较shared和lost boundaries", max_results=3)
        self.assertEqual(matched[0], "genomics/chromatin/tad-boundary-change")


class SkillRuntimeRegistryTests(unittest.TestCase):
    def make_manager(self, root: Path) -> SkillManager:
        skill_dir = root / "skills" / "genomics" / "chromatin" / "tad-calling"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: tad-analysis
description: TAD calling
runtime:
  default_env: hicexplore
  env_runner: conda run -n hicexplore python
  language: python
  packages: [hicexplorer, cooler]
  capabilities: [tad-calling]
  healthcheck: python -c "print('OK')"
---
# TAD
""",
            encoding="utf-8",
        )
        return SkillManager(str(root / "skills"))

    def test_external_skill_registry_import_syncs_package_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)
            package_manager.BIO_SOFTWARE_ROOT = None
            package_manager.BIO_SOURCE_INSTALL_DIR = None
            pm = PackageManager(work_dir=str(root / "software"))

            manager.merge_runtime_registry_data(
                {
                    "skills": {
                        "genomics/chromatin/tad-calling": {
                            "env": "hicexplore",
                            "env_runner": "conda run -n hicexplore python",
                            "status": "verified",
                            "healthcheck": "python -c \"print('OK')\"",
                            "packages": ["hicexplorer", "cooler", "matplotlib"],
                        }
                    }
                },
                package_manager=pm,
            )

            self.assertEqual(manager.get_runtime_env("genomics/chromatin/tad-calling"), "hicexplore")
            self.assertEqual(pm.registry["environments"]["hicexplore"]["status"], "verified")
            self.assertIn("genomics/chromatin/tad-calling", pm.registry["environments"]["hicexplore"]["skill_ids"])

    def test_software_registry_environment_syncs_back_to_skill_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)
            package_manager.BIO_SOFTWARE_ROOT = None
            package_manager.BIO_SOURCE_INSTALL_DIR = None
            pm = PackageManager(work_dir=str(root / "software"))

            manager.merge_runtime_registry_data(
                {
                    "environments": {
                        "hicexplore": {
                            "status": "verified",
                            "language": "python",
                            "packages": ["hicexplorer", "cooler"],
                            "capabilities": ["tad-calling"],
                            "healthcheck": "python -c \"print('OK')\"",
                            "skill_ids": ["genomics/chromatin/tad-calling"],
                        }
                    }
                },
                package_manager=pm,
            )

            record = manager.get_runtime_record("genomics/chromatin/tad-calling")
            self.assertEqual(record["env"], "hicexplore")
            self.assertEqual(record["status"], "verified")
            self.assertEqual(record["healthcheck"], "python -c \"print('OK')\"")


if __name__ == "__main__":
    unittest.main()
