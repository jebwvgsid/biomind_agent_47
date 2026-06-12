#!/usr/bin/env python3
import argparse

from gwas_common import map_snps_to_genes, standardize_summary, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize GWAS summary statistics to the BioMind crop GWAS schema.")
    parser.add_argument("--gwas", required=True, help="GWAS result table with p-value, chromosome, and position columns.")
    parser.add_argument("--outdir", required=True, help="Output directory.")
    parser.add_argument("--trait", required=True, help="Trait name to record in outputs.")
    parser.add_argument("--method", default="GWAS", help="Association method label.")
    parser.add_argument("--source", default="", help="Optional source label for the input result.")
    parser.add_argument("--p-col", default=None)
    parser.add_argument("--chr-col", default=None)
    parser.add_argument("--pos-col", default=None)
    parser.add_argument("--id-col", default=None)
    parser.add_argument("--effect-col", default=None)
    parser.add_argument("--gff", default=None, help="Optional GFF3 gene annotation for candidate mapping.")
    parser.add_argument("--window", type=int, default=100000, help="Candidate gene window around lead SNPs.")
    args = parser.parse_args()

    result = standardize_summary(
        args.gwas,
        args.outdir,
        trait=args.trait,
        method=args.method,
        p_col=args.p_col,
        chr_col=args.chr_col,
        pos_col=args.pos_col,
        id_col=args.id_col,
        effect_col=args.effect_col,
        source=args.source,
    )
    if args.gff:
        mapping = map_snps_to_genes(f"{args.outdir}/lead_snps.tsv", args.gff, args.outdir, window=args.window)
        result.update(mapping)
        write_json(f"{args.outdir}/result.json", result)
    print(f"normalized {result.get('n_variants', 0)} variants -> {args.outdir}")


if __name__ == "__main__":
    main()
