#!/usr/bin/env python3
"""Sort phenotype or covariate rows to PLINK TFAM sample order."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def split_row(line: str) -> list[str]:
    return line.rstrip("\n").split("\t") if "\t" in line else line.split()


def read_table(path: Path) -> tuple[list[str] | None, dict[str, list[str]]]:
    rows = [split_row(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"empty table: {path}")
    header = rows[0]
    has_header = any(x.lower() in {"sample", "sample_id", "id", "fid", "iid"} for x in header)
    if has_header:
        sample_col = next((i for i, x in enumerate(header) if x.lower() in {"sample", "sample_id", "id", "fid", "iid"}), 0)
        data_rows = rows[1:]
    else:
        sample_col = 0
        header = None
        data_rows = rows
    data: dict[str, list[str]] = {}
    for row in data_rows:
        if len(row) <= sample_col:
            continue
        data[row[sample_col]] = row
    return header, data


def resolve_trait_indexes(header: list[str] | None, traits: str, data: dict[str, list[str]]) -> list[int]:
    names = [x.strip() for x in traits.split(",") if x.strip()]
    if header:
        idx = []
        for name in names:
            if name not in header:
                raise SystemExit(f"trait/covariate column not found: {name}")
            idx.append(header.index(name))
        return idx
    first = next(iter(data.values()))
    idx = []
    for name in names:
        if name.isdigit():
            idx.append(int(name) - 1)
        elif len(first) == 2:
            idx.append(1)
        else:
            raise SystemExit("no header: use 1-based numeric column indexes, e.g. --trait 2 or --trait 2,3,4")
    return idx


def read_tfam_samples(tfam: Path) -> list[tuple[str, str]]:
    samples = []
    for line in tfam.read_text().splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 2:
            raise SystemExit(f"malformed TFAM row: {line}")
        samples.append((fields[0], fields[1]))
    if not samples:
        raise SystemExit(f"empty TFAM: {tfam}")
    return samples


def clean_missing(value: str) -> str:
    if value in {"", "NA", "NaN", "nan", ".", "null", "NULL"}:
        return "NA"
    return value


def write_emmax(path: Path, samples: list[tuple[str, str]], data: dict[str, list[str]], idx: list[int]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        for fid, iid in samples:
            row = data.get(iid) or data.get(fid)
            vals = ["NA"] * len(idx) if row is None else [clean_missing(row[i]) if i < len(row) else "NA" for i in idx]
            writer.writerow([fid, iid, *vals])


def write_emmax_covar(path: Path, samples: list[tuple[str, str]], data: dict[str, list[str]], idx: list[int]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        for fid, iid in samples:
            row = data.get(iid) or data.get(fid)
            vals = ["NA"] * len(idx) if row is None else [clean_missing(row[i]) if i < len(row) else "NA" for i in idx]
            writer.writerow([fid, iid, "1", *vals])


def write_tassel(path: Path, samples: list[tuple[str, str]], data: dict[str, list[str]], idx: list[int], names: list[str], covariate: bool) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        if covariate:
            writer.writerow(["<Covariate>"])
        writer.writerow(["<Trait>", *names])
        for fid, iid in samples:
            row = data.get(iid) or data.get(fid)
            vals = ["NA"] * len(idx) if row is None else [clean_missing(row[i]) if i < len(row) else "NA" for i in idx]
            writer.writerow([iid, *vals])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tfam", required=True, type=Path)
    parser.add_argument("--phenotype", required=True, type=Path)
    parser.add_argument("--trait", required=True, help="Column name(s), or 1-based indexes when no header exists")
    parser.add_argument("--out-emmax", type=Path)
    parser.add_argument("--out-emmax-covar", type=Path)
    parser.add_argument("--out-tassel", type=Path)
    parser.add_argument("--tassel-covariate", action="store_true")
    args = parser.parse_args()

    header, data = read_table(args.phenotype)
    idx = resolve_trait_indexes(header, args.trait, data)
    names = [header[i] if header else f"V{i + 1}" for i in idx]
    samples = read_tfam_samples(args.tfam)

    missing = sum(1 for fid, iid in samples if iid not in data and fid not in data)
    if missing:
        print(f"[WARN] {missing} TFAM samples missing in phenotype/covariate table", file=sys.stderr)

    if args.out_emmax:
        write_emmax(args.out_emmax, samples, data, idx)
    if args.out_emmax_covar:
        write_emmax_covar(args.out_emmax_covar, samples, data, idx)
    if args.out_tassel:
        write_tassel(args.out_tassel, samples, data, idx, names, args.tassel_covariate or len(idx) > 1)


if __name__ == "__main__":
    main()
