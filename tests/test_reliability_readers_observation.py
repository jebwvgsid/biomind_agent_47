"""L1 only: bounded bytes and observation contracts, without a graph or runner.

Streams are synthetic BytesIO objects. No provider, store, shell, scientific
workflow, original input, or session is accessed by these tests.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from copy import deepcopy

import pytest

from agent_core.inspection_readers import InspectionReadError, ReadLimits, inspect_stream
from agent_core.semantic_completion import evaluate_boolean_predicates
from agent_core.step_effects import (
    emit_observation,
    extract_structured_effect_evidence,
    match_step_effect_evidence,
    observation_emission_contract,
    structured_effect_parse_receipt,
)


class SizedReads(io.BytesIO):
    """Fail immediately if the reader asks for an unbounded source scan."""

    def __init__(self, body: bytes):
        super().__init__(body)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        assert 0 <= size <= 8192, "source read must be bounded before allocation"
        self.read_sizes.append(size)
        return super().read(size)


def fastq(records: int, *, width: int = 80) -> bytes:
    sequence = ("ACGT" * ((width + 3) // 4))[:width]
    return "".join(
        f"@synthetic_{i}/1\n{sequence}\n+\n{'I' * width}\n"
        for i in range(records)
    ).encode()


def check_digest_and_bounds(result, stream, limits):
    assert result["content_sha256"] == hashlib.sha256(result["content"].encode()).hexdigest()
    assert result["scanned_input_bytes"] == stream.tell() <= limits.input_bytes
    assert result["decoded_bytes"] <= limits.decoded_bytes
    assert result["full_scientific_validation"] is False
    assert not stream.closed, "the caller continues to own its authorized stream"


def test_plain_text_returns_actual_prefix_and_digest():
    stream = SizedReads("甲\nsecond\nthird\n".encode())
    limits = ReadLimits(lines=2)
    result = inspect_stream(stream, representation="text", limits=limits)
    assert result["status"] == "observed" and result["error"] is None
    assert result["content"] == "甲\nsecond"
    assert result["returned_lines"] == 2
    assert result["stop_reason"] == "line_limit"
    assert result["eof"] is False
    assert result["prefix_valid"] is None  # text is not a FASTQ predicate
    check_digest_and_bounds(result, stream, limits)


def test_gzip_text_decodes_bytes_and_reports_actual_eof():
    stream = SizedReads(gzip.compress(b"first\nsecond\n", mtime=0))
    limits = ReadLimits(lines=4)
    result = inspect_stream(stream, representation="gzip_text", limits=limits)
    assert result["content"] == "first\nsecond"
    assert result["representation"] == "gzip"
    assert result["status"] == "observed"
    assert result["eof"] is True and result["full_compression_verified"] is True
    check_digest_and_bounds(result, stream, limits)


@pytest.mark.parametrize("count", [1, 32, 10000])
def test_valid_small_and_large_gzip_fastq_stop_at_record_boundary(count):
    stream = SizedReads(gzip.compress(fastq(count), mtime=0))
    limits = ReadLimits(records=32)
    result = inspect_stream(stream, representation="fastq_prefix", limits=limits)
    assert result["status"] == "observed" and result["error"] is None
    assert result["checked_records"] == min(count, 32)
    assert result["returned_lines"] == 4 * min(count, 32)
    assert result["prefix_valid"] is True
    assert result["content"].splitlines() == fastq(min(count, 32)).decode().splitlines()
    if count >= 32:
        assert result["stop_reason"] == "record_limit"
        assert result["eof"] is False
        assert result["full_compression_verified"] is False
    else:
        assert result["stop_reason"] == "eof"
        assert result["eof"] is True
    check_digest_and_bounds(result, stream, limits)


@pytest.mark.parametrize("compressed", [False, True])
def test_empty_fastq_is_an_observation_not_a_decode_failure(compressed):
    stream = SizedReads(gzip.compress(b"", mtime=0) if compressed else b"")
    limits = ReadLimits()
    result = inspect_stream(stream, representation="fastq_prefix", limits=limits)
    assert result["status"] == "observed" and result["error"] is None
    assert result["stop_reason"] == "empty_input"
    assert result["checked_records"] == 0 and result["prefix_valid"] is False
    assert result["eof"] is True
    check_digest_and_bounds(result, stream, limits)


@pytest.mark.parametrize("kind", ["bad_method", "missing_trailer", "bad_crc"])
def test_damaged_gzip_does_not_become_zero_reads(kind):
    valid = gzip.compress(fastq(1), mtime=0)
    if kind == "bad_method":
        body = b"\x1f\x8b\x00" + valid[3:]
    elif kind == "missing_trailer":
        body = valid[:-8]
    else:
        changed = bytearray(valid)
        changed[-8] ^= 1
        body = bytes(changed)
    stream = SizedReads(body)
    limits = ReadLimits()
    result = inspect_stream(stream, representation="fastq_prefix", limits=limits)
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_DECODE_ERROR"
    assert result["stop_reason"] != "empty_input"
    assert result["prefix_valid"] is None
    assert result["full_compression_verified"] is False
    check_digest_and_bounds(result, stream, limits)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"@r\nAC\n+\n", "incomplete_fastq_record"),
        (b"r\nAC\n+\nII\n", "invalid_fastq_in_checked_range"),
        (b"@r\nAC\n-\nII\n", "invalid_fastq_in_checked_range"),
        (b"@r\nAC\n+\nI\n", "invalid_fastq_in_checked_range"),
    ],
)
def test_checked_fastq_structure_failure_is_distinct_from_io(body, reason):
    stream = SizedReads(gzip.compress(body, mtime=0))
    result = inspect_stream(stream, representation="fastq_prefix")
    assert result["status"] == "observed" and result["error"] is None
    assert result["prefix_valid"] is False and result["checked_records"] == 0
    assert result["stop_reason"] == reason
    assert result["full_scientific_validation"] is False


@pytest.mark.parametrize("prefix", [b"", b"short\n"])
def test_line_limit_applies_to_every_buffered_line(prefix):
    # A short first line must not hide a later oversized line in the same read.
    stream = SizedReads(prefix + b"A" * 33 + b"\n")
    limits = ReadLimits(line_bytes=32, decoded_bytes=256, lines=10)
    result = inspect_stream(stream, representation="text", limits=limits)
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_LINE_LIMIT"
    assert all(len(line.encode()) <= limits.line_bytes for line in result["content"].splitlines())
    assert result["prefix_valid"] is None
    check_digest_and_bounds(result, stream, limits)


@pytest.mark.parametrize("compressed", [False, True])
def test_decoded_budget_is_enforced_before_unbounded_expansion(compressed):
    body = b"A" * 100000
    stream = SizedReads(gzip.compress(body, mtime=0) if compressed else body)
    limits = ReadLimits(decoded_bytes=64, line_bytes=256)
    result = inspect_stream(
        stream, representation="gzip_text" if compressed else "text", limits=limits,
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_DECODED_LIMIT"
    assert result["prefix_valid"] is None
    check_digest_and_bounds(result, stream, limits)


def test_compressed_input_scan_budget_is_not_reported_as_empty():
    stream = SizedReads(gzip.compress(fastq(10), mtime=0))
    limits = ReadLimits(input_bytes=16)
    result = inspect_stream(stream, representation="fastq_prefix", limits=limits)
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_INPUT_LIMIT"
    assert result["prefix_valid"] is None and result["eof"] is False
    check_digest_and_bounds(result, stream, limits)


def test_deadline_preserves_timeout_instead_of_absence():
    now = [0.0]

    class SlowStream(SizedReads):
        def read(self, size=-1):
            result = super().read(size)
            now[0] += 0.4  # deterministic elapsed time; never sleep
            return result

    stream = SlowStream(fastq(4))
    limits = ReadLimits(seconds=0.6)
    result = inspect_stream(stream, representation="fastq_prefix", limits=limits, clock=lambda: now[0])
    assert len(stream.read_sizes) == 2
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_TIMEOUT"
    assert result["prefix_valid"] is None and result["eof"] is False
    assert result["full_scientific_validation"] is False


def test_unknown_representation_is_rejected_before_reading():
    stream = SizedReads(b"not opened for this representation")
    with pytest.raises(InspectionReadError) as caught:
        inspect_stream(stream, representation="unregistered_domain_binary")
    assert caught.value.code == "READ_REPRESENTATION_UNSUPPORTED"
    assert stream.read_sizes == []


def test_permission_failure_preserves_io_error_and_exception_identity():
    class DeniedAfterHeader(SizedReads):
        def read(self, size=-1):
            if self.tell():
                raise PermissionError("synthetic access revoked")
            return super().read(size)

    result = inspect_stream(DeniedAfterHeader(b"text\n"), representation="text")
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_IO_ERROR"
    assert result["error"]["type"] == "PermissionError"
    assert result["prefix_valid"] is None
    assert result["stop_reason"] != "READ_REPRESENTATION_UNSUPPORTED"


@pytest.mark.parametrize("body", [b"abc\x00def\n", b"abc\xffdef\n"])
def test_binary_text_requests_return_an_unsupported_representation(body):
    result = inspect_stream(SizedReads(body), representation="text")
    assert result["status"] == "error"
    assert result["error"]["code"] == "READ_REPRESENTATION_UNSUPPORTED"
    assert result["prefix_valid"] is None


def test_format_selection_does_not_silently_reinterpret_plain_or_gzip():
    with pytest.raises(InspectionReadError) as mismatch:
        inspect_stream(SizedReads(b"ordinary text\n"), representation="gzip_text")
    assert mismatch.value.code == "READ_FORMAT_MISMATCH"
    with pytest.raises(InspectionReadError) as compressed:
        inspect_stream(SizedReads(gzip.compress(b"text\n", mtime=0)), representation="text")
    assert compressed.value.code == "READ_REPRESENTATION_UNSUPPORTED"


def declared_observation():
    return {"produces_observations": [{
        "observation_id": "checked_condition",
        "evidence_contract": {"required_fields": ["value"]},
        "predicate": {
            "predicate_id": "test.required_condition", "operator": "boolean_equals",
            "expected": True, "failure_disposition": "correction_required",
            "missing_disposition": "inspection_required",
        },
    }]}


@pytest.mark.parametrize("actual", [False, True])
def test_emission_identity_and_negative_value_do_not_bypass_predicate(actual):
    effect = declared_observation()
    before = deepcopy(effect)
    wire = emit_observation("checked_condition", actual)
    receipt = structured_effect_parse_receipt(wire, effect)
    parsed = extract_structured_effect_evidence(wire)
    assert receipt["status"] == "parsed" and receipt["issues"] == []
    assert receipt["raw_stdout_sha256"] == hashlib.sha256(wire.encode()).hexdigest()
    assert parsed["produces_observations"][0]["value"] is actual
    assert match_step_effect_evidence(effect, parsed)["complete"] is True
    plan = {
        "task_id": "synthetic-observation", "plan_id": "synthetic-plan", "plan_version": "plan_v1",
        "current_step_id": "check", "steps": [{"step_id": "check", "step_effect": effect}],
    }
    original_plan = deepcopy(plan)
    predicates = evaluate_boolean_predicates(
        plan_state=plan, validation={"step_id": "check", "attempt_id": "synthetic-attempt"},
        execution_evidence=parsed,
    )
    assert len(predicates) == 1
    assert predicates[0]["passed"] is actual
    assert predicates[0]["disposition"] == ("completion_accepted" if actual else "correction_required")
    assert plan == original_plan and effect == before  # no store/step completion mutation
    payload = json.loads(wire.split("=", 1)[1])
    assert not {"task_id", "plan_id", "step_id", "action_id", "attempt_id"}.intersection(payload)


def test_public_contract_examples_roundtrip_through_same_parser():
    effect = declared_observation()
    contract = observation_emission_contract(effect)
    assert contract["approved_observation_ids"] == ["checked_condition"]
    assert contract["schema_version"] == 1
    assert len(contract["examples"]) == 1
    assert structured_effect_parse_receipt(contract["examples"][0], effect)["status"] == "parsed"
    assert contract["examples"][0].startswith(contract["prefix"])


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"value": {"ok": False}}, "EFFECT_EMISSION_IDENTITY_MISSING"),
        ({"schema_version": 1, "produces_observations": [{"identity": "foreign_condition", "value": False}]}, "EFFECT_EMISSION_IDENTITY_UNDECLARED"),
        ({"schema_version": 999, "produces_observations": [{"identity": "checked_condition", "value": False}]}, "EFFECT_EMISSION_VERSION_UNSUPPORTED"),
        ({"schema_version": True, "produces_observations": [{"identity": "checked_condition", "value": False}]}, "EFFECT_EMISSION_VERSION_UNSUPPORTED"),
        ({"schema_version": "1", "produces_observations": [{"identity": "checked_condition", "value": False}]}, "EFFECT_EMISSION_VERSION_UNSUPPORTED"),
    ],
)
def test_invalid_emissions_remain_rejected_with_original_stdout_digest(payload, code):
    effect = declared_observation()
    prefix = observation_emission_contract(effect)["prefix"]
    wire = "ordinary output\n" + prefix + json.dumps(payload) + "\n"
    receipt = structured_effect_parse_receipt(wire, effect)
    assert receipt["status"] == "rejected" and receipt["emission_count"] == 1
    assert receipt["issues"][0]["code"] == code
    assert receipt["issues"][0]["field_path"].startswith("stdout.lines[2]")
    assert receipt["raw_stdout_sha256"] == hashlib.sha256(wire.encode()).hexdigest()
    assert match_step_effect_evidence(effect, extract_structured_effect_evidence(wire))["complete"] is False


def test_malformed_json_and_plain_stdout_never_invent_an_observation():
    effect = declared_observation()
    prefix = observation_emission_contract(effect)["prefix"]
    invalid = prefix + '{"value":'
    receipt = structured_effect_parse_receipt(invalid, effect)
    assert receipt["status"] == "rejected"
    assert receipt["issues"][0]["code"] == "EFFECT_EMISSION_JSON_INVALID"
    for wire in (invalid, "no reads found; value false"):
        extracted = extract_structured_effect_evidence(wire)
        assert extracted["produces_observations"] == []
        assert match_step_effect_evidence(effect, extracted)["complete"] is False


@pytest.mark.parametrize("identity", [None, "", " ", 1])
def test_emitter_requires_an_explicit_identity(identity):
    with pytest.raises(ValueError, match="OBSERVATION_IDENTITY_REQUIRED"):
        emit_observation(identity, False)
