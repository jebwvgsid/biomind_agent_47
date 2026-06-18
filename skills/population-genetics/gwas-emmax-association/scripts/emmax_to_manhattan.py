#!/usr/bin/env python3
"""Convert EMMAX .ps plus PLINK .tped marker metadata to SNP/CHR/BP/P."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def read_tped(path: Path) -> list[tuple[str, str, str]]:
    markers = []
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) < 4:
                raise SystemExit(f"malformed TPED row: {line[:80]}")
            chrom, snp, _genetic_dist, bp = fields[:4]
            markers.append((snp, chrom, bp))
    return markers


def read_emmax(path: Path) -> list[tuple[str, str]]:
    values = []
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            marker = fields[0]
            pval = fields[-1]
            values.append((marker, pval))
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tped", required=True, type=Path)
    parser.add_argument("--emmax", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    markers = read_tped(args.tped)
    values = read_emmax(args.emmax)
    if len(markers) != len(values):
        print(f"[WARN] TPED markers={len(markers)} EMMAX rows={len(values)}")

    n = min(len(markers), len(values))
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(["SNP", "CHR", "BP", "P"])
        for (snp, chrom, bp), (emmax_marker, pval) in zip(markers[:n], values[:n]):
            if emmax_marker != snp:
                # EMMAX and PLINK usually preserve order. Keep TPED coordinates, but surface mismatches.
                print(f"[WARN] marker mismatch: TPED={snp} EMMAX={emmax_marker}")
            try:
                p = float(pval)
            except ValueError:
                continue
            if 0 < p <= 1:
                writer.writerow([snp, chrom, bp, pval])


if __name__ == "__main__":
    main()
