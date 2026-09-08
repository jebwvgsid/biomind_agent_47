"""Deterministic, read-only contig parsing for genomics completion predicates.

This module deliberately does not interpret generated shell text.  It reads the
Controller-approved input paths, validates the relevant text formats, and emits
a content-addressed receipt that can be recomputed at CommitStep.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, TextIO


CONTIG_PARSER_ID = "biomind.genomics.contig_compatibility.v1"
CONTIG_PARSER_VERSION = 1
_REPORT_LIMIT = 64


class ContigParserError(ValueError):
    """A stable, format-specific failure from the deterministic parser."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "CONTIG_PARSER_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class ParsedContigs:
    contigs: frozenset[str]
    record_count: int
    declared_contigs: frozenset[str] = frozenset()
    record_contigs: frozenset[str] = frozenset()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _open_text(path: Path) -> TextIO:
    try:
        with path.open("rb") as handle:
            magic = handle.read(2)
    except OSError as exc:
        raise ContigParserError("CONTIG_INPUT_UNREADABLE", str(path)) from exc
    try:
        if magic == b"\x1f\x8b":
            return gzip.open(path, "rt", encoding="utf-8", errors="strict", newline="")
        return path.open("rt", encoding="utf-8", errors="strict", newline="")
    except OSError as exc:
        raise ContigParserError("CONTIG_INPUT_UNREADABLE", str(path)) from exc


def _contig_id(value: Any, *, source: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or any(character.isspace() for character in text)
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or "," in text
    ):
        raise ContigParserError("CONTIG_IDENTIFIER_INVALID", f"{source}:{text[:120]}")
    return text


def _vcf_structured_attributes(payload: str, *, line_number: int) -> Dict[str, str]:
    """Parse one VCF structured meta-value without splitting quoted commas.

    VCF structured header values are comma-separated only outside double
    quotes.  In particular, a Description such as ``"x,ID=fake"`` must not be
    mistaken for the actual ID attribute.
    """

    fields: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in payload:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if quoted and character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == '"':
            current.append(character)
            quoted = not quoted
            continue
        if character == "," and not quoted:
            fields.append("".join(current))
            current = []
            continue
        current.append(character)
    if quoted or escaped:
        raise ContigParserError(
            "VCF_CONTIG_HEADER_QUOTE_INVALID", f"line={line_number}"
        )
    fields.append("".join(current))

    attributes: Dict[str, str] = {}
    for raw_field in fields:
        field = raw_field.strip()
        key, separator, raw_value = field.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()
        if not separator or not key:
            raise ContigParserError(
                "VCF_CONTIG_HEADER_ATTRIBUTE_INVALID", f"line={line_number}"
            )
        if key in attributes:
            raise ContigParserError(
                "VCF_CONTIG_HEADER_ATTRIBUTE_DUPLICATED",
                f"line={line_number};attribute={key}",
            )
        if raw_value.startswith('"'):
            if len(raw_value) < 2 or not raw_value.endswith('"'):
                raise ContigParserError(
                    "VCF_CONTIG_HEADER_QUOTE_INVALID", f"line={line_number}"
                )
            value = raw_value[1:-1]
            decoded: list[str] = []
            escaped = False
            for character in value:
                if escaped:
                    decoded.append(character)
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    raise ContigParserError(
                        "VCF_CONTIG_HEADER_QUOTE_INVALID", f"line={line_number}"
                    )
                else:
                    decoded.append(character)
            if escaped:
                raise ContigParserError(
                    "VCF_CONTIG_HEADER_QUOTE_INVALID", f"line={line_number}"
                )
            value = "".join(decoded)
        else:
            if '"' in raw_value:
                raise ContigParserError(
                    "VCF_CONTIG_HEADER_QUOTE_INVALID", f"line={line_number}"
                )
            value = raw_value
        attributes[key] = value
    return attributes


def parse_vcf_contigs(path: str | Path) -> ParsedContigs:
    source = Path(path)
    fileformat_seen = False
    column_header_seen = False
    declared: set[str] = set()
    records: set[str] = set()
    record_count = 0
    try:
        with _open_text(source) as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if line.startswith("##fileformat=VCFv"):
                    fileformat_seen = True
                    continue
                if line.startswith("##contig=<"):
                    if not line.endswith(">"):
                        raise ContigParserError(
                            "VCF_CONTIG_HEADER_INVALID", f"line={line_number}"
                        )
                    attributes = _vcf_structured_attributes(
                        line[len("##contig=<") : -1], line_number=line_number
                    )
                    if "ID" not in attributes:
                        raise ContigParserError(
                            "VCF_CONTIG_HEADER_ID_MISSING", f"line={line_number}"
                        )
                    if not attributes["ID"].strip():
                        raise ContigParserError(
                            "VCF_CONTIG_HEADER_ID_EMPTY", f"line={line_number}"
                        )
                    contig = _contig_id(
                        attributes["ID"], source=f"vcf:{line_number}"
                    )
                    if contig in declared:
                        raise ContigParserError(
                            "VCF_CONTIG_HEADER_ID_DUPLICATED",
                            f"line={line_number};ID={contig}",
                        )
                    declared.add(contig)
                    continue
                if line.startswith("##"):
                    continue
                if line.startswith("#CHROM"):
                    if column_header_seen:
                        raise ContigParserError(
                            "VCF_COLUMN_HEADER_DUPLICATED", f"line={line_number}"
                        )
                    columns = line.split("\t")
                    expected = ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]
                    if columns[:8] != expected:
                        raise ContigParserError(
                            "VCF_COLUMN_HEADER_INVALID", f"line={line_number}"
                        )
                    column_header_seen = True
                    continue
                if not line:
                    continue
                if line.startswith("#"):
                    raise ContigParserError("VCF_HEADER_INVALID", f"line={line_number}")
                if not column_header_seen:
                    raise ContigParserError(
                        "VCF_RECORD_BEFORE_COLUMN_HEADER", f"line={line_number}"
                    )
                fields = line.split("\t")
                if len(fields) < 8:
                    raise ContigParserError("VCF_RECORD_FIELD_COUNT_INVALID", f"line={line_number}")
                try:
                    position = int(fields[1])
                except ValueError as exc:
                    raise ContigParserError("VCF_POSITION_INVALID", f"line={line_number}") from exc
                if position < 1 or not fields[3] or not fields[4]:
                    raise ContigParserError("VCF_RECORD_INVALID", f"line={line_number}")
                records.add(_contig_id(fields[0], source=f"vcf:{line_number}"))
                record_count += 1
    except UnicodeError as exc:
        raise ContigParserError("CONTIG_INPUT_ENCODING_INVALID", str(source)) from exc
    except OSError as exc:
        raise ContigParserError("CONTIG_INPUT_READ_FAILED", str(source)) from exc

    if not fileformat_seen:
        raise ContigParserError("VCF_FILEFORMAT_HEADER_MISSING", str(source))
    if not column_header_seen:
        raise ContigParserError("VCF_COLUMN_HEADER_MISSING", str(source))
    # Records are the authoritative set of variants that will be consumed.
    # For an empty VCF, declarations remain the only bounded compatibility
    # evidence.  Keep both sets separately in every case.
    contigs = records if records else declared
    if not contigs:
        raise ContigParserError("VCF_CONTIG_EVIDENCE_EMPTY", str(source))
    return ParsedContigs(
        contigs=frozenset(contigs),
        record_count=record_count,
        declared_contigs=frozenset(declared),
        record_contigs=frozenset(records),
    )


def parse_fasta_contigs(path: str | Path) -> ParsedContigs:
    source = Path(path)
    contigs: set[str] = set()
    sequence_lines = 0
    try:
        with _open_text(source) as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if line.startswith(">"):
                    token = line[1:].strip().split(maxsplit=1)[0] if line[1:].strip() else ""
                    contigs.add(_contig_id(token, source=f"fasta:{line_number}"))
                elif line.strip():
                    if not contigs:
                        raise ContigParserError(
                            "FASTA_SEQUENCE_BEFORE_HEADER", f"line={line_number}"
                        )
                    sequence_lines += 1
    except UnicodeError as exc:
        raise ContigParserError("CONTIG_INPUT_ENCODING_INVALID", str(source)) from exc
    except OSError as exc:
        raise ContigParserError("CONTIG_INPUT_READ_FAILED", str(source)) from exc
    if not contigs:
        raise ContigParserError("FASTA_CONTIG_EVIDENCE_EMPTY", str(source))
    return ParsedContigs(frozenset(contigs), sequence_lines)


def parse_gtf_contigs(path: str | Path) -> ParsedContigs:
    source = Path(path)
    contigs: set[str] = set()
    record_count = 0
    try:
        with _open_text(source) as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) != 9:
                    raise ContigParserError("GTF_RECORD_FIELD_COUNT_INVALID", f"line={line_number}")
                try:
                    start = int(fields[3])
                    end = int(fields[4])
                except ValueError as exc:
                    raise ContigParserError("GTF_COORDINATE_INVALID", f"line={line_number}") from exc
                if start < 1 or end < start:
                    raise ContigParserError("GTF_COORDINATE_INVALID", f"line={line_number}")
                contigs.add(_contig_id(fields[0], source=f"gtf:{line_number}"))
                record_count += 1
    except UnicodeError as exc:
        raise ContigParserError("CONTIG_INPUT_ENCODING_INVALID", str(source)) from exc
    except OSError as exc:
        raise ContigParserError("CONTIG_INPUT_READ_FAILED", str(source)) from exc
    if not contigs or record_count < 1:
        raise ContigParserError("GTF_CONTIG_EVIDENCE_EMPTY", str(source))
    return ParsedContigs(frozenset(contigs), record_count)


def _bounded(values: Iterable[str]) -> Dict[str, Any]:
    ordered = sorted({str(value) for value in values})
    return {
        "count": len(ordered),
        "values": ordered[:_REPORT_LIMIT],
        "truncated": len(ordered) > _REPORT_LIMIT,
        "set_hash": _digest(ordered),
    }


def _source_identity(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ContigParserError("CONTIG_INPUT_UNREADABLE", str(path)) from exc
    if not path.is_file() or stat.st_size <= 0:
        raise ContigParserError("CONTIG_INPUT_EMPTY_OR_NOT_FILE", str(path))
    return {
        "path": str(path),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def build_contig_compatibility_receipt(
    input_paths: Mapping[str, str | Path],
) -> Dict[str, Any]:
    """Parse the exact VCF/FASTA/GTF inputs and return a bounded receipt."""

    required = ("variant_vcf", "reference_fasta", "annotation_gtf")
    missing = [role for role in required if not str(input_paths.get(role) or "").strip()]
    if missing:
        raise ContigParserError("CONTIG_INPUT_ROLE_MISSING", ",".join(missing))
    paths = {role: Path(str(input_paths[role])) for role in required}
    noncanonical = [role for role, path in paths.items() if not path.is_absolute()]
    if noncanonical:
        raise ContigParserError(
            "CONTIG_INPUT_PATH_NOT_CANONICAL", ",".join(noncanonical)
        )
    sources = {role: _source_identity(path) for role, path in paths.items()}
    vcf = parse_vcf_contigs(paths["variant_vcf"])
    fasta = parse_fasta_contigs(paths["reference_fasta"])
    gtf = parse_gtf_contigs(paths["annotation_gtf"])

    missing_vcf_in_fasta = vcf.contigs - fasta.contigs
    missing_vcf_in_gtf = vcf.contigs - gtf.contigs
    missing_gtf_in_fasta = gtf.contigs - fasta.contigs
    compatible = not any(
        (missing_vcf_in_fasta, missing_vcf_in_gtf, missing_gtf_in_fasta)
    )
    body: Dict[str, Any] = {
        "schema_version": 1,
        "parser_id": CONTIG_PARSER_ID,
        "parser_version": CONTIG_PARSER_VERSION,
        "parser_status": "valid",
        "value": compatible,
        "source_inputs": sources,
        "vcf_record_count": vcf.record_count,
        "gtf_record_count": gtf.record_count,
        "vcf_contigs": _bounded(vcf.contigs),
        "vcf_declared_contigs": _bounded(vcf.declared_contigs),
        "vcf_record_contigs": _bounded(vcf.record_contigs),
        "vcf_declared_not_observed": _bounded(
            vcf.declared_contigs - vcf.record_contigs
        ),
        "vcf_observed_not_declared": _bounded(
            vcf.record_contigs - vcf.declared_contigs
        ),
        "fasta_contigs": _bounded(fasta.contigs),
        "annotation_contigs": _bounded(gtf.contigs),
        "missing_vcf_in_fasta": _bounded(missing_vcf_in_fasta),
        "missing_vcf_in_annotation": _bounded(missing_vcf_in_gtf),
        "missing_annotation_in_fasta": _bounded(missing_gtf_in_fasta),
    }
    body["receipt_id"] = "contig-receipt-" + _digest(body).removeprefix("sha256:")
    return body


__all__ = [
    "CONTIG_PARSER_ID",
    "CONTIG_PARSER_VERSION",
    "ContigParserError",
    "ParsedContigs",
    "build_contig_compatibility_receipt",
    "parse_fasta_contigs",
    "parse_gtf_contigs",
    "parse_vcf_contigs",
]
