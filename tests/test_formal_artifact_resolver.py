import tempfile
import unittest
from pathlib import Path

from agent_core.formal_artifact_resolver import (
    format_formal_upstream_context,
    resolve_formal_upstream_artifacts,
)


class FormalArtifactResolverTests(unittest.TestCase):
    def _write_domain(self, root: Path, task_id: str, sample: str, resolution: str = "40000") -> Path:
        path = root / "benchmarks" / "biomind_agent_benchmark" / "runs" / "3d_dna" / task_id
        path = path / "TADs" / "HiCExplorer" / resolution / f"{sample}_domains.bed"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Chr01\t0\t40000\tID_1\n", encoding="utf-8")
        return path

    def _write_boundary(self, root: Path, task_id: str, sample: str, resolution: str = "40000") -> Path:
        path = root / "benchmarks" / "biomind_agent_benchmark" / "runs" / "3d_dna" / task_id
        path = path / "TADs" / "HiCExplorer" / resolution / f"{sample}_boundaries.bed"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Chr01\t38000\t42000\tboundary_1\n", encoding="utf-8")
        return path

    def test_medium_difftad_prefers_same_level_formal_tad_outputs_without_business_question(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            medium_leaf = self._write_domain(root, "BM-3DDNA-TAD-MEDIUM-005", "leaf")
            medium_bud = self._write_domain(root, "BM-3DDNA-TAD-MEDIUM-005", "bud")
            self._write_domain(root, "BM-3DDNA-TAD-STRICT-004", "leaf")
            self._write_domain(root, "BM-3DDNA-TAD-VAGUE-006", "leaf")
            query = """
            benchmark_id: BM-3DDNA-DIFFTAD-MEDIUM-008
            ambiguity_level: medium
            请比较 leaf 和 bud 在 40kb 下的差异 TAD。如果需要上游 TAD domains，请使用同一项目里已经生成的正式 TAD calling 结果。
            - benchmarks/01.test/Matrix/Final/leaf.mcool
            - benchmarks/01.test/Matrix/Final/bud.mcool
            - OPTIONAL_UPSTREAM: TADs/HiCExplorer/40000/*_domains.bed
            """

            result = resolve_formal_upstream_artifacts(query, work_dir=root, task_id="BM-3DDNA-DIFFTAD-MEDIUM-008")
            context = format_formal_upstream_context(result)

        self.assertTrue(result["relevant"])
        self.assertEqual(result["selected_by_sample"]["leaf"]["path"], str(medium_leaf.resolve()))
        self.assertEqual(result["selected_by_sample"]["bud"]["path"], str(medium_bud.resolve()))
        self.assertEqual(result["status"], "resolved")
        self.assertFalse(result["unresolved"])
        self.assertNotIn("target/control", context)
        self.assertIn("[INPUT_RESOLUTION_AUDIT]", context)
        self.assertIn("[FORMAL_UPSTREAM_CANDIDATES]", context)
        self.assertIn(str(medium_leaf.resolve()), context)

    def test_resolver_does_not_evaluate_target_control_direction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            self._write_domain(root, "BM-3DDNA-TAD-STRICT-004", "leaf")
            query = """
            benchmark_id: BM-3DDNA-DIFFTAD-STRICT-007
            请使用 one-way differential TAD:
            target: leaf
            control: bud
            target domains: benchmarks/biomind_agent_benchmark/runs/3d_dna/BM-3DDNA-TAD-STRICT-004/TADs/HiCExplorer/40000/leaf_domains.bed
            """

            result = resolve_formal_upstream_artifacts(query, work_dir=root, task_id="BM-3DDNA-DIFFTAD-STRICT-007")

        self.assertNotIn("target_control_explicit", result)
        self.assertFalse(result["unresolved"])
        self.assertEqual(result["status"], "resolved")

    def test_multiple_same_priority_candidates_require_user_choice(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            self._write_domain(root, "BM-3DDNA-TAD-MEDIUM-005", "leaf")
            self._write_domain(root, "BM-3DDNA-TAD-MEDIUM-099", "leaf")
            query = """
            benchmark_id: BM-3DDNA-DIFFTAD-MEDIUM-008
            ambiguity_level: medium
            differential TAD needs leaf domains at 40kb
            - leaf.mcool
            """

            result = resolve_formal_upstream_artifacts(query, work_dir=root)

        self.assertTrue(result["unresolved"])
        self.assertEqual(result["status"], "ambiguous_candidates")
        self.assertIn("leaf", result["ambiguous_samples"])
        self.assertEqual(result.get("question", ""), "")

    def test_target_control_unclear_text_stays_consultant_skill_responsibility(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            self._write_domain(root, "BM-3DDNA-TAD-MEDIUM-005", "leaf")
            self._write_domain(root, "BM-3DDNA-TAD-MEDIUM-005", "bud")
            query = """
            benchmark_id: BM-3DDNA-DIFFTAD-MEDIUM-008
            ambiguity_level: medium
            differential TAD leaf.mcool bud.mcool 40kb TAD domains
            禁止假设 leaf 一定是 target；target/control 不明确时需要确认。
            """

            result = resolve_formal_upstream_artifacts(query, work_dir=root)

        self.assertNotIn("target_control_explicit", result)
        self.assertEqual(result["status"], "resolved")
        self.assertFalse(result["unresolved"])
        self.assertEqual(result.get("question", ""), "")

    def test_vague_leaf_bud_directory_prompt_finds_domains_without_direction_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            self._write_domain(root, "BM-3DDNA-TAD-VAGUE-006", "leaf")
            self._write_domain(root, "BM-3DDNA-TAD-VAGUE-006", "bud")
            query = """
            benchmark_id: BM-3DDNA-DIFFTAD-VAGUE-009
            ambiguity_level: vague
            我想看看 leaf 和 bud 的 TAD 有没有明显变化。数据在 benchmarks/01.test/03.3d/00.data。
            OPTIONAL_UPSTREAM: benchmarks/biomind_agent_benchmark/runs/3d_dna/*/TADs/HiCExplorer/40000/*_domains.bed
            """

            result = resolve_formal_upstream_artifacts(query, work_dir=root)

        self.assertEqual(result["selected_by_sample"]["leaf"]["task_id"], "BM-3DDNA-TAD-VAGUE-006")
        self.assertEqual(result["selected_by_sample"]["bud"]["task_id"], "BM-3DDNA-TAD-VAGUE-006")
        self.assertEqual(result["samples"], ["leaf", "bud"])
        self.assertEqual(result["status"], "resolved")
        self.assertFalse(result["unresolved"])
        self.assertEqual(result.get("question", ""), "")

    def test_forbidden_fixture_domains_are_excluded(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            fixture = root / "benchmarks" / "biomind_agent_benchmark" / "runs" / "3d_dna"
            fixture = fixture / "fixtures_legacy_toy" / "TADs" / "HiCExplorer" / "40000" / "leaf_domains.bed"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("Chr01\t0\t40000\ttoy\n", encoding="utf-8")
            query = "BM-3DDNA-DIFFTAD-MEDIUM-008 differential TAD leaf.mcool 40kb TAD domains"

            result = resolve_formal_upstream_artifacts(query, work_dir=root)

        self.assertEqual(result["formal_candidates"], [])
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["status"], "missing_candidates")
        self.assertIn("forbidden path token", result["excluded_candidates"][0]["reason"])

    def test_boundary_task_uses_boundary_artifacts_not_difftad_direction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            leaf = self._write_boundary(root, "BM-3DDNA-TAD-STRICT-004", "leaf")
            bud = self._write_boundary(root, "BM-3DDNA-TAD-STRICT-004", "bud")
            query = f"""
            benchmark_id: BM-3DDNA-BOUNDARY-STRICT-010
            Resume context mentions differential TAD domains and SessionArchive Wiki events.
            Please compare leaf and bud TAD boundaries using:
            - {leaf}
            - {bud}
            """

            result = resolve_formal_upstream_artifacts(
                query,
                work_dir=root,
                task_id="BM-3DDNA-BOUNDARY-STRICT-010",
            )

        self.assertTrue(result["relevant"])
        self.assertEqual(result["artifact_kind"], "tad_boundaries")
        self.assertEqual(result["status"], "resolved")
        self.assertFalse(result["unresolved"])
        self.assertEqual(result.get("question", ""), "")
        self.assertEqual(set(result["selected_by_sample"]), {"leaf", "bud"})

    def test_boundary_vague_multiple_resolutions_require_user_choice(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            self._write_boundary(root, "BM-3DDNA-TAD-VAGUE-006", "leaf", "40000")
            self._write_boundary(root, "BM-3DDNA-TAD-VAGUE-006", "bud", "40000")
            self._write_boundary(root, "BM-3DDNA-BALANCE-VAGUE-003", "leaf", "10000")
            self._write_boundary(root, "BM-3DDNA-BALANCE-VAGUE-003", "bud", "10000")
            query = """
            benchmark_id: BM-3DDNA-BOUNDARY-VAGUE-012
            ambiguity_level: vague
            探索 leaf 和 bud 是否存在 TAD boundary shifts。数据目录已给出，但没有确认 resolution。
            """

            result = resolve_formal_upstream_artifacts(query, work_dir=root)

        self.assertEqual(result["artifact_kind"], "tad_boundaries")
        self.assertEqual(result["status"], "ambiguous_resolution")
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["ambiguous_resolutions"], ["10000", "40000"])
        self.assertEqual(result.get("question", ""), "")


if __name__ == "__main__":
    unittest.main()
