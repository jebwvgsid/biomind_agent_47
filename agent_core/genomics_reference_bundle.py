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
from urllib.parse import unquote_to_bytes, urlsplit

from agent_core.approved_resource_locator import ResourceLocatorError, open_approved_resource
from agent_core.managed_artifacts import ManagedArtifactError, build_managed_artifact_bindings

REFERENCE_BUNDLE_PARSER_ID = "biomind.genomics.reference_bundle.v1"
REFERENCE_BUNDLE_FINAL_URI_PARSER_ID = "biomind.genomics.reference_bundle.v2"
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


def reference_file_uri(path: str | Path) -> str:
    """Encode an explicit absolute final path without following or opening it."""
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts, "REFERENCE_DICTIONARY_FINAL_PATH_INVALID")
    try:
        uri = path.as_uri()
        parsed = urlsplit(uri)
        decoded = unquote_to_bytes(parsed.path).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ReferenceBundleError("REFERENCE_DICTIONARY_FINAL_PATH_INVALID") from exc
    _require(parsed.scheme == "file" and not parsed.netloc and not parsed.query and not parsed.fragment
             and decoded == str(path), "REFERENCE_DICTIONARY_FINAL_PATH_INVALID")
    return uri


def _dictionary_detail(handle, line_number, tag, raw_value, **extra):
    return json.dumps({"source": str(handle if isinstance(handle, (str, Path)) else getattr(handle, "name", "<stream>")), "line": line_number,
                      "field": tag, "raw_value_hex_prefix": raw_value[:128].hex(),
                      "raw_value_bytes": len(raw_value), "raw_value_sha256": hashlib.sha256(raw_value).hexdigest(),
                      **extra}, ensure_ascii=True, sort_keys=True)


def _dictionary_uri(handle, line_number, raw_value, *, expected_uri, require_final_uri):
    detail = _dictionary_detail(handle, line_number, "UR", raw_value)
    try:
        value = raw_value.decode("ascii")
    except UnicodeError:
        try:
            raw_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReferenceBundleError("REFERENCE_DICTIONARY_URI_ENCODING_INVALID",
                _dictionary_detail(handle, line_number, "UR", raw_value, byte_offset=exc.start,
                                   policy="regenerate_dictionary; encoding not guessed")) from exc
        # SAM v1.6 permits UTF-8 in SQ:DS, not SQ:UR. Historical bare UTF-8
        # is diagnosed, never silently promoted to a conforming new bundle.
        raise ReferenceBundleError("REFERENCE_DICTIONARY_URI_REGENERATION_REQUIRED",
            _dictionary_detail(handle, line_number, "UR", raw_value,
                               policy="bare_utf8_UR_is_not_SAM_conforming; regenerate_with_URI"))
    _require(bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value),
             "REFERENCE_DICTIONARY_URI_INVALID", detail)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ReferenceBundleError("REFERENCE_DICTIONARY_URI_INVALID", detail) from exc
    if parsed.scheme:
        # Percent escapes must be well formed. Plain filesystem paths (no
        # scheme) remain permitted by the old SAM contract and are not opened.
        _require(not re.search(r"%(?![0-9A-Fa-f]{2})", value) and " " not in value,
                 "REFERENCE_DICTIONARY_URI_REGENERATION_REQUIRED", detail)
        if parsed.scheme == "file":
            _require(not parsed.query and not parsed.fragment and parsed.netloc in {"", "localhost"}
                     and parsed.path.startswith("/"), "REFERENCE_DICTIONARY_URI_INVALID", detail)
            try:
                decoded = unquote_to_bytes(parsed.path).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReferenceBundleError("REFERENCE_DICTIONARY_URI_ENCODING_INVALID", detail) from exc
            _require(not any(ord(c) < 32 or ord(c) == 127 for c in decoded),
                     "REFERENCE_DICTIONARY_URI_INVALID", detail)
    if require_final_uri:
        _require(value == expected_uri, "REFERENCE_DICTIONARY_FINAL_URI_MISMATCH",
                 _dictionary_detail(handle, line_number, "UR", raw_value, expected_uri=expected_uri))
        status = "canonical_final_file_uri"
    else:
        status = "legacy_ascii_metadata_only_target_unchecked"
    return {"line": line_number, "value": value, "status": status,
            "raw_value_sha256": hashlib.sha256(raw_value).hexdigest()}


def _validate_dict(handle, records, *, expected_final_reference=None, require_final_uri=False, source_path=None):
    """Parse record/tag bytes first, decoding only each field's allowed encoding.

    SAM v1.6 sections 1.2.1, 1.3 and 1.3.2 govern names, header syntax and
    sequence M5. No metadata URI is used as a file locator. Legacy non-ASCII
    UR values require explicit dictionary regeneration, not a lossy decode.
    """
    detail_source = source_path if source_path is not None else handle
    expected_uri = reference_file_uri(expected_final_reference) if expected_final_reference is not None else None
    _require(not require_final_uri or expected_uri is not None, "REFERENCE_DICTIONARY_FINAL_PATH_REQUIRED")
    rows, names, aliases, uris = [], set(), set(), []
    utf8_fields = {(b"@SQ", b"DS"), (b"@RG", b"DS"), (b"@PG", b"DS"), (b"@PG", b"CL")}
    name_pattern = r"[0-9A-Za-z!#$%&+./:;?@^_|~-][0-9A-Za-z!#$%&*+./:;=?@^_|~-]*"
    for number, raw in enumerate(_lines(handle), 1):
        line = raw[:-1] if raw.endswith(b"\n") else raw
        line = line[:-1] if line.endswith(b"\r") else line
        _require(not any(c < 32 and c != 9 or c == 127 for c in line),
                 "REFERENCE_DICTIONARY_RECORD_INVALID", _dictionary_detail(detail_source, number, "record", line))
        parts = line.split(b"\t")
        kind = parts[0]
        _require(kind in {b"@HD", b"@SQ", b"@RG", b"@PG", b"@CO"} and len(parts) > 1,
                 "REFERENCE_DICTIONARY_RECORD_INVALID", _dictionary_detail(detail_source, number, "record", line))
        if kind == b"@CO":
            try:
                line[4:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReferenceBundleError("REFERENCE_DICTIONARY_FIELD_ENCODING_INVALID",
                    _dictionary_detail(detail_source, number, "CO", line[4:], byte_offset=exc.start)) from exc
            continue
        row, raw_fields = {}, {}
        for field in parts[1:]:
            _require(len(field) >= 4 and field[2:3] == b":" and re.fullmatch(rb"[A-Za-z][A-Za-z0-9]", field[:2]) is not None,
                     "REFERENCE_DICTIONARY_ROW_INVALID", _dictionary_detail(detail_source, number, "field", field))
            tag, value = field[:2], field[3:]
            tag_text = tag.decode("ascii")
            _require(tag_text not in row, "REFERENCE_DICTIONARY_DUPLICATE_TAG",
                     _dictionary_detail(detail_source, number, tag_text, value))
            raw_fields[tag_text] = value
            if kind == b"@SQ" and tag == b"UR":
                row[tag_text] = value  # Decode after independently checking SN/LN/M5.
                continue
            try:
                row[tag_text] = value.decode("utf-8" if (kind, tag) in utf8_fields else "ascii")
            except UnicodeDecodeError as exc:
                raise ReferenceBundleError("REFERENCE_DICTIONARY_FIELD_ENCODING_INVALID",
                    _dictionary_detail(detail_source, number, tag_text, value, byte_offset=exc.start)) from exc
        if kind == b"@HD":
            _require(number == 1 and re.fullmatch(r"[0-9]+\.[0-9]+", row.get("VN", "")) is not None,
                     "REFERENCE_DICTIONARY_HEADER_INVALID")
        if kind in {b"@PG", b"@RG"}:
            _require(bool(row.get("ID")), "REFERENCE_DICTIONARY_HEADER_INVALID")
        if kind != b"@SQ":
            continue
        _require(re.fullmatch(name_pattern, row.get("SN", "")) is not None,
                 "REFERENCE_DICTIONARY_NAME_INVALID", _dictionary_detail(detail_source, number, "SN", raw_fields.get("SN", b"")))
        name = row["SN"]
        _require(name not in names and name not in aliases, "REFERENCE_DICTIONARY_DUPLICATE_CONTIG", name)
        names.add(name)
        for alias in row.get("AN", "").split(",") if "AN" in row else []:
            _require(re.fullmatch(name_pattern, alias) is not None and alias not in names and alias not in aliases,
                     "REFERENCE_DICTIONARY_ALIAS_INVALID", name)
            aliases.add(alias)
        _require(re.fullmatch(r"[0-9]+", row.get("LN", "")) is not None
                 and 1 <= _parse_int(row["LN"], "REFERENCE_DICTIONARY_LENGTH_INVALID") <= 2 ** 31 - 1,
                 "REFERENCE_DICTIONARY_LENGTH_INVALID", name)
        _require(re.fullmatch(r"[0-9A-Fa-f]{32}", row.get("M5", "")) is not None,
                 "REFERENCE_DICTIONARY_MD5_INVALID", name)
        rows.append((number, row))
    _require(len(rows) == len(records), "REFERENCE_DICTIONARY_CONTIG_MISMATCH")
    for (number, row), record in zip(rows, records):
        _require(row["SN"] == record["name"] and int(row["LN"]) == record["length"],
                 "REFERENCE_DICTIONARY_REFERENCE_MISMATCH", record["name"])
        _require(row["M5"].lower() == record["md5"], "REFERENCE_DICTIONARY_MD5_MISMATCH", record["name"])
    for number, row in rows:
        _require(not require_final_uri or "UR" in row, "REFERENCE_DICTIONARY_FINAL_URI_REQUIRED", row["SN"])
        if "UR" in row:
            try:
                uris.append(_dictionary_uri(detail_source, number, row["UR"], expected_uri=expected_uri,
                                            require_final_uri=require_final_uri))
            except ReferenceBundleError as exc:
                # Preserve the failure. This records only checks already
                # completed for every row before entering metadata validation.
                detail = json.loads(exc.detail)
                detail.update(sequence_content_verified=True, verified_contig_count=len(records))
                raise ReferenceBundleError(exc.code, json.dumps(detail, sort_keys=True)) from exc
    return {"policy": "canonical_final_file_uri" if require_final_uri else "legacy_ascii_metadata_only",
            "expected_final_uri": expected_uri, "sequence_content_verified": True, "uris": uris,
            "uri_target_verified": require_final_uri}


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


def _requires_final_uri(step):
    from agent_core.step_effects import canonical_step_effect
    effect = canonical_step_effect(step)
    return any((row.get("predicate") or {}).get("parser_id") == REFERENCE_BUNDLE_FINAL_URI_PARSER_ID
               for row in effect.get("produces_observations", []) if isinstance(row, Mapping))


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
    allow_evidence_output = _requires_final_uri(step)
    for binding in bindings.values():
        aliases = {binding.artifact_id, binding.semantic_handle, *binding.aliases}
        if binding.direction == "input" and binding.metadata.get("role") == "reference_fasta" and aliases & inputs:
            source[binding.artifact_id] = binding.canonical_path
        if binding.direction == "output" and aliases & outputs:
            if allow_evidence_output and binding.metadata.get("role") == "reference_preparation_evidence":
                continue
            _require(binding.artifact_kind == "directory" or binding.validation_contract_id in {"directory_exists", "nonempty_directory"},
                     "REFERENCE_BUNDLE_OUTPUT_NOT_DIRECTORY", binding.artifact_id)
            bundles[binding.artifact_id] = binding.canonical_path
    for artifact_id, row in (plan_state.get("plan_artifacts") or {}).items():
        if str(artifact_id) in outputs:
            if allow_evidence_output and row.get("role") == "reference_preparation_evidence":
                continue
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


def validate_reference_component(source_fasta: str | Path, bundle_dir: str | Path, component: str,
                                 *, expected_final_reference=None, require_final_uri=False) -> dict:
    """Read-only component check for an explicitly owned helper staging bundle.

    This establishes content identity, not task ownership, permission to reuse,
    or Commit. The caller must obtain those from the existing approved journal.
    """
    return _validate_reference_files(source_fasta, bundle_dir, component=component,
        expected_final_reference=expected_final_reference, require_final_uri=require_final_uri)


def validate_reference_bundle_files(source_fasta: str | Path, bundle_dir: str | Path,
                                    *, expected_final_reference=None, require_final_uri=False) -> dict:
    """Validate eight explicit candidate files; never resolve a path from UR."""
    return _validate_reference_files(source_fasta, bundle_dir, component="bundle",
        expected_final_reference=expected_final_reference, require_final_uri=require_final_uri)


def _validate_reference_files(source_fasta, bundle_dir, *, component,
                              expected_final_reference, require_final_uri):
    source, bundle = Path(source_fasta), Path(bundle_dir)
    _require(source.is_absolute() and bundle.is_absolute(), "REFERENCE_BUNDLE_PATH_UNVERIFIABLE")
    _require(not source.is_relative_to(bundle), "REFERENCE_BUNDLE_SOURCE_OUTPUT_OVERLAP")
    selected = {"fasta": ("reference.fa",), "fai": ("reference.fa", "reference.fa.fai"),
                "dictionary": ("reference.fa", "reference.dict"),
                "bwa": ("reference.fa", *("reference.fa." + suffix for suffix in ("amb", "ann", "bwt", "pac", "sa"))),
                "bundle": _MEMBERS}
    _require(component in selected, "REFERENCE_BUNDLE_COMPONENT_INVALID", str(component))
    dictionary_metadata = None
    try:
        with ExitStack() as stack:
            source_handle = stack.enter_context(open_approved_resource(source))
            handles = {name: stack.enter_context(open_approved_resource(bundle / name)) for name in selected[component]}
            before = {name: _identity(handle) for name, handle in {"source": source_handle, **handles}.items()}
            _require(all(identity[2] > 0 for identity in before.values()), "REFERENCE_BUNDLE_MEMBER_EMPTY")
            source_hash = _sha256(source_handle)
            files = {name: {"sha256": _sha256(handle), "size": before[name][2]} for name, handle in handles.items()}
            _require(source_hash == files["reference.fa"]["sha256"], "REFERENCE_BUNDLE_SOURCE_IDENTITY_MISMATCH")
            records = _parse_fasta(handles["reference.fa"], pac=handles.get("reference.fa.pac"))
            if component in {"fai", "bundle"}:
                _validate_fai(handles["reference.fa.fai"], records)
            if component in {"dictionary", "bundle"}:
                dictionary_metadata = _validate_dict(handles["reference.dict"], records, source_path=bundle / "reference.dict",
                    expected_final_reference=expected_final_reference, require_final_uri=require_final_uri)
            if component in {"bwa", "bundle"}:
                _validate_bwa(handles, records)
            _require(before == {name: _identity(handle) for name, handle in {"source": source_handle, **handles}.items()},
                     "REFERENCE_BUNDLE_CHANGED_DURING_VALIDATION")
    except (OSError, ResourceLocatorError) as exc:
        raise ReferenceBundleError("REFERENCE_BUNDLE_MEMBER_UNREADABLE", str(exc)) from exc
    except (UnicodeError, struct.error) as exc:
        raise ReferenceBundleError("REFERENCE_BUNDLE_FORMAT_INVALID", str(exc)) from exc
    receipt = {"schema_version": 1,
               "parser_id": REFERENCE_BUNDLE_FINAL_URI_PARSER_ID if require_final_uri else REFERENCE_BUNDLE_PARSER_ID,
               "value": True, "validation_scope": component,
               "source_path": str(source), "source_sha256": source_hash,
               "bundle_path": str(bundle), "files": files,
               "contig_count": len(records), "total_bases": sum(row["length"] for row in records),
               "contig_digest": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
               "bwa_format": "bwa-0.7-little-endian-64-bit" if component in {"bwa", "bundle"} else None,
               "dictionary_metadata": dictionary_metadata}
    receipt["receipt_id"] = "reference-bundle-" + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    return receipt


def build_reference_bundle_receipt(plan_state: Mapping[str, Any], step: Mapping[str, Any]) -> dict:
    source_id, source, bundle_id, bundle = _formal_paths(plan_state, step)
    # The frozen predicate is the policy authority. Reading a live Skill here
    # would silently change an approved historical contract.
    strict_uri = _requires_final_uri(step)
    receipt = validate_reference_bundle_files(source, bundle,
        expected_final_reference=bundle / "reference.fa" if strict_uri else None, require_final_uri=strict_uri)
    receipt.update(source_artifact_id=source_id, bundle_artifact_id=bundle_id)
    receipt.pop("receipt_id", None)
    receipt["receipt_id"] = "reference-bundle-" + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    return receipt
