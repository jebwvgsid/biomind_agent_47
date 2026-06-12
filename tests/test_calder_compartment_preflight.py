import argparse
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills" / "3d-dna" / "calder2-subcompartment" / "scripts" / "run_calder_compartments.py"
SPEC = importlib.util.spec_from_file_location("run_calder_compartments", SCRIPT_PATH)
calder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(calder)


class _FakeIndex(list):
    def astype(self, _type):
        return [str(item) for item in self]


class _FakeChromsizes:
    index = _FakeIndex(["Chr01"])

    def astype(self, _type):
        return [80000]


class _FakeCoolerObject:
    chromnames = ["Chr01"]
    chromsizes = _FakeChromsizes()


class CalderFeatureTrackPreflightTests(unittest.TestCase):
    def test_feature_track_rejects_zero_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            feature = Path(tmp) / "gene_density.bed"
            feature.write_text("Chr01\t0\t40000\t0\n", encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "column 4 sum is 0"):
                calder.validate_feature_track_basic(feature)

    def test_feature_track_requires_one_row_per_cooler_bin(self):
        old_try_import = calder.try_import_cooler
        calder.try_import_cooler = lambda: SimpleNamespace(Cooler=lambda _uri: _FakeCoolerObject())
        args = argparse.Namespace(chromosome=None, exclude_chromosome=[])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                feature = Path(tmp) / "gene_density.bed"
                feature.write_text("Chr01\t0\t40000\t1\n", encoding="utf-8")

                with self.assertRaisesRegex(SystemExit, "not one binned row per cooler bin"):
                    calder.validate_feature_track_for_cooler(str(feature), "leaf.mcool", "leaf", 40000, args)

                feature.write_text("Chr01\t0\t40000\t1\nChr01\t40000\t80000\t2\n", encoding="utf-8")
                calder.validate_feature_track_for_cooler(str(feature), "leaf.mcool", "leaf", 40000, args)
        finally:
            calder.try_import_cooler = old_try_import


if __name__ == "__main__":
    unittest.main()
