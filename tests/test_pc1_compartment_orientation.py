import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills" / "3d-dna" / "compartment-calling" / "scripts" / "call_ab_compartments_pc1.py"
SPEC = importlib.util.spec_from_file_location("call_ab_compartments_pc1", SCRIPT_PATH)
pc1 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pc1)


class PC1CompartmentOrientationTests(unittest.TestCase):
    def test_invalid_pc1_stays_na_in_outputs_and_is_counted_separately(self):
        # Only the matrix reader/eigendecomposition are stubbed; exercise the
        # wrapper's real classification, coordinate validation and file writers.
        scores = [2.0, -2.0, 0.0, float("nan"), float("inf"), -float("inf"), None, pd.NA]
        eigvecs = pd.DataFrame({
            "chrom": ["Chr01"] * len(scores),
            "start": [i * 40000 for i in range(len(scores))],
            "end": [(i + 1) * 40000 for i in range(len(scores))],
            "E1": pd.Series(scores, dtype=object),
        })
        expected_labels = ["A", "B", "A", "NA", "NA", "NA", "NA", "NA"]
        clr = SimpleNamespace(chromnames=["Chr01"], chromsizes={"Chr01": 320000})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix = root / "input.cool"
            matrix.touch()
            track = root / "gene_density.bed"
            # Deliberately reorder the feature track. High density in invalid
            # bins must not contaminate the B mean or cause a false failure.
            track.write_text("".join(
                f"Chr01\t{i * 40000}\t{(i + 1) * 40000}\t{value}\n"
                for i, value in reversed(list(enumerate([5, 1, 5, 1000, 1000, 1000, 1000, 1000])))
            ))
            argv = ["call_ab_compartments_pc1.py", "--sample", "leaf", str(matrix),
                    "--resolution-bp", "40000", "--phasing-track", str(track),
                    "--out-root", str(root / "out")]
            with patch.dict(sys.modules, {"cooler": SimpleNamespace(Cooler=lambda uri: clr)}), \
                 patch.object(pc1, "run_eigs_cis", return_value=(None, eigvecs)), \
                 patch.object(sys, "argv", argv):
                pc1.main()

            def read_tsv(path):
                with path.open() as handle:
                    return list(csv.DictReader(handle, delimiter="\t"))

            out = root / "out" / "40000"
            for path in [out / "leaf" / "eigenvectors.tsv", out / "compartments_long.tsv"]:
                rows = read_tsv(path)
                self.assertEqual([r["compartment"] for r in rows], expected_labels)
                self.assertEqual([int(r["start"]) for r in rows], eigvecs["start"].tolist())
                self.assertEqual([float(r["score"]) for r in rows[:3]], scores[:3])
            bed = [line.split("\t") for line in (out / "leaf" / "ab_compartments.bed").read_text().splitlines()]
            self.assertEqual([r[3] for r in bed], expected_labels)
            self.assertEqual([int(r[2]) for r in bed], eigvecs["end"].tolist())
            lengths = {r["compartment"]: (int(r["count"]), int(r["total_length"]))
                       for r in read_tsv(out / "compartment_length_summary.tsv")}
            self.assertEqual(lengths, {"A": (2, 80000), "B": (1, 40000), "NA": (5, 200000)})
            orientation = {r["compartment"]: float(r["mean_phasing_value"])
                           for r in read_tsv(out / "leaf" / "orientation_validation.tsv")}
            self.assertEqual(orientation, {"A": 5.0, "B": 1.0, "NA": 1000.0})

    def test_all_invalid_pc1_produces_only_na_bins(self):
        eigvecs = pd.DataFrame({
            "chrom": ["Chr01", "Chr01"],
            "start": [0, 40000],
            "end": [40000, 60000],
            "E1": [float("nan"), float("nan")],
        })
        clr = SimpleNamespace(chromnames=["Chr01"], chromsizes={"Chr01": 60000})
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(chromosome=None, phasing_track=None, n_eigs=1, out_root=tmp)
            with patch.dict(sys.modules, {"cooler": SimpleNamespace(Cooler=lambda uri: clr)}), \
                 patch.object(pc1, "run_eigs_cis", return_value=(None, eigvecs)):
                rows = pc1.call_sample("leaf", "input.cool", 40000, args)

            self.assertEqual([r["compartment"] for r in rows], ["NA", "NA"])
            self.assertEqual(pc1.summarize_lengths(rows, 40000), [{
                "sample": "leaf", "resolution_bp": 40000, "compartment": "NA",
                "count": 2, "total_length": 60000,
            }])

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
