#!/usr/bin/env python
"""Summarize shared, shifted, and sample-specific TAD boundaries."""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import math
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


BOUNDARY_PATH_COLUMNS = ("boundaries", "boundary_file", "boundary_path", "path", "file")
CHROM_COLUMNS = ("chr", "chrom", "chromosome", "seqname")
START_COLUMNS = ("start", "boundary_start")
END_COLUMNS = ("end", "boundary_end")
POSITION_COLUMNS = ("position", "pos", "midpoint", "boundary_position")
ID_COLUMNS = ("id", "name", "boundary_id", "ID")
SCORE_COLUMNS = ("separation_score", "score", "boundary_score")
DEFAULT_OUT_DIR = "tad_boundary_changes"


@dataclass
class Boundary:
    sample: str
    chrom: str
    start: int
    end: int
    position: int
    boundary_id: str = ""
    score: str = ""
    resolution_bp: Optional[int] = None
    source_file: str = ""
    row_number: int = 0


@dataclass
class SampleMeta:
    sample: str
    boundaries: Optional[Path] = None
    matrix: str = ""
    resolution_bp: Optional[int] = None
    resolution_source: str = ""


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(label).strip())
    return cleaned.strip("._-") or "sample"


def parse_optional_int(value: object, label: str) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer, got: {value}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite, got: {value}")
    return int(number)


def parse_resolution_from_uri(matrix: str) -> Optional[int]:
    match = re.search(r"::/resolutions/(\d+)(?:$|/)", str(matrix))
    return int(match.group(1)) if match else None


def matrix_base_path(matrix: str) -> str:
    return str(matrix).split("::", 1)[0]


def infer_resolution_from_path(path: Path) -> Optional[int]:
    for part in [path.parent.name, *[parent.name for parent in path.parents]]:
        if re.fullmatch(r"\d{3,}", part or ""):
            return int(part)
    return None


def import_cooler():
    try:
        import cooler  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "cooler is required to infer resolution from .cool/.mcool inputs. "
            "Pass --resolution-bp or --boundary-tol-bp, or delegate: "
            '<install_tool env="hicexplore">cooler</install_tool>'
        ) from exc
    return cooler


def infer_resolution_from_matrix(matrix: str, explicit_resolution: Optional[int]) -> Tuple[Optional[int], str]:
    if not matrix:
        return None, ""

    uri_resolution = parse_resolution_from_uri(matrix)
    if uri_resolution is not None:
        return uri_resolution, "matrix_uri"

    base = matrix_base_path(matrix)
    lower = base.lower()
    if lower.endswith(".mcool"):
        if explicit_resolution is not None:
            return explicit_resolution, "explicit_resolution_for_mcool"
        available = ""
        try:
            cooler = import_cooler()
            values = cooler.fileops.list_coolers(base)
            available = f" Available resolutions: {values}"
        except Exception as exc:
            available = f" Could not list resolutions: {exc}"
        raise ValueError(
            f"plain .mcool input requires explicit resolution: {matrix}.{available}"
        )
    if lower.endswith(".cool"):
        if explicit_resolution is not None:
            return explicit_resolution, "explicit_resolution_for_cool"
        cooler = import_cooler()
        try:
            binsize = cooler.Cooler(matrix).binsize
        except Exception as exc:
            raise ValueError(f"cannot infer .cool binsize from {matrix}: {exc}") from exc
        if binsize:
            return int(binsize), "cool_binsize"
    return None, ""


def detect_delimiter(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if path.suffix.lower() == ".csv":
                    return ","
                if "," in stripped and "\t" not in stripped:
                    return ","
                return "\t" if "\t" in stripped else None  # type: ignore[return-value]
    return "\t"


def split_line(line: str, delimiter: Optional[str]) -> List[str]:
    if delimiter:
        return next(csv.reader([line], delimiter=delimiter))
    return line.strip().split()


def first_existing_key(row: Dict[str, str], candidates: Iterable[str]) -> str:
    lower_to_key = {key.lower(): key for key in row}
    for candidate in candidates:
        key = lower_to_key.get(candidate.lower())
        if key is not None:
            return key
    return ""


def looks_like_header(fields: Sequence[str]) -> bool:
    if len(fields) < 3:
        return False
    lower = {field.strip().lower().lstrip("#") for field in fields}
    if lower & set(CHROM_COLUMNS):
        return True
    try:
        int(float(fields[1]))
        int(float(fields[2]))
        return False
    except (ValueError, IndexError):
        return True


def read_rows(path: Path) -> Tuple[Optional[List[str]], List[Tuple[int, List[str]]], Optional[str]]:
    if not path.is_file():
        raise FileNotFoundError(f"input file does not exist: {path}")
    delimiter = detect_delimiter(path)
    header: Optional[List[str]] = None
    rows: List[Tuple[int, List[str]]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") and not header:
                candidate = split_line(stripped.lstrip("#").strip(), delimiter)
                if looks_like_header(candidate):
                    header = [field.strip().lstrip("#") for field in candidate]
                continue
            if stripped.startswith("#"):
                continue
            fields = split_line(raw_line, delimiter)
            if header is None and looks_like_header(fields):
                header = [field.strip().lstrip("#") for field in fields]
                continue
            rows.append((line_number, fields))
    return header, rows, delimiter


def value_from_row(row: Dict[str, str], candidates: Iterable[str]) -> str:
    key = first_existing_key(row, candidates)
    return row.get(key, "").strip() if key else ""


def boundary_from_dict(
    row: Dict[str, str],
    sample: str,
    source_file: Path,
    row_number: int,
    position_mode: str,
    fallback_resolution: Optional[int],
) -> Optional[Boundary]:
    chrom = value_from_row(row, CHROM_COLUMNS)
    if not chrom:
        return None

    start_text = value_from_row(row, START_COLUMNS)
    end_text = value_from_row(row, END_COLUMNS)
    pos_text = value_from_row(row, POSITION_COLUMNS)

    try:
        if start_text and end_text:
            start = int(float(start_text))
            end = int(float(end_text))
        elif pos_text:
            start = int(float(pos_text))
            end = start + 1
        else:
            return None
    except ValueError:
        return None

    if pos_text and not (start_text and end_text):
        position = int(float(pos_text))
    elif position_mode == "start":
        position = start
    elif position_mode == "end":
        position = end
    else:
        position = (start + end) // 2

    row_resolution = parse_optional_int(value_from_row(row, ("resolution_bp", "resolution", "binsize")), "resolution_bp")
    return Boundary(
        sample=sample,
        chrom=chrom,
        start=start,
        end=end,
        position=position,
        boundary_id=value_from_row(row, ID_COLUMNS),
        score=value_from_row(row, SCORE_COLUMNS),
        resolution_bp=row_resolution or fallback_resolution,
        source_file=str(source_file),
        row_number=row_number,
    )


def parse_boundary_file(
    path: Path,
    sample: str,
    position_mode: str,
    fallback_resolution: Optional[int],
) -> List[Boundary]:
    header, rows, _delimiter = read_rows(path)
    parsed: List[Boundary] = []
    if header:
        normalized_header = [field.strip().lstrip("#") or f"col_{idx + 1}" for idx, field in enumerate(header)]
        for row_number, fields in rows:
            if len(fields) > len(normalized_header):
                normalized_header = normalized_header + [
                    f"col_{idx + 1}" for idx in range(len(normalized_header), len(fields))
                ]
            row = {normalized_header[idx]: value for idx, value in enumerate(fields[: len(normalized_header)])}
            boundary = boundary_from_dict(row, sample, path, row_number, position_mode, fallback_resolution)
            if boundary:
                parsed.append(boundary)
        return parsed

    for row_number, fields in rows:
        if len(fields) < 3:
            continue
        try:
            start = int(float(fields[1]))
            end = int(float(fields[2]))
        except ValueError:
            continue
        if position_mode == "start":
            position = start
        elif position_mode == "end":
            position = end
        else:
            position = (start + end) // 2
        parsed.append(
            Boundary(
                sample=sample,
                chrom=fields[0],
                start=start,
                end=end,
                position=position,
                boundary_id=fields[3] if len(fields) > 3 else "",
                score=fields[4] if len(fields) > 4 else "",
                resolution_bp=fallback_resolution,
                source_file=str(path),
                row_number=row_number,
            )
        )
    return parsed


def parse_boundaries_long(
    path: Path,
    position_mode: str,
    fallback_resolution: Optional[int],
    include_samples: Optional[set],
) -> Dict[str, List[Boundary]]:
    header, rows, _delimiter = read_rows(path)
    if not header:
        raise ValueError(f"--boundaries-long requires a header table: {path}")
    normalized_header = [field.strip().lstrip("#") or f"col_{idx + 1}" for idx, field in enumerate(header)]
    sample_key = first_existing_key({name: "" for name in normalized_header}, ("sample", "sample_id", "label"))
    if not sample_key:
        raise ValueError(f"--boundaries-long missing sample column: {path}")

    by_sample: Dict[str, List[Boundary]] = {}
    for row_number, fields in rows:
        if len(fields) > len(normalized_header):
            normalized_header = normalized_header + [
                f"col_{idx + 1}" for idx in range(len(normalized_header), len(fields))
            ]
        row = {normalized_header[idx]: value for idx, value in enumerate(fields[: len(normalized_header)])}
        sample = row.get(sample_key, "").strip()
        if not sample or (include_samples and sample not in include_samples):
            continue
        boundary = boundary_from_dict(row, sample, path, row_number, position_mode, fallback_resolution)
        if boundary:
            by_sample.setdefault(sample, []).append(boundary)
    return by_sample


def read_table_dicts(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"sample sheet does not exist: {path}")
    delimiter = detect_delimiter(path)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter or "\t")
        return [dict(row) for row in reader]


def parse_sample_sheet(path: Path) -> List[SampleMeta]:
    rows = read_table_dicts(path)
    if not rows:
        raise ValueError(f"sample sheet has no rows: {path}")
    first = rows[0]
    sample_key = first_existing_key(first, ("sample", "sample_id", "label"))
    path_key = first_existing_key(first, BOUNDARY_PATH_COLUMNS)
    if not sample_key:
        raise ValueError(f"sample sheet missing required column: sample ({path})")
    if not path_key:
        raise ValueError(
            f"sample sheet missing boundary path column in {path}; use one of {BOUNDARY_PATH_COLUMNS}"
        )
    matrix_key = first_existing_key(first, ("matrix", "cooler", "cool", "mcool"))
    res_key = first_existing_key(first, ("resolution_bp", "resolution", "binsize"))
    samples: List[SampleMeta] = []
    for idx, row in enumerate(rows, start=2):
        sample = row.get(sample_key, "").strip()
        boundary_path = row.get(path_key, "").strip()
        if not sample or not boundary_path:
            continue
        resolution = parse_optional_int(row.get(res_key, ""), f"resolution_bp line {idx}") if res_key else None
        samples.append(
            SampleMeta(
                sample=sample,
                boundaries=Path(boundary_path),
                matrix=row.get(matrix_key, "").strip() if matrix_key else "",
                resolution_bp=resolution,
                resolution_source="sample_sheet" if resolution is not None else "",
            )
        )
    return samples


def find_tad_boundary_files(directory: Path, include_samples: Optional[set]) -> List[SampleMeta]:
    if not directory.is_dir():
        raise NotADirectoryError(f"--tad-out-dir is not a directory: {directory}")
    inferred_resolution = infer_resolution_from_path(directory)
    samples: List[SampleMeta] = []
    for path in sorted(directory.glob("*_boundaries.bed")):
        sample = path.name[: -len("_boundaries.bed")]
        if include_samples and sample not in include_samples:
            continue
        samples.append(
            SampleMeta(
                sample=sample,
                boundaries=path,
                resolution_bp=inferred_resolution,
                resolution_source="tad_out_dir" if inferred_resolution is not None else "",
            )
        )
    return samples


def merge_resolution_candidates(
    sample: str,
    candidates: Sequence[Tuple[Optional[int], str]],
    allow_mixed_resolution: bool,
) -> Tuple[Optional[int], str]:
    values = [(value, source) for value, source in candidates if value is not None]
    unique = sorted({int(value) for value, _source in values})
    if len(unique) > 1 and not allow_mixed_resolution:
        details = ", ".join(f"{source}={value}" for value, source in values)
        raise ValueError(f"resolution mismatch for sample={sample}: {details}")
    if not values:
        return None, ""
    value, source = values[0]
    return int(value), source


def deduplicate_boundaries(boundaries: Sequence[Boundary]) -> Tuple[List[Boundary], int]:
    seen = set()
    unique: List[Boundary] = []
    duplicates = 0
    for boundary in sorted(boundaries, key=lambda b: (b.chrom, b.position, b.start, b.end, b.boundary_id)):
        key = (boundary.chrom, boundary.position)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(boundary)
    return unique, duplicates


def collect_inputs(args: argparse.Namespace) -> Tuple[Dict[str, List[Boundary]], List[Dict[str, object]]]:
    include_samples = set(args.include_sample or []) or None
    sample_meta: List[SampleMeta] = []
    boundaries_by_sample: Dict[str, List[Boundary]] = {}
    resolution_source_by_sample: Dict[str, str] = {}
    sample_stats: List[Dict[str, object]] = []

    for item in args.sample or []:
        sample_meta.append(SampleMeta(sample=item[0], boundaries=Path(item[1])))
    for sheet in args.sample_sheet or []:
        sample_meta.extend(parse_sample_sheet(Path(sheet)))
    for directory in args.tad_out_dir or []:
        sample_meta.extend(find_tad_boundary_files(Path(directory), include_samples))

    matrix_by_sample = {label: matrix for label, matrix in args.matrix or []}
    for meta in sample_meta:
        if meta.sample in matrix_by_sample:
            meta.matrix = matrix_by_sample[meta.sample]

    for long_table in args.boundaries_long or []:
        fallback = args.resolution_bp or infer_resolution_from_path(Path(long_table))
        parsed = parse_boundaries_long(Path(long_table), args.position_mode, fallback, include_samples)
        for sample, rows in parsed.items():
            boundaries_by_sample.setdefault(sample, []).extend(rows)
            if args.resolution_bp is not None:
                resolution_source_by_sample.setdefault(sample, "cli_resolution")
            elif infer_resolution_from_path(Path(long_table)) is not None:
                resolution_source_by_sample.setdefault(sample, "path")
            else:
                resolution_source_by_sample.setdefault(sample, "boundary_rows")

    meta_by_sample: Dict[str, SampleMeta] = {}
    for meta in sample_meta:
        if include_samples and meta.sample not in include_samples:
            continue
        if meta.sample in meta_by_sample:
            raise ValueError(f"duplicate sample metadata: {meta.sample}")
        meta_by_sample[meta.sample] = meta

    for sample, meta in meta_by_sample.items():
        if not meta.boundaries:
            continue
        path_resolution = infer_resolution_from_path(meta.boundaries)
        matrix_resolution, matrix_source = infer_resolution_from_matrix(
            meta.matrix,
            args.resolution_bp or meta.resolution_bp,
        )
        resolution, source = merge_resolution_candidates(
            sample,
            [
                (args.resolution_bp, "cli_resolution"),
                (meta.resolution_bp, meta.resolution_source or "sample_metadata"),
                (matrix_resolution, matrix_source),
                (path_resolution, "path"),
            ],
            args.allow_mixed_resolution,
        )
        if source:
            resolution_source_by_sample[sample] = source
        rows = parse_boundary_file(meta.boundaries, sample, args.position_mode, resolution)
        for row in rows:
            row_resolution, _row_source = merge_resolution_candidates(
                sample,
                [(resolution, source), (row.resolution_bp, "boundary_row")],
                args.allow_mixed_resolution,
            )
            row.resolution_bp = row_resolution
        boundaries_by_sample.setdefault(sample, []).extend(rows)

    if not boundaries_by_sample:
        raise ValueError("no boundary inputs were provided")

    for sample, rows in sorted(boundaries_by_sample.items()):
        if not rows:
            raise ValueError(f"no boundary rows parsed for sample={sample}")
        resolutions = sorted({row.resolution_bp for row in rows if row.resolution_bp is not None})
        if len(resolutions) > 1 and not args.allow_mixed_resolution:
            raise ValueError(f"mixed boundary row resolutions for sample={sample}: {resolutions}")
        resolution = resolutions[0] if resolutions else None
        source = resolution_source_by_sample.get(sample) or ("boundary_rows" if resolution is not None else "")
        unique_rows, duplicate_count = deduplicate_boundaries(rows)
        boundaries_by_sample[sample] = unique_rows
        sample_stats.append(
            {
                "sample": sample,
                "resolution_bp": resolution if resolution is not None else "",
                "resolution_source": source,
                "source_files": ";".join(sorted({row.source_file for row in rows})),
                "n_boundaries_input": len(rows),
                "n_boundaries_unique_positions": len(unique_rows),
                "n_duplicate_positions_removed": duplicate_count,
            }
        )
    return boundaries_by_sample, sample_stats


def comparison_pairs(samples: Sequence[str], args: argparse.Namespace) -> List[Tuple[str, str]]:
    sample_set = set(samples)
    if args.comparison:
        pairs = [(a, b) for a, b in args.comparison]
    elif args.reference:
        if args.reference not in sample_set:
            raise ValueError(f"--reference sample not found: {args.reference}")
        pairs = [(args.reference, sample) for sample in samples if sample != args.reference]
    else:
        pairs = list(itertools.combinations(samples, 2))
    for a, b in pairs:
        if a not in sample_set:
            raise ValueError(f"comparison sample not found: {a}")
        if b not in sample_set:
            raise ValueError(f"comparison sample not found: {b}")
        if a == b:
            raise ValueError(f"comparison uses the same sample twice: {a}")
    return pairs


def comparison_resolution(
    sample_a: str,
    sample_b: str,
    rows_a: Sequence[Boundary],
    rows_b: Sequence[Boundary],
    args: argparse.Namespace,
) -> Tuple[str, int, str]:
    resolutions = sorted(
        {
            row.resolution_bp
            for row in list(rows_a) + list(rows_b)
            if row.resolution_bp is not None
        }
    )
    if args.boundary_tol_bp is not None:
        if len(resolutions) > 1 and not args.allow_mixed_resolution:
            raise ValueError(
                f"mixed resolutions for {sample_a} vs {sample_b}: {resolutions}; "
                "use --allow-mixed-resolution only with an explicit --boundary-tol-bp"
            )
        label = "mixed" if len(resolutions) > 1 else (str(resolutions[0]) if resolutions else "")
        return label, int(args.boundary_tol_bp), "explicit_bp"
    if not resolutions:
        raise ValueError(
            f"resolution is required to derive --boundary-tol-bp for {sample_a} vs {sample_b}; "
            "pass --resolution-bp or --boundary-tol-bp"
        )
    if len(resolutions) > 1:
        raise ValueError(
            f"mixed resolutions are not supported for {sample_a} vs {sample_b}: {resolutions}; "
            "split comparisons by resolution or pass --boundary-tol-bp with --allow-mixed-resolution"
        )
    tolerance = int(round(float(args.boundary_tol_bins) * int(resolutions[0])))
    return str(resolutions[0]), tolerance, "resolution_bins"


def group_by_chrom(rows: Sequence[Boundary]) -> Dict[str, List[Boundary]]:
    grouped: Dict[str, List[Boundary]] = {}
    for row in rows:
        grouped.setdefault(row.chrom, []).append(row)
    for chrom in grouped:
        grouped[chrom].sort(key=lambda b: (b.position, b.start, b.end, b.boundary_id))
    return grouped


def match_boundaries(
    rows_a: Sequence[Boundary],
    rows_b: Sequence[Boundary],
    tolerance_bp: int,
) -> Tuple[List[Tuple[Boundary, Boundary, int]], List[Boundary], List[Boundary]]:
    grouped_a = group_by_chrom(rows_a)
    grouped_b = group_by_chrom(rows_b)
    matches: List[Tuple[Boundary, Boundary, int]] = []
    used_a = set()
    used_b = set()

    for chrom in sorted(set(grouped_a) | set(grouped_b)):
        a_rows = grouped_a.get(chrom, [])
        b_rows = grouped_b.get(chrom, [])
        b_positions = [row.position for row in b_rows]
        candidates: List[Tuple[int, int, int, int, int]] = []
        for a_idx, a_row in enumerate(a_rows):
            left = bisect.bisect_left(b_positions, a_row.position - tolerance_bp)
            right = bisect.bisect_right(b_positions, a_row.position + tolerance_bp)
            for b_idx in range(left, right):
                b_row = b_rows[b_idx]
                delta = b_row.position - a_row.position
                candidates.append((abs(delta), a_row.position, b_row.position, a_idx, b_idx))
        for _abs_delta, _a_pos, _b_pos, a_idx, b_idx in sorted(candidates):
            a_key = (chrom, a_idx)
            b_key = (chrom, b_idx)
            if a_key in used_a or b_key in used_b:
                continue
            used_a.add(a_key)
            used_b.add(b_key)
            a_row = a_rows[a_idx]
            b_row = b_rows[b_idx]
            matches.append((a_row, b_row, b_row.position - a_row.position))

    unmatched_a: List[Boundary] = []
    unmatched_b: List[Boundary] = []
    for chrom, rows in grouped_a.items():
        for idx, row in enumerate(rows):
            if (chrom, idx) not in used_a:
                unmatched_a.append(row)
    for chrom, rows in grouped_b.items():
        for idx, row in enumerate(rows):
            if (chrom, idx) not in used_b:
                unmatched_b.append(row)
    return matches, unmatched_a, unmatched_b


def mean_or_blank(values: Sequence[int]) -> object:
    return round(statistics.mean(values), 6) if values else ""


def median_or_blank(values: Sequence[int]) -> object:
    return round(statistics.median(values), 6) if values else ""


def fraction(numerator: int, denominator: int) -> object:
    return round(numerator / denominator, 6) if denominator else ""


def boundary_long_row(
    comparison: str,
    sample: str,
    compared_to: str,
    role: str,
    change_class: str,
    boundary: Boundary,
    matched_sample: str,
    matched_position: object,
    delta_bp: object,
    abs_delta_bp: object,
    tolerance_bp: int,
) -> Dict[str, object]:
    return {
        "comparison": comparison,
        "sample": sample,
        "compared_to": compared_to,
        "role": role,
        "change_class": change_class,
        "chrom": boundary.chrom,
        "start": boundary.start,
        "end": boundary.end,
        "position": boundary.position,
        "id": boundary.boundary_id,
        "separation_score": boundary.score,
        "matched_sample": matched_sample,
        "matched_position": matched_position,
        "delta_bp": delta_bp,
        "abs_delta_bp": abs_delta_bp,
        "resolution_bp": boundary.resolution_bp if boundary.resolution_bp is not None else "",
        "boundary_tol_bp": tolerance_bp,
        "source_file": boundary.source_file,
    }


def summarize_comparison(
    sample_a: str,
    sample_b: str,
    rows_a: Sequence[Boundary],
    rows_b: Sequence[Boundary],
    args: argparse.Namespace,
) -> Tuple[Dict[str, object], List[Dict[str, object]], List[Dict[str, object]]]:
    resolution_label, tolerance_bp, tolerance_source = comparison_resolution(sample_a, sample_b, rows_a, rows_b, args)
    matches, unmatched_a, unmatched_b = match_boundaries(rows_a, rows_b, tolerance_bp)
    comparison = f"{safe_label(sample_a)}_vs_{safe_label(sample_b)}"
    match_rows: List[Dict[str, object]] = []
    long_rows: List[Dict[str, object]] = []
    abs_shifts: List[int] = []
    n_exact = 0
    n_shifted = 0

    for a_row, b_row, delta in sorted(matches, key=lambda item: (item[0].chrom, item[0].position, item[1].position)):
        abs_delta = abs(delta)
        abs_shifts.append(abs_delta)
        match_class = "shared_exact" if delta == 0 else "shifted_within_tol"
        if delta == 0:
            n_exact += 1
        else:
            n_shifted += 1
        match_rows.append(
            {
                "comparison": comparison,
                "sample_a": sample_a,
                "sample_b": sample_b,
                "chrom": a_row.chrom,
                "a_start": a_row.start,
                "a_end": a_row.end,
                "a_position": a_row.position,
                "a_id": a_row.boundary_id,
                "a_score": a_row.score,
                "b_start": b_row.start,
                "b_end": b_row.end,
                "b_position": b_row.position,
                "b_id": b_row.boundary_id,
                "b_score": b_row.score,
                "delta_bp": delta,
                "abs_delta_bp": abs_delta,
                "match_class": match_class,
                "boundary_tol_bp": tolerance_bp,
                "a_source_file": a_row.source_file,
                "b_source_file": b_row.source_file,
            }
        )
        long_rows.append(
            boundary_long_row(
                comparison,
                sample_a,
                sample_b,
                "sample_a",
                match_class,
                a_row,
                sample_b,
                b_row.position,
                delta,
                abs_delta,
                tolerance_bp,
            )
        )
        long_rows.append(
            boundary_long_row(
                comparison,
                sample_b,
                sample_a,
                "sample_b",
                match_class,
                b_row,
                sample_a,
                a_row.position,
                delta,
                abs_delta,
                tolerance_bp,
            )
        )

    for row in sorted(unmatched_a, key=lambda b: (b.chrom, b.position)):
        long_rows.append(
            boundary_long_row(
                comparison,
                sample_a,
                sample_b,
                "sample_a",
                "sample_a_specific",
                row,
                "",
                "",
                "",
                "",
                tolerance_bp,
            )
        )
    for row in sorted(unmatched_b, key=lambda b: (b.chrom, b.position)):
        long_rows.append(
            boundary_long_row(
                comparison,
                sample_b,
                sample_a,
                "sample_b",
                "sample_b_specific",
                row,
                "",
                "",
                "",
                "",
                tolerance_bp,
            )
        )

    summary = {
        "comparison": comparison,
        "sample_a": sample_a,
        "sample_b": sample_b,
        "resolution_bp": resolution_label,
        "boundary_tol_bp": tolerance_bp,
        "boundary_tol_bins": args.boundary_tol_bins if tolerance_source == "resolution_bins" else "",
        "n_a_boundaries": len(rows_a),
        "n_b_boundaries": len(rows_b),
        "n_matched": len(matches),
        "n_shared_exact": n_exact,
        "n_shifted_within_tol": n_shifted,
        "n_a_specific": len(unmatched_a),
        "n_b_specific": len(unmatched_b),
        "fraction_a_matched": fraction(len(matches), len(rows_a)),
        "fraction_b_matched": fraction(len(matches), len(rows_b)),
        "mean_abs_shift_bp": mean_or_blank(abs_shifts),
        "median_abs_shift_bp": median_or_blank(abs_shifts),
        "max_abs_shift_bp": max(abs_shifts) if abs_shifts else "",
    }
    return summary, match_rows, long_rows


def write_tsv(path: Path, rows: Sequence[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"[INFO] saved: {path}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare TAD boundary files and summarize shared, shifted, and sample-specific boundaries.",
        formatter_class=HelpFormatter,
        epilog="""Examples:
  python summarize_tad_boundary_changes.py --sample A A_boundaries.bed --sample B B_boundaries.bed --resolution-bp 40000 --out-dir boundary_changes
  python summarize_tad_boundary_changes.py --boundaries-long TADs/HiCExplorer/40000/tad_boundaries_long.tsv --out-dir TADs/HiCExplorer/40000/boundary_changes
  python summarize_tad_boundary_changes.py --tad-out-dir TADs/HiCExplorer/40000 --comparison leaf bud --out-dir boundary_changes
""",
    )
    parser.add_argument("--sample", nargs=2, action="append", metavar=("LABEL", "BOUNDARIES"), help="Repeat for boundary BED/TSV files.")
    parser.add_argument("--sample-sheet", action="append", help="TSV/CSV with sample and boundaries columns; optional matrix,resolution_bp.")
    parser.add_argument("--boundaries-long", action="append", help="Existing tad_boundaries_long.tsv table from TAD calling.")
    parser.add_argument("--tad-out-dir", action="append", help="Directory containing *_boundaries.bed files.")
    parser.add_argument("--matrix", nargs=2, action="append", metavar=("LABEL", "MATRIX"), help="Optional matrix path/URI for resolution inference; repeat per sample.")
    parser.add_argument("--include-sample", action="append", help="Restrict parsed inputs to a sample; repeat for multiple samples.")
    parser.add_argument("--comparison", nargs=2, action="append", metavar=("SAMPLE_A", "SAMPLE_B"), help="Comparison to run; repeat for multiple pairs.")
    parser.add_argument("--reference", help="Compare this sample against every other sample.")
    parser.add_argument("--resolution-bp", type=int, help="Confirmed resolution in bp for all inputs.")
    parser.add_argument("--boundary-tol-bp", type=int, help="Explicit boundary matching tolerance in bp.")
    parser.add_argument("--boundary-tol-bins", type=float, default=1.0, help="Tolerance in bins when --boundary-tol-bp is not set.")
    parser.add_argument("--allow-mixed-resolution", action="store_true", help="Allow mixed-resolution inputs only when tolerance is explicit.")
    parser.add_argument("--position-mode", choices=("midpoint", "start", "end"), default="midpoint", help="Boundary coordinate used for matching.")
    parser.add_argument("--out-dir", type=Path, default=Path(DEFAULT_OUT_DIR), help="Output directory.")
    parser.add_argument("--dry-run", action="store_true", help="Print comparison plan without writing output files.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.boundary_tol_bp is not None and args.boundary_tol_bp < 0:
            raise ValueError("--boundary-tol-bp must be non-negative")
        if args.boundary_tol_bins < 0:
            raise ValueError("--boundary-tol-bins must be non-negative")
        if args.allow_mixed_resolution and args.boundary_tol_bp is None:
            raise ValueError("--allow-mixed-resolution requires explicit --boundary-tol-bp")

        boundaries_by_sample, sample_stats = collect_inputs(args)
        samples = sorted(boundaries_by_sample)
        pairs = comparison_pairs(samples, args)
        if not pairs:
            raise ValueError("at least two samples are required for boundary comparison")

        print(f"[INFO] samples: {', '.join(samples)}")
        print(f"[INFO] comparisons: {', '.join(f'{a}_vs_{b}' for a, b in pairs)}")

        if args.dry_run:
            for a, b in pairs:
                resolution_label, tolerance_bp, source = comparison_resolution(
                    a,
                    b,
                    boundaries_by_sample[a],
                    boundaries_by_sample[b],
                    args,
                )
                print(
                    f"[DRY-RUN] {a} vs {b}: resolution={resolution_label}, "
                    f"boundary_tol_bp={tolerance_bp} ({source})"
                )
            return 0

        all_summary: List[Dict[str, object]] = []
        all_matches: List[Dict[str, object]] = []
        all_long: List[Dict[str, object]] = []
        for a, b in pairs:
            summary, matches, long_rows = summarize_comparison(
                a,
                b,
                boundaries_by_sample[a],
                boundaries_by_sample[b],
                args,
            )
            all_summary.append(summary)
            all_matches.extend(matches)
            all_long.extend(long_rows)

        out_dir = args.out_dir
        write_tsv(
            out_dir / "tad_boundary_sample_stats.tsv",
            sample_stats,
            [
                "sample",
                "resolution_bp",
                "resolution_source",
                "source_files",
                "n_boundaries_input",
                "n_boundaries_unique_positions",
                "n_duplicate_positions_removed",
            ],
        )
        write_tsv(
            out_dir / "tad_boundary_change_summary.tsv",
            all_summary,
            [
                "comparison",
                "sample_a",
                "sample_b",
                "resolution_bp",
                "boundary_tol_bp",
                "boundary_tol_bins",
                "n_a_boundaries",
                "n_b_boundaries",
                "n_matched",
                "n_shared_exact",
                "n_shifted_within_tol",
                "n_a_specific",
                "n_b_specific",
                "fraction_a_matched",
                "fraction_b_matched",
                "mean_abs_shift_bp",
                "median_abs_shift_bp",
                "max_abs_shift_bp",
            ],
        )
        write_tsv(
            out_dir / "tad_boundary_matches.tsv",
            all_matches,
            [
                "comparison",
                "sample_a",
                "sample_b",
                "chrom",
                "a_start",
                "a_end",
                "a_position",
                "a_id",
                "a_score",
                "b_start",
                "b_end",
                "b_position",
                "b_id",
                "b_score",
                "delta_bp",
                "abs_delta_bp",
                "match_class",
                "boundary_tol_bp",
                "a_source_file",
                "b_source_file",
            ],
        )
        write_tsv(
            out_dir / "tad_boundary_changes_long.tsv",
            all_long,
            [
                "comparison",
                "sample",
                "compared_to",
                "role",
                "change_class",
                "chrom",
                "start",
                "end",
                "position",
                "id",
                "separation_score",
                "matched_sample",
                "matched_position",
                "delta_bp",
                "abs_delta_bp",
                "resolution_bp",
                "boundary_tol_bp",
                "source_file",
            ],
        )
        write_tsv(
            out_dir / "tad_boundary_change_run_summary.tsv",
            [
                {
                    "n_samples": len(samples),
                    "samples": ",".join(samples),
                    "n_comparisons": len(pairs),
                    "comparisons": ",".join(f"{a}_vs_{b}" for a, b in pairs),
                    "position_mode": args.position_mode,
                    "boundary_tol_bp": args.boundary_tol_bp if args.boundary_tol_bp is not None else "",
                    "boundary_tol_bins": args.boundary_tol_bins,
                    "allow_mixed_resolution": args.allow_mixed_resolution,
                    "command": " ".join(sys.argv),
                }
            ],
            [
                "n_samples",
                "samples",
                "n_comparisons",
                "comparisons",
                "position_mode",
                "boundary_tol_bp",
                "boundary_tol_bins",
                "allow_mixed_resolution",
                "command",
            ],
        )
        print(
            f"[INFO] summary: samples={len(samples)}, comparisons={len(pairs)}, "
            f"matched_pairs={len(all_matches)}, classified_boundaries={len(all_long)}"
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
