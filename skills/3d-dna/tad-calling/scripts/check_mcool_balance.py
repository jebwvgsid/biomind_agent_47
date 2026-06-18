#!/usr/bin/env python
"""Read-only balance preflight for .cool/.mcool matrices.

This script inspects cooler bin weight columns. It never runs ``cooler balance``
and never modifies input matrices.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


DEFAULT_WEIGHT_NAME = "weight"


def import_cooler():
    try:
        import cooler  # type: ignore
    except ImportError as exc:
        raise SystemExit(f"[ERROR] missing dependency: {exc}") from exc
    return cooler


def matrix_base_path(matrix: str) -> str:
    return matrix.split("::", 1)[0]


def parse_resolution_from_uri(matrix: str) -> Optional[int]:
    match = re.search(r"::/resolutions/(\d+)(?:$|/)", matrix)
    return int(match.group(1)) if match else None


def build_matrix_uri(matrix: str, resolution_bp: Optional[int]) -> str:
    if "::" in matrix:
        return matrix
    if matrix.lower().endswith(".mcool"):
        if resolution_bp is None:
            raise ValueError(f"resolution is required for .mcool matrix: {matrix}")
        return f"{matrix}::/resolutions/{resolution_bp}"
    return matrix


def list_resolutions(matrix: str) -> List[int]:
    uri_resolution = parse_resolution_from_uri(matrix)
    if uri_resolution is not None:
        return [uri_resolution]

    base = matrix_base_path(matrix)
    cooler = import_cooler()
    if base.lower().endswith(".mcool"):
        coolers = cooler.fileops.list_coolers(base)
        values: List[int] = []
        for uri in coolers:
            match = re.search(r"/resolutions/(\d+)$", str(uri))
            if match:
                values.append(int(match.group(1)))
        return sorted(set(values))

    clr = cooler.Cooler(matrix)
    if clr.binsize:
        return [int(clr.binsize)]
    return []


def summarize_weights(matrix_uri: str, weight_name: str) -> Dict[str, object]:
    cooler = import_cooler()
    try:
        clr = cooler.Cooler(matrix_uri)
        bin_columns = list(clr.bins().columns)
    except Exception as exc:  # noqa: BLE001
        return {
            "weight_column_present": "no",
            "total_bins": 0,
            "finite_weight_bins": 0,
            "positive_finite_weight_bins": 0,
            "finite_fraction": "0.000000",
            "positive_fraction": "0.000000",
            "balanced": "no",
            "usable_for_3d_genome_analysis": "no",
            "message": f"could not open cooler URI: {exc}",
        }

    if weight_name not in bin_columns:
        return {
            "weight_column_present": "no",
            "total_bins": "",
            "finite_weight_bins": 0,
            "positive_finite_weight_bins": 0,
            "finite_fraction": "0.000000",
            "positive_fraction": "0.000000",
            "balanced": "no",
            "usable_for_3d_genome_analysis": "no",
            "message": f"weight column {weight_name!r} not found; bin columns: {bin_columns}",
        }

    try:
        weights = clr.bins()[weight_name][:]
    except Exception as exc:  # noqa: BLE001
        return {
            "weight_column_present": "yes",
            "total_bins": "",
            "finite_weight_bins": 0,
            "positive_finite_weight_bins": 0,
            "finite_fraction": "0.000000",
            "positive_fraction": "0.000000",
            "balanced": "no",
            "usable_for_3d_genome_analysis": "no",
            "message": f"could not read weight column {weight_name!r}: {exc}",
        }

    total = 0
    finite = 0
    positive = 0
    for value in weights:
        total += 1
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            finite += 1
            if number > 0:
                positive += 1

    finite_fraction = finite / total if total else 0.0
    positive_fraction = positive / total if total else 0.0
    ok = total > 0 and finite > 0 and positive > 0
    if total == 0:
        message = "weight column exists but cooler has zero bins"
    elif ok:
        message = (
            f"weight column {weight_name!r} has finite positive values; "
            "NaN/non-finite bins are reported but do not by themselves fail this read-only check"
        )
    elif finite == 0:
        message = f"weight column {weight_name!r} exists but has no finite values"
    else:
        message = f"weight column {weight_name!r} has finite values but none are >0"

    return {
        "weight_column_present": "yes",
        "total_bins": total,
        "finite_weight_bins": finite,
        "positive_finite_weight_bins": positive,
        "finite_fraction": f"{finite_fraction:.6f}",
        "positive_fraction": f"{positive_fraction:.6f}",
        "balanced": "yes" if ok else "no",
        "usable_for_3d_genome_analysis": "yes" if ok else "no",
        "message": message,
    }


def collect_rows(samples: Sequence[Tuple[str, str]], weight_name: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for sample, matrix in samples:
        base_path = matrix_base_path(matrix)
        if not os.path.exists(base_path):
            rows.append(
                {
                    "sample": sample,
                    "matrix": matrix,
                    "resolution_bp": "",
                    "uri": matrix,
                    "weight_column_present": "no",
                    "total_bins": "",
                    "finite_weight_bins": 0,
                    "positive_finite_weight_bins": 0,
                    "finite_fraction": "0.000000",
                    "positive_fraction": "0.000000",
                    "balanced": "no",
                    "usable_for_3d_genome_analysis": "no",
                    "message": f"input file does not exist: {base_path}",
                }
            )
            continue
        try:
            resolutions = list_resolutions(matrix)
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "sample": sample,
                    "matrix": matrix,
                    "resolution_bp": "",
                    "uri": matrix,
                    "weight_column_present": "no",
                    "total_bins": "",
                    "finite_weight_bins": 0,
                    "positive_finite_weight_bins": 0,
                    "finite_fraction": "0.000000",
                    "positive_fraction": "0.000000",
                    "balanced": "no",
                    "usable_for_3d_genome_analysis": "no",
                    "message": f"could not list cooler resolutions: {exc}",
                }
            )
            continue
        if not resolutions:
            rows.append(
                {
                    "sample": sample,
                    "matrix": matrix,
                    "resolution_bp": "",
                    "uri": matrix,
                    "weight_column_present": "no",
                    "total_bins": "",
                    "finite_weight_bins": 0,
                    "positive_finite_weight_bins": 0,
                    "finite_fraction": "0.000000",
                    "positive_fraction": "0.000000",
                    "balanced": "no",
                    "usable_for_3d_genome_analysis": "no",
                    "message": "no resolutions found",
                }
            )
            continue
        for resolution in resolutions:
            try:
                uri = build_matrix_uri(matrix, resolution)
            except ValueError as exc:
                uri = matrix
                summary = {
                    "weight_column_present": "no",
                    "total_bins": "",
                    "finite_weight_bins": 0,
                    "positive_finite_weight_bins": 0,
                    "finite_fraction": "0.000000",
                    "positive_fraction": "0.000000",
                    "balanced": "no",
                    "usable_for_3d_genome_analysis": "no",
                    "message": str(exc),
                }
            else:
                summary = summarize_weights(uri, weight_name)
            rows.append(
                {
                    "sample": sample,
                    "matrix": matrix,
                    "resolution_bp": resolution,
                    "uri": uri,
                    **summary,
                }
            )
    return rows


def write_tsv(path: Path, rows: Sequence[Dict[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    print(f"[INFO] saved: {path}")


def common_usable_resolutions(rows: Sequence[Dict[str, object]]) -> List[int]:
    by_sample: Dict[str, set[int]] = {}
    for row in rows:
        if row.get("usable_for_3d_genome_analysis") != "yes":
            continue
        try:
            resolution = int(row.get("resolution_bp"))
        except (TypeError, ValueError):
            continue
        by_sample.setdefault(str(row.get("sample")), set()).add(resolution)
    if not by_sample:
        return []
    shared = set.intersection(*by_sample.values())
    return sorted(shared)


def per_sample_usable(rows: Sequence[Dict[str, object]]) -> Dict[str, List[int]]:
    result: Dict[str, List[int]] = {}
    for row in rows:
        sample = str(row.get("sample"))
        if row.get("usable_for_3d_genome_analysis") != "yes":
            result.setdefault(sample, [])
            continue
        try:
            resolution = int(row.get("resolution_bp"))
        except (TypeError, ValueError):
            continue
        result.setdefault(sample, []).append(resolution)
    return {sample: sorted(set(values)) for sample, values in result.items()}


def markdown_table(rows: Sequence[Dict[str, object]]) -> str:
    lines = [
        "| sample | resolution_bp | balanced | finite_weight_bins/total_bins | finite_fraction | positive_finite_weight_bins | message |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for row in rows:
        total = row.get("total_bins", "")
        finite = row.get("finite_weight_bins", "")
        finite_total = f"{finite}/{total}" if total != "" else str(finite)
        message = str(row.get("message", "")).replace("|", "\\|")
        lines.append(
            "| {sample} | {resolution} | {balanced} | {finite_total} | {finite_fraction} | {positive} | {message} |".format(
                sample=row.get("sample", ""),
                resolution=row.get("resolution_bp", ""),
                balanced=row.get("balanced", ""),
                finite_total=finite_total,
                finite_fraction=row.get("finite_fraction", ""),
                positive=row.get("positive_finite_weight_bins", ""),
                message=message,
            )
        )
    return "\n".join(lines)


def write_report(path: Path, rows: Sequence[Dict[str, object]], weight_name: str) -> None:
    usable_common = common_usable_resolutions(rows)
    usable_by_sample = per_sample_usable(rows)
    total_rows = len(rows)
    usable_rows = sum(1 for row in rows if row.get("usable_for_3d_genome_analysis") == "yes")
    all_usable = total_rows > 0 and usable_rows == total_rows
    any_missing_weight = any(row.get("weight_column_present") != "yes" for row in rows)

    lines = [
        "# mcool balance preflight report",
        "",
        "## Summary",
        "",
        f"- Weight column checked: `{weight_name}`",
        f"- Resolution rows checked: {total_rows}",
        f"- Rows with usable finite positive weights: {usable_rows}",
        f"- All checked sample-resolution rows usable: {'yes' if all_usable else 'no'}",
        f"- Any missing weight column or unreadable matrix: {'yes' if any_missing_weight else 'no'}",
        "- Common resolutions usable in every sample: "
        + (", ".join(str(value) for value in usable_common) if usable_common else "none"),
        "",
        "Rule: a resolution is marked usable when the bins table contains the requested weight column, at least one finite weight exists, and at least one finite weight is >0. NaN/non-finite weights are counted and reported, but they do not automatically mean the matrix is unbalanced.",
        "",
        "## Usable Resolutions By Sample",
        "",
    ]
    for sample in sorted(usable_by_sample):
        values = usable_by_sample[sample]
        lines.append(f"- {sample}: " + (", ".join(str(value) for value in values) if values else "none"))
    lines.extend(["", "## Per-Resolution Results", "", markdown_table(rows), ""])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[INFO] saved: {path}")


def write_stage_status(path: Path, rows: Sequence[Dict[str, object]]) -> None:
    usable_common = common_usable_resolutions(rows)
    status = "success" if rows else "failed"
    message = "balance preflight completed"
    if not rows:
        message = "no rows checked"
    elif not usable_common:
        message = "no common usable resolution across all samples"
    stage_rows = [
        {
            "stage": "input_resolution_discovery",
            "status": status,
            "message": f"checked {len(rows)} sample-resolution rows",
        },
        {
            "stage": "balance_weight_preflight",
            "status": status,
            "message": message,
        },
    ]
    write_tsv(path, stage_rows, ["stage", "status", "message"])


def write_reproduce(path: Path, args: argparse.Namespace) -> None:
    script_path = Path(__file__).resolve()
    parts = ["conda", "run", "-n", args.env_name, "python", str(script_path)]
    for sample, matrix in args.sample:
        parts.extend(["--sample", sample, matrix])
    parts.extend(["--out-dir", str(Path(args.out_dir).resolve())])
    parts.extend(["--weight-name", args.weight_name])
    parts.extend(["--env-name", args.env_name])
    command = " ".join(shlex.quote(part) for part in parts)
    text = "#!/usr/bin/env bash\nset -euo pipefail\n\n" + command + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    print(f"[INFO] saved: {path}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect .cool/.mcool balance weight columns across resolutions.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sample",
        nargs=2,
        action="append",
        required=True,
        metavar=("LABEL", "MATRIX"),
        help="Sample label and .cool/.mcool matrix path or full cooler URI. Repeat for multiple samples.",
    )
    parser.add_argument("--out-dir", required=True, help="Directory for report.md, TSV outputs, and reproduce.sh.")
    parser.add_argument("--weight-name", default=DEFAULT_WEIGHT_NAME, help="Cooler bins-table weight column to inspect.")
    parser.add_argument("--env-name", default="hicexplore", help="Environment name written to reproduce.sh.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    rows = collect_rows(args.sample, args.weight_name)
    columns = [
        "sample",
        "matrix",
        "resolution_bp",
        "uri",
        "weight_column_present",
        "total_bins",
        "finite_weight_bins",
        "positive_finite_weight_bins",
        "finite_fraction",
        "positive_fraction",
        "balanced",
        "usable_for_3d_genome_analysis",
        "message",
    ]
    write_tsv(out_dir / "mcool_balance_preflight.tsv", rows, columns)
    write_report(out_dir / "report.md", rows, args.weight_name)
    write_stage_status(out_dir / "stage_status.tsv", rows)
    write_reproduce(out_dir / "reproduce.sh", args)
    usable = common_usable_resolutions(rows)
    print("[INFO] common usable resolutions: " + (", ".join(str(value) for value in usable) if usable else "none"))
    if any("does not exist" in str(row.get("message", "")) for row in rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
