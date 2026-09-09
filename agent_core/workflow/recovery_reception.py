"""Bounded transport decoding, never an alternative Recovery authority."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping

from agent_core.workflow.stage_contracts import RecoveryDirective, StageContractError

PROTOCOL = "recovery-submission-v1"
SUBMIT_TOOL = "submit_recovery_directive"
MAX_CHARS = 128_000
MAX_DEPTH = 32


class ReceptionError(ValueError):
    def __init__(self, code, detail="", *, stage="recovery.response_decode", candidate=None):
        self.code, self.detail, self.stage, self.candidate = code, detail, stage, candidate
        super().__init__(code + ": " + detail)


def strict_json(text):
    if not isinstance(text, str) or len(text) > MAX_CHARS:
        raise ReceptionError("RECOVERY_RESPONSE_SIZE_LIMIT")
    # Bound nesting before the JSON decoder allocates a recursive object.
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ReceptionError("RECOVERY_RESPONSE_DEPTH_LIMIT")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReceptionError("RECOVERY_JSON_DUPLICATE_KEY", key)
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ReceptionError("RECOVERY_JSON_NONFINITE_NUMBER", value)
        return result

    def constant(value):
        raise ReceptionError("RECOVERY_JSON_NONFINITE_NUMBER", value)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ReceptionError):
            raise
        raise ReceptionError("RECOVERY_DIRECTIVE_JSON_INVALID", str(exc)) from exc


def directive_schema():
    arrays = {"requested_slots", "evidence_refs", "diagnostic_inputs"}
    properties = {name: ({"type": "array", "items": {"type": "string"}} if name in arrays else {"type": "string"})
                  for name in RecoveryDirective.REQUIRED | RecoveryDirective.OPTIONAL}
    properties["schema_version"] = {"type": "integer", "enum": [1]}
    properties["action"] = {"type": "string", "enum": sorted(RecoveryDirective.ALLOWED_ACTIONS)}
    return {"type": "object", "properties": properties, "required": sorted(RecoveryDirective.REQUIRED), "additionalProperties": False}


def validate_candidate(raw):
    if not isinstance(raw, Mapping):
        raise ReceptionError("RECOVERY_DIRECTIVE_OBJECT_REQUIRED", stage="recovery.schema")
    try:
        strict_json(json.dumps(dict(raw), ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, ReceptionError):
            raise
        raise ReceptionError("RECOVERY_DIRECTIVE_VALUE_INVALID", str(exc), stage="recovery.schema") from exc
    # Do not let tuple()/str() compatibility coercions in old callers weaken
    # the public command boundary (in particular a string of evidence refs).
    for key, value in raw.items():
        spec = directive_schema()["properties"].get(key)
        if spec is None:
            continue  # The existing contract supplies the unknown-field error.
        kind = spec["type"]
        valid = (isinstance(value, str) if kind == "string" else
                 type(value) is int if kind == "integer" else
                 isinstance(value, list) and all(isinstance(x, str) for x in value))
        if not valid:
            raise ReceptionError("RECOVERY_DIRECTIVE_FIELD_TYPE", "/" + key, stage="recovery.schema")
    try:
        return RecoveryDirective.from_mapping(raw)
    except StageContractError as exc:
        raise ReceptionError(exc.code, exc.detail, stage="recovery.schema") from exc


def decode_text(content):
    """Only advertised transports submit. Ordinary JSON fences are evidence."""
    if not isinstance(content, str) or len(content) > MAX_CHARS:
        raise ReceptionError("RECOVERY_RESPONSE_SIZE_LIMIT")
    text = content.strip()
    if text.startswith("{"):
        raw = strict_json(text)
        if isinstance(raw, dict) and raw.get("protocol") == PROTOCOL:
            if set(raw) != {"protocol", "explanation", "directive"} or not isinstance(raw["explanation"], str):
                raise ReceptionError("RECOVERY_ENVELOPE_INVALID")
            return raw["directive"], raw["explanation"], {"transport": PROTOCOL, "start": 0, "end": len(content)}
        return raw, "", {"transport": "legacy_bare_directive", "start": 0, "end": len(content)}
    # Full-line explicit protocol delimiters: no brace search, no repair, no
    # selection among competing blocks. Normal code in prose is not a command.
    blocks = list(re.finditer(r"(?m)^```([^\n`]*)\n([\s\S]*?)^```[ \t]*(?:\n|$)", content))
    declared = [b for b in blocks if b.group(1).strip() == PROTOCOL]
    if declared:
        if len(declared) != 1 or any(b.group(1).strip() in {"json", PROTOCOL} for b in blocks if b is not declared[0]):
            raise ReceptionError("RECOVERY_SUBMISSION_AMBIGUOUS")
        b = declared[0]
        raw = strict_json(b.group(2))
        return raw, content[:b.start()] + content[b.end():], {"transport": "declared_block_v1", "start": b.start(), "end": b.end()}
    ordinary = [b for b in blocks if b.group(1).strip() == "json"]
    if len(ordinary) > 1:
        raise ReceptionError("RECOVERY_SUBMISSION_AMBIGUOUS")
    if len(ordinary) == 1:
        b = ordinary[0]
        raw = strict_json(b.group(2))
        # This is a diagnostic extraction ONLY. Even a schema-valid object
        # in an old ordinary fence must be resubmitted by the same Recovery.
        validate_candidate(raw)
        raise ReceptionError("RECOVERY_CANONICAL_RESUBMISSION_REQUIRED", "Ordinary JSON fence has no submission authority", candidate={
            "start": b.start(), "end": b.end(), "json_sha256": hashlib.sha256(b.group(2).encode()).hexdigest(), "schema_status": "valid"})
    raise ReceptionError("RECOVERY_SUBMISSION_MISSING", "Explanation saved; submit a directive using the advertised protocol")


def has_explicit_text_submission(content):
    """Recognize only reserved framing, never actions in narrative examples."""
    if re.search(r"(?m)^```" + re.escape(PROTOCOL) + r"[ \t]*$", content):
        return True
    if content.lstrip().startswith("{"):
        try:
            raw = strict_json(content)
        except ReceptionError:
            return False
        return isinstance(raw, dict) and raw.get("protocol") == PROTOCOL
    return False
