#!/usr/bin/env python3
"""Classify loop anchors by overlap with BED mark tracks."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


REQUIRED_LOOP_COLUMNS = [
    "BIN1_CHR",
    "BIN1_START",
    "BIN1_END",
    "BIN2_CHROMOSOME",
    "BIN2_START",
    "BIN2_END",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify Mustache-like loop calls by anchor overlap with BED marks."
    )
    parser.add_argument("--loops", required=True, help="Mustache-like loop TSV.")
    parser.add_argument(
        "--mark",
        nargs=2,
        action="append",
        metavar=("MARK", "BED"),
        help="Mark name and BED path. Can be repeated.",
    )
    parser.add_argument(
        "--mark-sheet",
        help="TSV/CSV with columns: mark,path. Optional: id_prefix.",
    )
    parser.add_argument("--out-dir", required=True, help="Output directory.")
    parser.add_argument(
        "--overlap-fraction",
        type=float,
        default=0.5,
        help="Minimum overlap fraction. Default matches the teaching example.",
    )
    parser.add_argument(
        "--fraction-target",
        choices=["mark", "anchor", "either", "both"],
        default="mark",
        help="Interval used for the overlap fraction. 'mark' matches bedtools -F.",
    )
    parser.add_argument(
        "--engine",
        choices=["auto", "python", "bedtools"],
        default="auto",
        help="Intersection engine. auto uses bedtools if available, else Python.",
    )
    parser.add_argument("--bedtools-bin", default="bedtools", help="bedtools command.")
    parser.add_argument(
        "--drop-intermediates",
        action="store_true",
        help="Remove loops_bin1.bed, loops_bin2.bed, and marks.bed6 after classification.",
    )
    return parser.parse_args()


def sniff_rows(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    with p.open(newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        return list(csv.DictReader(handle, dialect=dialect))


def load_marks(args: argparse.Namespace) -> list[tuple[str, Path]]:
    marks: list[tuple[str, Path]] = []
    for mark in args.mark or []:
        marks.append((mark[0], Path(mark[1])))
    if args.mark_sheet:
        for row in sniff_rows(args.mark_sheet):
            if not row.get("mark") or not row.get("path"):
                raise SystemExit("[ERROR] mark sheet requires mark and path columns")
            marks.append((row["mark"], Path(row["path"])))
    if not marks:
        raise SystemExit("[ERROR] provide --mark or --mark-sheet")
    for name, path in marks:
        if not path.exists():
            raise SystemExit(f"[ERROR] mark BED not found for {name}: {path}")
    return marks


def read_loops(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames:
            raise SystemExit("[ERROR] loop file has no header")
        missing = [c for c in REQUIRED_LOOP_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise SystemExit(f"[ERROR] loop file missing required columns: {', '.join(missing)}")
        rows = list(reader)
    for i, row in enumerate(rows, start=1):
        row["loop_id"] = row.get("loop_id") or row.get("LOOP_ID") or f"Loop{i}"
    return rows


def write_tsv(path: Path, rows: Iterable[dict[str, str]], fieldnames: list[str] | None = None) -> None:
    rows = list(rows)
    if fieldnames is None and rows:
        fieldnames = list(rows[0].keys())
    if not fieldnames:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_anchor_beds(loops: list[dict[str, str]], out_dir: Path) -> tuple[Path, Path]:
    bin1_path = out_dir / "loops_bin1.bed"
    bin2_path = out_dir / "loops_bin2.bed"
    with bin1_path.open("w", encoding="utf-8") as bin1, bin2_path.open("w", encoding="utf-8") as bin2:
        for row in loops:
            bin1.write(
                f"{row['BIN1_CHR']}\t{row['BIN1_START']}\t{row['BIN1_END']}\t{row['loop_id']}\n"
            )
            bin2.write(
                f"{row['BIN2_CHROMOSOME']}\t{row['BIN2_START']}\t{row['BIN2_END']}\t{row['loop_id']}\n"
            )
    return bin1_path, bin2_path


def build_marks_bed6(marks: list[tuple[str, Path]], out_dir: Path) -> Path:
    out_path = out_dir / "marks.bed6"
    with out_path.open("w", encoding="utf-8") as out:
        for mark_name, bed_path in marks:
            count = 0
            with bed_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip() or line.startswith("#"):
                        continue
                    fields = line.rstrip("\n").split("\t")
                    if len(fields) < 3:
                        continue
                    try:
                        int(fields[1])
                        int(fields[2])
                    except ValueError:
                        continue
                    count += 1
                    peak_id = fields[3] if len(fields) >= 4 and fields[3] else f"{mark_name}_{count}"
                    out.write("\t".join([fields[0], fields[1], fields[2], peak_id, mark_name]) + "\n")
    return out_path


def overlap_len(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def overlap_passes(
    overlap: int,
    anchor_len: int,
    mark_len: int,
    fraction: float,
    target: str,
) -> bool:
    if overlap <= 0:
        return False
    anchor_frac = overlap / anchor_len if anchor_len else 0
    mark_frac = overlap / mark_len if mark_len else 0
    if target == "anchor":
        return anchor_frac >= fraction
    if target == "mark":
        return mark_frac >= fraction
    if target == "either":
        return anchor_frac >= fraction or mark_frac >= fraction
    if target == "both":
        return anchor_frac >= fraction and mark_frac >= fraction
    raise ValueError(target)


def read_bed(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 4:
                rows.append(fields)
    return rows


def intersect_python(
    anchor_bed: Path,
    marks_bed: Path,
    side: str,
    fraction: float,
    target: str,
) -> list[dict[str, str]]:
    anchors_by_chrom: dict[str, list[list[str]]] = defaultdict(list)
    for row in read_bed(anchor_bed):
        anchors_by_chrom[row[0]].append(row)
    marks_by_chrom: dict[str, list[list[str]]] = defaultdict(list)
    for row in read_bed(marks_bed):
        marks_by_chrom[row[0]].append(row)
    output: list[dict[str, str]] = []
    for chrom, anchors in anchors_by_chrom.items():
        marks = marks_by_chrom.get(chrom, [])
        for anchor in anchors:
            a_start, a_end = int(anchor[1]), int(anchor[2])
            for mark in marks:
                b_start, b_end = int(mark[1]), int(mark[2])
                overlap = overlap_len(a_start, a_end, b_start, b_end)
                if overlap_passes(overlap, a_end - a_start, b_end - b_start, fraction, target):
                    output.append(
                        {
                            "loop_id": anchor[3],
                            "side": side,
                            "anchor_chr": anchor[0],
                            "anchor_start": anchor[1],
                            "anchor_end": anchor[2],
                            "mark_chr": mark[0],
                            "mark_start": mark[1],
                            "mark_end": mark[2],
                            "mark_id": mark[3],
                            "mark_type": mark[4],
                            "overlap_bp": str(overlap),
                        }
                    )
    return output


def bedtools_args_for_policy(fraction: float, target: str) -> list[str]:
    if target == "mark":
        return ["-F", str(fraction)]
    if target == "anchor":
        return ["-f", str(fraction)]
    if target == "either":
        return ["-f", str(fraction), "-F", str(fraction), "-e"]
    if target == "both":
        return ["-f", str(fraction), "-F", str(fraction)]
    raise ValueError(target)


def intersect_bedtools(
    anchor_bed: Path,
    marks_bed: Path,
    side: str,
    args: argparse.Namespace,
) -> list[dict[str, str]]:
    cmd = [
        args.bedtools_bin,
        "intersect",
        "-a",
        str(anchor_bed),
        "-b",
        str(marks_bed),
        *bedtools_args_for_policy(args.overlap_fraction, args.fraction_target),
        "-wa",
        "-wb",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    rows: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 9:
            continue
        overlap = overlap_len(int(fields[1]), int(fields[2]), int(fields[5]), int(fields[6]))
        rows.append(
            {
                "loop_id": fields[3],
                "side": side,
                "anchor_chr": fields[0],
                "anchor_start": fields[1],
                "anchor_end": fields[2],
                "mark_chr": fields[4],
                "mark_start": fields[5],
                "mark_end": fields[6],
                "mark_id": fields[7],
                "mark_type": fields[8],
                "overlap_bp": str(overlap),
            }
        )
    return rows


def choose_engine(args: argparse.Namespace) -> str:
    if args.engine == "python":
        return "python"
    if args.engine == "bedtools":
        if not shutil.which(args.bedtools_bin):
            raise SystemExit(f"[ERROR] {args.bedtools_bin!r} not found on PATH")
        return "bedtools"
    return "bedtools" if shutil.which(args.bedtools_bin) else "python"


def anchor_role(marks: set[str]) -> str:
    if "H3K4me3" in marks:
        return "promoter"
    if "H3K27ac" in marks and "H3K4me1" in marks:
        return "active_enhancer"
    if "H3K27ac" in marks or "H3K4me1" in marks:
        return "enhancer"
    if marks:
        return "marked"
    return "unmarked"


def join_marks(marks: set[str]) -> str:
    return ";".join(sorted(marks)) if marks else "none"


def classify_loops(
    loops: list[dict[str, str]],
    overlaps: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    marks_by_loop_side: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in overlaps:
        marks_by_loop_side[(row["loop_id"], row["side"])].add(row["mark_type"])
    classified: list[dict[str, str]] = []
    class_counts: Counter[str] = Counter()
    for row in loops:
        loop_id = row["loop_id"]
        bin1_marks = marks_by_loop_side[(loop_id, "bin1")]
        bin2_marks = marks_by_loop_side[(loop_id, "bin2")]
        bin1_role = anchor_role(bin1_marks)
        bin2_role = anchor_role(bin2_marks)
        role_pair = sorted([bin1_role, bin2_role])
        loop_class = f"{role_pair[0]}-{role_pair[1]}"
        try:
            distance = int(float(row["BIN2_START"])) - int(float(row["BIN1_START"]))
        except ValueError:
            distance = ""
        enriched = dict(row)
        enriched.update(
            {
                "bin1_marks": join_marks(bin1_marks),
                "bin2_marks": join_marks(bin2_marks),
                "bin1_role": bin1_role,
                "bin2_role": bin2_role,
                "loop_class": loop_class,
                "mark_pair": f"{join_marks(bin1_marks)}|{join_marks(bin2_marks)}",
                "distance_bp": str(distance),
            }
        )
        class_counts[loop_class] += 1
        classified.append(enriched)
    summary = [
        {"loop_class": loop_class, "count": str(count)}
        for loop_class, count in sorted(class_counts.items())
    ]
    return classified, summary


def main() -> None:
    args = parse_args()
    loops = read_loops(Path(args.loops))
    marks = load_marks(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bin1_bed, bin2_bed = write_anchor_beds(loops, out_dir)
    marks_bed = build_marks_bed6(marks, out_dir)
    engine = choose_engine(args)
    if engine == "bedtools":
        overlaps = intersect_bedtools(bin1_bed, marks_bed, "bin1", args)
        overlaps.extend(intersect_bedtools(bin2_bed, marks_bed, "bin2", args))
    else:
        overlaps = intersect_python(
            bin1_bed, marks_bed, "bin1", args.overlap_fraction, args.fraction_target
        )
        overlaps.extend(
            intersect_python(bin2_bed, marks_bed, "bin2", args.overlap_fraction, args.fraction_target)
        )

    classified, summary = classify_loops(loops, overlaps)
    write_tsv(out_dir / "anchor_mark_overlaps.tsv", overlaps)
    write_tsv(out_dir / "classification_summary.tsv", summary, ["loop_class", "count"])
    fieldnames = list(classified[0].keys()) if classified else []
    write_tsv(out_dir / "loops_anchors.tsv", classified, fieldnames)

    if args.drop_intermediates:
        for path in (bin1_bed, bin2_bed, marks_bed):
            path.unlink(missing_ok=True)
    print(f"[OK] classified {len(loops)} loops with {len(overlaps)} anchor-mark overlaps using {engine}")


if __name__ == "__main__":
    main()
