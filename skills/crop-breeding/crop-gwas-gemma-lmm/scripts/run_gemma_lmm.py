#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


COMMON = Path(__file__).resolve().parents[2] / "_gwas_common" / "scripts"
sys.path.insert(0, str(COMMON))

from gwas_common import ensure_dir, run_command, standardize_summary, write_json, write_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GEMMA kinship and LMM for crop GWAS and normalize association output.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--bfile", help="PLINK bed/bim/fam prefix for GEMMA.")
    inputs.add_argument("--pfile", help="PLINK2 pfile prefix; converted to bed before GEMMA.")
    parser.add_argument("--phenotype", required=True, help="GEMMA phenotype file or compatible phenotype vector.")
    parser.add_argument("--trait", required=True)
    parser.add_argument("--covariates", default=None, help="Optional GEMMA covariate file.")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--gff", default=None, help="Optional GFF3 annotation for lead SNP mapping.")
    parser.add_argument("--window", type=int, default=100000)
    parser.add_argument("--p-col", default="p_wald")
    parser.add_argument("--chr-col", default=None)
    parser.add_argument("--pos-col", default=None)
    parser.add_argument("--id-col", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    outdir = ensure_dir(args.outdir)
    work = ensure_dir(outdir / "gemma_work")
    repro = ensure_dir(outdir / "reproducibility")
    commands_log = repro / "commands.sh"
    commands_log.write_text("", encoding="utf-8")

    bfile = args.bfile
    if args.pfile:
        bfile = str(work / "gemma_input")
        run_command(["plink2", "--pfile", args.pfile, "--make-bed", "--out", bfile], commands_log, dry_run=args.dry_run)

    safe_trait = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in args.trait).strip("._") or "trait"
    kinship_name = f"{safe_trait}.kinship"
    assoc_name = f"{safe_trait}.gemma_lmm"
    run_command(["gemma", "-bfile", bfile, "-gk", "1", "-o", kinship_name], commands_log, cwd=work, dry_run=args.dry_run)
    assoc_cmd = [
        "gemma",
        "-bfile",
        bfile,
        "-k",
        str(work / "output" / f"{kinship_name}.cXX.txt"),
        "-p",
        args.phenotype,
        "-lmm",
        "4",
        "-o",
        assoc_name,
    ]
    if args.covariates:
        assoc_cmd.extend(["-c", args.covariates])
    run_command(assoc_cmd, commands_log, cwd=work, dry_run=args.dry_run)

    assoc_path = work / "output" / f"{assoc_name}.assoc.txt"
    result = {"dry_run": bool(args.dry_run), "expected_assoc": str(assoc_path)}
    if assoc_path.is_file():
        result.update(
            standardize_summary(
                assoc_path,
                outdir,
                trait=args.trait,
                method="GEMMA-LMM",
                p_col=args.p_col,
                chr_col=args.chr_col,
                pos_col=args.pos_col,
                id_col=args.id_col,
                source=str(assoc_path),
            )
        )
        if args.gff:
            from gwas_common import map_snps_to_genes

            result.update(map_snps_to_genes(outdir / "lead_snps.tsv", args.gff, outdir, window=args.window))
    write_json(outdir / "result.json", result)
    write_report(
        outdir / "report.md",
        [
            "# Crop GWAS GEMMA LMM",
            "",
            f"- trait: {args.trait}",
            f"- bfile: {bfile}",
            f"- phenotype: {args.phenotype}",
            f"- covariates: {args.covariates or 'none'}",
            f"- expected association output: {assoc_path}",
            f"- dry_run: {args.dry_run}",
        ],
    )
    print(f"GEMMA-LMM commands written to {commands_log}")


if __name__ == "__main__":
    main()
