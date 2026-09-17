"""Read-only BWA 0.7 reference-bundle predicate for an explicitly selected Skill.

The v1 layout is reference.fa, reference.fa.fai, reference.dict and the five
reference.fa.{amb,ann,bwt,pac,sa} files. This parser never infers an output from
shell text, a directory listing or a basename outside the approved artifact set.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import struct
from typing import Any, Mapping

from agent_core.approved_resource_locator import ResourceLocatorError, open_approved_resource
from agent_core.managed_artifacts import ManagedArtifactError, build_managed_artifact_bindings

REFERENCE_BUNDLE_PARSER_ID = "biomind.genomics.reference_bundle.v1"
_MEMBERS = ("reference.fa", "reference.fa.fai", "reference.dict", *(
    "reference.fa." + suffix for suffix in ("amb", "ann", "bwt", "pac", "sa")))
_MAX_TEXT_LINE = 16 * 1024 * 1024


class ReferenceBundleError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(code + (": " + detail if detail else ""))


def _require(value: bool, code: str, detail: str = "") -> None:
    if not value:
        raise ReferenceBundleError(code, detail)


def _identity(handle):
    stat = os.fstat(handle.fileno())
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _lines(handle):
    handle.seek(0)
    while True:
        value = handle.readline(_MAX_TEXT_LINE + 1)
        if not value:
            return
        _require(len(value) <= _MAX_TEXT_LINE, "REFERENCE_BUNDLE_TEXT_LINE_UNSUPPORTED")
        yield value


def _sha256(handle):
    handle.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _parse_fasta(handle, *, pac=None):
    records, current, names = [], None, set()
    offset = 0
    carry = b""
    digits = bytes.maketrans(b"ACGTNRYKMSWBDHV", b"0123" + b"0" * 11)
    masks = bytes.maketrans(b"ACGTNRYKMSWBDHV", b"3333" + b"0" * 11)
    if pac is not None:
        pac.seek(0)

    def compare_packed(sequence):
        nonlocal carry
        pending = carry + sequence
        length = len(pending) // 4 * 4
        packed, carry = pending[:length], pending[length:]
        if packed:
            wanted = int(packed.translate(digits), 4)
            known = int(packed.translate(masks), 4)
            actual = pac.read(length // 4)
            _require(len(actual) == length // 4 and ((int.from_bytes(actual, "big") ^ wanted) & known) == 0,
                     "REFERENCE_BWA_PAC_SEQUENCE_MISMATCH")

    def finish():
        if current is not None:
            _require(current["length"] > 0, "REFERENCE_FASTA_EMPTY_CONTIG", current["name"])
            records.append({key: value for key, value in current.items() if key not in {"md5_context", "short_line"}} | {
                "md5": current["md5_context"].hexdigest()})

    for raw in _lines(handle):
        line = raw.rstrip(b"\r\n")
        if line.startswith(b">"):
            finish()
            try:
                name = line[1:].split()[0].decode("ascii")
            except (IndexError, UnicodeError) as exc:
                raise ReferenceBundleError("REFERENCE_FASTA_HEADER_INVALID") from exc
            _require(name not in names, "REFERENCE_FASTA_DUPLICATE_CONTIG", name)
            names.add(name)
            current = {"name": name, "length": 0, "offset": offset + len(raw), "line_bases": 0,
                       "line_width": 0, "md5_context": hashlib.md5(), "short_line": False, "ambiguities": []}
        else:
            sequence = line.upper()
            _require(current is not None and bool(sequence) and not set(sequence) - set(b"ACGTNRYKMSWBDHV"),
                     "REFERENCE_FASTA_SEQUENCE_INVALID")
            _require(not current["short_line"], "REFERENCE_FASTA_IRREGULAR_WRAP", current["name"])
            if not current["line_bases"]:
                current.update(line_bases=len(sequence), line_width=len(raw))
            else:
                _require(len(sequence) <= current["line_bases"], "REFERENCE_FASTA_IRREGULAR_WRAP", current["name"])
                _require(len(raw) <= current["line_width"], "REFERENCE_FASTA_IRREGULAR_WRAP", current["name"])
                current["short_line"] = len(sequence) < current["line_bases"] or len(raw) < current["line_width"]
            for match in re.finditer(rb"([NRYKMSWBDHV])\1*", sequence):
                start = current["length"] + match.start()
                length, symbol = len(match[0]), chr(match[0][0])
                prior = current["ambiguities"][-1] if current["ambiguities"] else None
                if prior and prior[0] + prior[1] == start and prior[2] == symbol:
                    prior[1] += length
                else:
                    current["ambiguities"].append([start, length, symbol])
            current["length"] += len(sequence)
            current["md5_context"].update(sequence)
            if pac is not None:
                compare_packed(sequence)
        offset += len(raw)
    finish()
    _require(bool(records), "REFERENCE_FASTA_EMPTY")
    if pac is not None:
        remainder = len(carry)
        compare_packed(b"N" * (4 - remainder))
        _require(pac.read() == bytes([remainder]), "REFERENCE_BWA_PAC_TRAILER_INVALID")
    return records


def _parse_int(value, code):
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ReferenceBundleError(code) from exc


def _validate_fai(handle, records):
    rows = [line.decode("ascii").rstrip("\r\n").split("\t") for line in _lines(handle)]
    _require(len(rows) == len(records), "REFERENCE_FAI_CONTIG_MISMATCH")
    for row, record in zip(rows, records):
        _require(len(row) == 5, "REFERENCE_FAI_ROW_INVALID")
        _require(row[0] == record["name"] and [_parse_int(v, "REFERENCE_FAI_ROW_INVALID") for v in row[1:]] == [
            record[key] for key in ("length", "offset", "line_bases", "line_width")],
            "REFERENCE_FAI_REFERENCE_MISMATCH", record["name"])


def _validate_dict(handle, records):
    rows = []
    for raw in _lines(handle):
        line = raw.decode("ascii").rstrip("\r\n")
        if not line.startswith("@SQ\t"):
            continue
        fields = line.split("\t")[1:]
        row = dict(item.split(":", 1) for item in fields if ":" in item)
        _require(len(row) == len(fields), "REFERENCE_DICTIONARY_ROW_INVALID")
        rows.append(row)
    _require(len(rows) == len(records), "REFERENCE_DICTIONARY_CONTIG_MISMATCH")
    for row, record in zip(rows, records):
        _require(row.get("SN") == record["name"] and _parse_int(row.get("LN"), "REFERENCE_DICTIONARY_ROW_INVALID") == record["length"],
                 "REFERENCE_DICTIONARY_REFERENCE_MISMATCH", record["name"])
        _require(str(row.get("M5") or "").lower() == record["md5"], "REFERENCE_DICTIONARY_MD5_MISMATCH", record["name"])


def _validate_bwa(handles, records):
    total = sum(row["length"] for row in records)
    ann = [line.decode("ascii").split() for line in _lines(handles["reference.fa.ann"])]
    _require(bool(ann) and len(ann[0]) == 3 and len(ann) == 1 + 2 * len(records), "REFERENCE_BWA_ANN_INVALID")
    _require([_parse_int(v, "REFERENCE_BWA_ANN_INVALID") for v in ann[0][:2]] == [total, len(records)], "REFERENCE_BWA_ANN_REFERENCE_MISMATCH")
    position, ambiguities = 0, []
    for index, record in enumerate(records):
        name, location = ann[1 + index * 2:3 + index * 2]
        _require(len(name) >= 2 and len(location) == 3, "REFERENCE_BWA_ANN_INVALID")
        _require(name[1] == record["name"] and [_parse_int(v, "REFERENCE_BWA_ANN_INVALID") for v in location[:2]] == [position, record["length"]],
                 "REFERENCE_BWA_ANN_REFERENCE_MISMATCH", record["name"])
        _require(_parse_int(location[2], "REFERENCE_BWA_ANN_INVALID") == len(record["ambiguities"]),
                 "REFERENCE_BWA_ANN_AMBIGUITY_MISMATCH", record["name"])
        ambiguities.extend([position + start, length, symbol] for start, length, symbol in record["ambiguities"])
        position += record["length"]
    amb = [line.decode("ascii").split() for line in _lines(handles["reference.fa.amb"])]
    _require(bool(amb) and len(amb[0]) == 3, "REFERENCE_BWA_AMB_INVALID")
    header = [_parse_int(v, "REFERENCE_BWA_AMB_INVALID") for v in amb[0]]
    _require(header == [total, len(records), len(amb) - 1], "REFERENCE_BWA_AMB_REFERENCE_MISMATCH")
    observed_ambiguities = []
    for row in amb[1:]:
        _require(len(row) == 3, "REFERENCE_BWA_AMB_INVALID")
        start, length = [_parse_int(v, "REFERENCE_BWA_AMB_INVALID") for v in row[:2]]
        observed_ambiguities.append([start, length, row[2]])
    _require(observed_ambiguities == ambiguities, "REFERENCE_BWA_AMB_REFERENCE_MISMATCH")
    pac_size = os.fstat(handles["reference.fa.pac"].fileno()).st_size
    _require(pac_size == total // 4 + 2, "REFERENCE_BWA_PAC_SIZE_MISMATCH")
    bwt, sa = handles["reference.fa.bwt"], handles["reference.fa.sa"]
    bwt.seek(0); sa.seek(0)
    bwt_header, sa_header = bwt.read(40), sa.read(56)
    _require(len(bwt_header) == 40 and len(sa_header) == 56, "REFERENCE_BWA_BINARY_HEADER_INVALID")
    primary, *cumulative = struct.unpack("<5Q", bwt_header)
    _require(cumulative == sorted(cumulative) and cumulative[-1] == 2 * total and primary <= 2 * total,
             "REFERENCE_BWA_BWT_REFERENCE_MISMATCH")
    words = (2 * total + 15) // 16 + ((2 * total + 127) // 128 + 1) * 8
    _require(os.fstat(bwt.fileno()).st_size == 40 + words * 4, "REFERENCE_BWA_BWT_SIZE_MISMATCH")
    _require(sa_header[:40] == bwt_header, "REFERENCE_BWA_SA_BWT_MISMATCH")
    interval, seq_len = struct.unpack("<2Q", sa_header[40:])
    _require(interval > 0 and interval & (interval - 1) == 0 and seq_len == 2 * total,
             "REFERENCE_BWA_SA_HEADER_INVALID")
    _require(os.fstat(sa.fileno()).st_size == 56 + (((seq_len + interval) // interval) - 1) * 8,
             "REFERENCE_BWA_SA_SIZE_MISMATCH")


def _formal_paths(plan_state, step):
    from agent_core.plan_bindings import PlanBindingError, step_resource_inputs

    task = dict((plan_state.get("current_task") or {}).get("task_spec") or {})
    try:
        bindings = build_managed_artifact_bindings(task, plan_state=plan_state)
        inputs = {str(item) for item in step_resource_inputs(step)} | set(step.get("input_artifact_ids") or [])
    except (ManagedArtifactError, PlanBindingError) as exc:
        raise ReferenceBundleError("REFERENCE_BUNDLE_SCOPE_UNVERIFIABLE", str(exc)) from exc
    outputs = {str(item) for item in step.get("outputs", []) or []} | set(step.get("output_artifact_ids") or [])
    source, bundles = {}, {}
    for binding in bindings.values():
        aliases = {binding.artifact_id, binding.semantic_handle, *binding.aliases}
        if binding.direction == "input" and binding.metadata.get("role") == "reference_fasta" and aliases & inputs:
            source[binding.artifact_id] = binding.canonical_path
        if binding.direction == "output" and aliases & outputs:
            _require(binding.artifact_kind == "directory" or binding.validation_contract_id in {"directory_exists", "nonempty_directory"},
                     "REFERENCE_BUNDLE_OUTPUT_NOT_DIRECTORY", binding.artifact_id)
            bundles[binding.artifact_id] = binding.canonical_path
    for artifact_id, row in (plan_state.get("plan_artifacts") or {}).items():
        if str(artifact_id) in outputs:
            _require(row.get("object_kind") == "directory" and row.get("resource_kind", "immutable_artifact") == "immutable_artifact",
                     "REFERENCE_BUNDLE_OUTPUT_NOT_IMMUTABLE_DIRECTORY", str(artifact_id))
            bundles[str(artifact_id)] = str(row.get("canonical_path") or "")
    _require(len(source) == 1, "REFERENCE_BUNDLE_SOURCE_ROLE_UNVERIFIABLE", "expected exactly one formal reference_fasta step input")
    _require(len(bundles) == 1, "REFERENCE_BUNDLE_OUTPUT_SCOPE_UNVERIFIABLE", "expected exactly one formal immutable directory step output")
    source_id, source_path = next(iter(source.items()))
    bundle_id, bundle_path = next(iter(bundles.items()))
    _require(Path(source_path).is_absolute() and Path(bundle_path).is_absolute(), "REFERENCE_BUNDLE_PATH_UNVERIFIABLE")
    _require(not Path(source_path).is_relative_to(Path(bundle_path)), "REFERENCE_BUNDLE_SOURCE_OUTPUT_OVERLAP")
    return source_id, Path(source_path), bundle_id, Path(bundle_path)


def build_reference_bundle_receipt(plan_state: Mapping[str, Any], step: Mapping[str, Any]) -> dict:
    source_id, source, bundle_id, bundle = _formal_paths(plan_state, step)
    try:
        with ExitStack() as stack:
            source_handle = stack.enter_context(open_approved_resource(source))
            handles = {name: stack.enter_context(open_approved_resource(bundle / name)) for name in _MEMBERS}
            before = {name: _identity(handle) for name, handle in {"source": source_handle, **handles}.items()}
            _require(all(identity[2] > 0 for identity in before.values()), "REFERENCE_BUNDLE_MEMBER_EMPTY")
            source_hash = _sha256(source_handle)
            files = {name: {"sha256": _sha256(handle), "size": before[name][2]} for name, handle in handles.items()}
            _require(source_hash == files["reference.fa"]["sha256"], "REFERENCE_BUNDLE_SOURCE_IDENTITY_MISMATCH")
            records = _parse_fasta(handles["reference.fa"], pac=handles["reference.fa.pac"])
            _validate_fai(handles["reference.fa.fai"], records)
            _validate_dict(handles["reference.dict"], records)
            _validate_bwa(handles, records)
            _require(before == {name: _identity(handle) for name, handle in {"source": source_handle, **handles}.items()},
                     "REFERENCE_BUNDLE_CHANGED_DURING_VALIDATION")
    except (OSError, ResourceLocatorError) as exc:
        raise ReferenceBundleError("REFERENCE_BUNDLE_MEMBER_UNREADABLE", str(exc)) from exc
    except (UnicodeError, struct.error) as exc:
        raise ReferenceBundleError("REFERENCE_BUNDLE_FORMAT_INVALID", str(exc)) from exc
    receipt = {"schema_version": 1, "parser_id": REFERENCE_BUNDLE_PARSER_ID, "value": True,
               "source_artifact_id": source_id, "source_path": str(source), "source_sha256": source_hash,
               "bundle_artifact_id": bundle_id, "bundle_path": str(bundle), "files": files,
               "contig_count": len(records), "total_bases": sum(row["length"] for row in records),
               "contig_digest": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
               "bwa_format": "bwa-0.7-little-endian-64-bit"}
    receipt["receipt_id"] = "reference-bundle-" + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    return receipt
