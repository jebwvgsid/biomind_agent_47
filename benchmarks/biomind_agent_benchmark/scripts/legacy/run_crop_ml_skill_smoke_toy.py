#!/usr/bin/env python3
"""Generate synthetic crop ML data and smoke-test BioMind crop prediction skills.

This does not reproduce GP-WAITER, DNNGP, CERIS-JGRA, Grapepan, Cropformer, or
the conserved-transcriptome paper models. It verifies that BioMind can discover
the skills, load their metadata, create compatible input/output schemas, and
run the existing baseline crop genomic prediction script on simulated data.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd


NEW_SKILLS = [
    "crop-breeding/crop-gp-waiter-transformer",
    "crop-breeding/crop-dnngp-multiomics-prediction",
    "crop-breeding/crop-gxe-environmental-index-prediction",
    "crop-breeding/crop-pangenome-sv-genomic-selection",
    "crop-breeding/crop-conserved-transcriptome-ml",
    "crop-breeding/crop-cnn-attention-genomic-prediction",
    "crop-breeding/crop-smart-breeding-strategy",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def write_tsv(path: Path, data: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(path, sep="\t", index=False)


def make_synthetic_inputs(outdir: Path, seed: int = 7) -> dict[str, Path]:
    rng = np.random.default_rng(seed)
    inputs = outdir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)

    n_samples = 60
    n_snps = 24
    n_genes = 30
    sample_ids = [f"S{i:03d}" for i in range(1, n_samples + 1)]
    snps = [f"SNP{i:03d}" for i in range(1, n_snps + 1)]
    genes = [f"Gene{i:03d}" for i in range(1, n_genes + 1)]

    geno_values = rng.integers(0, 3, size=(n_samples, n_snps))
    effects = np.zeros(n_snps)
    effects[:5] = [1.1, -0.8, 0.6, 0.35, -0.25]
    trait = 15 + geno_values @ effects + rng.normal(0, 0.8, size=n_samples)

    genotype = pd.DataFrame(geno_values, columns=snps)
    genotype.insert(0, "sample_id", sample_ids)
    phenotype = pd.DataFrame(
        {
            "sample_id": sample_ids,
            "yield": trait.round(4),
            "year": np.where(np.arange(n_samples) % 2 == 0, 2025, 2026),
            "location": np.where(np.arange(n_samples) % 3 == 0, "LocA", "LocB"),
            "family": [f"F{(i % 6) + 1}" for i in range(n_samples)],
        }
    )
    write_tsv(inputs / "genotype_matrix.tsv", genotype)
    write_tsv(inputs / "phenotype.tsv", phenotype)

    ranks = np.arange(1, n_snps + 1)
    p_values = np.clip(10 ** (-(5.5 - ranks / 8.0)), 1e-8, 1)
    gwas = pd.DataFrame(
        {
            "marker_id": snps,
            "chrom": [f"chr{(i % 4) + 1}" for i in range(n_snps)],
            "pos": np.arange(1000, 1000 + n_snps * 500, 500),
            "p_value": p_values,
        }
    )
    write_tsv(inputs / "gwas_summary.tsv", gwas)

    annotation = pd.DataFrame(
        {
            "marker_id": snps,
            "gene_id": [genes[i % n_genes] for i in range(n_snps)],
            "distance_bp": rng.integers(0, 5000, size=n_snps),
            "feature_type": "nearest_gene",
        }
    )
    write_tsv(inputs / "marker_annotation.tsv", annotation)

    expression_values = rng.normal(0, 1, size=(n_samples, n_genes))
    expression_values[:, :4] += trait.reshape(-1, 1) / 10
    expression = pd.DataFrame(expression_values.round(5), columns=genes)
    expression.insert(0, "sample_id", sample_ids)
    write_tsv(inputs / "expression_matrix.tsv", expression)

    metabolites = pd.DataFrame(rng.normal(0, 1, size=(n_samples, 8)).round(5), columns=[f"Met{i:02d}" for i in range(1, 9)])
    metabolites.insert(0, "sample_id", sample_ids)
    write_tsv(inputs / "metabolite_matrix.tsv", metabolites)

    ortholog = pd.DataFrame(
        {
            "crop_gene_id": genes,
            "model_species_gene_id": [f"AT{i:05d}" for i in range(1, n_genes + 1)],
            "orthology_type": "one_to_one",
        }
    )
    write_tsv(inputs / "species_ortholog_table.tsv", ortholog)
    condition = pd.DataFrame(
        {
            "sample_id": sample_ids,
            "species": "synthetic_crop",
            "condition": np.where(np.arange(n_samples) % 2 == 0, "low_N", "high_N"),
            "genotype": [f"G{(i % 10) + 1}" for i in range(n_samples)],
        }
    )
    write_tsv(inputs / "condition_metadata.tsv", condition)

    env_ids = ["E1", "E2", "E3", "E4"]
    env_index = np.array([0.2, 0.8, 1.4, 2.0])
    env_rows = []
    pheno_env_rows = []
    for env, idx in zip(env_ids, env_index):
        for day in range(1, 8):
            env_rows.append({"environment_id": env, "day": day, "variable": "vpd", "value": round(idx + 0.05 * day, 4)})
        for sid, base in zip(sample_ids, trait):
            slope = rng.normal(0.5, 0.12)
            pheno_env_rows.append({"sample_id": sid, "environment_id": env, "yield": round(base + slope * idx + rng.normal(0, 0.3), 4)})
    write_tsv(inputs / "environment_timeseries.tsv", pd.DataFrame(env_rows))
    write_tsv(inputs / "phenotype_multi_environment.tsv", pd.DataFrame(pheno_env_rows))
    write_tsv(inputs / "growth_stage_window.tsv", pd.DataFrame([{"stage": "flowering", "start_day": 1, "end_day": 7, "variable": "vpd"}]))

    pav = pd.DataFrame(rng.integers(0, 2, size=(n_samples, 10)), columns=[f"PAV{i:02d}" for i in range(1, 11)])
    pav.insert(0, "sample_id", sample_ids)
    write_tsv(inputs / "pangenome_gene_presence_absence.tsv", pav)

    vcf_path = inputs / "sv.vcf"
    with vcf_path.open("w", encoding="utf-8") as handle:
        handle.write("##fileformat=VCFv4.2\n")
        handle.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(sample_ids[:12]) + "\n")
        for i in range(1, 7):
            calls = ["0/1" if (j + i) % 3 == 0 else "0/0" for j in range(12)]
            handle.write(f"chr1\t{10000 + i * 1000}\tSV{i:03d}\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END={10500 + i * 1000}\tGT\t" + "\t".join(calls) + "\n")

    return {
        "genotype": inputs / "genotype_matrix.tsv",
        "phenotype": inputs / "phenotype.tsv",
        "gwas": inputs / "gwas_summary.tsv",
        "annotation": inputs / "marker_annotation.tsv",
        "expression": inputs / "expression_matrix.tsv",
        "metabolites": inputs / "metabolite_matrix.tsv",
        "ortholog": inputs / "species_ortholog_table.tsv",
        "condition": inputs / "condition_metadata.tsv",
        "environment": inputs / "environment_timeseries.tsv",
        "multi_environment_phenotype": inputs / "phenotype_multi_environment.tsv",
        "growth_window": inputs / "growth_stage_window.tsv",
        "pav": inputs / "pangenome_gene_presence_absence.tsv",
        "sv_vcf": vcf_path,
    }


def check_skill_discovery(root: Path) -> dict[str, object]:
    sys.path.insert(0, str(root))
    from agent_core.skill_manager import SkillManager

    manager = SkillManager(str(root / "skills"))
    missing = [sid for sid in NEW_SKILLS if not manager.get_skill_meta(sid)]
    route_checks = {
        "gp_waiter": manager.match_skills("GP-WAITER GWAS weighted Transformer phenotype prediction", max_results=3),
        "dnngp": manager.match_skills("DNNGP multi-omics phenotype prediction", max_results=3),
        "gxe": manager.match_skills("environmental index GxE reaction norm prediction", max_results=3),
        "pangenome": manager.match_skills("pangenome SV genomic selection polygenic score", max_results=3),
        "transcriptome": manager.match_skills("conserved transcriptome XGBoost GENIE3 phenotype prediction", max_results=3),
        "cropformer": manager.match_skills("Cropformer CNN self-attention genomic prediction", max_results=3),
        "strategy": manager.match_skills("smart breeding strategy SNP SV omics environment", max_results=3),
    }
    return {"missing": missing, "route_checks": route_checks}


def run_baseline_prediction(root: Path, paths: dict[str, Path], outdir: Path) -> Path:
    baseline_dir = outdir / "baseline_genomic_prediction"
    script = root / "skills" / "crop-breeding" / "crop-genomic-prediction" / "scripts" / "run_ml_models.py"
    cmd = [
        sys.executable,
        str(script),
        "--genotype",
        str(paths["genotype"]),
        "--phenotype",
        str(paths["phenotype"]),
        "--trait",
        "yield",
        "--model",
        "elasticnet",
        "--cv",
        "kfold",
        "--folds",
        "5",
        "--outdir",
        str(baseline_dir),
    ]
    subprocess.run(cmd, check=True, cwd=str(root))
    return baseline_dir


def list_output_files(skill_dir: Path, outdir: Path) -> list[str]:
    return [
        str(path.relative_to(outdir))
        for path in sorted(skill_dir.rglob("*"))
        if path.is_file()
    ]


def run_real_skill_models(root: Path, outdir: Path, paths: dict[str, Path]) -> dict[str, list[str]]:
    """Run each runnable skill wrapper on the synthetic inputs."""
    scripts = root / "skills" / "crop-breeding"
    outputs: dict[str, list[str]] = {}

    jobs = [
        (
            "crop-gp-waiter-transformer",
            scripts / "crop-gp-waiter-transformer" / "scripts" / "run_gp_waiter_ml.py",
            [
                "--genotype", str(paths["genotype"]),
                "--phenotype", str(paths["phenotype"]),
                "--gwas", str(paths["gwas"]),
                "--marker-annotation", str(paths["annotation"]),
                "--trait", "yield",
                "--model", "elasticnet",
            ],
        ),
        (
            "crop-cnn-attention-genomic-prediction",
            scripts / "crop-cnn-attention-genomic-prediction" / "scripts" / "run_cnn_attention_ml.py",
            [
                "--genotype", str(paths["genotype"]),
                "--phenotype", str(paths["phenotype"]),
                "--marker-annotation", str(paths["annotation"]),
                "--trait", "yield",
                "--model", "elasticnet",
            ],
        ),
        (
            "crop-dnngp-multiomics-prediction",
            scripts / "crop-dnngp-multiomics-prediction" / "scripts" / "run_multiomics_ml.py",
            [
                "--phenotype", str(paths["phenotype"]),
                "--trait", "yield",
                "--model", "mlp",
                "--feature-table", f"SNP={paths['genotype']}",
                "--feature-table", f"expression={paths['expression']}",
                "--feature-table", f"metabolite={paths['metabolites']}",
            ],
        ),
        (
            "crop-gxe-environmental-index-prediction",
            scripts / "crop-gxe-environmental-index-prediction" / "scripts" / "run_gxe_reaction_norm_ml.py",
            [
                "--phenotype-multi-environment", str(paths["multi_environment_phenotype"]),
                "--genotype", str(paths["genotype"]),
                "--environment-timeseries", str(paths["environment"]),
                "--growth-stage-window", str(paths["growth_window"]),
                "--trait", "yield",
                "--model", "elasticnet",
            ],
        ),
        (
            "crop-pangenome-sv-genomic-selection",
            scripts / "crop-pangenome-sv-genomic-selection" / "scripts" / "run_pangenome_sv_ml.py",
            [
                "--phenotype", str(paths["phenotype"]),
                "--trait", "yield",
                "--model", "elasticnet",
                "--snp", str(paths["genotype"]),
                "--sv", str(paths["sv_vcf"]),
                "--pav", str(paths["pav"]),
            ],
        ),
        (
            "crop-conserved-transcriptome-ml",
            scripts / "crop-conserved-transcriptome-ml" / "scripts" / "run_conserved_transcriptome_ml.py",
            [
                "--expression", str(paths["expression"]),
                "--phenotype", str(paths["phenotype"]),
                "--ortholog", str(paths["ortholog"]),
                "--trait", "yield",
                "--model", "gradientboosting",
            ],
        ),
    ]

    for skill_name, script, base_args in jobs:
        skill_out = outdir / "skill_outputs" / skill_name
        cmd = [sys.executable, str(script), *base_args, "--outdir", str(skill_out)]
        subprocess.run(cmd, check=True, cwd=str(root))
        outputs[skill_name] = list_output_files(skill_out, outdir)

    strategy_dir = outdir / "skill_outputs" / "crop-smart-breeding-strategy"
    write_tsv(strategy_dir / "selected_skills.tsv", pd.DataFrame({"rank": range(1, len(NEW_SKILLS) + 1), "skill_id": NEW_SKILLS}))
    write_tsv(strategy_dir / "data_requirements.tsv", pd.DataFrame({"data_type": ["genotype", "phenotype", "gwas", "expression", "environment"], "available": [True, True, True, True, True]}))
    write_tsv(strategy_dir / "validation_plan.tsv", pd.DataFrame({"split": ["kfold", "leave-location-out"], "purpose": ["baseline smoke", "GxE validation"]}))
    write_tsv(strategy_dir / "expected_outputs.tsv", pd.DataFrame({"skill_id": list(outputs.keys()), "status": "actual_ml_run"}))
    (strategy_dir / "strategy_report.md").write_text("# Synthetic smart-breeding strategy smoke report\n\nAll method wrappers were executed on simulated data.\n", encoding="utf-8")
    outputs["crop-smart-breeding-strategy"] = list_output_files(strategy_dir, outdir)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test BioMind crop ML prediction skills with synthetic data.")
    parser.add_argument("--outdir", default="results/crop_ml_skill_smoke")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    root = repo_root()
    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = root / outdir
    outdir.mkdir(parents=True, exist_ok=True)

    paths = make_synthetic_inputs(outdir, seed=args.seed)
    discovery = check_skill_discovery(root)
    if discovery["missing"]:
        raise SystemExit(f"Missing skills: {discovery['missing']}")
    baseline_dir = run_baseline_prediction(root, paths, outdir)
    actual_model_outputs = run_real_skill_models(root, outdir, paths)

    manifest = {
        "status": "ok",
        "scope": "synthetic actual ML wrapper run plus existing ElasticNet baseline; not paper-model reproduction",
        "repo_root": str(root),
        "outdir": str(outdir),
        "skills_checked": NEW_SKILLS,
        "route_checks": discovery["route_checks"],
        "inputs": {key: str(value.relative_to(outdir)) for key, value in paths.items()},
        "baseline_output": str(baseline_dir.relative_to(outdir)),
        "actual_model_outputs": actual_model_outputs,
    }
    (outdir / "smoke_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "outdir": str(outdir), "manifest": str(outdir / "smoke_manifest.json")}, indent=2))


if __name__ == "__main__":
    main()
