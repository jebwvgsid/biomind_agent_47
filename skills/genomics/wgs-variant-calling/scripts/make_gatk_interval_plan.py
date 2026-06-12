#!/usr/bin/env python3
"""Create a GATK interval plan from a FASTA .fai index.

The default policy keeps contigs/chromosomes <= 500 Mb as single intervals
and splits only larger sequences. Output order follows the .fai file.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def optional_regex(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regex {pattern!r}: {exc}") from exc


def sanitize_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return safe or "interval"


def unique_id(base: str, seen: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in seen:
        candidate = f"{base}_{index}"
        index += 1
    seen.add(candidate)
    return candidate


def read_fai(
    fai: Path,
    include: re.Pattern[str] | None,
    exclude: re.Pattern[str] | None,
    min_contig_bp: int,
) -> tuple[list[tuple[str, int]], list[dict[str, str | int]]]:
    selected: list[tuple[str, int]] = []
    skipped: list[dict[str, str | int]] = []

    with fai.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 2:
                raise SystemExit(f"{fai}:{line_no}: expected at least 2 tab-separated columns")
            contig = fields[0]
            try:
                length = int(fields[1])
            except ValueError as exc:
                raise SystemExit(f"{fai}:{line_no}: invalid contig length {fields[1]!r}") from exc

            reason = ""
            if include and not include.search(contig):
                reason = "include_regex"
            elif exclude and exclude.search(contig):
                reason = "exclude_regex"
            elif length < min_contig_bp:
                reason = "min_contig_bp"

            if reason:
                skipped.append({"contig": contig, "length_bp": length, "reason": reason})
                continue
            selected.append((contig, length))

    return selected, skipped


def build_intervals(contigs: list[tuple[str, int]], max_interval_bp: int) -> list[dict[str, str | int | bool]]:
    intervals: list[dict[str, str | int | bool]] = []
    seen_ids: set[str] = set()

    for contig, length in contigs:
        part_count = int(math.ceil(length / max_interval_bp))
        base_id = sanitize_id(contig)

        if part_count == 1:
            interval_id = unique_id(base_id, seen_ids)
            intervals.append(
                {
                    "interval_id": interval_id,
                    "contig": contig,
                    "start": 1,
                    "end": length,
                    "gatk_interval": contig,
                    "length_bp": length,
                    "contig_length_bp": length,
                    "part_index": 1,
                    "part_count": 1,
                    "split_from_contig": False,
                }
            )
            continue

        width = len(str(part_count))
        for part_index, start in enumerate(range(1, length + 1, max_interval_bp), start=1):
            end = min(start + max_interval_bp - 1, length)
            interval_id = unique_id(f"{base_id}.part{part_index:0{width}d}", seen_ids)
            intervals.append(
                {
                    "interval_id": interval_id,
                    "contig": contig,
                    "start": start,
                    "end": end,
                    "gatk_interval": f"{contig}:{start}-{end}",
                    "length_bp": end - start + 1,
                    "contig_length_bp": length,
                    "part_index": part_index,
                    "part_count": part_count,
                    "split_from_contig": True,
                }
            )

    return intervals


def write_outputs(
    out_prefix: Path,
    intervals: list[dict[str, str | int | bool]],
    skipped: list[dict[str, str | int]],
    args: argparse.Namespace,
) -> None:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    intervals_tsv = out_prefix.with_suffix(".intervals.tsv")
    interval_list = out_prefix.with_suffix(".interval_list")
    summary_json = out_prefix.with_suffix(".summary.json")

    fields = [
        "interval_id",
        "contig",
        "start",
        "end",
        "gatk_interval",
        "length_bp",
        "contig_length_bp",
        "part_index",
        "part_count",
        "split_from_contig",
    ]
    with intervals_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fields)
        writer.writeheader()
        for row in intervals:
            writer.writerow({field: row[field] for field in fields})

    with interval_list.open("w", encoding="utf-8") as handle:
        for row in intervals:
            handle.write(f"{row['gatk_interval']}\n")

    split_contigs = sorted({str(row["contig"]) for row in intervals if row["split_from_contig"]})
    unsplit_contigs = sorted({str(row["contig"]) for row in intervals if not row["split_from_contig"]})
    summary = {
        "source_fai": str(Path(args.fai)),
        "max_interval_bp": args.max_interval_bp,
        "min_contig_bp": args.min_contig_bp,
        "include_regex": args.include_regex,
        "exclude_regex": args.exclude_regex,
        "selected_contigs": len(split_contigs) + len(unsplit_contigs),
        "intervals": len(intervals),
        "total_bp": sum(int(row["length_bp"]) for row in intervals),
        "split_contigs": split_contigs,
        "unsplit_contigs": unsplit_contigs,
        "skipped": skipped,
        "outputs": {
            "intervals_tsv": str(intervals_tsv),
            "interval_list": str(interval_list),
            "summary_json": str(summary_json),
        },
    }
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary["outputs"], indent=2))
    print(
        f"selected_contigs={summary['selected_contigs']} "
        f"intervals={summary['intervals']} "
        f"split_contigs={len(split_contigs)} "
        f"total_bp={summary['total_bp']}",
        file=sys.stderr,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a GATK interval plan from FASTA .fai")
    parser.add_argument("--fai", required=True, help="FASTA index file generated by samtools faidx")
    parser.add_argument("--out-prefix", default="gatk_intervals", help="Output prefix")
    parser.add_argument(
        "--max-interval-bp",
        type=positive_int,
        default=500_000_000,
        help="Keep contigs <= this size whole; split larger contigs into windows of this size",
    )
    parser.add_argument("--min-contig-bp", type=positive_int, default=1, help="Skip contigs shorter than this")
    parser.add_argument("--include-regex", help="Only include contig names matching this regex")
    parser.add_argument("--exclude-regex", help="Exclude contig names matching this regex")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fai = Path(args.fai)
    if not fai.is_file():
        raise SystemExit(f"FAI file not found: {fai}")

    include = optional_regex(args.include_regex)
    exclude = optional_regex(args.exclude_regex)
    contigs, skipped = read_fai(fai, include, exclude, args.min_contig_bp)
    if not contigs:
        raise SystemExit("No contigs selected from the FAI file")

    intervals = build_intervals(contigs, args.max_interval_bp)
    write_outputs(Path(args.out_prefix), intervals, skipped, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
