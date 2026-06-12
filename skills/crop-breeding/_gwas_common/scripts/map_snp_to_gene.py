#!/usr/bin/env python3
import argparse

from gwas_common import map_snps_to_genes, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Map crop GWAS lead SNPs to nearest and window genes from GFF3.")
    parser.add_argument("--snps", required=True, help="Lead SNP table.")
    parser.add_argument("--gff", required=True, help="GFF3 annotation containing gene features.")
    parser.add_argument("--outdir", required=True, help="Output directory.")
    parser.add_argument("--window", type=int, default=100000, help="Candidate gene window in bp.")
    parser.add_argument("--chr-col", default=None)
    parser.add_argument("--pos-col", default=None)
    parser.add_argument("--id-col", default=None)
    args = parser.parse_args()

    result = map_snps_to_genes(
        args.snps,
        args.gff,
        args.outdir,
        window=args.window,
        chr_col=args.chr_col,
        pos_col=args.pos_col,
        id_col=args.id_col,
    )
    write_json(f"{args.outdir}/candidate_gene_mapping.result.json", result)
    print(f"mapped {result.get('snps', 0)} SNPs -> {args.outdir}")


if __name__ == "__main__":
    main()
