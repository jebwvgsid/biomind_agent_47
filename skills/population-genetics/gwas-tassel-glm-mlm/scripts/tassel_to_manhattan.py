#!/usr/bin/env python3
"""Convert TASSEL GLM/MLM association output to SNP/CHR/BP/P."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


ALIASES = {
    "snp": {"snp", "marker", "trait_marker", "site"},
    "chr": {"chr", "chrom", "chromosome", "chr."},
    "bp": {"bp", "pos", "position"},
    "p": {"p", "p_marker", "pvalue", "p-value", "p_marker.", "marker_p"},
}


def norm(x: str) -> str:
    return x.strip().lower().replace(" ", "_")


def find_col(header: list[str], key: str, explicit: str | None) -> int:
    if explicit:
        if explicit not in header:
            raise SystemExit(f"explicit column not found: {explicit}")
        return header.index(explicit)
    normalized = [norm(x) for x in header]
    for alias in ALIASES[key]:
        if alias in normalized:
            return normalized.index(alias)
    raise SystemExit(f"could not identify {key} column from header: {header}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--snp-col")
    parser.add_argument("--chr-col")
    parser.add_argument("--bp-col")
    parser.add_argument("--p-col")
    args = parser.parse_args()

    with args.input.open() as fh:
        reader = csv.reader(fh, delimiter="\t")
        rows = [row for row in reader if row and not row[0].startswith("#")]
    if not rows:
        raise SystemExit("empty TASSEL output")
    header = rows[0]
    if len(header) == 1:
        # Some TASSEL outputs are whitespace-delimited despite .txt suffix.
        rows = [line.split() for line in args.input.read_text().splitlines() if line.strip() and not line.startswith("#")]
        header = rows[0]

    cols = {
        "snp": find_col(header, "snp", args.snp_col),
        "chr": find_col(header, "chr", args.chr_col),
        "bp": find_col(header, "bp", args.bp_col),
        "p": find_col(header, "p", args.p_col),
    }

    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(["SNP", "CHR", "BP", "P"])
        for row in rows[1:]:
            if len(row) <= max(cols.values()):
                continue
            try:
                p = float(row[cols["p"]])
            except ValueError:
                continue
            if 0 < p <= 1:
                writer.writerow([row[cols["snp"]], row[cols["chr"]], row[cols["bp"]], row[cols["p"]]])


if __name__ == "__main__":
    main()
