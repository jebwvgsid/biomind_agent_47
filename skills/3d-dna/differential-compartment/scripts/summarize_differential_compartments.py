#!/usr/bin/env python
"""Summarize differential A/B and sub-compartment transitions between samples."""

from __future__ import annotations

import argparse
import csv
import itertools
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


LEVELS = ("Compartment2", "Compartment4", "Compartment8")


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
    return cleaned.strip("._-") or "sample"


def read_rows(path: str | Path) -> List[Dict[str, str]]:
    p = Path(path)
    with p.open(newline="", encoding="utf-8", errors="replace") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        except csv.Error:
            dialect = csv.excel_tab
        return list(csv.DictReader(handle, dialect=dialect))


def load_sample_paths(args: argparse.Namespace) -> Dict[str, Path]:
    samples: Dict[str, Path] = {}
    for item in args.sample or []:
        label, path = item
        samples[safe_label(label)] = Path(path)
    if args.sample_sheet:
        for row in read_rows(args.sample_sheet):
            label = row.get("sample") or row.get("label")
            path = row.get("compartments") or row.get("path") or row.get("result")
            if not label or not path:
                raise SystemExit("[ERROR] sample sheet requires sample and compartments/path columns")
            samples[safe_label(label)] = Path(path)
    return samples


def parse_int(value: object) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value)))
    except ValueError:
        return None


def comp_levels(comp_name: str) -> Dict[str, str]:
    return {
        "Compartment2": comp_name[:1],
        "Compartment4": comp_name[:3],
        "Compartment8": comp_name[:5],
    }


def find_compartment_file(path: Path) -> Optional[Path]:
    candidates: List[Path] = []
    if path.is_file():
        candidates.append(path)
    else:
        candidates.extend(
            [
                path / "compartments_long.tsv",
                path / "sub_compartments" / "all_sub_compartments.tsv",
                path / "sub_compartments" / "all_sub_compartments.bed",
                path / "all_sub_compartments.tsv",
                path / "all_sub_compartments.bed",
            ]
        )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def parse_normalized(path: Path, forced_sample: Optional[str], resolution_bp: Optional[int]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for raw in read_rows(path):
        sample = forced_sample or raw.get("sample") or raw.get("label")
        chrom = raw.get("chr") or raw.get("Chr") or raw.get("chrom") or raw.get("chromosome")
        start = parse_int(raw.get("start"))
        end = parse_int(raw.get("end"))
        if not sample or not chrom or start is None or end is None:
            continue
        comp2 = raw.get("Compartment2") or raw.get("compartment2") or raw.get("AB") or ""
        comp4 = raw.get("Compartment4") or raw.get("compartment4") or ""
        comp8 = raw.get("Compartment8") or raw.get("compartment8") or ""
        comp_name = raw.get("comp_name") or raw.get("calder") or comp8 or comp4 or comp2
        if not (comp2 and comp4 and comp8) and comp_name:
            levels = comp_levels(comp_name)
            comp2 = comp2 or levels["Compartment2"]
            comp4 = comp4 or levels["Compartment4"]
            comp8 = comp8 or levels["Compartment8"]
        rows.append(
            {
                "sample": safe_label(sample),
                "resolution_bp": raw.get("resolution_bp") or resolution_bp or "",
                "chr": chrom,
                "start": start,
                "end": end,
                "length": parse_int(raw.get("length")) or max(0, end - start),
                "comp_name": comp_name,
                "Compartment2": comp2,
                "Compartment4": comp4,
                "Compartment8": comp8,
                "color": raw.get("color", ""),
                "source_file": str(path),
            }
        )
    return rows


def parse_calder_tsv(path: Path, sample: str, resolution_bp: Optional[int]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for raw in read_rows(path):
        chrom = raw.get("Chr") or raw.get("chr") or raw.get("chrom") or raw.get("chromosome")
        comp_name = raw.get("comp_name") or raw.get("calder") or raw.get("name")
        pos_start = parse_int(raw.get("pos_start") or raw.get("start"))
        pos_end = parse_int(raw.get("pos_end") or raw.get("end"))
        if not chrom or not comp_name or pos_start is None or pos_end is None:
            continue
        start = max(0, pos_start - 1)
        end = pos_end
        rows.append(
            {
                "sample": sample,
                "resolution_bp": resolution_bp or "",
                "chr": chrom,
                "start": start,
                "end": end,
                "length": max(0, end - start),
                "comp_name": comp_name,
                **comp_levels(comp_name),
                "color": "",
                "source_file": str(path),
            }
        )
    return rows


def parse_calder_bed(path: Path, sample: str, resolution_bp: Optional[int], bed_start_one_based: bool) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 4:
                fields = line.strip().split()
            if len(fields) < 4:
                continue
            start = parse_int(fields[1])
            end = parse_int(fields[2])
            if start is None or end is None:
                continue
            if bed_start_one_based:
                start = max(0, start - 1)
            comp_name = fields[3]
            rows.append(
                {
                    "sample": sample,
                    "resolution_bp": resolution_bp or "",
                    "chr": fields[0],
                    "start": start,
                    "end": end,
                    "length": max(0, end - start),
                    "comp_name": comp_name,
                    **comp_levels(comp_name),
                    "color": fields[8] if len(fields) >= 9 else "",
                    "source_file": str(path),
                }
            )
    return rows


def parse_compartment_path(
    path: Path,
    sample: Optional[str],
    resolution_bp: Optional[int],
    bed_start_one_based: bool,
) -> List[Dict[str, object]]:
    comp_file = find_compartment_file(path)
    if not comp_file:
        raise SystemExit(f"[ERROR] no compartment file found: {path}")
    if comp_file.name == "compartments_long.tsv":
        return parse_normalized(comp_file, sample, resolution_bp)
    if comp_file.suffix.lower() == ".tsv":
        rows = parse_calder_tsv(comp_file, sample or "", resolution_bp)
        if rows:
            return rows
        return parse_normalized(comp_file, sample, resolution_bp)
    return parse_calder_bed(comp_file, sample or "", resolution_bp, bed_start_one_based)


def write_tsv(path: Path, rows: Iterable[Dict[str, object]], fieldnames: Optional[Sequence[str]] = None) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not fieldnames:
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_all_compartments(args: argparse.Namespace) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if args.compartments_long:
        rows.extend(parse_normalized(Path(args.compartments_long), None, args.resolution_bp))
    for sample, path in sorted(load_sample_paths(args).items()):
        rows.extend(parse_compartment_path(path, sample, args.resolution_bp, args.bed_start_one_based))
    if not rows:
        raise SystemExit("[ERROR] no compartment rows parsed")
    return rows


def key_for(row: Dict[str, object]) -> Tuple[str, int, int]:
    return str(row["chr"]), int(row["start"]), int(row["end"])


def index_by_sample(rows: Sequence[Dict[str, object]]) -> Dict[str, Dict[Tuple[str, int, int], Dict[str, object]]]:
    indexed: Dict[str, Dict[Tuple[str, int, int], Dict[str, object]]] = {}
    for row in rows:
        sample = str(row["sample"])
        indexed.setdefault(sample, {})[key_for(row)] = row
    return indexed


def parse_comparisons(args: argparse.Namespace, samples: Sequence[str]) -> List[Tuple[str, str]]:
    comparisons = [(safe_label(a), safe_label(b)) for a, b in (args.comparison or [])]
    if comparisons:
        return comparisons
    if args.reference:
        ref = safe_label(args.reference)
        return [(ref, sample) for sample in samples if sample != ref]
    return list(itertools.combinations(samples, 2))


def selected_levels(args: argparse.Namespace) -> List[str]:
    if not args.level or "all" in args.level:
        return list(LEVELS)
    return args.level


def transform_label(value_a: str, value_b: str) -> str:
    if value_a == value_b:
        return "same"
    return f"{value_a}_to_{value_b}"


def compare_pair(
    pair: Tuple[str, str],
    level: str,
    indexed: Dict[str, Dict[Tuple[str, int, int], Dict[str, object]]],
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    sample_a, sample_b = pair
    rows_a = indexed.get(sample_a)
    rows_b = indexed.get(sample_b)
    if rows_a is None or rows_b is None:
        raise SystemExit(f"[ERROR] comparison references unknown sample: {sample_a}, {sample_b}")
    keys_a = set(rows_a)
    keys_b = set(rows_b)
    matched = sorted(keys_a & keys_b)
    transition_rows: List[Dict[str, object]] = []
    for key in matched:
        row_a = rows_a[key]
        row_b = rows_b[key]
        value_a = str(row_a.get(level, ""))
        value_b = str(row_b.get(level, ""))
        transition = transform_label(value_a, value_b)
        chrom, start, end = key
        transition_rows.append(
            {
                "comparison": f"{sample_a}_vs_{sample_b}",
                "sample_a": sample_a,
                "sample_b": sample_b,
                "level": level,
                "chr": chrom,
                "start": start,
                "end": end,
                "length": max(0, end - start),
                f"{level}_{sample_a}": value_a,
                f"{level}_{sample_b}": value_b,
                "transform": transition,
                "is_changed": str(transition != "same"),
            }
        )
    overlap = {
        "comparison": f"{sample_a}_vs_{sample_b}",
        "sample_a": sample_a,
        "sample_b": sample_b,
        "level": level,
        "sample_a_bins": len(keys_a),
        "sample_b_bins": len(keys_b),
        "matched_bins": len(matched),
        "sample_a_only_bins": len(keys_a - keys_b),
        "sample_b_only_bins": len(keys_b - keys_a),
        "matched_fraction_of_sample_a": (len(matched) / len(keys_a)) if keys_a else "",
        "matched_fraction_of_sample_b": (len(matched) / len(keys_b)) if keys_b else "",
    }
    return transition_rows, overlap


def summarize_transitions(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    grouped: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    totals: Dict[Tuple[str, str], int] = {}
    length_totals: Dict[Tuple[str, str], int] = {}
    for row in rows:
        comparison = str(row["comparison"])
        level = str(row["level"])
        transform = str(row["transform"])
        length = int(row["length"])
        totals[(comparison, level)] = totals.get((comparison, level), 0) + 1
        length_totals[(comparison, level)] = length_totals.get((comparison, level), 0) + length
        entry = grouped.setdefault(
            (comparison, level, transform),
            {
                "comparison": comparison,
                "level": level,
                "transform": transform,
                "count": 0,
                "total_length": 0,
            },
        )
        entry["count"] = int(entry["count"]) + 1
        entry["total_length"] = int(entry["total_length"]) + length
    summary: List[Dict[str, object]] = []
    for (comparison, level, _), entry in grouped.items():
        total_count = totals.get((comparison, level), 0)
        total_len = length_totals.get((comparison, level), 0)
        entry["count_fraction"] = (int(entry["count"]) / total_count) if total_count else ""
        entry["length_fraction"] = (int(entry["total_length"]) / total_len) if total_len else ""
        summary.append(entry)
    summary.sort(key=lambda r: (str(r["comparison"]), str(r["level"]), -int(r["count"])))
    return summary


def merge_changed_intervals(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    changed = [row for row in rows if str(row.get("is_changed")) == "True"]
    changed.sort(key=lambda r: (str(r["comparison"]), str(r["level"]), str(r["chr"]), int(r["start"]), int(r["end"])))
    merged: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    for row in changed:
        identity = (row["comparison"], row["level"], row["chr"], row["transform"])
        if current:
            current_identity = (current["comparison"], current["level"], current["chr"], current["transform"])
            if identity == current_identity and int(row["start"]) <= int(current["end"]):
                current["end"] = max(int(current["end"]), int(row["end"]))
                current["length"] = int(current["end"]) - int(current["start"])
                current["n_bins"] = int(current["n_bins"]) + 1
                continue
            merged.append(current)
        current = {
            "comparison": row["comparison"],
            "level": row["level"],
            "chr": row["chr"],
            "start": row["start"],
            "end": row["end"],
            "length": row["length"],
            "transform": row["transform"],
            "n_bins": 1,
        }
    if current:
        merged.append(current)
    return merged


def write_report(
    out_dir: Path,
    comparisons: Sequence[Tuple[str, str]],
    levels: Sequence[str],
    transition_summary: Sequence[Dict[str, object]],
    overlaps: Sequence[Dict[str, object]],
) -> None:
    lines = [
        "# Differential Compartment Analysis Report",
        "",
        f"- Comparisons: {', '.join(f'{a}_vs_{b}' for a, b in comparisons)}",
        f"- Levels: {', '.join(levels)}",
        f"- Output directory: {out_dir}",
        "",
        "## Overlap Summary",
        "",
        "| comparison | level | matched_bins | sample_a_only | sample_b_only |",
        "|---|---|---:|---:|---:|",
    ]
    for row in overlaps:
        lines.append(
            f"| {row.get('comparison', '')} | {row.get('level', '')} | {row.get('matched_bins', '')} | "
            f"{row.get('sample_a_only_bins', '')} | {row.get('sample_b_only_bins', '')} |"
        )
    lines.extend(["", "## Top Transitions", "", "| comparison | level | transform | count | total_length |", "|---|---|---|---:|---:|"])
    for row in list(transition_summary)[:20]:
        lines.append(
            f"| {row.get('comparison', '')} | {row.get('level', '')} | {row.get('transform', '')} | "
            f"{row.get('count', '')} | {row.get('total_length', '')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation Notes",
            "",
            "- Transitions are coordinate-matched label changes, not replicate-aware statistical tests.",
            "- A/B sign and sub-compartment direction depend on the upstream compartment calling and feature-track orientation.",
            "- Compare only samples using the same resolution, coordinate system, and chromosome naming convention.",
        ]
    )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize compartment transitions between samples.",
        formatter_class=HelpFormatter,
    )
    parser.add_argument("--sample", nargs=2, action="append", metavar=("SAMPLE", "COMPARTMENT_PATH"))
    parser.add_argument("--sample-sheet", help="TSV/CSV with sample and compartments/path columns.")
    parser.add_argument("--compartments-long", help="Combined compartments_long.tsv from compartment-calling.")
    parser.add_argument("--comparison", nargs=2, action="append", metavar=("SAMPLE_A", "SAMPLE_B"))
    parser.add_argument("--reference", help="Compare this sample against every other sample.")
    parser.add_argument("--level", choices=[*LEVELS, "all"], action="append", default=[])
    parser.add_argument("--resolution-bp", type=int)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--bed-start-one-based",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Treat CALDER2 BED start as 1-based and subtract one, matching the project tutorial.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_all_compartments(args)
    indexed = index_by_sample(rows)
    samples = sorted(indexed)
    if len(samples) < 2:
        raise SystemExit("[ERROR] at least two samples are required")
    comparisons = parse_comparisons(args, samples)
    levels = selected_levels(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_transitions: List[Dict[str, object]] = []
    overlaps: List[Dict[str, object]] = []
    for pair in comparisons:
        pair_transitions: List[Dict[str, object]] = []
        for level in levels:
            transitions, overlap = compare_pair(pair, level, indexed)
            pair_transitions.extend(transitions)
            overlaps.append(overlap)
            prefix = f"{pair[0]}_vs_{pair[1]}_{level}"
            fields = [
                "comparison",
                "sample_a",
                "sample_b",
                "level",
                "chr",
                "start",
                "end",
                "length",
                f"{level}_{pair[0]}",
                f"{level}_{pair[1]}",
                "transform",
                "is_changed",
            ]
            write_tsv(out_dir / f"{prefix}_transitions.tsv", transitions, fields)
        all_transitions.extend(pair_transitions)

    transition_summary = summarize_transitions(all_transitions)
    merged = merge_changed_intervals(all_transitions)
    write_tsv(
        out_dir / "compartment_transition_summary.tsv",
        transition_summary,
        ["comparison", "level", "transform", "count", "total_length", "count_fraction", "length_fraction"],
    )
    write_tsv(
        out_dir / "compartment_pair_overlap_summary.tsv",
        overlaps,
        [
            "comparison",
            "sample_a",
            "sample_b",
            "level",
            "sample_a_bins",
            "sample_b_bins",
            "matched_bins",
            "sample_a_only_bins",
            "sample_b_only_bins",
            "matched_fraction_of_sample_a",
            "matched_fraction_of_sample_b",
        ],
    )
    write_tsv(
        out_dir / "differential_compartments_merged.tsv",
        merged,
        ["comparison", "level", "chr", "start", "end", "length", "transform", "n_bins"],
    )
    write_tsv(
        out_dir / "differential_compartment_run_summary.tsv",
        [
            {
                "samples": ",".join(samples),
                "comparisons": ",".join(f"{a}_vs_{b}" for a, b in comparisons),
                "levels": ",".join(levels),
                "n_input_rows": len(rows),
                "n_transition_rows": len(all_transitions),
                "n_merged_changed_intervals": len(merged),
                "out_dir": str(out_dir),
            }
        ],
        [
            "samples",
            "comparisons",
            "levels",
            "n_input_rows",
            "n_transition_rows",
            "n_merged_changed_intervals",
            "out_dir",
        ],
    )
    write_report(out_dir, comparisons, levels, transition_summary, overlaps)
    print(f"[OK] wrote differential compartment outputs under {out_dir}")


if __name__ == "__main__":
    main()
