import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/3d-dna/tad-boundary-change/scripts/summarize_tad_boundary_changes.py"
)


class TadBoundaryChangeScriptTests(unittest.TestCase):
    def test_script_writes_tables_report_reproduce_and_stage_status(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            leaf = root / "leaf_boundaries.bed"
            bud = root / "bud_boundaries.bed"
            out_dir = root / "boundary_changes"
            leaf.write_text("Chr01\t0\t40000\tleaf_1\nChr01\t100000\t140000\tleaf_2\n", encoding="utf-8")
            bud.write_text("Chr01\t0\t40000\tbud_1\nChr01\t130000\t170000\tbud_2\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--sample",
                    "leaf",
                    str(leaf),
                    "--sample",
                    "bud",
                    str(bud),
                    "--resolution-bp",
                    "40000",
                    "--out-dir",
                    str(out_dir),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            expected = {
                "tad_boundary_sample_stats.tsv",
                "tad_boundary_change_summary.tsv",
                "tad_boundary_matches.tsv",
                "tad_boundary_changes_long.tsv",
                "tad_boundary_change_run_summary.tsv",
                "report.md",
                "reproduce.sh",
                "stage_status.tsv",
            }
            self.assertEqual({path.name for path in out_dir.iterdir()}, expected)
            self.assertIn("completed", (out_dir / "stage_status.tsv").read_text(encoding="utf-8"))
            self.assertIn("TAD Boundary Change Report", (out_dir / "report.md").read_text(encoding="utf-8"))
            reproduce = out_dir / "reproduce.sh"
            self.assertIn(str(SCRIPT), reproduce.read_text(encoding="utf-8"))
            self.assertTrue(reproduce.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
