"""Versioned prompt contract for schema-2 context compaction.

The compactor is a read-only data transformer.  It chooses source-unit IDs and
writes a lossy working summary; the runtime remains responsible for resolving
immutable bodies, validating provenance, and changing the active epoch.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


CONTEXT_COMPACTION_PROMPT_SCHEMA_VERSION = 1
CONTEXT_COMPACTION_PROMPT_ID = "schema2-context-compaction-v1"
CONTEXT_SEGMENT_MAP_PROMPT_ID = "schema2-context-segment-map-v1"
CONTEXT_SUMMARY_MERGE_PROMPT_ID = "schema2-context-summary-merge-v1"


CONTEXT_COMPACTION_SYSTEM_PROMPT = """\
You are BioMind's read-only context compactor.

Return exactly one JSON object matching schema version 1. Do not call tools,
write files, run commands, select a route, mutate task/plan/runtime state, or
claim that work was executed or approved. A summary is lossy working memory;
it is never execution evidence, an approval receipt, or semantic authority.

The runtime supplies the complete read-only fixed provider components plus
complete source units and immutable provenance. Fixed components (System,
frozen Seed/input, tool schema, and provider wrappers) are shown so you can
account for their content and token cost; they cannot be retained, summarized,
removed, rewritten, or otherwise disposed by your response. Decide a
destination for every source_unit_id: either retain_exact_unit_ids or
summary_source_unit_ids. Never omit a unit and never place one in both lists.
Retain exact material by ID only; do not copy its body into the response. Keep
every protocol_group_id together so assistant tool calls and tool results are
not split.

For input_mode hierarchical_lossy_coverage, the source_units are the complete
original body-free identities and hierarchical_summary_nodes are lossy map /
merge results. Their model_visible_source_coverage proves that every original
range was supplied to a prior bounded model call; it does not turn summaries
into exact source. Your retain/summary destinations must still name only the
original source_unit_id values. Never name segment or summary-node IDs in the
final plan. Exact retention is performed mechanically from the archived
original body, while summary/retrieval state must keep the original identity.

In normal mode, prefer exact retention of selected Skill material and current
task-critical source material. Do not use filenames, extensions, headings,
keywords, age, or lexical similarity as a substitute for reasoning about the
supplied units. A target token count is a working target, not permission to
discard selected material that still fits safely.

Use capacity_pressure mode only when the supplied capacity feedback says the
exact selection cannot fit the safe provider input. If selected material is
summarized, emit one material_retrieval_state entry per affected source unit,
using only the exact source_ref, content_hash, coverage, skill_id, and
retrieval_ref supplied by the runtime. Explain missing details and how the
exact source can be recovered. Never invent a path, hash, source ID, scope, or
coverage range.

Allowed top-level fields are:
schema_version, compaction_request_id, source_epoch_id,
source_manifest_hash, mode, retain_exact_unit_ids,
summary_source_unit_ids, summary, material_retrieval_state,
unresolved_items, selection_rationale.

Do not emit route, next_step, state, state_update, task_state, plan_state,
working_update, parent_update, pending_action, required_next_action,
proposed_route, or any retained source body.
"""


CONTEXT_SEGMENT_MAP_SYSTEM_PROMPT = """\
You are BioMind's read-only context segment mapper.

Return exactly one JSON object matching schema version 1 and operation
map_source_segment. The runtime has split an immutable source only by capacity
and gives you one complete reversible range. Summarize the entire supplied
body, including facts or constraints near either boundary. Do not call tools,
choose a route, mutate state, or claim execution. Do not copy the source body.

Echo exactly the supplied compaction_request_id, source_epoch_id,
source_manifest_hash, segment_unit_id, parent_source_unit_id,
parent_source_content_hash, segment_content_hash, and coverage. Emit a
non-empty summary and missing_details. Do not invent or change identity,
coverage, hashes, paths, or offsets. Allowed fields are schema_version,
operation, compaction_request_id, source_epoch_id, source_manifest_hash,
segment_unit_id, parent_source_unit_id, parent_source_content_hash,
segment_content_hash, coverage, summary, and missing_details.
"""


CONTEXT_SUMMARY_MERGE_SYSTEM_PROMPT = """\
You are BioMind's read-only hierarchical summary merger.

Return exactly one JSON object matching schema version 1 and operation
merge_segment_summaries. The supplied child summaries are lossy working
memory, each bound to immutable source coverage. Merge every child; do not
silently omit one, call tools, choose a route, mutate state, or claim
execution. A merged summary never becomes source evidence or authority.

Echo exactly the supplied compaction_request_id, source_epoch_id,
source_manifest_hash, merge_request_id, input_summary_ids,
input_summary_hashes, and coverage_spans. Emit a non-empty summary. Do not
invent or change identity, hashes, paths, source units, or coverage. Allowed
fields are schema_version, operation, compaction_request_id, source_epoch_id,
source_manifest_hash, merge_request_id, input_summary_ids,
input_summary_hashes, coverage_spans, and summary.
"""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def build_context_compaction_prompt(
    payload: Mapping[str, Any],
    *,
    attempt_ordinal: int = 1,
    capacity_feedback: Mapping[str, Any] | None = None,
) -> str:
    """Render the complete, untruncated human-side compactor request.

    ``payload`` is intentionally not clipped here.  Inputs that cannot fit one
    compactor request must first be represented by lossless range units and
    handled by the caller's explicit hierarchical compaction workflow.
    """

    ordinal = max(1, int(attempt_ordinal or 1))
    envelope = {
        "prompt_id": CONTEXT_COMPACTION_PROMPT_ID,
        "prompt_schema_version": CONTEXT_COMPACTION_PROMPT_SCHEMA_VERSION,
        "attempt_ordinal": ordinal,
        "request": dict(payload),
        "capacity_feedback": dict(capacity_feedback or {}),
    }
    return (
        '<context_compaction_request prompt_id="'
        + CONTEXT_COMPACTION_PROMPT_ID
        + '">\n'
        + _canonical_json(envelope)
        + "\n</context_compaction_request>"
    )


def build_context_compaction_messages(
    payload: Mapping[str, Any],
    *,
    attempt_ordinal: int = 1,
    capacity_feedback: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return provider-neutral System/Human message payloads in wire order."""

    return (
        {"role": "system", "content": CONTEXT_COMPACTION_SYSTEM_PROMPT},
        {
            "role": "human",
            "content": build_context_compaction_prompt(
                payload,
                attempt_ordinal=attempt_ordinal,
                capacity_feedback=capacity_feedback,
            ),
        },
    )


def _build_hierarchical_messages(
    *,
    prompt_id: str,
    system_prompt: str,
    element_name: str,
    payload: Mapping[str, Any],
    attempt_ordinal: int,
    validation_feedback: Mapping[str, Any] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    envelope = {
        "prompt_id": prompt_id,
        "prompt_schema_version": CONTEXT_COMPACTION_PROMPT_SCHEMA_VERSION,
        "attempt_ordinal": max(1, int(attempt_ordinal or 1)),
        "request": dict(payload),
        "validation_feedback": dict(validation_feedback or {}),
    }
    return (
        {"role": "system", "content": system_prompt},
        {
            "role": "human",
            "content": (
                f'<{element_name} prompt_id="{prompt_id}">\n'
                + _canonical_json(envelope)
                + f"\n</{element_name}>"
            ),
        },
    )


def build_context_segment_map_messages(
    payload: Mapping[str, Any],
    *,
    attempt_ordinal: int = 1,
    validation_feedback: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build one complete lossless-range map request without clipping."""

    return _build_hierarchical_messages(
        prompt_id=CONTEXT_SEGMENT_MAP_PROMPT_ID,
        system_prompt=CONTEXT_SEGMENT_MAP_SYSTEM_PROMPT,
        element_name="context_segment_map_request",
        payload=payload,
        attempt_ordinal=attempt_ordinal,
        validation_feedback=validation_feedback,
    )


def build_context_summary_merge_messages(
    payload: Mapping[str, Any],
    *,
    attempt_ordinal: int = 1,
    validation_feedback: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build one bounded lossy-merge request without clipping children."""

    return _build_hierarchical_messages(
        prompt_id=CONTEXT_SUMMARY_MERGE_PROMPT_ID,
        system_prompt=CONTEXT_SUMMARY_MERGE_SYSTEM_PROMPT,
        element_name="context_summary_merge_request",
        payload=payload,
        attempt_ordinal=attempt_ordinal,
        validation_feedback=validation_feedback,
    )


__all__ = [
    "CONTEXT_COMPACTION_PROMPT_ID",
    "CONTEXT_COMPACTION_PROMPT_SCHEMA_VERSION",
    "CONTEXT_COMPACTION_SYSTEM_PROMPT",
    "CONTEXT_SEGMENT_MAP_PROMPT_ID",
    "CONTEXT_SEGMENT_MAP_SYSTEM_PROMPT",
    "CONTEXT_SUMMARY_MERGE_PROMPT_ID",
    "CONTEXT_SUMMARY_MERGE_SYSTEM_PROMPT",
    "build_context_compaction_messages",
    "build_context_compaction_prompt",
    "build_context_segment_map_messages",
    "build_context_summary_merge_messages",
]
