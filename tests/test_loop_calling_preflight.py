import importlib.util
import math
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills" / "3d-dna" / "loop-calling" / "scripts" / "run_mustache_loops.py"
SPEC = importlib.util.spec_from_file_location("run_mustache_loops", SCRIPT_PATH)
loop_calling = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(loop_calling)


class _FakeBins:
    def __init__(self, columns, weights):
        self.columns = columns
        self._weights = weights

    def __getitem__(self, key):
        if key != "weight":
            raise KeyError(key)
        return self

    def __getslice__(self, *_args):
        return self._weights

    def __getitem__(self, item):  # noqa: F811
        if item == "weight":
            return self
        if isinstance(item, slice):
            return self._weights
        raise KeyError(item)


class _FakeCoolerObject:
    def __init__(self, columns, weights):
        self._bins = _FakeBins(columns, weights)

    def bins(self):
        return self._bins


class LoopCallingPreflightTests(unittest.TestCase):
    def test_balance_flags_work_without_python39_argparse(self):
        with patch.object(loop_calling.argparse, "BooleanOptionalAction", None, create=True):
            for flags, expected in [([], True), (["--no-auto-balance"], False),
                                    (["--no-auto-balance", "--auto-balance"], True)]:
                with patch.object(sys, "argv", [str(SCRIPT_PATH), *flags]):
                    self.assertEqual(loop_calling.parse_args().auto_balance, expected)

    def test_native_mcool_argument_keeps_resolution_in_r_flag(self):
        self.assertEqual(loop_calling.mustache_matrix_path(
            "中文 path/leaf.mcool::/resolutions/10000", 10000), "中文 path/leaf.mcool")
        with self.assertRaisesRegex(SystemExit, "does not match resolution"):
            loop_calling.mustache_matrix_path("leaf.mcool::/resolutions/5000", 10000)

    def test_source_entrypoints_use_same_python_and_preserve_arguments(self):
        with tempfile.TemporaryDirectory(prefix="mustache source 空格 ") as tmpdir:
            root = Path(tmpdir)
            script = root / "mustache.py"
            script.write_text("import sys\nassert '--help' in sys.argv\n")
            diff = root / "diff_mustache.py"
            diff.write_text(script.read_text())
            result = subprocess.run([sys.executable, str(SCRIPT_PATH),
                "--mustache-script", str(script), "--diff", "leaf", "bud",
                "--runtime-check-only"], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = [shlex.split(line[len("[RUNTIME OK] "):])
                        for line in result.stdout.splitlines()]
            self.assertEqual(commands, [[sys.executable, str(script), "--help"],
                                        [sys.executable, str(diff), "--help"]])
            self.assertFalse((root / "Loops").exists())

    def test_runtime_failure_stops_before_matrices_or_output_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = root / "mustache.py"
            script.write_text("import sys\nprint('missing scientific dependency')\nsys.exit(7)\n")
            result = subprocess.run([sys.executable, str(SCRIPT_PATH),
                "--mustache-script", str(script), "--sample", "leaf", "absent.mcool",
                "--resolution-bp", "10000"], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime check failed (exit 7)", result.stderr)
            self.assertIn("missing scientific dependency", result.stderr)
            self.assertFalse((root / "Loops").exists())

    def test_source_call_and_diff_commands_use_native_paths(self):
        with tempfile.TemporaryDirectory(prefix="loop commands ") as tmpdir:
            root = Path(tmpdir)
            script = root / "mustache.py"; script.touch()
            (root / "diff_mustache.py").touch()
            with patch.object(sys, "argv", [str(SCRIPT_PATH), "--mustache-script", str(script),
                "--sample", "leaf", "leaf space.mcool", "--sample", "bud", "bud.mcool",
                "--resolution-bp", "10000", "--diff", "leaf", "bud", "--dry-run"]):
                args = loop_calling.parse_args()
            samples = loop_calling.load_samples(args)
            with patch.object(loop_calling, "check_cooler_balance", return_value=(True, "finite weights")):
                rows = loop_calling.call_loops(args, samples, root)
                diff_rows = loop_calling.call_differential(args, samples, loop_calling.load_pairs(args), root)
            call = shlex.split(rows[0]["command"])
            pair = shlex.split(diff_rows[0]["command"])
            self.assertEqual(call[:2], [sys.executable, str(script)])
            self.assertEqual(call[call.index("-f") + 1], "leaf space.mcool")
            self.assertEqual(rows[0]["matrix_uri"], "leaf space.mcool::/resolutions/10000")
            self.assertEqual(pair[pair.index("-f1") + 1], "leaf space.mcool")
            self.assertEqual(pair[pair.index("-f2") + 1], "bud.mcool")

    def test_mcool_path_expands_to_resolution_uri(self):
        self.assertEqual(
            loop_calling.effective_matrix_uri("leaf.mcool", 10000),
            "leaf.mcool::/resolutions/10000",
        )
        self.assertEqual(
            loop_calling.effective_matrix_uri("leaf.mcool::/resolutions/5000", 10000),
            "leaf.mcool::/resolutions/5000",
        )

    def test_balance_check_requires_finite_weight_column(self):
        old_try_import = loop_calling.try_import_cooler
        loop_calling.try_import_cooler = lambda: SimpleNamespace(
            Cooler=lambda _uri: _FakeCoolerObject(["chrom", "start", "end", "weight"], [math.nan, 0.5, 1.0])
        )
        try:
            ok, message = loop_calling.check_cooler_balance("leaf.mcool::/resolutions/10000", "weight")
            self.assertTrue(ok)
            self.assertIn("finite bins: 2/3", message)
        finally:
            loop_calling.try_import_cooler = old_try_import

    def test_balance_check_rejects_missing_weight_column(self):
        old_try_import = loop_calling.try_import_cooler
        loop_calling.try_import_cooler = lambda: SimpleNamespace(
            Cooler=lambda _uri: _FakeCoolerObject(["chrom", "start", "end"], [])
        )
        try:
            ok, message = loop_calling.check_cooler_balance("leaf.mcool::/resolutions/10000", "weight")
            self.assertFalse(ok)
            self.assertIn("weight column 'weight' not found", message)
        finally:
            loop_calling.try_import_cooler = old_try_import

    def test_preflight_samples_reports_all_samples_without_balancing(self):
        def fake_cooler(uri):
            if "leaf.mcool" in uri:
                return _FakeCoolerObject(["chrom", "start", "end", "weight"], [0.4, 0.8])
            return _FakeCoolerObject(["chrom", "start", "end"], [])

        old_try_import = loop_calling.try_import_cooler
        loop_calling.try_import_cooler = lambda: SimpleNamespace(Cooler=fake_cooler)
        args = SimpleNamespace(resolution_bp=10000, skip_balance_check=False, weight_name="weight")
        samples = {
            "leaf": {"sample": "leaf", "matrix": "leaf.mcool"},
            "bud": {"sample": "bud", "matrix": "bud.mcool"},
        }
        try:
            rows = loop_calling.preflight_samples(args, samples)
        finally:
            loop_calling.try_import_cooler = old_try_import

        self.assertEqual([row["sample"] for row in rows], ["leaf", "bud"])
        self.assertEqual(rows[0]["balanced"], "yes")
        self.assertEqual(rows[0]["matrix_uri"], "leaf.mcool::/resolutions/10000")
        self.assertEqual(rows[1]["balanced"], "no")
        self.assertIn("weight column 'weight' not found", rows[1]["message"])

    def test_call_loops_auto_balances_missing_weight_before_mustache(self):
        calls = {"check": 0, "balance": 0, "mustache": 0}
        old_check = loop_calling.check_cooler_balance
        old_balance = loop_calling.run_cooler_balance
        old_run = loop_calling.run_command

        def fake_check(_matrix_uri, _weight_name):
            calls["check"] += 1
            if calls["check"] == 1:
                return False, "weight column 'weight' not found"
            return True, "weight column 'weight' finite bins: 2/2"

        def fake_balance(_matrix_uri, _log_path):
            calls["balance"] += 1
            return "cooler balance", 0, "ok"

        def fake_run(_cmd, _dry_run, _log_path):
            calls["mustache"] += 1
            return "mustache", 0, "ok"

        loop_calling.check_cooler_balance = fake_check
        loop_calling.run_cooler_balance = fake_balance
        loop_calling.run_command = fake_run
        try:
            args = SimpleNamespace(
                stats_only=False,
                mustache_bin="",
                resolution_bp=10000,
                skip_balance_check=False,
                auto_balance=True,
                weight_name="weight",
                skip_existing=False,
                mustache_module="mustache",
                pt=0.05,
                st=0.88,
                extra_arg=[],
                dry_run=False,
            )
            samples = {"bud": {"sample": "bud", "matrix": "bud.mcool"}}
            with tempfile.TemporaryDirectory() as tmpdir:
                rows = loop_calling.call_loops(args, samples, Path(tmpdir))
        finally:
            loop_calling.check_cooler_balance = old_check
            loop_calling.run_cooler_balance = old_balance
            loop_calling.run_command = old_run

        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["balanced"], "auto_balanced")
        self.assertEqual(calls["balance"], 1)
        self.assertEqual(calls["mustache"], 1)


if __name__ == "__main__":
    unittest.main()
