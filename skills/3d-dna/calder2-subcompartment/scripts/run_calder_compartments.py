#!/usr/bin/env python
"""Run CALDER2 compartment calling and summarize sub-compartment outputs."""

from __future__ import annotations

import argparse
import csv
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_OUT_ROOT = "Compartments/CALDER2"
DEFAULT_MATRIX_TYPE = "mcool"
DEFAULT_GENOME = "others"
CALDER_MODES = ("auto", "official-r", "patched-cli")
COMPARTMENT_LEVELS = ("Compartment2", "Compartment4", "Compartment8")
DEFAULT_EXCLUDED_CHROMS = ("ALL", "M", "chrM", "MT", "chrMT", "Y", "chrY")
TEXT_EXTENSIONS = {".txt", ".tsv", ".csv", ".bed", ".gff", ".bedgraph"}


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


def build_matrix_uri(matrix: str, resolution_bp: int) -> str:
    if "::" in matrix:
        return matrix
    if matrix.lower().endswith(".mcool"):
        return f"{matrix}::/resolutions/{resolution_bp}"
    return matrix


def matrix_suffix(matrix: str) -> str:
    return Path(matrix_base_path(matrix)).suffix.lower()


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


def load_samples(args: argparse.Namespace) -> Dict[str, Dict[str, str]]:
    samples: Dict[str, Dict[str, str]] = {}
    for item in args.sample or []:
        label, matrix = item
        samples[safe_label(label)] = {"sample": safe_label(label), "matrix": matrix}
    if args.sample_sheet:
        for row in read_rows(args.sample_sheet):
            label = row.get("sample") or row.get("label")
            matrix = row.get("matrix") or row.get("path")
            if not label or not matrix:
                raise SystemExit("[ERROR] sample sheet requires sample and matrix columns")
            samples[safe_label(label)] = {**row, "sample": safe_label(label), "matrix": matrix}
    return samples


def load_existing_samples(args: argparse.Namespace) -> Dict[str, Path]:
    existing: Dict[str, Path] = {}
    for item in args.existing_sample or []:
        label, path = item
        existing[safe_label(label)] = Path(path)
    if args.existing_sample_sheet:
        for row in read_rows(args.existing_sample_sheet):
            label = row.get("sample") or row.get("label")
            path = row.get("compartments") or row.get("path") or row.get("result")
            if not label or not path:
                raise SystemExit("[ERROR] existing sample sheet requires sample and compartments/path columns")
            existing[safe_label(label)] = Path(path)
    return existing


def infer_resolution(args: argparse.Namespace, samples: Dict[str, Dict[str, str]]) -> int:
    values: List[int] = []
    if args.resolution_bp:
        values.append(args.resolution_bp)
    for row in samples.values():
        if row.get("resolution_bp"):
            values.append(int(float(row["resolution_bp"])))
        matrix_res = parse_resolution_from_uri(row["matrix"])
        if matrix_res:
            values.append(matrix_res)
    if not values:
        raise SystemExit("[ERROR] --resolution-bp is required for compartment calling from .mcool/.cool matrices")
    first = values[0]
    if any(value != first for value in values):
        raise SystemExit(f"[ERROR] mixed resolutions are not supported in one run: {values}")
    return first


def try_import_cooler():
    try:
        import cooler  # type: ignore

        return cooler
    except Exception:
        return None


def inspect_matrix(sample: str, matrix: str, resolution_bp: int) -> Dict[str, str]:
    base = Path(matrix_base_path(matrix))
    row: Dict[str, str] = {
        "sample": sample,
        "matrix": matrix,
        "base_path": str(base),
        "base_exists": str(base.is_file()),
        "resolution_bp": str(resolution_bp),
        "cooler_uri": build_matrix_uri(matrix, resolution_bp),
        "available_resolutions": "",
        "weight_column": "",
        "finite_weight_fraction": "",
        "preflight_status": "not_checked",
        "preflight_note": "",
    }
    if not base.is_file():
        row["preflight_status"] = "failed"
        row["preflight_note"] = "matrix base file not found"
        return row
    cooler = try_import_cooler()
    if cooler is None:
        row["preflight_status"] = "warning"
        row["preflight_note"] = "cooler Python package unavailable; skipped resolution/balance inspection"
        return row
    try:
        if str(base).lower().endswith(".mcool"):
            row["available_resolutions"] = ",".join(cooler.fileops.list_coolers(str(base)))
        clr = cooler.Cooler(row["cooler_uri"])
        bins = clr.bins()[:]
        if "weight" in bins.columns:
            row["weight_column"] = "weight"
            weights = bins["weight"]
            finite = weights.notna()
            row["finite_weight_fraction"] = f"{float(finite.mean()):.6f}" if len(finite) else ""
            row["preflight_status"] = "ok" if finite.any() else "warning"
            row["preflight_note"] = "weight column present" if finite.any() else "weight column has no finite values"
        else:
            row["preflight_status"] = "warning"
            row["preflight_note"] = "weight column missing"
    except Exception as exc:
        row["preflight_status"] = "warning"
        row["preflight_note"] = f"cooler inspection failed: {exc}"
    return row


def run_command(cmd: Sequence[str], log_path: Path, dry_run: bool = False) -> Tuple[str, int, str]:
    rendered = " ".join(cmd)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("Command:\n")
        log.write(rendered + "\n\n")
        if dry_run:
            log.write("DRY RUN: command not executed.\n")
            return rendered, 0, "dry-run"
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    return rendered, result.returncode, "ok" if result.returncode == 0 else "failed"


def quote_r_string(value: str | Path) -> str:
    text = str(value).replace("\\", "/").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def calder_chrom_name(chrom: str, args: argparse.Namespace) -> str:
    if args.strip_chr_prefix_for_calder and args.chr_prefix and chrom.startswith(args.chr_prefix):
        return chrom[len(args.chr_prefix) :]
    return chrom


def rewrite_chrom_prefix(chrom: str, from_prefix: str, to_prefix: str) -> str:
    if not from_prefix:
        return chrom
    if chrom.startswith(from_prefix):
        return f"{to_prefix}{chrom[len(from_prefix):]}"
    return chrom


def prepare_feature_track(args: argparse.Namespace, sample_out: Path) -> Optional[Path]:
    if not args.feature_track:
        return None
    source = Path(args.feature_track)
    if not args.feature_track_calder_prefix:
        return source.resolve()
    target = sample_out / "feature_track_for_calder.bed"
    target.parent.mkdir(parents=True, exist_ok=True)
    from_prefix = args.chr_prefix
    to_prefix = args.feature_track_calder_prefix
    with source.open(encoding="utf-8", errors="replace") as src, target.open("w", encoding="utf-8") as dst:
        for raw in src:
            if not raw.strip() or raw.startswith("#"):
                dst.write(raw)
                continue
            line = raw.rstrip("\n")
            delimiter = "\t" if "\t" in line else None
            fields = line.split(delimiter)
            if fields:
                fields[0] = rewrite_chrom_prefix(fields[0], from_prefix, to_prefix)
            dst.write(("\t" if delimiter == "\t" else " ").join(fields) + "\n")
    return target.resolve()


def restore_text_prefixes(out_dir: Path, from_prefix: str, to_prefix: str) -> None:
    if not from_prefix or not to_prefix or from_prefix == to_prefix:
        return
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            continue
        changed = False
        rewritten: List[str] = []
        for line in lines:
            if line.startswith(from_prefix):
                rewritten.append(f"{to_prefix}{line[len(from_prefix):]}")
                changed = True
            else:
                rewritten.append(line)
        if changed:
            path.write_text("".join(rewritten), encoding="utf-8")


def make_calder_output_aliases(sample_out: Path) -> None:
    """Expose CALDER's nested result files under the standard wrapper layout."""
    sub_dir = sample_out / "sub_compartments"
    if (sub_dir / "all_sub_compartments.tsv").is_file():
        return
    candidates = sorted(sample_out.rglob("all_sub_compartments.tsv"))
    if not candidates:
        candidates = sorted(sample_out.rglob("all_sub_compartments.bed"))
    if not candidates:
        return
    source_dir = candidates[0].parent
    sub_dir.mkdir(parents=True, exist_ok=True)
    for source in source_dir.glob("all_sub_compartments.*"):
        target = sub_dir / source.name
        if not target.exists():
            target.write_bytes(source.read_bytes())


def selected_chromosomes(args: argparse.Namespace, clr) -> List[str]:
    if args.chromosome:
        return list(args.chromosome)
    excluded = {chrom.upper() for chrom in args.exclude_chromosome}
    return [chrom for chrom in clr.chromnames if chrom.upper() not in excluded]


def validate_feature_track_basic(path: Path) -> Tuple[Dict[str, int], float, int, int]:
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"[ERROR] feature track not found or empty: {path}")
    chrom_rows: Dict[str, int] = {}
    total = 0.0
    nonzero = 0
    bad_rows = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip() or raw.startswith("#"):
                continue
            fields = raw.rstrip("\n").split("\t")
            if len(fields) < 4:
                fields = raw.strip().split()
            if len(fields) < 4:
                bad_rows += 1
                continue
            try:
                start = int(fields[1])
                end = int(fields[2])
                value = float(fields[3])
            except ValueError:
                raise SystemExit(f"[ERROR] feature track has non-numeric BED coordinates/value at line {line_number}: {path}")
            if start < 0 or end <= start or not math.isfinite(value):
                bad_rows += 1
                continue
            chrom_rows[fields[0]] = chrom_rows.get(fields[0], 0) + 1
            total += value
            if value > 0:
                nonzero += 1
    if bad_rows:
        raise SystemExit(f"[ERROR] feature track contains {bad_rows} malformed BED rows: {path}")
    if not chrom_rows:
        raise SystemExit(f"[ERROR] feature track has no usable BED rows: {path}")
    if total <= 0:
        raise SystemExit(f"[ERROR] gene_density.bed exists but column 4 sum is 0: {path}")
    if nonzero <= 0:
        raise SystemExit(f"[ERROR] gene_density.bed has no non-zero bins: {path}")
    return chrom_rows, total, nonzero, bad_rows


def validate_feature_track_for_cooler(
    feature_track: str,
    matrix: str,
    sample: str,
    resolution_bp: int,
    args: argparse.Namespace,
) -> None:
    path = Path(feature_track)
    chrom_rows, _total, _nonzero, _bad_rows = validate_feature_track_basic(path)
    cooler = try_import_cooler()
    if cooler is None:
        return
    suffix = matrix_suffix(matrix)
    if suffix not in {".cool", ".mcool"}:
        return
    clr = cooler.Cooler(build_matrix_uri(matrix, resolution_bp))
    selected = selected_chromosomes(args, clr)
    selected_set = set(selected)
    track_set = set(chrom_rows)
    missing = sorted(selected_set - track_set)
    extra = sorted(track_set - selected_set)
    if missing or extra:
        raise SystemExit(
            "[ERROR] feature track chromosome names do not match cooler chromnames for "
            f"{sample}: missing={missing[:8]}, extra={extra[:8]}"
        )

    chromsizes = dict(zip(clr.chromsizes.index.astype(str), clr.chromsizes.astype(int)))
    expected_bins = {
        chrom: int(math.ceil(chromsizes[chrom] / float(resolution_bp)))
        for chrom in selected
        if chrom in chromsizes
    }
    seen_starts: Dict[str, set[int]] = {chrom: set() for chrom in selected}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip() or raw.startswith("#"):
                continue
            fields = raw.rstrip("\n").split("\t")
            if len(fields) < 4:
                fields = raw.strip().split()
            chrom = fields[0]
            start = int(fields[1])
            end = int(fields[2])
            chrom_size = chromsizes.get(chrom)
            if chrom_size is None:
                raise SystemExit(f"[ERROR] feature track chromosome {chrom!r} is not in cooler chromsizes for {sample}")
            if start % resolution_bp != 0:
                raise SystemExit(f"[ERROR] feature track start is not aligned to {resolution_bp} bp at line {line_number}: {path}")
            width = end - start
            if not (width == resolution_bp or end == chrom_size):
                raise SystemExit(
                    f"[ERROR] feature track bin width mismatch at line {line_number}: "
                    f"width={width}, resolution={resolution_bp}, chrom_size={chrom_size}"
                )
            if end > chrom_size:
                raise SystemExit(f"[ERROR] feature track interval exceeds cooler chromsize at line {line_number}: {path}")
            if start in seen_starts.setdefault(chrom, set()):
                raise SystemExit(f"[ERROR] duplicate feature-track bin for {chrom}:{start}-{end} at line {line_number}: {path}")
            seen_starts[chrom].add(start)
    incomplete = [
        f"{chrom}: expected {expected_bins[chrom]}, found {len(seen_starts.get(chrom, set()))}"
        for chrom in selected
        if chrom in expected_bins and len(seen_starts.get(chrom, set())) != expected_bins[chrom]
    ]
    if incomplete:
        raise SystemExit(f"[ERROR] feature track is not one binned row per cooler bin for {sample}: {incomplete[:8]}")


def export_cooler_to_calder_dump(
    matrix: str,
    sample: str,
    resolution_bp: int,
    sample_out: Path,
    chromosomes: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, Path]:
    cooler = try_import_cooler()
    if cooler is None:
        raise SystemExit("[ERROR] cooler is required to convert .cool/.mcool to CALDER2 dump input")
    uri = build_matrix_uri(matrix, resolution_bp)
    clr = cooler.Cooler(uri)
    dump_dir = sample_out / "calder_dump"
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_paths: Dict[str, Path] = {}
    for chrom in chromosomes:
        if chrom not in clr.chromnames:
            raise SystemExit(f"[ERROR] chromosome {chrom!r} not found in {matrix}")
        calder_chrom = calder_chrom_name(chrom, args)
        out_path = dump_dir / f"{calder_chrom}.dump.tsv"
        if out_path.exists() and out_path.stat().st_size > 0:
            dump_paths[calder_chrom] = out_path
            continue
        try:
            pixels = clr.matrix(balance=args.dump_balance, as_pixels=True, join=True).fetch(chrom)
        except Exception as exc:
            hint = " Use --no-dump-balance for raw counts if balanced pixels are unavailable."
            raise SystemExit(f"[ERROR] failed to export {sample} {chrom} from cooler: {exc}.{hint}")
        if args.dump_balance:
            if "balanced" not in pixels.columns:
                raise SystemExit("[ERROR] balanced pixel column not found after cooler export; use --no-dump-balance")
            value_col = "balanced"
        else:
            value_col = "count"
        with out_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            for record in pixels.itertuples(index=False):
                start1 = getattr(record, "start1")
                start2 = getattr(record, "start2")
                value = getattr(record, value_col)
                if value is None:
                    continue
                numeric = float(value)
                if not math.isfinite(numeric):
                    continue
                if args.skip_nonpositive_dump_values and numeric <= 0:
                    continue
                writer.writerow([int(start1), int(start2), numeric])
        dump_paths[calder_chrom] = out_path
    return dump_paths


def build_official_calder_r_script(
    args: argparse.Namespace,
    sample: str,
    matrix: str,
    resolution_bp: int,
    sample_out: Path,
) -> Path:
    sample_out.mkdir(parents=True, exist_ok=True)
    suffix = matrix_suffix(matrix)
    r_script = sample_out / f"{sample}.run_calder_official.R"
    feature_line = "feature_track <- NULL"
    feature_arg = "feature_track = NULL"
    feature_track = prepare_feature_track(args, sample_out)
    if feature_track:
        feature_path = quote_r_string(feature_track)
        feature_line = "\n".join(
            [
                "suppressPackageStartupMessages(library(data.table))",
                f"feature_track <- data.table::fread({feature_path}, header = FALSE)",
                "feature_track <- as.data.frame(feature_track[, seq_len(min(4, ncol(feature_track))), with = FALSE])",
            ]
        )
        feature_arg = "feature_track = feature_track"

    if suffix == ".hic":
        if not args.chromosome:
            raise SystemExit("[ERROR] official-r mode with .hic input requires --chromosome entries for CALDER chrs")
        contact_lines = [
            f"contact_file_hic <- {quote_r_string(Path(matrix_base_path(matrix)).resolve())}",
            "contact_file_dump <- NULL",
            "chrs <- c(" + ", ".join(quote_r_string(chrom) for chrom in args.chromosome) + ")",
        ]
        contact_args = "contact_file_hic = contact_file_hic, contact_file_dump = contact_file_dump"
    else:
        cooler = try_import_cooler()
        if cooler is None:
            raise SystemExit("[ERROR] cooler is required to convert .cool/.mcool to CALDER2 dump input")
        clr = cooler.Cooler(build_matrix_uri(matrix, resolution_bp))
        chromosomes = selected_chromosomes(args, clr)
        dump_paths = export_cooler_to_calder_dump(matrix, sample, resolution_bp, sample_out, chromosomes, args)
        calder_chroms = [calder_chrom_name(chrom, args) for chrom in chromosomes]
        if len(set(calder_chroms)) != len(calder_chroms):
            raise SystemExit(f"[ERROR] chromosome names are not unique after prefix stripping: {calder_chroms}")
        chrs_r = "c(" + ", ".join(quote_r_string(chrom) for chrom in calder_chroms) + ")"
        dump_r = "stats::setNames(list(" + ", ".join(quote_r_string(path.resolve()) for path in dump_paths.values()) + "), chrs)"
        contact_lines = [
            "contact_file_hic <- NULL",
            f"chrs <- {chrs_r}",
            f"contact_file_dump <- {dump_r}",
        ]
        contact_args = "contact_file_hic = contact_file_hic, contact_file_dump = contact_file_dump"

    r_script.write_text(
        "\n".join(
            [
                "suppressPackageStartupMessages(library(CALDER))",
                feature_line,
                *contact_lines,
                f"out_dir <- {quote_r_string(sample_out.resolve())}",
                "dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)",
                "if (!exists('chrs')) { chrs <- NULL }",
                "CALDER(",
                f"  {contact_args},",
                "  chrs = chrs,",
                f"  bin_size = {resolution_bp},",
                f"  genome = {quote_r_string(args.genome)},",
                f"  save_dir = out_dir,",
                f"  single_binsize_only = {str(args.single_binsize_only).upper()},",
                f"  save_intermediate_data = {str(args.save_intermediate_data).upper()},",
                f"  n_cores = {args.threads},",
                f"  sub_domains = {str(args.sub_domains).upper()},",
                f"  {feature_arg}",
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return r_script


def resolve_calder_mode(args: argparse.Namespace, matrix: str) -> str:
    if args.calder_mode != "auto":
        return args.calder_mode
    return "official-r"


def find_compartment_file(path: Path) -> Optional[Path]:
    candidates: List[Path] = []
    if path.is_file():
        candidates.append(path)
    else:
        candidates.extend(
            [
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


def sibling_bed(path: Path) -> Optional[Path]:
    if path.suffix.lower() == ".bed":
        return path
    candidate = path.with_suffix(".bed")
    if candidate.is_file():
        return candidate
    candidate = path.parent / "all_sub_compartments.bed"
    return candidate if candidate.is_file() else None


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


def read_bed_color_map(path: Optional[Path]) -> Dict[str, str]:
    colors: Dict[str, str] = {}
    if not path or not path.is_file():
        return colors
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 9:
                fields = line.strip().split()
            if len(fields) >= 9:
                colors.setdefault(fields[3], fields[8])
    return colors


def parse_tsv(path: Path, sample: str, resolution_bp: int) -> List[Dict[str, object]]:
    color_map = read_bed_color_map(sibling_bed(path))
    rows: List[Dict[str, object]] = []
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames:
            return rows
        for raw in reader:
            chrom = raw.get("Chr") or raw.get("chr") or raw.get("chrom") or raw.get("chromosome")
            comp_name = raw.get("comp_name") or raw.get("calder") or raw.get("name")
            pos_start = parse_int(raw.get("pos_start") or raw.get("start"))
            pos_end = parse_int(raw.get("pos_end") or raw.get("end"))
            if not chrom or not comp_name or pos_start is None or pos_end is None:
                continue
            start = max(0, pos_start - 1)
            end = pos_end
            levels = comp_levels(comp_name)
            rows.append(
                {
                    "sample": sample,
                    "resolution_bp": resolution_bp,
                    "chr": chrom,
                    "start": start,
                    "end": end,
                    "length": max(0, end - start),
                    "comp_name": comp_name,
                    **levels,
                    "comp_rank": raw.get("comp_rank", ""),
                    "continuous_rank": raw.get("continous_rank") or raw.get("continuous_rank") or "",
                    "score": "",
                    "color": color_map.get(comp_name, ""),
                    "source_file": str(path),
                }
            )
    return rows


def parse_bed(path: Path, sample: str, resolution_bp: int, bed_start_one_based: bool) -> List[Dict[str, object]]:
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
            levels = comp_levels(comp_name)
            rows.append(
                {
                    "sample": sample,
                    "resolution_bp": resolution_bp,
                    "chr": fields[0],
                    "start": start,
                    "end": end,
                    "length": max(0, end - start),
                    "comp_name": comp_name,
                    **levels,
                    "comp_rank": "",
                    "continuous_rank": "",
                    "score": fields[4] if len(fields) >= 5 else "",
                    "color": fields[8] if len(fields) >= 9 else "",
                    "source_file": str(path),
                }
            )
    return rows


def parse_compartment_file(path: Path, sample: str, resolution_bp: int, bed_start_one_based: bool) -> List[Dict[str, object]]:
    if path.suffix.lower() == ".tsv":
        rows = parse_tsv(path, sample, resolution_bp)
        if rows:
            return rows
    return parse_bed(path, sample, resolution_bp, bed_start_one_based)


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


def summarize_compartments(rows: Sequence[Dict[str, object]]) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    by_sample: Dict[str, Dict[str, object]] = {}
    grouped: Dict[Tuple[str, str, str, str], Dict[str, object]] = {}
    sample_total_length: Dict[str, int] = {}
    for row in rows:
        sample = str(row["sample"])
        length = int(row.get("length") or 0)
        sample_total_length[sample] = sample_total_length.get(sample, 0) + length
        stats = by_sample.setdefault(
            sample,
            {
                "sample": sample,
                "resolution_bp": row.get("resolution_bp", ""),
                "n_bins_or_segments": 0,
                "total_length": 0,
                "n_chromosomes": set(),
            },
        )
        stats["n_bins_or_segments"] = int(stats["n_bins_or_segments"]) + 1
        stats["total_length"] = int(stats["total_length"]) + length
        stats["n_chromosomes"].add(row.get("chr", ""))
        for level in COMPARTMENT_LEVELS:
            comp = str(row.get(level, ""))
            key = (sample, level, comp, str(row.get("color", "")))
            entry = grouped.setdefault(
                key,
                {
                    "sample": sample,
                    "resolution_bp": row.get("resolution_bp", ""),
                    "level": level,
                    "compartment": comp,
                    "color": row.get("color", ""),
                    "count": 0,
                    "total_length": 0,
                },
            )
            entry["count"] = int(entry["count"]) + 1
            entry["total_length"] = int(entry["total_length"]) + length
    sample_rows: List[Dict[str, object]] = []
    for stats in by_sample.values():
        chroms = stats.pop("n_chromosomes")
        stats["n_chromosomes"] = len([chrom for chrom in chroms if chrom])
        sample_rows.append(stats)
    summary_rows = []
    for entry in grouped.values():
        total = sample_total_length.get(str(entry["sample"]), 0)
        entry["proportion"] = (float(entry["total_length"]) / total) if total else ""
        summary_rows.append(entry)
    summary_rows.sort(key=lambda r: (str(r["sample"]), str(r["level"]), str(r["compartment"])))
    sample_rows.sort(key=lambda r: str(r["sample"]))
    return sample_rows, summary_rows


def run_calder(args: argparse.Namespace, samples: Dict[str, Dict[str, str]], resolution_bp: int, out_dir: Path) -> List[Dict[str, object]]:
    if args.stats_only:
        return []
    if not args.allow_no_feature_track:
        if not args.feature_track:
            raise SystemExit("[ERROR] --feature-track is required unless --allow-no-feature-track is set")
        if not Path(args.feature_track).is_file():
            raise SystemExit(f"[ERROR] feature track not found: {args.feature_track}")
    rows: List[Dict[str, object]] = []
    for sample, row in samples.items():
        matrix = row["matrix"]
        if not Path(matrix_base_path(matrix)).is_file():
            raise SystemExit(f"[ERROR] matrix file not found for {sample}: {matrix_base_path(matrix)}")
        sample_out = out_dir / sample
        expected = find_compartment_file(sample_out)
        mode = resolve_calder_mode(args, matrix)
        if expected and args.skip_existing:
            rows.append(
                {
                    "step": "calder",
                    "mode": mode,
                    "sample": sample,
                    "matrix": matrix,
                    "resolution_bp": resolution_bp,
                    "output_dir": str(sample_out),
                    "status": "skipped_existing",
                    "return_code": 0,
                    "command": "",
                }
            )
            continue
        if args.feature_track:
            validate_feature_track_for_cooler(args.feature_track, matrix, sample, resolution_bp, args)
        if mode == "patched-cli":
            calder_script = Path(args.calder_script)
            if not calder_script.is_file():
                raise SystemExit(f"[ERROR] patched calder script not found: {calder_script}")
            cmd = [
                args.rscript_bin,
                str(calder_script),
                "-i",
                matrix,
                "-t",
                args.matrix_type,
                "-b",
                str(resolution_bp),
                "-g",
                args.genome,
                "-p",
                str(args.threads),
                "-o",
                str(sample_out),
            ]
            if args.chr_prefix:
                cmd.extend(["--chr_prefix", args.chr_prefix])
            if args.feature_track:
                cmd.extend(["-f", args.feature_track])
            cmd.extend(args.extra_arg)
        else:
            r_script = build_official_calder_r_script(args, sample, matrix, resolution_bp, sample_out)
            cmd = [args.rscript_bin, str(r_script), *args.extra_arg]
        command, return_code, status = run_command(cmd, sample_out / f"{sample}.calder.log", args.dry_run)
        if return_code == 0 and not args.dry_run:
            restore_text_prefixes(sample_out, args.restore_from_prefix, args.restore_output_prefix)
            make_calder_output_aliases(sample_out)
        rows.append(
            {
                "step": "calder",
                "mode": mode,
                "sample": sample,
                "matrix": matrix,
                "resolution_bp": resolution_bp,
                "output_dir": str(sample_out),
                "status": status,
                "return_code": return_code,
                "command": command,
            }
        )
        if return_code != 0:
            raise SystemExit(f"[ERROR] CALDER2 failed for {sample}; see {sample_out / (sample + '.calder.log')}")
    return rows


def write_report(out_dir: Path, args: argparse.Namespace, sample_stats: Sequence[Dict[str, object]], run_rows: Sequence[Dict[str, object]]) -> None:
    lines = [
        "# CALDER2 Compartment Analysis Report",
        "",
        f"- Resolution: {args.resolution_bp or 'inferred from inputs'}",
        f"- Output directory: {out_dir}",
        f"- CALDER mode: {args.calder_mode}",
        f"- Patched CALDER script: {args.calder_script}",
        f"- Feature track: {args.feature_track or 'not provided'}",
        f"- Chromosome prefix: {args.chr_prefix or 'not provided'}",
        "",
        "## Sample Summary",
        "",
        "| sample | bins_or_segments | chromosomes | total_length |",
        "|---|---:|---:|---:|",
    ]
    for row in sample_stats:
        lines.append(
            f"| {row.get('sample', '')} | {row.get('n_bins_or_segments', '')} | "
            f"{row.get('n_chromosomes', '')} | {row.get('total_length', '')} |"
        )
    lines.extend(["", "## Run Status", "", "| sample | mode | status | return_code | output_dir |", "|---|---|---|---:|---|"])
    for row in run_rows:
        lines.append(
            f"| {row.get('sample', '')} | {row.get('mode', '')} | {row.get('status', '')} | {row.get('return_code', '')} | "
            f"{row.get('output_dir', '')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation Notes",
            "",
            "- A/B and sub-compartment calls are resolution- and normalization-dependent.",
            "- If no feature track was used, PC1 or A/B sign orientation may be arbitrary.",
            "- Official CALDER2 accepts Juicer `.hic` or per-chromosome dump contact tables; `.mcool/.cool` inputs are converted to dump tables unless patched-cli mode is selected.",
            "- The tutorial's `scripts/calder -t mcool --chr_prefix` path is a patched CLI mode, not an original CALDER2 interface.",
        ]
    )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run CALDER2 compartment calling and summarize outputs.",
        formatter_class=HelpFormatter,
    )
    parser.add_argument("--sample", nargs=2, action="append", metavar=("SAMPLE", "MATRIX"))
    parser.add_argument("--sample-sheet", help="TSV/CSV with sample and matrix columns.")
    parser.add_argument("--existing-sample", nargs=2, action="append", metavar=("SAMPLE", "COMPARTMENT_PATH"))
    parser.add_argument("--existing-sample-sheet", help="TSV/CSV with sample and compartments/path columns.")
    parser.add_argument("--resolution-bp", type=int, help="Compartment resolution in bp.")
    parser.add_argument("--out-root", default=DEFAULT_OUT_ROOT)
    parser.add_argument("--calder-mode", choices=CALDER_MODES, default="auto")
    parser.add_argument("--calder-script", default="scripts/calder")
    parser.add_argument("--rscript-bin", default="Rscript")
    parser.add_argument("--matrix-type", default=DEFAULT_MATRIX_TYPE)
    parser.add_argument("--genome", default=DEFAULT_GENOME)
    parser.add_argument("--chr-prefix", default="")
    parser.add_argument("--chromosome", action="append", help="Restrict official-r dump conversion to one chromosome; repeatable.")
    parser.add_argument("--exclude-chromosome", action="append", default=list(DEFAULT_EXCLUDED_CHROMS))
    parser.add_argument("--strip-chr-prefix-for-calder", action="store_true")
    parser.add_argument("--feature-track-calder-prefix", default="")
    parser.add_argument("--restore-from-prefix", default="")
    parser.add_argument("--restore-output-prefix", default="")
    parser.add_argument("--feature-track", help="Gene-density/GC feature BED for compartment orientation.")
    parser.add_argument("--allow-no-feature-track", action="store_true")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--dump-balance", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-nonpositive-dump-values", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--single-binsize-only", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--sub-domains", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--save-intermediate-data", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--extra-arg", action="append", default=[])
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--stats-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--bed-start-one-based",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Treat CALDER2 BED start as 1-based and subtract one, matching the project tutorial.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = load_samples(args)
    existing = load_existing_samples(args)
    if not samples and not existing:
        raise SystemExit("[ERROR] provide --sample/--sample-sheet or --existing-sample/--existing-sample-sheet")
    resolution_bp = infer_resolution(args, samples) if samples else args.resolution_bp
    if not resolution_bp:
        raise SystemExit("[ERROR] --resolution-bp is required when summarizing existing compartments")
    args.resolution_bp = resolution_bp
    out_dir = Path(args.out_root) / str(resolution_bp)
    out_dir.mkdir(parents=True, exist_ok=True)

    preflight_rows = [inspect_matrix(sample, row["matrix"], resolution_bp) for sample, row in samples.items()]
    preflight_fields = [
        "sample",
        "matrix",
        "base_path",
        "base_exists",
        "resolution_bp",
        "cooler_uri",
        "available_resolutions",
        "weight_column",
        "finite_weight_fraction",
        "preflight_status",
        "preflight_note",
    ]
    write_tsv(out_dir / "compartment_matrix_preflight.tsv", preflight_rows, preflight_fields)

    run_rows = run_calder(args, samples, resolution_bp, out_dir)

    all_rows: List[Dict[str, object]] = []
    for sample in sorted(samples):
        comp_file = find_compartment_file(out_dir / sample)
        if comp_file:
            all_rows.extend(parse_compartment_file(comp_file, sample, resolution_bp, args.bed_start_one_based))
    for sample, path in sorted(existing.items()):
        comp_file = find_compartment_file(path)
        if not comp_file:
            raise SystemExit(f"[ERROR] no CALDER2 compartment file found for {sample}: {path}")
        all_rows.extend(parse_compartment_file(comp_file, sample, resolution_bp, args.bed_start_one_based))

    long_fields = [
        "sample",
        "resolution_bp",
        "chr",
        "start",
        "end",
        "length",
        "comp_name",
        "Compartment2",
        "Compartment4",
        "Compartment8",
        "comp_rank",
        "continuous_rank",
        "score",
        "color",
        "source_file",
    ]
    write_tsv(out_dir / "compartments_long.tsv", all_rows, long_fields)
    sample_stats, summary_rows = summarize_compartments(all_rows)
    write_tsv(
        out_dir / "compartment_sample_stats.tsv",
        sample_stats,
        ["sample", "resolution_bp", "n_bins_or_segments", "total_length", "n_chromosomes"],
    )
    write_tsv(
        out_dir / "compartment_length_summary.tsv",
        summary_rows,
        ["sample", "resolution_bp", "level", "compartment", "color", "count", "total_length", "proportion"],
    )
    write_tsv(
        out_dir / "compartment_run_summary.tsv",
        run_rows,
        ["step", "mode", "sample", "matrix", "resolution_bp", "output_dir", "status", "return_code", "command"],
    )
    write_report(out_dir, args, sample_stats, run_rows)
    print(f"[OK] wrote compartment outputs under {out_dir}")


if __name__ == "__main__":
    main()
