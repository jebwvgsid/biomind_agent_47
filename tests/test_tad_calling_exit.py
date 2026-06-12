import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def _load_call_tads_module():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "skills" / "3d-dna" / "tad-calling" / "scripts" / "call_tads.py"
    spec = importlib.util.spec_from_file_location("call_tads_for_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TadCallingExitTests(unittest.TestCase):
    def test_partial_sample_failure_exits_nonzero_by_default(self):
        call_tads = _load_call_tads_module()

        def fake_process_sample(row, args):
            sample = str(row["sample"])
            status = "success" if sample == "leaf" else "failed"
            run_status = {
                "sample": sample,
                "resolution_bp": 40000,
                "matrix": str(row["matrix"]),
                "out_prefix": str(Path(args.out_root) / "40000" / sample),
                "balanced": "yes" if status == "success" else "no",
                "status": status,
                "return_code": 0 if status == "success" else "",
                "message": "ok" if status == "success" else "missing weight",
            }
            stats = {
                "sample": sample,
                "resolution_bp": 40000,
                "count": 1 if status == "success" else 0,
                "max_length": 40000 if status == "success" else "",
                "min_length": 40000 if status == "success" else "",
                "mean_length": 40000 if status == "success" else "",
                "median_length": 40000 if status == "success" else "",
                "total_length": 40000 if status == "success" else 0,
                "n_boundaries": 1 if status == "success" else 0,
            }
            return run_status, [], [], stats

        call_tads.ensure_input_file = lambda matrix: None
        call_tads.resolve_resolution = lambda sample, matrix, row_resolution, global_resolution: int(global_resolution)
        call_tads.ensure_hicfindtads_available = lambda: None
        call_tads.process_sample = fake_process_sample

        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                "--sample",
                "leaf",
                "leaf.mcool",
                "--sample",
                "bud",
                "bud.mcool",
                "--resolution-bp",
                "40000",
                "--out-root",
                str(Path(tmpdir) / "tads"),
            ]
            with self.assertRaises(SystemExit) as ctx:
                call_tads.main(argv)

        self.assertEqual(ctx.exception.code, 1)

    def test_partial_sample_failure_can_be_explicitly_allowed(self):
        call_tads = _load_call_tads_module()

        def fake_process_sample(row, args):
            sample = str(row["sample"])
            status = "success" if sample == "leaf" else "failed"
            return (
                {
                    "sample": sample,
                    "resolution_bp": 40000,
                    "matrix": str(row["matrix"]),
                    "out_prefix": str(Path(args.out_root) / "40000" / sample),
                    "balanced": "",
                    "status": status,
                    "return_code": "",
                    "message": status,
                },
                [],
                [],
                {
                    "sample": sample,
                    "resolution_bp": 40000,
                    "count": 0,
                    "max_length": "",
                    "min_length": "",
                    "mean_length": "",
                    "median_length": "",
                    "total_length": 0,
                    "n_boundaries": 0,
                },
            )

        call_tads.ensure_input_file = lambda matrix: None
        call_tads.resolve_resolution = lambda sample, matrix, row_resolution, global_resolution: int(global_resolution)
        call_tads.ensure_hicfindtads_available = lambda: None
        call_tads.process_sample = fake_process_sample

        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                "--sample",
                "leaf",
                "leaf.mcool",
                "--sample",
                "bud",
                "bud.mcool",
                "--resolution-bp",
                "40000",
                "--out-root",
                str(Path(tmpdir) / "tads"),
                "--allow-partial-success",
            ]
            call_tads.main(argv)

    def test_preflight_only_checks_balance_without_hicfindtads(self):
        call_tads = _load_call_tads_module()
        hicfindtads_checked = {"value": False}

        call_tads.ensure_input_file = lambda matrix: None
        call_tads.resolve_resolution = lambda sample, matrix, row_resolution, global_resolution: int(global_resolution)
        call_tads.ensure_hicfindtads_available = lambda: hicfindtads_checked.__setitem__("value", True)
        call_tads.check_cooler_balance = lambda matrix_uri, weight_name: (
            (False, "missing weight") if "bud" in matrix_uri else (True, "finite bins: 10/10")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                "--sample",
                "leaf",
                "leaf.mcool",
                "--sample",
                "bud",
                "bud.mcool",
                "--resolution-bp",
                "40000",
                "--out-root",
                str(Path(tmpdir) / "tads"),
                "--preflight-only",
            ]
            with self.assertRaises(SystemExit) as ctx:
                call_tads.main(argv)
            preflight = Path(tmpdir) / "tads" / "40000" / "tad_preflight.tsv"
            preflight_exists = preflight.exists()

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(hicfindtads_checked["value"])
        self.assertTrue(preflight_exists)

    def test_process_sample_auto_balances_missing_weight_before_tad_calling(self):
        call_tads = _load_call_tads_module()
        calls = {"check": 0, "balance": 0, "hic": 0}
        originals = {
            "check_cooler_balance": call_tads.check_cooler_balance,
            "run_cooler_balance": call_tads.run_cooler_balance,
            "run_command": call_tads.run_command,
            "ensure_expected_outputs": call_tads.ensure_expected_outputs,
            "report_native_outputs": call_tads.report_native_outputs,
            "parse_domains_bed": call_tads.parse_domains_bed,
            "parse_boundaries_bed": call_tads.parse_boundaries_bed,
        }

        def fake_check(_matrix_uri, _weight_name):
            calls["check"] += 1
            if calls["check"] == 1:
                return False, "weight column 'weight' not found"
            return True, "weight column 'weight' finite bins: 10/10"

        def fake_balance(_matrix_uri, _log_path):
            calls["balance"] += 1
            return 0

        def fake_run_command(_cmd, _log_path):
            calls["hic"] += 1
            return 0

        try:
            call_tads.check_cooler_balance = fake_check
            call_tads.run_cooler_balance = fake_balance
            call_tads.run_command = fake_run_command
            call_tads.ensure_expected_outputs = lambda _out_prefix: (True, "ok")
            call_tads.report_native_outputs = lambda _out_prefix: None
            call_tads.parse_domains_bed = lambda _path, _sample, _resolution_bp: []
            call_tads.parse_boundaries_bed = lambda _path, _sample, _resolution_bp: []
            with tempfile.TemporaryDirectory() as tmpdir:
                args = SimpleNamespace(
                    out_root=str(Path(tmpdir) / "tads"),
                    correct_for_multiple_testing="fdr",
                    number_of_processors=None,
                    chromosome=None,
                    extra_arg=None,
                    dry_run=False,
                    stats_only=False,
                    skip_existing=False,
                    skip_balance_check=False,
                    auto_balance=True,
                    weight_name="weight",
                )
                row = {"sample": "bud", "matrix": "bud.mcool", "effective_resolution_bp": 40000}
                run_status, _domains, _boundaries, _stats = call_tads.process_sample(row, args)
        finally:
            for name, value in originals.items():
                setattr(call_tads, name, value)

        self.assertEqual(run_status["status"], "success")
        self.assertEqual(run_status["balanced"], "auto_balanced")
        self.assertEqual(calls["balance"], 1)
        self.assertEqual(calls["hic"], 1)


if __name__ == "__main__":
    unittest.main()
