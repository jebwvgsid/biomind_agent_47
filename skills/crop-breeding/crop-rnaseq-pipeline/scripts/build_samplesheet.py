#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path
import sys

REQUIRED = ["sample", "fastq_1", "fastq_2", "tissue", "genotype", "condition", "replicate", "strandedness"]
VALID_STRANDEDNESS = {"auto", "forward", "reverse", "unstranded"}


def main():
    p = argparse.ArgumentParser(description="Validate and normalize a crop RNA-seq samplesheet.")
    p.add_argument("--input", required=True, help="Input CSV samplesheet.")
    p.add_argument("--output", required=True, help="Validated output CSV.")
    p.add_argument("--check-files", action="store_true", help="Fail if FASTQ paths do not exist.")
    args = p.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
        fields = fh.name and (rows[0].keys() if rows else [])
    if not rows:
        sys.exit("samplesheet is empty")
    missing = [c for c in REQUIRED if c not in rows[0]]
    if missing:
        sys.exit(f"missing required columns: {', '.join(missing)}")

    seen = set()
    errors = []
    for i, row in enumerate(rows, 2):
        sample = row["sample"].strip()
        if not sample:
            errors.append(f"line {i}: empty sample")
        if sample in seen:
            errors.append(f"line {i}: duplicate sample {sample}")
        seen.add(sample)
        stranded = row["strandedness"].strip().lower() or "auto"
        if stranded not in VALID_STRANDEDNESS:
            errors.append(f"line {i}: invalid strandedness {stranded}")
        row["strandedness"] = stranded
        for col in ("fastq_1", "fastq_2"):
            value = row[col].strip()
            if col == "fastq_2" and value in {"", "NA", "na", "-"}:
                row[col] = ""
                continue
            if args.check_files and not Path(value).exists():
                errors.append(f"line {i}: {col} does not exist: {value}")
    if errors:
        sys.exit("\n".join(errors))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"validated {len(rows)} samples -> {out}")


if __name__ == "__main__":
    main()
