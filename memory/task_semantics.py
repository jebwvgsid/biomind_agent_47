from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .patch_validator import redact_secrets, sanitize_value
from .utils import now_iso


TASK_SEMANTIC_LIST_KEYS = {
    "skills",
    "samples",
    "output_artifacts",
    "output_dirs",
    "resolutions",
}
TASK_SEMANTIC_SCALAR_KEYS = {
    "task_id",
    "task_title",
    "task_type",
    "workflow",
    "workflow_id",
    "analysis_type",
    "task_date",
}


def infer_task_semantics(
    task_id: str,
    stage: str = "",
    messages: Optional[List[Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    messages = messages or []
    text = _semantic_context_text(task_id, stage, payload, messages)
    intent_text = _semantic_intent_text(task_id, stage, payload, messages)
    sample_text = _semantic_sample_text(payload, messages)

    workflow_id = _first_text(
        payload.get("workflow_id"),
        _nested_get(payload, ("workflow_handoff", "workflow_id")),
        _nested_get(payload, ("task_semantics", "workflow_id")),
        _nested_get(payload, ("task_semantics", "workflow")),
    )
    skills = _semantic_list(
        payload.get("selected_skills"),
        payload.get("active_skills"),
        payload.get("selected_skills_raw"),
        payload.get("skill_ids"),
        _nested_get(payload, ("workflow_handoff", "active_skills")),
        _nested_get(payload, ("workflow_handoff", "future_skills")),
        _nested_get(payload, ("task_semantics", "skills")),
    )
    samples = _semantic_list(
        payload.get("samples"),
        payload.get("sample"),
        payload.get("sample_id"),
        payload.get("sample_name"),
        _nested_get(payload, ("task_semantics", "samples")),
        _extract_samples(sample_text),
    )
    resolutions = _semantic_list(
        payload.get("resolution"),
        payload.get("resolutions"),
        payload.get("bin_size"),
        payload.get("binsize"),
        _nested_get(payload, ("task_semantics", "resolutions")),
        _extract_resolutions(text),
    )
    artifacts = _semantic_list(
        payload.get("artifacts"),
        payload.get("artifact"),
        payload.get("output_artifacts"),
        payload.get("output_paths"),
        payload.get("output_path"),
        payload.get("output_file"),
        payload.get("out_dir"),
        payload.get("plan_file"),
        payload.get("script_file"),
        _nested_get(payload, ("task_semantics", "output_artifacts")),
        _extract_path_like_values(text),
    )
    output_dirs = _semantic_list(
        payload.get("output_dirs"),
        payload.get("output_dir"),
        payload.get("out_dir"),
        _nested_get(payload, ("task_semantics", "output_dirs")),
        _artifact_dirs(artifacts),
    )

    task_type = _first_text(
        payload.get("task_type"),
        _nested_get(payload, ("task_semantics", "task_type")),
        _infer_task_type(intent_text, skills, workflow_id),
    )
    analysis_type = _first_text(
        payload.get("analysis_type"),
        _nested_get(payload, ("task_semantics", "analysis_type")),
        _infer_analysis_type(intent_text, skills, workflow_id),
    )
    task_date = _first_text(
        payload.get("task_date"),
        _nested_get(payload, ("task_semantics", "task_date")),
        now_iso()[:10].replace("-", ""),
    )
    task_title = _first_text(
        payload.get("task_title"),
        _nested_get(payload, ("task_semantics", "task_title")),
        _build_task_title(samples, analysis_type, task_date),
    )

    return normalize_task_semantics({
        "task_id": task_id,
        "task_title": task_title,
        "task_type": task_type,
        "workflow": workflow_id,
        "workflow_id": workflow_id,
        "analysis_type": analysis_type,
        "task_date": task_date,
        "skills": skills,
        "samples": samples,
        "resolutions": resolutions,
        "output_artifacts": artifacts,
        "output_dirs": output_dirs,
    })


def normalize_task_semantics(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for key in TASK_SEMANTIC_SCALAR_KEYS:
        text = redact_secrets(value.get(key) or "").strip()
        if text and text.lower() not in {"none", "n/a", "null"}:
            normalized[key] = text[:180]
    for key in TASK_SEMANTIC_LIST_KEYS:
        items = _ensure_list(value.get(key))
        if items:
            normalized[key] = items[:20]
    workflow = normalized.get("workflow") or normalized.get("workflow_id")
    if workflow:
        normalized["workflow"] = str(workflow)
        normalized["workflow_id"] = str(workflow)
    return normalized


def merge_task_semantics(*items: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for item in items:
        normalized = normalize_task_semantics(item)
        for key, value in normalized.items():
            if key in TASK_SEMANTIC_LIST_KEYS:
                merged[key] = _merge_lists(merged.get(key), value)
            elif value:
                merged[key] = value
    return normalize_task_semantics(merged)


def _content_of(message: Any) -> str:
    return str(getattr(message, "content", message))


def _role_of(message: Any) -> str:
    name = type(message).__name__.replace("Message", "")
    return name or "Message"


def _ensure_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: List[str] = []
    for item in items:
        text = redact_secrets(item).strip()
        if text and text.lower() not in {"none", "n/a", "null"}:
            result.append(text)
    return result


def _merge_lists(*values: Any) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _ensure_list(value):
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _nested_get(value: Any, path: Sequence[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = next((item for item in value if str(item).strip()), "")
        text = redact_secrets(value).strip()
        if text and text.lower() not in {"none", "n/a", "null"}:
            return text
    return ""


def _semantic_list(*values: Any) -> List[str]:
    merged: List[str] = []
    for value in values:
        merged = _merge_lists(merged, value)
    return merged[:20]


def _semantic_context_text(
    task_id: str,
    stage: str,
    payload: Dict[str, Any],
    messages: Sequence[Any],
) -> str:
    recent_messages = [
        {"role": _role_of(message), "content": _content_of(message)}
        for message in list(messages)[-6:]
    ]
    context = {
        "task_id": task_id,
        "stage": stage,
        "payload": sanitize_value(payload),
        "messages": recent_messages,
    }
    return json.dumps(context, ensure_ascii=False, default=str)[:120_000]


def _semantic_intent_text(
    task_id: str,
    stage: str,
    payload: Dict[str, Any],
    messages: Sequence[Any],
) -> str:
    user_messages = []
    for message in list(messages)[-12:]:
        role = _role_of(message)
        content = _content_of(message)
        if role.lower() in {"human", "message", "str"}:
            user_messages.append({"role": role, "content": content})
    if not user_messages:
        user_messages = [
            {"role": _role_of(message), "content": _content_of(message)}
            for message in list(messages)[-2:]
        ]

    intent_payload: Dict[str, Any] = {}
    for key in (
        "task_type",
        "analysis_type",
        "workflow_id",
        "selected_skills",
        "active_skills",
        "selected_skills_raw",
        "skill_ids",
        "samples",
        "sample",
        "sample_id",
        "sample_name",
        "resolution",
        "resolutions",
        "bin_size",
        "binsize",
    ):
        if key in payload:
            intent_payload[key] = payload.get(key)
    task_semantics = payload.get("task_semantics")
    if isinstance(task_semantics, dict):
        intent_payload["task_semantics"] = {
            key: task_semantics.get(key)
            for key in (
                "task_type",
                "analysis_type",
                "workflow_id",
                "workflow",
                "skills",
                "samples",
                "resolutions",
            )
            if key in task_semantics
        }

    context = {
        # A generated task title can contain stale inferred semantics. Treat it
        # as fallback evidence only when no user-authored message is available.
        "task_id": task_id if not user_messages else "",
        "stage": stage,
        "payload": sanitize_value(intent_payload),
        "messages": user_messages,
    }
    return json.dumps(context, ensure_ascii=False, default=str)[:40_000]


def _semantic_sample_text(payload: Dict[str, Any], messages: Sequence[Any]) -> str:
    """Project only user-authored and explicitly sample-bearing text."""
    parts: List[str] = []
    for message in list(messages)[-12:]:
        if _role_of(message).lower() in {"human", "message", "str"}:
            parts.append(_content_of(message))
    if not parts:
        parts.extend(_content_of(message) for message in list(messages)[-2:])

    for key in (
        "user_query",
        "original_user_query",
        "original_request",
        "user_request",
        "query",
        "prompt",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)[:40_000]


def _extract_samples(text: str) -> List[str]:
    """Extract biological sample candidates with role-aware evidence.

    Explicit sample declarations are strong evidence. Basenames from primary
    input data are medium evidence, but report/status/output filenames and
    ambiguous aggregate formats such as VCF/TSV are not sample identities.
    """
    samples: List[str] = []
    for match in re.finditer(
        r"(?i)(?:sample|samples|sample_id|sample_name|样本(?:id|编号|名称)?)\s*[:=：]\s*"
        r"([A-Za-z0-9_.-][A-Za-z0-9_. ,;-]{0,160})",
        text,
    ):
        raw = re.split(r"[\n\r\]\[{}()]", match.group(1), maxsplit=1)[0]
        samples.extend(part.strip(" ,;") for part in re.split(r"[,;]\s*", raw) if part.strip(" ,;"))

    section_role = "neutral"
    for line in str(text or "").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if not stripped:
            continue
        if re.search(r"(?:输入文件|输入数据|正式输入|数据位置|input files?|input data|data location)\s*[:：]?", lower):
            section_role = "input"
        elif re.search(
            r"(?:输出目录|输出文件|预期产物|最终产物|通用要求|停止条件|边界条件|禁止|允许|"
            r"output directory|expected outputs?|deliverables?|constraints?)\s*[:：]?",
            lower,
        ):
            section_role = "non_sample"
        elif stripped.endswith((":", "：")) and len(stripped) <= 100:
            section_role = "neutral"

        if section_role == "non_sample" or re.search(
            r"(?:预期产物|最终至少生成|输出目录|输出文件|report|status|audit|log file|禁止|边界条件)",
            lower,
        ):
            continue

        for match in re.finditer(
            r"(?i)([A-Za-z0-9][A-Za-z0-9_.-]{1,100})\."
            r"(?:mcool|cool|hic|bam|cram|fastq|fq|rds|h5ad)(?:\.gz)?\b",
            stripped,
        ):
            stem = match.group(1).strip("._-")
            stem = re.sub(r"(?i)(?:[._-](?:R?[12]|READ[12]))$", "", stem).strip("._-")
            if stem and stem.lower() not in {
                "tad_domains",
                "tad_boundaries",
                "summary",
                "matrix",
                "contact_matrix",
                "reference",
                "genome",
            }:
                samples.append(stem)
    return _dedupe_semantic_items(samples, limit=12)


def _extract_resolutions(text: str) -> List[str]:
    values: List[str] = []
    for match in re.finditer(
        r"(?i)(?:resolution|bin[_ -]?size|binsize|分辨率)\D{0,24}(\d+(?:\.\d+)?\s*(?:kb|k|bp|mb)?)",
        text,
    ):
        values.append(_normalize_resolution(match.group(1)))
    for match in re.finditer(r"(?i)\b(\d+(?:\.\d+)?)\s*(kb|bp|mb)\b", text):
        values.append(_normalize_resolution("".join(match.groups())))
    return _dedupe_semantic_items(values, limit=8)


def _normalize_resolution(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(k|kb|mb|bp)?", text)
    if not match:
        return text
    amount = float(match.group(1))
    unit = match.group(2) or "bp"
    if unit in {"k", "kb"}:
        return str(int(amount * 1000))
    if unit == "mb":
        return str(int(amount * 1_000_000))
    return str(int(amount))


def _extract_path_like_values(text: str) -> List[str]:
    values: List[str] = []
    pattern = r"(?i)([A-Za-z]:[\\/][^\"'\s<>|]+|(?:\.{0,2}[\\/])?[\w./\\:-]+\.(?:md|json|jsonl|sqlite3|txt|log|sh|py|r|R|tsv|csv|bed|cool|mcool|hic|png|pdf|html|rds|h5ad))"
    for match in re.finditer(pattern, text):
        values.append(match.group(1).rstrip(".,;)]}"))
    return _dedupe_semantic_items(values, limit=20)


def extract_path_like_values(text: str) -> List[str]:
    """Public wrapper for path-like token extraction used by task audit code."""
    return _extract_path_like_values(text)


def _artifact_dirs(artifacts: Sequence[str]) -> List[str]:
    dirs: List[str] = []
    for artifact in artifacts:
        value = str(artifact or "").strip()
        if not value or not re.search(r"[\\/]", value):
            continue
        parent = Path(value).parent
        if str(parent) not in {"", "."}:
            dirs.append(str(parent))
    return _dedupe_semantic_items(dirs, limit=12)


def _dedupe_semantic_items(items: Iterable[Any], limit: int = 20) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        text = redact_secrets(item).strip().strip("\"'")
        if not text or text.lower() in {"none", "n/a", "null"}:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text[:240])
        if len(result) >= limit:
            break
    return result


def _infer_task_type(text: str, skills: Sequence[str], workflow_id: str) -> str:
    haystack = " ".join([text, workflow_id, " ".join(skills)]).lower()
    if any(token in haystack for token in ("loop-calling", "loop_calling", "loop calling", "loop 鉴定", "loop鉴定", "mustache")):
        return "chromatin_contact_loop"
    if any(token in haystack for token in ("compartment-calling", "compartment_calling", "区室鉴定", "a/b区室", "ab区室")):
        return "chromatin_contact_compartment"
    if any(token in haystack for token in ("tad", "hicfindtads", "hicexplorer", "micro-c", "hi-c", "chromatin_contact")):
        return "chromatin_contact_tad"
    if any(token in haystack for token in ("scrna", "single_cell", "single-cell", "seurat")):
        return "single_cell_rnaseq"
    if any(token in haystack for token in ("methylation", "modkit", "dmr")):
        return "epigenomics_methylation"
    if any(token in haystack for token in ("blast", "sequence-similarity", "sequence_similarity")):
        return "sequence_search"
    return "general"


def _infer_analysis_type(text: str, skills: Sequence[str], workflow_id: str) -> str:
    haystack = " ".join([text, workflow_id, " ".join(skills)]).lower()
    if any(token in haystack for token in ("loop-calling", "loop_calling", "loop calling", "loop 鉴定", "loop鉴定", "mustache")):
        return "loop_calling"
    if any(token in haystack for token in ("differential-compartment", "differential_compartment", "差异区室")):
        return "differential_compartment"
    if any(token in haystack for token in ("compartment-calling", "compartment_calling", "区室鉴定", "a/b区室", "ab区室")):
        return "compartment_calling"
    if any(token in haystack for token in ("differential-tad", "differential_tad", "differential tad", "hicdifferentialtad", "差异tad", "差异 tad", "差异拓扑结构域")):
        return "differential_tad"
    if is_tad_boundary_change_intent(haystack):
        return "tad_boundary_change"
    if any(token in haystack for token in ("tad-calling", "tad_calling", "tad calling", "hicfindtads", "call tads", "tad 鉴定", "tad鉴定")):
        return "tad_calling"
    if any(token in haystack for token in ("tad", "hicexplorer", "hi-c", "micro-c")):
        return "tad_analysis"
    if any(token in haystack for token in ("scrna", "seurat", "single-cell")):
        return "scrna_analysis"
    return "general_analysis"


def is_explicit_tad_boundary_artifact_intent(text: str) -> bool:
    """Return whether text explicitly names a TAD/domain boundary artifact."""
    value = str(text or "").lower()
    if "_boundaries.bed" in value:
        return True
    return bool(
        re.search(
            r"(?:\btad[\s_-]*boundar(?:y|ies)\b|"
            r"\b(?:topological(?:ly)? associating|chromatin) domain[\s_-]*boundar(?:y|ies)\b|"
            r"\btad\s*边界|拓扑结构域\s*边界|染色质结构域\s*边界)",
            value,
            flags=re.IGNORECASE,
        )
    )


def is_tad_boundary_change_intent(text: str) -> bool:
    """Require explicit TAD boundary objects plus change/comparison intent."""
    value = str(text or "").lower()
    if re.search(r"\btad[-_ ]boundary[-_ ]change(?:s)?\b", value):
        return True
    domain_evidence = bool(
        re.search(
            r"(?:\btad\b|topological(?:ly)? associating domain|topology domain|"
            r"chromatin domain|3d genome|3d-dna|hi-c|\bhic\b|拓扑结构域|染色质结构域)",
            value,
            flags=re.IGNORECASE,
        )
    )
    change_evidence = bool(
        re.search(
            r"(?:\bchange(?:s|d)?\b|\bdifference(?:s)?\b|\bdifferential\b|\bcompare\b|"
            r"\bcomparison\b|\bgain(?:ed|s)?\b|\bloss(?:es)?\b|\blost\b|\bshift(?:s|ed)?\b|"
            r"变化|差异|比较|新增|丢失|移动)",
            value,
            flags=re.IGNORECASE,
        )
    )
    return domain_evidence and is_explicit_tad_boundary_artifact_intent(value) and change_evidence


def _build_task_title(samples: Sequence[str], analysis_type: str, task_date: str) -> str:
    sample_part = "_".join(_slug(sample, default="sample") for sample in list(samples)[:2])
    sample_part = sample_part or "project"
    analysis = _slug(analysis_type or "analysis", default="analysis")
    date = re.sub(r"\D+", "", task_date or "")[:8] or now_iso()[:10].replace("-", "")
    return f"{sample_part}_{analysis}_{date}"


def _slug(value: Any, default: str = "item") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip().lower()).strip("-._")
    return text[:120] or default
