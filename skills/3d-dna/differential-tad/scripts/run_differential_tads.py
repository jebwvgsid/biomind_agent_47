#!/usr/bin/env python
"""Run HiCExplorer hicDifferentialTAD and summarize accepted/rejected outputs."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


MODES = ("intra-TAD", "left-inter-TAD", "right-inter-TAD", "all")
MODE_REJECTS = ("one", "all")
DEFAULT_OUT_ROOT = "TADs/HiCExplorer"
DEFAULT_P_VALUE = 0.05
DEFAULT_THREADS = 4


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
    return cleaned.strip("._-") or "sample"


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
            raise ValueError(f"--resolution-bp is required for plain .mcool input: {matrix}")
        return f"{matrix}::/resolutions/{resolution_bp}"
    return matrix


def effective_resolution_label(target_matrix: str, control_matrix: str, resolution_bp: Optional[int]) -> str:
    target_res = parse_resolution_from_uri(target_matrix)
    control_res = parse_resolution_from_uri(control_matrix)
    seen = [value for value in (target_res, control_res, resolution_bp) if value is not None]
    if seen and any(value != seen[0] for value in seen):
        raise ValueError(
            "target/control resolution mismatch: "
            f"target={target_res}, control={control_res}, --resolution-bp={resolution_bp}"
        )
    return str(seen[0]) if seen else "native"


def require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"{label} not found or empty: {path}")


def expected_diff_outputs(prefix: Path) -> Dict[str, Path]:
    return {
        "accepted": Path(f"{prefix}_accepted.diff_tad"),
        "rejected": Path(f"{prefix}_rejected.diff_tad"),
    }


def infer_domains(out_root: Path, resolution_label: str, sample_label: str) -> Path:
    return out_root / resolution_label / f"{safe_label(sample_label)}_domains.bed"


def split_tsv_or_ws(line: str) -> List[str]:
    parts = line.rstrip("\n").split("\t")
    if len(parts) == 1:
        parts = line.strip().split()
    return parts


def normalize_header(parts: Sequence[str]) -> List[str]:
    header = [part.strip() for part in parts]
    if header:
        header[0] = header[0].lstrip("#").strip() or "Chromosome"
    seen: Dict[str, int] = {}
    normalized = []
    for idx, col in enumerate(header, start=1):
        name = col or f"col_{idx}"
        count = seen.get(name, 0)
        seen[name] = count + 1
        normalized.append(name if count == 0 else f"{name}_{count + 1}")
    return normalized


def default_header(n_cols: int) -> List[str]:
    known = [
        "Chromosome",
        "start",
        "end",
        "name",
        "score",
        "strand",
        "p-value left-inter-TAD",
        "p-value right-inter-TAD",
        "p-value intra-TAD",
    ]
    if n_cols <= len(known):
        return known[:n_cols]
    return known + [f"col_{idx}" for idx in range(len(known) + 1, n_cols + 1)]


def read_diff_tad(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    if not path.is_file():
        return [], []
    header: Optional[List[str]] = None
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            parts = split_tsv_or_ws(raw_line)
            first = parts[0].lstrip("#").strip().lower() if parts else ""
            if raw_line.lstrip().startswith("#"):
                if first in {"chromosome", "chr"} or "chromosome" in first:
                    header = normalize_header(parts)
                continue
            if header is None:
                header = default_header(len(parts))
            if len(parts) > len(header):
                header = list(header) + [f"col_{idx}" for idx in range(len(header) + 1, len(parts) + 1)]
            row = {header[idx]: value for idx, value in enumerate(parts)}
            rows.append(row)
    return header or [], rows


def parse_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pvalue_columns(header: Iterable[str], mode: str) -> List[str]:
    mode_key = mode.lower()
    return [
        col
        for col in header
        if "p-value" in col.lower() and mode_key in col.lower()
    ]


def count_mode_hits(rows: Sequence[Dict[str, str]], columns: Sequence[str], p_value: float) -> int:
    if not columns:
        return 0
    count = 0
    for row in rows:
        for col in columns:
            value = parse_float(row.get(col, ""))
            if value is not None and value <= p_value:
                count += 1
                break
    return count


def write_tsv(path: Path, rows: Sequence[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_command(cmd: Sequence[str], log_path: Path, dry_run: bool = False) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("Command:\n")
        log.write(" ".join(cmd) + "\n\n")
        if dry_run:
            log.write("DRY RUN: command not executed.\n")
            return 0
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    return result.returncode


def build_hicdiff_command(
    executable: str,
    target_matrix: str,
    control_matrix: str,
    tad_domains: Path,
    out_prefix: Path,
    mode: str,
    mode_reject: str,
    p_value: float,
    threads: int,
    extra_args: Sequence[str],
) -> List[str]:
    return [
        executable,
        "--targetMatrix",
        target_matrix,
        "--controlMatrix",
        control_matrix,
        "--tadDomains",
        str(tad_domains),
        "--outFileNamePrefix",
        str(out_prefix),
        "--mode",
        mode,
        "--modeReject",
        mode_reject,
        "--pValue",
        str(p_value),
        "--threads",
        str(threads),
        *extra_args,
    ]


def comparison_plan(
    target: Tuple[str, str],
    control: Tuple[str, str],
    target_domains: Path,
    control_domains: Optional[Path],
    out_root: Path,
    resolution_label: str,
    bidirectional: bool,
) -> List[Dict[str, object]]:
    target_label, target_matrix = target
    control_label, control_matrix = control
    output_dir = out_root / resolution_label

    plan: List[Dict[str, object]] = [
        {
            "direction": "target_vs_control",
            "target_label": target_label,
            "control_label": control_label,
            "target_matrix": target_matrix,
            "control_matrix": control_matrix,
            "domains": target_domains,
            "prefix": output_dir / f"{safe_label(target_label)}_vs_{safe_label(control_label)}",
        }
    ]

    if bidirectional:
        reverse_domains = control_domains or infer_domains(out_root, resolution_label, control_label)
        plan.append(
            {
                "direction": "control_vs_target",
                "target_label": control_label,
                "control_label": target_label,
                "target_matrix": control_matrix,
                "control_matrix": target_matrix,
                "domains": reverse_domains,
                "prefix": output_dir / f"{safe_label(control_label)}_vs_{safe_label(target_label)}",
            }
        )
    return plan


def summarize_outputs(
    completed: Sequence[Dict[str, object]],
    summary_path: Path,
    long_path: Path,
    p_value: float,
) -> None:
    summary_rows: List[Dict[str, object]] = []
    long_rows: List[Dict[str, object]] = []
    long_fields: List[str] = [
        "comparison",
        "direction",
        "target",
        "control",
        "source_file",
        "result_type",
    ]

    for item in completed:
        outputs = expected_diff_outputs(Path(str(item["prefix"])))
        comparison = f"{item['target_label']}_vs_{item['control_label']}"
        for result_type, path in outputs.items():
            header, rows = read_diff_tad(path)
            intra_cols = pvalue_columns(header, "intra-TAD")
            left_cols = pvalue_columns(header, "left-inter-TAD")
            right_cols = pvalue_columns(header, "right-inter-TAD")
            summary_rows.append(
                {
                    "comparison": comparison,
                    "direction": item["direction"],
                    "target": item["target_label"],
                    "control": item["control_label"],
                    "result_type": result_type,
                    "records": len(rows),
                    "n_intra_tad_p_le_threshold": count_mode_hits(rows, intra_cols, p_value),
                    "n_left_inter_tad_p_le_threshold": count_mode_hits(rows, left_cols, p_value),
                    "n_right_inter_tad_p_le_threshold": count_mode_hits(rows, right_cols, p_value),
                    "source_file": str(path),
                }
            )
            if result_type == "rejected":
                for col in header:
                    if col not in long_fields:
                        long_fields.append(col)
                for row in rows:
                    long_rows.append(
                        {
                            "comparison": comparison,
                            "direction": item["direction"],
                            "target": item["target_label"],
                            "control": item["control_label"],
                            "source_file": str(path),
                            "result_type": result_type,
                            **row,
                        }
                    )

    write_tsv(
        summary_path,
        summary_rows,
        [
            "comparison",
            "direction",
            "target",
            "control",
            "result_type",
            "records",
            "n_intra_tad_p_le_threshold",
            "n_left_inter_tad_p_le_threshold",
            "n_right_inter_tad_p_le_threshold",
            "source_file",
        ],
    )
    write_tsv(long_path, long_rows, long_fields)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hicDifferentialTAD for one target/control pair, optionally bidirectional.",
        formatter_class=HelpFormatter,
    )
    parser.add_argument("--target", nargs=2, metavar=("LABEL", "MATRIX"), required=True, help="Target sample label and matrix path/URI.")
    parser.add_argument("--control", nargs=2, metavar=("LABEL", "MATRIX"), required=True, help="Control sample label and matrix path/URI.")
    parser.add_argument("--tad-domains", required=True, type=Path, help="TAD domains BED corresponding to the target sample.")
    parser.add_argument("--control-tad-domains", type=Path, help="TAD domains BED for the control sample, required for bidirectional runs unless inferable.")
    parser.add_argument("--bidirectional", action="store_true", help="Run target_vs_control and control_vs_target.")
    parser.add_argument("--out-root", default=DEFAULT_OUT_ROOT, type=Path, help="Root output directory; resolution subfolder is created below it.")
    parser.add_argument("--resolution-bp", type=int, help="Resolution for plain .mcool paths; inferred from matrix URI when present.")
    parser.add_argument("--mode", choices=MODES, default="all", help="hicDifferentialTAD mode.")
    parser.add_argument("--mode-reject", choices=MODE_REJECTS, default="one", help="hicDifferentialTAD --modeReject value.")
    parser.add_argument("--p-value", type=float, default=DEFAULT_P_VALUE, help="hicDifferentialTAD --pValue threshold.")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="hicDifferentialTAD --threads.")
    parser.add_argument("--hic-differential-tad", default="hicDifferentialTAD", help="Path/name of hicDifferentialTAD executable.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip a comparison when accepted/rejected outputs already exist.")
    parser.add_argument("--force", action="store_true", help="Allow existing accepted/rejected outputs to be overwritten by hicDifferentialTAD.")
    parser.add_argument("--dry-run", action="store_true", help="Print/log commands and preflight only; do not run hicDifferentialTAD.")
    parser.add_argument("--extra-arg", action="append", default=[], help="Additional hicDifferentialTAD argument; repeat for each token.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    executable = shutil.which(args.hic_differential_tad) or (args.hic_differential_tad if Path(args.hic_differential_tad).is_file() else "")
    if not executable and not args.dry_run:
        print(f"ERROR: hicDifferentialTAD not found on PATH: {args.hic_differential_tad}", file=sys.stderr)
        return 1
    executable = executable or args.hic_differential_tad

    try:
        target_label, target_matrix_raw = args.target
        control_label, control_matrix_raw = args.control
        resolution_label = effective_resolution_label(target_matrix_raw, control_matrix_raw, args.resolution_bp)
        target_matrix = build_matrix_uri(target_matrix_raw, args.resolution_bp)
        control_matrix = build_matrix_uri(control_matrix_raw, args.resolution_bp)

        require_file(Path(matrix_base_path(target_matrix)), "target matrix")
        require_file(Path(matrix_base_path(control_matrix)), "control matrix")
        require_file(args.tad_domains, "target TAD domains")

        plan = comparison_plan(
            (target_label, target_matrix),
            (control_label, control_matrix),
            args.tad_domains,
            args.control_tad_domains,
            args.out_root,
            resolution_label,
            args.bidirectional,
        )
        if args.bidirectional:
            reverse_domains = Path(str(plan[1]["domains"]))
            require_file(reverse_domains, "control TAD domains for bidirectional run")

        output_dir = args.out_root / resolution_label
        output_dir.mkdir(parents=True, exist_ok=True)
        run_summary_path = output_dir / "differential_tad_run_summary.tsv"
        stats_path = output_dir / "differential_tad_summary.tsv"
        long_path = output_dir / "differential_tads_rejected_long.tsv"
        run_rows: List[Dict[str, object]] = []
        completed: List[Dict[str, object]] = []

        for item in plan:
            prefix = Path(str(item["prefix"]))
            outputs = expected_diff_outputs(prefix)
            existing = [path for path in outputs.values() if path.exists()]
            status = "pending"
            return_code: Optional[int] = None
            message = ""

            if existing and not args.force:
                if args.skip_existing and all(path.is_file() for path in outputs.values()):
                    status = "skipped_existing"
                    completed.append(item)
                else:
                    raise FileExistsError(
                        f"output already exists for prefix {prefix}; use --skip-existing or --force"
                    )
            else:
                cmd = build_hicdiff_command(
                    executable,
                    str(item["target_matrix"]),
                    str(item["control_matrix"]),
                    Path(str(item["domains"])),
                    prefix,
                    args.mode,
                    args.mode_reject,
                    args.p_value,
                    args.threads,
                    args.extra_arg,
                )
                log_path = Path(f"{prefix}.hicDifferentialTAD.log")
                print("Running:", " ".join(cmd))
                return_code = run_command(cmd, log_path, dry_run=args.dry_run)
                if return_code == 0:
                    status = "dry_run" if args.dry_run else "completed"
                    if not args.dry_run:
                        for label, path in outputs.items():
                            require_file(path, f"{label} diff_tad output")
                        completed.append(item)
                else:
                    status = "failed"
                    message = f"hicDifferentialTAD exited with code {return_code}; see {log_path}"

            run_rows.append(
                {
                    "comparison": f"{item['target_label']}_vs_{item['control_label']}",
                    "direction": item["direction"],
                    "target": item["target_label"],
                    "control": item["control_label"],
                    "target_matrix": item["target_matrix"],
                    "control_matrix": item["control_matrix"],
                    "tad_domains": item["domains"],
                    "out_prefix": prefix,
                    "accepted_file": outputs["accepted"],
                    "rejected_file": outputs["rejected"],
                    "mode": args.mode,
                    "mode_reject": args.mode_reject,
                    "p_value": args.p_value,
                    "threads": args.threads,
                    "status": status,
                    "return_code": "" if return_code is None else return_code,
                    "message": message,
                }
            )

        write_tsv(
            run_summary_path,
            run_rows,
            [
                "comparison",
                "direction",
                "target",
                "control",
                "target_matrix",
                "control_matrix",
                "tad_domains",
                "out_prefix",
                "accepted_file",
                "rejected_file",
                "mode",
                "mode_reject",
                "p_value",
                "threads",
                "status",
                "return_code",
                "message",
            ],
        )

        if completed and not args.dry_run:
            summarize_outputs(completed, stats_path, long_path, args.p_value)

        print(f"Run summary: {run_summary_path}")
        if completed and not args.dry_run:
            print(f"Differential TAD summary: {stats_path}")
            print(f"Rejected differential TAD table: {long_path}")
        return 0 if all(row["status"] in {"completed", "skipped_existing", "dry_run"} for row in run_rows) else 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
