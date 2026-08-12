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
from memory.task_semantics import is_explicit_tad_boundary_artifact_intent


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


def resolve_formal_upstream_artifacts(
    query: str,
    work_dir: str | Path,
    task_id: str = "",
    project_root: str | Path | None = None,
    max_candidates: int = 24,
) -> Dict[str, Any]:
    """Search formal project outputs for reusable upstream files.

    The resolver is intentionally artifact-spec driven instead of benchmark-case
    driven. It only performs read-only path verification and candidate discovery.
    Biological intent, target/control direction, thresholds, and project
    background remain Consultant/skill responsibilities.
    """

    query_text = str(query or "")
    work_path = Path(work_dir or ".").expanduser().resolve()
    project_path = Path(project_root).expanduser().resolve() if project_root else None
    extracted_benchmark_id = _extract_benchmark_id(query_text)
    current_task_id = task_id or extracted_benchmark_id or ""
    spec = _select_artifact_spec(query_text, current_task_id)
    generic_discovery = _discover_local_inputs(query_text, work_path, project_path, max_candidates=max_candidates)
    relevant = spec is not None or bool(generic_discovery.get("relevant"))
    result: Dict[str, Any] = {
        "relevant": relevant,
        "artifact_kind": spec.kind if spec else "",
        "artifact_label": spec.label if spec else "",
        "artifact_role": spec.role if spec else "",
        "status": "not_applicable",
        "task_id": current_task_id,
        "benchmark_id": extracted_benchmark_id or task_id or "",
        "ambiguity_level": _extract_ambiguity_level(query_text) or _extract_ambiguity_level(task_id),
        "samples": [],
        "resolutions": [],
        "search_roots": [],
        "verified_explicit_paths": [],
        "invalid_explicit_paths": [],
        "excluded_candidates": [],
        "formal_candidates": [],
        "selected_by_sample": {},
        "ambiguous_samples": [],
        "ambiguous_resolutions": [],
        "resolved_inputs": generic_discovery.get("resolved_inputs", []),
        "candidate_groups": generic_discovery.get("candidate_groups", {}),
        "missing_roles": generic_discovery.get("missing_roles", []),
        "ambiguous_roles": generic_discovery.get("ambiguous_roles", []),
        "discovery_log": generic_discovery.get("discovery_log", []),
        "unresolved": False,
        "unresolved_reasons": [],
        "consultant_action_hint": "",
    }
    if not relevant:
        return result

    samples = _extract_samples(query_text)
    resolutions = _extract_resolution_labels(query_text)
    result["samples"] = samples
    result["resolutions"] = resolutions

    if spec is None:
        if result["ambiguous_roles"]:
            result["status"] = "ambiguous_inputs"
            result["unresolved"] = True
            result["unresolved_reasons"] = ["multiple plausible input candidates"]
            result["consultant_action_hint"] = "ask_user_to_choose_input_candidate"
        elif result["missing_roles"]:
            result["status"] = "missing_candidates"
            result["unresolved"] = True
            result["unresolved_reasons"] = ["local input discovery found unresolved roles"]
            result["consultant_action_hint"] = "ask_user_for_formal_paths_or_upstream_permission"
        else:
            result["status"] = "resolved"
        return result

    explicit_valid, explicit_invalid = _verify_explicit_artifact_paths(query_text, spec, work_path, project_path)
    result["verified_explicit_paths"] = [str(path) for path in explicit_valid]
    result["invalid_explicit_paths"] = [str(path) for path in explicit_invalid]

    search_roots = _candidate_search_roots(work_path, project_path)
    result["search_roots"] = [str(path) for path in search_roots]

    formal_candidates: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    for root in search_roots:
        for glob in spec.search_globs:
            for path in sorted(root.rglob(glob)):
                candidate, reason = _candidate_record(
                    path,
                    spec,
                    samples,
                    resolutions,
                    result["ambiguity_level"],
                )
                if reason:
                    if len(excluded) < max_candidates:
                        excluded.append({"path": str(path.resolve()), "reason": reason})
                    continue
                if candidate:
                    formal_candidates.append(candidate)

    formal_candidates = _dedupe_candidates(formal_candidates)
    formal_candidates.sort(
        key=lambda item: (
            int(item.get("score", 0)),
            str(item.get("task_id") or ""),
            str(item.get("path") or ""),
        ),
        reverse=True,
    )
    result["formal_candidates"] = formal_candidates[:max_candidates]
    result["excluded_candidates"] = excluded

    selected_by_sample, ambiguous_samples = _select_candidates_by_sample(formal_candidates, samples)
    ambiguous_resolutions = _ambiguous_resolution_values(formal_candidates, resolutions)
    result["selected_by_sample"] = selected_by_sample
    result["ambiguous_samples"] = ambiguous_samples
    result["ambiguous_resolutions"] = ambiguous_resolutions

    if ambiguous_samples:
        result["status"] = "ambiguous_candidates"
        result["unresolved"] = True
        result["unresolved_reasons"] = ["multiple same-priority formal candidates"]
        result["consultant_action_hint"] = "ask_user_to_choose_candidate"
    elif ambiguous_resolutions:
        result["status"] = "ambiguous_resolution"
        result["unresolved"] = True
        result["unresolved_reasons"] = ["multiple formal candidate resolutions"]
        result["consultant_action_hint"] = "ask_user_to_confirm_resolution_or_candidate"
    elif formal_candidates or explicit_valid:
        result["status"] = "resolved"
    else:
        result["status"] = "missing_candidates"
        result["unresolved"] = True
        result["unresolved_reasons"] = ["no valid formal upstream candidates found"]
        result["consultant_action_hint"] = "ask_user_for_formal_paths_or_upstream_permission"

    return result


def format_formal_upstream_context(result: Dict[str, Any]) -> str:
    """Format resolver output for Consultant/Planner prompt injection."""

    if not result or not result.get("relevant"):
        return ""

    lines = [
        "[INPUT_RESOLUTION_AUDIT]",
        "policy:先找再问; verify formal project artifacts before ASK or BLOCK",
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


def _discover_local_inputs(
    text: str,
    work_dir: Path,
    project_root: Optional[Path],
    max_candidates: int = 24,
) -> Dict[str, Any]:
    """Discover generic local input relationships without domain decisions."""

    result: Dict[str, Any] = {
        "relevant": False,
        "resolved_inputs": [],
        "candidate_groups": {},
        "missing_roles": [],
        "ambiguous_roles": [],
        "discovery_log": [],
    }
    explicit_tokens = _raw_path_tokens(text)
    if not explicit_tokens:
        return result

    explicit_dirs: List[Path] = []
    explicit_files: List[Path] = []
    invalid_paths: List[str] = []
    excluded: List[str] = []
    for raw in explicit_tokens:
        candidates = _expand_path_token(raw, work_dir, project_root)
        resolved_raw = False
        candidate_failures: List[str] = []
        for path in candidates:
            if _forbidden_path_reason(path, explicit=True):
                excluded.append(str(path))
                continue
            if _is_dir_safe(path):
                explicit_dirs.append(path.resolve())
                result["resolved_inputs"].append(_input_record("input_directory", path, "explicit"))
                resolved_raw = True
                continue
            if _is_file_safe(path):
                explicit_files.append(path.resolve())
                result["resolved_inputs"].append(_input_record(_role_for_path(path), path, "explicit"))
                explicit_dirs.append(path.parent.resolve())
                resolved_raw = True
            else:
                candidate_failures.append(str(path))
        if not resolved_raw and _looks_like_relevant_input_path(raw):
            invalid_paths.extend(candidate_failures or [raw])

    explicit_dirs = _dedupe_paths(explicit_dirs)
    explicit_files = _dedupe_paths(explicit_files)
    if invalid_paths and explicit_files:
        existing_by_name: Dict[str, List[Path]] = {}
        for path in explicit_files:
            existing_by_name.setdefault(path.name.lower(), []).append(path)
        retained_invalid: List[str] = []
        for value in invalid_paths:
            matches = existing_by_name.get(Path(value).name.lower(), [])
            if len(matches) == 1:
                result["discovery_log"].append(
                    f"explicit alias resolved: {value} -> {matches[0]}"
                )
                continue
            retained_invalid.append(value)
        invalid_paths = retained_invalid
    result["relevant"] = bool(explicit_files or explicit_dirs or invalid_paths or excluded)
    if invalid_paths:
        result["missing_roles"].append("explicit_input_path")
        result["discovery_log"].append("explicit input path(s) did not resolve: " + "; ".join(invalid_paths[:6]))
    if excluded:
        result["discovery_log"].append("excluded forbidden explicit path(s): " + "; ".join(excluded[:6]))

    _discover_paired_fastqs(result, explicit_files, explicit_dirs, max_candidates=max_candidates)
    _discover_sidecars(result, explicit_files)
    if _should_discover_reference_annotation(text, explicit_files):
        _discover_reference_annotation_pairs(result, explicit_files, explicit_dirs, work_dir, project_root, max_candidates)
    result["resolved_inputs"] = _dedupe_resolved_inputs(result.get("resolved_inputs") or [])
    result["missing_roles"] = _dedupe_strings(result.get("missing_roles") or [])
    result["ambiguous_roles"] = _dedupe_strings(result.get("ambiguous_roles") or [])
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
        if not explicit_fastqs and len(paired_groups) == 1:
            group = paired_groups[0]
            result["resolved_inputs"].append(
                {
                    "role": "paired_fastq",
                    "paths": [group["r1"], group["r2"]],
                    "sample": group["sample"],
                    "source": "directory_discovery",
                }
            )
        elif not explicit_fastqs and len(paired_groups) > 1:
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
) -> None:
    roots = _local_input_search_roots(explicit_dirs, work_dir, project_root)
    references = _dedupe_paths([path for path in explicit_files if _role_for_path(path) == "reference_fasta"])
    annotations = _dedupe_paths([path for path in explicit_files if _role_for_path(path) == "annotation_file"])
    if not references:
        references = _find_files_by_suffixes(roots, (".fa", ".fasta", ".fna"), max_candidates=max_candidates)
    if not annotations:
        annotations = _find_files_by_suffixes(roots, (".gtf", ".gff", ".gff3"), max_candidates=max_candidates)

    reference_records = [_input_record("reference_fasta", path, "local_discovery") for path in references[:max_candidates]]
    annotation_records: List[Dict[str, Any]] = []
    for annotation in annotations[:max_candidates]:
        record = _input_record(_role_for_path(annotation), annotation, "local_discovery")
        if references:
            compatibility = _annotation_compatibility(references[0], annotation)
            record["compatibility"] = compatibility["status"]
            if compatibility.get("detail"):
                record["detail"] = compatibility["detail"]
        annotation_records.append(record)

    if reference_records:
        result["candidate_groups"]["reference_fasta"] = reference_records
        if len(reference_records) == 1:
            result["resolved_inputs"].append(reference_records[0])
        else:
            result["ambiguous_roles"].append("reference_fasta")
    if annotation_records:
        result["candidate_groups"]["annotation_file"] = annotation_records
        compatible = [item for item in annotation_records if item.get("compatibility") in ("compatible", "unchecked")]
        if len(compatible) == 1:
            result["resolved_inputs"].append(compatible[0])
            result["discovery_log"].append(f"annotation candidate selected: {Path(compatible[0]['path']).name}")
        elif len(compatible) > 1:
            result["ambiguous_roles"].append("annotation_file")
        elif annotation_records:
            result["ambiguous_roles"].append("annotation_file")
            result["discovery_log"].append("annotation candidates found but none passed lightweight compatibility")


def _should_discover_reference_annotation(text: str, explicit_files: Sequence[Path]) -> bool:
    if any(_role_for_path(path) in {"reference_fasta", "annotation_file"} for path in explicit_files):
        return True
    lower = str(text or "").lower()
    return any(
        token in lower
        for token in (
            "annotation",
            "annotate",
            "gtf",
            "gff",
            "gff3",
            "reference",
            "genome fasta",
            "参考",
            "注释",
        )
    )


def _local_input_search_roots(
    explicit_dirs: Sequence[Path],
    work_dir: Path,
    project_root: Optional[Path],
) -> List[Path]:
    roots: List[Path] = []
    seeds = list(explicit_dirs) or [work_dir]
    if project_root:
        seeds.append(project_root)
    target_names = {"data", "ref", "refs", "reference", "annotation", "annotations", "genome", "metadata", "samples"}
    for seed in seeds:
        if _is_dir_safe(seed) and seed not in roots:
            roots.append(seed)
        for parent in list(seed.parents)[:4]:
            if parent not in roots:
                roots.append(parent)
            try:
                children = list(parent.iterdir())[:200]
            except OSError:
                children = []
            for child in children:
                if not _is_dir_safe(child):
                    continue
                child_name = child.name.lower()
                if child_name in target_names or any(token in child_name for token in target_names):
                    if child not in roots:
                        roots.append(child)
                data_child = child / "data"
                if _is_dir_safe(data_child) and data_child not in roots:
                    roots.append(data_child)
    return roots[:80]


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
            if _forbidden_path_reason(path, explicit=False):
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


def _select_artifact_spec(text: str, task_id: str = "") -> Optional[ArtifactSpec]:
    task_upper = str(task_id or "").upper()
    for spec in ARTIFACT_SPECS:
        canonical_benchmark_task = task_upper.startswith("BM-")
        if spec.kind == "tad_boundaries":
            canonical_benchmark_task = canonical_benchmark_task and any(
                token in task_upper for token in ("3DDNA", "3D-DNA", "TAD", "HIC")
            )
        if canonical_benchmark_task and any(token in task_upper for token in spec.task_id_tokens):
            return spec

    lower = str(text or "").lower()
    for spec in ARTIFACT_SPECS:
        if _query_matches_spec(lower, spec):
            return spec
    return None


def _query_matches_spec(lower_text: str, spec: ArtifactSpec) -> bool:
    has_tad_context = "tad" in lower_text or "拓扑" in lower_text
    if spec.kind == "tad_domains":
        return has_tad_context and any(token in lower_text for token in spec.query_tokens)
    if spec.kind == "tad_boundaries":
        return is_explicit_tad_boundary_artifact_intent(lower_text)
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
    bases = [work_dir]
    if project_root and project_root not in bases:
        bases.append(project_root)
    paths: List[Path] = []
    token_path = Path(raw).expanduser()
    if token_path.is_absolute():
        bases = [Path("/")]
    for base in bases:
        candidate = token_path if token_path.is_absolute() else base / token_path
        if "*" in str(candidate):
            paths.extend(Path(path).resolve() for path in candidate.parent.glob(candidate.name))
        else:
            paths.append(candidate.resolve())
    return _dedupe_paths(paths)


def _candidate_search_roots(work_dir: Path, project_root: Optional[Path]) -> List[Path]:
    bases: List[Path] = []
    for path in [work_dir, project_root]:
        if path and path not in bases:
            bases.append(path)
        if path:
            for parent in list(path.parents)[:3]:
                if parent not in bases:
                    bases.append(parent)

    roots: List[Path] = []
    for base in bases:
        for rel in (
            Path("benchmarks/biomind_agent_benchmark/runs"),
            Path("benchmarks/biomind_agent_benchmark/runs/3d_dna"),
            Path("../benchmarks/biomind_agent_benchmark/runs"),
            Path("../benchmarks/biomind_agent_benchmark/runs/3d_dna"),
        ):
            candidate = (base / rel).resolve()
            if _is_dir_safe(candidate) and candidate not in roots:
                roots.append(candidate)
    return roots


def _candidate_record(
    path: Path,
    spec: ArtifactSpec,
    samples: Sequence[str],
    resolutions: Sequence[str],
    ambiguity_level: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    resolved = path.resolve()
    reason = _forbidden_path_reason(resolved)
    if reason:
        return None, reason
    if not _is_valid_formal_file(resolved):
        return None, "missing or empty"
    if not _path_has_spec_suffix(resolved, spec):
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
    if task_id and spec.preferred_task_token in task_id:
        score += 10
    if ambiguity_level and f"-{ambiguity_level}-" in task_id:
        score += 12
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
        sample_candidates = [item for item in candidates if item.get("sample") == sample]
        if not sample_candidates:
            continue
        top_score = max(int(item.get("score", 0)) for item in sample_candidates)
        top = [item for item in sample_candidates if int(item.get("score", 0)) == top_score]
        if len(top) == 1:
            selected[sample] = top[0]
        else:
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
    if result.get("ambiguous_samples"):
        return "multiple formal upstream candidates found; Consultant must resolve before handoff"
    if result.get("ambiguous_resolutions"):
        return "multiple formal upstream resolutions found; Consultant must resolve before handoff"
    if result.get("unresolved"):
        return "formal artifact search completed; unresolved input evidence remains"
    if result.get("formal_candidates") or result.get("verified_explicit_paths"):
        return "formal upstream candidates are available after read-only verification"
    return "no valid formal upstream candidates found"
