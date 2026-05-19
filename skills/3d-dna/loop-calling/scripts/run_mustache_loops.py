#!/usr/bin/env python3
"""Run Mustache loop calling and optional differential loop analysis.

This wrapper keeps sample naming, output layout, and summary tables stable for
BioMind workflows while leaving native Mustache outputs untouched.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Iterable


LOOP_COLUMNS = {
    "BIN1_CHR",
    "BIN1_START",
    "BIN1_END",
    "BIN2_CHROMOSOME",
    "BIN2_START",
    "BIN2_END",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Mustache loop calling and summarize loop outputs."
    )
    parser.add_argument(
        "--sample",
        nargs=2,
        action="append",
        metavar=("SAMPLE", "MATRIX"),
        help="Sample label and .cool/.mcool matrix path. Can be repeated.",
    )
    parser.add_argument(
        "--sample-sheet",
        help="TSV/CSV with columns: sample,matrix. Optional: resolution_bp.",
    )
    parser.add_argument(
        "--diff",
        nargs=2,
        action="append",
        metavar=("SAMPLE1", "SAMPLE2"),
        help="Run diff_mustache.py for a pair of named samples. Can be repeated.",
    )
    parser.add_argument(
        "--pair-sheet",
        help="TSV/CSV with columns: sample1,sample2. Optional: prefix.",
    )
    parser.add_argument("--resolution-bp", type=int, help="Matrix resolution in bp.")
    parser.add_argument("--out-root", default="Loops/Mustache", help="Output root.")
    parser.add_argument("--processes", type=int, default=6, help="Mustache processes.")
    parser.add_argument("--pt", type=float, default=0.05, help="Mustache -pt value.")
    parser.add_argument(
        "--pt2",
        type=float,
        default=None,
        help="Mustache diff -pt2 value. Defaults to --pt.",
    )
    parser.add_argument("--st", type=float, default=0.88, help="Mustache -st value.")
    parser.add_argument("--mustache-bin", default="mustache", help="Mustache command.")
    parser.add_argument(
        "--diff-script",
        help="Path to diff_mustache.py. Auto-detected from PATH or Python package.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to mustache calls. Repeat for multiple tokens.",
    )
    parser.add_argument(
        "--diff-extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to diff_mustache.py. Repeat for multiple tokens.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip native Mustache calls when expected output already exists.",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Only summarize existing outputs under the resolution directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands and write run summary without executing Mustache.",
    )
    return parser.parse_args()


def read_rows(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    with p.open(newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        return list(csv.DictReader(handle, dialect=dialect))


def load_samples(args: argparse.Namespace) -> dict[str, dict[str, str]]:
    samples: dict[str, dict[str, str]] = {}
    for item in args.sample or []:
        label, matrix = item
        samples[label] = {"sample": label, "matrix": matrix}
    if args.sample_sheet:
        for row in read_rows(args.sample_sheet):
            if not row.get("sample") or not row.get("matrix"):
                raise SystemExit("[ERROR] sample sheet requires sample and matrix columns")
            samples[row["sample"]] = row
    return samples


def load_pairs(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    for sample1, sample2 in args.diff or []:
        pairs.append((sample1, sample2, f"{sample1}_vs_{sample2}"))
    if args.pair_sheet:
        for row in read_rows(args.pair_sheet):
            if not row.get("sample1") or not row.get("sample2"):
                raise SystemExit("[ERROR] pair sheet requires sample1 and sample2 columns")
            prefix = row.get("prefix") or f"{row['sample1']}_vs_{row['sample2']}"
            pairs.append((row["sample1"], row["sample2"], prefix))
    return pairs


def effective_resolution(row: dict[str, str], global_resolution: int | None) -> int:
    value = row.get("resolution_bp") or global_resolution
    if not value:
        raise SystemExit("[ERROR] --resolution-bp is required unless every sample row has resolution_bp")
    return int(value)


def find_diff_script(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    found = shutil.which("diff_mustache.py")
    if found:
        return Path(found)
    spec = importlib.util.find_spec("mustache")
    if spec and spec.origin:
        candidate = Path(spec.origin).resolve().parent / "diff_mustache.py"
        if candidate.exists():
            return candidate
    return None


def run_command(cmd: list[str], dry_run: bool, log_path: Path) -> tuple[str, int, str]:
    rendered = " ".join(cmd)
    if dry_run:
        log_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return rendered, 0, "dry-run"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    status = "ok" if proc.returncode == 0 else "failed"
    return rendered, proc.returncode, status


def call_loops(
    args: argparse.Namespace,
    samples: dict[str, dict[str, str]],
    out_dir: Path,
) -> list[dict[str, str]]:
    if args.stats_only:
        return []
    if not shutil.which(args.mustache_bin):
        raise SystemExit(f"[ERROR] {args.mustache_bin!r} not found on PATH")

    rows: list[dict[str, str]] = []
    for label, row in samples.items():
        resolution = effective_resolution(row, args.resolution_bp)
        if args.resolution_bp and resolution != args.resolution_bp:
            raise SystemExit("[ERROR] mixed resolutions in one run are not supported")
        outfile = out_dir / f"{label}.tsv"
        if outfile.exists() and args.skip_existing:
            rows.append(
                {
                    "step": "call",
                    "sample1": label,
                    "sample2": "",
                    "resolution_bp": str(resolution),
                    "output": str(outfile),
                    "status": "skipped_existing",
                    "return_code": "0",
                    "command": "",
                }
            )
            continue
        cmd = [
            args.mustache_bin,
            "--file",
            row["matrix"],
            "--resolution",
            str(resolution),
            "--outfile",
            str(outfile),
            "--processes",
            str(args.processes),
            "-pt",
            str(args.pt),
            "-st",
            str(args.st),
        ] + args.extra_arg
        command, return_code, status = run_command(cmd, args.dry_run, out_dir / f"{label}.mustache.log")
        rows.append(
            {
                "step": "call",
                "sample1": label,
                "sample2": "",
                "resolution_bp": str(resolution),
                "output": str(outfile),
                "status": status,
                "return_code": str(return_code),
                "command": command,
            }
        )
        if return_code != 0:
            raise SystemExit(f"[ERROR] Mustache failed for {label}; see {out_dir / (label + '.mustache.log')}")
    return rows


def call_differential(
    args: argparse.Namespace,
    samples: dict[str, dict[str, str]],
    pairs: list[tuple[str, str, str]],
    out_dir: Path,
) -> list[dict[str, str]]:
    if args.stats_only or not pairs:
        return []
    script = find_diff_script(args.diff_script)
    if not script:
        raise SystemExit("[ERROR] diff_mustache.py not found; pass --diff-script")
    rows: list[dict[str, str]] = []
    for sample1, sample2, prefix in pairs:
        if sample1 not in samples or sample2 not in samples:
            raise SystemExit(f"[ERROR] pair references unknown sample: {sample1}, {sample2}")
        row1 = samples[sample1]
        row2 = samples[sample2]
        resolution1 = effective_resolution(row1, args.resolution_bp)
        resolution2 = effective_resolution(row2, args.resolution_bp)
        if resolution1 != resolution2:
            raise SystemExit(f"[ERROR] pair has mixed resolutions: {sample1}, {sample2}")
        out_prefix = out_dir / prefix
        expected = out_dir / f"{prefix}.diffloop1"
        if expected.exists() and args.skip_existing:
            status = "skipped_existing"
            command = ""
            return_code = 0
        else:
            cmd = [
                sys.executable,
                str(script),
                "-f1",
                row1["matrix"],
                "-f2",
                row2["matrix"],
                "--resolution",
                str(resolution1),
                "--outfile",
                str(out_prefix),
                "--processes",
                str(args.processes),
                "-pt",
                str(args.pt),
                "-pt2",
                str(args.pt2 if args.pt2 is not None else args.pt),
                "-st",
                str(args.st),
            ] + args.diff_extra_arg
            command, return_code, status = run_command(cmd, args.dry_run, out_dir / f"{prefix}.diff_mustache.log")
        rows.append(
            {
                "step": "diff",
                "sample1": sample1,
                "sample2": sample2,
                "resolution_bp": str(resolution1),
                "output": str(out_prefix),
                "status": status,
                "return_code": str(return_code),
                "command": command,
            }
        )
        if return_code != 0:
            raise SystemExit(f"[ERROR] diff_mustache failed for {prefix}; see {out_dir / (prefix + '.diff_mustache.log')}")
    return rows


def parse_loop_file(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or not LOOP_COLUMNS.issubset(set(reader.fieldnames)):
            return []
        return list(reader)


def as_float(row: dict[str, str], key: str) -> float | None:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def summarize_loop_files(out_dir: Path) -> None:
    distance_rows: list[dict[str, str]] = []
    stat_rows: list[dict[str, str]] = []
    candidates = sorted(
        p for p in out_dir.iterdir() if p.is_file() and (p.suffix == ".tsv" or ".loop" in p.name)
    )
    for path in candidates:
        if path.name in {"loop_run_summary.tsv", "loop_stats.tsv", "loop_distances.tsv"}:
            continue
        loops = parse_loop_file(path)
        if not loops:
            continue
        distances: list[int] = []
        fdrs: list[float] = []
        scales: list[float] = []
        for idx, row in enumerate(loops, start=1):
            try:
                distance = int(float(row["BIN2_START"])) - int(float(row["BIN1_START"]))
            except (KeyError, ValueError):
                continue
            distances.append(distance)
            fdr = as_float(row, "FDR")
            scale = as_float(row, "DETECTION_SCALE")
            if fdr is not None:
                fdrs.append(fdr)
            if scale is not None:
                scales.append(scale)
            distance_rows.append(
                {
                    "source_file": path.name,
                    "loop_index": str(idx),
                    "chrom1": row.get("BIN1_CHR", ""),
                    "start1": row.get("BIN1_START", ""),
                    "chrom2": row.get("BIN2_CHROMOSOME", ""),
                    "start2": row.get("BIN2_START", ""),
                    "distance_bp": str(distance),
                    "fdr": row.get("FDR", ""),
                    "detection_scale": row.get("DETECTION_SCALE", ""),
                }
            )
        stat_rows.append(
            {
                "source_file": path.name,
                "loop_count": str(len(loops)),
                "median_distance_bp": str(int(statistics.median(distances))) if distances else "",
                "mean_distance_bp": f"{statistics.mean(distances):.3f}" if distances else "",
                "min_distance_bp": str(min(distances)) if distances else "",
                "max_distance_bp": str(max(distances)) if distances else "",
                "median_fdr": f"{statistics.median(fdrs):.6g}" if fdrs else "",
                "median_detection_scale": f"{statistics.median(scales):.6g}" if scales else "",
            }
        )
    write_tsv(out_dir / "loop_distances.tsv", distance_rows)
    write_tsv(out_dir / "loop_stats.tsv", stat_rows)


def write_tsv(path: Path, rows: Iterable[dict[str, str]]) -> None:
    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    samples = load_samples(args)
    pairs = load_pairs(args)
    if not samples and not args.stats_only:
        raise SystemExit("[ERROR] provide --sample, --sample-sheet, or --stats-only")
    if args.resolution_bp is None and samples:
        resolutions = {row.get("resolution_bp") for row in samples.values() if row.get("resolution_bp")}
        if len(resolutions) != 1:
            raise SystemExit("[ERROR] provide --resolution-bp or one shared resolution_bp in the sample sheet")
        args.resolution_bp = int(next(iter(resolutions)))
    resolution_label = str(args.resolution_bp or "existing")
    out_dir = Path(args.out_root) / resolution_label
    out_dir.mkdir(parents=True, exist_ok=True)

    run_rows = []
    run_rows.extend(call_loops(args, samples, out_dir))
    run_rows.extend(call_differential(args, samples, pairs, out_dir))
    write_tsv(out_dir / "loop_run_summary.tsv", run_rows)
    summarize_loop_files(out_dir)
    print(f"[OK] wrote outputs under {out_dir}")


if __name__ == "__main__":
    main()
