#!/usr/bin/env python3
import argparse
from pathlib import Path
import pandas as pd
from run_popgen_stats import read_fasta


def main():
    p = argparse.ArgumentParser(description="Create haplotype network input table from FASTA alignment.")
    p.add_argument("--alignment", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    seqs = read_fasta(args.alignment)
    rows = []
    for hap_id, (seq, sub) in enumerate(pd.Series(seqs).groupby(pd.Series(seqs).values), 1):
        for sample in sub.index:
            rows.append({"haplotype_id": f"H{hap_id}", "sample_id": sample, "sequence": seq})
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.output, sep="\t", index=False)
    print(f"wrote haplotypes -> {args.output}")


if __name__ == "__main__":
    main()
