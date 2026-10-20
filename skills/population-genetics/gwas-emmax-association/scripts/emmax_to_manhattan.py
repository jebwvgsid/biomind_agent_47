#!/usr/bin/env python3
"""Join EMMAX INTEL .ps results to TPED metadata by unique marker ID.

Default/only implemented schema: four whitespace-separated, headerless columns
SNP_ID, beta, SE(beta), P. Other formats require a separately named, documented
schema implementation; this converter never guesses a P column. The existing
--tped/--emmax/--out CLI and SNP/CHR/BP/P output header are retained.

Validation completes before atomically replacing --out. JSON on stdout records
counts and source hashes on success or failure; nonzero exit blocks consumers.
Only TPED marker metadata is validated here, not genotype calls or model validity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path


DECIMAL_TOKEN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class Problems:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def add(self, code: str, example: dict, count: int = 1) -> None:
        item = self.items.setdefault(code, {"count": 0, "examples": []})
        item["count"] += count
        if len(item["examples"]) < 5:
            item["examples"].append(example)


def source_lines(path: Path, info: dict, problems: Problems):
    """Read once; hash the exact bytes, including blank and malformed rows."""
    digest = hashlib.sha256()
    info.update(path=str(path.resolve()), physical_lines=0, data_rows=0)
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            digest.update(raw)
            info["physical_lines"] += 1
            if not raw.strip():
                continue
            info["data_rows"] += 1
            try:
                yield number, raw.decode("utf-8")
            except UnicodeDecodeError:
                problems.add("invalid_utf8", {"file": str(path), "line": number})
    info["sha256"] = digest.hexdigest()


def read_tped(path: Path, info: dict, problems: Problems) -> dict:
    markers = {}
    seen = set()
    for number, line in source_lines(path, info, problems):
        # The first four fields are CHR, ID, cM, BP; genotypes are not parsed.
        fields = line.split(maxsplit=4)
        if len(fields) < 4:
            problems.add("malformed_tped", {"line": number, "reason": "fewer_than_4_fields"})
            continue
        chrom, marker, _cm, bp = fields[:4]
        if marker == "." or chrom == ".":
            problems.add("malformed_tped", {"line": number, "reason": "missing_marker_or_chromosome"})
            continue
        if marker in seen:
            problems.add("duplicate_tped_id", {"line": number, "id": marker})
            continue
        seen.add(marker)
        try:
            valid_bp = bp.isascii() and bp.isdecimal() and int(bp) > 0
        except ValueError:
            valid_bp = False
        if not valid_bp:
            problems.add("invalid_bp", {"line": number, "id": marker, "value": bp})
            continue
        markers[marker] = (chrom, bp)
    info["unique_ids"] = len(seen)
    info["valid_metadata_ids"] = len(markers)
    if not info["data_rows"]:
        problems.add("empty_tped", {"file": str(path)})
    return markers


def read_emmax(path: Path, info: dict, problems: Problems) -> dict:
    values = {}
    info.update(valid_p_rows=0, invalid_p_rows=0)
    for number, line in source_lines(path, info, problems):
        fields = line.split()
        if len(fields) != 4:
            problems.add("emmax_schema_mismatch", {"line": number, "expected_columns": 4, "actual_columns": len(fields)})
            continue
        marker, _beta, _se, p_text = fields
        if marker == ".":
            problems.add("missing_emmax_id", {"line": number})
            continue
        duplicate = marker in values
        if duplicate:
            problems.add("duplicate_emmax_id", {"line": number, "id": marker})
        try:
            p = Decimal(p_text)
            valid = bool(DECIMAL_TOKEN.fullmatch(p_text)) and p.is_finite() and Decimal(0) < p <= Decimal(1)
        except InvalidOperation:
            valid = False
        if not valid:
            info["invalid_p_rows"] += 1
            problems.add("invalid_p", {"line": number, "id": marker, "value": p_text})
        else:
            info["valid_p_rows"] += 1
        if not duplicate:
            # Keep the ID even when P is invalid so set reconciliation is honest.
            values[marker] = p_text if valid else None
    info["unique_ids"] = len(values)
    if not info["data_rows"]:
        problems.add("empty_emmax", {"file": str(path)})
    return values


def same_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and os.path.samefile(left, right)


def write_output(path: Path, markers: dict, values: dict) -> str:
    """Leave a pre-existing output unchanged on validation/write failure."""
    temporary = None
    digest = hashlib.sha256()
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=path.name + ".tmp.", delete=False) as handle:
            temporary = Path(handle.name)
            header = b"SNP\tCHR\tBP\tP\n"
            handle.write(header)
            digest.update(header)
            for marker, (chrom, bp) in markers.items():
                row = f"{marker}\t{chrom}\t{bp}\t{values[marker]}\n".encode("utf-8")
                handle.write(row)
                digest.update(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tped", required=True, type=Path)
    parser.add_argument("--emmax", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--schema", choices=("emmax-intel4",), default="emmax-intel4",
                        help="named schema; default emmax-intel4 = ID, beta, SE(beta), P; no autodetection")
    args = parser.parse_args()
    problems = Problems()
    audit = {
        "schema_version": 1,
        "status": "failed",
        "emmax_schema": args.schema,
        "tped": {},
        "emmax": {},
        "output": {"path": str(args.out.resolve()), "rows": 0, "published": False},
        "valid_tested_markers": None,
        "validation_scope": "marker identity, coordinates and P values; not association-model validity",
    }
    try:
        if same_file(args.out, args.tped) or same_file(args.out, args.emmax):
            problems.add("output_overwrites_input", {"path": str(args.out)})
        else:
            markers = read_tped(args.tped, audit["tped"], problems)
            values = read_emmax(args.emmax, audit["emmax"], problems)
            missing = markers.keys() - values.keys()
            extra = values.keys() - markers.keys()
            matched = len(markers.keys() & values.keys())
            audit["reconciliation"] = {
                "matched_ids": matched,
                "missing_in_emmax": len(missing),
                "extra_in_emmax": len(extra),
                "set_policy": "exact_equality_required",
            }
            if missing:
                problems.add("missing_emmax_ids", {"ids": sorted(missing)[:5]}, len(missing))
            if extra:
                problems.add("extra_emmax_ids", {"ids": sorted(extra)[:5]}, len(extra))
            if not problems.items:
                reordered = any(left != right for left, right in zip(markers, values))
                # Equality, uniqueness, schema and P checks above make this a
                # one-to-one lookup; no positional pairing or truncation occurs.
                output_hash = write_output(args.out, markers, values)
                audit["output"].update(rows=len(markers), published=True, sha256=output_hash,
                                       order="tped", emmax_rows_reordered=reordered)
                audit["valid_tested_markers"] = len(markers)
                audit["status"] = "passed"
    except (OSError, UnicodeError) as exc:
        problems.add("io_error", {"error": str(exc)})
    audit["issues"] = problems.items
    print(json.dumps(audit, indent=2, ensure_ascii=False, allow_nan=False))
    if audit["status"] != "passed":
        print("[ERROR] Conversion failed; no new result was published. See JSON counts and issues.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
