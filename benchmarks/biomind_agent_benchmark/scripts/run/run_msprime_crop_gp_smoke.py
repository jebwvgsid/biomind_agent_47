from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd


BENCHMARK_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BENCHMARK_DIR.parents[1]
GENERATOR = BENCHMARK_DIR / "scripts" / "generate" / "generate_msprime_fixtures.py"


def run_command(cmd: list[str]) -> None:
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def ensure_msprime_fixture(fixture_dir: Path, seed: int) -> None:
    required = [
        fixture_dir / "msprime" / "genotype_matrix.tsv",
        fixture_dir / "msprime" / "phenotype.tsv",
        fixture_dir / "msprime" / "gwas_summary.tsv",
        fixture_dir / "msprime" / "marker_annotation.tsv",
        fixture_dir / "msprime" / "simulation_truth.tsv",
    ]
    if all(path.exists() and path.stat().st_size > 0 for path in required):
        return
    run_command([sys.executable, str(GENERATOR), "--outdir", str(fixture_dir), "--seed", str(seed)])


def normalize_gwas_for_biomind(msprime_dir: Path) -> Path:
    src = msprime_dir / "gwas_summary.tsv"
    dst = msprime_dir / "gwas_summary_biomind.tsv"
    gwas = pd.read_csv(src, sep="\t")
    if "pvalue" in gwas.columns and "p_value" not in gwas.columns:
        gwas["p_value"] = gwas["pvalue"]
    gwas.to_csv(dst, sep="\t", index=False)
    return dst


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a crop genomic-prediction smoke test from msprime fixtures.")
    parser.add_argument("--fixture-dir", default="results/linux_professional_synthetic")
    parser.add_argument("--outdir", default="results/linux_msprime_crop_gp_smoke")
    parser.add_argument("--seed", type=int, default=20260529)
    parser.add_argument("--skip-generate", action="store_true")
    args = parser.parse_args()

    fixture_dir = (REPO_ROOT / args.fixture_dir).resolve() if not Path(args.fixture_dir).is_absolute() else Path(args.fixture_dir).resolve()
    outdir = (REPO_ROOT / args.outdir).resolve() if not Path(args.outdir).is_absolute() else Path(args.outdir).resolve()
    msprime_dir = fixture_dir / "msprime"

    if not args.skip_generate:
        ensure_msprime_fixture(fixture_dir, args.seed)

    gwas = normalize_gwas_for_biomind(msprime_dir)
    gp_out = outdir / "crop_genomic_prediction"
    gp_waiter_out = outdir / "gp_waiter_weighted_prediction"

    run_command(
        [
            sys.executable,
            "skills/crop-breeding/crop-genomic-prediction/scripts/run_ml_models.py",
            "--genotype",
            str(msprime_dir / "genotype_matrix.tsv"),
            "--phenotype",
            str(msprime_dir / "phenotype.tsv"),
            "--trait",
            "trait_yield",
            "--model",
            "elasticnet",
            "--cv",
            "kfold",
            "--folds",
            "5",
            "--outdir",
            str(gp_out),
        ]
    )
    run_command(
        [
            sys.executable,
            "skills/crop-breeding/crop-gp-waiter-transformer/scripts/run_gp_waiter_ml.py",
            "--genotype",
            str(msprime_dir / "genotype_matrix.tsv"),
            "--phenotype",
            str(msprime_dir / "phenotype.tsv"),
            "--gwas",
            str(gwas),
            "--marker-annotation",
            str(msprime_dir / "marker_annotation.tsv"),
            "--trait",
            "trait_yield",
            "--model",
            "elasticnet",
            "--outdir",
            str(gp_waiter_out),
        ]
    )

    manifest = {
        "status": "ok",
        "scope": "msprime professional genotype simulation plus BioMind-controlled phenotype and GWAS truth labels",
        "fixture_dir": str(fixture_dir),
        "outdir": str(outdir),
        "inputs": {
            "genotype": str(msprime_dir / "genotype_matrix.tsv"),
            "phenotype": str(msprime_dir / "phenotype.tsv"),
            "gwas": str(gwas),
            "marker_annotation": str(msprime_dir / "marker_annotation.tsv"),
            "simulation_truth": str(msprime_dir / "simulation_truth.tsv"),
        },
        "outputs": {
            "crop_genomic_prediction": str(gp_out),
            "gp_waiter_weighted_prediction": str(gp_waiter_out),
        },
    }
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "smoke_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "manifest": str(outdir / "smoke_manifest.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

