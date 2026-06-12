from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

import msprime
import numpy as np


BENCHMARK_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BENCHMARK_DIR.parents[1]
DEFAULT_OUTDIR = BENCHMARK_DIR / "fixtures_professional"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_tsv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_fasta(path: Path, records: list[tuple[str, str]], line_width: int = 80) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for name, seq in records:
            handle.write(f">{name}\n")
            for i in range(0, len(seq), line_width):
                handle.write(seq[i : i + line_width] + "\n")


def rel_from_benchmark_or_repo(path: Path) -> str:
    target = path.resolve()
    for base in (BENCHMARK_DIR, REPO_ROOT):
        try:
            return target.relative_to(base).as_posix()
        except ValueError:
            continue
    return target.as_posix()


def make_msprime_tree_sequence(seed: int, sequence_length: int, individuals_per_pop: int) -> msprime.TreeSequence:
    demography = msprime.Demography()
    demography.add_population(name="popA", initial_size=3_000)
    demography.add_population(name="popB", initial_size=3_000)
    demography.add_population(name="ancestral", initial_size=8_000)
    demography.add_population_split(time=800, derived=["popA", "popB"], ancestral="ancestral")
    demography.sort_events()

    ancestry = msprime.sim_ancestry(
        samples={"popA": individuals_per_pop, "popB": individuals_per_pop},
        demography=demography,
        sequence_length=sequence_length,
        recombination_rate=1e-8,
        ploidy=2,
        random_seed=seed,
    )
    mutated = msprime.sim_mutations(ancestry, rate=8e-7, random_seed=seed + 1)
    if mutated.num_sites < 12:
        mutated = msprime.sim_mutations(ancestry, rate=2e-6, random_seed=seed + 2)
    return mutated


def write_vcf(ts: msprime.TreeSequence, path: Path, individual_names: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        ts.write_vcf(handle, contig_id="Chr01", individual_names=individual_names)


def genotype_matrix_from_ts(ts: msprime.TreeSequence) -> tuple[list[str], np.ndarray, list[int]]:
    variant_ids: list[str] = []
    positions: list[int] = []
    genotypes: list[np.ndarray] = []
    n_individuals = ts.num_individuals
    for idx, variant in enumerate(ts.variants()):
        if len(variant.alleles) != 2:
            continue
        marker_id = f"MSP_SNP_{idx + 1:04d}"
        haploid = np.array(variant.genotypes, dtype=int)
        diploid = haploid.reshape((n_individuals, 2)).sum(axis=1)
        variant_ids.append(marker_id)
        positions.append(max(1, int(round(variant.site.position))))
        genotypes.append(diploid)
        if len(variant_ids) >= 64:
            break
    if not genotypes:
        raise RuntimeError("msprime generated no biallelic variants")
    return variant_ids, np.vstack(genotypes).T, positions


def simulate_msprime_crop_and_popgen(outdir: Path, seed: int) -> None:
    rng = np.random.default_rng(seed + 10)
    out = ensure_dir(outdir / "msprime")
    ts = make_msprime_tree_sequence(seed=seed, sequence_length=80_000, individuals_per_pop=12)
    n_individuals = ts.num_individuals
    sample_names = [f"MSP_CROP_{i + 1:03d}" for i in range(n_individuals)]
    groups = ["popA" if i < n_individuals // 2 else "popB" for i in range(n_individuals)]

    write_vcf(ts, out / "msprime_variants.vcf", sample_names)
    marker_ids, genotype, positions = genotype_matrix_from_ts(ts)

    geno_rows = []
    for sample_idx, sample in enumerate(sample_names):
        row: dict[str, object] = {"sample_id": sample}
        row.update({marker_id: int(genotype[sample_idx, j]) for j, marker_id in enumerate(marker_ids)})
        geno_rows.append(row)
    write_tsv(out / "genotype_matrix.tsv", geno_rows, ["sample_id", *marker_ids])

    causal_count = min(8, len(marker_ids))
    causal_indices = np.linspace(0, len(marker_ids) - 1, causal_count, dtype=int)
    effects = np.zeros(len(marker_ids))
    effects[causal_indices] = rng.normal(0.0, 1.1, size=causal_count)
    breeding_value = genotype @ effects
    group_effect = np.array([0.8 if g == "popA" else -0.8 for g in groups])
    phenotype = 100 + breeding_value + group_effect + rng.normal(0, 1.2, n_individuals)

    phenotype_rows = [
        {
            "sample_id": sample,
            "trait_yield": round(float(phenotype[i]), 4),
            "true_breeding_value": round(float(breeding_value[i]), 4),
            "population": groups[i],
        }
        for i, sample in enumerate(sample_names)
    ]
    write_tsv(
        out / "phenotype.tsv",
        phenotype_rows,
        ["sample_id", "trait_yield", "true_breeding_value", "population"],
    )

    marker_rows = [
        {
            "marker_id": marker_id,
            "chrom": "Chr01",
            "pos": int(positions[i]),
            "gene_id": f"MSP_GENE_{(i // 2) + 1:04d}",
            "causal": bool(i in set(causal_indices.tolist())),
            "true_effect": round(float(effects[i]), 5),
        }
        for i, marker_id in enumerate(marker_ids)
    ]
    write_tsv(
        out / "marker_annotation.tsv",
        marker_rows,
        ["marker_id", "chrom", "pos", "gene_id", "causal", "true_effect"],
    )

    pvalues = np.ones(len(marker_ids))
    for i in range(len(marker_ids)):
        if i in set(causal_indices.tolist()):
            pvalues[i] = 10 ** (-rng.uniform(4, 8))
        else:
            pvalues[i] = rng.uniform(0.05, 0.95)
    gwas_rows = [
        {
            "marker_id": marker_ids[i],
            "chrom": "Chr01",
            "pos": int(positions[i]),
            "beta": round(float(effects[i] + rng.normal(0, 0.03)), 5),
            "pvalue": f"{pvalues[i]:.6g}",
            "is_causal_truth": bool(i in set(causal_indices.tolist())),
        }
        for i in range(len(marker_ids))
    ]
    write_tsv(
        out / "gwas_summary.tsv",
        gwas_rows,
        ["marker_id", "chrom", "pos", "beta", "pvalue", "is_causal_truth"],
    )

    marker_effect_rows = [
        {
            "marker_id": marker_ids[i],
            "effect_allele": "ALT",
            "effect": round(float(effects[i]), 5),
            "trait_direction": "high" if effects[i] >= 0 else "low",
        }
        for i in causal_indices
    ]
    write_tsv(
        out / "marker_effects.tsv",
        marker_effect_rows,
        ["marker_id", "effect_allele", "effect", "trait_direction"],
    )

    group_rows = [
        {"sample_id": sample, "population": groups[i], "ploidy": 2}
        for i, sample in enumerate(sample_names)
    ]
    write_tsv(out / "sample_groups.tsv", group_rows, ["sample_id", "population", "ploidy"])

    haplotypes = []
    for idx, hap in enumerate(ts.haplotypes()):
        if idx >= min(12, ts.num_samples):
            break
        haplotypes.append((f"MSP_HAP_{idx + 1:03d}", hap[:20_000]))
    write_fasta(out / "haplotypes.fasta", haplotypes)

    truth_rows = [
        {"metric": "simulator", "value": "msprime"},
        {"metric": "msprime_version", "value": msprime.__version__},
        {"metric": "sequence_length", "value": int(ts.sequence_length)},
        {"metric": "num_individuals", "value": n_individuals},
        {"metric": "num_sites", "value": ts.num_sites},
        {"metric": "num_markers_exported", "value": len(marker_ids)},
        {"metric": "num_causal_markers", "value": causal_count},
        {"metric": "seed", "value": seed},
    ]
    write_tsv(out / "simulation_truth.tsv", truth_rows, ["metric", "value"])

    root = rel_from_benchmark_or_repo(outdir)
    files = {
        "genotype": rel_from_benchmark_or_repo(out / "genotype_matrix.tsv"),
        "phenotype": rel_from_benchmark_or_repo(out / "phenotype.tsv"),
        "gwas": rel_from_benchmark_or_repo(out / "gwas_summary.tsv"),
        "annotation": rel_from_benchmark_or_repo(out / "marker_annotation.tsv"),
        "vcf": rel_from_benchmark_or_repo(out / "msprime_variants.vcf"),
        "haplotypes": rel_from_benchmark_or_repo(out / "haplotypes.fasta"),
        "sample_groups": rel_from_benchmark_or_repo(out / "sample_groups.tsv"),
        "marker_effects": rel_from_benchmark_or_repo(out / "marker_effects.tsv"),
        "truth": rel_from_benchmark_or_repo(out / "simulation_truth.tsv"),
    }
    manifest = f"""fixture_set_id: biomind-professional-msprime-fixtures
version: 0.1.0
seed: {seed}
simulators:
  msprime: {msprime.__version__}
root: {root}
notes:
  - Generated with msprime coalescent ancestry and mutation simulation.
  - Active professional simulated data are restricted to msprime outputs.
  - Phenotype and GWAS-like p-values are BioMind-controlled truth labels derived from simulated genotypes.

task_fixtures:
  BM-CROP-GP-GWAS-013:
    - {files["genotype"]}
    - {files["phenotype"]}
    - {files["gwas"]}
    - {files["annotation"]}
  BM-CROP-POPGEN-MARKER-018:
    - {files["vcf"]}
    - {files["haplotypes"]}
    - {files["sample_groups"]}
    - {files["marker_effects"]}
  BM-CROP-DEEP-GP-016:
    - {files["genotype"]}
    - {files["phenotype"]}
    - {files["gwas"]}
    - {files["annotation"]}
  BM-CROP-MSPrime-TRUTH:
    - {files["truth"]}
"""
    manifest_path = BENCHMARK_DIR / "manifests" / "professional_fixtures_manifest.yaml"
    ensure_dir(manifest_path.parent)
    manifest_path.write_text(manifest, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate simulator-backed professional BioMind fixtures.")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="Output fixture directory.")
    parser.add_argument("--seed", type=int, default=20260526, help="Random seed.")
    args = parser.parse_args()

    outdir = Path(args.outdir).resolve()
    simulate_msprime_crop_and_popgen(outdir, args.seed)
    print(f"Generated msprime professional fixtures under {outdir}")
    print(f"Manifest: {BENCHMARK_DIR / 'manifests' / 'professional_fixtures_manifest.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
