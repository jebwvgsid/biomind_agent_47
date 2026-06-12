#!/usr/bin/env python
"""
Run HiCExplorer hicFindTADs and summarize TAD domains/boundaries for many samples.

Usage:
  python scripts/call_tads.py \
    --sample leaf hic_matrix/Corrected/leaf.mcool \
    --sample bud hic_matrix/Corrected/bud.mcool \
    --resolution-bp 40000 \
    --out-root TADs/HiCExplorer \
    --correct-for-multiple-testing fdr

  python scripts/call_tads.py \
    --sample-sheet samples.tsv \
    --resolution-bp 40000 \
    --out-root TADs/HiCExplorer

Requirements:
  Python >= 3.8
  Command-line tool: hicFindTADs from HiCExplorer must be available on PATH.
  Required for preflight checks: cooler Python package.
  Optional for --plot-lengths: matplotlib

Install delegation:
  <install_tool env="hicexplore">hicexplorer</install_tool>
  <install_tool env="hicexplore">cooler</install_tool>
  <install_tool env="hicexplore">matplotlib</install_tool>
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional, Sequence, Tuple


DEFAULT_OUT_ROOT = "TADs/HiCExplorer"
DEFAULT_CORRECTION = "fdr"
DEFAULT_WEIGHT_NAME = "weight"


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


# Return the base filesystem path before any cooler URI suffix.
def matrix_base_path(matrix: str) -> str:
    return matrix.split("::", 1)[0]


# Return the /resolutions/N value encoded in a cooler URI.
def parse_resolution_from_uri(matrix: str) -> Optional[int]:
    match = re.search(r"::/resolutions/(\d+)(?:$|/)", matrix)
    return int(match.group(1)) if match else None


# Convert optional text to an integer resolution.
def parse_optional_int(value: object, field_name: str) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        raise ValueError(f"{field_name} must be an integer, got: {value}")


# Build a HiCExplorer matrix URI from .cool/.mcool input.
def build_matrix_uri(matrix: str, resolution_bp: int) -> str:
    if "::" in matrix:
        return matrix
    if matrix.lower().endswith(".mcool"):
        return f"{matrix}::/resolutions/{resolution_bp}"
    return matrix


# Convert a sample label into a safe output prefix.
def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
    return cleaned.strip("._-") or "sample"


# Return all native HiCExplorer output paths for one prefix.
def expected_output_paths(out_prefix: Path) -> Dict[str, Path]:
    prefix = str(out_prefix)
    return {
        "boundaries_bed": Path(f"{prefix}_boundaries.bed"),
        "boundaries_gff": Path(f"{prefix}_boundaries.gff"),
        "domains_bed": Path(f"{prefix}_domains.bed"),
        "score_bedgraph": Path(f"{prefix}_score.bedgraph"),
        "tad_score_bm": Path(f"{prefix}_tad_score.bm"),
    }


# Check whether the core parseable outputs already exist.
def core_outputs_exist(out_prefix: Path) -> bool:
    paths = expected_output_paths(out_prefix)
    return paths["boundaries_bed"].is_file() and paths["domains_bed"].is_file() and paths["score_bedgraph"].is_file()


# Return a short list of files near an output prefix for diagnostics.
def nearby_outputs(out_prefix: Path) -> List[str]:
    parent = out_prefix.parent
    if not parent.is_dir():
        return []
    prefix_name = out_prefix.name
    return sorted(p.name for p in parent.iterdir() if p.name.startswith(prefix_name[: max(1, len(prefix_name) // 2)]))


# Read non-empty non-comment rows from a whitespace-delimited genomics file.
def read_data_rows(path: Path) -> List[List[str]]:
    rows: List[List[str]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            rows.append(stripped.split())
    return rows


# Parse HiCExplorer domains BED into normalized dictionaries.
def parse_domains_bed(path: Path, sample: str, resolution_bp: int) -> List[Dict[str, object]]:
    parsed: List[Dict[str, object]] = []
    rows = read_data_rows(path)
    for fields in rows:
        if len(fields) < 4:
            continue
        try:
            start = int(float(fields[1]))
            end = int(float(fields[2]))
        except ValueError:
            continue
        parsed.append(
            {
                "sample": sample,
                "resolution_bp": resolution_bp,
                "chr": fields[0],
                "start": start,
                "end": end,
                "id": fields[3],
                "length": end - start,
                "source_file": str(path),
            }
        )
    return parsed


# Parse HiCExplorer boundary BED into normalized dictionaries.
def parse_boundaries_bed(path: Path, sample: str, resolution_bp: int) -> List[Dict[str, object]]:
    parsed: List[Dict[str, object]] = []
    rows = read_data_rows(path)
    for fields in rows:
        if len(fields) < 4:
            continue
        try:
            start = int(float(fields[1]))
            end = int(float(fields[2]))
        except ValueError:
            continue
        score = ""
        if len(fields) >= 5:
            try:
                score = float(fields[4])
            except ValueError:
                score = fields[4]
        parsed.append(
            {
                "sample": sample,
                "resolution_bp": resolution_bp,
                "chr": fields[0],
                "start": start,
                "end": end,
                "id": fields[3],
                "separation_score": score,
                "source_file": str(path),
            }
        )
    return parsed


# Summarize domain lengths for one sample.
def summarize_domains(sample: str, resolution_bp: int, domains: List[Dict[str, object]], n_boundaries: int) -> Dict[str, object]:
    lengths = [int(row["length"]) for row in domains if int(row["length"]) >= 0]
    if not lengths:
        return {
            "sample": sample,
            "resolution_bp": resolution_bp,
            "count": 0,
            "max_length": "",
            "min_length": "",
            "mean_length": "",
            "median_length": "",
            "total_length": 0,
            "n_boundaries": n_boundaries,
        }
    return {
        "sample": sample,
        "resolution_bp": resolution_bp,
        "count": len(lengths),
        "max_length": max(lengths),
        "min_length": min(lengths),
        "mean_length": round(mean(lengths), 6),
        "median_length": round(median(lengths), 6),
        "total_length": sum(lengths),
        "n_boundaries": n_boundaries,
    }


# Write a list of dictionaries as TSV.
def write_tsv(path: Path, rows: List[Dict[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(columns) + "\n")
        for row in rows:
            values = [str(row.get(col, "")) for col in columns]
            handle.write("\t".join(values) + "\n")
    print(f"[INFO] saved: {path}")


# Print a saved message for native output files that HiCExplorer created.
def report_native_outputs(out_prefix: Path) -> None:
    for path in expected_output_paths(out_prefix).values():
        if path.exists():
            print(f"[INFO] saved: {path}")


# Return a tail of a log file for concise error messages.
def read_log_tail(path: Path, n_lines: int = 30) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n_lines:])


# Import cooler only when matrix preflight requires it.
def import_cooler():
    try:
        import cooler
    except ImportError as exc:
        print(f"[ERROR] missing dependency: {exc}")
        print('[ERROR] delegate installation with: <install_tool env="hicexplore">cooler</install_tool>')
        sys.exit(1)
    return cooler


# List available resolution groups from an .mcool file.
def list_mcool_resolutions(matrix: str) -> List[int]:
    base = matrix_base_path(matrix)
    cooler = import_cooler()
    try:
        coolers = cooler.fileops.list_coolers(base)
    except Exception:
        return []
    values = []
    for uri in coolers:
        match = re.search(r"/resolutions/(\d+)$", str(uri))
        if match:
            values.append(int(match.group(1)))
    return sorted(set(values))


# Resolve one sample's resolution without silently choosing among .mcool resolutions.
def resolve_resolution(sample: str, matrix: str, row_resolution: Optional[int], global_resolution: Optional[int]) -> int:
    uri_resolution = parse_resolution_from_uri(matrix)
    candidates = [value for value in [row_resolution, global_resolution, uri_resolution] if value is not None]
    unique = sorted(set(candidates))
    if len(unique) > 1:
        raise ValueError(
            f"resolution mismatch for sample={sample}: sample_sheet={row_resolution}, "
            f"--resolution-bp={global_resolution}, uri={uri_resolution}"
        )
    if unique:
        return int(unique[0])
    if matrix.lower().endswith(".mcool"):
        available = list_mcool_resolutions(matrix)
        suffix = f" Available resolutions: {available}" if available else " Could not list available resolutions."
        raise ValueError(f"--resolution-bp is required for .mcool sample={sample}.{suffix}")
    if matrix.lower().endswith(".cool"):
        cooler = import_cooler()
        try:
            binsize = cooler.Cooler(matrix).binsize
        except Exception as exc:
            raise ValueError(f"cannot infer .cool resolution for sample={sample}: {exc}")
        if binsize:
            print(f"[INFO] inferred single-resolution .cool binsize for sample={sample}: {binsize}")
            return int(binsize)
    raise ValueError(f"--resolution-bp is required for sample={sample}; matrix does not encode /resolutions/N")


# Read a sample sheet with columns sample, matrix, and optional resolution_bp.
def read_sample_sheet(path: str) -> List[Dict[str, object]]:
    sheet = Path(path)
    if not sheet.is_file():
        print(f"[ERROR] sample sheet does not exist: {sheet}")
        sys.exit(1)
    text = sheet.read_text(encoding="utf-8-sig", errors="replace")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = "," if sheet.suffix.lower() == ".csv" or "," in first_line and "\t" not in first_line else "\t"
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    columns = reader.fieldnames or []
    required = {"sample", "matrix"}
    missing = sorted(required - set(columns))
    if missing:
        print(f"[ERROR] sample sheet missing required column(s): {missing}")
        print(f"[ERROR] available columns: {columns}")
        sys.exit(1)
    rows: List[Dict[str, object]] = []
    for line_number, row in enumerate(reader, start=2):
        sample = str(row.get("sample", "")).strip()
        matrix = str(row.get("matrix", "")).strip()
        if not sample or not matrix:
            print(f"[WARN] skipping sample sheet line {line_number}: sample or matrix is empty")
            continue
        try:
            resolution = parse_optional_int(row.get("resolution_bp"), "resolution_bp")
        except ValueError as exc:
            print(f"[ERROR] sample sheet line {line_number}: {exc}")
            sys.exit(1)
        rows.append({"sample": sample, "matrix": matrix, "resolution_bp": resolution, "source": str(sheet)})
    if not rows:
        print(f"[ERROR] sample sheet has no usable rows: {sheet}")
        sys.exit(1)
    return rows


# Collect samples from repeated --sample and optional sample sheet.
def collect_samples(args: argparse.Namespace) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for sample_sheet in args.sample_sheet or []:
        rows.extend(read_sample_sheet(sample_sheet))
    for sample, matrix in args.sample or []:
        rows.append({"sample": sample, "matrix": matrix, "resolution_bp": None, "source": "--sample"})
    if not rows:
        print("[ERROR] provide at least one --sample LABEL MATRIX or --sample-sheet samples.tsv")
        sys.exit(1)

    seen: Dict[str, str] = {}
    for row in rows:
        safe = safe_label(str(row["sample"]))
        if safe in seen:
            print(f"[ERROR] duplicate output sample label after sanitizing: {row['sample']} and {seen[safe]} both become {safe}")
            sys.exit(1)
        seen[safe] = str(row["sample"])
    return rows


# Fail early if a referenced matrix file does not exist.
def ensure_input_file(matrix: str) -> None:
    base_path = matrix_base_path(matrix)
    if not os.path.exists(base_path):
        print(f"[ERROR] file does not exist: {base_path}")
        sys.exit(1)


def ensure_hicfindtads_available() -> None:
    if shutil.which("hicFindTADs") is None:
        print("[ERROR] hicFindTADs not found on PATH")
        print('[ERROR] delegate installation with: <install_tool env="hicexplore">hicexplorer</install_tool>')
        sys.exit(1)


# Check for a usable cooler balancing weight column.
def check_cooler_balance(matrix_uri: str, weight_name: str) -> Tuple[bool, str]:
    cooler = import_cooler()
    try:
        clr = cooler.Cooler(matrix_uri)
        bin_columns = list(clr.bins().columns)
    except Exception as exc:
        return False, f"could not open cooler for balance check: {exc}"
    if weight_name not in bin_columns:
        return False, f"weight column '{weight_name}' not found. Available bin columns: {bin_columns}"
    try:
        weights = clr.bins()[weight_name][:]
    except Exception as exc:
        return False, f"could not read weight column '{weight_name}': {exc}"
    valid = 0
    total = 0
    for value in weights:
        total += 1
        try:
            if value is not None and math.isfinite(float(value)):
                valid += 1
        except (TypeError, ValueError):
            continue
    if total == 0:
        return False, f"cooler has zero bins at {matrix_uri}"
    if valid == 0:
        return False, f"weight column '{weight_name}' exists but has no finite values"
    return True, f"weight column '{weight_name}' finite bins: {valid}/{total}"


def is_balance_correctable_failure(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "weight column" in text
        and (
            "not found" in text
            or "no finite values" in text
        )
    )


def run_cooler_balance(matrix_uri: str, log_path: Path) -> int:
    cmd = ["cooler", "balance", matrix_uri]
    return run_command(cmd, log_path)


def ensure_expected_outputs(out_prefix: Path) -> Tuple[bool, str]:
    paths = expected_output_paths(out_prefix)
    missing = [str(path) for key, path in paths.items() if key in {"boundaries_bed", "domains_bed", "score_bedgraph"} and not path.is_file()]
    if missing:
        nearby = nearby_outputs(out_prefix)
        message = f"missing expected output: {missing}"
        if nearby:
            message += f"; nearby files: {nearby}"
        return False, message
    return True, "ok"


def import_matplotlib():
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        print(f"[ERROR] matplotlib is required for --plot-lengths: {exc}")
        print('[ERROR] delegate installation with: <install_tool env="hicexplore">matplotlib</install_tool>')
        sys.exit(1)
    return plt


# Build the hicFindTADs command from parsed arguments.
def build_hicfindtads_command(matrix_uri: str, out_prefix: Path, args: argparse.Namespace) -> List[str]:
    cmd = [
        "hicFindTADs",
        "--matrix",
        matrix_uri,
        "--outPrefix",
        str(out_prefix),
        "--correctForMultipleTesting",
        args.correct_for_multiple_testing,
    ]
    if args.number_of_processors:
        cmd.extend(["--numberOfProcessors", str(args.number_of_processors)])
    if args.chromosome:
        cmd.append("--chromosomes")
        cmd.extend(args.chromosome)
    # AGENT-EDITABLE: hicFindTADs passthrough - add stable first-class flags here if they become common.
    for extra in args.extra_arg or []:
        cmd.append(extra)
    return cmd


# Run hicFindTADs and capture stdout/stderr in a log file.
def run_command(cmd: Sequence[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] running: {' '.join(cmd)}")
    with log_path.open("w", encoding="utf-8") as log_handle:
        completed = subprocess.run(cmd, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    print(f"[INFO] saved: {log_path}")
    return int(completed.returncode)


# AGENT-EDITABLE: chart layout - modify bins, colors, or labels for publication figures.
def plot_domain_lengths(rows: List[Dict[str, object]], out_path: Path, dpi: int) -> None:
    if not rows:
        print("[WARN] plot skipped: no domains parsed")
        return
    plt = import_matplotlib()
    samples = sorted({str(row["sample"]) for row in rows})
    ncols = 1
    nrows = len(samples)
    fig, axes = plt.subplots(nrows=nrows, ncols=ncols, figsize=(8, max(3, 2.8 * nrows)), squeeze=False)
    for ax, sample in zip([a[0] for a in axes], samples):
        lengths = [int(row["length"]) for row in rows if str(row["sample"]) == sample and int(row["length"]) > 0]
        if lengths:
            ax.hist(lengths, bins=40, color="#4c78a8", edgecolor="white")
        ax.set_title(sample)
        ax.set_xlabel("TAD length (bp)")
        ax.set_ylabel("Count")
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
    print(f"[INFO] saved: {out_path}")


def process_sample(row: Dict[str, object], args: argparse.Namespace) -> Tuple[Dict[str, object], List[Dict[str, object]], List[Dict[str, object]], Dict[str, object]]:
    sample = str(row["sample"])
    matrix = str(row["matrix"])
    resolution_bp = int(row["effective_resolution_bp"])
    safe = safe_label(sample)
    resolution_dir = Path(args.out_root) / str(resolution_bp)
    out_prefix = resolution_dir / safe
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    matrix_uri = build_matrix_uri(matrix, resolution_bp)
    log_path = Path(f"{out_prefix}.hicFindTADs.log")
    cmd = build_hicfindtads_command(matrix_uri, out_prefix, args)

    run_status = {
        "sample": sample,
        "resolution_bp": resolution_bp,
        "matrix": matrix_uri,
        "out_prefix": str(out_prefix),
        "balanced": "",
        "status": "pending",
        "return_code": "",
        "message": "",
    }

    if args.dry_run:
        print(f"[INFO] dry-run command: {' '.join(cmd)}")
        run_status.update({"status": "dry_run", "return_code": "", "message": "command printed only"})
        return run_status, [], [], summarize_domains(sample, resolution_bp, [], 0)

    should_run = not args.stats_only
    if args.skip_existing and core_outputs_exist(out_prefix):
        print(f"[INFO] skipping existing sample={sample}: {out_prefix}")
        should_run = False

    if should_run and not args.skip_balance_check:
        ok, message = check_cooler_balance(matrix_uri, args.weight_name)
        run_status["balanced"] = "yes" if ok else "no"
        if ok:
            print(f"[INFO] balance check passed for sample={sample}: {message}")
        elif args.auto_balance and is_balance_correctable_failure(message):
            print(f"[WARN] balance check failed for sample={sample}: {message}")
            balance_log = Path(f"{out_prefix}.cooler_balance.log")
            balance_code = run_cooler_balance(matrix_uri, balance_log)
            if balance_code != 0:
                tail = read_log_tail(balance_log)
                run_status.update(
                    {
                        "status": "failed",
                        "return_code": balance_code,
                        "message": tail or f"cooler balance exited with {balance_code}",
                    }
                )
                return run_status, [], [], summarize_domains(sample, resolution_bp, [], 0)
            ok, message = check_cooler_balance(matrix_uri, args.weight_name)
            run_status["balanced"] = "auto_balanced" if ok else "no"
            if ok:
                print(f"[INFO] auto-balance succeeded for sample={sample}: {message}")
            else:
                run_status.update({"status": "failed", "message": f"auto-balance finished but balance check still failed: {message}"})
                print(f"[ERROR] balance check failed after auto-balance for sample={sample}: {message}")
                return run_status, [], [], summarize_domains(sample, resolution_bp, [], 0)
        else:
            run_status.update({"status": "failed", "message": message})
            print(f"[ERROR] balance check failed for sample={sample}: {message}")
            print("[ERROR] automatic balancing is disabled or this failure is not balance-correctable; use --auto-balance or provide a balanced/corrected cooler.")
            return run_status, [], [], summarize_domains(sample, resolution_bp, [], 0)
    elif should_run:
        run_status["balanced"] = "skipped"
        print(f"[WARN] balance check skipped for sample={sample}")

    if should_run:
        return_code = run_command(cmd, log_path)
        run_status["return_code"] = return_code
        if return_code != 0:
            tail = read_log_tail(log_path)
            run_status.update({"status": "failed", "message": tail or f"hicFindTADs exited with {return_code}"})
            return run_status, [], [], summarize_domains(sample, resolution_bp, [], 0)

    ok, message = ensure_expected_outputs(out_prefix)
    if not ok:
        print(f"[ERROR] sample={sample}: {message}")
        run_status.update({"status": "failed", "message": message})
        return run_status, [], [], summarize_domains(sample, resolution_bp, [], 0)

    report_native_outputs(out_prefix)
    paths = expected_output_paths(out_prefix)
    domains = parse_domains_bed(paths["domains_bed"], sample, resolution_bp)
    boundaries = parse_boundaries_bed(paths["boundaries_bed"], sample, resolution_bp)
    if not domains:
        print(f"[WARN] no domains parsed for sample={sample}: {paths['domains_bed']}")
    if not boundaries:
        print(f"[WARN] no boundaries parsed for sample={sample}: {paths['boundaries_bed']}")

    stats = summarize_domains(sample, resolution_bp, domains, len(boundaries))
    run_status.update({"status": "success", "message": "ok"})
    return run_status, domains, boundaries, stats


def preflight_samples(samples: Sequence[Dict[str, object]], args: argparse.Namespace, out_dir: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for row in samples:
        sample = str(row["sample"])
        resolution_bp = int(row["effective_resolution_bp"])
        matrix_uri = build_matrix_uri(str(row["matrix"]), resolution_bp)
        result = {
            "sample": sample,
            "resolution_bp": resolution_bp,
            "matrix": matrix_uri,
            "balanced": "",
            "status": "pending",
            "message": "",
        }
        if args.skip_balance_check:
            result.update({"balanced": "skipped", "status": "skipped", "message": "balance check skipped"})
            print(f"[WARN] preflight balance check skipped for sample={sample}")
        else:
            ok, message = check_cooler_balance(matrix_uri, args.weight_name)
            result.update({"balanced": "yes" if ok else "no", "status": "success" if ok else "failed", "message": message})
            level = "INFO" if ok else "ERROR"
            print(f"[{level}] preflight balance check {'passed' if ok else 'failed'} for sample={sample}: {message}")
        rows.append(result)
    write_tsv(
        out_dir / "tad_preflight.tsv",
        rows,
        ["sample", "resolution_bp", "matrix", "balanced", "status", "message"],
    )
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run HiCExplorer hicFindTADs for many .cool/.mcool matrices and summarize TAD results.",
        formatter_class=HelpFormatter,
        epilog="""Examples:
  python scripts/call_tads.py --sample leaf hic_matrix/Corrected/leaf.mcool --resolution-bp 40000 --out-root TADs/HiCExplorer
  python scripts/call_tads.py --sample-sheet samples.tsv --resolution-bp 40000 --out-root TADs/HiCExplorer --correct-for-multiple-testing fdr
  python scripts/call_tads.py --sample-sheet samples.tsv --resolution-bp 40000 --out-root TADs/HiCExplorer --stats-only --plot-lengths

Sample sheet TSV columns:
  sample<TAB>matrix<TAB>resolution_bp
  leaf<TAB>hic_matrix/Corrected/leaf.mcool<TAB>40000
""",
    )
    parser.add_argument(
        "--sample",
        nargs=2,
        action="append",
        metavar=("LABEL", "MATRIX"),
        default=None,
        help="Repeat for each sample. MATRIX can be .cool, .mcool, or a full URI like leaf.mcool::/resolutions/40000.",
    )
    parser.add_argument(
        "--sample-sheet",
        action="append",
        default=None,
        help="TSV/CSV sample sheet with required columns sample,matrix and optional resolution_bp. Repeat to merge sheets.",
    )
    parser.add_argument(
        "--resolution-bp",
        type=int,
        default=None,  # Deliberately optional so full cooler URIs or sample-sheet resolution_bp can carry the explicit resolution.
        help="Confirmed resolution in bp. Required for plain .mcool paths unless each sample sheet row has resolution_bp.",
    )
    # AGENT-EDITABLE: output layout default - matches the TADs/HiCExplorer/40000/sample_* layout.
    parser.add_argument(
        "--out-root",
        default=DEFAULT_OUT_ROOT,  # Keep the common project layout unless the user gives another output root.
        help="Root output directory. Results are written under OUT_ROOT/RESOLUTION_BP/.",
    )
    # AGENT-EDITABLE: key parameter default - fdr matches the provided reference workflow.
    parser.add_argument(
        "--correct-for-multiple-testing",
        default=DEFAULT_CORRECTION,  # HiCExplorer argument --correctForMultipleTesting; fdr is the common reproducible choice.
        help="Value passed to hicFindTADs --correctForMultipleTesting.",
    )
    parser.add_argument(
        "--number-of-processors",
        type=int,
        default=None,  # Leave unset so hicFindTADs uses its own default unless the user requests threads.
        help="Optional value passed to hicFindTADs --numberOfProcessors.",
    )
    parser.add_argument(
        "--chromosome",
        action="append",
        default=None,  # Default all chromosomes.
        help="Restrict hicFindTADs to a chromosome. Repeat for multiple chromosomes.",
    )
    parser.add_argument(
        "--weight-name",
        default=DEFAULT_WEIGHT_NAME,  # cooler balance writes this column by default.
        help="Cooler bin weight column that must exist before running hicFindTADs.",
    )
    parser.add_argument(
        "--skip-balance-check",
        action="store_true",
        help="Skip cooler balance preflight. Use only if correction was verified outside this script.",
    )
    parser.add_argument(
        "--auto-balance",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically run cooler balance on samples whose weight column is missing or has no finite values before TAD calling.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Only validate inputs, resolution, and balance weight columns, then write tad_preflight.tsv without running hicFindTADs.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=None,  # Allows advanced HiCExplorer flags without editing the wrapper.
        help="Append one raw hicFindTADs argument token. Repeat for flags and values, e.g. --extra-arg=--minDepth --extra-arg=80000.",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Do not run hicFindTADs; parse existing *_domains.bed and *_boundaries.bed files.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip hicFindTADs for samples whose core output files already exist, then parse them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow rerunning even when outputs exist. Existing files may be overwritten by HiCExplorer.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print hicFindTADs commands without running or parsing outputs.",
    )
    parser.add_argument(
        "--plot-lengths",
        action="store_true",
        help="Write a PNG histogram of domain length distributions across samples.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=180,  # QC figure default; increase for publication.
        help="DPI for --plot-lengths output.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failed sample.",
    )
    parser.add_argument(
        "--allow-partial-success",
        action="store_true",
        help="Exit 0 when at least one sample succeeds even if other samples fail. By default any failed sample exits non-zero.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    samples = collect_samples(args)

    for row in samples:
        ensure_input_file(str(row["matrix"]))
        try:
            row["effective_resolution_bp"] = resolve_resolution(
                str(row["sample"]),
                str(row["matrix"]),
                parse_optional_int(row.get("resolution_bp"), "resolution_bp"),
                args.resolution_bp,
            )
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)

    resolutions = sorted({int(row["effective_resolution_bp"]) for row in samples})
    if len(resolutions) > 1:
        print(f"[ERROR] mixed resolutions in one run are not supported for combined TAD statistics: {resolutions}")
        print("[ERROR] run one confirmed resolution at a time, or split your sample sheet by resolution.")
        sys.exit(1)

    if args.force and args.skip_existing:
        print("[WARN] both --force and --skip-existing were set; --force takes precedence")
        args.skip_existing = False

    out_dir = Path(args.out_root) / str(resolutions[0])
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.preflight_only:
        preflight_rows = preflight_samples(samples, args, out_dir)
        failed_rows = [row for row in preflight_rows if row.get("status") == "failed"]
        print(
            f"[INFO] preflight summary: samples={len(samples)}, failed={len(failed_rows)}, "
            f"resolution_bp={resolutions[0]}"
        )
        if failed_rows:
            sys.exit(1)
        return

    if not args.stats_only and not args.dry_run:
        ensure_hicfindtads_available()

    run_rows: List[Dict[str, object]] = []
    all_domains: List[Dict[str, object]] = []
    all_boundaries: List[Dict[str, object]] = []
    stats_rows: List[Dict[str, object]] = []
    success = 0
    skipped = 0

    for row in samples:
        run_status, domains, boundaries, stats = process_sample(row, args)
        run_rows.append(run_status)
        all_domains.extend(domains)
        all_boundaries.extend(boundaries)
        stats_rows.append(stats)

        if run_status["status"] in {"success", "dry_run"}:
            success += 1
        else:
            skipped += 1
            if args.fail_fast:
                break

    # AGENT-EDITABLE: summary output schema - add project-specific columns here if needed.
    write_tsv(
        out_dir / "tad_domains_long.tsv",
        all_domains,
        ["sample", "resolution_bp", "chr", "start", "end", "id", "length", "source_file"],
    )
    write_tsv(
        out_dir / "tad_boundaries_long.tsv",
        all_boundaries,
        ["sample", "resolution_bp", "chr", "start", "end", "id", "separation_score", "source_file"],
    )
    write_tsv(
        out_dir / "tad_domain_stats.tsv",
        stats_rows,
        ["sample", "resolution_bp", "count", "max_length", "min_length", "mean_length", "median_length", "total_length", "n_boundaries"],
    )
    write_tsv(
        out_dir / "tad_run_summary.tsv",
        run_rows,
        ["sample", "resolution_bp", "matrix", "out_prefix", "balanced", "status", "return_code", "message"],
    )

    if args.plot_lengths:
        plot_domain_lengths(all_domains, out_dir / "tad_domain_lengths.png", args.dpi)

    print(
        f"[INFO] summary: samples={len(samples)}, success={success}, skipped={skipped}, "
        f"resolution_bp={resolutions[0]}, domains={len(all_domains)}, boundaries={len(all_boundaries)}"
    )
    failed_rows = [row for row in run_rows if row.get("status") not in {"success", "dry_run"}]
    if success == 0 and not args.dry_run:
        print("[ERROR] all samples failed")
        sys.exit(1)
    if failed_rows and not args.dry_run and not args.allow_partial_success:
        failed_samples = ", ".join(str(row.get("sample", "")) for row in failed_rows)
        print(f"[ERROR] failed sample(s): {failed_samples}")
        sys.exit(1)


if __name__ == "__main__":
    main()
