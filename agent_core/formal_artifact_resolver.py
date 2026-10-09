"""Resolve formal upstream analysis artifacts before asking or blocking.

The resolver is intentionally read-only and evidence-oriented. It should not
encode benchmark cases as control flow. Domain-specific artifacts are declared
as file relationship patterns, while biological intent remains Consultant and
skill responsibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agent_core.task_spec import extract_path_tokens


ALWAYS_FORBIDDEN_PATH_TOKENS = (
    ".answers",
    "answer_key",
    "expected_answers",
    "gold",
    "hidden",
    "solution",
)

CONDITIONALLY_FORBIDDEN_PATH_TOKENS = (
    "fixtures_legacy_toy",
    "toy",
    "tests",
    "demo",
)

ALLOWED_FORMAL_FIXTURE_TOKENS = (
    "fixtures_public",
    "fixtures_simulated_truth",
)


@dataclass(frozen=True)
class ArtifactSpec:
    kind: str
    role: str
    label: str
    filename_suffixes: Tuple[str, ...]
    search_globs: Tuple[str, ...]
    query_tokens: Tuple[str, ...]
    task_id_tokens: Tuple[str, ...]
    preferred_task_token: str
    directory_tokens: Tuple[str, ...]


ARTIFACT_SPECS: Tuple[ArtifactSpec, ...] = (
    ArtifactSpec(
        kind="tad_boundaries",
        role="region_boundary_bed",
        label="Region boundary BED",
        filename_suffixes=("_boundaries.bed",),
        search_globs=("*_boundaries.bed",),
        query_tokens=(
            "tad boundary",
            "tad boundaries",
            "_boundaries.bed",
            "tad边界",
            "tad 边界",
            "拓扑结构域边界",
        ),
        task_id_tokens=("-BOUNDARY-",),
        preferred_task_token="-TAD-",
        directory_tokens=("TADs/HiCExplorer", "TADs\\HiCExplorer"),
    ),
    ArtifactSpec(
        kind="tad_domains",
        role="region_domain_bed",
        label="Region domain BED",
        filename_suffixes=("_domains.bed",),
        search_globs=("*_domains.bed",),
        query_tokens=(
            "differential tad",
            "differential-tad",
            "difftad",
            "hicdifferentialtad",
            "tad domains",
            "tad domain",
            "_domains.bed",
            "差异 tad",
            "差异tad",
            "domains",
        ),
        task_id_tokens=("-DIFFTAD-",),
        preferred_task_token="-TAD-",
        directory_tokens=("TADs/HiCExplorer", "TADs\\HiCExplorer"),
    ),
)


RESOLUTION_POLICY_VERSION = 2


def formal_input_resolution_cache_key(
    query: str,
    work_dir: str | Path,
    task_id: str = "",
    project_root: str | Path | None = None,
    candidate_search_roles: Sequence[str] = (),
    required_input_roles: Sequence[str] = (),
    known_inputs: Sequence[Dict[str, Any]] = (),
    *,
    known_outputs: Sequence[Dict[str, Any]] = (),
    search_directories: Sequence[str | Path] = (),
    associated_history_directories: Sequence[str | Path] = (),
) -> str:
    """Bind cached discovery to accepted facts, never a former candidate choice.

    This is a request identity, not fresh filesystem verification. Callers must
    not reuse a legacy result lacking this version and matching fingerprint.
    """
    payload = {
        "policy_version": RESOLUTION_POLICY_VERSION,
        "query": str(query or ""),
        "work_dir": str(Path(work_dir or ".").expanduser().resolve()),
        "project_root": str(Path(project_root).expanduser().resolve()) if project_root else "",
        "task_id": str(task_id or ""),
        "candidate_search_roles": list(candidate_search_roles),
        "required_input_roles": list(required_input_roles),
        "known_inputs": list(known_inputs),
        "known_outputs": list(known_outputs),
        "search_directories": [str(path) for path in search_directories],
        "associated_history_directories": [str(path) for path in associated_history_directories],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    default=str, separators=(",", ":")).encode()).hexdigest()


def resolve_formal_upstream_artifacts(
    query: str,
    work_dir: str | Path,
    task_id: str = "",
    project_root: str | Path | None = None,
    max_candidates: int = 24,
    candidate_search_roles: Sequence[str] = (),
    required_input_roles: Sequence[str] = (),
    known_inputs: Sequence[Dict[str, Any]] = (),
    *,
    known_outputs: Sequence[Dict[str, Any]] = (),
    search_directories: Sequence[str | Path] = (),
    associated_history_directories: Sequence[str | Path] = (),
) -> Dict[str, Any]:
    """Verify accepted inputs and discover candidates for explicit input roles.

    Literal paths with no accepted direction remain observations. A requested
    output, a domain keyword or a benchmark identifier never establishes an
    upstream need. Candidate rank and uniqueness never establish selection.
    """
    query_text = str(query or "")
    work_path = Path(work_dir or ".").expanduser().resolve()
    project_path = Path(project_root).expanduser().resolve() if project_root else None
    requested_roles = _dedupe_strings([*candidate_search_roles, *required_input_roles])
    accepted_roles = [str(row.get("role") or "") for row in known_inputs
                      if isinstance(row, dict) and _is_accepted_input_record(row)]
    specs = [spec for spec in ARTIFACT_SPECS if spec.role in {*requested_roles, *accepted_roles}]
    spec = specs[0] if specs else None
    generic = _discover_local_inputs(
        query_text, work_path, project_path, max_candidates=max_candidates,
        candidate_search_roles=candidate_search_roles, required_input_roles=required_input_roles,
        known_inputs=known_inputs, known_outputs=known_outputs,
        search_directories=search_directories,
        associated_history_directories=associated_history_directories,
    )
    result: Dict[str, Any] = {
        **generic,
        "resolution_policy_version": RESOLUTION_POLICY_VERSION,
        "request_fingerprint": formal_input_resolution_cache_key(
            query, work_dir, task_id, project_root, candidate_search_roles,
            required_input_roles, known_inputs, known_outputs=known_outputs,
            search_directories=search_directories,
            associated_history_directories=associated_history_directories,
        ),
        "relevant": bool(generic.get("relevant") or requested_roles),
        "artifact_kind": spec.kind if spec else "",
        "artifact_label": spec.label if spec else "",
        "artifact_role": spec.role if spec else "",
        "artifact_roles": [item.role for item in specs],
        "status": "not_applicable",
        "task_id": task_id,
        "benchmark_id": _extract_benchmark_id(query_text) or task_id or "",
        "ambiguity_level": "",
        "samples": _dedupe_strings(str(row.get("sample") or "") for row in known_inputs if isinstance(row, dict)),
        "resolutions": _extract_resolution_labels(query_text),
        "formal_candidates": [],
        "selected_by_sample": {},
        "ambiguous_samples": [],
        "ambiguous_resolutions": [],
        "unresolved": False,
        "unresolved_reasons": [],
        "consultant_action_hint": "",
    }
    if not result["relevant"]:
        return result

    accepted_paths_by_role: Dict[str, List[Dict[str, Any]]] = {}
    for row in result["resolved_inputs"]:
        accepted_paths_by_role.setdefault(str(row.get("role") or ""), []).append(row)
    blocked_roles = set(result.pop("invalid_declared_roles", []))
    roots = [Path(value) for value in result["search_roots"]]
    output_paths = _declared_output_paths(known_outputs, work_path, project_path)
    for artifact_spec in specs:
        accepted = accepted_paths_by_role.get(artifact_spec.role, [])
        for row in accepted:
            path = Path(str(row.get("path") or ""))
            if not _explicit_path_matches_spec(path, artifact_spec):
                continue
            candidate, reason = _candidate_record(path, artifact_spec, (), (), "", explicit=True)
            if candidate:
                candidate.update(binding_status="accepted_input", source=row.get("source") or "known_input")
                sample = str(row.get("sample") or candidate.get("sample") or "")
                if sample:
                    result["selected_by_sample"][sample] = candidate
        # A named path, valid or invalid, is authoritative. Do not replace it
        # with another task's similarly named artifact after validation fails.
        if accepted or artifact_spec.role in blocked_roles or artifact_spec.role not in requested_roles:
            continue
        found: List[Dict[str, Any]] = []
        for root in roots:
            for glob in artifact_spec.search_globs:
                try:
                    paths = sorted(root.rglob(glob))
                except OSError:
                    continue
                for path in paths:
                    if not path.resolve().is_relative_to(root.resolve()) or _within_declared_outputs(path, output_paths):
                        continue
                    candidate, reason = _candidate_record(path, artifact_spec, (), (), "")
                    if reason:
                        if len(result["excluded_candidates"]) < max_candidates:
                            result["excluded_candidates"].append({"path": str(path.resolve()), "reason": reason})
                    elif candidate:
                        candidate.update(binding_status="candidate_only", source="scoped_candidate_search")
                        found.append(candidate)
        found = _dedupe_candidates(found)
        found.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))
        if found:
            result["candidate_groups"][artifact_spec.role] = found[:max_candidates]
            result["formal_candidates"].extend(found[:max_candidates])
            if artifact_spec.role in result["missing_roles"]:
                result["missing_roles"].remove(artifact_spec.role)
        elif artifact_spec.role in required_input_roles:
            result["missing_roles"].append(artifact_spec.role)

    result["formal_candidates"] = _dedupe_candidates(result["formal_candidates"])[:max_candidates]
    result["missing_roles"] = _dedupe_strings(result["missing_roles"])
    unresolved_roles = [role for role in requested_roles if not accepted_paths_by_role.get(role)]
    selection_roles = [role for role in unresolved_roles if result["candidate_groups"].get(role)]
    result["missing_roles"] = _dedupe_strings([
        *result["missing_roles"],
        *(role for role in required_input_roles if not accepted_paths_by_role.get(role)
          and not result["candidate_groups"].get(role)),
    ])
    result["selection_required_roles"] = selection_roles
    if result["invalid_explicit_paths"] or result["missing_roles"]:
        result.update(status="missing_inputs", unresolved=True,
                      unresolved_reasons=["explicit input verification or required input discovery remains unresolved"],
                      consultant_action_hint="report_exact_missing_paths_or_unverified_inputs")
    elif selection_roles or result["ambiguous_roles"]:
        result.update(status="candidates_available", unresolved=True,
                      unresolved_reasons=["candidate evidence does not establish input selection"],
                      consultant_action_hint="confirm_input_selection_or_handoff_with_pending_verification")
    elif result["resolved_inputs"]:
        result["status"] = "resolved"
    elif result["observed_artifacts"]:
        result["status"] = "observed_paths"
    elif requested_roles:
        result.update(status="missing_candidates", unresolved=True,
                      unresolved_reasons=["no candidates in the permitted search scope"],
                      consultant_action_hint="handoff_with_pending_input_or_request_required_decision")
    return result


def format_formal_upstream_context(result: Dict[str, Any]) -> str:
    """Format resolver output for Consultant/Planner prompt injection."""

    if not result or not result.get("relevant"):
        return ""

    lines = [
        "[INPUT_RESOLUTION_AUDIT]",
        "policy:verify accepted paths first; search only explicit input roles within declared scope; candidates are not selected inputs",
        f"artifact_kind:{result.get('artifact_kind') or 'unknown'}",
        f"artifact_label:{result.get('artifact_label') or 'unknown'}",
        f"artifact_role:{result.get('artifact_role') or 'unknown'}",
        f"status:{result.get('status') or 'unknown'}",
        f"task_id:{result.get('task_id') or 'unknown'}",
        f"benchmark_id:{result.get('benchmark_id') or 'unknown'}",
        f"ambiguity_level:{result.get('ambiguity_level') or 'unknown'}",
        "samples:" + (", ".join(result.get("samples") or []) or "unknown"),
        "resolutions:" + (", ".join(result.get("resolutions") or []) or "unknown"),
        f"unresolved:{bool(result.get('unresolved'))}",
        "unresolved_reasons:" + (", ".join(result.get("unresolved_reasons") or []) or "none"),
        f"consultant_action_hint:{result.get('consultant_action_hint') or 'none'}",
        "decision:" + _decision_text(result),
    ]
    if result.get("verified_explicit_paths"):
        lines.append("verified_explicit_paths:")
        lines.extend(f"- {path}" for path in result.get("verified_explicit_paths") or [])
    if result.get("invalid_explicit_paths"):
        lines.append("invalid_explicit_paths:")
        lines.extend(f"- {path}" for path in result.get("invalid_explicit_paths") or [])
    if result.get("ambiguous_samples"):
        lines.append("ambiguous_samples:" + ", ".join(result.get("ambiguous_samples") or []))
    if result.get("ambiguous_resolutions"):
        lines.append("ambiguous_resolutions:" + ", ".join(result.get("ambiguous_resolutions") or []))
    if result.get("resolved_inputs"):
        lines.append("resolved_inputs:")
        for item in (result.get("resolved_inputs") or [])[:16]:
            lines.append(
                f"- role={item.get('role') or 'unknown'}; source={item.get('source') or 'unknown'}; "
                f"path={item.get('path') or item.get('paths') or 'unknown'}"
                + (f"; detail={item.get('detail')}" if item.get("detail") else "")
            )
    if result.get("observed_artifacts"):
        lines.append("observed_paths (not selected inputs):")
        for item in (result.get("observed_artifacts") or [])[:16]:
            if item.get("binding_status") == "accepted_input":
                continue
            lines.append(f"- direction={item.get('direction') or 'unknown'}; exists={item.get('exists')}; path={item.get('path')}")
    if result.get("ambiguous_roles"):
        lines.append("ambiguous_roles:" + ", ".join(result.get("ambiguous_roles") or []))
    if result.get("missing_roles"):
        lines.append("missing_roles:" + ", ".join(result.get("missing_roles") or []))
    if result.get("discovery_log"):
        lines.append("discovery_log:")
        lines.extend(f"- {entry}" for entry in (result.get("discovery_log") or [])[:12])
    lines.append("[/INPUT_RESOLUTION_AUDIT]")

    lines.append("[FORMAL_UPSTREAM_CANDIDATES]")
    selected = result.get("selected_by_sample") or {}
    if selected:
        lines.append("selected_by_sample:")
        for sample in sorted(selected):
            item = selected[sample]
            lines.append(
                f"- {sample}: {item.get('path')} "
                f"(task_id={item.get('task_id') or 'unknown'}; "
                f"resolution={item.get('resolution') or 'unknown'}; score={item.get('score')})"
            )
    candidates = result.get("formal_candidates") or []
    if candidates:
        lines.append("formal_candidates:")
        for item in candidates[:12]:
            lines.append(
                f"- sample={item.get('sample') or 'unknown'}; "
                f"resolution={item.get('resolution') or 'unknown'}; "
                f"task_id={item.get('task_id') or 'unknown'}; "
                f"kind={item.get('artifact_kind') or 'unknown'}; "
                f"role={item.get('artifact_role') or 'unknown'}; "
                f"path={item.get('path')}"
            )
    else:
        lines.append("- none")
    excluded = result.get("excluded_candidates") or []
    if excluded:
        lines.append("excluded_candidates:")
        for item in excluded[:8]:
            lines.append(f"- {item.get('path')}: {item.get('reason')}")
    lines.append("[/FORMAL_UPSTREAM_CANDIDATES]")

    return "\n".join(lines)


def _is_accepted_input_record(row: Dict[str, Any]) -> bool:
    """Distinguish an accepted fact from an input-shaped discovery cache row."""
    if str(row.get("direction") or "").lower() == "output":
        return False
    sources = {
        str(row.get("source") or "").lower(),
        str(row.get("source_type") or "").lower(),
        *(str(item.get("source_type") or item.get("source") or "").lower()
          for item in row.get("provenance") or [] if isinstance(item, dict)),
    }
    if sources.intersection({
        "explicit", "user", "user_initial", "user_feedback", "user_explicit",
        "consultant_confirmed", "consultant_selected",
    }):
        return True
    if sources.intersection({
        "candidate", "formal_candidate", "discovery", "local_discovery",
        "local_candidate_search", "scoped_candidate_search", "directory_discovery",
        "formal_artifact_resolver",
    }):
        return False
    if str(row.get("binding_status") or "").lower() in {
        "candidate", "candidate_only", "observed_only", "unselected",
    }:
        return False
    # A previous result's status/score cannot serve as selection evidence.
    if str(row.get("status") or "").lower() in {"resolved", "selected"} and row.get("direction") != "input":
        return False
    if "score" in row and "artifact_kind" in row:
        return False
    # Plain known_inputs and canonical direction=input rows are facts accepted
    # by the caller; no new requirement for a second binding protocol is added.
    return True


def _record_paths(row: Dict[str, Any]) -> List[str]:
    path = row.get("canonical_path") or row.get("resolved_path") or row.get("path") or ""
    paths = row.get("paths") or []
    return _dedupe_strings([str(path)] if path else [str(value) for value in paths if value])


def _declared_output_paths(
    rows: Sequence[Dict[str, Any]], work_dir: Path, project_root: Optional[Path],
) -> List[Path]:
    return _dedupe_paths(path.resolve() for row in rows if isinstance(row, dict)
                         for raw in _record_paths(row)
                         for path in _expand_path_token(raw, work_dir, project_root))


def _within_declared_outputs(path: Path, outputs: Sequence[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == output or resolved.is_relative_to(output) for output in outputs)


def _path_observation(path: Path, *, direction: str, source: str) -> Dict[str, Any]:
    """Existence/shape observation; an unclassified path is not an input read."""
    return {
        "path": str(path.resolve()), "exists": path.exists(),
        "is_file": _is_file_safe(path), "is_directory": _is_dir_safe(path),
        "direction": direction, "source": source,
        "binding_status": "output_conflict_check" if direction == "output" else "observed_only",
    }


def _discover_local_inputs(
    text: str,
    work_dir: Path,
    project_root: Optional[Path],
    max_candidates: int = 24,
    candidate_search_roles: Sequence[str] = (),
    required_input_roles: Sequence[str] = (),
    known_inputs: Sequence[Dict[str, Any]] = (),
    *,
    known_outputs: Sequence[Dict[str, Any]] = (),
    search_directories: Sequence[str | Path] = (),
    associated_history_directories: Sequence[str | Path] = (),
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "relevant": False, "resolved_inputs": [], "observed_artifacts": [],
        "candidate_groups": {}, "missing_roles": [], "ambiguous_roles": [],
        "discovery_log": [], "verified_explicit_paths": [], "invalid_explicit_paths": [],
        "invalid_declared_roles": [], "excluded_candidates": [], "search_roots": [],
    }
    requested = _dedupe_strings([*candidate_search_roles, *required_input_roles])
    explicit_dirs: List[Path] = []
    accepted_files: List[Path] = []
    classified_paths: set[str] = set()
    for direction, records in (("output", known_outputs), ("input", known_inputs)):
        for row in records:
            if not isinstance(row, dict):
                continue
            for raw in _record_paths(row):
                for path in _expand_path_token(raw, work_dir, project_root):
                    resolved = str(path.resolve())
                    # A contradictory input/output direction is not silently
                    # converted into a confirmed input by file existence.
                    if resolved in classified_paths:
                        continue
                    classified_paths.add(resolved)
                    reason = _forbidden_path_reason(path, explicit=True)
                    if reason:
                        result["excluded_candidates"].append({"path": resolved, "reason": reason})
                        result["discovery_log"].append("excluded explicit path: " + resolved + ": " + reason)
                        if direction == "input":
                            result["invalid_explicit_paths"].append(resolved)
                            result["invalid_declared_roles"].append(str(row.get("role") or "input_file"))
                        continue
                    if direction == "output":
                        result["observed_artifacts"].append(_path_observation(path, direction=direction, source="known_output"))
                        continue
                    role = str(row.get("role") or ("input_directory" if _is_dir_safe(path) else _role_for_path(path)))
                    # Old discovery evidence is not accepted merely because a
                    # caller retained it in a prior input-shaped collection.
                    if not _is_accepted_input_record(row):
                        result["observed_artifacts"].append(_path_observation(path, direction="unknown", source="prior_candidate"))
                        continue
                    record = _input_record(role, path, "explicit", binding_status="accepted_input")
                    artifact_spec = _select_artifact_spec(input_roles=[role])
                    if artifact_spec and not _explicit_path_matches_spec(path, artifact_spec):
                        record["verified"] = False
                        record["verification_error"] = "declared artifact role does not match file suffix"
                    if row.get("sample"):
                        record["sample"] = row["sample"]
                    result["observed_artifacts"].append({**record, "direction": "input", "evidence_status": "checked"})
                    if record["verified"]:
                        result["resolved_inputs"].append(record)
                        result["verified_explicit_paths"].append(resolved)
                        if record["is_file"]:
                            accepted_files.append(path.resolve())
                        else:
                            explicit_dirs.append(path.resolve())
                    else:
                        result["invalid_explicit_paths"].append(resolved)
                        result["invalid_declared_roles"].append(role)
                        result["missing_roles"].append(role)
                        result["discovery_log"].append("accepted input path failed verification: " + resolved)

    # Query paths retain their exact direction-unknown identity until Consultant
    # accepts an input fact. Known output paths are observations even if they end
    # in the same suffix as a requested upstream artifact.
    for raw in _raw_path_tokens(text):
        for path in _expand_path_token(raw, work_dir, project_root):
            resolved = str(path.resolve())
            if resolved in classified_paths:
                continue
            classified_paths.add(resolved)
            reason = _forbidden_path_reason(path, explicit=True)
            if reason:
                result["excluded_candidates"].append({"path": resolved, "reason": reason})
                continue
            result["observed_artifacts"].append(_path_observation(path, direction="unknown", source="user_path_mention"))
            if _is_dir_safe(path):
                explicit_dirs.append(path.resolve())

    roots = _candidate_search_roots(
        work_dir, project_root, explicit_directories=explicit_dirs,
        search_directories=search_directories,
        associated_history_directories=associated_history_directories,
    ) if requested else []
    result["search_roots"] = [str(path) for path in roots]
    # Companion identity derives from an accepted file relationship, never from
    # a directory containing a single matching group.
    _discover_paired_fastqs(result, accepted_files, (), max_candidates=max_candidates)
    _discover_sidecars(result, accepted_files)
    if "paired_fastq" in requested:
        _discover_paired_fastqs(result, (), roots, max_candidates=max_candidates)
    if _should_discover_reference_annotation(requested):
        blocked = set(result["invalid_declared_roles"])
        _discover_reference_annotation_pairs(
            result, accepted_files, roots, work_dir, project_root, max_candidates,
            candidate_search_roles=[role for role in requested if role not in blocked],
            required_input_roles=required_input_roles,
        )
    output_paths = _declared_output_paths(known_outputs, work_dir, project_root)
    result["resolved_inputs"] = _dedupe_resolved_inputs([
        row for row in result["resolved_inputs"]
        if not any(_within_declared_outputs(Path(raw), output_paths) for raw in _record_paths(row))
    ])
    for role, rows in list(result["candidate_groups"].items()):
        result["candidate_groups"][role] = [
            row for row in rows if not any(_within_declared_outputs(Path(raw), output_paths)
                                           for raw in _record_paths(row) + [str(row.get(key) or "") for key in ("r1", "r2") if row.get(key)])
        ]
    result["missing_roles"] = _dedupe_strings(result["missing_roles"])
    result["ambiguous_roles"] = _dedupe_strings(result["ambiguous_roles"])
    result["relevant"] = bool(result["observed_artifacts"] or result["excluded_candidates"] or requested)
    return result


def _dedupe_resolved_inputs(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge explicit and discovery evidence for the same role/path."""
    def as_list(value: Any) -> List[Any]:
        if value in (None, ""):
            return []
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    merged: Dict[tuple[str, str], Dict[str, Any]] = {}
    order: List[tuple[str, str]] = []
    for raw in records:
        item = dict(raw or {})
        path_value = item.get("path") or item.get("resolved_path") or item.get("paths") or ""
        if isinstance(path_value, (list, tuple)):
            normalized_path = "|".join(str(value) for value in path_value)
        else:
            normalized_path = str(path_value)
        key = (str(item.get("role") or "unknown"), normalized_path)
        if key not in merged:
            merged[key] = item
            order.append(key)
            continue
        current = merged[key]
        source_values = as_list(current.get("sources") or current.get("source"))
        source_values.append(item.get("source"))
        sources = [str(value) for value in source_values if str(value or "").strip()]
        current["sources"] = list(dict.fromkeys(sources))
        if str(item.get("source") or "") == "explicit":
            current["source"] = "explicit"
        for field in ("detail", "verified_at"):
            if item.get(field) and not current.get(field):
                current[field] = item[field]
        for field in ("exists", "is_file", "is_directory", "non_empty", "readable", "verified"):
            if item.get(field) is True:
                current[field] = True
        evidence = [
            *as_list(current.get("verification_evidence")),
            *as_list(item.get("verification_evidence")),
        ]
        if evidence:
            current["verification_evidence"] = list(dict.fromkeys(evidence))
    return [merged[key] for key in order]


def _input_record(role: str, path: Path, source: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
    resolved = path.resolve()
    exists = resolved.exists()
    is_file = exists and resolved.is_file()
    is_directory = exists and resolved.is_dir()
    readable = exists and os.access(resolved, os.R_OK)
    stat_identity: Dict[str, Any] = {}
    try:
        stat = resolved.stat()
        stat_identity = {
            "kind": "file" if is_file else "directory" if is_directory else "other",
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "mode": int(stat.st_mode),
        }
        # A bounded sample catches same-size rewrites without reading a large
        # genomics artifact in full.  The fingerprint (not wall-clock fields)
        # is what the immutable finalize context binds to.
        content_sample_hash = ""
        if is_file and readable:
            sample_hasher = hashlib.sha256()
            with resolved.open("rb") as handle:
                head = handle.read(65536)
                sample_hasher.update(head)
                if stat.st_size > 65536:
                    handle.seek(max(0, stat.st_size - 65536))
                    sample_hasher.update(handle.read(65536))
            content_sample_hash = sample_hasher.hexdigest()
        stat_identity["content_sample_sha256"] = content_sample_hash
    except OSError:
        stat_identity = {}
    try:
        non_empty = bool(
            (is_file and resolved.stat().st_size > 0)
            or (is_directory and next(resolved.iterdir(), None) is not None)
        )
    except OSError:
        non_empty = False
    verified = bool(exists and readable and (non_empty if is_file else is_directory))
    evidence = [
        label for label, present in (
            ("exists", exists),
            ("is_file", is_file),
            ("is_directory", is_directory),
            ("non_empty", non_empty),
            ("readable", readable),
        ) if present
    ]
    record: Dict[str, Any] = {
        "role": role,
        "path": str(resolved),
        "resolved_path": str(resolved),
        "source": source,
        "exists": exists,
        "is_file": is_file,
        "is_directory": is_directory,
        "non_empty": non_empty,
        "readable": readable,
        "verified": verified,
        "verified_at": datetime.now().isoformat(timespec="seconds"),
        "verification_evidence": evidence,
        "evidence_fingerprint": (
            hashlib.sha256(
                json.dumps(
                    stat_identity,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if stat_identity
            else ""
        ),
    }
    if detail:
        record["detail"] = detail
    record.update(extra)
    return record


def _role_for_path(path: Path) -> str:
    suffix = _compound_suffix(path)
    if suffix in {".fq", ".fastq", ".fq.gz", ".fastq.gz"}:
        return "fastq"
    if suffix in {".fa", ".fasta", ".fna"}:
        return "reference_fasta"
    if suffix == ".gtf":
        return "annotation_file"
    if suffix in {".gff", ".gff3"}:
        return "annotation_file"
    if suffix == ".bam":
        return "alignment_bam"
    if suffix in {".vcf", ".vcf.gz", ".bcf"}:
        return "variant_file"
    if suffix == ".bed":
        return "bed_file"
    return "input_file"


def _compound_suffix(path: Path) -> str:
    name = path.name.lower()
    for suffix in (
        ".fastq.gz",
        ".fq.gz",
        ".vcf.gz",
        ".fasta",
        ".fastq",
        ".gff3",
        ".fna",
        ".gtf",
        ".gff",
        ".bam",
        ".vcf",
        ".bcf",
        ".bed",
        ".fa",
        ".fq",
    ):
        if name.endswith(suffix):
            return suffix
    return path.suffix.lower()


def _looks_like_relevant_input_path(raw: str) -> bool:
    lower = str(raw or "").lower()
    return any(
        token in lower
        for token in (
            ".fq",
            ".fastq",
            ".fa",
            ".fasta",
            ".fna",
            ".gtf",
            ".gff",
            ".gff3",
            ".bam",
            ".vcf",
            ".bcf",
            ".bed",
            ".cool",
            ".mcool",
            ".rds",
            ".h5ad",
        )
    )


def _discover_paired_fastqs(
    result: Dict[str, Any],
    explicit_files: Sequence[Path],
    explicit_dirs: Sequence[Path],
    max_candidates: int,
) -> None:
    explicit_fastqs = [path for path in explicit_files if _role_for_path(path) == "fastq"]
    paired_groups: List[Dict[str, Any]] = []
    for path in explicit_fastqs:
        mate = _fastq_mate_path(path)
        if mate and _is_valid_formal_file(mate) and not _forbidden_path_reason(mate, explicit=True):
            group = _paired_fastq_group(path, mate, source="sibling_discovery")
            paired_groups.append(group)
            result["resolved_inputs"].append(
                {
                    "role": "paired_fastq",
                    "paths": [group["r1"], group["r2"]],
                    "sample": group["sample"],
                    "source": "sibling_discovery",
                }
            )
            result["discovery_log"].append(f"paired FASTQ mate found for {path.name}: {mate.name}")
        elif mate:
            result["missing_roles"].append("fastq_mate")
            result["discovery_log"].append(f"paired FASTQ mate not found for {path.name}: expected {mate.name}")

    if not explicit_fastqs:
        for directory in explicit_dirs[:8]:
            paired_groups.extend(_scan_fastq_pairs(directory, max_candidates=max_candidates))

    paired_groups = _dedupe_group_records(paired_groups, key_fields=("r1", "r2"))
    if paired_groups:
        result["candidate_groups"]["paired_fastq"] = paired_groups[:max_candidates]
        if not explicit_fastqs and len(paired_groups) > 1:
            result["ambiguous_roles"].append("paired_fastq")
            result["discovery_log"].append(
                f"multiple paired FASTQ groups found: {', '.join(group['sample'] for group in paired_groups[:6])}"
            )


def _fastq_mate_path(path: Path) -> Optional[Path]:
    name = path.name
    patterns = (
        (r"^(?P<prefix>.+?)(?P<sep>[_\-.])R1(?P<tail>.*\.(?:f(?:ast)?q)(?:\.gz)?)$", "R2"),
        (r"^(?P<prefix>.+?)(?P<sep>[_\-.])1(?P<tail>\.(?:f(?:ast)?q)(?:\.gz)?)$", "2"),
        (r"^(?P<prefix>.+?)R1(?P<tail>\.(?:f(?:ast)?q)(?:\.gz)?)$", "R2"),
    )
    for pattern, mate_token in patterns:
        match = re.match(pattern, name, re.IGNORECASE)
        if not match:
            continue
        if "sep" in match.groupdict():
            mate_name = f"{match.group('prefix')}{match.group('sep')}{mate_token}{match.group('tail')}"
        else:
            mate_name = f"{match.group('prefix')}{mate_token}{match.group('tail')}"
        if mate_name != name:
            return path.with_name(mate_name)
    return None


def _paired_fastq_group(r1: Path, r2: Path, source: str) -> Dict[str, Any]:
    sample = _fastq_sample_name(r1)
    return {
        "role": "paired_fastq",
        "sample": sample,
        "r1": str(r1.resolve()),
        "r2": str(r2.resolve()),
        "source": source,
    }


def _fastq_sample_name(path: Path) -> str:
    name = path.name
    for pattern in (
        r"(?P<sample>.+?)(?:[_\-.])R[12].*\.(?:f(?:ast)?q)(?:\.gz)?$",
        r"(?P<sample>.+?)(?:[_\-.])[12]\.(?:f(?:ast)?q)(?:\.gz)?$",
        r"(?P<sample>.+?)R[12]\.(?:f(?:ast)?q)(?:\.gz)?$",
    ):
        match = re.match(pattern, name, re.IGNORECASE)
        if match:
            return match.group("sample").strip("._-")
    return path.stem


def _scan_fastq_pairs(directory: Path, max_candidates: int) -> List[Dict[str, Any]]:
    if not _is_dir_safe(directory):
        return []
    groups: List[Dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if not _is_file_safe(path) or _role_for_path(path) != "fastq":
            continue
        mate = _fastq_mate_path(path)
        if not mate or not _is_valid_formal_file(mate) or _forbidden_path_reason(mate, explicit=True):
            continue
        if path.name > mate.name:
            continue
        groups.append(_paired_fastq_group(path, mate, source="directory_discovery"))
        if len(groups) >= max_candidates:
            break
    return groups


def _discover_sidecars(result: Dict[str, Any], explicit_files: Sequence[Path]) -> None:
    for path in explicit_files:
        sidecars = _sidecar_paths(path)
        found = [
            sidecar for sidecar in sidecars if _is_valid_formal_file(sidecar) and not _forbidden_path_reason(sidecar, explicit=True)
        ]
        for sidecar in found:
            result["resolved_inputs"].append(_input_record(_role_for_path(sidecar), sidecar, "sidecar_discovery", detail=f"sidecar for {path.name}"))
        if sidecars:
            result["candidate_groups"].setdefault("sidecar_files", []).extend(
                _input_record(_role_for_path(sidecar), sidecar, "sidecar_discovery", detail=f"sidecar for {path.name}")
                for sidecar in found
            )


def _sidecar_paths(path: Path) -> List[Path]:
    suffix = _compound_suffix(path)
    if suffix in {".fa", ".fasta", ".fna"}:
        return [Path(str(path) + ".fai"), path.with_suffix(".dict")]
    if suffix == ".bam":
        return [Path(str(path) + ".bai"), path.with_suffix(".bai")]
    if suffix in {".vcf", ".vcf.gz"}:
        return [Path(str(path) + ".tbi"), Path(str(path) + ".csi")]
    return []


def _discover_reference_annotation_pairs(
    result: Dict[str, Any],
    explicit_files: Sequence[Path],
    explicit_dirs: Sequence[Path],
    work_dir: Path,
    project_root: Optional[Path],
    max_candidates: int,
    *,
    candidate_search_roles: Sequence[str],
    required_input_roles: Sequence[str] = (),
) -> None:
    requested = set(_dedupe_strings(candidate_search_roles))
    required = set(_dedupe_strings(required_input_roles))
    roots = _local_input_search_roots(explicit_dirs, work_dir, project_root)
    references = _dedupe_paths([path for path in explicit_files if _role_for_path(path) == "reference_fasta"])
    annotations = _dedupe_paths([path for path in explicit_files if _role_for_path(path) == "annotation_file"])
    if "reference_fasta" in requested and not references:
        references = _find_files_by_suffixes(roots, (".fa", ".fasta", ".fna"), max_candidates=max_candidates)
    if "annotation_file" in requested and not annotations:
        annotations = _find_files_by_suffixes(roots, (".gtf", ".gff", ".gff3"), max_candidates=max_candidates)

    reference_records = [
        _input_record("reference_fasta", path, "local_candidate_search")
        for path in references[:max_candidates]
        if "reference_fasta" in requested
        and path not in explicit_files
    ]
    annotation_records: List[Dict[str, Any]] = []
    for annotation in annotations[:max_candidates]:
        if "annotation_file" not in requested or annotation in explicit_files:
            continue
        record = _input_record(
            _role_for_path(annotation), annotation, "local_candidate_search"
        )
        if references:
            compatibility = _annotation_compatibility(references[0], annotation)
            record["compatibility"] = compatibility["status"]
            if compatibility.get("detail"):
                record["detail"] = compatibility["detail"]
        annotation_records.append(record)

    if reference_records:
        result["candidate_groups"]["reference_fasta"] = reference_records
        if len(reference_records) > 1 and "reference_fasta" in required:
            result["ambiguous_roles"].append("reference_fasta")
    elif "reference_fasta" in required and not references:
        result["missing_roles"].append("reference_fasta")
    if annotation_records:
        result["candidate_groups"]["annotation_file"] = annotation_records
        compatible = [item for item in annotation_records if item.get("compatibility") in ("compatible", "unchecked")]
        if len(compatible) > 1 and "annotation_file" in required:
            result["ambiguous_roles"].append("annotation_file")
        elif not compatible and "annotation_file" in required:
            result["ambiguous_roles"].append("annotation_file")
            result["discovery_log"].append("annotation candidates found but none passed lightweight compatibility")
    elif "annotation_file" in required and not annotations:
        result["missing_roles"].append("annotation_file")


def _should_discover_reference_annotation(
    requested_roles: Sequence[str],
) -> bool:
    """Only a typed Consultant/TaskSpec request may expand local companions."""
    return bool(
        {"reference_fasta", "annotation_file"}.intersection(
            _dedupe_strings(requested_roles)
        )
    )


def _local_input_search_roots(
    explicit_dirs: Sequence[Path],
    work_dir: Path,
    project_root: Optional[Path],
) -> List[Path]:
    # The caller has already established scope. Do not widen it to parents,
    # sibling projects, benchmark runs or project_root.
    return _dedupe_paths(path for path in explicit_dirs if _is_dir_safe(path))


def _find_files_by_suffixes(roots: Sequence[Path], suffixes: Sequence[str], max_candidates: int) -> List[Path]:
    found: List[Path] = []
    for root in roots:
        if not _is_dir_safe(root):
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for path in sorted(entries):
            if len(found) >= max_candidates:
                return _dedupe_paths(found)
            if not _is_file_safe(path):
                continue
            if not path.resolve().is_relative_to(root.resolve()) or _forbidden_path_reason(path, explicit=False):
                continue
            if path.name.lower().endswith(tuple(suffixes)) and _is_valid_formal_file(path):
                found.append(path.resolve())
    return _dedupe_paths(found)


def _annotation_compatibility(reference: Path, annotation: Path) -> Dict[str, str]:
    ref_names = _fasta_contigs(reference)
    ann_names = _annotation_contigs(annotation)
    if not ref_names or not ann_names:
        return {"status": "unchecked", "detail": "contig preview unavailable"}
    overlap = ref_names & ann_names
    if overlap:
        return {"status": "compatible", "detail": f"shared_contigs={len(overlap)}"}
    return {
        "status": "compatibility_warning",
        "detail": f"reference_contigs={','.join(sorted(ref_names)[:4])}; annotation_contigs={','.join(sorted(ann_names)[:4])}",
    }


def _fasta_contigs(path: Path, limit: int = 32) -> set[str]:
    names: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith(">"):
                    names.add(line[1:].strip().split()[0])
                    if len(names) >= limit:
                        break
    except OSError:
        return set()
    return names


def _annotation_contigs(path: Path, limit: int = 128) -> set[str]:
    names: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                names.add(line.split("\t", 1)[0].strip())
                if len(names) >= limit:
                    break
    except OSError:
        return set()
    return names


def _dedupe_strings(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    unique: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _dedupe_group_records(records: Iterable[Dict[str, Any]], key_fields: Sequence[str]) -> List[Dict[str, Any]]:
    seen: set[Tuple[str, ...]] = set()
    unique: List[Dict[str, Any]] = []
    for record in records:
        key = tuple(str(record.get(field) or "") for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _select_artifact_spec(
    text: str = "", task_id: str = "", *, input_roles: Sequence[str] = (),
) -> Optional[ArtifactSpec]:
    """Compatibility helper: only accepted input roles select a domain spec."""
    return next((spec for spec in ARTIFACT_SPECS if spec.role in input_roles), None)


def _query_matches_spec(lower_text: str, spec: ArtifactSpec) -> bool:
    # A sentence describes inputs, outputs and history together. It has no
    # authority to create an input role. Preserve the helper for old importers.
    return False


def _extract_benchmark_id(text: str) -> str:
    match = re.search(r"\b(BM-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+)\b", str(text or ""), re.IGNORECASE)
    return match.group(1) if match else ""


def _extract_ambiguity_level(text: str) -> str:
    match = re.search(r"\b(?:ambiguity_level[:：\s]+)?(STRICT|MEDIUM|VAGUE)\b", str(text or ""), re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _extract_samples(text: str) -> List[str]:
    samples: List[str] = []
    stopwords = {
        "and",
        "archive",
        "artifact",
        "artifacts",
        "benchmark",
        "boundary",
        "boundaries",
        "candidate",
        "candidates",
        "compare",
        "context",
        "control",
        "data",
        "domain",
        "domains",
        "event",
        "events",
        "formal",
        "matrix",
        "mentions",
        "please",
        "resolution",
        "resume",
        "session",
        "sessionarchive",
        "tad",
        "target",
        "upstream",
        "using",
        "versus",
        "vs",
        "wiki",
    }
    file_suffixes = (
        ".md",
        ".sh",
        ".tsv",
        ".txt",
        ".yaml",
        ".yml",
        ".bed",
        ".bedgraph",
        ".png",
        ".pdf",
        ".csv",
        ".json",
    )

    def add_sample(raw: str) -> None:
        sample = Path(raw).name
        sample = re.sub(r"[^A-Za-z0-9_.-]", "", sample)
        sample = sample.strip("._-")
        if sample.lower().endswith(file_suffixes):
            return
        if sample and sample.lower() not in stopwords and sample not in samples:
            samples.append(sample)

    for pattern in (
        r"([^/\s,;:]+)\.m?cool\b",
        r"([^/\s,;:]+)_(?:domains|boundaries)\.bed\b",
        r"(?:target|control|目标|对照)\s*[:=：-]\s*([A-Za-z0-9_.-]+)",
    ):
        for match in re.finditer(pattern, str(text or ""), re.IGNORECASE):
            add_sample(match.group(1))
    for match in re.finditer(
        r"\b([A-Za-z][A-Za-z0-9_.-]*)\b\s*(?:and|vs\.?|versus|和|与)\s*\b([A-Za-z][A-Za-z0-9_.-]*)\b",
        str(text or ""),
        re.IGNORECASE,
    ):
        add_sample(match.group(1))
        add_sample(match.group(2))
    return samples


def _extract_resolution_labels(text: str) -> List[str]:
    labels: List[str] = []
    for match in re.finditer(r"\b(\d+)\s*kb\b", str(text or ""), re.IGNORECASE):
        value = int(match.group(1)) * 1000
        label = str(value)
        if label not in labels:
            labels.append(label)
    for pattern in (
        r"HiCExplorer[/\\](\d{4,8})\b",
        r"resolutions[/\\](\d{4,8})\b",
        r"\bresolution(?:-bp)?[:= ]+(\d{4,8})\b",
        r"\bres[:= ]+(\d{4,8})\b",
    ):
        for match in re.finditer(pattern, str(text or ""), re.IGNORECASE):
            label = match.group(1)
            if label not in labels:
                labels.append(label)
    return labels


def _verify_explicit_artifact_paths(
    text: str,
    spec: ArtifactSpec,
    work_dir: Path,
    project_root: Optional[Path],
) -> Tuple[List[Path], List[Path]]:
    valid: List[Path] = []
    invalid: List[Path] = []
    for raw in _path_tokens(text):
        if not _token_matches_spec(raw, spec):
            continue
        expanded = _expand_path_token(raw, work_dir, project_root)
        if not expanded:
            continue
        for path in expanded:
            if _is_valid_formal_file(path) and not _forbidden_path_reason(path, explicit=True):
                valid.append(path.resolve())
            elif "*" not in raw:
                invalid.append(path.resolve())
    return _dedupe_paths(valid), _dedupe_paths(invalid)


def _token_matches_spec(raw: str, spec: ArtifactSpec) -> bool:
    lower = raw.lower()
    return any(suffix in lower for suffix in spec.filename_suffixes)


def _path_tokens(text: str) -> List[str]:
    tokens = []
    for token in _raw_path_tokens(text):
        lower = token.lower()
        if ".bed" in lower or "*" in token:
            tokens.append(token)
    return tokens


def _raw_path_tokens(text: str) -> List[str]:
    return extract_path_tokens(text)


def _expand_path_token(raw: str, work_dir: Path, project_root: Optional[Path]) -> List[Path]:
    # An explicit project binding wins even when another cwd has a same-named file.
    bases = [project_root or work_dir]
    paths: List[Path] = []
    token_path = Path(raw).expanduser()
    if token_path.is_absolute():
        bases = [Path("/")]
    for base in bases:
        candidate = token_path if token_path.is_absolute() else base / token_path
        if "*" in str(candidate):
            paths.extend(Path(path).resolve() for path in candidate.parent.glob(candidate.name))
        else:
            paths.append(Path(os.path.abspath(candidate)))
    return _dedupe_paths(paths)


def _candidate_search_roots(
    work_dir: Path,
    project_root: Optional[Path],
    *,
    explicit_directories: Sequence[Path] = (),
    search_directories: Sequence[str | Path] = (),
    associated_history_directories: Sequence[str | Path] = (),
) -> List[Path]:
    roots = list(explicit_directories)
    for raw in [*search_directories, *associated_history_directories]:
        roots.extend(_expand_path_token(str(raw), work_dir, project_root))
    # work_dir is a usable fallback only when it is task-local, not the project
    # itself. Explicit directories take precedence over the fallback.
    if not roots and work_dir != project_root:
        roots.append(work_dir)
    return _dedupe_paths(path.resolve() for path in roots
                         if _is_dir_safe(path) and not _forbidden_path_reason(path, explicit=True))


def _candidate_record(
    path: Path,
    spec: ArtifactSpec,
    samples: Sequence[str],
    resolutions: Sequence[str],
    ambiguity_level: str,
    *,
    explicit: bool = False,
) -> Tuple[Optional[Dict[str, Any]], str]:
    resolved = path.resolve()
    reason = _forbidden_path_reason(resolved, explicit=explicit)
    if reason:
        return None, reason
    if not _is_valid_formal_file(resolved):
        return None, "missing or empty"
    matches = _explicit_path_matches_spec(resolved, spec) if explicit else _path_has_spec_suffix(resolved, spec)
    if not matches:
        return None, "filename does not match artifact kind"

    sample = _sample_from_artifact_path(resolved, spec)
    if samples and sample not in samples:
        return None, "sample does not match requested samples"
    resolution = _resolution_from_path(resolved)
    if resolutions and resolution and resolution not in resolutions:
        return None, "resolution does not match requested resolution"

    task_id = _benchmark_id_from_path(resolved)
    score = 0
    if sample in samples:
        score += 20
    if not samples:
        score += 4
    if resolution and resolution in resolutions:
        score += 20
    if any(token in str(resolved) for token in spec.directory_tokens):
        score += 4

    return {
        "path": str(resolved),
        "sample": sample,
        "resolution": resolution,
        "task_id": task_id,
        "score": score,
        "artifact_kind": spec.kind,
        "artifact_label": spec.label,
        "artifact_role": spec.role,
    }, ""


def _forbidden_path_reason(path: Path, explicit: bool = False) -> str:
    lower = str(path.resolve()).lower()
    for token in ALWAYS_FORBIDDEN_PATH_TOKENS:
        if token in lower:
            return f"forbidden path token: {token}"
    if explicit and any(token in lower for token in ALLOWED_FORMAL_FIXTURE_TOKENS):
        return ""
    for token in CONDITIONALLY_FORBIDDEN_PATH_TOKENS:
        if token in lower:
            return f"forbidden path token: {token}"
    return ""


def _explicit_path_matches_spec(path: Path, spec: ArtifactSpec) -> bool:
    # A user's accepted file need not use the discovery filename convention.
    # Validate only the declared format here; semantic BED contents belong to
    # the normal downstream preflight, not a filename-based replacement rule.
    return any(path.name.lower().endswith(Path(suffix).suffix.lower())
               for suffix in spec.filename_suffixes)


def _path_has_spec_suffix(path: Path, spec: ArtifactSpec) -> bool:
    lower = path.name.lower()
    return any(lower.endswith(suffix) for suffix in spec.filename_suffixes)


def _sample_from_artifact_path(path: Path, spec: ArtifactSpec) -> str:
    name = path.name
    lower_name = name.lower()
    for suffix in spec.filename_suffixes:
        if lower_name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _resolution_from_path(path: Path) -> str:
    for part in reversed(path.parts):
        if re.fullmatch(r"\d{4,8}", part):
            return part
    return ""


def _benchmark_id_from_path(path: Path) -> str:
    match = re.search(r"\b(BM-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+)\b", str(path), re.IGNORECASE)
    return match.group(1) if match else ""


def _is_valid_formal_file(path: Path) -> bool:
    try:
        return _is_file_safe(path) and path.stat().st_size > 0
    except OSError:
        return False


def _is_dir_safe(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file_safe(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _dedupe_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in candidates:
        path = str(item.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        unique.append(item)
    return unique


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen: set[str] = set()
    unique: List[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _select_candidates_by_sample(
    candidates: Sequence[Dict[str, Any]],
    samples: Sequence[str],
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    selected: Dict[str, Dict[str, Any]] = {}
    ambiguous: List[str] = []
    target_samples = list(samples) or sorted({str(item.get("sample") or "") for item in candidates if item.get("sample")})
    for sample in target_samples:
        accepted = [item for item in candidates if item.get("sample") == sample
                    and item.get("binding_status") == "accepted_input"]
        if len(accepted) == 1:
            selected[sample] = accepted[0]
        elif len(accepted) > 1:
            ambiguous.append(sample)
    return selected, ambiguous


def _ambiguous_resolution_values(
    candidates: Sequence[Dict[str, Any]],
    requested_resolutions: Sequence[str],
) -> List[str]:
    if requested_resolutions:
        return []
    values = sorted({str(item.get("resolution") or "") for item in candidates if item.get("resolution")})
    return values if len(values) > 1 else []


def _decision_text(result: Dict[str, Any]) -> str:
    if result.get("selection_required_roles") or result.get("formal_candidates"):
        return "scoped candidates are observations only; no input was selected by rank or uniqueness"
    if result.get("unresolved"):
        return "input evidence remains unresolved; preserve exact paths and pending verification at handoff"
    if result.get("resolved_inputs"):
        return "accepted input paths were verified; existing outputs and other observed paths remain separate"
    return "path existence is observed; no input direction was inferred from the request text"
