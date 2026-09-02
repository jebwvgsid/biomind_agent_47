"""Control-plane/LLM-plane boundary for BioMind node invocations.

Only :class:`LLMPayload` is serialised into an LLM message.  Controller
identity, concurrency guards, immutable refs and trace fingerprints remain in
the accompanying :class:`ControllerBinding` / :class:`ProjectionTrace`.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from agent_core.plan_tracker import plan_record_hash, semantic_plan_hash
from agent_core.task_spec import (
    semantic_constraints_from_task_spec,
    task_record_hash,
    task_spec_revision,
    task_semantic_hash,
)
from agent_core.plan_contract import new_candidate_scope_id
from execution.runtime_contract import (
    execution_runtime_contract,
    validation_scope_contract,
)

NODE_CONTEXT_SCHEMA_VERSION = 2
# Recovery's 256k policy is explicitly measured in serialised provider-payload
# characters.  Legacy DiagnosticContext limits use a different compatibility
# surface and have no provider eligibility authority.
RECOVERY_PROVIDER_MAX_PAYLOAD_CHARS = 256_000

NODE_STALENESS_DOMAINS = {
    "consultant": "consultant-semantic-mutation-v1",
    "planner": "planner-v1",
    "planner_repair": "planner-repair-v1",
    "generate": "generate-v1",
    "recovery": "recovery-v1",
    "supervisor": "supervisor-review-v1",
    "plan_review": "plan-review-v1",
}

# Keys that carry identity/integrity/concurrency metadata rather than domain
# meaning.  The boundary rejects them recursively instead of silently deleting
# them, so a newly introduced leak fails before a model call.
_CONTROL_KEY_EXACT = {
    "invocation_id",
    "state_generation",
    "generation",
    "task_revision",
    "task_record_hash",
    "task_semantic_hash",
    "plan_revision",
    "plan_record_hash",
    "plan_semantic_hash",
    "approval_id",
    "approval_ref",
    "approval_scope_hash",
    "projection_semantic_hash",
    "node_context_hash",
    "cas_token",
    "content_ref",
    "object_ref",
    "semantic_handle_map_ref",
    "failure_fingerprint",
    "violation_set_identity",
    "context_id",
    "task_id",
    "plan_id",
    "step_id",
    "action_id",
    "attempt_id",
    "execution_id",
    "manifest_id",
    "validation_id",
}
_CONTROL_KEY_SUFFIXES = (
    "_hash",
    "_generation",
    "_cas",
    "_cas_token",
    "_object_ref",
    "_content_ref",
)
# Content digests for immutable Skill source/excerpts are model-visible
# provenance, not task/plan concurrency tokens.  Keep this allowlist exact;
# all other ``*_hash`` fields remain control-plane only.
_SEMANTIC_CONTENT_HASH_KEYS = {
    "content_hash",
    "resource_content_hash",
}
_CONTROL_VALUE_PATTERNS = (
    re.compile(r"^sha256:[0-9a-f]{32,}$", re.IGNORECASE),
    re.compile(r"^(?:object|audit|evidence):sha256:[0-9a-f]{16,}$", re.IGNORECASE),
    re.compile(r"^cas:[A-Za-z0-9._:-]+$"),
)
_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_PLACEHOLDER_RE = re.compile(r"^\{(input|output):([A-Za-z][A-Za-z0-9_.-]*)\}$")


class NodeContextError(ValueError):
    """Base class for deterministic node-context protocol failures."""


class ControlMetadataLeakError(NodeContextError):
    code = "LLM_CONTROL_METADATA_LEAK"


class ContextCapacityExceeded(NodeContextError):
    code = "CONTEXT_CAPACITY_EXCEEDED"


class SemanticHandleError(NodeContextError):
    code = "SEMANTIC_HANDLE_INVALID"


class InvocationProtocolError(NodeContextError):
    code = "INVOCATION_PROTOCOL_ERROR"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _slug(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text).strip("_.-")
    return (text or fallback)[:80]


def _without_control_metadata(value: Any) -> Any:
    """Make semantic rows safe without converting them into a summary."""
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if (
                lowered in _CONTROL_KEY_EXACT
                or (
                    lowered.endswith(_CONTROL_KEY_SUFFIXES)
                    and lowered not in _SEMANTIC_CONTENT_HASH_KEYS
                )
            ):
                continue
            if lowered in {
                "source", "source_id", "source_ref", "source_refs", "source_type",
                "provenance", "audit_ref", "audit_refs", "evidence_ref", "evidence_refs",
                "created_at", "updated_at", "recorded_at", "timestamp",
            }:
                continue
            if item in (None, "", [], {}):
                continue
            result[key] = _without_control_metadata(item)
        return result
    if isinstance(value, list):
        return [_without_control_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_without_control_metadata(item) for item in value]
    return deepcopy(value)


def control_metadata_leaks(value: Any, path: str = "$LLMPayload") -> List[str]:
    """Return precise paths at which control-plane metadata entered a payload."""
    leaks: List[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            child = f"{path}.{key}"
            if (
                lowered in _CONTROL_KEY_EXACT
                or (
                    lowered.endswith(_CONTROL_KEY_SUFFIXES)
                    and lowered not in _SEMANTIC_CONTENT_HASH_KEYS
                )
            ):
                leaks.append(child)
            leaks.extend(control_metadata_leaks(item, child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            leaks.extend(control_metadata_leaks(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        stripped = value.strip()
        exact_user_turn = (
            ".semantic_history.conversation[" in path
            and path.endswith(".content")
        )
        if not exact_user_turn and any(
            pattern.match(stripped) for pattern in _CONTROL_VALUE_PATTERNS
        ):
            leaks.append(path)
    return sorted(set(leaks))


def assert_llm_payload_clean(value: Any) -> None:
    leaks = control_metadata_leaks(value)
    if leaks:
        raise ControlMetadataLeakError(
            f"LLM_CONTROL_METADATA_LEAK: prohibited control metadata at {', '.join(leaks[:20])}"
        )


@dataclass(frozen=True)
class SemanticHandleBinding:
    semantic_handle: str
    object_type: str
    stable_identity: str
    direction: str = ""
    allowed_usage: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _HANDLE_RE.match(self.semantic_handle):
            raise SemanticHandleError(f"SEMANTIC_HANDLE_INVALID: {self.semantic_handle!r}")
        if not self.stable_identity:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_IDENTITY_MISSING: {self.semantic_handle}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "semantic_handle": self.semantic_handle,
            "object_type": self.object_type,
            "stable_identity": self.stable_identity,
            "direction": self.direction,
            "allowed_usage": list(self.allowed_usage),
            "metadata": deepcopy(dict(self.metadata)),
        }


class SemanticHandleMap:
    """Invocation binding that resolves every model-visible handle uniquely."""

    def __init__(self, bindings: Iterable[SemanticHandleBinding] = ()) -> None:
        self._bindings: Dict[str, SemanticHandleBinding] = {}
        for binding in bindings:
            self.add(binding)

    def add(self, binding: SemanticHandleBinding) -> None:
        existing = self._bindings.get(binding.semantic_handle)
        if existing is not None and existing != binding:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_AMBIGUOUS: {binding.semantic_handle} maps to "
                f"{existing.stable_identity!r} and {binding.stable_identity!r}"
            )
        self._bindings[binding.semantic_handle] = binding

    def resolve(
        self,
        handle: str,
        *,
        object_type: str = "",
        direction: str = "",
        usage: str = "",
    ) -> SemanticHandleBinding:
        binding = self._bindings.get(str(handle or ""))
        if binding is None:
            raise SemanticHandleError(f"SEMANTIC_HANDLE_UNKNOWN: {handle!r}")
        if object_type and binding.object_type != object_type:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_TYPE_MISMATCH: {handle} is {binding.object_type}, expected {object_type}"
            )
        if direction and binding.direction != direction:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_DIRECTION_MISMATCH: {handle} is {binding.direction}, expected {direction}"
            )
        if usage and binding.allowed_usage and usage not in binding.allowed_usage:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_USAGE_FORBIDDEN: {handle} cannot be used for {usage}"
            )
        return binding

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return {key: value.to_dict() for key, value in sorted(self._bindings.items())}

    def __contains__(self, handle: object) -> bool:
        return str(handle) in self._bindings

    def __len__(self) -> int:
        return len(self._bindings)


@dataclass(frozen=True)
class ControllerBinding:
    invocation_id: str
    node_type: str
    staleness_domain: str
    state_generation: int
    task_semantic_hash: str
    task_record_hash: str
    plan_semantic_hash: str = ""
    plan_record_hash: str = ""
    task_revision: int = 0
    plan_revision: int = 0
    authoritative_user_turn_revision: int = 0
    continuation_id: str = ""
    approval_ref: str = ""
    approval_applicability: str = ""
    step_id: str = ""
    execution_epoch: str = ""
    attempt_id: str = ""
    failure_fingerprint: str = ""
    violation_set_identity: str = ""
    action_identity: str = ""
    evidence_identity: str = ""
    candidate_scope_id: str = ""
    touched_entity_versions: Mapping[str, str] = field(default_factory=dict)
    semantic_handle_map: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    created_at_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "node_type": self.node_type,
            "staleness_domain": self.staleness_domain,
            "state_generation": self.state_generation,
            "task_semantic_hash": self.task_semantic_hash,
            "task_record_hash": self.task_record_hash,
            "plan_semantic_hash": self.plan_semantic_hash,
            "plan_record_hash": self.plan_record_hash,
            "task_revision": int(self.task_revision),
            "plan_revision": int(self.plan_revision),
            "authoritative_user_turn_revision": int(self.authoritative_user_turn_revision),
            "continuation_id": self.continuation_id,
            "approval_ref": self.approval_ref,
            "approval_applicability": self.approval_applicability,
            "step_id": self.step_id,
            "execution_epoch": self.execution_epoch,
            "attempt_id": self.attempt_id,
            "failure_fingerprint": self.failure_fingerprint,
            "violation_set_identity": self.violation_set_identity,
            "action_identity": self.action_identity,
            "evidence_identity": self.evidence_identity,
            "candidate_scope_id": self.candidate_scope_id,
            "touched_entity_versions": dict(self.touched_entity_versions),
            "semantic_handle_map": deepcopy(dict(self.semantic_handle_map)),
        }


@dataclass(frozen=True)
class ProjectionTrace:
    projection_type: str
    projection_version: str
    selection_policy: str
    included_constraint_handles: Tuple[str, ...]
    included_artifact_handles: Tuple[str, ...]
    excluded_handles_with_reason: Mapping[str, str]
    projection_semantic_hash: str
    node_context_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "projection_type": self.projection_type,
            "projection_version": self.projection_version,
            "selection_policy": self.selection_policy,
            "included_constraint_handles": list(self.included_constraint_handles),
            "included_artifact_handles": list(self.included_artifact_handles),
            "excluded_handles_with_reason": dict(self.excluded_handles_with_reason),
            "projection_semantic_hash": self.projection_semantic_hash,
            "node_context_hash": self.node_context_hash,
        }


@dataclass(frozen=True)
class LLMPayload:
    """Non-authoritative semantic data that may be serialised for the model."""

    value: Mapping[str, Any]
    compatibility_value: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        assert_llm_payload_clean(self.value)
        if self.compatibility_value:
            assert_llm_payload_clean(self.compatibility_value)

    def to_dict(self) -> Dict[str, Any]:
        """Return the payload plus read-only V1 aliases for local callers.

        The aliases keep older diagnostics/tests source-compatible.  Provider
        prompt builders must select :meth:`to_provider_dict`, or equivalently
        the two V2 sections, so aliases are never serialised twice.
        """
        result = deepcopy(dict(self.value))
        if self.compatibility_value:
            result.update(deepcopy(dict(self.compatibility_value)))
        semantic = result.get("semantic_history")
        focus = result.get("current_focus")
        if isinstance(semantic, Mapping) and isinstance(focus, Mapping):
            task = semantic.get("task_spec")
            if isinstance(task, Mapping):
                result.setdefault("task", deepcopy(dict(task)))
            plan = semantic.get("approved_plan")
            if isinstance(plan, Mapping):
                result.setdefault("plan", deepcopy(dict(plan)))
            elif isinstance(focus.get("candidate_plan"), Mapping):
                result.setdefault(
                    "plan", deepcopy(dict(focus.get("candidate_plan") or {}))
                )
            for key in (
                "current_step", "dependency_contracts", "runtime", "feedback",
                "violations", "evidence", "allowed_operations",
            ):
                if key in focus:
                    result.setdefault(key, deepcopy(focus[key]))
        return result

    def to_provider_dict(self) -> Dict[str, Any]:
        """Return exactly the provider-facing payload, without V1 aliases."""
        return deepcopy(dict(self.value))

    def to_json(self, *, indent: Optional[int] = None) -> str:
        # Insertion order is a cache contract: stable semantics precede the
        # invocation-local focus and must not be alphabetically reshuffled.
        return json.dumps(self.value, ensure_ascii=False, sort_keys=False, indent=indent, default=str)


@dataclass(frozen=True)
class NodeContext:
    binding: ControllerBinding
    llm_payload: LLMPayload
    projection_trace: Optional[ProjectionTrace] = None


def provider_context_manifest(
    llm_payload: Mapping[str, Any] | LLMPayload,
) -> Dict[str, Any]:
    """Describe the exact provider-visible sections without duplicating them.

    The manifest is trace metadata, never a second source of semantic truth.
    It lets incident audits prove which current-session sections reached a
    provider request and whether any section was already explicitly bounded.
    """
    if isinstance(llm_payload, LLMPayload):
        payload = llm_payload.to_provider_dict()
    else:
        payload = dict(llm_payload or {})

    def explicitly_truncated(value: Any) -> bool:
        if isinstance(value, Mapping):
            if value.get("truncated") is True or value.get("content_truncated") is True:
                return True
            return any(explicitly_truncated(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(explicitly_truncated(item) for item in value)
        return isinstance(value, str) and "[truncated]" in value.lower()

    semantic = dict(payload.get("semantic_history") or {})
    focus = dict(payload.get("current_focus") or {})
    runtime = dict(focus.get("runtime") or {})
    recovery_focus = dict(runtime.get("recovery_focus") or {})
    feedback = dict(focus.get("feedback") or {})
    action_repair = dict(feedback.get("action_repair") or {})
    recovery_decision = dict(feedback.get("recovery_decision") or {})
    execution_projection = {
        key: value
        for key, value in {
            "evidence": focus.get("evidence") or [],
            "failed_command": recovery_focus.get("failed_command"),
            "exit_code": recovery_focus.get("exit_code"),
            "bounded_stdout": recovery_focus.get("bounded_stdout"),
            "bounded_stderr": recovery_focus.get("bounded_stderr"),
            "available_evidence": recovery_focus.get("available_evidence") or [],
        }.items()
        if value not in (None, "", [], {})
    }

    # These stable audit names intentionally mirror the provider-contract
    # vocabulary.  A trace reader should not need to reverse engineer the
    # internal NodeContext nesting to answer whether a required section was
    # provider-visible.  Empty sections are retained with ``present=false`` so
    # absence is itself explicit evidence.
    section_values: List[Tuple[str, str, str, Any]] = [
        (
            "session_user_context",
            "semantic_history.conversation",
            "current_session_conversation_evidence",
            semantic.get("conversation"),
        ),
        (
            "consultant_handoff",
            "semantic_history.consultant_handoff",
            "accepted_current_session_consultant_handoff",
            semantic.get("consultant_handoff"),
        ),
        (
            "task_spec",
            "semantic_history.task_spec",
            "canonical_current_task_spec",
            semantic.get("task_spec"),
        ),
        (
            "selected_skill_refs",
            "semantic_history.selected_skill_refs",
            "canonical_session_skill_snapshots",
            semantic.get("selected_skill_refs"),
        ),
        (
            "available_skill_headings",
            "semantic_history.available_skill_headings",
            "immutable_skill_heading_indexes",
            semantic.get("available_skill_headings"),
        ),
        (
            "relevant_skill_excerpts",
            "current_focus.relevant_skill_excerpts",
            "current_step_exact_skill_source",
            focus.get("relevant_skill_excerpts"),
        ),
        (
            "skill_tool_results",
            "current_focus.skill_tool_results",
            "current_skill_continuation_result",
            focus.get("skill_tool_results"),
        ),
        (
            "approved_plan",
            "semantic_history.approved_plan",
            "approved_current_plan_snapshot",
            semantic.get("approved_plan"),
        ),
        (
            "current_step",
            "current_focus.current_step",
            "current_approved_plan_step",
            focus.get("current_step"),
        ),
        (
            "runtime_bindings",
            "current_focus.runtime.resolved_runtime",
            "controller_resolved_runtime_snapshot",
            runtime.get("resolved_runtime") or runtime.get("runtime_bindings"),
        ),
        (
            "execution_runtime_contract",
            "current_focus.runtime.execution_runtime_contract",
            "canonical_executor_policy_projection",
            runtime.get("execution_runtime_contract"),
        ),
        (
            "validation_scope_contract",
            "current_focus.runtime.validation_scope_contract",
            "canonical_generate_validation_scope_projection",
            runtime.get("validation_scope_contract"),
        ),
        (
            "previous_script",
            "current_focus.runtime.recovery_focus.previous_script",
            "current_attempt_script_evidence",
            recovery_focus.get("previous_script")
            or action_repair.get("previous_script"),
        ),
        (
            "machine_audit",
            "current_focus.runtime.recovery_focus.controller_machine_audit_findings",
            "controller_machine_observations",
            recovery_focus.get("controller_machine_audit_findings")
            or action_repair.get("controller_machine_audit_findings")
            or focus.get("violations"),
        ),
        (
            "execution_evidence",
            "current_focus.evidence",
            "bounded_current_attempt_evidence",
            execution_projection,
        ),
        (
            "recovery_diagnosis",
            "current_focus.feedback.recovery_decision",
            "current_recovery_diagnosis",
            recovery_decision
            or recovery_focus.get("current_diagnostic")
            or {
                key: action_repair.get(key)
                for key in (
                    "recovery_root_cause", "repair_target", "repair_instruction",
                )
                if action_repair.get(key) not in (None, "", [], {})
            },
        ),
    ]
    sections: List[Dict[str, Any]] = []
    for section, payload_path, source, value in section_values:
        present = value not in (None, "", [], {})
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=False,
                separators=(",", ":"),
                default=str,
            )
            if present
            else ""
        )
        sections.append({
            "section": section,
            "payload_path": payload_path,
            "source": source,
            "chars": len(encoded),
            "characters": len(encoded),
            "estimated_tokens": (len(encoded) + 3) // 4 if encoded else 0,
            "truncated": explicitly_truncated(value),
            "present": present,
        })
    return {
        "schema_version": 1,
        "sections": sections,
        "total_characters": sum(int(item["characters"]) for item in sections),
        "estimated_tokens": sum(int(item["estimated_tokens"]) for item in sections),
    }


def _unique_handle(prefix: str, seed: Any, used: MutableMapping[str, int]) -> str:
    base = f"{prefix}.{_slug(seed, fallback='item')}"
    count = used.get(base, 0) + 1
    used[base] = count
    return base if count == 1 else f"{base}.{count}"


def _declared_or_unique_handle(
    prefix: str,
    declared: Any,
    seed: Any,
    used: MutableMapping[str, int],
) -> str:
    value = str(declared or "").strip()
    if value.startswith(prefix + ".") and _HANDLE_RE.match(value):
        count = used.get(value, 0) + 1
        used[value] = count
        return value if count == 1 else f"{value}.{count}"
    return _unique_handle(prefix, seed, used)


def _semantic_artifact_rows(
    task_spec: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    for index, raw in enumerate(task_spec.get("inputs", []) or [], start=1):
        if not isinstance(raw, Mapping):
            continue
        role = raw.get("role") or raw.get("name") or raw.get("input_id") or f"input_{index}"
        handle = _declared_or_unique_handle(
            "input", raw.get("semantic_handle"), role, used
        )
        stable_id = str(raw.get("artifact_id") or raw.get("input_id") or raw.get("fact_id") or role)
        handle_map.add(SemanticHandleBinding(
            handle,
            "artifact",
            stable_id,
            direction="input",
            allowed_usage=("read", "materialize_input"),
            metadata={
                "index": index,
                "role": str(role),
                "input_id": str(raw.get("input_id") or stable_id),
                "artifact_id": str(raw.get("artifact_id") or ""),
                "artifact_kind": str(raw.get("artifact_kind") or raw.get("object_kind") or ""),
                "producer_owner": str(raw.get("producer_owner") or "external_input"),
                "canonical_path": str(
                    raw.get("canonical_path")
                    or raw.get("resolved_path")
                    or raw.get("path")
                    or ""
                ),
            },
        ))
        row = {
            "handle": handle,
            "role": str(role),
            "format": raw.get("format") or raw.get("type") or "",
            "required": bool(raw.get("required", True)),
            "verification_status": (
                "verified"
                if bool(raw.get("verified"))
                else str(raw.get("status") or "declared")
            ),
        }
        if raw.get("description"):
            row["description"] = str(raw.get("description"))
        # A basename is useful semantic context; the verified absolute path is
        # materialised by the Controller and stays out of the model context.
        path = str(raw.get("canonical_path") or raw.get("resolved_path") or raw.get("path") or "")
        if path:
            row["basename"] = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        inputs.append(row)

    raw_outputs = task_spec.get("output_contracts", []) or []
    for index, raw in enumerate(raw_outputs, start=1):
        if not isinstance(raw, Mapping):
            continue
        role = raw.get("role") or raw.get("name") or raw.get("output_id") or f"output_{index}"
        handle = _declared_or_unique_handle(
            "output", raw.get("semantic_handle"), role, used
        )
        stable_id = str(raw.get("artifact_id") or raw.get("output_id") or raw.get("fact_id") or role)
        handle_map.add(SemanticHandleBinding(
            handle,
            "artifact",
            stable_id,
            direction="output",
            allowed_usage=("write", "declare_output", "materialize_output"),
            metadata={
                "index": index,
                "role": str(role),
                "output_id": str(raw.get("output_id") or stable_id),
                "artifact_id": str(raw.get("artifact_id") or ""),
                "artifact_kind": str(raw.get("artifact_kind") or raw.get("object_kind") or "file"),
                "producer_owner": str(raw.get("producer_owner") or "planner"),
                "canonical_path": str(
                    raw.get("canonical_path")
                    or raw.get("path")
                    or raw.get("filename")
                    or ""
                ),
            },
        ))
        row = {
            "handle": handle,
            "output_id": str(raw.get("output_id") or stable_id),
            "role": str(role),
            "artifact_kind": str(raw.get("artifact_kind") or raw.get("object_kind") or "file"),
            "format": raw.get("format") or raw.get("type") or "",
            "required": bool(raw.get("required", True)),
            "path_policy": str(raw.get("path_policy") or ""),
            "root_ref": str(raw.get("root_ref") or ""),
            "producer_owner": str(raw.get("producer_owner") or "planner"),
            "required_basename": str(raw.get("required_basename") or ""),
            "alternative_group": str(raw.get("alternative_group") or ""),
            "description": str(raw.get("description") or ""),
            "placeholder": "{output:" + handle.removeprefix("output.") + "}",
        }
        path = str(raw.get("canonical_path") or raw.get("path") or raw.get("filename") or "")
        if path and not row.get("required_basename"):
            row["required_basename"] = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        outputs.append(row)
    output_handle_aliases: Dict[str, str] = {}
    for output_handle, binding in handle_map.to_dict().items():
        if (
            binding.get("object_type") != "artifact"
            or binding.get("direction") != "output"
        ):
            continue
        metadata = dict(binding.get("metadata") or {})
        for alias in (
            binding.get("stable_identity"), metadata.get("output_id"),
            metadata.get("artifact_id"), metadata.get("role"),
        ):
            if str(alias or ""):
                output_handle_aliases[str(alias)] = str(output_handle)
    for row in outputs:
        root_ref = str(row.get("root_ref") or "")
        if root_ref in output_handle_aliases:
            row["root_ref"] = output_handle_aliases[root_ref]
    return inputs, outputs


def _register_planner_plan_local_artifacts(
    snapshot: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> None:
    """Expose approved plan-local identities through the ``plan.*`` namespace."""
    ledger = dict(
        snapshot.get("plan_local_artifacts")
        or snapshot.get("plan_artifacts")
        or {}
    )
    for artifact_id, raw in sorted(ledger.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, Mapping) or not str(artifact_id or ""):
            continue
        entry = dict(raw)
        semantic_view = (
            dict(entry.get("semantic_view") or {})
            if isinstance(entry.get("semantic_view"), Mapping)
            else {}
        )
        role = str(
            semantic_view.get("role")
            or entry.get("local_ref")
            or artifact_id
        )
        handle = _declared_or_unique_handle(
            "plan",
            entry.get("local_ref"),
            role,
            used,
        )
        handle_map.add(SemanticHandleBinding(
            handle,
            "artifact",
            str(artifact_id),
            allowed_usage=("read", "write", "reference"),
            metadata={
                "role": role,
                "local_ref": str(entry.get("local_ref") or ""),
                "artifact_id": str(artifact_id),
                "artifact_kind": str(entry.get("object_kind") or ""),
                "producer_owner": "planner",
                "canonical_path": str(entry.get("canonical_path") or ""),
                "source_authority": "approved_plan_snapshot",
            },
        ))


def _constraint_rows(
    task_spec: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    artifact_handles: Dict[Tuple[str, str], str] = {}
    artifact_basenames: Dict[str, str] = {}
    for artifact_handle, binding in handle_map.to_dict().items():
        if binding.get("object_type") != "artifact":
            continue
        metadata = binding.get("metadata") or {}
        role = str(metadata.get("role") or "")
        direction = str(binding.get("direction") or "")
        if role and direction:
            artifact_handles[(direction, role)] = artifact_handle
        path = str(metadata.get("canonical_path") or "")
        if path:
            artifact_basenames[artifact_handle] = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    constraints = semantic_constraints_from_task_spec(task_spec)
    ordered = sorted(
        (dict(item) for item in constraints if isinstance(item, Mapping)),
        key=lambda item: (
            str(item.get("semantic_key") or ""),
            str(item.get("constraint_id") or item.get("requirement_id") or item.get("id") or ""),
            json.dumps(item.get("value"), ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    for index, raw in enumerate(ordered, start=1):
        semantic_key = raw.get("semantic_key") or raw.get("key") or f"constraint_{index}"
        handle = _unique_handle("constraint", semantic_key, used)
        stable_id = str(
            raw.get("constraint_id")
            or raw.get("requirement_id")
            or raw.get("id")
            or f"{semantic_key}:{index}"
        )
        handle_map.add(SemanticHandleBinding(
            handle,
            "constraint",
            stable_id,
            allowed_usage=("reference", "satisfy", "report_violation"),
            metadata={"semantic_key": str(semantic_key)},
        ))
        row = _without_control_metadata(raw)
        for key in ("constraint_id", "requirement_id", "id"):
            row.pop(key, None)
        category, _, role = str(semantic_key).partition(".")
        direction = (
            "input" if category == "required_input"
            else "output" if category == "required_output"
            else ""
        )
        artifact_handle = artifact_handles.get((direction, role)) if direction else ""
        if artifact_handle:
            # Exact artifact locations belong to the ControllerBinding.  The
            # model sees the semantic edge and a basename only.
            for key in (
                "value", "path", "canonical_path", "resolved_path", "filename",
                "source_path", "target_path",
            ):
                row.pop(key, None)
            row["artifact_handle"] = artifact_handle
            basename = artifact_basenames.get(artifact_handle, "")
            if basename:
                row["required_basename" if direction == "output" else "basename"] = basename
        row["handle"] = handle
        rows.append(row)
    return rows


def _step_rows(
    plan_state: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    rows: List[Dict[str, Any]] = []
    by_id: Dict[str, str] = {}
    for index, raw in enumerate(plan_state.get("steps", []) or [], start=1):
        if not isinstance(raw, Mapping):
            continue
        step_id = str(raw.get("step_id") or f"step-{index}")
        handle = _declared_or_unique_handle(
            "step",
            raw.get("semantic_handle"),
            step_id,
            used,
        )
        by_id[step_id] = handle
        handle_map.add(SemanticHandleBinding(
            handle,
            "plan_step",
            step_id,
            allowed_usage=("reference", "replace", "execute", "review"),
            metadata={"index": index},
        ))
        semantic_fields = {
            "title", "action", "purpose", "tool", "inputs", "input_ids",
            "input_artifacts", "input_refs", "input_contracts", "outputs",
            "output_ids", "output_artifacts", "output_refs", "output_contracts",
            "constraints", "constraint_ids", "semantic_constraint_ids",
            "requirement_ids", "dependencies", "depends_on", "success_criteria",
            "risk_level", "step_type", "skill_ids", "skills_declared", "command_intent",
            "execution_capability_id",
            "capability_contract_hash",
            "artifacts",
        }
        row = {
            str(key): _without_control_metadata(value)
            for key, value in raw.items()
            if str(key) in semantic_fields and value not in (None, "", [], {})
        }
        if isinstance(row.get("artifacts"), list):
            row["artifacts"] = [
                {
                    str(key): deepcopy(value)
                    for key, value in artifact.items()
                    if str(key) in {
                        "role", "artifact_type", "required", "validator",
                        "path", "format", "description", "producer_step_id",
                    }
                    and value not in (None, "", [], {})
                }
                for artifact in row["artifacts"]
                if isinstance(artifact, Mapping)
            ]
        row["handle"] = handle
        rows.append(row)
    for row, raw in zip(rows, [x for x in plan_state.get("steps", []) or [] if isinstance(x, Mapping)]):
        deps = raw.get("dependencies") or raw.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        row["dependencies"] = [by_id.get(str(dep), str(dep)) for dep in deps]
    return rows, by_id


def _project_step_semantic_references(
    rows: Sequence[MutableMapping[str, Any]],
    handle_map: SemanticHandleMap,
) -> None:
    """Project only typed identity fields to provider-visible handles.

    PlanSpec pins stable Task/Plan identities so that deterministic code can
    validate them.  Those identities are not model vocabulary.  The model sees
    invocation-local semantic handles and the Controller resolves them on
    return.  Natural-language fields are intentionally opaque: an artifact
    basename in a title or success criterion is not an identity reference.
    """
    stable_to_handle: Dict[str, Dict[str, str]] = {}
    for handle, binding in handle_map.to_dict().items():
        object_type = str(binding.get("object_type") or "")
        by_type = stable_to_handle.setdefault(object_type, {})
        stable = str(binding.get("stable_identity") or "")
        if stable:
            by_type[stable] = handle
        metadata = binding.get("metadata") or {}
        if isinstance(metadata, Mapping):
            semantic_key = str(metadata.get("semantic_key") or "")
            if semantic_key:
                by_type.setdefault(semantic_key, handle)
            canonical_path = str(metadata.get("canonical_path") or "")
            if canonical_path:
                by_type.setdefault(canonical_path, handle)

    list_identity_fields = {
        "dependencies": "plan_step",
        "depends_on": "plan_step",
        "producer_step_ids": "plan_step",
        "affected_step_ids": "plan_step",
        "cycle": "plan_step",
        "inputs": "artifact",
        "input_ids": "artifact",
        "input_artifacts": "artifact",
        "input_refs": "artifact",
        "outputs": "artifact",
        "output_ids": "artifact",
        "output_artifacts": "artifact",
        "output_refs": "artifact",
        "constraint_ids": "constraint",
        "semantic_constraint_ids": "constraint",
        "requirement_ids": "constraint",
        "skill_ids": "skill",
        "workflow_stage_ids": "workflow_stage",
    }
    scalar_identity_fields = {
        "step_id": "plan_step",
        "dependency_step_id": "plan_step",
        "producer_step_id": "plan_step",
        "consumer_step_id": "plan_step",
        "input_ref": "artifact",
        "output_ref": "artifact",
        "artifact_id": "artifact",
        "constraint_id": "constraint",
        "requirement_id": "constraint",
        "skill_id": "skill",
        "workflow_stage_id": "workflow_stage",
    }

    def project_identity(value: Any, object_type: str) -> Any:
        if isinstance(value, str):
            # Exact lookup makes projection idempotent.  In particular,
            # ``step.step-1`` is not rewritten because it merely contains the
            # stable ID ``step-1`` as prose-like text.
            return stable_to_handle.get(object_type, {}).get(value, value)
        if isinstance(value, (list, tuple)):
            return [project_identity(item, object_type) for item in value]
        if isinstance(value, Mapping):
            return project_record(value)
        return deepcopy(value)

    def project_record(value: Mapping[str, Any]) -> Dict[str, Any]:
        projected: Dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in list_identity_fields:
                projected[key] = project_identity(item, list_identity_fields[key])
            elif key in scalar_identity_fields:
                projected[key] = project_identity(item, scalar_identity_fields[key])
            elif isinstance(item, Mapping):
                projected[key] = project_record(item)
            elif isinstance(item, list):
                projected[key] = [
                    project_record(child) if isinstance(child, Mapping) else deepcopy(child)
                    for child in item
                ]
            else:
                projected[key] = deepcopy(item)
        return projected

    for row in rows:
        projected = project_record(row)
        row.clear()
        row.update(projected)


def _visible_handles(value: Any, known: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if value in known:
            found.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            found.update(_visible_handles(item, known))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_visible_handles(item, known))
    return found


def approved_current_step_artifact_usage(
    approved_plan_snapshot: Mapping[str, Any],
    current_step_id: str,
) -> Dict[str, Dict[str, Any]]:
    """Return invocation-local artifact roles from one approved snapshot.

    The result is identity/ref driven.  It neither reads mutable step prose nor
    exposes unrelated ledger entries.  A stable plan-local identity may be an
    output for its producer invocation and an input for a later consumer.
    """
    snapshot = dict(approved_plan_snapshot or {})
    steps = [
        dict(item)
        for item in snapshot.get("steps", []) or []
        if isinstance(item, Mapping)
    ]
    step_id = str(current_step_id or "")
    current = next(
        (item for item in steps if str(item.get("step_id") or "") == step_id),
        None,
    )
    if not current:
        return {}

    relations: Dict[str, Dict[str, Any]] = {}

    def add(identity: Any, relation: str, *, field: str) -> None:
        stable = str(identity or "").strip()
        if not stable:
            return
        direction = "output" if relation == "produce" else "input"
        prior = relations.get(stable)
        if prior and str(prior.get("direction") or "") != direction:
            raise NodeContextError(
                f"APPROVED_ARTIFACT_ROLE_CONFLICT: {step_id}:{stable}"
            )
        relations[stable] = {
            "artifact_id": stable,
            "step_id": step_id,
            "relation": relation,
            "direction": direction,
            "field": field,
        }

    for row in snapshot.get("resolved_artifact_refs", []) or []:
        if not isinstance(row, Mapping) or str(row.get("step_id") or "") != step_id:
            continue
        ref = dict(row.get("artifact_ref") or {})
        if str(ref.get("kind") or "") != "authoritative":
            continue
        relation = str(row.get("relation") or "")
        if relation in {"consume", "produce"}:
            add(ref.get("identity"), relation, field=str(row.get("field") or ""))

    # Snapshot steps carry the same typed refs.  This is a compatibility
    # projection for early v1.2.1 snapshots whose resolved usage list was
    # absent, not a semantic inference from prose.
    for field, relation in (("inputs", "consume"), ("outputs", "produce")):
        for value in current.get(field, []) or []:
            add(value, relation, field=field)

    plan_local = dict(
        snapshot.get("plan_local_artifacts")
        or snapshot.get("plan_artifacts")
        or {}
    )
    producer_by_artifact: Dict[str, str] = {}
    for row in snapshot.get("resolved_artifact_refs", []) or []:
        if not isinstance(row, Mapping) or str(row.get("relation") or "") != "produce":
            continue
        ref = dict(row.get("artifact_ref") or {})
        if str(ref.get("kind") or "") == "authoritative":
            producer_by_artifact[str(ref.get("identity") or "")] = str(
                row.get("step_id") or ""
            )
    for step in steps:
        producer_step_id = str(step.get("step_id") or "")
        for artifact_id in step.get("outputs", []) or []:
            stable = str(artifact_id or "")
            if stable in plan_local:
                producer_by_artifact.setdefault(stable, producer_step_id)
    for artifact_id, value in relations.items():
        if artifact_id in plan_local:
            value["plan_local"] = True
            value["producer_step_id"] = producer_by_artifact.get(artifact_id, "")
            value["ledger_entry"] = deepcopy(dict(plan_local[artifact_id] or {}))
        else:
            value["plan_local"] = False
    return relations


def _plan_local_runtime_available(
    plan_state: Mapping[str, Any],
    artifact_id: str,
    producer_step_id: str,
) -> bool:
    producer = next(
        (
            step
            for step in plan_state.get("steps", []) or []
            if isinstance(step, Mapping)
            and str(step.get("step_id") or "") == str(producer_step_id or "")
        ),
        {},
    )
    if str(producer.get("status") or "").lower() not in {
        "done", "completed", "passed", "skipped",
    }:
        return False
    provenance = dict(plan_state.get("provenance_state") or {})
    artifacts = dict(provenance.get("artifacts") or {})
    for head in dict(provenance.get("artifact_heads") or {}).values():
        record = dict(artifacts.get(str(head)) or {})
        if str(record.get("logical_artifact_id") or "") != str(artifact_id):
            continue
        if (
            str(record.get("status") or "").lower() == "valid"
            and str(record.get("validation_status") or "").lower()
            in {"valid", "passed", "pass"}
        ):
            return True
    return False


def _select_current_step_closure(
    *,
    node_type: str,
    raw_steps: Sequence[Mapping[str, Any]],
    step_rows: Sequence[Mapping[str, Any]],
    current_step_id: str,
    constraints: Sequence[Mapping[str, Any]],
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    raw_outputs: Sequence[Mapping[str, Any]],
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, Any],
    List[Dict[str, Any]],
    Dict[str, str],
    str,
]:
    """Select the deterministic semantic dependency closure for one step.

    The closure is identity/ref driven.  It never uses character budgets or
    natural-language similarity, so a large required closure fails capacity
    checks instead of silently losing the tail of the task contract.
    """
    known_constraint_handles = {
        str(row.get("handle") or "") for row in constraints if row.get("handle")
    }
    known_artifact_handles = {
        str(row.get("handle") or "") for row in [*inputs, *outputs] if row.get("handle")
    }
    known_step_handles = {
        str(row.get("handle") or "") for row in step_rows if row.get("handle")
    }
    selected_index = -1
    for index, raw in enumerate(raw_steps):
        if current_step_id and str(raw.get("step_id") or "") == str(current_step_id):
            selected_index = index
            break
    if selected_index < 0 and step_rows:
        selected_index = 0
    current = deepcopy(dict(step_rows[selected_index])) if selected_index >= 0 else {}

    by_step_handle = {
        str(row.get("handle") or ""): index
        for index, row in enumerate(step_rows)
        if row.get("handle")
    }
    dependency_indexes: List[int] = []
    pending = list(_visible_handles(current.get("dependencies", []), known_step_handles))
    seen_steps: set[str] = set()
    while pending:
        handle = pending.pop(0)
        if handle in seen_steps or handle not in by_step_handle:
            continue
        seen_steps.add(handle)
        index = by_step_handle[handle]
        dependency_indexes.append(index)
        pending.extend(
            sorted(
                _visible_handles(step_rows[index].get("dependencies", []), known_step_handles)
                - seen_steps
            )
        )

    referenced_constraints = _visible_handles(current, known_constraint_handles)
    referenced_artifacts = _visible_handles(current, known_artifact_handles)
    dependency_contracts: List[Dict[str, Any]] = []
    for index in dependency_indexes:
        dependency = deepcopy(dict(step_rows[index]))
        # Only explicitly declared dependency outputs can satisfy the current
        # step. Handle spelling is identity-only and never supplies usage.
        dependency_outputs: set[str] = set()
        for field_name in (
            "outputs", "output_refs", "output_artifacts",
            "output_artifact_handles", "bound_outputs",
        ):
            dependency_outputs.update(
                _visible_handles(dependency.get(field_name, []), known_artifact_handles)
            )
        referenced_artifacts.update(dependency_outputs)
        referenced_constraints.update(
            _visible_handles(dependency, known_constraint_handles)
        )
        dependency_contracts.append({
            key: deepcopy(value)
            for key, value in dependency.items()
            if key in {
                "handle", "title", "action", "outputs", "output_refs",
                "output_artifacts", "output_artifact_handles", "success_criteria",
            }
            and value not in (None, "", [], {})
        })

    for row in constraints:
        handle = str(row.get("handle") or "")
        semantic_key = str(row.get("semantic_key") or "")
        category = semantic_key.partition(".")[0]
        strength = str(row.get("strength") or "").lower()
        scope = str(row.get("enforcement_scope") or "").lower()
        if (
            row.get("task_global") is True
            or scope == "runtime_global"
            or (
                strength in {"hard", "required"}
                and category not in {"required_input", "required_output", "required_skill"}
            )
        ):
            referenced_constraints.add(handle)

    # Artifact requirements selected through the step's constraint refs are
    # explicit semantic edges too.  This also upgrades legacy plans whose
    # steps referenced constraint identities but stored literal paths.
    for row in constraints:
        if str(row.get("handle") or "") in referenced_constraints:
            referenced_artifacts.update(
                _visible_handles(row, known_artifact_handles)
            )

    # Producer ownership is an explicit edge even when a legacy step omitted
    # its output handle list.
    if selected_index >= 0:
        selected_stable_id = str(raw_steps[selected_index].get("step_id") or "")
        for row, raw in zip(outputs, raw_outputs):
            if str(raw.get("producer_step_id") or "") == selected_stable_id:
                referenced_artifacts.add(str(row.get("handle") or ""))

    has_explicit_edges = bool(referenced_constraints or referenced_artifacts)
    policy = "current-step-closure-v2"
    if current and not has_explicit_edges:
        # Read-only compatibility for plans created before semantic references
        # were mandatory.  New PlanSpec validation rejects this shape.
        referenced_constraints = set(known_constraint_handles)
        referenced_artifacts = set(known_artifact_handles)
        policy = "legacy-unbound-step-full-closure-v1"

    selected_constraints = [
        deepcopy(dict(row)) for row in constraints
        if str(row.get("handle") or "") in referenced_constraints
    ]
    selected_inputs = [
        deepcopy(dict(row)) for row in inputs
        if str(row.get("handle") or "") in referenced_artifacts
    ]
    selected_outputs = [
        deepcopy(dict(row)) for row in outputs
        if str(row.get("handle") or "") in referenced_artifacts
    ]
    excluded: Dict[str, str] = {}
    included = referenced_constraints | referenced_artifacts
    for handle in sorted(known_constraint_handles | known_artifact_handles):
        if handle not in included:
            excluded[handle] = f"outside_{node_type}_current_step_dependency_closure"
    return (
        selected_constraints,
        selected_inputs,
        selected_outputs,
        current,
        dependency_contracts,
        excluded,
        policy,
    )


def _evidence_rows(
    evidence: Sequence[Mapping[str, Any]],
    handle_map: SemanticHandleMap,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    user_index = 0
    generic_index = 0
    for index, raw in enumerate(evidence, start=1):
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or raw.get("type") or "evidence")
        if kind == "user_message":
            user_index += 1
            handle = f"U{user_index}"
        else:
            generic_index += 1
            handle = f"E{generic_index}"
        stable_id = str(raw.get("evidence_id") or raw.get("id") or raw.get("ref") or f"evidence:{index}")
        handle_map.add(SemanticHandleBinding(
            handle,
            "evidence",
            stable_id,
            allowed_usage=("cite", "diagnose", "review"),
            metadata={
                "offset": raw.get("offset"),
                "truncated": bool(raw.get("truncated", False)),
                "kind": kind,
            },
        ))
        excerpt = raw.get("excerpt") or raw.get("text") or raw.get("summary") or ""
        rows.append({
            "handle": handle,
            "kind": kind,
            "excerpt": str(excerpt),
            "truncated": bool(raw.get("truncated", False)),
        })
    return rows


def _normalize_hash(value: str) -> str:
    text = str(value or "")
    return text if not text or text.startswith("sha256:") else f"sha256:{text}"


def _semantic_conversation_rows(
    turns: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Project complete ordered user semantics without storage/trace metadata."""
    rows: List[Dict[str, Any]] = []
    for raw in turns:
        if not isinstance(raw, Mapping):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role != "user":
            continue
        content = str(raw.get("content") or "")
        if not content:
            continue
        rows.append({
            "ordinal": len(rows) + 1,
            "role": "user",
            "content": content,
        })
    return rows


def _accepted_consultant_handoff_view(
    handoff: Optional[Mapping[str, Any]],
    *,
    include_authoritative_lists: bool = True,
) -> Dict[str, Any]:
    """Whitelist the accepted semantic handoff; never project retry chatter."""
    raw = dict(handoff or {})
    allowed = (
        "summary", "requirements_summary", "goal_interpretation", "input_files",
        "required_outputs", "selected_skills",
        "capability_decisions", "runtime_requirements", "resolved_ambiguities",
        "unresolved_questions", "database_strategy",
    )
    result = OrderedDict()
    for key in allowed:
        if (
            not include_authoritative_lists
            and key in {
                "input_files", "required_outputs", "selected_skills",
                "runtime_requirements",
            }
        ):
            continue
        value = raw.get(key)
        if value not in (None, "", [], {}):
            result[key] = _without_control_metadata(value)
    return dict(result)


def _compact_planner_runtime_projection(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    """Project only current runtime facts needed to construct a Plan."""
    raw = dict(runtime or {})
    rows = [
        dict(item) for item in raw.get("input_audit", []) or []
        if isinstance(item, Mapping)
    ]
    candidates = [raw, *reversed(rows)]

    def first(*keys: str) -> Any:
        for candidate in candidates:
            for key in keys:
                value = candidate.get(key)
                if value not in (None, "", [], {}):
                    return deepcopy(value)
        return None

    status = first("runtime_ready", "decision", "status")
    projection: Dict[str, Any] = OrderedDict()
    if isinstance(status, bool):
        projection["runtime_ready"] = status
    elif status is not None:
        projection["runtime_ready"] = str(status).lower() in {
            "runtime_ready", "ready", "resolved", "success", "passed",
        }
    selected_environment = first(
        "selected_environment", "environment", "environment_name", "conda_env"
    )
    if selected_environment is not None:
        projection["selected_environment"] = selected_environment
    runner = first("runner", "execution_runner", "shell_runner")
    if runner is not None:
        projection["runner"] = runner
    resolved = first("resolved_executable", "resolved_executables", "executable")
    if resolved is not None:
        projection["resolved_executable"] = resolved
    constraints = first("critical_constraints", "runtime_constraints")
    if constraints is not None:
        projection["critical_constraints"] = constraints
    return _without_control_metadata(projection)


def _planning_failure_focus(
    failure: Mapping[str, Any],
    handle_map: SemanticHandleMap,
) -> Dict[str, Any]:
    """Return the provider-visible correction objective without control hashes."""
    previous = deepcopy(dict(failure.get("previous_candidate") or {}))
    previous_steps = [
        step for step in previous.get("steps", []) or []
        if isinstance(step, MutableMapping)
    ]
    _project_step_semantic_references(previous_steps, handle_map)
    for index, step in enumerate(previous.get("steps", []) or [], start=1):
        if not isinstance(step, dict):
            continue
        step_key = str(step.pop("step_id", "") or f"candidate-step-{index}")
        step["step_key"] = step_key
    materializations = [
        row for row in previous.get("task_output_materializations", []) or []
        if isinstance(row, MutableMapping)
    ]
    _project_step_semantic_references(materializations, handle_map)
    stable_artifact_handles = {
        str(binding.get("stable_identity") or ""): str(handle)
        for handle, binding in handle_map.to_dict().items()
        if binding.get("object_type") == "artifact"
        and str(binding.get("stable_identity") or "")
    }

    def project_artifact_identities(value: Any) -> Any:
        if isinstance(value, str):
            return stable_artifact_handles.get(value, value)
        if isinstance(value, Mapping):
            return {
                str(key): project_artifact_identities(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [project_artifact_identities(item) for item in value]
        if isinstance(value, tuple):
            return [project_artifact_identities(item) for item in value]
        return deepcopy(value)

    violations = []
    for raw in failure.get("validation_issues", []) or []:
        if not isinstance(raw, Mapping):
            continue
        visible_fields = {
            "code", "message", "field_path", "affected_step_ids", "affected_artifact",
            "affected_output", "expected_identity", "observed_identity",
            "violated_invariant", "required_postcondition", "affected_fields",
            "machine_evidence", "expected_type", "received_type",
            "received_excerpt", "parser_message", "line", "column", "offset",
            "response_excerpt",
        }
        row = _without_control_metadata({
            key: deepcopy(value)
            for key, value in raw.items()
            if str(key) in visible_fields
        })
        violations.append(project_artifact_identities(row))
    affected_step_keys: set[str] = set()
    affected_fields: set[str] = set()
    for violation in violations:
        for value in violation.get("affected_step_ids", []) or []:
            if str(value or ""):
                affected_step_keys.add(str(value))
        for value in violation.get("affected_fields", []) or []:
            if str(value or ""):
                affected_fields.add(str(value))
        evidence = dict(violation.get("machine_evidence") or {})
        for key in ("consumer_step_id", "dependency_step_id"):
            if str(evidence.get(key) or ""):
                affected_step_keys.add(str(evidence[key]))
        for key in ("producer_step_ids", "cycle"):
            for value in evidence.get(key, []) or []:
                if str(value or ""):
                    affected_step_keys.add(str(value))
    affected_steps = [
        {
            key: deepcopy(value)
            for key, value in step.items()
            if key in {
                "step_key", "action", "purpose", "tool", "inputs", "outputs",
                "dependencies", "skill_ids", "workflow_stage_ids", "batch_id",
                "risk_level", "execution_capability_id", "capability_contract_hash",
            }
        }
        for step in previous.get("steps", []) or []
        if isinstance(step, Mapping)
        and (
            not affected_step_keys
            or str(step.get("step_key") or "") in affected_step_keys
        )
    ]
    previous_correction_raw = dict(failure.get("previous_correction") or {})
    previous_correction = {
        key: deepcopy(previous_correction_raw.get(key))
        for key in ("status", "outcome_code")
        if previous_correction_raw.get(key) not in (None, "")
    }
    repeat_count = int(failure.get("repeat_count") or 0)
    return {
        "mode": "correct_previous_plan",
        "attempt": int(failure.get("attempt") or 1) + 1,
        "previous_candidate": previous,
        "violations": violations,
        "previous_correction": previous_correction,
        "affected_subgraph": {
            "step_keys": sorted(affected_step_keys),
            "affected_fields": sorted(affected_fields),
            "steps": affected_steps,
        },
        "convergence": {
            "repeat_count": repeat_count,
            "no_structural_progress": repeat_count > 0,
        },
        "instruction": {
            "return_complete_plan": True,
            "resolve_every_blocking_violation": True,
            "self_check_required_postconditions": True,
            "change_affected_structure_if_no_progress": repeat_count > 0,
        },
    }


def _approved_plan_semantic_view(
    snapshot: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    handle_map: SemanticHandleMap,
) -> Dict[str, Any]:
    """Return one complete semantic Plan view, stripped of approval records."""
    view: Dict[str, Any] = OrderedDict()
    stable_to_handle = {
        str(row.get("stable_identity") or ""): str(handle)
        for handle, row in handle_map.to_dict().items()
        if row.get("object_type") == "plan_step"
    }
    semantic_fields = {
        "title", "action", "purpose", "tool", "inputs", "input_ids",
        "input_artifacts", "input_refs", "input_contracts", "outputs",
        "output_ids", "output_artifacts", "output_refs", "output_contracts",
        "constraints", "constraint_ids", "semantic_constraint_ids",
        "requirement_ids", "dependencies", "depends_on", "success_criteria",
        "risk_level", "step_type", "skill_ids", "skills_declared", "command_intent",
        "execution_capability_id",
        "capability_contract_hash",
        "artifacts",
    }
    approved_steps: List[Dict[str, Any]] = []
    for index, raw in enumerate(snapshot.get("steps", []) or [], start=1):
        if not isinstance(raw, Mapping):
            continue
        step_id = str(raw.get("step_id") or "")
        row = {
            str(key): _without_control_metadata(value)
            for key, value in raw.items()
            if str(key) in semantic_fields and value not in (None, "", [], {})
        }
        if isinstance(row.get("artifacts"), list):
            row["artifacts"] = [
                {
                    str(key): deepcopy(value)
                    for key, value in artifact.items()
                    if str(key) in {
                        "role", "artifact_type", "required", "validator",
                        "format", "description", "producer_step_id",
                    }
                    and value not in (None, "", [], {})
                }
                for artifact in row["artifacts"]
                if isinstance(artifact, Mapping)
            ]
        row["handle"] = stable_to_handle.get(step_id) or f"step.approved_{index}"
        deps = raw.get("dependencies") or raw.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        row["dependencies"] = [
            stable_to_handle.get(str(dep), str(dep)) for dep in deps
        ]
        approved_steps.append(row)
    if not approved_steps:
        approved_steps = [deepcopy(dict(item)) for item in steps]
    _project_step_semantic_references(approved_steps, handle_map)
    view["steps"] = approved_steps
    artifact_handles = {
        str(row.get("stable_identity") or ""): str(handle)
        for handle, row in handle_map.to_dict().items()
        if row.get("object_type") == "artifact"
        and str(handle).startswith("plan.")
    }
    approved_artifacts: List[Dict[str, Any]] = []
    plan_local_ledger = dict(
        snapshot.get("plan_local_artifacts")
        or snapshot.get("plan_artifacts")
        or {}
    )
    for artifact_id, raw in sorted(
        plan_local_ledger.items(), key=lambda item: str(item[0])
    ):
        if not isinstance(raw, Mapping):
            continue
        handle = artifact_handles.get(str(artifact_id or ""), "")
        if not handle:
            continue
        semantic_view = (
            dict(raw.get("semantic_view") or {})
            if isinstance(raw.get("semantic_view"), Mapping)
            else {}
        )
        approved_artifacts.append({
            "handle": handle,
            "semantic_view": {
                key: deepcopy(semantic_view.get(key))
                for key in ("role", "format", "description")
                if semantic_view.get(key) not in (None, "", [], {})
            },
            "artifact_contract": {
                "object_kind": str(raw.get("object_kind") or ""),
                "lifecycle": str(raw.get("lifecycle") or "plan_local"),
                "materialization_policy_id": str(
                    raw.get("materialization_policy_id") or ""
                ),
            },
        })
    if approved_artifacts:
        view["plan_artifacts"] = approved_artifacts
    proposed: List[Dict[str, Any]] = []
    for raw in snapshot.get("proposed_artifacts", []) or []:
        if not isinstance(raw, Mapping):
            continue
        row = {
            str(key): _without_control_metadata(value)
            for key, value in raw.items()
            if str(key) in {
                "local_ref", "object_kind", "lifecycle", "semantic_view",
                "artifact_contract", "materialization_policy_id",
            }
            and value not in (None, "", [], {})
        }
        if row:
            proposed.append(row)
    if proposed:
        _project_step_semantic_references(proposed, handle_map)
        view["proposed_artifacts"] = proposed
    materializations: List[Dict[str, Any]] = []
    for raw in snapshot.get("task_output_materializations", []) or []:
        if not isinstance(raw, Mapping):
            continue
        row = {
            str(key): deepcopy(value)
            for key, value in raw.items()
            if str(key) in {"output_ref", "path"}
            and value not in (None, "", [], {})
        }
        if row:
            materializations.append(row)
    if materializations:
        _project_step_semantic_references(materializations, handle_map)
        view["task_output_materializations"] = materializations
    return dict(view)


def _accepted_execution_outcomes(
    plan_state: Mapping[str, Any],
    execution_records: Sequence[Mapping[str, Any]],
    step_handles: Mapping[str, str],
    handle_map: SemanticHandleMap,
) -> List[Dict[str, Any]]:
    """Project successful durable outcomes, never historical code or logs."""
    completed_steps = {
        str(step.get("step_id") or ""): dict(step)
        for step in plan_state.get("steps", []) or []
        if isinstance(step, Mapping)
        and str(step.get("status") or "").lower()
        in {"done", "completed", "passed", "skipped"}
    }
    stable_to_handle = {
        str(row.get("stable_identity") or ""): str(handle)
        for handle, row in handle_map.to_dict().items()
        if str(row.get("stable_identity") or "")
    }
    latest_by_step: Dict[str, Mapping[str, Any]] = {}
    for raw in execution_records:
        if not isinstance(raw, Mapping):
            continue
        step_id = str(raw.get("step_id") or "")
        try:
            exit_ok = raw.get("exit_code") is None or int(raw.get("exit_code")) == 0
        except (TypeError, ValueError):
            exit_ok = False
        if (
            step_id in completed_steps
            and exit_ok
            and str(raw.get("execution_status") or "").lower()
            in {"succeeded", "success", "completed", "passed"}
        ):
            latest_by_step[step_id] = raw
    rows: List[Dict[str, Any]] = []
    for step_id, step in completed_steps.items():
        raw = latest_by_step.get(step_id)
        if raw is None:
            continue
        artifacts: List[str] = []
        candidates = list(raw.get("produced_artifact_ids") or [])
        for reconciled in raw.get("reconciled_outputs", []) or []:
            if isinstance(reconciled, Mapping):
                candidates.append(
                    reconciled.get("logical_artifact_id")
                    or reconciled.get("artifact_id")
                    or reconciled.get("output_id")
                )
        for identity in candidates:
            handle = stable_to_handle.get(str(identity or ""), "")
            if handle and handle not in artifacts:
                artifacts.append(handle)
        row: Dict[str, Any] = {
            "step": step_handles.get(step_id, "step.completed"),
            "status": "completed",
            "action_summary": str(
                step.get("title") or step.get("action") or "completed plan step"
            ),
        }
        if artifacts:
            row["produced_artifacts"] = artifacts
        reconciliation = str(raw.get("output_reconciliation_status") or "")
        if reconciliation:
            row["output_reconciliation_status"] = reconciliation
        rows.append(row)
    return rows


def _accepted_recovery_decisions(
    attempts: Sequence[Mapping[str, Any]],
    step_handles: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """Project accepted decisions only; omit attempts, IDs, evidence and traces."""
    rows: List[Dict[str, Any]] = []
    allowed = (
        "recommended_action", "failure_owner", "failure_type", "root_cause",
        "repair_strategy", "reason", "verdict", "action_type",
        "requires_user_input",
    )
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        raw = attempt.get("recovery_decision") or attempt.get("decision")
        if not isinstance(raw, Mapping):
            # Recovery decision files may themselves be supplied as rows.
            raw = attempt if any(key in attempt for key in allowed) else {}
        if not raw:
            continue
        row: Dict[str, Any] = OrderedDict()
        step_id = str(attempt.get("step_id") or raw.get("step_id") or "")
        if step_id and step_id in step_handles:
            row["step"] = step_handles[step_id]
        for key in allowed:
            value = raw.get(key)
            if value not in (None, "", [], {}):
                row[key] = _without_control_metadata(value)
        if row:
            rows.append(dict(row))
    return rows


def build_node_context(
    *,
    node_type: str,
    task_spec: Mapping[str, Any],
    plan_state: Optional[Mapping[str, Any]] = None,
    state_generation: int = 0,
    current_step_id: str = "",
    runtime: Optional[Mapping[str, Any]] = None,
    feedback: Optional[Mapping[str, Any]] = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    violations: Sequence[Mapping[str, Any]] = (),
    allowed_operations: Sequence[str] = (),
    approval_ref: str = "",
    approval_applicability: str = "",
    execution_epoch: Any = "",
    attempt_id: str = "",
    failure_fingerprint: str = "",
    violation_set_identity: str = "",
    action_identity: str = "",
    evidence_identity: str = "",
    touched_entity_versions: Optional[Mapping[str, str]] = None,
    trace_enabled: bool = False,
    max_payload_chars: Optional[int] = None,
    invocation_id: str = "",
    candidate_scope_id: str = "",
    approved_plan_snapshot: Optional[Mapping[str, Any]] = None,
    plan_snapshot: Optional[Mapping[str, Any]] = None,
    plan_snapshot_status: str = "",
    conversation_turns: Sequence[Mapping[str, Any]] = (),
    consultant_handoff: Optional[Mapping[str, Any]] = None,
    execution_records: Sequence[Mapping[str, Any]] = (),
    recovery_decisions: Sequence[Mapping[str, Any]] = (),
    skill_context: Optional[Mapping[str, Any]] = None,
    authoritative_user_turn_revision: int = 0,
    continuation_id: str = "",
) -> NodeContext:
    """Build the three-object invocation protocol from authoritative state.

    No semantic list is shortened to satisfy ``max_payload_chars``.  If the
    complete required view does not fit, the call fails explicitly.
    """
    normalized_node = str(node_type or "").strip().lower()
    if normalized_node not in NODE_STALENESS_DOMAINS:
        raise NodeContextError(f"NODE_TYPE_UNSUPPORTED: {node_type!r}")
    runtime = dict(runtime or {})
    skill_context = dict(skill_context or {})
    if normalized_node in {"generate", "recovery"}:
        # Inject canonical facts here rather than copying prose into individual
        # prompts. Initial Generate, Corrective Generate and Recovery therefore
        # consume the same policy that renders the Executor prologue.
        runtime["execution_runtime_contract"] = execution_runtime_contract()
        runtime["validation_scope_contract"] = validation_scope_contract()
    plan = dict(plan_state or {})
    semantic_plan_snapshot = dict(
        plan_snapshot or approved_plan_snapshot or {}
    )
    planning_plan = (
        semantic_plan_snapshot
        if semantic_plan_snapshot
        else plan
    )
    step_plan = (
        plan
        if normalized_node == "planner_repair" and plan.get("steps")
        else planning_plan
    )
    handles = SemanticHandleMap()
    used: Dict[str, int] = {}
    inputs, outputs = _semantic_artifact_rows(task_spec, handles, used)
    if normalized_node == "planner" and semantic_plan_snapshot:
        _register_planner_plan_local_artifacts(
            semantic_plan_snapshot,
            handles,
            used,
        )
    if approved_plan_snapshot and current_step_id:
        invocation_usage = approved_current_step_artifact_usage(
            approved_plan_snapshot,
            current_step_id,
        )
        for artifact_id, usage in sorted(invocation_usage.items()):
            if not usage.get("plan_local"):
                continue
            direction = str(usage.get("direction") or "")
            ledger_entry = dict(usage.get("ledger_entry") or {})
            semantic_view = dict(ledger_entry.get("semantic_view") or {})
            handle = _declared_or_unique_handle(
                direction,
                f"{direction}.{artifact_id}",
                artifact_id,
                used,
            )
            canonical_path = str(ledger_entry.get("canonical_path") or "")
            handles.add(SemanticHandleBinding(
                handle,
                "artifact",
                artifact_id,
                direction=direction,
                allowed_usage=(
                    ("write", "declare_output", "materialize_output")
                    if direction == "output"
                    else ("read", "materialize_input")
                ),
                metadata={
                    "role": str(semantic_view.get("role") or artifact_id),
                    "canonical_path": canonical_path,
                    "source_authority": "approved_plan_snapshot",
                },
            ))
            row = {
                "handle": handle,
                "role": str(semantic_view.get("role") or artifact_id),
                "format": str(semantic_view.get("format") or ""),
                "artifact_kind": str(ledger_entry.get("object_kind") or ""),
                "required": True,
                "placeholder": "{" + direction + ":" + artifact_id + "}",
                "availability": (
                    "planned_writable_output"
                    if direction == "output"
                    else (
                        "runtime_valid"
                        if _plan_local_runtime_available(
                            plan,
                            artifact_id,
                            str(usage.get("producer_step_id") or ""),
                        )
                        else "dependency_runtime_required"
                    )
                ),
            }
            if canonical_path:
                basename = canonical_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                row["required_basename" if direction == "output" else "basename"] = basename
            (outputs if direction == "output" else inputs).append(row)
    # Open-world requirements are projected directly, in canonical order, and
    # never enter the semantic-handle registry.  ``_constraint_rows`` remains
    # only as a read-old adapter for historical payload tooling.
    constraints: List[Dict[str, Any]] = []
    highlighted_constraints: List[Dict[str, Any]] = []
    for raw in task_spec.get("requirements", []) or []:
        if not isinstance(raw, Mapping):
            continue
        value = raw.get("value")
        if isinstance(value, str):
            text = value
        else:
            text = str(raw.get("source_text") or raw.get("description") or "").strip()
            if not text:
                key = str(raw.get("semantic_key") or raw.get("requirement_id") or "").strip()
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                text = f"{key} = {encoded}" if key else encoded
        if not text:
            continue
        importance = re.sub(
            r"[^a-z0-9]+", "_", str(raw.get("strength") or "").lower()
        ).strip("_")
        if importance not in {
            "hard", "required", "preference", "conditional", "advisory",
            "ambiguous",
        }:
            level = re.sub(
                r"[^a-z0-9]+", "_", str(raw.get("level") or "").lower()
            ).strip("_")
            importance = {
                "required": "required",
                "conditional": "conditional",
                "recommended": "preference",
                "optional_validation": "advisory",
                "known_absent": "advisory",
                "unknown": "ambiguous",
            }.get(level, "ambiguous")
        source_refs: List[str] = []
        for ref in (raw.get("source_ref"), raw.get("source_id")):
            ref_text = str(ref or "").strip()
            if ref_text:
                display_ref = (
                    "source-ref-" + hashlib.sha256(
                        ref_text.encode("utf-8")
                    ).hexdigest()[:16]
                    if re.match(
                        r"^(?:sha256:[0-9a-f]{32,}|"
                        r"(?:object|audit|evidence):sha256:[0-9a-f]{16,}|"
                        r"cas:[A-Za-z0-9._:-]+)$",
                        ref_text,
                        flags=re.IGNORECASE,
                    )
                    else ref_text
                )
                if display_ref not in source_refs:
                    source_refs.append(display_ref)
        for provenance in raw.get("provenance", []) or []:
            if not isinstance(provenance, Mapping):
                continue
            ref_text = str(
                provenance.get("source_ref") or provenance.get("source_id") or ""
            ).strip()
            if ref_text:
                display_ref = (
                    "source-ref-" + hashlib.sha256(
                        ref_text.encode("utf-8")
                    ).hexdigest()[:16]
                    if re.match(
                        r"^(?:sha256:[0-9a-f]{32,}|"
                        r"(?:object|audit|evidence):sha256:[0-9a-f]{16,}|"
                        r"cas:[A-Za-z0-9._:-]+)$",
                        ref_text,
                        flags=re.IGNORECASE,
                    )
                    else ref_text
                )
                if display_ref not in source_refs:
                    source_refs.append(display_ref)
        highlighted_constraints.append({
            "text": text,
            "importance": importance,
            "source_refs": source_refs,
        })
    steps, step_handles = _step_rows(step_plan, handles, used)
    _project_step_semantic_references(steps, handles)
    evidence_rows = _evidence_rows(evidence, handles)

    # TaskSpec remains complete for every managed semantic node.  The closure
    # below selects only invocation-local focus and never replaces task truth.
    focus_constraints = constraints
    focus_inputs = inputs
    focus_outputs = outputs
    selected_step: Dict[str, Any] = {}
    dependency_contracts: List[Dict[str, Any]] = []
    excluded_handles: Dict[str, str] = {}
    selection_policy = "required-task-semantics-v2"
    if normalized_node in {"generate", "recovery", "supervisor"}:
        raw_steps = [
            dict(item) for item in planning_plan.get("steps", []) or []
            if isinstance(item, Mapping)
        ]
        raw_outputs = [
            dict(item) for item in task_spec.get("output_contracts", []) or []
            if isinstance(item, Mapping)
        ]
        (
            focus_constraints,
            focus_inputs,
            focus_outputs,
            selected_step,
            dependency_contracts,
            excluded_handles,
            selection_policy,
        ) = _select_current_step_closure(
            node_type=normalized_node,
            raw_steps=raw_steps,
            step_rows=steps,
            current_step_id=current_step_id,
            constraints=constraints,
            inputs=inputs,
            outputs=outputs,
            raw_outputs=raw_outputs,
        )

    goal_view: Dict[str, Any] = {"goal": str(task_spec.get("goal") or "")}
    _project_step_semantic_references([goal_view], handles)
    task_view: Dict[str, Any] = {
        "goal": str(goal_view.get("goal") or ""),
        "highlighted_constraints": highlighted_constraints,
        "inputs": inputs,
        "outputs": outputs,
    }
    for key in ("confirmed_decisions", "unresolved_questions"):
        values = task_spec.get(key, []) or []
        if values:
            semantic_values = {key: _without_control_metadata(values)}
            _project_step_semantic_references([semantic_values], handles)
            task_view[key] = semantic_values[key]

    visible_violations: List[Dict[str, Any]] = []
    for index, raw in enumerate(violations, start=1):
        row = _without_control_metadata(raw)
        row.pop("violation_id", None)
        row.setdefault("violation_handle", f"V{index}")
        visible_violations.append(row)

    compatibility_payload: Optional[Dict[str, Any]] = None
    semantic_nodes = {"planner", "planner_repair", "generate", "recovery"}
    if normalized_node in semantic_nodes:
        semantic_history: Dict[str, Any] = OrderedDict()
        conversation = _semantic_conversation_rows(conversation_turns)
        if conversation:
            semantic_history["conversation"] = conversation
        accepted_handoff = _accepted_consultant_handoff_view(
            consultant_handoff,
            include_authoritative_lists=normalized_node != "planner",
        )
        if accepted_handoff:
            semantic_history["consultant_handoff"] = accepted_handoff
        semantic_history["task_spec"] = task_view
        selected_skill_refs = [
            _without_control_metadata(item)
            for item in skill_context.get("selected_skill_refs", []) or []
            if isinstance(item, Mapping)
        ]
        if selected_skill_refs:
            semantic_history["selected_skill_refs"] = selected_skill_refs
        available_skill_headings = [
            _without_control_metadata(item)
            for item in skill_context.get("available_skill_headings", []) or []
            if isinstance(item, Mapping)
        ]
        if available_skill_headings:
            semantic_history["available_skill_headings"] = available_skill_headings
        selected_capability_source = (
            (runtime or {}).get("available_capabilities")
            if normalized_node == "planner"
            else (
                plan.get("selected_skill_ids")
                or (runtime or {}).get("available_capabilities")
                or accepted_handoff.get("selected_skills")
            )
        )
        selected_capabilities = [
            str(value)
            for value in (selected_capability_source or [])
            if str(value)
        ]
        if selected_capabilities:
            semantic_history["selected_capabilities"] = selected_capabilities
        capability_contract = (runtime or {}).get("capability_contract")
        if capability_contract:
            semantic_history["capabilities"] = _without_control_metadata(
                capability_contract
            )
        if normalized_node == "planner" and runtime:
            planner_runtime = _compact_planner_runtime_projection(runtime)
            if planner_runtime:
                semantic_history["runtime"] = planner_runtime
        if plan_snapshot:
            snapshot_view = _approved_plan_semantic_view(
                planning_plan,
                steps,
                handles,
            )
            if str(plan_snapshot_status or ""):
                snapshot_view["snapshot_status"] = str(plan_snapshot_status)
            semantic_history["plan_snapshot"] = snapshot_view
        elif approved_plan_snapshot:
            semantic_history["approved_plan"] = _approved_plan_semantic_view(
                planning_plan,
                steps,
                handles,
            )
        outcomes = _accepted_execution_outcomes(
            plan,
            execution_records,
            step_handles,
            handles,
        )
        if outcomes:
            semantic_history["accepted_execution_outcomes"] = outcomes
        accepted_decisions = _accepted_recovery_decisions(
            recovery_decisions,
            step_handles,
        )
        if accepted_decisions:
            semantic_history["accepted_recovery_decisions"] = accepted_decisions

        current_focus: Dict[str, Any] = OrderedDict()
        planning_failure = dict(
            (plan.get("task_state") or {}).get("planning_failure") or {}
        )
        if normalized_node == "planner":
            planning_override = (
                dict((feedback or {}).get("planning") or {})
                if isinstance((feedback or {}).get("planning"), Mapping)
                else {}
            )
            if str(planning_override.get("mode") or "") == "plan_impact_review":
                current_focus["planning"] = _without_control_metadata(
                    planning_override
                )
            elif str(planning_failure.get("status") or "") == "unresolved":
                current_focus["planning"] = _planning_failure_focus(
                    planning_failure,
                    handles,
                )
            else:
                current_focus["planning"] = {
                    "mode": "create_complete_plan",
                    "attempt": 1,
                    "instruction": {"return_complete_plan": True},
                }
        if normalized_node == "planner_repair":
            current_focus["candidate_plan"] = {"steps": steps}
        if normalized_node in {"generate", "recovery"}:
            current_focus["current_step"] = selected_step
            if dependency_contracts:
                current_focus["dependency_contracts"] = dependency_contracts
        relevant_skill_excerpts = [
            _without_control_metadata(item)
            for item in skill_context.get("relevant_skill_excerpts", []) or []
            if isinstance(item, Mapping)
        ]
        if relevant_skill_excerpts:
            current_focus["relevant_skill_excerpts"] = relevant_skill_excerpts
        skill_tool_results = [
            _without_control_metadata(item)
            for item in skill_context.get("skill_tool_results", []) or []
            if isinstance(item, Mapping)
        ]
        if skill_tool_results:
            current_focus["skill_tool_results"] = skill_tool_results
        if runtime and normalized_node != "planner":
            current_focus["runtime"] = _without_control_metadata(runtime)
        if feedback:
            current_focus["feedback"] = _without_control_metadata(feedback)
        if visible_violations:
            current_focus["violations"] = visible_violations
        if evidence_rows:
            current_focus["evidence"] = evidence_rows
        if allowed_operations:
            current_focus["allowed_operations"] = [
                str(value) for value in allowed_operations
            ]
        if (
            normalized_node == "planner"
            and str(planning_failure.get("status") or "") == "unresolved"
            and not isinstance(current_focus.get("planning"), Mapping)
        ):
            raise InvocationProtocolError(
                "PLANNING_FAILURE_PROJECTION_MISSING"
            )
        payload: Dict[str, Any] = OrderedDict((
            ("semantic_history", semantic_history),
            ("current_focus", current_focus),
        ))
        compatibility_task = deepcopy(task_view)
        if normalized_node in {"generate", "recovery"}:
            compatibility_task.update({
                "inputs": focus_inputs,
                "outputs": focus_outputs,
            })
        compatibility_payload = {"task": compatibility_task}
        if normalized_node == "planner_repair":
            compatibility_payload["plan"] = {"steps": steps}
        compatibility_payload.update(deepcopy(current_focus))
        selection_policy = "full-task-with-current-step-focus-v1"
    else:
        # Consultant, Supervisor and PlanReview keep their existing flat
        # contracts. Consultant nevertheless receives the same bounded,
        # immutable Skill projection as the other semantic callers.
        payload = {"task": task_view}
        if normalized_node in {"planner_repair", "plan_review"}:
            payload["plan"] = {"steps": steps}
        if normalized_node in {"generate", "recovery", "supervisor"}:
            payload["current_step"] = selected_step
            if dependency_contracts:
                payload["dependency_contracts"] = dependency_contracts
        if runtime:
            payload["runtime"] = _without_control_metadata(runtime)
        if feedback:
            payload["feedback"] = _without_control_metadata(feedback)
        if visible_violations:
            payload["violations"] = visible_violations
        if evidence_rows:
            payload["evidence"] = evidence_rows
        if normalized_node == "consultant":
            for key in (
                "selected_skill_refs",
                "available_skill_headings",
                "relevant_skill_excerpts",
                "skill_tool_results",
            ):
                rows = [
                    _without_control_metadata(item)
                    for item in skill_context.get(key, []) or []
                    if isinstance(item, Mapping)
                ]
                if rows:
                    payload[key] = rows
        if allowed_operations:
            payload["allowed_operations"] = [str(value) for value in allowed_operations]

    llm_payload = LLMPayload(payload, compatibility_payload)
    payload_chars = len(llm_payload.to_json())
    if max_payload_chars is not None and payload_chars > int(max_payload_chars):
        report = payload_observability_report(payload)
        raise ContextCapacityExceeded(
            "CONTEXT_CAPACITY_EXCEEDED: "
            f"required payload is {payload_chars} characters, capacity is {int(max_payload_chars)}; "
            f"top_sections={report['top_sections']}"
        )

    task_sem_hash = task_semantic_hash(task_spec)
    task_rec_hash = task_record_hash(task_spec)
    plan_sem_hash = _normalize_hash(semantic_plan_hash(step_plan)) if step_plan else ""
    plan_rec_hash = plan_record_hash(step_plan) if step_plan else ""
    binding = ControllerBinding(
        invocation_id=invocation_id or f"inv-{uuid.uuid4()}",
        node_type=normalized_node,
        staleness_domain=NODE_STALENESS_DOMAINS[normalized_node],
        state_generation=max(0, int(state_generation or 0)),
        task_semantic_hash=task_sem_hash,
        task_record_hash=task_rec_hash,
        plan_semantic_hash=plan_sem_hash,
        plan_record_hash=plan_rec_hash,
        task_revision=max(0, int(task_spec_revision(task_spec))),
        plan_revision=max(0, int(plan.get("plan_revision") or 0)),
        authoritative_user_turn_revision=max(
            0, int(authoritative_user_turn_revision or 0)
        ),
        continuation_id=str(continuation_id or ""),
        approval_ref=str(approval_ref or ""),
        approval_applicability=str(approval_applicability or ""),
        step_id=str(current_step_id or ""),
        execution_epoch=str(execution_epoch if execution_epoch is not None else ""),
        attempt_id=str(attempt_id or ""),
        failure_fingerprint=str(failure_fingerprint or ""),
        violation_set_identity=str(
            violation_set_identity
            or (_sha256([_without_control_metadata(item) for item in violations]) if violations else "")
        ),
        action_identity=str(action_identity or ""),
        evidence_identity=str(evidence_identity or ""),
        candidate_scope_id=(
            str(candidate_scope_id or new_candidate_scope_id())
            if normalized_node in {"planner", "planner_repair"}
            else ""
        ),
        touched_entity_versions=dict(touched_entity_versions or {}),
        semantic_handle_map=handles.to_dict(),
    )

    trace: Optional[ProjectionTrace] = None
    if trace_enabled:
        constraint_handles = tuple(row["handle"] for row in constraints)
        artifact_handles = tuple(row["handle"] for row in [*inputs, *outputs])
        projection_identity = {
            "projection_type": f"{normalized_node}_task_view",
            "projection_version": f"{normalized_node}-v2",
            "selection_policy": selection_policy,
            "task_semantic_hash": task_sem_hash,
            "plan_semantic_hash": plan_sem_hash,
            "included_constraint_handles": constraint_handles,
            "included_artifact_handles": artifact_handles,
            "semantic_payload": payload,
        }
        trace = ProjectionTrace(
            projection_type=f"{normalized_node}_task_view",
            projection_version=f"{normalized_node}-v2",
            selection_policy=str(projection_identity["selection_policy"]),
            included_constraint_handles=constraint_handles,
            included_artifact_handles=artifact_handles,
            excluded_handles_with_reason=excluded_handles,
            projection_semantic_hash=_sha256(projection_identity),
            node_context_hash=_sha256({
                "projection": projection_identity,
                "binding": binding.to_dict(),
            }),
        )
    return NodeContext(binding=binding, llm_payload=llm_payload, projection_trace=trace)


@dataclass(frozen=True)
class StalenessDecision:
    usable: bool
    code: str
    changed_preconditions: Tuple[str, ...] = ()
    ignored_record_changes: Tuple[str, ...] = ()
    stale_signature: str = ""


_NODE_RELEVANT_PRECONDITIONS = {
    "consultant": (
        "task_semantic_hash", "task_revision", "plan_semantic_hash",
        "plan_revision", "authoritative_user_turn_revision", "step_id",
        "continuation_id", "touched_entity_versions",
    ),
    "planner": (
        "task_semantic_hash", "task_revision", "plan_semantic_hash",
        "plan_revision", "authoritative_user_turn_revision", "step_id",
        "continuation_id",
    ),
    "planner_repair": (
        "task_semantic_hash", "task_revision", "plan_semantic_hash",
        "plan_revision", "authoritative_user_turn_revision",
        "step_id", "violation_set_identity", "continuation_id",
    ),
    "generate": (
        "task_semantic_hash", "task_revision", "plan_semantic_hash",
        "plan_revision", "authoritative_user_turn_revision", "approval_applicability",
        "approval_ref", "step_id", "execution_epoch", "continuation_id",
    ),
    "recovery": (
        "task_semantic_hash", "task_revision", "plan_semantic_hash",
        "plan_revision", "authoritative_user_turn_revision", "approval_ref", "step_id",
        "action_identity", "attempt_id", "failure_fingerprint", "evidence_identity",
        "continuation_id",
    ),
    "supervisor": (
        "step_id", "action_identity", "attempt_id", "evidence_identity", "task_semantic_hash",
    ),
    "plan_review": (
        "task_semantic_hash", "task_revision", "plan_semantic_hash",
        "plan_revision", "authoritative_user_turn_revision", "step_id",
        "continuation_id",
    ),
}


def evaluate_node_staleness(
    binding: ControllerBinding,
    current: Mapping[str, Any],
) -> StalenessDecision:
    """Compare only the correctness domain of the bound node invocation."""
    relevant = _NODE_RELEVANT_PRECONDITIONS[binding.node_type]
    changed: List[str] = []
    for field_name in relevant:
        before = getattr(binding, field_name)
        after = current.get(field_name, before)
        if field_name == "touched_entity_versions":
            after_map = dict(after or {})
            for entity, version in dict(before or {}).items():
                if str(after_map.get(entity, version)) != str(version):
                    changed.append(f"touched_entity_versions.{entity}")
        elif str(after if after is not None else "") != str(before if before is not None else ""):
            changed.append(field_name)

    ignored = []
    for field_name in ("state_generation", "task_record_hash", "plan_record_hash"):
        before = getattr(binding, field_name)
        if field_name in current and str(current.get(field_name)) != str(before):
            ignored.append(field_name)
    if not changed:
        return StalenessDecision(
            usable=True,
            code="REVALIDATE_ON_CURRENT_RECORD" if ignored else "CURRENT",
            ignored_record_changes=tuple(sorted(ignored)),
        )
    code = {
        "consultant": "STALE_TASK_SEMANTICS",
        "planner": "STALE_TASK_SEMANTICS",
        "planner_repair": "STALE_PLAN_REPAIR_BASE",
        "generate": "STALE_GENERATE_CONTEXT",
        "recovery": "STALE_RECOVERY_CONTEXT",
        "supervisor": "STALE_SUPERVISOR_CONTEXT",
        "plan_review": "STALE_REVIEW_CONTEXT",
    }[binding.node_type]
    signature_payload = {
        "node_type": binding.node_type,
        "domain": binding.staleness_domain,
        "changed": sorted(changed),
        "before": {name: getattr(binding, name.split(".", 1)[0]) for name in relevant},
        "after": {name: current.get(name) for name in relevant if name in current},
        "reason": code,
    }
    return StalenessDecision(
        usable=False,
        code=code,
        changed_preconditions=tuple(sorted(changed)),
        ignored_record_changes=tuple(sorted(ignored)),
        stale_signature=_sha256(signature_payload),
    )


@dataclass
class _InvocationEntry:
    binding: ControllerBinding
    stale_signature: str
    status: str = "active"


class InvocationRegistry:
    """Bounded response registry with duplicate/late and stale deduplication."""

    def __init__(self, *, max_entries: int = 512, max_stale_signatures: int = 512) -> None:
        if max_entries < 1 or max_stale_signatures < 1:
            raise ValueError("registry bounds must be positive")
        self.max_entries = int(max_entries)
        self.max_stale_signatures = int(max_stale_signatures)
        self._entries: "OrderedDict[str, _InvocationEntry]" = OrderedDict()
        self._active_signatures: Dict[str, str] = {}
        self._stale_signatures: "OrderedDict[str, None]" = OrderedDict()

    def register(self, binding: ControllerBinding, *, stale_signature: str = "") -> None:
        invocation_id = binding.invocation_id
        if invocation_id in self._entries:
            raise InvocationProtocolError(f"INVOCATION_DUPLICATE: {invocation_id}")
        if stale_signature:
            active = self._active_signatures.get(stale_signature)
            if active:
                raise InvocationProtocolError(
                    f"EQUIVALENT_INVOCATION_ALREADY_ACTIVE: {stale_signature} ({active})"
                )
            if stale_signature in self._stale_signatures:
                raise InvocationProtocolError(f"STALE_SIGNATURE_ALREADY_HANDLED: {stale_signature}")
            self._active_signatures[stale_signature] = invocation_id
        self._entries[invocation_id] = _InvocationEntry(binding, stale_signature)
        self._entries.move_to_end(invocation_id)
        self._trim_entries()

    def binding_for(self, invocation_id: str) -> ControllerBinding:
        entry = self._entries.get(str(invocation_id or ""))
        if entry is None:
            raise InvocationProtocolError(f"INVOCATION_UNKNOWN: {invocation_id}")
        if entry.status != "active":
            raise InvocationProtocolError(
                f"INVOCATION_NOT_ACTIVE: {invocation_id} ({entry.status})"
            )
        return entry.binding

    def consume(self, invocation_id: str) -> ControllerBinding:
        binding = self.binding_for(invocation_id)
        entry = self._entries[invocation_id]
        entry.status = "consumed"
        if entry.stale_signature:
            self._active_signatures.pop(entry.stale_signature, None)
        self._entries.move_to_end(invocation_id)
        return binding

    def terminate(self, invocation_id: str, *, status: str = "failed") -> None:
        entry = self._entries.get(str(invocation_id or ""))
        if entry is None:
            raise InvocationProtocolError(f"INVOCATION_UNKNOWN: {invocation_id}")
        if entry.status != "active":
            raise InvocationProtocolError(
                f"INVOCATION_NOT_ACTIVE: {invocation_id} ({entry.status})"
            )
        entry.status = str(status or "failed")
        if entry.stale_signature:
            self._active_signatures.pop(entry.stale_signature, None)
        self._entries.move_to_end(invocation_id)

    def mark_stale_signature_handled(self, signature: str) -> bool:
        key = str(signature or "")
        if not key:
            return False
        if key in self._stale_signatures:
            self._stale_signatures.move_to_end(key)
            return False
        self._stale_signatures[key] = None
        while len(self._stale_signatures) > self.max_stale_signatures:
            self._stale_signatures.popitem(last=False)
        return True

    def _trim_entries(self) -> None:
        while len(self._entries) > self.max_entries:
            removable = next(
                (key for key, value in self._entries.items() if value.status != "active"),
                None,
            )
            if removable is None:
                # Never evict an active binding: doing so would make a valid
                # response unverifiable.  The caller must apply backpressure.
                newest = next(reversed(self._entries))
                entry = self._entries.pop(newest)
                if entry.stale_signature:
                    self._active_signatures.pop(entry.stale_signature, None)
                raise InvocationProtocolError("INVOCATION_REGISTRY_CAPACITY_EXCEEDED")
            self._entries.pop(removable)

    @property
    def size(self) -> int:
        return len(self._entries)


def payload_observability_report(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Phase-0 metrics for prompt size and control-metadata leakage."""
    provider_payload: Mapping[str, Any] = payload
    if "semantic_history" in payload and "current_focus" in payload:
        provider_payload = {
            "semantic_history": payload.get("semantic_history") or {},
            "current_focus": payload.get("current_focus") or {},
        }
    section_chars = {
        str(key): len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
        for key, value in provider_payload.items()
    }
    top_sections = sorted(section_chars.items(), key=lambda item: (-item[1], item[0]))
    serialised = json.dumps(provider_payload, ensure_ascii=False, sort_keys=False, default=str)
    counts = {"constraints": 0, "inputs": 0, "outputs": 0, "evidence": 0}
    semantic = (
        provider_payload.get("semantic_history")
        if isinstance(provider_payload.get("semantic_history"), Mapping)
        else {}
    )
    focus = (
        provider_payload.get("current_focus")
        if isinstance(provider_payload.get("current_focus"), Mapping)
        else {}
    )
    task = (
        semantic.get("task_spec")
        if isinstance(semantic, Mapping) and isinstance(semantic.get("task_spec"), Mapping)
        else provider_payload.get("task")
        if isinstance(provider_payload.get("task"), Mapping)
        else {}
    )
    for key in ("constraints", "inputs", "outputs"):
        value = task.get(key, []) if isinstance(task, Mapping) else []
        counts[key] = len(value) if isinstance(value, list) else 0
    counts["evidence"] = len(
        focus.get("evidence", [])
        if isinstance(focus, Mapping)
        else provider_payload.get("evidence", []) or []
    )
    return {
        "total_chars": len(serialised),
        "section_chars": section_chars,
        "top_sections": top_sections,
        "included_counts": counts,
        "control_metadata_leaks": control_metadata_leaks(provider_payload),
    }


def resolve_response_handles(
    binding: ControllerBinding,
    handles: Iterable[str],
    *,
    object_type: str = "",
    direction: str = "",
    usage: str = "",
) -> List[Dict[str, Any]]:
    mapping = SemanticHandleMap(
        SemanticHandleBinding(
            semantic_handle=str(item.get("semantic_handle") or key),
            object_type=str(item.get("object_type") or ""),
            stable_identity=str(item.get("stable_identity") or ""),
            direction=str(item.get("direction") or ""),
            allowed_usage=tuple(item.get("allowed_usage") or ()),
            metadata=dict(item.get("metadata") or {}),
        )
        for key, item in binding.semantic_handle_map.items()
        if isinstance(item, Mapping)
    )
    return [
        mapping.resolve(
            str(handle),
            object_type=object_type,
            direction=direction,
            usage=usage,
        ).to_dict()
        for handle in handles
    ]


def resolve_plan_proposal_handles(
    value: Mapping[str, Any],
    binding: ControllerBinding,
) -> Dict[str, Any]:
    """Resolve declared plan reference fields without touching free text."""
    proposal = deepcopy(dict(value or {}))
    handle_map = SemanticHandleMap(
        SemanticHandleBinding(
            semantic_handle=str(item.get("semantic_handle") or key),
            object_type=str(item.get("object_type") or ""),
            stable_identity=str(item.get("stable_identity") or ""),
            direction=str(item.get("direction") or ""),
            allowed_usage=tuple(item.get("allowed_usage") or ()),
            metadata=dict(item.get("metadata") or {}),
        )
        for key, item in binding.semantic_handle_map.items()
        if isinstance(item, Mapping)
    )

    def resolve_list(raw: Any, *, object_type: str = "") -> Any:
        """Resolve identity only; the containing Plan field owns step usage."""
        if not isinstance(raw, list):
            return raw
        resolved: List[Any] = []
        for item in raw:
            text = str(item or "")
            if text in handle_map:
                resolved.append(handle_map.resolve(
                    text,
                    object_type=object_type,
                ).stable_identity)
            else:
                resolved.append(deepcopy(item))
        return resolved

    def resolve_scalar(raw: Any, *, object_type: str) -> Any:
        text = str(raw or "")
        if text in handle_map:
            return handle_map.resolve(
                text,
                object_type=object_type,
            ).stable_identity
        return deepcopy(raw)

    def resolve_step_fields(step: MutableMapping[str, Any]) -> None:
        if "step_id" in step:
            step["step_id"] = resolve_scalar(
                step.get("step_id"), object_type="plan_step",
            )
        for field in ("dependencies", "depends_on"):
            if field in step:
                step[field] = resolve_list(
                    step.get(field), object_type="plan_step",
                )
        for field in ("inputs", "input_ids", "input_artifacts", "input_refs"):
            if field in step:
                step[field] = resolve_list(
                    step.get(field), object_type="artifact",
                )
        for field in ("outputs", "output_ids", "output_artifacts", "output_refs"):
            if field in step:
                step[field] = resolve_list(
                    step.get(field), object_type="artifact",
                )
        if "skill_ids" in step:
            step["skill_ids"] = resolve_list(
                step.get("skill_ids"), object_type="skill",
            )
        if "workflow_stage_ids" in step:
            step["workflow_stage_ids"] = resolve_list(
                step.get("workflow_stage_ids"), object_type="workflow_stage",
            )

    steps = proposal.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, MutableMapping):
                continue
            resolve_step_fields(step)
    materializations = proposal.get("task_output_materializations")
    if isinstance(materializations, list):
        for row in materializations:
            if not isinstance(row, MutableMapping):
                continue
            output_ref = str(row.get("output_ref") or "")
            if output_ref in handle_map:
                row["output_ref"] = handle_map.resolve(
                    output_ref,
                    object_type="artifact",
                    direction="output",
                ).stable_identity
    replacements = proposal.get("replace_steps")
    if isinstance(replacements, list):
        for replacement in replacements:
            if not isinstance(replacement, MutableMapping):
                continue
            step_handle = str(replacement.get("step_handle") or "")
            if step_handle:
                replacement["step_id"] = handle_map.resolve(
                    step_handle, object_type="plan_step",
                ).stable_identity
                replacement.pop("step_handle", None)
            elif "step_id" in replacement:
                replacement["step_id"] = resolve_scalar(
                    replacement.get("step_id"), object_type="plan_step",
                )
            new_step = replacement.get("new_step")
            if isinstance(new_step, MutableMapping):
                resolve_step_fields(new_step)
    return proposal


def parse_managed_placeholder(value: str) -> Tuple[str, str]:
    match = _PLACEHOLDER_RE.fullmatch(str(value or ""))
    if not match:
        raise SemanticHandleError(f"MANAGED_PLACEHOLDER_INVALID: {value!r}")
    direction, name = match.groups()
    return direction, f"{direction}.{name}"
