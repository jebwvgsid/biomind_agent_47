#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


COMMON = Path(__file__).resolve().parents[2] / "_gwas_common" / "scripts"
sys.path.insert(0, str(COMMON))

from gwas_common import (  # noqa: E402
    ensure_dir,
    read_fam_samples,
    read_phenotype_samples,
    read_psam_samples,
    read_vcf_samples,
    run_command,
    write_json,
    write_report,
    write_sample_overlap,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run crop GWAS genotype QC with PLINK2/VCF tools and optional Beagle imputation.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--vcf", help="Input VCF/VCF.GZ.")
    inputs.add_argument("--pfile", help="Input PLINK2 pfile prefix.")
    inputs.add_argument("--bfile", help="Input PLINK bed/bim/fam prefix.")
    parser.add_argument("--phenotype", required=True, help="Phenotype table used for sample overlap checks.")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--sample-col", default=None, help="Phenotype sample ID column override.")
    parser.add_argument("--geno", default="0.1")
    parser.add_argument("--mind", default="0.1")
    parser.add_argument("--maf", default="0.05")
    parser.add_argument("--hwe", default="1e-6")
    parser.add_argument("--max-alleles", default="2")
    parser.add_argument("--ld-prune", default=None, help="PLINK2 --indep-pairwise values, e.g. '50 5 0.2'.")
    parser.add_argument("--impute-beagle", action="store_true", help="Run Beagle before PLINK2 conversion.")
    parser.add_argument("--beagle-jar", default=None, help="Optional Beagle jar path. If omitted, command uses 'beagle'.")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Write commands and reports without executing external tools.")
    args = parser.parse_args()

    outdir = ensure_dir(args.outdir)
    qc_dir = ensure_dir(outdir / "qc")
    repro = ensure_dir(outdir / "reproducibility")
    commands_log = repro / "commands.sh"
    commands_log.write_text("", encoding="utf-8")

    phenotype_samples = read_phenotype_samples(args.phenotype, args.sample_col)
    genotype_samples = []
    source_prefix = ""
    source_kind = ""

    if args.vcf:
        vcf_path = Path(args.vcf)
        if not vcf_path.is_file():
            raise FileNotFoundError(vcf_path)
        source_kind = "vcf"
        source_vcf = str(vcf_path)
        genotype_samples = read_vcf_samples(vcf_path)
        if args.impute_beagle:
            imputed_prefix = qc_dir / "beagle_imputed"
            if args.beagle_jar:
                run_command(
                    ["java", "-jar", args.beagle_jar, f"gt={source_vcf}", f"out={imputed_prefix}", f"nthreads={args.threads}"],
                    commands_log,
                    dry_run=args.dry_run,
                )
            else:
                run_command(
                    ["beagle", f"gt={source_vcf}", f"out={imputed_prefix}", f"nthreads={args.threads}"],
                    commands_log,
                    dry_run=args.dry_run,
                )
            source_vcf = str(imputed_prefix) + ".vcf.gz"
        converted = qc_dir / "plink2_raw"
        run_command(
            [
                "plink2",
                "--vcf",
                source_vcf,
                "--max-alleles",
                str(args.max_alleles),
                "--threads",
                str(args.threads),
                "--make-pgen",
                "--out",
                str(converted),
            ],
            commands_log,
            dry_run=args.dry_run,
        )
        source_prefix = str(converted)
    elif args.pfile:
        source_kind = "pfile"
        source_prefix = args.pfile
        genotype_samples = read_psam_samples(args.pfile)
    else:
        source_kind = "bfile"
        source_prefix = args.bfile
        genotype_samples = read_fam_samples(args.bfile)

    overlap = write_sample_overlap(genotype_samples, phenotype_samples, qc_dir)
    qc_prefix = qc_dir / "genotypes_qc"
    source_flag = "--pfile" if source_kind in {"vcf", "pfile"} else "--bfile"
    run_command(
        [
            "plink2",
            source_flag,
            source_prefix,
            "--geno",
            str(args.geno),
            "--mind",
            str(args.mind),
            "--maf",
            str(args.maf),
            "--hwe",
            str(args.hwe),
            "--max-alleles",
            str(args.max_alleles),
            "--threads",
            str(args.threads),
            "--make-pgen",
            "--out",
            str(qc_prefix),
        ],
        commands_log,
        dry_run=args.dry_run,
    )
    run_command(
        ["plink2", "--pfile", str(qc_prefix), "--pca", "10", "--threads", str(args.threads), "--out", str(qc_prefix) + ".pca"],
        commands_log,
        dry_run=args.dry_run,
    )
    if args.ld_prune:
        prune_values = args.ld_prune.split()
        if len(prune_values) != 3:
            raise ValueError("--ld-prune must contain exactly three PLINK2 --indep-pairwise values.")
        run_command(
            ["plink2", "--pfile", str(qc_prefix), "--indep-pairwise", *prune_values, "--threads", str(args.threads), "--out", str(qc_prefix) + ".ld_prune"],
            commands_log,
            dry_run=args.dry_run,
        )

    result = {
        "input_type": source_kind,
        "qc_prefix": str(qc_prefix),
        "dry_run": bool(args.dry_run),
        **overlap,
    }
    write_json(outdir / "result.json", result)
    write_report(
        outdir / "report.md",
        [
            "# Crop GWAS Genotype QC",
            "",
            f"- input_type: {source_kind}",
            f"- qc_prefix: {qc_prefix}",
            f"- phenotype: {args.phenotype}",
            f"- sample overlap: {overlap['overlap_samples']} of {overlap['genotype_samples']} genotype samples and {overlap['phenotype_samples']} phenotype samples",
            f"- dry_run: {args.dry_run}",
        ],
    )
    print(f"crop GWAS QC commands written to {commands_log}")


if __name__ == "__main__":
    main()
