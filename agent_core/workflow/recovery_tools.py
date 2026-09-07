"""Typed, read-only inspection tools for one Recovery invocation.

The provider sees opaque typed references only.  This module resolves them to
the existing phase-archive, SkillAccessService, managed-artifact, and recovery
evidence backends after enforcing task/plan/step scope.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from agent_core.managed_artifacts import build_approved_visible_artifact_set
from agent_core.approved_resource_locator import approved_resource_locator, open_approved_resource, ResourceLocatorError
from agent_core.plan_tracker import canonical_task_spec_for_state
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.skill_access import REQUEST_SKILL_CONTEXT, SkillAccessService
from agent_core.workflow.phase_archive import (
    PHASE_ARCHIVE_REF_PREFIX,
    PhaseArchiveStore,
)
from agent_core.workflow.phase_inspection import PhaseInspectionService
from agent_core.workflow.stage_contracts import RecoverySeed


MAX_TOOL_RESULT_CHARS = 24_000
MAX_STEP_ITEMS = 50
MAX_ARTIFACT_READ_BYTES = 64 * 1024
MAX_ARTIFACT_READ_LINES = 240
MAX_ARTIFACT_HASH_BYTES = 64 * 1024 * 1024

_KINDS = frozenset({
    "phase_archive",
    "failure",
    "execution",
    "attempt",
    "artifact",
    "evidence",
    "evidence_scope",
    "skill_resource",
})


class RecoveryToolError(ValueError):
    """Stable fail-closed error for registry and local tool violations."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "RECOVERY_TOOL_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _required(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RecoveryToolError("INSPECTION_REF_FIELD_REQUIRED", field_name)
    return text


def _scope_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(deepcopy(dict(value or {})))


@dataclass(frozen=True)
class InspectionRef:
    ref_id: str
    kind: str
    task_id: str
    plan_id: str
    step_id: str
    backend: str
    digest: str
    size_hint: int
    scope: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("ref_id", "task_id", "backend", "digest"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        kind = _required(self.kind, "kind")
        if kind not in _KINDS:
            raise RecoveryToolError("INSPECTION_REF_KIND_INVALID", kind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "plan_id", str(self.plan_id or "").strip())
        object.__setattr__(self, "step_id", str(self.step_id or "").strip())
        try:
            size_hint = int(self.size_hint)
        except (TypeError, ValueError) as exc:
            raise RecoveryToolError("INSPECTION_REF_SIZE_INVALID", self.ref_id) from exc
        if size_hint < 0:
            raise RecoveryToolError("INSPECTION_REF_SIZE_INVALID", self.ref_id)
        object.__setattr__(self, "size_hint", size_hint)
        object.__setattr__(self, "scope", _scope_mapping(self.scope))
        _canonical(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "kind": self.kind,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "backend": self.backend,
            "digest": self.digest,
            "size_hint": self.size_hint,
            "scope": deepcopy(dict(self.scope)),
        }


class TypedInspectionRegistry:
    """Invocation-local registry whose locators are never provider-visible."""

    def __init__(self, *, task_id: str, plan_id: str, step_id: str) -> None:
        self.task_id = _required(task_id, "task_id")
        self.plan_id = str(plan_id or "").strip()
        self.step_id = _required(step_id, "step_id")
        self._entries: dict[str, InspectionRef] = {}
        self._locators: dict[str, Mapping[str, Any]] = {}

    def register(
        self, entry: InspectionRef, *, locator: Mapping[str, Any] | None = None
    ) -> InspectionRef:
        if entry.task_id != self.task_id:
            raise RecoveryToolError("INSPECTION_REF_SCOPE_MISMATCH", entry.ref_id)
        if entry.plan_id and self.plan_id and entry.plan_id != self.plan_id:
            raise RecoveryToolError("INSPECTION_REF_SCOPE_MISMATCH", entry.ref_id)
        normalized_locator = MappingProxyType(deepcopy(dict(locator or {})))
        prior = self._entries.get(entry.ref_id)
        if prior is not None:
            if prior != entry or dict(self._locators[entry.ref_id]) != dict(normalized_locator):
                raise RecoveryToolError("INSPECTION_REF_COLLISION", entry.ref_id)
            return prior
        self._entries[entry.ref_id] = entry
        self._locators[entry.ref_id] = normalized_locator
        return entry

    def resolve(
        self,
        ref_id: str,
        *,
        kinds: Sequence[str] = (),
        allow_dependency_step: bool = False,
    ) -> tuple[InspectionRef, Mapping[str, Any]]:
        ref = self._entries.get(str(ref_id or ""))
        if ref is None:
            raise RecoveryToolError("INSPECTION_REF_NOT_DECLARED", str(ref_id or ""))
        if kinds and ref.kind not in set(kinds):
            raise RecoveryToolError(
                "INSPECTION_REF_KIND_MISMATCH", f"{ref.ref_id}:{ref.kind}"
            )
        access = str(ref.scope.get("access") or "current_step")
        if ref.step_id and ref.step_id != self.step_id:
            if not allow_dependency_step or access != "direct_dependency":
                raise RecoveryToolError("INSPECTION_REF_SCOPE_MISMATCH", ref.ref_id)
        return ref, self._locators[ref.ref_id]

    def provider_manifest(self) -> list[dict[str, Any]]:
        return [self._entries[key].to_dict() for key in sorted(self._entries)]


def _ref_kind(ref_id: str) -> str:
    value = str(ref_id or "")
    if value.startswith(PHASE_ARCHIVE_REF_PREFIX):
        return "phase_archive"
    if value.startswith("artifact:"):
        return "artifact"
    if value.startswith("attempt:"):
        return "attempt"
    if value.startswith(("execution:", "outcome:")):
        return "execution"
    return "evidence"


def _entry_from_durable(
    value: Mapping[str, Any], *, default_ref: str, default_task: str
) -> tuple[InspectionRef, Mapping[str, Any]]:
    locator = dict(value.get("locator") or {})
    descriptor = InspectionRef(
        ref_id=str(value.get("ref_id") or default_ref),
        kind=str(value.get("kind") or _ref_kind(default_ref)),
        task_id=str(value.get("task_id") or default_task),
        plan_id=str(value.get("plan_id") or ""),
        step_id=str(value.get("step_id") or ""),
        backend=str(value.get("backend") or "durable_registry"),
        digest=str(value.get("digest") or _digest({"ref_id": default_ref})),
        size_hint=int(value.get("size_hint") or 0),
        scope=dict(value.get("scope") or {}),
    )
    return descriptor, locator


def build_recovery_inspection_registry(
    seed: RecoverySeed | Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    phase_store: PhaseArchiveStore,
) -> TypedInspectionRegistry:
    """Compile only seed-declared/current-step references into one registry."""

    parsed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
    packet = parsed.failure_packet
    registry = TypedInspectionRegistry(
        task_id=packet.task_id,
        plan_id=parsed.plan_spec_ref.plan_id,
        step_id=packet.step_id,
    )
    registry.register(InspectionRef(
        ref_id=packet.source_outcome_ref,
        kind="failure",
        task_id=packet.task_id,
        plan_id=parsed.plan_spec_ref.plan_id,
        step_id=packet.step_id,
        backend="causal_failure_packet",
        digest=_digest(packet.to_dict()),
        size_hint=len(_canonical(packet.to_dict())),
        scope={"access": "current_step"},
    ), locator={"failure_packet": packet.to_dict()})

    task_state = dict(durable.get("task_state") or {})
    durable_refs = dict(task_state.get("inspection_ref_registry") or {})
    declared_refs = set(packet.inspection_refs) | set(packet.evidence_refs)
    for dependency in parsed.dependency_outcomes:
        for field_name in ("outcome_ref", "archive_ref"):
            if dependency.get(field_name):
                declared_refs.add(str(dependency[field_name]))
        for field_name in ("evidence_refs", "artifact_refs", "binding_refs"):
            declared_refs.update(str(item) for item in dependency.get(field_name, ()) or ())

    dependency_steps = {
        str(item.get("step_id") or "")
        for item in parsed.dependency_outcomes
        if str(item.get("step_id") or "")
    }
    for ref_id in sorted(declared_refs):
        if ref_id == packet.source_outcome_ref:
            continue
        if ref_id.startswith(PHASE_ARCHIVE_REF_PREFIX):
            record = phase_store.read(ref_id)
            metadata = dict(record.metadata or {})
            archive_step = str(metadata.get("step_id") or packet.step_id)
            access = (
                "current_step"
                if archive_step == packet.step_id
                else "direct_dependency"
                if archive_step in dependency_steps
                else "forbidden"
            )
            if access == "forbidden":
                raise RecoveryToolError("INSPECTION_REF_SCOPE_MISMATCH", ref_id)
            registry.register(InspectionRef(
                ref_id=ref_id,
                kind="phase_archive",
                task_id=record.task_id,
                plan_id=str(metadata.get("plan_id") or parsed.plan_spec_ref.plan_id),
                step_id=archive_step,
                backend="phase_archive",
                digest="sha256:" + record.digest,
                size_hint=len(_canonical(record.to_dict())),
                scope={"access": access, "phase": record.phase},
            ), locator={"archive_ref": ref_id, "phase": record.phase})
            continue
        durable_entry = durable_refs.get(ref_id)
        if isinstance(durable_entry, Mapping):
            entry, locator = _entry_from_durable(
                durable_entry, default_ref=ref_id, default_task=packet.task_id
            )
            registry.register(entry, locator=locator)
            continue
        registry.register(InspectionRef(
            ref_id=ref_id,
            kind=_ref_kind(ref_id),
            task_id=packet.task_id,
            plan_id=parsed.plan_spec_ref.plan_id,
            step_id=packet.step_id,
            backend="opaque_reference",
            digest=_digest({"ref_id": ref_id}),
            size_hint=0,
            scope={"access": "current_step", "resolvable": False},
        ))

    try:
        task_spec = canonical_task_spec_for_state(durable)
        visible = build_approved_visible_artifact_set(
            task_spec,
            plan_state=durable,
            approved_plan_snapshot=dict(
                (durable.get("current_plan") or {}).get("plan_spec") or {}
            ),
            current_step_id=packet.step_id,
        )
    except Exception:
        visible = {}
    for artifact_id, value in sorted(visible.items()):
        row = dict(value.get("row") or {})
        try:
            normalized = approved_resource_locator(row)
        except ResourceLocatorError as exc:
            normalized = {"locator_error": exc.code}
        path = str(normalized.get("path") or "")
        ref_id = "artifact:" + str(artifact_id)
        registry.register(InspectionRef(
            ref_id=ref_id,
            kind="artifact",
            task_id=packet.task_id,
            plan_id=parsed.plan_spec_ref.plan_id,
            step_id=packet.step_id,
            backend="managed_artifacts",
            digest=str(row.get("sha256") or _digest(value)),
            size_hint=int(row.get("size") or 0),
            scope={"access": "current_step", "direction": value.get("direction")},
        ), locator={**normalized, "artifact_id": artifact_id, "path": path, "binding": value})

    for skill in parsed.selected_skill_bundle.skills:
        registry.register(InspectionRef(
            ref_id="skill:" + skill.skill_id,
            kind="skill_resource",
            task_id=packet.task_id,
            plan_id=parsed.plan_spec_ref.plan_id,
            step_id=packet.step_id,
            backend="skill_access_service",
            digest=skill.content_hash,
            size_hint=0,
            scope={"access": "selected_skill", "skill_id": skill.skill_id},
        ), locator={"skill_id": skill.skill_id, "snapshot_id": skill.snapshot_id})
    return registry


class RecoveryLocalToolDispatcher:
    """Dispatch the bounded provider tools without accepting filesystem paths."""

    def __init__(
        self,
        *,
        seed: RecoverySeed | Mapping[str, Any],
        durable: Mapping[str, Any],
        registry: TypedInspectionRegistry,
        phase_store: PhaseArchiveStore,
        skill_service: SkillAccessService | None = None,
        evidence_reader: RecoveryEvidenceReader | None = None,
        max_result_chars: int = MAX_TOOL_RESULT_CHARS,
    ) -> None:
        self.seed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
        self.durable = deepcopy(dict(durable))
        self.registry = registry
        authority = Path(phase_store.root.parent) / "task_outputs" / self.seed.failure_packet.task_id / "plan_state.json"
        self._artifact_authority_path = authority if authority.is_file() else None
        self.phase_service = PhaseInspectionService(
            phase_store,
            reference_resolver=lambda ref: next(
                (
                    item for item in registry.provider_manifest()
                    if item["ref_id"] == ref
                ),
                None,
            ),
        )
        self.skill_service = skill_service
        self.evidence_reader = evidence_reader
        self.max_result_chars = max(512, min(int(max_result_chars), MAX_TOOL_RESULT_CHARS))
        self._skill_state = deepcopy(dict(
            (durable.get("task_state") or {}).get("skill_access_state")
            or durable.get("skill_access_state")
            or {}
        ))
        self._selected_skills = {
            item.skill_id for item in self.seed.selected_skill_bundle.skills
        }

    def _bounded(self, tool: str, value: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(value))
        result.setdefault("tool", tool)
        result["result_chars"] = 0
        size = len(_canonical(result))
        result["result_chars"] = size
        final_size = len(_canonical(result))
        if final_size != size:
            result["result_chars"] = final_size
            final_size = len(_canonical(result))
        if final_size > self.max_result_chars:
            return {
                "tool": tool,
                "status": "REQUIRES_NARROWING",
                "result_chars": final_size,
                "max_result_chars": self.max_result_chars,
            }
        return result

    def dispatch(self, tool: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        name = str(tool or "").strip().lower()
        if name not in {
            "failure.inspect", "step.inspect", "artifact.inspect", "artifact.read",
            "skill.headings", "skill.read", "skill.search", "skill.resource",
            "evidence.read", "evidence.glob",
        }:
            name = name.replace("_", ".")
        args = dict(arguments or {})
        handlers = {
            "failure.inspect": self._failure_inspect,
            "step.inspect": self._step_inspect,
            "artifact.inspect": self._artifact_inspect,
            "artifact.read": self._artifact_read,
            "skill.headings": self._skill_headings,
            "skill.read": self._skill_read,
            "skill.search": self._skill_search,
            "skill.resource": self._skill_resource,
            "evidence.read": self._evidence_read,
            "evidence.glob": self._evidence_glob,
        }
        handler = handlers.get(name)
        if handler is None:
            raise RecoveryToolError("RECOVERY_TOOL_UNKNOWN", name)
        try:
            return self._bounded(name, handler(args))
        except ResourceLocatorError as exc:
            raise RecoveryToolError(exc.code, name) from exc
        except PermissionError as exc:
            if not name.startswith("artifact."): raise
            raise RecoveryToolError("ARTIFACT_PERMISSION_DENIED", name) from exc
        except FileNotFoundError as exc:
            if not name.startswith("artifact."): raise
            raise RecoveryToolError("ARTIFACT_FILE_NOT_FOUND", name) from exc
        except OSError as exc:
            if not name.startswith("artifact."): raise
            raise RecoveryToolError("ARTIFACT_IO_ERROR", name) from exc

    def _failure_inspect(self, args: Mapping[str, Any]) -> dict[str, Any]:
        packet = self.seed.failure_packet
        requested = str(args.get("ref_id") or packet.source_outcome_ref)
        self.registry.resolve(requested, kinds=("failure",))
        return {
            "failure_packet": packet.to_dict(),
            "inspection_refs": [
                item for item in self.registry.provider_manifest()
                if item["ref_id"] in set(packet.inspection_refs)
            ],
        }

    def _step_inspect(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, locator = self.registry.resolve(
            str(args.get("ref_id") or ""),
            kinds=("phase_archive",),
            allow_dependency_step=True,
        )
        section = str(args.get("section") or "summary")
        try:
            offset = max(0, int(args.get("offset") or 0))
            limit = min(MAX_STEP_ITEMS, max(1, int(args.get("limit") or MAX_STEP_ITEMS)))
        except (TypeError, ValueError) as exc:
            raise RecoveryToolError("RECOVERY_TOOL_ARGUMENT_INVALID", "step pagination") from exc
        view = self.phase_service.inspect_step(
            archive_ref=str(locator.get("archive_ref") or entry.ref_id),
            task_id=entry.task_id,
            phase=str(locator.get("phase") or entry.scope.get("phase") or "step_worker"),
            step_id=entry.step_id,
            section=section,
            offset=offset,
            limit=(limit if section == "transcript" else None),
        )
        if section == "transcript":
            rows = list(view.get("transcript") or [])
            event_type = str(args.get("event_type") or "").casefold()
            tool_name = str(args.get("tool") or "").casefold()
            validator = str(args.get("validator") or "").casefold()
            execution = str(args.get("execution") or "").casefold()
            query = str(args.get("query") or "").casefold()
            filtered = []
            for row in rows:
                mapping = dict(row) if isinstance(row, Mapping) else {"text": str(row)}
                encoded = _canonical(mapping).casefold()
                observed_type = str(
                    mapping.get("event_type") or mapping.get("type") or mapping.get("role") or ""
                ).casefold()
                observed_tool = str(mapping.get("tool") or mapping.get("name") or "").casefold()
                observed_validator = str(mapping.get("validator_id") or "").casefold()
                observed_execution = str(mapping.get("execution_id") or "").casefold()
                if event_type and event_type != observed_type:
                    continue
                if tool_name and tool_name != observed_tool:
                    continue
                if validator and not (
                    validator == observed_validator
                    or (validator in {"1", "true", "yes"} and observed_type == "validator")
                ):
                    continue
                if execution and not (
                    execution == observed_execution
                    or (execution in {"1", "true", "yes"} and observed_type == "execution")
                ):
                    continue
                if query and query not in encoded:
                    continue
                filtered.append(mapping)
            view["transcript"] = filtered[:MAX_STEP_ITEMS]
            view["filter"] = {
                "event_type": event_type,
                "tool": tool_name,
                "validator": validator,
                "execution": execution,
                "query": str(args.get("query") or ""),
                "returned": len(view["transcript"]),
            }
        return view

    def _artifact_locator(self, args: Mapping[str, Any]) -> tuple[InspectionRef, Mapping[str, Any], Path]:
        entry, locator = self.registry.resolve(
            str(args.get("ref_id") or ""), kinds=("artifact",)
        )
        if any(key in args for key in ("path", "canonical_path", "resolved_path", "root", "task_id", "revision")):
            raise RecoveryToolError("ARTIFACT_AUTHORITY_ARGUMENT_FORBIDDEN", entry.ref_id)
        if self._artifact_authority_path is not None:
            # Production managed readers recheck the current durable resource
            # authority. Legacy in-memory dispatchers retain registry scope.
            try:
                with open_approved_resource(self._artifact_authority_path) as stream:
                    encoded = stream.read(16 * 1024 * 1024 + 1)
                if len(encoded) > 16 * 1024 * 1024:
                    raise RecoveryToolError("ARTIFACT_AUTHORITY_SIZE_LIMIT", entry.ref_id)
                fresh = json.loads(encoded)
                if not isinstance(fresh, Mapping): raise ValueError()
            except OSError as exc:
                raise RecoveryToolError("ARTIFACT_AUTHORITY_IO_ERROR", entry.ref_id) from exc
            except (ValueError, TypeError) as exc:
                if isinstance(exc, RecoveryToolError): raise
                raise RecoveryToolError("ARTIFACT_AUTHORITY_INVALID", entry.ref_id) from exc
            for key in ("task_id", "plan_id", "plan_version", "current_step_id", "plan_revision"):
                if fresh.get(key) != self.durable.get(key):
                    raise RecoveryToolError("ARTIFACT_AUTHORITY_STALE", key)
            current, expected = fresh.get("current_task") or {}, self.durable.get("current_task") or {}
            for key in ("task_revision", "task_semantic_hash"):
                if current.get(key) != expected.get(key):
                    raise RecoveryToolError("ARTIFACT_AUTHORITY_STALE", key)
        if locator.get("locator_error"):
            raise RecoveryToolError(str(locator["locator_error"]), entry.ref_id)
        raw_path = str(locator.get("path") or "")
        try:
            # Validate again at the read boundary; a link may have appeared
            # since this invocation's manifest was built.
            checked = approved_resource_locator({"canonical_path": raw_path})
        except ResourceLocatorError as exc:
            raise RecoveryToolError(exc.code, entry.ref_id) from exc
        path = Path(checked["path"])
        try:
            if not path.exists():
                raise RecoveryToolError("ARTIFACT_FILE_NOT_FOUND", entry.ref_id)
            if not path.is_file():
                raise RecoveryToolError("ARTIFACT_FORMAT_UNSUPPORTED", entry.ref_id)
        except PermissionError as exc:
            raise RecoveryToolError("ARTIFACT_PERMISSION_DENIED", entry.ref_id) from exc
        return entry, locator, path

    def _artifact_inspect(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, locator, path = self._artifact_locator(args)
        exists = path.exists()
        is_file = path.is_file() if exists else False
        size = path.stat().st_size if is_file else 0
        observed_hash = ""
        hash_status = "not_file"
        if is_file and size <= MAX_ARTIFACT_HASH_BYTES:
            digest = hashlib.sha256()
            with open_approved_resource(path) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            observed_hash = "sha256:" + digest.hexdigest()
            hash_status = "computed"
        elif is_file:
            hash_status = "size_limit"
        binding = dict(locator.get("binding") or {})
        row = dict(binding.get("row") or {})
        return {
            "ref": entry.to_dict(),
            "artifact_id": str(locator.get("artifact_id") or ""),
            "binding": {
                "direction": binding.get("direction"),
                "source_authority": binding.get("source_authority"),
                "producer_step_id": binding.get("producer_step_id"),
            },
            "path": str(path),
            "exists": exists,
            "is_file": is_file,
            "size": size,
            "hash": observed_hash,
            "hash_status": hash_status,
            "declared_hash": str(row.get("sha256") or ""),
            "validation_status": row.get("validation_status"),
            "expected_vs_observed": deepcopy(dict(row.get("expected_vs_observed") or {})),
        }

    def _artifact_read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, _locator, path = self._artifact_locator(args)
        if not path.is_file():
            raise RecoveryToolError("ARTIFACT_CONTENT_UNAVAILABLE", entry.ref_id)
        binary_policy = str(args.get("binary_policy") or "deny").lower()
        if binary_policy != "deny":
            raise RecoveryToolError("ARTIFACT_BINARY_POLICY_UNSUPPORTED", binary_policy)
        try:
            start_line = max(1, int(args.get("start_line") or 1))
            requested_end = int(args.get("end_line") or (start_line + MAX_ARTIFACT_READ_LINES - 1))
            max_bytes = min(
                MAX_ARTIFACT_READ_BYTES,
                max(1, int(args.get("max_bytes") or MAX_ARTIFACT_READ_BYTES)),
            )
            max_chars = min(
                self.max_result_chars,
                max(1, int(args.get("max_chars") or self.max_result_chars)),
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryToolError("RECOVERY_TOOL_ARGUMENT_INVALID", "artifact bounds") from exc
        end_line = min(requested_end, start_line + MAX_ARTIFACT_READ_LINES - 1)
        if end_line < start_line:
            raise RecoveryToolError("RECOVERY_TOOL_ARGUMENT_INVALID", "artifact line range")
        try:
            with open_approved_resource(path) as handle:
                payload = handle.read(max_bytes + 1)
        except PermissionError as exc:
            raise RecoveryToolError("ARTIFACT_PERMISSION_DENIED", entry.ref_id) from exc
        except OSError as exc:
            raise RecoveryToolError("ARTIFACT_IO_ERROR", entry.ref_id) from exc
        if b"\x00" in payload[:4096]:
            raise RecoveryToolError("ARTIFACT_BINARY_READ_FORBIDDEN", entry.ref_id)
        try:
            text = payload[:max_bytes].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecoveryToolError("ARTIFACT_BINARY_READ_FORBIDDEN", entry.ref_id) from exc
        lines = text.splitlines()
        selected = "\n".join(lines[start_line - 1:end_line])[:max_chars]
        return {
            "ref_id": entry.ref_id,
            "start_line": start_line,
            "end_line": start_line + len(lines[start_line - 1:end_line]) - 1,
            "content": selected,
            "truncated": bool(
                len(payload) > max_bytes
                or requested_end > end_line
                or len("\n".join(lines[start_line - 1:end_line])) > max_chars
            ),
            "byte_limit": max_bytes,
            "char_limit": max_chars,
            "line_limit": MAX_ARTIFACT_READ_LINES,
            "binary_policy": binary_policy,
        }

    def _skill_call(self, skill_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        if skill_id not in self._selected_skills:
            raise RecoveryToolError("RECOVERY_SKILL_NOT_SELECTED", skill_id)
        self.registry.resolve("skill:" + skill_id, kinds=("skill_resource",))
        if self.skill_service is None:
            raise RecoveryToolError("RECOVERY_SKILL_SERVICE_UNAVAILABLE", skill_id)
        runtime = {
            "caller_node": "recovery",
            "caller_invocation_id": self.seed.seed_id,
            "task_revision": self.seed.task_spec_ref.task_revision,
            "plan_revision": self.seed.plan_spec_ref.plan_revision,
            "authoritative_user_turn_revision": int(
                self.durable.get("authoritative_user_turn_revision") or 0
            ),
            "current_step_id": self.seed.failure_packet.step_id,
        }
        self._skill_state, result = self.skill_service.serve_context_request(
            self._skill_state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "Recovery requested a selected Skill excerpt.",
                "requests": [dict(request)],
            },
            runtime_envelope=runtime,
        )
        return result

    def _skill_headings(self, args: Mapping[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or "")
        return self._skill_call(skill_id, {"skill_id": skill_id, "operation": "headings"})

    def _skill_read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or "")
        if args.get("heading"):
            request = {"skill_id": skill_id, "operation": "section", "heading": args["heading"]}
        elif args.get("start_line") is not None or args.get("end_line") is not None:
            request = {
                "skill_id": skill_id,
                "operation": "range",
                "start_line": args.get("start_line"),
                "end_line": args.get("end_line"),
            }
        elif args.get("full") is True:
            request = {"skill_id": skill_id, "operation": "full"}
        else:
            raise RecoveryToolError("RECOVERY_TOOL_ARGUMENT_INVALID", "skill.read requires heading or range")
        return self._skill_call(skill_id, request)

    def _skill_search(self, args: Mapping[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or "")
        return self._skill_call(skill_id, {
            "skill_id": skill_id,
            "operation": "search",
            "query": str(args.get("query") or ""),
        })

    def _skill_resource(self, args: Mapping[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or "")
        request = {
            "skill_id": skill_id,
            "operation": "resource",
            "resource_path": str(args.get("resource_path") or ""),
        }
        if args.get("start_line") is not None:
            request["start_line"] = args["start_line"]
        if args.get("end_line") is not None:
            request["end_line"] = args["end_line"]
        return self._skill_call(skill_id, request)

    def _evidence_read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, locator = self.registry.resolve(
            str(args.get("evidence_ref") or ""), kinds=("evidence", "execution", "attempt")
        )
        if self.evidence_reader is None:
            raise RecoveryToolError("RECOVERY_EVIDENCE_SERVICE_UNAVAILABLE", entry.ref_id)
        path = str(locator.get("relative_path") or locator.get("path") or "")
        if not path:
            raise RecoveryToolError("INSPECTION_REF_NOT_RESOLVABLE", entry.ref_id)
        return self.evidence_reader.read(
            path,
            start_line=int(args.get("start_line") or 1),
            end_line=(int(args["end_line"]) if args.get("end_line") is not None else None),
        )

    def _evidence_glob(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, locator = self.registry.resolve(
            str(args.get("scope_ref") or ""), kinds=("evidence_scope",)
        )
        if self.evidence_reader is None:
            raise RecoveryToolError("RECOVERY_EVIDENCE_SERVICE_UNAVAILABLE", entry.ref_id)
        pattern = str(locator.get("pattern") or "")
        if not pattern:
            raise RecoveryToolError("INSPECTION_REF_NOT_RESOLVABLE", entry.ref_id)
        return self.evidence_reader.glob(pattern)


__all__ = [
    "InspectionRef",
    "MAX_TOOL_RESULT_CHARS",
    "RecoveryLocalToolDispatcher",
    "RecoveryToolError",
    "TypedInspectionRegistry",
    "build_recovery_inspection_registry",
]
