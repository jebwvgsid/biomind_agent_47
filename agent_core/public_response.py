"""Optional display metadata at the wire boundary; no business normalization.

The original response remains owned by its existing archive/history. Only the
top-level display member is removed from the parser's copy, without serializing
or rewriting any business value.
"""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class ResponseProjection:
    payload: str
    public_message: str | None = None
    error: str = ""


def _non_json_constant(value):
    raise ValueError("Non-JSON constant")


def _object_members(raw):
    decoder = json.JSONDecoder(parse_constant=_non_json_constant)
    start = len(raw) - len(raw.lstrip(" \t\r\n"))
    value, end = decoder.raw_decode(raw, start)
    if not isinstance(value, dict) or raw[end:].strip(" \t\r\n"):
        raise ValueError("One complete JSON object required")
    position = start + 1
    members = []
    while True:
        while raw[position] in " \t\r\n":
            position += 1
        if raw[position] == "}":
            break
        key_start = position
        key, position = decoder.raw_decode(raw, position)
        while raw[position] in " \t\r\n":
            position += 1
        position += 1  # Colon already validated by the whole-object decoder.
        while raw[position] in " \t\r\n":
            position += 1
        _, value_end = decoder.raw_decode(raw, position)
        position = value_end
        while raw[position] in " \t\r\n":
            position += 1
        comma = position if raw[position] == "," else None
        members.append((key, key_start, value_end, comma))
        if comma is None:
            break
        position += 1
    return value, members


def project_public_json(raw: str) -> ResponseProjection:
    """Strict whole JSON only; invalid/legacy wires retain their old parser path."""
    try:
        value, members = _object_members(raw)
    except (ValueError, TypeError, IndexError, RecursionError):
        return ResponseProjection(raw)
    selected = [index for index, member in enumerate(members) if member[0] == "public_message"]
    if not selected:
        return ResponseProjection(raw)
    public = value.get("public_message")
    if len(selected) != 1 or not isinstance(public, str) or not public.strip():
        public = None
    # Remove entire contiguous runs of display members and one adjacent comma.
    # Business key/value spans, including nested names and string escapes, stay
    # exactly as received. JSON whitespace outside them has no new authority.
    removals = []
    for index in selected:
        _, begin, end, comma = members[index]
        if comma is not None:
            end = comma + 1
        elif index:
            begin = members[index - 1][3]
        if removals and begin <= removals[-1][1]:
            removals[-1] = (removals[-1][0], max(end, removals[-1][1]))
        else:
            removals.append((begin, end))
    payload = raw
    for begin, end in reversed(removals):
        payload = payload[:begin] + payload[end:]
    return ResponseProjection(payload, public)


def provider_response_metadata(response) -> dict:
    """Only provider/attempt completion facts, never fields from model text."""
    metadata = getattr(response, "response_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    attempt = metadata.get("biomind_provider_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    values = {
        "finish_reason": attempt.get("finish_reason") or metadata.get("finish_reason") or metadata.get("stop_reason"),
        "completion_integrity": attempt.get("content_status") or attempt.get("completion_integrity")
            or metadata.get("completion_integrity") or metadata.get("content_status"),
    }
    return {key: value for key, value in values.items() if isinstance(value, str) and value and len(value) <= 80}


def response_incomplete(metadata: dict) -> bool:
    return bool(
        str(metadata.get("finish_reason") or "").lower() in {"length", "max_tokens", "content_filter"}
        or str(metadata.get("completion_integrity") or "").lower() in
        {"truncated", "interrupted", "incomplete", "content_filtered", "content_filter"}
    )


def project_generate_response(raw: str, *, script_protocol: bool,
                              response_metadata: dict | None = None) -> ResponseProjection:
    """Unwrap one explicit display transport before the original script parser."""
    if response_incomplete(response_metadata or {}):
        return ResponseProjection(raw, error="GENERATE_RESPONSE_INCOMPLETE: provider did not finish a complete response")
    try:
        value, members = _object_members(raw)
    except (ValueError, TypeError, IndexError, RecursionError):
        # A malformed JSON response cannot use a tag embedded inside its string
        # as an alternative executable definition or a terminal answer.
        if script_protocol and isinstance(raw, str) and raw.lstrip().startswith("{"):
            return ResponseProjection(raw, error="GENERATE_DISPLAY_INVALID_JSON: one complete JSON response is required")
        return ResponseProjection(raw)
    if not script_protocol or "response_type" not in value:
        return project_public_json(raw)
    if value.get("response_type") != "biomind_generate_display_v1":
        return ResponseProjection(raw, error="GENERATE_DISPLAY_TYPE_UNRECOGNIZED: unsupported response transport")
    keys = [member[0] for member in members]
    if keys.count("response_type") != 1 or keys.count("payload") > 1 or set(keys) - {"response_type", "public_message", "payload"}:
        return ResponseProjection(raw, error="GENERATE_DISPLAY_CONFLICT: only one payload and the display fields are allowed")
    payload = value.get("payload")
    if not isinstance(payload, str) or not payload:
        return ResponseProjection(raw, error="GENERATE_DISPLAY_PAYLOAD_REQUIRED: payload must be a non-empty script-block string")
    public = value.get("public_message")
    if keys.count("public_message") != 1 or not isinstance(public, str) or not public.strip():
        public = None
    # The envelope is exclusively a script transport. Use the existing strict
    # parser before any legacy Skill/tag/final-answer branch can inspect it.
    from agent_core.controller_audit import parse_executable_script_block, ScriptBlockError
    try:
        parse_executable_script_block(payload)
    except ScriptBlockError as exc:
        # The sole business payload is known even though it is invalid. Keep
        # its malformed-candidate identity independent of optional prose.
        return ResponseProjection(payload, public, error=str(exc))
    return ResponseProjection(payload, public)
