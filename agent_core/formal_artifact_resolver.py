"""Resolve formal upstream analysis artifacts before asking or blocking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


FORBIDDEN_PATH_TOKENS = (
    "fixtures_legacy_toy",
    "fixtures",
    "toy",
    "tests",
    "expected_answers",
    "answer_key",
    "hidden",
    "truth",
    "demo",
)


@dataclass(frozen=True)
class ArtifactSpec:
    kind: str
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
        label="TAD boundary BED",
        filename_suffixes=("_boundaries.bed",),
        search_globs=("*_boundaries.bed",),
        query_tokens=(
            "tad boundary",
            "tad boundaries",
            "boundary",
            "boundaries",
            "_boundaries.bed",
            "边界",
            "tad边界",
        ),
        task_id_tokens=("-BOUNDARY-",),
        preferred_task_token="-TAD-",
        directory_tokens=("TADs/HiCExplorer", "TADs\\HiCExplorer"),
    ),
    ArtifactSpec(
        kind="tad_domains",
        label="TAD domain BED",
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
    relevant = spec is not None
    result: Dict[str, Any] = {
        "relevant": relevant,
        "artifact_kind": spec.kind if spec else "",
        "artifact_label": spec.label if spec else "",
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
        "unresolved": False,
        "unresolved_reasons": [],
        "consultant_action_hint": "",
    }
    if not relevant or spec is None:
        return result

    samples = _extract_samples(query_text)
    resolutions = _extract_resolution_labels(query_text)
    result["samples"] = samples
    result["resolutions"] = resolutions

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


def _select_artifact_spec(text: str, task_id: str = "") -> Optional[ArtifactSpec]:
    task_upper = str(task_id or "").upper()
    for spec in ARTIFACT_SPECS:
        if any(token in task_upper for token in spec.task_id_tokens):
            return spec

    if task_upper.startswith("BM-3DDNA-") and any(
        token in task_upper for token in ("-BALANCE-", "-COMP-", "-LOOP-", "-TAD-")
    ):
        return None

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
        return (
            has_tad_context
            or "_boundaries.bed" in lower_text
            or "边界" in lower_text
        ) and any(token in lower_text for token in spec.query_tokens)
    return False


def _extract_benchmark_id(text: str) -> str:
    match = re.search(r"\b(BM-3DDNA-[A-Z]+-[A-Z]+-\d+)\b", str(text or ""))
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
            if _is_valid_formal_file(path) and not _forbidden_path_reason(path):
                valid.append(path.resolve())
            elif "*" not in raw:
                invalid.append(path.resolve())
    return _dedupe_paths(valid), _dedupe_paths(invalid)


def _token_matches_spec(raw: str, spec: ArtifactSpec) -> bool:
    lower = raw.lower()
    return any(suffix in lower for suffix in spec.filename_suffixes)


def _path_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for match in re.finditer(r"(?P<path>(?:/|\.{0,2}/|[A-Za-z0-9_.-]+/)[^\s`'\"，,;；)）\]】]+)", str(text or "")):
        token = match.group("path").strip()
        token = token.rstrip(".:")
        if "/" in token and (".bed" in token or "*" in token):
            tokens.append(token)
    return tokens


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
            Path("benchmarks/biomind_agent_benchmark/runs/3d_dna"),
            Path("../benchmarks/biomind_agent_benchmark/runs/3d_dna"),
        ):
            candidate = (base / rel).resolve()
            if candidate.is_dir() and candidate not in roots:
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
    }, ""


def _forbidden_path_reason(path: Path) -> str:
    lower = str(path.resolve()).lower()
    for token in FORBIDDEN_PATH_TOKENS:
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
    match = re.search(r"\b(BM-3DDNA-[^/\\]+)\b", str(path))
    return match.group(1) if match else ""


def _is_valid_formal_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
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
