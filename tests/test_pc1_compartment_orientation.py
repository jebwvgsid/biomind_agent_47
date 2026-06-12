import importlib.util
import tempfile
import unittest
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills" / "3d-dna" / "compartment-calling" / "scripts" / "call_ab_compartments_pc1.py"
SPEC = importlib.util.spec_from_file_location("call_ab_compartments_pc1", SCRIPT_PATH)
pc1 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pc1)


class PC1CompartmentOrientationTests(unittest.TestCase):
    def test_orientation_validation_uses_coordinate_merge_and_passes_when_a_is_high(self):
        eigvecs = pd.DataFrame(
            [
                {"chrom": "Chr01", "start": 0, "end": 40000, "compartment": "A"},
                {"chrom": "Chr01", "start": 40000, "end": 80000, "compartment": "B"},
            ]
        )
        track = pd.DataFrame(
            [
                {"chrom": "Chr01", "start": 0, "end": 40000, "value": 5},
                {"chrom": "Chr01", "start": 40000, "end": 80000, "value": 1},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            rows = pc1.validate_orientation_by_coordinate(eigvecs, track, "leaf", Path(tmp))

            means = {row["compartment"]: row["mean_phasing_value"] for row in rows}
            self.assertGreater(means["A"], means["B"])
            self.assertTrue((Path(tmp) / "orientation_validation.tsv").is_file())

    def test_orientation_validation_fails_when_b_has_higher_gene_density(self):
        eigvecs = pd.DataFrame(
            [
                {"chrom": "Chr01", "start": 0, "end": 40000, "compartment": "A"},
                {"chrom": "Chr01", "start": 40000, "end": 80000, "compartment": "B"},
            ]
        )
        track = pd.DataFrame(
            [
                {"chrom": "Chr01", "start": 0, "end": 40000, "value": 1},
                {"chrom": "Chr01", "start": 40000, "end": 80000, "value": 5},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "A/B orientation failed"):
                pc1.validate_orientation_by_coordinate(eigvecs, track, "leaf", Path(tmp))


if __name__ == "__main__":
    unittest.main()
