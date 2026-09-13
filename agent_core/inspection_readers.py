"""Bounded representations of an already authorized regular-file stream.

This module grants no access and executes no programs. Recovery resolves its
manifest before opening a stream; approved Skill actions use the same reader.
Prefix inspection never certifies a whole scientific input.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import time
import zlib
from dataclasses import dataclass
from typing import Any, BinaryIO


class InspectionReadError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(code + (": " + detail if detail else ""))


@dataclass(frozen=True)
class ReadLimits:
    input_bytes: int = 1024 * 1024
    decoded_bytes: int = 64 * 1024
    line_bytes: int = 16 * 1024
    records: int = 32
    lines: int = 120
    seconds: float = 2.0

    def __post_init__(self) -> None:
        for key, ceiling in (("input_bytes", 8 * 1024 * 1024),
                             ("decoded_bytes", 256 * 1024), ("line_bytes", 64 * 1024),
                             ("records", 256), ("lines", 1024)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise InspectionReadError("READ_LIMIT_INVALID", key)
        if not 0 < self.seconds <= 10:
            raise InspectionReadError("READ_LIMIT_INVALID", "seconds")


class _InputBudget:
    def __init__(self, stream: BinaryIO, limits: ReadLimits, clock: Any) -> None:
        self.stream, self.limits, self.clock = stream, limits, clock
        self.deadline = clock() + limits.seconds
        self.consumed = 0

    def check(self) -> None:
        if self.clock() >= self.deadline:
            raise InspectionReadError("READ_TIMEOUT", "bounded inspection deadline")

    def read(self, size: int = -1) -> bytes:
        self.check()
        remaining = self.limits.input_bytes - self.consumed
        if remaining <= 0:
            raise InspectionReadError("READ_INPUT_LIMIT", "input scan budget reached")
        result = self.stream.read(min(remaining, 8192, size if size >= 0 else 8192))
        self.consumed += len(result)
        self.check()
        return result

    def seekable(self) -> bool:
        return False


def inspect_stream(
    stream: BinaryIO, *, representation: str = "text", limits: ReadLimits | None = None,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    """Read text, gzip text or a four-line FASTQ prefix with explicit bounds.

    A caller's executor supplies its outer wall-time limit as well: deadline
    checks cannot interrupt an operating-system read blocked in a filesystem.
    All calls here are sized, including gzip's compressed input requests.
    """
    limits = limits or ReadLimits()
    if representation not in {"text", "gzip_text", "fastq_prefix"}:
        raise InspectionReadError("READ_REPRESENTATION_UNSUPPORTED", representation)
    bounded = _InputBudget(stream, limits, clock)
    first = bounded.read(2)
    compressed = first == b"\x1f\x8b"
    if representation == "gzip_text" and not compressed:
        raise InspectionReadError("READ_FORMAT_MISMATCH", "gzip magic required")
    if representation == "text" and compressed:
        raise InspectionReadError("READ_REPRESENTATION_UNSUPPORTED", "use gzip_text or fastq_prefix")

    class Prefix:
        def __init__(self) -> None:
            self.prefix = first

        def read(self, size: int = -1) -> bytes:
            bounded.check()
            if self.prefix:
                count = len(self.prefix) if size < 0 else size
                value, self.prefix = self.prefix[:count], self.prefix[count:]
                return value
            return bounded.read(size)

        def seekable(self) -> bool:
            return False

    source = gzip.GzipFile(fileobj=Prefix(), mode="rb") if compressed else Prefix()
    pending = bytearray()
    decoded = 0
    eof = False
    lines: list[str] = []
    records = 0
    stop = ""
    technical_error: dict[str, str] | None = None
    content_valid: bool | None = None

    def line() -> bytes:
        nonlocal decoded, eof
        while b"\n" not in pending and not eof:
            bounded.check()
            available = min(limits.decoded_bytes - decoded, limits.line_bytes + 1 - len(pending))
            if available <= 0:
                code = "READ_LINE_LIMIT" if len(pending) >= limits.line_bytes else "READ_DECODED_LIMIT"
                raise InspectionReadError(code, "prefix coverage incomplete")
            chunk = source.read(min(available, 4096))
            decoded += len(chunk)
            if not chunk:
                eof = True
                break
            pending.extend(chunk)
            if len(pending.split(b"\n", 1)[0]) > limits.line_bytes:
                raise InspectionReadError("READ_LINE_LIMIT", "record representation exceeds limit")
        if b"\n" in pending:
            pos = pending.index(b"\n") + 1
        else:
            pos = len(pending)
        if pos - (1 if pending[:pos].endswith(b"\n") else 0) > limits.line_bytes:
            raise InspectionReadError("READ_LINE_LIMIT", "record representation exceeds limit")
        value = bytes(pending[:pos]); del pending[:pos]
        return value

    try:
        target = limits.records * 4 if representation == "fastq_prefix" else limits.lines
        while len(lines) < target:
            value = line()
            if not value:
                stop = "eof"
                break
            if b"\x00" in value:
                raise InspectionReadError("READ_REPRESENTATION_UNSUPPORTED", "binary content; use an authorized diagnostic")
            try:
                lines.append(value.decode("utf-8").rstrip("\r\n"))
            except UnicodeDecodeError as exc:
                raise InspectionReadError("READ_REPRESENTATION_UNSUPPORTED", "non UTF-8 content; use an authorized diagnostic") from exc
            if representation == "fastq_prefix" and len(lines) % 4 == 0:
                header, sequence, plus, quality = lines[-4:]
                if not (header.startswith("@") and plus.startswith("+") and sequence
                        and len(sequence) == len(quality)):
                    content_valid = False
                    stop = "invalid_fastq_in_checked_range"
                    break
                records += 1
        if not stop:
            stop = "record_limit" if representation == "fastq_prefix" else "line_limit"
        if representation == "fastq_prefix" and content_valid is None:
            content_valid = bool(records) and len(lines) % 4 == 0
            if not records and not lines and eof:
                stop = "empty_input"
            elif len(lines) % 4:
                stop = "incomplete_fastq_record"
    except (InspectionReadError, OSError, EOFError, zlib.error) as exc:
        technical_error = {
            "code": getattr(exc, "code", "READ_DECODE_ERROR" if isinstance(exc, (gzip.BadGzipFile, EOFError, zlib.error)) else "READ_IO_ERROR"),
            "type": type(exc).__name__,
            "detail": getattr(exc, "detail", "compressed stream could not be decoded"),
        }
        stop = technical_error["code"]
        content_valid = None
    finally:
        if compressed:
            source.close()
    text = "\n".join(lines)
    return {
        "schema_version": 1, "operation": representation,
        "status": "error" if technical_error else "observed", "error": technical_error,
        "representation": "gzip" if compressed else "plain", "content": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "checked_records": records, "returned_lines": len(lines),
        "scanned_input_bytes": bounded.consumed, "decoded_bytes": decoded,
        "stop_reason": stop, "eof": eof,
        "full_compression_verified": bool(compressed and eof and not technical_error),
        "full_scientific_validation": False, "prefix_valid": content_valid,
        "limits": dict(vars(limits)),
    }


def inspect_fasta_header(stream: BinaryIO, *, limits: ReadLimits | None = None,
                         clock: Any = time.monotonic) -> dict[str, Any]:
    """Plain FASTA header and one sequence byte, never a whole sequence line.

    Limits bound the header itself. A long sequence line is not scanned to find
    its end. The executor owns cancellation of a blocked OS read, as for FASTQ.
    """
    limits = limits or ReadLimits(input_bytes=8192, line_bytes=4096)
    bounded = _InputBudget(stream, limits, clock)
    header = bytearray()
    valid = None
    error = None
    sequence_bytes = 0
    stop = ""
    try:
        while True:
            byte = bounded.read(1)
            if not byte:
                valid = False
                stop = "empty" if not header else "header_without_sequence"
                break
            if byte == b"\n":
                break
            header.extend(byte)
            if header == b"\x1f\x8b":
                raise InspectionReadError("READ_REPRESENTATION_UNSUPPORTED", "FASTA header reader supports plain bytes only")
            if len(header) > limits.line_bytes:
                raise InspectionReadError("READ_HEADER_LIMIT", "FASTA header coverage incomplete")
            if len(header) > limits.decoded_bytes:
                raise InspectionReadError("READ_DECODED_LIMIT", "FASTA header coverage incomplete")
        if valid is None:
            if header[:2] == b"\x1f\x8b":
                raise InspectionReadError("READ_REPRESENTATION_UNSUPPORTED", "FASTA header reader supports plain bytes only")
            try:
                name = bytes(header).rstrip(b"\r").decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InspectionReadError("READ_DECODE_ERROR", "FASTA header is not UTF-8") from exc
            valid = name.startswith(">") and bool(name[1:].strip()) and "\x00" not in name
            if valid:
                byte = bounded.read(1)
                sequence_bytes = len(byte)
                valid = bool(byte) and byte.upper() in b"ACGTURYSWKMBDHVN.-*"
                stop = "header_and_first_sequence_byte" if valid else "sequence_prefix_invalid_or_empty"
            else:
                stop = "header_format_mismatch"
    except (InspectionReadError, OSError) as exc:
        valid = None
        error = {"code": getattr(exc, "code", "READ_IO_ERROR"),
                 "detail": getattr(exc, "detail", type(exc).__name__)}
        stop = "incomplete"
    return {"schema_version": 1, "operation": "fasta_header", "representation": "plain",
            "status": "error" if error else "observed", "error": error,
            "scope": "fasta_header_and_first_sequence_byte", "prefix_valid": valid,
            "full_file_validated": False, "checked_headers": int(valid is True),
            "header_bytes": len(header), "sequence_bytes": sequence_bytes,
            "scanned_input_bytes": bounded.consumed, "stop_reason": stop,
            "limits": {"input_bytes": limits.input_bytes, "header_bytes": limits.line_bytes,
                       "decoded_bytes": limits.decoded_bytes, "seconds": limits.seconds}}
