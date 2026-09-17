"""Local GWAS input checks; no inference of unrecorded sample identities."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from pathlib import Path

MAX_TABLE_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 4 * 1024 * 1024
ID_NAMES = ("iid", "sample_id", "sample", "id", "fid")
MISSING = {"", "NA", "NaN", "nan", ".", "null", "NULL"}


class InputError(ValueError):
    def __init__(self, code, message, **facts):
        super().__init__(message)
        self.facts = {"code": code, "message": message, **facts}


def fail(code, message, **facts):
    raise InputError(code, message, **facts)


def read_bytes(path: Path):
    with path.open("rb") as handle:
        content = handle.read(MAX_TABLE_BYTES + 1)
    if len(content) > MAX_TABLE_BYTES:
        fail("INPUT_TABLE_SIZE_LIMIT", "Table exceeds the local bounded reader limit", path=str(path), limit_bytes=MAX_TABLE_BYTES)
    return content


def split_row(line):
    return [value.strip() for value in line.rstrip("\r\n").split("\t")] if "\t" in line else line.split()


def grid(path: Path):
    content = read_bytes(path)
    rows = [split_row(line) for line in content.decode("utf-8-sig").splitlines() if line.strip()]
    if not rows:
        fail("INPUT_TABLE_EMPTY", "Input table has no rows", path=str(path))
    width = len(rows[0])
    malformed = [i + 1 for i, row in enumerate(rows) if len(row) != width]
    if malformed:
        fail("INPUT_TABLE_RAGGED", "Rows have inconsistent column counts", path=str(path), rows=malformed[:20], expected_columns=width)
    return rows, {"path": str(path.resolve()), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "identity_scope": "full_file"}


def numeric(value):
    try:
        return math.isfinite(float(value))
    except ValueError:
        return False


def numeric_token(value):
    """Numeric syntax identifies no sample column, even for non-finite data."""
    try:
        float(value)
        return True
    except ValueError:
        return False


def unique_ids(ids, *, origin):
    seen, duplicates = set(), []
    for sample in ids:
        if sample in MISSING:
            fail("SAMPLE_ID_MISSING", "A sample ID is empty or a missing-value token", origin=origin)
        if sample in seen and sample not in duplicates:
            duplicates.append(sample)
        seen.add(sample)
    if duplicates:
        fail("SAMPLE_ID_DUPLICATE", "Sample IDs must identify one row each", origin=origin, duplicate_ids=duplicates[:20])


def table(path: Path, *, header_mode="auto", sample_column=None, require_ids=True):
    rows, source = grid(path)
    # A first sample literally named "ID" beside numeric/missing data is not a header.
    # Numeric or otherwise ambiguous headers must use header_mode="present".
    named_id = any(value.lower() in ID_NAMES for value in rows[0]) and all(
        not numeric_token(value) and value not in MISSING for value in rows[0])
    q_header = all(re.fullmatch(r"(?:q|pc)\d+", value, re.IGNORECASE) for value in rows[0])
    has_header = header_mode == "present" or (header_mode == "auto" and (named_id or q_header))
    header, data_rows = (rows[0], rows[1:]) if has_header else (None, rows)
    if not data_rows:
        fail("INPUT_TABLE_EMPTY", "Header has no data rows", path=str(path))
    if header and len(set(header)) != len(header):
        fail("INPUT_HEADER_DUPLICATE", "Header names must be unique", path=str(path))
    column = None
    if sample_column is not None:
        text = str(sample_column)
        if text.isdigit() and int(text) >= 1:
            column = int(text) - 1
        elif header and text in header:
            column = header.index(text)
        else:
            fail("SAMPLE_COLUMN_INVALID", "Use an existing header name or a positive 1-based sample column", sample_column=text)
    elif header:
        for name in ID_NAMES:
            column = next((i for i, value in enumerate(header) if value.lower() == name), None)
            if column is not None:
                break
    elif not all(numeric_token(row[0]) or row[0] in MISSING for row in data_rows):
        column = 0
    if column is not None and column >= len(data_rows[0]):
        fail("SAMPLE_COLUMN_INVALID", "Sample column exceeds table width", sample_column=column + 1)
    if column is None and require_ids:
        fail("SAMPLE_ID_COLUMN_UNRESOLVED", "Numeric or unlabeled covariates do not establish sample IDs; provide explicit structure or a validated Q row map", path=str(path))
    ids = [row[column] for row in data_rows] if column is not None else []
    if ids:
        unique_ids(ids, origin=str(path))
    return {"header": header, "rows": data_rows, "sample_column": column, "ids": ids, "source": source,
        "row_count": len(data_rows), "column_count": len(data_rows[0]), "header_present": bool(header)}


def tfam(path: Path):
    rows, source = grid(path)
    if len(rows[0]) < 2:
        fail("TFAM_ROW_INVALID", "TFAM requires FID and IID columns", path=str(path))
    pairs = [(row[0], row[1]) for row in rows]
    unique_ids([iid for _, iid in pairs], origin="TFAM IID")
    return pairs, source


def compare_ids(target, observed):
    missing = sorted(set(target) - set(observed))
    extra = sorted(set(observed) - set(target))
    return {"target_count": len(target), "observed_count": len(observed), "missing_ids": missing,
        "extra_ids": extra, "same_ids": not missing and not extra,
        "order_matches": target == observed, "alignment": "identical" if target == observed else "reorder_required" if not missing and not extra else "ids_differ"}


def q_row_map(path: Path, q, pairs, target_source):
    content = read_bytes(path)
    try:
        mapping = json.loads(content)
    except (ValueError, UnicodeError):
        fail("Q_ROW_MAP_INVALID", "Q mapping must be an explicit JSON row assignment")
    required = {"schema_version", "q_sha256", "tfam_sha256", "basis", "rows"}
    if not isinstance(mapping, dict) or set(mapping) != required or type(mapping["schema_version"]) is not int or mapping["schema_version"] != 1:
        fail("Q_ROW_MAP_INVALID", "Expected the documented local Q mapping v1 fields")
    if mapping["q_sha256"] != q["source"]["sha256"] or mapping["tfam_sha256"] != target_source["sha256"]:
        fail("Q_ROW_MAP_VERSION_MISMATCH", "Q matrix or target TFAM changed since the explicit mapping was recorded")
    if not isinstance(mapping["basis"], str) or not mapping["basis"].strip():
        fail("Q_ROW_MAP_BASIS_MISSING", "Record the approved row correspondence or generating-source basis; file existence and equal counts are insufficient")
    assignments = mapping["rows"]
    if not isinstance(assignments, list) or len(assignments) != q["row_count"]:
        fail("Q_ROW_MAP_COVERAGE_INVALID", "The mapping must cover every physical Q data row exactly once")
    ids = []
    for index, row in enumerate(assignments, 1):
        if not isinstance(row, dict) or set(row) != {"q_row", "sample_id"} or type(row["q_row"]) is not int or row["q_row"] != index:
            fail("Q_ROW_MAP_ROW_ORDER_INVALID", "Assignments must enumerate physical data rows 1..N in order; header is excluded")
        if not isinstance(row["sample_id"], str):
            fail("Q_ROW_MAP_INVALID", "Mapping sample IDs must be strings")
        ids.append(row["sample_id"])
    unique_ids(ids, origin="Q row map")
    comparison = compare_ids([iid for _, iid in pairs], ids)
    if not comparison["same_ids"]:
        fail("Q_ROW_MAP_SAMPLE_MISMATCH", "Q mapping does not cover the target genotype sample identities", **comparison)
    if q["ids"] and q["ids"] != ids:
        fail("Q_ROW_MAP_ID_CONFLICT", "The explicit map conflicts with the Q file's own sample IDs")
    return ids, {"status": "explicit_mapping_validated", "mapping_sha256": hashlib.sha256(content).hexdigest(),
        "q_sha256": mapping["q_sha256"], "tfam_sha256": mapping["tfam_sha256"], "basis": mapping["basis"],
        "comparison": comparison, "authority": "supplied row assignment; not independent proof of biological provenance"}


def vcf_header(path: Path, limit=MAX_HEADER_BYTES):
    opener = gzip.open if path.suffix.lower() in {".gz", ".bgz", ".bgzf"} else open
    content = bytearray()
    with opener(path, "rb") as handle:
        while len(content) <= limit:
            line = handle.readline(limit + 1 - len(content))
            if not line:
                break
            content.extend(line)
            if len(content) > limit:
                fail("VCF_HEADER_LIMIT", "VCF header exceeds bounded decoded-byte limit", limit_bytes=limit)
            if line.startswith(b"#CHROM\t"):
                columns = line.decode("utf-8").rstrip("\r\n").split("\t")
                if columns[:9] != ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"] or len(columns) < 10:
                    fail("VCF_SAMPLE_HEADER_INVALID", "VCF header lacks the sample columns required for alignment")
                ids = columns[9:]
                unique_ids(ids, origin="VCF header")
                return {"sample_ids": ids, "sample_count": len(ids), "decoded_bytes_read": len(content),
                    "source": {"path": str(path.resolve()), "header_sha256": hashlib.sha256(content).hexdigest(),
                        "identity_scope": "header_only", "file_bytes": path.stat().st_size}, "whole_vcf_validated": False}
            if not line.startswith(b"#"):
                fail("VCF_SAMPLE_HEADER_MISSING", "Variant data appeared before the VCF sample header")
    fail("VCF_SAMPLE_HEADER_MISSING", "No sample header was found within the bounded read")
