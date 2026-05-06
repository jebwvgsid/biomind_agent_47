import unittest
from pathlib import Path

from agent_core.skill_manager import SkillManager


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


if __name__ == "__main__":
    unittest.main()
