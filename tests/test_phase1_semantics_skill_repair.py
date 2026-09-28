from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_core.formal_artifact_resolver import resolve_formal_upstream_artifacts
from agent_core.skill_manager import SkillManager
from memory.task_semantics import infer_task_semantics


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "snp_annotation_original_prompt.txt"
FIXTURE_SHA256 = "7fba598afad158dba0975b77102e4fe7c14813e740ba198e36146e14a063b08f"


class PhaseOneTaskSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompt_bytes = FIXTURE.read_bytes()
        cls.prompt = cls.prompt_bytes.decode("utf-8")

    def test_original_prompt_fixture_is_byte_exact(self):
        self.assertEqual(len(self.prompt), 1294)
        self.assertEqual(len(self.prompt.splitlines()), 32)
        self.assertFalse(self.prompt.endswith("\n"))
        self.assertEqual(hashlib.sha256(self.prompt_bytes).hexdigest(), FIXTURE_SHA256)

    def test_status_output_is_not_inferred_as_sample(self):
        semantics = infer_task_semantics(
            "session_phase1",
            messages=[self.prompt],
            payload={"user_query": self.prompt},
        )

        self.assertNotIn("stage_status", semantics.get("samples", []))
        self.assertNotEqual(semantics.get("analysis_type"), "tad_boundary_change")
        self.assertNotIn("tad_boundary_change", semantics.get("task_title", ""))

    def test_explicit_and_input_read_samples_are_preserved(self):
        prompt = """sample_id: WT_01
输入文件：
- sample01.bam
- tumor_sample.cram
- WT_01_R1.fastq.gz
"""
        semantics = infer_task_semantics("session_samples", messages=[prompt])

        self.assertIn("WT_01", semantics.get("samples", []))
        self.assertIn("sample01", semantics.get("samples", []))
        self.assertIn("tumor_sample", semantics.get("samples", []))

    def test_vcf_basename_is_not_assumed_to_be_sample(self):
        semantics = infer_task_semantics(
            "session_cohort_vcf",
            messages=["输入文件：cohort.vcf；这是一个可能包含多个样本的 VCF。"],
        )

        self.assertNotIn("cohort", semantics.get("samples", []))

    def test_generic_boundary_constraint_is_not_tad_change(self):
        semantics = infer_task_semantics(
            "session_boundary_constraint",
            messages=["边界条件/禁止：不得读取 benchmark/hidden/fixture。"],
        )
        self.assertNotEqual(semantics.get("analysis_type"), "tad_boundary_change")

    def test_hic_loop_with_generic_boundary_is_not_tad_change(self):
        semantics = infer_task_semantics(
            "session_hic_loop",
            messages=["对 Hi-C 数据进行 loop calling。边界条件：不得读取 benchmark/hidden/fixture。"],
        )
        self.assertEqual(semantics.get("analysis_type"), "loop_calling")

    def test_hic_contact_matrix_is_not_tad_change(self):
        semantics = infer_task_semantics(
            "session_hic_matrix",
            messages=["使用 Hi-C 数据构建 contact matrix。"],
        )
        self.assertNotEqual(semantics.get("analysis_type"), "tad_boundary_change")

    def test_tad_calling_is_not_boundary_change(self):
        semantics = infer_task_semantics(
            "session_tad_calling",
            messages=["调用 TAD 并输出 domain 坐标。"],
        )
        self.assertNotEqual(semantics.get("analysis_type"), "tad_boundary_change")

    def test_true_tad_boundary_change_is_preserved(self):
        semantics = infer_task_semantics(
            "session_tad_change",
            messages=["比较处理组和对照组 Hi-C 数据中的 TAD 边界变化。"],
        )
        self.assertEqual(semantics.get("analysis_type"), "tad_boundary_change")


class PhaseOneFormalArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompt = FIXTURE.read_text(encoding="utf-8")

    def _resolve(self, query: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            return resolve_formal_upstream_artifacts(
                query,
                work_dir=tmpdir,
                project_root=tmpdir,
            )

    def test_snp_prompt_does_not_request_tad_boundaries(self):
        result = self._resolve(self.prompt)
        self.assertNotEqual(result.get("artifact_kind"), "tad_boundaries")

    def test_hic_loop_generic_boundary_does_not_request_tad_boundaries(self):
        result = self._resolve(
            "对 Hi-C 数据进行 loop calling。边界条件：不要读取 hidden fixture。"
        )
        self.assertNotEqual(result.get("artifact_kind"), "tad_boundaries")

    def test_explicit_tad_boundary_artifact_is_preserved(self):
        result = self._resolve(
            "比较处理组和对照组 Hi-C 中的 TAD boundary changes，输出 boundary coordinates。"
        )
        self.assertEqual(result.get("artifact_kind"), "tad_boundaries")


class PhaseOneSkillRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = SkillManager(str(REPO_ROOT / "skills"))
        cls.candidates = [
            "genomics/reseq-variant-workflow",
            "3d-dna/tad-calling",
            "scrna/annotate/basic",
            "genomics/long-read-sv-sniffles2",
        ]

    def test_declared_tool_match_retrieves_snpeff_as_candidate(self):
        prompt = FIXTURE.read_text(encoding="utf-8")
        candidates = self.manager.candidate_skill_ids_for_prompt(
            prompt,
            max_results=self.manager.MAX_SELECTED_SKILLS,
        )

        self.assertIn("genomics/snpeff-annotation", candidates)


    def test_valid_selection_survives_an_invalid_peer(self):
        result = self.manager.repair_selected_skill_ids(
            ["genomics/reseq-variant-workflow", "missing/not-real"],
            self.candidates,
        )
        self.assertEqual(result["valid_selected"], ["genomics/reseq-variant-workflow"])
        self.assertEqual(result["selected"], ["genomics/reseq-variant-workflow"])
        self.assertEqual(result["unresolved_invalid_ids"], ["missing/not-real"])

    def test_normalization_typo_repairs_to_one_allowlisted_candidate(self):
        result = self.manager.repair_selected_skill_ids(
            ["genomics/reseq_variant_workflow"],
            self.candidates,
        )
        self.assertEqual(result["repaired_ids"], ["genomics/reseq-variant-workflow"])
        self.assertEqual(result["selected"], ["genomics/reseq-variant-workflow"])

    def test_semantic_hallucination_cannot_bulk_promote_candidates(self):
        result = self.manager.repair_selected_skill_ids(
            ["genomics/reference-guided-variant-annotation"],
            self.candidates,
        )
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["repaired_ids"], [])
        self.assertEqual(
            result["unresolved_invalid_ids"],
            ["genomics/reference-guided-variant-annotation"],
        )

    def test_registry_skill_outside_original_candidates_is_rejected(self):
        result = self.manager.repair_selected_skill_ids(
            ["genomics/snpeff-annotation"],
            self.candidates,
        )
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["unresolved_invalid_ids"], ["genomics/snpeff-annotation"])

    def test_one_invalid_id_adds_at_most_one_replacement(self):
        result = self.manager.repair_selected_skill_ids(
            ["genomics/reseq_variant_workflow"],
            self.candidates,
        )
        self.assertLessEqual(len(result["repaired_ids"]), 1)
        self.assertTrue(set(result["selected"]).issubset(set(self.candidates)))



if __name__ == "__main__":
    unittest.main()
