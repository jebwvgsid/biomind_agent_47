"""Structured, user-owned task state shared by BioMind nodes.

The task spec preserves exact user directives while allowing later answers and
plan-review feedback to supersede earlier assumptions without relying on chat
history. Skill selection and execution state intentionally live elsewhere.
"""

from __future__ import annotations

import json
import hashlib
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping

TaskSpec = Dict[str, Any]
TaskChangeType = Literal[
    "requirement_change",
    "plan_feedback",
    "execution_feedback",
    "approval",
    "informational",
]

REQUIREMENT_LEVELS = {
    "required",
    "conditional",
    "recommended",
    "optional_validation",
    "known_absent",
    "unknown",
}

TASK_SPEC_SCHEMA_VERSION = 4
TASK_SPEC_MIGRATION_REQUEST_BACKFILL = "task_spec_v3_request_backfill"
TASK_SPEC_MIGRATION_GENERIC_FACTS = "task_spec_v4_generic_user_facts"
SOURCE_PRIORITY = {
    "current_task_user_explicit": 90,
    "current_task_user_confirmed": 80,
    "inherited_explicit": 70,
    "user_explicit": 40,
    "user_initial": 40,
    "user_feedback": 40,
    "user": 40,
    "consultant_inferred": 30,
    "consultant": 30,
    "runtime_detected": 25,
    "archive_retrieved": 15,
    "skill_default": 10,
    "planner_derived": 20,
    "planner": 20,
    "generate_inferred": 10,
    "legacy_task_spec": 10,
    "legacy_task_input": 10,
}

INPUT_ROLE_ARGUMENT_CANDIDATES = {
    "variant_vcf": ("vcf", "variant_file", "variants"),
    "reference_fasta": ("reference_fasta", "reference", "genome_fasta", "fasta"),
    "annotation_gtf": ("annotation_gtf", "gtf", "annotation_file"),
    "annotation_gff3": ("annotation_gff3", "gff3", "annotation_file"),
    "annotation_genbank": ("annotation_genbank", "genbank", "annotation_file"),
    "alignment_bam": ("bam", "alignment_bam", "alignment"),
    "fastq": ("fastq", "reads"),
}

_LIST_FIELDS = (
    "user_directives",
    "inputs",
    "output_contracts",
    "must_do",
    "must_not_do",
    "stop_conditions",
    "acceptance_criteria",
    "confirmed_decisions",
    "unresolved_questions",
    "requirements",
    "expected_outputs",
    "facts",
    "artifact_bindings",
    "entity_bindings",
    "parameter_bindings",
    "constraint_bindings",
    "decision_bindings",
    "relation_bindings",
    "unresolved_facts",
)

FACT_FIELDS = {
    "artifact": "artifact_bindings",
    "entity": "entity_bindings",
    "parameter": "parameter_bindings",
    "constraint": "constraint_bindings",
    "decision": "decision_bindings",
    "relation": "relation_bindings",
    "unresolved": "unresolved_facts",
}

USER_MESSAGE_ORIGINS = {"user", "human_user", "interactive_user"}

_ARTIFACT_ROLE_RULES = (
    (r"seurat[_-]?qc", "seurat_qc_object"),
    (r"marker[_-]?(?:reference|rules?)", "marker_reference"),
    (r"background[_-]?gene", "background_gene_list"),
    (r"(?:^|[_-])gene[_-]?list", "gene_list"),
    (r"\bgenotype(?:[_-]|\.)", "genotype"),
    (r"\bphenotype(?:[_-]|\.)", "phenotype"),
    (r"\bcovariates?(?:[_-]|\.)", "covariates"),
)

_FORMAT_ROLE_RULES = {
    "vcf": "variant_vcf",
    "bcf": "variant_vcf",
    "fa": "reference_fasta",
    "fasta": "reference_fasta",
    "fna": "reference_fasta",
    "gtf": "annotation_gtf",
    "gff": "annotation_gff3",
    "gff3": "annotation_gff3",
    "bam": "alignment_bam",
    "cool": "contact_matrix",
    "mcool": "contact_matrix",
    "rds": "r_object",
    "h5ad": "single_cell_object",
    "csv": "input_table",
    "tsv": "input_table",
    "txt": "input_text",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _field_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _text(value).lower().lstrip("-")).strip("_")


def _stable_fact_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return "fact-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _message_id(text: str, source_task_id: str = "") -> str:
    return "msg-" + hashlib.sha256(
        f"{source_task_id}\0{_text(text)}".encode("utf-8")
    ).hexdigest()[:20]


def _fact_source(source: str, *, confirmed: bool = False) -> str:
    normalized = _field_key(source)
    if normalized in {"user", "user_initial", "user_feedback", "user_explicit"}:
        return "current_task_user_confirmed" if confirmed else "current_task_user_explicit"
    if normalized in SOURCE_PRIORITY:
        return normalized
    return _binding_source(source)


def _scalar(value: str) -> Any:
    text = _text(value).strip("`'\"，,；;。")
    lower = text.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        try:
            return int(text)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:e[-+]?\d+)?", text, re.IGNORECASE):
        try:
            return float(text)
        except ValueError:
            pass
    return text


def _artifact_format(locator: str) -> str:
    name = Path(_text(locator)).name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix or ("directory" if not Path(name).suffix else "file")


def generic_artifact_role(locator: str, context: str = "") -> str:
    """Infer an open artifact role from local lexical evidence, never a skill ID."""
    name = Path(_text(locator)).name.lower()
    # Apply lexical role hints to the candidate itself. Using the whole user
    # sentence would let one labelled path contaminate sibling paths.
    lexical = name
    for pattern, role in _ARTIFACT_ROLE_RULES:
        if re.search(pattern, lexical):
            return role
    file_format = _artifact_format(locator)
    return _FORMAT_ROLE_RULES.get(file_format, "input_file")


def _fact_metadata(
    candidate: Mapping[str, Any],
    *,
    source: str,
    revision: int,
    source_message_id: str,
    source_task_id: str,
    migration_source: str = "",
) -> Dict[str, Any]:
    value = deepcopy(dict(candidate))
    kind = _field_key(value.get("fact_kind")) or "unresolved"
    fact_source = _fact_source(_text(value.get("source") or source))
    status = _text(value.get("status") or "active").lower()
    if status not in {"active", "superseded", "withdrawn", "unresolved", "provisional"}:
        status = "active"
    identity = (
        value.get("role"), value.get("locator"), value.get("entity_type"),
        value.get("namespace"), value.get("key"), value.get("value"),
        value.get("subject"), value.get("predicate"), value.get("object"),
        value.get("text"),
    )
    message_id = _text(value.get("source_message_id") or source_message_id)
    task_id = _text(value.get("source_task_id") or source_task_id)
    value.update({
        "fact_kind": kind,
        "fact_id": _text(value.get("fact_id")) or _stable_fact_id(task_id, message_id, kind, identity),
        "source": fact_source,
        "source_type": _text(value.get("source_type") or fact_source),
        "source_message_id": message_id,
        "source_task_id": task_id,
        "revision_id": int(value.get("revision_id") or revision),
        "created_at": _text(value.get("created_at") or _now()),
        "status": status,
        "supersedes": list(value.get("supersedes") or []),
        "merge_reason": _text(value.get("merge_reason") or "user_fact_extraction"),
        "confidence": float(value.get("confidence", 1.0)),
    })
    if migration_source:
        value["migration_source"] = migration_source
    return value


def extract_user_fact_candidates(text: str) -> List[Dict[str, Any]]:
    """Extract domain-neutral fact candidates from one confirmed user message."""
    raw = _text(text)
    if not raw:
        return []
    candidates: List[Dict[str, Any]] = []
    lower = raw.lower()
    replacement = bool(re.search(r"\b(?:replace|change|instead)\b|改为|改成|换成|替换", lower))
    withdrawal = bool(re.search(r"\b(?:withdraw|remove|stop using|no longer use)\b|不再使用|撤销|移除", lower))
    retain_only = bool(re.search(r"\b(?:keep|retain)\s+only\b|只保留|仅保留", lower))

    output_dir = _request_output_directory(raw)
    path_tokens = extract_path_tokens(raw)
    for index, locator in enumerate(path_tokens):
        if output_dir and locator.rstrip("/.") == output_dir.rstrip("/."):
            continue
        role = generic_artifact_role(locator, raw)
        status = "active"
        cardinality = "many" if role == "contact_matrix" else "unknown"
        if withdrawal:
            status = "withdrawn"
        elif replacement and len(path_tokens) >= 2:
            status = "withdrawn" if index == 0 else "active"
            cardinality = "one"
        elif replacement and len(path_tokens) == 1:
            cardinality = "one"
        candidates.append({
            "fact_kind": "artifact",
            "role": role,
            "direction": "input",
            "locator": locator,
            "artifact_type": "file",
            "format": _artifact_format(locator),
            "cardinality": cardinality,
            "required": True,
            "scope": "task",
            "status": status,
            "retain_only": retain_only,
        })
    if output_dir:
        candidates.append({
            "fact_kind": "artifact",
            "role": "output_dir",
            "direction": "output",
            "locator": output_dir,
            "artifact_type": "directory",
            "format": "directory",
            "cardinality": "one",
            "required": True,
            "scope": "task",
            "status": "active",
        })

    entity_pattern = re.compile(
        r"(?P<key>species|物种|sample|样本|group|组别|trait|性状|cell[_ ]?type|细胞类型|reference[_ ]?build|基因组版本)"
        r"\s*(?:is|是|=|:|：)\s*(?P<value>[^\n,，;；。]+)",
        re.IGNORECASE,
    )
    entity_names = {
        "物种": "species", "species": "species", "样本": "sample", "sample": "sample",
        "组别": "group", "group": "group", "性状": "trait", "trait": "trait",
        "细胞类型": "cell_type", "cell type": "cell_type", "cell_type": "cell_type",
        "基因组版本": "reference_build", "reference build": "reference_build", "reference_build": "reference_build",
    }
    for match in entity_pattern.finditer(raw):
        key = entity_names.get(match.group("key").lower().replace("  ", " "), _field_key(match.group("key")))
        candidates.append({
            "fact_kind": "entity",
            "entity_type": key,
            "value": _text(match.group("value")),
            "cardinality": "many" if key in {"sample", "group", "cell_type"} else "one",
            "scope": "task",
            "status": "active",
        })

    assignments = re.compile(
        r"(?<![-\w])(?P<key>[A-Za-z][A-Za-z0-9_.-]{1,80})\s*=\s*(?P<value>[^\s,，;；。]+)"
    )
    entity_assignment_keys = {"species", "sample", "group", "trait", "cell_type", "reference_build"}
    for match in assignments.finditer(raw):
        raw_key = match.group("key")
        value = _scalar(match.group("value"))
        key_parts = raw_key.split(".", 1)
        namespace, key = (key_parts[0], key_parts[1]) if len(key_parts) == 2 else ("", raw_key)
        candidates.append({
            "fact_kind": "parameter",
            "namespace": _field_key(namespace),
            "key": _field_key(key),
            "value": value,
            "value_type": type(value).__name__,
            "unit": "",
            "scope": "task",
            "cardinality": "one",
            "status": "active",
        })
        if _field_key(raw_key) in entity_assignment_keys:
            candidates.append({
                "fact_kind": "entity",
                "entity_type": _field_key(raw_key),
                "value": value,
                "cardinality": "one",
                "scope": "task",
                "status": "active",
            })

    for flag in re.finditer(r"(?<!\w)--?(?P<key>[A-Za-z][A-Za-z0-9_-]{2,})(?!\w)", raw):
        candidates.append({
            "fact_kind": "parameter",
            "namespace": "cli",
            "key": _field_key(flag.group("key")),
            "value": True,
            "value_type": "bool",
            "unit": "",
            "scope": "task",
            "cardinality": "one",
            "status": "active",
        })

    constraint_match = re.search(
        r"\b(?:must|must not|do not|don't|never|only|at least|at most|prefer)\b|必须|禁止|不得|不要|只允许|仅允许|优先|至少|最多",
        raw,
        re.IGNORECASE,
    )
    if constraint_match:
        marker = constraint_match.group(0).lower()
        mode = "prohibit" if marker in {"must not", "do not", "don't", "never", "禁止", "不得", "不要"} else "require"
        if marker in {"prefer", "优先"}:
            mode = "prefer"
        candidates.append({
            "fact_kind": "constraint",
            "constraint_type": mode,
            "text": raw,
            "scope": "task",
            "cardinality": "many",
            "status": "active",
        })

    decision_match = re.search(r"确认|拒绝|替换|撤销|选择|改为|换成|\b(?:confirm|reject|replace|withdraw|select)\b", raw, re.IGNORECASE)
    if decision_match:
        candidates.append({
            "fact_kind": "decision",
            "decision_type": _field_key(decision_match.group(0)),
            "text": raw,
            "scope": "task",
            "cardinality": "many",
            "status": "active",
        })

    relation_patterns = (
        re.compile(r"(?P<left>[A-Za-z0-9_.-]+)\s+(?P<predicate>compared_with|derived_from|belongs_to|depends_on|consumes|produced_by)\s+(?P<right>[A-Za-z0-9_.-]+)", re.IGNORECASE),
        re.compile(r"(?P<left>[A-Za-z0-9_.-]+)\s*(?:与|和)\s*(?P<right>[A-Za-z0-9_.-]+)\s*(?P<predicate>比较)", re.IGNORECASE),
    )
    for pattern in relation_patterns:
        for match in pattern.finditer(raw):
            predicate = match.group("predicate").lower()
            if predicate == "比较":
                predicate = "compared_with"
            candidates.append({
                "fact_kind": "relation",
                "subject": match.group("left"),
                "predicate": predicate,
                "object": match.group("right"),
                "scope": "task",
                "cardinality": "many",
                "status": "active",
            })
    return candidates


def normalize_fact_candidates_with_skill(
    candidates: Iterable[Mapping[str, Any]],
    skill_contract: Mapping[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Apply a selected skill's declared aliases/types without knowing the skill ID."""
    contract = dict(skill_contract or {})
    input_specs = contract.get("inputs") or []
    output_specs = contract.get("outputs") or []
    role_specs: List[Dict[str, Any]] = []
    for direction, values in (("input", input_specs), ("output", output_specs)):
        if isinstance(values, Mapping):
            values = [{"role": role, **(dict(meta) if isinstance(meta, Mapping) else {})} for role, meta in values.items()]
        for value in values if isinstance(values, list) else []:
            if isinstance(value, Mapping):
                role_specs.append({"direction": direction, **dict(value)})
    parameters = contract.get("parameters") if isinstance(contract.get("parameters"), Mapping) else {}
    normalized: List[Dict[str, Any]] = []
    for raw in candidates:
        item = deepcopy(dict(raw))
        if _field_key(item.get("fact_kind")) == "artifact":
            current = _field_key(item.get("role"))
            matches = [
                spec for spec in role_specs
                if current == _field_key(spec.get("role"))
                or current in {_field_key(alias) for alias in spec.get("aliases", []) or []}
            ]
            if len(matches) == 1:
                spec = matches[0]
                item["role"] = _text(spec.get("role")) or current
                item["direction"] = _text(spec.get("direction") or item.get("direction"))
                item["cardinality"] = _text(spec.get("cardinality") or item.get("cardinality") or "unknown")
                item["required"] = bool(spec.get("required", item.get("required", False)))
        elif _field_key(item.get("fact_kind")) == "parameter":
            key = _field_key(item.get("key"))
            spec = parameters.get(key) or next(
                (meta for name, meta in parameters.items() if key in {_field_key(alias) for alias in (meta.get("aliases", []) if isinstance(meta, Mapping) else [])}),
                None,
            )
            if isinstance(spec, Mapping):
                item["key"] = _text(spec.get("key") or key)
                item["namespace"] = _text(spec.get("namespace") or item.get("namespace"))
                item["value_type"] = _text(spec.get("type") or item.get("value_type"))
                item["unit"] = _text(spec.get("unit") or item.get("unit"))
        normalized.append(item)
    return normalized


def extract_path_tokens(text: str) -> List[str]:
    """Extract path-like request tokens without checking the filesystem."""
    tokens: List[str] = []
    for line in str(text or "").splitlines():
        for match in re.finditer(
            r"(?P<path>(?:/|\.{0,2}/|[A-Za-z0-9_.-]+/)[^\s`'\"，,、;；。)）\]】]+)",
            line,
        ):
            token = match.group("path").strip().rstrip(".:。；;，,")
            if "/" in token and token not in tokens:
                tokens.append(token)
    return tokens


def canonical_input_role(role: str = "", path: str = "") -> str:
    """Normalize broad discovery roles into reusable data roles."""
    normalized = _field_key(role)
    name = _text(path).lower()
    if normalized in {"variant_vcf", "variant_file", "variants", "vcf"} or re.search(r"\.vcf(?:\.gz)?$|\.bcf$", name):
        return "variant_vcf"
    if normalized in {"reference_fasta", "reference", "genome_fasta", "fasta"} or re.search(r"\.(?:fa|fasta|fna)$", name):
        return "reference_fasta"
    if normalized in {"annotation_gtf", "gtf"} or name.endswith(".gtf"):
        return "annotation_gtf"
    if normalized in {"annotation_gff3", "gff3", "gff"} or re.search(r"\.gff3?$", name):
        return "annotation_gff3"
    if normalized in {"annotation_genbank", "genbank"} or re.search(r"\.(?:gb|gbk|genbank)$", name):
        return "annotation_genbank"
    if normalized == "annotation_file":
        return "annotation_file"
    if normalized in {"alignment_bam", "bam"} or name.endswith(".bam"):
        return "alignment_bam"
    return normalized or generic_artifact_role(path)


def _binding_source(source: str) -> str:
    normalized = _field_key(source)
    if normalized in {"explicit", "user", "user_initial", "user_feedback", "user_explicit"}:
        return "user_explicit"
    if normalized.startswith("planner"):
        return "planner_derived"
    if normalized.startswith("consultant") or normalized in {
        "local_discovery", "directory_discovery", "sidecar_discovery",
    }:
        return "consultant_inferred"
    return normalized or "consultant_inferred"


def _resolved_binding_path(
    raw_path: str,
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> tuple[str, str]:
    path = Path(_text(raw_path)).expanduser()
    if path.is_absolute():
        return str(path.resolve()), "/"
    bases: List[Path] = []
    for value in (project_root, base_dir):
        if value:
            base = Path(value).expanduser().resolve()
            if base not in bases:
                bases.append(base)
    if not bases:
        return str(path), ""
    candidates = [(base / path).resolve() for base in bases]
    selected = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    selected_base = next((base for base, candidate in zip(bases, candidates) if candidate == selected), bases[0])
    return str(selected), str(selected_base)


def normalize_input_binding(
    value: Any,
    *,
    source: str = "consultant_inferred",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {"path": _text(value)}
    path, resolution_base = _resolved_binding_path(
        _text(raw.get("resolved_path") or raw.get("path")),
        base_dir=base_dir,
        project_root=project_root,
    )
    binding_source = _binding_source(_text(raw.get("source") or source))
    verified = bool(raw.get("verified") or _text(raw.get("status")).lower() == "verified")
    evidence = raw.get("verification_evidence") or raw.get("evidence") or []
    return {
        **raw,
        "role": canonical_input_role(_text(raw.get("role")), path),
        "path": path,
        "resolved_path": path,
        "resolution_base": _text(raw.get("resolution_base") or resolution_base),
        "required": bool(raw.get("required", True)),
        "source": binding_source,
        "verified": verified,
        "status": "verified" if verified else _text(raw.get("status") or "declared").lower(),
        "verification_evidence": list(evidence) if isinstance(evidence, list) else [_text(evidence)],
        "verified_at": _text(raw.get("verified_at")),
    }


def normalize_output_binding(
    value: Any,
    *,
    source: str = "user_explicit",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {"filename": _text(value)}
    role = _field_key(raw.get("role"))
    path_value = _text(raw.get("path"))
    filename = _text(raw.get("filename") or (Path(path_value).name if path_value else ""))
    if role == "output_dir" and path_value:
        path_value, resolution_base = _resolved_binding_path(
            path_value,
            base_dir=base_dir,
            project_root=project_root,
        )
    else:
        resolution_base = _text(raw.get("resolution_base"))
    if not role:
        role = _field_key(filename) or "output"
    return {
        **raw,
        "role": role,
        "path": path_value,
        "filename": filename,
        "required": bool(raw.get("required", True)),
        "validation": _text(raw.get("validation") or ("directory" if role == "output_dir" else "nonempty_file")),
        "source": _binding_source(_text(raw.get("source") or source)),
        "resolution_base": resolution_base,
    }


def _request_output_directory(text: str) -> str:
    inline = re.search(
        r"(?:输出目录|output\s*(?:directory|dir))\s*(?:改为|改成|[:：=])\s*([^\s`'\"，,;；。)）\]】]+)",
        str(text or ""),
        re.IGNORECASE,
    )
    if inline:
        return inline.group(1).rstrip(".:")
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:输出目录|output\s*(?:directory|dir))\s*[:：]?\s*(.*?)\s*$", line, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return value.strip("`'\"")
        for candidate in lines[index + 1:]:
            candidate = candidate.strip()
            if candidate:
                return candidate.lstrip("- ").strip("`'\"")
    return ""


def _request_output_filenames(text: str) -> List[str]:
    values: List[str] = []
    file_re = re.compile(r"(?<![/\w.-])([A-Za-z0-9_.-]+\.(?:vcf(?:\.gz)?|tsv|csv|html?|md|json|txt|png|pdf|rds|h5ad|sh))(?![/\w.-])", re.IGNORECASE)
    lines = str(text or "").splitlines()
    in_output_section = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^(?:预期产物|expected\s+outputs?|outputs?)\s*[:：]?", stripped, re.IGNORECASE):
            in_output_section = True
            stripped = re.sub(r"^(?:预期产物|expected\s+outputs?|outputs?)\s*[:：]?", "", stripped, flags=re.IGNORECASE)
        elif in_output_section and re.match(r"^[^\s-][^:：]{0,30}[:：]\s*$", stripped):
            in_output_section = False
        scan = stripped if in_output_section else ""
        if not scan and re.search(r"(?:输出|output|produce|生成)", stripped, re.IGNORECASE):
            scan = re.split(r"(?:输出|output|produce|生成)", stripped, maxsplit=1, flags=re.IGNORECASE)[-1]
        for match in file_re.finditer(scan):
            name = match.group(1)
            if name not in values:
                values.append(name)
    return values


def extract_request_contracts(
    request: str,
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Dict[str, Any]:
    """Extract explicit task bindings without making biological decisions."""
    output_dir_raw = _request_output_directory(request)
    output_dir, _ = _resolved_binding_path(output_dir_raw, base_dir=base_dir, project_root=project_root) if output_dir_raw else ("", "")
    inputs: List[Dict[str, Any]] = []
    supported_input_suffix = re.compile(
        r"\.(?:vcf(?:\.gz)?|bcf|fa|fasta|fna|gtf|gff3?|gb|gbk|genbank|bam|sam|cram|fastq(?:\.gz)?|fq(?:\.gz)?|cool|mcool|rds|h5ad|csv|tsv|txt)$",
        re.IGNORECASE,
    )
    for token in extract_path_tokens(request):
        if not supported_input_suffix.search(token):
            continue
        binding = normalize_input_binding(
            {"path": token, "source": "user_explicit", "required": True},
            base_dir=base_dir,
            project_root=project_root,
        )
        inputs.append(binding)
    requirements: List[Dict[str, Any]] = []
    for item in inputs:
        requirements.append(normalize_requirement(
            {**item, "requirement_id": item["role"], "level": "required", "value": item["path"]},
            default_level="required",
            source="user_explicit",
        ))
    lower = str(request or "").lower()
    format_match = re.search(r"(?<![\w.])-?(gtf22|gff3|genbank)\b", lower)
    if format_match:
        requirements.append(normalize_requirement({
            "requirement_id": "annotation_format",
            "role": "annotation_format",
            "value": format_match.group(1),
            "level": "required",
            "status": "satisfied",
            "source": "user_explicit",
        }))
    if re.search(r"\bcustom[ _-]+genome\b|自定义(?:基因组|参考)", lower):
        requirements.append(normalize_requirement({
            "requirement_id": "database_source",
            "role": "database_source",
            "value": "custom_genome",
            "level": "required",
            "status": "satisfied",
            "source": "user_explicit",
        }))
    if re.search(r"(?:do not|don't|never|禁止|不得|不要).{0,24}(?:guess|猜).{0,24}(?:species|物种).{0,16}(?:database|数据库)", lower):
        requirements.append(normalize_requirement({
            "requirement_id": "forbid_species_database_guess",
            "role": "forbid_species_database_guess",
            "value": True,
            "level": "required",
            "status": "satisfied",
            "source": "user_explicit",
        }))

    expected_outputs = [
        normalize_output_binding({"role": "output_dir", "path": output_dir_raw, "source": "user_explicit"}, base_dir=base_dir, project_root=project_root)
    ] if output_dir_raw else []
    expected_outputs.extend(
        normalize_output_binding({"filename": filename, "source": "user_explicit"})
        for filename in _request_output_filenames(request)
    )
    return {
        "inputs": inputs,
        "requirements": requirements,
        "expected_outputs": expected_outputs,
        "output_contracts": deepcopy(expected_outputs),
    }


def _fact_patch(
    candidates: Iterable[Mapping[str, Any]],
    *,
    source: str,
    revision: int,
    source_message_id: str,
    source_task_id: str,
    migration_source: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    patch: Dict[str, List[Dict[str, Any]]] = {
        "facts": [],
        **{field: [] for field in FACT_FIELDS.values()},
    }
    for candidate in candidates:
        fact = _fact_metadata(
            candidate,
            source=source,
            revision=revision,
            source_message_id=source_message_id,
            source_task_id=source_task_id,
            migration_source=migration_source,
        )
        field = FACT_FIELDS.get(fact["fact_kind"], "unresolved_facts")
        patch[field].append(deepcopy(fact))
        patch["facts"].append(deepcopy(fact))
    return patch


def _fact_group_key(value: Mapping[str, Any]) -> str:
    kind = _field_key(value.get("fact_kind"))
    if kind == "artifact":
        return f"artifact:{_field_key(value.get('direction'))}:{_field_key(value.get('role'))}"
    if kind == "entity":
        return f"entity:{_field_key(value.get('entity_type'))}"
    if kind == "parameter":
        return f"parameter:{_field_key(value.get('namespace'))}:{_field_key(value.get('key'))}"
    if kind == "constraint":
        return f"constraint:{_field_key(value.get('constraint_type'))}:{_text(value.get('text')).lower()}"
    if kind == "decision":
        return f"decision:{_text(value.get('text')).lower()}"
    if kind == "relation":
        return ":".join((
            "relation", _text(value.get("subject")).lower(),
            _field_key(value.get("predicate")), _text(value.get("object")).lower(),
        ))
    return f"unresolved:{_text(value.get('raw_text') or value.get('text')).lower()}"


def _merge_fact_list(current: Iterable[Any], additions: Iterable[Any]) -> List[Any]:
    merged = [deepcopy(item) for item in current if isinstance(item, Mapping)]
    by_id = {_text(item.get("fact_id")): index for index, item in enumerate(merged) if _text(item.get("fact_id"))}
    prepared_additions = [deepcopy(dict(item)) for item in additions if isinstance(item, Mapping)]
    retain_groups: Dict[str, set[str]] = {}
    for item in prepared_additions:
        if item.get("retain_only") and _field_key(item.get("fact_kind")) == "artifact":
            retain_groups.setdefault(_fact_group_key(item), set()).add(_text(item.get("locator")))
    for group, locators in retain_groups.items():
        for existing in merged:
            if (
                _text(existing.get("status") or "active") == "active"
                and _fact_group_key(existing) == group
                and _text(existing.get("locator")) not in locators
            ):
                existing["status"] = "withdrawn"
                existing["active"] = False
                existing["merge_reason"] = "retain_only_user_decision"

    for raw in prepared_additions:
        if not isinstance(raw, Mapping):
            continue
        incoming = deepcopy(dict(raw))
        fact_id = _text(incoming.get("fact_id"))
        if fact_id and fact_id in by_id:
            existing = merged[by_id[fact_id]]
            if SOURCE_PRIORITY.get(_text(incoming.get("source")), 0) >= SOURCE_PRIORITY.get(_text(existing.get("source")), 0):
                existing.update(incoming)
            continue
        if _text(incoming.get("status")) == "withdrawn":
            for existing in merged:
                same_locator = (
                    _field_key(existing.get("fact_kind")) == _field_key(incoming.get("fact_kind")) == "artifact"
                    and _text(existing.get("locator")) == _text(incoming.get("locator"))
                )
                if same_locator and _text(existing.get("status") or "active") == "active":
                    existing["status"] = "withdrawn"
                    existing["active"] = False
                    existing["withdrawn_by"] = fact_id
                    existing["merge_reason"] = "explicit_user_withdrawal"
        elif _text(incoming.get("status") or "active") == "active" and _text(incoming.get("cardinality")) == "one":
            incoming_priority = SOURCE_PRIORITY.get(_text(incoming.get("source")), 0)
            incoming_revision = int(incoming.get("revision_id") or 0)
            superseded: List[str] = []
            for existing in merged:
                if _text(existing.get("status") or "active") != "active":
                    continue
                if _fact_group_key(existing) != _fact_group_key(incoming):
                    continue
                existing_priority = SOURCE_PRIORITY.get(_text(existing.get("source")), 0)
                existing_revision = int(existing.get("revision_id") or 0)
                if incoming_priority < existing_priority or (
                    incoming_priority == existing_priority
                    and incoming_revision < existing_revision
                ):
                    incoming["status"] = "unresolved"
                    incoming["merge_reason"] = "lower_priority_or_older_conflict"
                    continue
                existing["status"] = "superseded"
                existing["active"] = False
                existing["superseded_by"] = fact_id
                existing["merge_reason"] = "newer_single_value_fact"
                if _text(existing.get("fact_id")):
                    superseded.append(_text(existing.get("fact_id")))
            incoming["supersedes"] = sorted(set(list(incoming.get("supersedes") or []) + superseded))
        by_id[fact_id] = len(merged)
        merged.append(incoming)
    return merged


def _parameters_from_candidates(candidates: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for item in candidates:
        if _field_key(item.get("fact_kind")) != "parameter":
            continue
        namespace = _field_key(item.get("namespace"))
        key = _field_key(item.get("key"))
        if key:
            values[f"{namespace}.{key}" if namespace else key] = item.get("value")
    return values


def task_spec_revision(spec: Mapping[str, Any] | None) -> int:
    try:
        return max(0, int((spec or {}).get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def create_task_spec(
    request: str,
    *,
    source: str = "user_initial",
    task_id: str = "",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> TaskSpec:
    request_text = _text(request)
    created_at = _now()
    source_message_id = _message_id(request_text, task_id)
    directive = {
        "text": request_text,
        "kind": "initial_request",
        "source": source,
        "message_origin": "user",
        "source_message_id": source_message_id,
        "source_task_id": _text(task_id),
        "revision": 1,
        "created_revision": 1,
        "confidence": 1.0,
        "active": True,
        "recorded_at": created_at,
    }
    contracts = extract_request_contracts(
        request_text,
        base_dir=base_dir,
        project_root=project_root,
    )
    candidates = extract_user_fact_candidates(request_text)
    facts = _fact_patch(
        candidates,
        source="current_task_user_explicit",
        revision=1,
        source_message_id=source_message_id,
        source_task_id=task_id,
    )
    return {
        "schema_version": TASK_SPEC_SCHEMA_VERSION,
        "task_id": _text(task_id),
        "revision": 1,
        "goal": request_text,
        "user_directives": [directive] if request_text else [],
        "inputs": contracts["inputs"],
        "parameters": _parameters_from_candidates(candidates),
        "output_contracts": contracts["output_contracts"],
        "expected_outputs": contracts["expected_outputs"],
        "must_do": [],
        "must_not_do": [],
        "stop_conditions": [],
        "acceptance_criteria": [],
        "confirmed_decisions": [],
        "unresolved_questions": [],
        "requirements": contracts["requirements"],
        **facts,
        "migrations_applied": [],
        "change_log": [
            {
                "revision": 1,
                "source": source,
                "reason": "initial_request",
                "recorded_at": created_at,
            }
        ],
    }


def ensure_task_spec(
    spec: Mapping[str, Any] | None,
    fallback_request: str = "",
    *,
    task_id: str = "",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> TaskSpec:
    if not isinstance(spec, Mapping) or not spec:
        return create_task_spec(
            fallback_request,
            task_id=task_id,
            base_dir=base_dir,
            project_root=project_root,
        )
    normalized = deepcopy(dict(spec))
    old_schema = int(normalized.get("schema_version") or 1)
    normalized["schema_version"] = max(TASK_SPEC_SCHEMA_VERSION, old_schema)
    normalized["revision"] = max(1, task_spec_revision(normalized))
    normalized.setdefault("goal", _text(fallback_request))
    normalized.setdefault("task_id", _text(task_id))
    for field in _LIST_FIELDS:
        value = normalized.get(field)
        normalized[field] = list(value) if isinstance(value, list) else []
    normalized["parameters"] = (
        dict(normalized.get("parameters") or {})
        if isinstance(normalized.get("parameters"), Mapping)
        else {}
    )
    change_log = normalized.get("change_log")
    normalized["change_log"] = list(change_log) if isinstance(change_log, list) else []
    normalized["requirements"] = [
        normalize_requirement(item, default_level="unknown", source="legacy_task_spec")
        for item in normalized.get("requirements", [])
        if isinstance(item, (Mapping, str))
    ]
    if not normalized["requirements"]:
        normalized["requirements"] = [
            normalize_requirement(item, default_level="unknown", source="legacy_task_input")
            for item in normalized.get("inputs", [])
            if isinstance(item, (Mapping, str))
        ]
    normalized["inputs"] = [
        normalize_input_binding(
            item,
            source=_text(item.get("source")) if isinstance(item, Mapping) else "legacy_task_input",
            base_dir=base_dir,
            project_root=project_root,
        )
        for item in normalized.get("inputs", [])
        if isinstance(item, (Mapping, str))
    ]
    normalized["expected_outputs"] = [
        normalize_output_binding(
            item,
            source=_text(item.get("source")) if isinstance(item, Mapping) else "legacy_task_spec",
            base_dir=base_dir,
            project_root=project_root,
        )
        for item in normalized.get("expected_outputs", [])
        if isinstance(item, (Mapping, str))
    ]
    # Older v4 prototypes may have only the aggregate `facts` list. Distribute
    # it into the typed views before migration and keep `facts` derived below.
    if normalized.get("facts"):
        for value in normalized.get("facts", []):
            if not isinstance(value, Mapping):
                continue
            field = FACT_FIELDS.get(_field_key(value.get("fact_kind")), "unresolved_facts")
            normalized[field] = _merge_fact_list(normalized.get(field, []), [value])
    migrations = list(normalized.get("migrations_applied") or [])
    goal = _text(normalized.get("goal") or fallback_request)
    needs_request_backfill = bool(
        goal
        and (
            old_schema < TASK_SPEC_SCHEMA_VERSION
            or (
                not normalized.get("inputs")
                and not normalized.get("requirements")
                and not normalized.get("expected_outputs")
            )
        )
        and TASK_SPEC_MIGRATION_REQUEST_BACKFILL not in migrations
    )
    if needs_request_backfill:
        backfill = extract_request_contracts(
            goal,
            base_dir=base_dir,
            project_root=project_root,
        )
        for field in ("inputs", "requirements", "expected_outputs", "output_contracts"):
            if not normalized.get(field) and backfill.get(field):
                normalized[field] = deepcopy(backfill[field])
        if TASK_SPEC_MIGRATION_REQUEST_BACKFILL not in migrations:
            migrations.append(TASK_SPEC_MIGRATION_REQUEST_BACKFILL)
        migration_log = list(normalized.get("migration_log") or [])
        migration_log.append({
            "migration": TASK_SPEC_MIGRATION_REQUEST_BACKFILL,
            "source": "task_spec.goal",
            "recorded_at": _now(),
        })
        normalized["migration_log"] = migration_log[-20:]

    migrated_directives = set(normalized.get("migrated_user_directive_ids") or [])
    migration_count = 0
    needs_generic_migration = bool(
        old_schema < TASK_SPEC_SCHEMA_VERSION
        or not any(normalized.get(field) for field in FACT_FIELDS.values())
    )
    directives_for_migration = normalized.get("user_directives", []) if needs_generic_migration else []
    for directive in directives_for_migration or []:
        if not isinstance(directive, Mapping):
            continue
        source = _field_key(directive.get("source"))
        origin = _field_key(directive.get("message_origin"))
        if origin and origin not in USER_MESSAGE_ORIGINS:
            continue
        if source not in {"user", "user_initial", "user_feedback", "user_explicit"}:
            continue
        text = _text(directive.get("text"))
        if not text:
            continue
        message_id = _text(directive.get("source_message_id")) or _message_id(text, _text(normalized.get("task_id") or task_id))
        migration_key = f"{message_id}:{int(directive.get('revision') or directive.get('created_revision') or 1)}"
        if migration_key in migrated_directives:
            continue
        candidates = extract_user_fact_candidates(text)
        patch = _fact_patch(
            candidates,
            source="current_task_user_explicit",
            revision=int(directive.get("revision") or directive.get("created_revision") or 1),
            source_message_id=message_id,
            source_task_id=_text(directive.get("source_task_id") or normalized.get("task_id") or task_id),
            migration_source="legacy_user_directive",
        )
        for field in FACT_FIELDS.values():
            normalized[field] = _merge_fact_list(normalized.get(field, []), patch.get(field, []))
        normalized["parameters"].update(_parameters_from_candidates(candidates))
        migrated_directives.add(migration_key)
        migration_count += 1
    if migration_count:
        migration_log = list(normalized.get("migration_log") or [])
        migration_log.append({
            "migration": TASK_SPEC_MIGRATION_GENERIC_FACTS,
            "migration_source": "legacy_user_directive",
            "directive_count": migration_count,
            "recorded_at": _now(),
        })
        normalized["migration_log"] = migration_log[-20:]
    normalized["migrated_user_directive_ids"] = sorted(migrated_directives)
    if migrated_directives and TASK_SPEC_MIGRATION_GENERIC_FACTS not in migrations:
        migrations.append(TASK_SPEC_MIGRATION_GENERIC_FACTS)
    normalized["facts"] = [
        deepcopy(item)
        for field in FACT_FIELDS.values()
        for item in normalized.get(field, [])
        if isinstance(item, Mapping)
    ]
    normalized["migrations_applied"] = migrations
    return normalized


def normalize_requirement(
    value: Any,
    *,
    default_level: str = "unknown",
    source: str = "contract",
) -> Dict[str, Any]:
    """Normalize one input/validation requirement without domain assumptions."""
    raw = dict(value) if isinstance(value, Mapping) else {"value": _text(value)}
    role = _text(raw.get("role") or raw.get("requirement_id") or raw.get("name"))
    path = _text(raw.get("path"))
    requirement_value = raw.get("value", path)
    level = _text(raw.get("level") or raw.get("requirement_level") or default_level).lower()
    if level not in REQUIREMENT_LEVELS:
        level = "unknown"
    requirement_id = _text(raw.get("requirement_id") or role)
    if not requirement_id:
        requirement_id = _text(Path(path).name if path else requirement_value)
    return {
        **raw,
        "requirement_id": requirement_id or "requirement",
        "role": role or requirement_id or "requirement",
        "path": path,
        "value": requirement_value,
        "level": level,
        "condition": raw.get("condition"),
        "status": _text(raw.get("status") or "unresolved").lower(),
        "source": _text(raw.get("source") or source),
        "evidence": list(raw.get("evidence") or []) if isinstance(raw.get("evidence"), list) else [],
    }


def _requirement_is_present(requirement: Mapping[str, Any]) -> bool:
    path = _text(requirement.get("path"))
    if path:
        try:
            candidate = Path(path).expanduser()
            return candidate.exists() and (not candidate.is_file() or candidate.stat().st_size > 0)
        except OSError:
            return False
    if requirement.get("present") is not None:
        return bool(requirement.get("present"))
    if _text(requirement.get("status")).lower() in {
        "present", "satisfied", "available", "ready", "verified", "completed",
    }:
        return True
    return bool(requirement.get("value") not in (None, "", [], {}))


def evaluate_requirements(requirements: Iterable[Any]) -> Dict[str, Any]:
    """Apply the shared requirement-level policy used by review and recovery."""
    evaluated: List[Dict[str, Any]] = []
    buckets: Dict[str, List[str]] = {
        "missing_required": [],
        "missing_conditional": [],
        "missing_recommended": [],
        "skipped_optional": [],
        "skipped_known_absent": [],
        "unknown": [],
    }
    for value in requirements or []:
        item = normalize_requirement(value)
        requirement_id = str(item["requirement_id"])
        level = str(item["level"])
        condition = item.get("condition")
        condition_active = condition not in (False, 0, "false", "False", "inactive")
        present = _requirement_is_present(item)
        if level == "known_absent":
            item["status"] = "skipped_known_absent"
            buckets["skipped_known_absent"].append(requirement_id)
        elif level == "conditional" and not condition_active:
            item["status"] = "skipped_condition_false"
        elif present:
            item["status"] = "satisfied"
        elif level == "required":
            item["status"] = "missing_required"
            buckets["missing_required"].append(requirement_id)
        elif level == "conditional":
            item["status"] = "missing_conditional"
            buckets["missing_conditional"].append(requirement_id)
        elif level == "recommended":
            item["status"] = "missing_recommended"
            buckets["missing_recommended"].append(requirement_id)
        elif level == "optional_validation":
            item["status"] = "skipped_optional_missing"
            buckets["skipped_optional"].append(requirement_id)
        else:
            item["status"] = "contract_unknown"
            buckets["unknown"].append(requirement_id)
        evaluated.append(item)
    return {
        "requirements": evaluated,
        **buckets,
        "blocked": bool(buckets["missing_required"] or buckets["missing_conditional"]),
        "needs_reconciliation": bool(buckets["unknown"]),
    }


def _item_key(value: Any) -> str:
    if isinstance(value, Mapping):
        for fields in (("path", "role"), ("question", "answer"), ("text", "kind")):
            parts = [_text(value.get(field)) for field in fields]
            if any(parts):
                return "|".join(parts).lower()
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    return _text(value).lower()


def _merge_list(current: Iterable[Any], additions: Iterable[Any]) -> List[Any]:
    merged = [deepcopy(item) for item in current]
    indexes = {_item_key(item): index for index, item in enumerate(merged)}
    for item in additions:
        key = _item_key(item)
        if not key:
            continue
        if key in indexes and isinstance(merged[indexes[key]], dict) and isinstance(item, Mapping):
            existing = merged[indexes[key]]
            incoming = deepcopy(dict(item))
            existing_priority = SOURCE_PRIORITY.get(_binding_source(_text(existing.get("source"))), 0)
            incoming_priority = SOURCE_PRIORITY.get(_binding_source(_text(incoming.get("source"))), 0)
            if existing_priority > incoming_priority:
                protected = {"role", "path", "resolved_path", "value", "source", "required"}
                for field, value in incoming.items():
                    if field not in protected and value not in (None, "", [], {}):
                        existing[field] = value
            else:
                existing.update(incoming)
            continue
        if key not in indexes:
            indexes[key] = len(merged)
            merged.append(deepcopy(item))
    return merged


def merge_task_spec(
    spec: Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
    *,
    source: str,
    reason: str,
    increment_revision: bool = True,
) -> TaskSpec:
    merged = ensure_task_spec(spec)
    updates = dict(patch or {})
    next_revision = task_spec_revision(merged) + (1 if increment_revision else 0)

    if _text(updates.get("goal")):
        merged["goal"] = _text(updates["goal"])
    for field in _LIST_FIELDS:
        additions = updates.get(field)
        if isinstance(additions, list):
            if field == "facts":
                continue
            prepared: List[Any] = []
            for item in additions:
                if not isinstance(item, Mapping):
                    prepared.append(item)
                    continue
                annotated = deepcopy(dict(item))
                annotated.setdefault("source", source)
                annotated.setdefault("confidence", 1.0 if source.startswith("user") else 0.8)
                annotated.setdefault("created_revision", max(1, next_revision))
                annotated.setdefault("active", True)
                if field == "inputs":
                    annotated = normalize_input_binding(annotated, source=source)
                elif field == "requirements":
                    annotated = normalize_requirement(annotated, source=source)
                elif field in {"expected_outputs", "output_contracts"}:
                    annotated = normalize_output_binding(annotated, source=source)
                prepared.append(annotated)
            if field in FACT_FIELDS.values():
                merged[field] = _merge_fact_list(merged.get(field, []), prepared)
            else:
                merged[field] = _merge_list(merged.get(field, []), prepared)
    if source.startswith("user") and isinstance(updates.get("inputs"), list):
        explicit_requirements = [
            normalize_requirement(item, default_level="required", source=source)
            for item in updates["inputs"]
            if isinstance(item, (Mapping, str))
        ]
        merged["requirements"] = _merge_list(
            merged.get("requirements", []),
            explicit_requirements,
        )
    parameters = updates.get("parameters")
    if isinstance(parameters, Mapping):
        merged["parameters"].update(deepcopy(dict(parameters)))

    merged["facts"] = [
        deepcopy(item)
        for field in FACT_FIELDS.values()
        for item in merged.get(field, [])
        if isinstance(item, Mapping)
    ]

    merged["revision"] = max(1, next_revision)
    merged.setdefault("change_log", []).append(
        {
            "revision": merged["revision"],
            "source": source,
            "reason": reason,
            "recorded_at": _now(),
        }
    )
    merged["change_log"] = merged["change_log"][-50:]
    return merged


def merge_task_spec_input_audit(
    spec: Mapping[str, Any] | None,
    input_audit: Mapping[str, Any] | None,
) -> TaskSpec:
    """Persist formal input verification without changing user-owned bindings."""
    normalized = ensure_task_spec(spec)
    audit = dict(input_audit or {})
    resolved = audit.get("resolved_inputs") or []
    if not isinstance(resolved, list):
        return normalized
    known_output_dirs = {
        str(item.get("path") or "")
        for item in normalized.get("expected_outputs", [])
        if isinstance(item, Mapping) and item.get("role") == "output_dir"
    }
    audit_time = _text(
        audit.get("checked_at")
        or normalized.get("input_audit_checked_at")
        or _now()
    )
    additions: List[Dict[str, Any]] = []
    for item in resolved:
        if not isinstance(item, Mapping):
            continue
        path = _text(item.get("resolved_path") or item.get("path"))
        if not path or (canonical_input_role(_text(item.get("role")), path) == "input_directory" and path in known_output_dirs):
            continue
        evidence = item.get("verification_evidence") or []
        if not evidence:
            evidence = [
                label for label, present in (
                    ("exists", item.get("exists")),
                    ("non_empty", item.get("non_empty")),
                    ("readable", item.get("readable")),
                ) if present
            ]
        verified = bool(
            item.get("verified")
            or (
                item.get("exists", True)
                and item.get("readable", True)
                and item.get("non_empty", True)
            )
        )
        additions.append(normalize_input_binding({
            **dict(item),
            "path": path,
            "source": _binding_source(_text(item.get("source"))),
            "verified": verified,
            "status": "verified" if verified else _text(item.get("status") or "unresolved"),
            "verification_evidence": list(evidence),
            "verified_at": _text(item.get("verified_at") or audit_time),
        }))
    if not additions:
        return normalized
    merged = merge_task_spec(
        normalized,
        {"inputs": additions},
        source="consultant_inferred",
        reason="formal_input_audit",
        increment_revision=False,
    )
    if merged.get("inputs") == normalized.get("inputs"):
        return normalized
    merged["input_audit_checked_at"] = audit_time
    return merged


def _renderer_argument_specs(invocation_schema: Mapping[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    schema = dict(invocation_schema or {})
    raw = schema.get("arguments") if isinstance(schema.get("arguments"), Mapping) else schema
    return {
        _field_key(name): dict(value) if isinstance(value, Mapping) else {}
        for name, value in dict(raw or {}).items()
        if _field_key(name)
    }


def _custom_genome_id(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", _text(task_id)).strip("_.-")
    return f"biomind_{slug or 'custom_genome'}"


def project_task_spec_to_renderer(
    spec: Mapping[str, Any] | None,
    invocation_schema: Mapping[str, Any] | None,
    *,
    task_id: str = "",
) -> Dict[str, Any]:
    """Project generic task bindings into a renderer's declared argument schema."""
    normalized = ensure_task_spec(spec)
    argument_specs = _renderer_argument_specs(invocation_schema)
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for item in normalized.get("inputs", []) or []:
        if not isinstance(item, Mapping):
            continue
        binding = normalize_input_binding(item)
        candidates.setdefault(binding["role"], []).append(binding)
    requirements = {
        _field_key(item.get("role") or item.get("requirement_id")): item
        for item in normalized.get("requirements", []) or []
        if isinstance(item, Mapping)
    }
    outputs = {
        _field_key(item.get("role")): item
        for item in normalized.get("expected_outputs", []) or []
        if isinstance(item, Mapping)
    }
    projected: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    unverified: List[str] = []

    def choose(role: str) -> Dict[str, Any]:
        items = list(candidates.get(role, []))
        items.sort(
            key=lambda item: (
                SOURCE_PRIORITY.get(_binding_source(_text(item.get("source"))), 0),
                int(bool(item.get("verified"))),
            ),
            reverse=True,
        )
        if len(items) > 1:
            top_priority = SOURCE_PRIORITY.get(_binding_source(_text(items[0].get("source"))), 0)
            tied = [
                item for item in items
                if SOURCE_PRIORITY.get(_binding_source(_text(item.get("source"))), 0) == top_priority
            ]
            distinct = {str(item.get("path") or "") for item in tied}
            if len(distinct) > 1:
                conflicts.append({"role": role, "paths": sorted(distinct), "reason": "same_priority_bindings"})
                return {}
        return items[0] if items else {}

    for argument, meta in argument_specs.items():
        aliases = {_field_key(value) for value in meta.get("aliases", []) or []}
        aliases.add(argument)
        value: Any = None
        source_detail: Dict[str, Any] = {}
        input_argument = False
        for role, names in INPUT_ROLE_ARGUMENT_CANDIDATES.items():
            if not aliases.intersection({_field_key(name) for name in names}):
                continue
            input_argument = True
            binding = choose(role)
            if binding:
                value = binding.get("path")
                source_detail = {"kind": "input", "role": role, "source": binding.get("source")}
                if not binding.get("verified"):
                    unverified.append(argument)
                break
        if value is None and not input_argument:
            requirement = next((requirements[name] for name in aliases if name in requirements), None)
            if requirement is not None:
                value = requirement.get("value")
                source_detail = {"kind": "requirement", "role": requirement.get("role"), "source": requirement.get("source")}
        if value is None and argument in {"outdir", "output_dir"} and outputs.get("output_dir"):
            value = outputs["output_dir"].get("path")
            source_detail = {"kind": "output", "role": "output_dir", "source": outputs["output_dir"].get("source")}
        if value is None and argument in {"genome_version", "database_id", "genome_id"}:
            database_source = requirements.get("database_source")
            if database_source and database_source.get("value") == "custom_genome":
                value = _custom_genome_id(task_id)
                source_detail = {"kind": "derived", "role": "database_source", "source": "planner_derived"}
        if value not in (None, ""):
            projected[argument] = value
            sources[argument] = source_detail

    outdir = projected.pop("outdir", projected.pop("output_dir", ""))
    missing = [
        argument for argument, meta in argument_specs.items()
        if meta.get("required") and argument not in projected and not (
            argument in {"outdir", "output_dir"} and outdir
        )
    ]
    return {
        "arguments": projected,
        "outdir": outdir,
        "missing_bindings": missing,
        "conflicts": conflicts,
        "unverified_bindings": sorted(set(unverified)),
        "binding_sources": sources,
        "complete": not missing and not conflicts,
        "schema_arguments": sorted(argument_specs),
    }


def apply_user_feedback(
    spec: Mapping[str, Any] | None,
    feedback: str,
    *,
    kind: str,
    question: str = "",
    change_type: TaskChangeType = "requirement_change",
    source_message_id: str = "",
    source_task_id: str = "",
    message_origin: str = "user",
    skill_contract: Mapping[str, Any] | None = None,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> TaskSpec:
    answer = _text(feedback)
    origin = _field_key(message_origin)
    if origin not in USER_MESSAGE_ORIGINS:
        return deepcopy(dict(spec)) if isinstance(spec, Mapping) else ensure_task_spec(spec)
    current_revision = task_spec_revision(spec)
    increments_revision = change_type == "requirement_change"
    created_revision = current_revision + (1 if increments_revision else 0)
    message_id = _text(source_message_id) or _message_id(answer, source_task_id)
    directive = {
        "text": answer,
        "kind": kind,
        "source": "user",
        "change_type": change_type,
        "revision": created_revision,
        "created_revision": created_revision,
        "source_message_id": message_id,
        "source_task_id": _text(source_task_id),
        "message_origin": "user",
        "confidence": 1.0,
        "active": True,
        "recorded_at": _now(),
    }
    candidates = extract_user_fact_candidates(answer)
    if skill_contract:
        candidates = normalize_fact_candidates_with_skill(candidates, skill_contract)
    fact_patch = _fact_patch(
        candidates,
        source="current_task_user_explicit",
        revision=created_revision,
        source_message_id=message_id,
        source_task_id=source_task_id,
    )
    contracts = extract_request_contracts(
        answer,
        base_dir=base_dir,
        project_root=project_root,
    )
    patch: Dict[str, Any] = {
        "user_directives": [directive] if answer else [],
        "inputs": contracts.get("inputs", []),
        "requirements": contracts.get("requirements", []),
        "expected_outputs": contracts.get("expected_outputs", []),
        "output_contracts": contracts.get("output_contracts", []),
        "parameters": _parameters_from_candidates(candidates),
        **fact_patch,
    }
    if _text(question) or answer:
        patch["confirmed_decisions"] = [
            {
                "question": _text(question),
                "answer": answer,
                "source": "user",
                "kind": kind,
                "change_type": change_type,
                "created_revision": created_revision,
                "source_message_id": message_id,
                "source_task_id": _text(source_task_id),
                "message_origin": "user",
                "confidence": 1.0,
                "active": True,
            }
        ]
    return merge_task_spec(
        spec,
        patch,
        source="user",
        reason=f"{kind}:{change_type}",
        increment_revision=increments_revision,
    )


def _effective_fact_sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return a deterministic order for resolved facts and audit output."""
    return (
        _field_key(value.get("fact_kind")),
        _fact_group_key(value),
        _text(value.get("locator")),
        _text(value.get("value")),
        _text(value.get("text")),
        _text(value.get("fact_id")),
    )


def _effective_fact_rank(value: Mapping[str, Any], task_id: str) -> tuple[Any, ...]:
    source = _text(value.get("source"))
    source_task_id = _text(value.get("source_task_id"))
    return (
        SOURCE_PRIORITY.get(source, 0),
        int(bool(task_id and source_task_id == task_id)),
        int(value.get("revision_id") or 0),
        float(value.get("confidence") or 0.0),
        _text(value.get("fact_id")),
    )


def _canonical_fact_payload(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Strip volatile bookkeeping that cannot change the effective meaning."""
    return {
        key: deepcopy(item)
        for key, item in sorted(dict(value).items())
        if key not in {"created_at", "recorded_at", "verified_at"}
    }


def _effective_fact_value_key(value: Mapping[str, Any]) -> str:
    semantic_fields = (
        "fact_kind", "role", "direction", "locator", "artifact_type", "format",
        "entity_type", "namespace", "key", "value", "value_type", "unit",
        "constraint_type", "text", "decision_type", "subject", "predicate",
        "object", "scope", "required",
    )
    payload = {
        field: deepcopy(value.get(field))
        for field in semantic_fields
        if value.get(field) not in (None, "", [], {})
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def resolve_effective_user_facts(spec: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Resolve the active, user-owned facts without projecting them to a node.

    Resolution is deterministic and side-effect free. Single-valued groups use
    source priority before revision; multi-valued groups preserve every unique
    active value. Superseded, withdrawn and unresolved candidates remain
    queryable as audit evidence but never leak into the active views.
    """
    normalized = ensure_task_spec(spec)
    task_id = _text(normalized.get("task_id"))
    typed: Dict[str, List[Dict[str, Any]]] = {
        kind: [
            deepcopy(dict(item))
            for item in normalized.get(field, []) or []
            if isinstance(item, Mapping)
        ]
        for kind, field in FACT_FIELDS.items()
    }
    known_artifact_locators = {
        _text(item.get("locator"))
        for item in typed["artifact"]
        if _text(item.get("locator"))
    }
    known_artifact_roles = {
        _field_key(item.get("role"))
        for item in typed["artifact"]
        if _field_key(item.get("role"))
    }

    def legacy_artifact(
        *,
        role: str,
        locator: str,
        direction: str,
        source: str,
        required: bool,
        verified: bool = False,
    ) -> Dict[str, Any]:
        identity = (task_id, "legacy_binding", role, direction, locator)
        return {
            "fact_kind": "artifact",
            "fact_id": _stable_fact_id(*identity),
            "role": role,
            "direction": direction,
            "locator": locator,
            "artifact_type": "directory" if role == "output_dir" else "file",
            "format": "directory" if role == "output_dir" else _artifact_format(locator),
            "cardinality": "one" if role == "output_dir" else "unknown",
            "required": required,
            "verified": verified,
            "scope": "task",
            "source": _fact_source(source),
            "source_type": _fact_source(source),
            "source_task_id": task_id,
            "source_message_id": "",
            "revision_id": task_spec_revision(normalized),
            "status": "active",
            "supersedes": [],
            "merge_reason": "legacy_binding_projection",
            "confidence": 1.0 if _binding_source(source) == "user_explicit" else 0.8,
        }

    for item in normalized.get("inputs", []) or []:
        if not isinstance(item, Mapping):
            continue
        locator = _text(item.get("resolved_path") or item.get("path"))
        if not locator or locator in known_artifact_locators:
            continue
        typed["artifact"].append(legacy_artifact(
            role=canonical_input_role(_text(item.get("role")), locator),
            locator=locator,
            direction="input",
            source=_text(item.get("source") or "legacy_task_input"),
            required=bool(item.get("required", True)),
            verified=bool(item.get("verified")),
        ))
        known_artifact_locators.add(locator)
    for item in normalized.get("expected_outputs", []) or []:
        if not isinstance(item, Mapping):
            continue
        role = _field_key(item.get("role")) or "output"
        locator = _text(item.get("path") or item.get("filename"))
        if not locator or locator in known_artifact_locators or role in known_artifact_roles:
            continue
        typed["artifact"].append(legacy_artifact(
            role=role,
            locator=locator,
            direction="output",
            source=_text(item.get("source") or "legacy_task_spec"),
            required=bool(item.get("required", True)),
        ))
        known_artifact_locators.add(locator)
        known_artifact_roles.add(role)
    known_constraint_text = {
        _text(item.get("text")) for item in typed["constraint"] if _text(item.get("text"))
    }
    for field, constraint_type in (("must_do", "require"), ("must_not_do", "prohibit")):
        for raw in normalized.get(field, []) or []:
            text = _text(raw.get("text") if isinstance(raw, Mapping) else raw)
            if not text or text in known_constraint_text:
                continue
            typed["constraint"].append({
                "fact_kind": "constraint",
                "fact_id": _stable_fact_id(task_id, "legacy_constraint", constraint_type, text),
                "constraint_type": constraint_type,
                "text": text,
                "scope": "task",
                "cardinality": "many",
                "source": "user_explicit",
                "source_type": "user_explicit",
                "source_task_id": task_id,
                "source_message_id": "",
                "revision_id": task_spec_revision(normalized),
                "status": "active",
                "supersedes": [],
                "merge_reason": "legacy_constraint_projection",
                "confidence": 1.0,
            })
            known_constraint_text.add(text)
    inactive: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    active_by_kind: Dict[str, List[Dict[str, Any]]] = {
        kind: [] for kind in FACT_FIELDS
    }

    for kind, values in typed.items():
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for value in values:
            status = _text(value.get("status") or "active").lower()
            if kind == "unresolved" or status in {"unresolved", "provisional"}:
                unresolved.append(value)
                continue
            if status != "active" or value.get("active") is False:
                inactive.append(value)
                continue
            grouped.setdefault(_fact_group_key(value), []).append(value)

        for group_values in grouped.values():
            group_values.sort(
                key=lambda item: _effective_fact_rank(item, task_id),
                reverse=True,
            )
            cardinality_one = any(
                _text(item.get("cardinality")).lower() == "one"
                for item in group_values
            )
            if cardinality_one:
                active_by_kind[kind].append(group_values[0])
                for shadowed in group_values[1:]:
                    shadowed = deepcopy(shadowed)
                    shadowed["effective_status"] = "shadowed"
                    shadowed["shadowed_by"] = _text(group_values[0].get("fact_id"))
                    inactive.append(shadowed)
                continue
            seen: set[str] = set()
            for item in group_values:
                identity = _effective_fact_value_key(item)
                if identity in seen:
                    duplicate = deepcopy(item)
                    duplicate["effective_status"] = "duplicate"
                    inactive.append(duplicate)
                    continue
                seen.add(identity)
                active_by_kind[kind].append(item)

    for values in active_by_kind.values():
        values.sort(key=_effective_fact_sort_key)
    unresolved.sort(key=_effective_fact_sort_key)
    inactive.sort(key=_effective_fact_sort_key)

    all_active = [
        item
        for kind in ("artifact", "entity", "parameter", "constraint", "decision", "relation")
        for item in active_by_kind[kind]
    ]
    inherited = sorted(
        [
            deepcopy(item)
            for item in all_active
            if _text(item.get("source")) == "inherited_explicit"
            or bool(task_id and _text(item.get("source_task_id")) not in {"", task_id})
        ],
        key=_effective_fact_sort_key,
    )
    source_refs = sorted(
        {
            (
                _text(item.get("fact_id")),
                _text(item.get("source")),
                _text(item.get("source_task_id")),
                _text(item.get("source_message_id")),
                int(item.get("revision_id") or 0),
            )
            for item in all_active + unresolved
        }
    )
    source_ref_payload = [
        {
            "fact_id": fact_id,
            "source": source,
            "source_task_id": source_task_id,
            "source_message_id": source_message_id,
            "revision_id": revision_id,
        }
        for fact_id, source, source_task_id, source_message_id, revision_id in source_refs
    ]
    fingerprint_payload = {
        "task_id": task_id,
        "task_revision": task_spec_revision(normalized),
        "active": [_canonical_fact_payload(item) for item in all_active],
        "unresolved": [_canonical_fact_payload(item) for item in unresolved],
    }
    effective_revision = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "task_id": task_id,
        "task_revision": task_spec_revision(normalized),
        "task_goal": _text(normalized.get("goal")),
        "effective_facts_revision": effective_revision,
        "active_artifacts": active_by_kind["artifact"],
        "active_entities": active_by_kind["entity"],
        "active_parameters": active_by_kind["parameter"],
        "active_constraints": active_by_kind["constraint"],
        "active_decisions": active_by_kind["decision"],
        "active_relations": active_by_kind["relation"],
        "unresolved_facts": unresolved,
        "inherited_facts": inherited,
        "source_refs": source_ref_payload,
        "inactive_facts": inactive,
    }


def _step_projection_tokens(step: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(step, Mapping):
        return set()
    tokens: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                tokens.add(_text(key).lower())
                visit(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
        elif value not in (None, ""):
            tokens.add(_text(value).lower())

    visit(step)
    return tokens


def _artifact_relevant_to_step(
    artifact: Mapping[str, Any],
    step_tokens: set[str],
) -> bool:
    if _text(artifact.get("direction")).lower() == "output":
        return True
    if not step_tokens:
        return True
    candidates = {
        _text(artifact.get("locator")).lower(),
        _text(artifact.get("role")).lower(),
        _text(artifact.get("fact_id")).lower(),
    }
    candidates.discard("")
    return any(
        candidate == token or candidate in token or token in candidate
        for candidate in candidates
        for token in step_tokens
        if len(token) > 2
    )


def project_effective_user_facts(
    resolved_facts: Mapping[str, Any],
    *,
    audience: str,
    step: Mapping[str, Any] | None = None,
    max_chars: int = 12000,
) -> Dict[str, Any]:
    """Build one bounded, valid-JSON node projection from resolved facts.

    The projector drops complete low-priority sections rather than clipping
    values. Active outputs and constraints are mandatory and may exceed the
    requested budget when preserving them is the only faithful option.
    """
    resolved = deepcopy(dict(resolved_facts or {}))
    step_tokens = _step_projection_tokens(step)
    artifacts = [
        deepcopy(dict(item))
        for item in resolved.get("active_artifacts", []) or []
        if isinstance(item, Mapping) and _artifact_relevant_to_step(item, step_tokens)
    ]
    artifacts.sort(key=_effective_fact_sort_key)
    max_chars = max(256, int(max_chars or 12000))
    base: Dict[str, Any] = {
        "schema_version": 1,
        "audience": _field_key(audience) or "node",
        "task_id": _text(resolved.get("task_id")),
        "task_revision": int(resolved.get("task_revision") or 0),
        "effective_facts_revision": _text(resolved.get("effective_facts_revision")),
        "active_artifacts": artifacts,
        "active_constraints": deepcopy(list(resolved.get("active_constraints") or [])),
    }
    optional_sections = (
        ("task_goal", _text(resolved.get("task_goal"))),
        ("active_entities", deepcopy(list(resolved.get("active_entities") or []))),
        ("active_decisions", deepcopy(list(resolved.get("active_decisions") or []))),
        ("active_relations", deepcopy(list(resolved.get("active_relations") or []))),
        ("active_parameters", deepcopy(list(resolved.get("active_parameters") or []))),
        ("unresolved_facts", deepcopy(list(resolved.get("unresolved_facts") or []))),
        ("inherited_facts", deepcopy(list(resolved.get("inherited_facts") or []))),
        ("source_refs", deepcopy(list(resolved.get("source_refs") or []))),
    )
    payload = deepcopy(base)
    omitted: List[str] = []
    for name, value in optional_sections:
        if value in (None, "", [], {}):
            payload[name] = value
            continue
        candidate = {**payload, name: value, "projection_truncated": False, "omitted_sections": []}
        rendered = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if len(rendered) <= max_chars:
            payload[name] = value
        else:
            payload[name] = [] if isinstance(value, list) else ""
            omitted.append(name)

    payload["projection_truncated"] = bool(omitted)
    payload["omitted_sections"] = omitted
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    result = deepcopy(payload)
    result["projection_text"] = rendered
    result["projection_chars"] = len(rendered)
    result["projection_budget"] = max_chars
    return result


def format_effective_user_facts_context(
    spec: Mapping[str, Any] | None,
    *,
    audience: str,
    step: Mapping[str, Any] | None = None,
    max_chars: int = 12000,
) -> str:
    """Render the shared projection with explicit fact/source boundaries."""
    projection = project_effective_user_facts(
        resolve_effective_user_facts(spec),
        audience=audience,
        step=step,
        max_chars=max_chars,
    )
    refs = list(projection.get("source_refs") or [])
    fact_payload = {
        key: deepcopy(value)
        for key, value in projection.items()
        if key not in {"projection_text", "projection_chars", "projection_budget", "source_refs"}
    }
    return (
        "[EFFECTIVE USER FACTS]\n"
        + json.dumps(fact_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        + "\n[USER SOURCE REFERENCES]\n"
        + json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    )


def effective_task_contract(spec: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Project active facts into the legacy contract consumed by QA gates."""
    normalized = ensure_task_spec(spec)
    resolved = resolve_effective_user_facts(normalized)
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    for artifact in resolved["active_artifacts"]:
        locator = _text(artifact.get("locator"))
        if not locator:
            continue
        if _text(artifact.get("direction")).lower() == "output":
            outputs.append(normalize_output_binding({
                "role": artifact.get("role"),
                "path": locator if artifact.get("role") == "output_dir" else "",
                "filename": "" if artifact.get("role") == "output_dir" else locator,
                "required": bool(artifact.get("required", True)),
                "source": artifact.get("source"),
                "fact_id": artifact.get("fact_id"),
            }))
        else:
            inputs.append(normalize_input_binding({
                "role": artifact.get("role"),
                "path": locator,
                "required": bool(artifact.get("required", True)),
                "source": artifact.get("source"),
                "verified": bool(artifact.get("verified")),
                "fact_id": artifact.get("fact_id"),
            }))

    active_input_paths = {_text(item.get("path")) for item in inputs}
    inactive_artifact_paths = {
        _text(item.get("locator"))
        for item in resolved["inactive_facts"]
        if _field_key(item.get("fact_kind")) == "artifact" and _text(item.get("locator"))
    }
    requirements: List[Dict[str, Any]] = []
    for item in normalized.get("requirements", []) or []:
        if not isinstance(item, Mapping):
            continue
        requirement = normalize_requirement(item)
        locator = _text(requirement.get("path") or requirement.get("value"))
        if locator in inactive_artifact_paths and locator not in active_input_paths:
            continue
        requirements.append(requirement)
    known_requirement_paths = {
        _text(item.get("path") or item.get("value")) for item in requirements
    }
    for item in inputs:
        path = _text(item.get("path"))
        if path in known_requirement_paths:
            continue
        requirements.append(normalize_requirement({
            "requirement_id": item.get("role") or Path(path).name,
            "role": item.get("role"),
            "path": path,
            "value": path,
            "level": "required" if item.get("required", True) else "recommended",
            "status": "verified" if item.get("verified") else "unresolved",
            "source": item.get("source"),
            "fact_id": item.get("fact_id"),
        }))
    return {
        "effective_facts_revision": resolved["effective_facts_revision"],
        "inputs": sorted(inputs, key=lambda item: (_text(item.get("role")), _text(item.get("path")))),
        "outputs": sorted(outputs, key=lambda item: (_text(item.get("role")), _text(item.get("path") or item.get("filename")))),
        "requirements": sorted(requirements, key=lambda item: (_text(item.get("role")), _text(item.get("path") or item.get("value")))),
        "parameters": deepcopy(resolved["active_parameters"]),
        "constraints": deepcopy(resolved["active_constraints"]),
        "decisions": deepcopy(resolved["active_decisions"]),
        "source_refs": deepcopy(resolved["source_refs"]),
    }


def classify_user_feedback_change(kind: str, feedback: str) -> TaskChangeType:
    """Classify user feedback without treating every acknowledgement as a new task."""
    normalized_kind = _text(kind).lower()
    text = _text(feedback)
    lower = text.lower()
    if normalized_kind in {"approval", "execute_approval", "continue"}:
        return "approval"
    if normalized_kind in {"execution_feedback", "code_review_feedback"}:
        return "execution_feedback"
    if normalized_kind.endswith("_answer"):
        return "requirement_change"
    requirement_markers = (
        "must ", "must not", "do not", "don't", "only ", "instead", "change ",
        "use ", "skip ", "output ", "input ", "parameter", "threshold", "resolution",
        "contrast", "target", "control", "不得", "不要", "必须", "只做", "改成", "使用",
        "跳过", "输出", "输入", "参数", "阈值", "分辨率", "对比", "目标", "对照",
    )
    path_or_assignment = bool(
        re.search(r"(?:^|\s)(?:/|\.{1,2}/)[^\s]+|\b[A-Za-z_][A-Za-z0-9_.-]*\s*=", text)
    )
    if any(marker in lower for marker in requirement_markers) or path_or_assignment:
        return "requirement_change"
    if "plan" in normalized_kind:
        return "plan_feedback"
    return "informational"


def task_spec_constraints(spec: Mapping[str, Any] | None) -> List[str]:
    """Return explicit effective constraints for legacy conflict detectors."""
    return list(dict.fromkeys(
        _text(item.get("text"))
        for item in resolve_effective_user_facts(spec).get("active_constraints", [])
        if _text(item.get("text"))
    ))


def render_task_spec(spec: Mapping[str, Any] | None, *, max_chars: int = 12000) -> str:
    normalized = ensure_task_spec(spec)
    goal = _text(normalized.get("goal"))
    goal_budget = max(1200, min(len(goal), max_chars // 2))
    if len(goal) > goal_budget:
        omitted = len(goal) - goal_budget
        half = max(1, goal_budget // 2)
        goal = f"{goal[:half]}\n... {omitted} chars omitted from middle ...\n{goal[-half:]}"
    later_directives = [
        item
        for item in normalized.get("user_directives", [])
        if isinstance(item, Mapping) and _text(item.get("kind")) != "initial_request"
    ]
    payload = {
        "revision": normalized["revision"],
        "goal": goal,
        "later_user_directives": later_directives[-6:],
        "inputs": normalized.get("inputs", []),
        "requirements": normalized.get("requirements", []),
        "parameters": normalized.get("parameters", {}),
        "output_contracts": normalized.get("output_contracts", []),
        "expected_outputs": normalized.get("expected_outputs", []),
        "must_do": normalized.get("must_do", []),
        "must_not_do": normalized.get("must_not_do", []),
        "stop_conditions": normalized.get("stop_conditions", []),
        "acceptance_criteria": normalized.get("acceptance_criteria", []),
        "confirmed_decisions": normalized.get("confirmed_decisions", [])[-10:],
        "unresolved_questions": normalized.get("unresolved_questions", []),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) <= max_chars:
        return text
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(compact) <= max_chars:
        return compact

    # Reduce only descriptive/duplicated fields. Hard constraints and output
    # contracts remain intact; callers must report overflow if they alone do
    # not fit rather than receiving partial JSON.
    reduced = deepcopy(payload)
    reduced["goal"] = _middle_clip(_text(reduced.get("goal")), max(400, max_chars // 5))
    reduced["later_user_directives"] = [
        {
            **dict(item),
            "text": _middle_clip(_text(item.get("text")), 500),
        }
        for item in reduced.get("later_user_directives", [])[-4:]
        if isinstance(item, Mapping)
    ]
    reduced["confirmed_decisions"] = reduced.get("confirmed_decisions", [])[-4:]
    reduced["unresolved_questions"] = reduced.get("unresolved_questions", [])[-4:]
    compact = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"), default=str)
    return compact


def _middle_clip(value: str, limit: int) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    marker = f"... {len(text) - limit} chars omitted ..."
    available = max(2, limit - len(marker))
    head = available // 2
    tail = available - head
    return text[:head] + marker + text[-tail:]
