import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_msprime_crop_prediction_smoke(tmp_path):
    if importlib.util.find_spec("msprime") is None:
        pytest.skip("msprime is required for active professional crop fixtures")

    repo_root = Path(__file__).resolve().parents[1]
    script = (
        repo_root
        / "benchmarks"
        / "biomind_agent_benchmark"
        / "scripts"
        / "run"
        / "run_msprime_crop_gp_smoke.py"
    )
    fixture_dir = tmp_path / "professional_fixtures"
    outdir = tmp_path / "msprime_crop_smoke"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--fixture-dir",
            str(fixture_dir),
            "--outdir",
            str(outdir),
            "--seed",
            "20260529",
        ],
        cwd=repo_root,
        check=True,
    )

    manifest_path = outdir / "smoke_manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "ok"
    assert "msprime professional genotype simulation" in manifest["scope"]
    assert (fixture_dir / "msprime" / "simulation_truth.tsv").is_file()
    assert (outdir / "crop_genomic_prediction" / "tables" / "model_performance.tsv").is_file()
    assert (outdir / "gp_waiter_weighted_prediction" / "model_performance.tsv").is_file()


def test_sommer_wrapper_does_not_coerce_fitted_list_directly():
    repo_root = Path(__file__).resolve().parents[1]
    script = (
        repo_root
        / "skills"
        / "crop-breeding"
        / "crop-gs-sommer-multikernel-gxe"
        / "scripts"
        / "run_sommer_multikernel_gxe.R"
    )
    text = script.read_text(encoding="utf-8")

    assert "extract_numeric_vector" in text
    assert "numeric_candidates" in text
    assert "pred <- as.numeric(fitted(fit))" not in text
