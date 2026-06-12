#!/usr/bin/env python3
import argparse
from pathlib import Path
import pandas as pd


def read_table(path):
    return pd.read_csv(path, sep=None, engine="python")


def main():
    p = argparse.ArgumentParser(description="Merge crop DEG table with gene function annotations.")
    p.add_argument("--de", required=True)
    p.add_argument("--annotation", required=True, help="Annotation table containing gene_id or first column as gene id.")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    de = read_table(args.de)
    ann = read_table(args.annotation)
    if "gene_id" not in de.columns:
        de = de.rename(columns={de.columns[0]: "gene_id"})
    if "gene_id" not in ann.columns:
        ann = ann.rename(columns={ann.columns[0]: "gene_id"})
    merged = de.merge(ann, on="gene_id", how="left", suffixes=("", "_annotation"))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.output, sep="\t", index=False)
    print(f"merged {len(merged)} genes -> {args.output}")


if __name__ == "__main__":
    main()
