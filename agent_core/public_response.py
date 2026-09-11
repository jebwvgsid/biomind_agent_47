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
