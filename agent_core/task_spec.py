"""Structured, user-owned task state shared by BioMind nodes.

The task spec preserves exact user directives while allowing later answers and
plan-review feedback to supersede earlier assumptions without relying on chat
history. Skill selection and execution state intentionally live elsewhere.
"""

from __future__ import annotations

import json
import os
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

class InputBindingContractError(ValueError):
    """Typed, route-free rejection of an input declaration or its selector."""

    def __init__(self, code: str, *, field_path: str, actual: Any, expected: Any) -> None:
        self.code = str(code)
        self.field_path = str(field_path)
        self.actual = deepcopy(actual)
        self.expected = deepcopy(expected)
        super().__init__(f"{self.code}: {self.field_path}: expected {self.expected}")

    def to_diagnostic(self) -> Dict[str, Any]:
        return {
            "code": self.code, "field_path": self.field_path,
            "actual": deepcopy(self.actual), "expected": deepcopy(self.expected),
            "message": str(self),
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.to_diagnostic()


def validate_input_binding_declaration(value: Mapping[str, Any], *, field_path: str = "$.input") -> None:
    """Validate the scalar input ABI without resolving paths or changing state."""
    if not isinstance(value, Mapping):
        raise InputBindingContractError(
            "INPUT_BINDING_INVALID", field_path=field_path,
            actual={"type": type(value).__name__}, expected="input object",
        )
    for key in ("path", "canonical_path", "resolved_path"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise InputBindingContractError(
                "INPUT_BINDING_PATH_TYPE_INVALID", field_path=f"{field_path}.{key}",
                actual={"type": type(value[key]).__name__, "value": deepcopy(value[key])},
                expected="one scalar string path; use separate input_instance records for collection members",
            )
    if "input_instance" not in value:
        return
    instance = value["input_instance"]
    instance_path = f"{field_path}.input_instance"
    required = {"schema_version", "group_id", "member_id"}
    if not isinstance(instance, Mapping) or set(instance) != required:
        raise InputBindingContractError(
            "INPUT_INSTANCE_INVALID", field_path=instance_path,
            actual=deepcopy(instance), expected={"schema_version": 1, "group_id": "non-empty string", "member_id": "non-empty string"},
        )
    if type(instance["schema_version"]) is not int or instance["schema_version"] != 1:
        raise InputBindingContractError(
            "INPUT_INSTANCE_SCHEMA_UNSUPPORTED", field_path=f"{instance_path}.schema_version",
            actual=instance["schema_version"], expected=1,
        )
    for key in ("group_id", "member_id"):
        if not isinstance(instance[key], str) or not instance[key].strip():
            raise InputBindingContractError(
                "INPUT_INSTANCE_INVALID", field_path=f"{instance_path}.{key}",
                actual=deepcopy(instance[key]), expected="non-empty semantic string declared by the caller",
            )


def _input_instance_key(value: Mapping[str, Any]) -> tuple[str, str] | None:
    instance = value.get("input_instance")
    if not isinstance(instance, Mapping):
        return None
    return (str(instance.get("group_id") or "").strip(), str(instance.get("member_id") or "").strip())


REQUIREMENT_LEVELS = {
    "required",
    "conditional",
    "recommended",
    "optional_validation",
    "known_absent",
    "unknown",
}

# These closed vocabularies are retained only for explicit typed machine
# policies and read-old compatibility.  Arbitrary natural-language meaning is
# canonical open text in ``TaskSpec.requirements``; the Controller no longer
# claims to prove prose compliance by admitting it to this registry.
SEMANTIC_CONSTRAINT_CATEGORIES = {
    "required_input",
    "required_output",
    "required_skill",
    "required_behavior",
    "forbidden_behavior",
    "preference",
    "conditional_policy",
    "failure_policy",
    "runtime_constraint",
}
SEMANTIC_CONSTRAINT_OPERATORS = {
    "eq", "neq", "contains", "forbid", "require", "prefer", "on_condition",
}
SEMANTIC_CONSTRAINT_STRENGTHS = {
    "hard", "required", "preference", "conditional", "advisory", "ambiguous",
}
SEMANTIC_CONSTRAINT_ENFORCEMENT_SCOPES = {
    "plan", "runtime_global", "context_only",
}

# Consultant output is allowed to use a very small compatibility surface.  The
# controller canonicalizes these aliases before schema validation, records the
# rewrite, and never asks the model to reinterpret an already valid key.
SEMANTIC_CONSTRAINT_CATEGORY_ALIASES = {
    "forbidden": "forbidden_behavior",
    "runtime": "runtime_constraint",
}
SEMANTIC_CONSTRAINT_ONTOLOGY_ALIASES = {
    "execution.output_dir": {
        "semantic_key": "required_output.output_dir",
    },
    "param.gtf22": {
        "semantic_key": "required_behavior.annotation_format",
        "value": "gtf22",
    },
    "forbidden.public_db_guess": {
        "semantic_key": "forbidden_behavior.species_database_guess",
        "value": True,
    },
    "forbidden.demo_data": {
        "semantic_key": "forbidden_behavior.demo_or_toy_input",
        "value": True,
    },
    "forbidden_behavior.no_public_db_guess": {
        "semantic_key": "forbidden_behavior.species_database_guess",
        "value": True,
    },
    "forbidden_behavior.no_demo_data": {
        "semantic_key": "forbidden_behavior.demo_or_toy_input",
        "value": True,
    },
    "runtime.env": {
        "semantic_key": "runtime_constraint.environment",
    },
}

TASK_SPEC_SCHEMA_VERSION = 6
TASK_CANONICALIZATION_VERSION = "task-c14n-v3"
TASK_SPEC_MUTATION_SCHEMA_VERSION = 1
TASK_SPEC_MUTATION_OPERATIONS = {
    "set_goal",
    "upsert_input",
    "remove_input",
    "upsert_constraint",
    "remove_constraint",
    "upsert_output_contract",
    "remove_output_contract",
    "confirm_decision",
    "remove_decision",
    "mark_ambiguity",
}
TASK_SPEC_MIGRATION_REQUEST_BACKFILL = "task_spec_v3_request_backfill"
TASK_SPEC_MIGRATION_GENERIC_FACTS = "task_spec_v4_generic_user_facts"
SOURCE_PRIORITY = {
    "current_task_user_explicit": 90,
    "current_task_user_confirmed": 80,
    "inherited_explicit": 70,
    "benchmark_contract": 60,
    "benchmark_question": 50,
    "user_explicit": 40,
    "user_initial": 40,
    "user_feedback": 40,
    "user": 40,
    "consultant_inferred": 30,
    "consultant": 30,
    "runtime_detected": 25,
    "runtime_evidence": 25,
    "controller_evidence": 25,
    "archive_retrieved": 15,
    "skill_default": 10,
    "skill_catalog": 10,
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

_CANONICAL_TASK_LIST_FIELDS = (
    "inputs",
    "output_contracts",
    "confirmed_decisions",
    "unresolved_questions",
    "requirements",
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


def _semantic_key(value: Any) -> str:
    """Normalize a schema key, never a natural-language sentence."""
    raw = _text(value).lower().strip(".")
    if "." not in raw:
        return ""
    category, slot = raw.split(".", 1)
    category = _field_key(category)
    slot = ".".join(_field_key(part) for part in slot.split(".") if _field_key(part))
    if category not in SEMANTIC_CONSTRAINT_CATEGORIES or not slot:
        return ""
    return f"{category}.{slot}"


def semantic_constraint_enforcement_scope(value: Mapping[str, Any] | Any) -> str:
    """Return the controller-owned enforcement layer for one constraint.

    Older artifacts do not carry this field.  Their scope is inferred from the
    closed semantic ontology so replaying them does not make anonymous prose
    constraints into mandatory plan-step bindings again.
    """
    raw = dict(value) if isinstance(value, Mapping) else {}
    declared = _field_key(raw.get("enforcement_scope"))
    if declared in SEMANTIC_CONSTRAINT_ENFORCEMENT_SCOPES:
        return declared
    semantic_key = _semantic_key(raw.get("semantic_key"))
    category, _, slot = semantic_key.partition(".")
    strength = _field_key(raw.get("strength"))
    if category == "preference" or strength in {"preference", "advisory"}:
        return "context_only"
    if category in {
        "failure_policy", "runtime_constraint", "conditional_policy",
        "forbidden_behavior",
    }:
        return "runtime_global"
    if (
        category in {"required_behavior", "forbidden_behavior"}
        and re.fullmatch(r"(?:user_)?directive_[a-f0-9]{8,64}", slot)
    ):
        return "runtime_global"
    return "plan"


def constraint_requires_plan_projection(value: Mapping[str, Any] | Any) -> bool:
    """Whether Planner must bind this hard constraint to an exact plan step."""
    if not isinstance(value, Mapping):
        return False
    strength = _field_key(value.get("strength"))
    if strength not in SEMANTIC_CONSTRAINT_STRENGTHS:
        level = _field_key(value.get("level"))
        strength = "hard" if level == "required" else ""
    return bool(
        strength in {"hard", "required"}
        and not value.get("needs_clarification")
        and semantic_constraint_enforcement_scope(value) == "plan"
    )


def canonicalize_compiled_constraint(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Canonicalize only declared ontology aliases and retain an audit trail."""
    raw = deepcopy(dict(value))
    original_key = _text(raw.get("semantic_key")).lower().strip(".")
    normalized_parts = [
        _field_key(part)
        for part in original_key.split(".")
        if _field_key(part)
    ]
    alias_key = ".".join(normalized_parts)
    rule_id = ""
    canonical_key = ""

    exact = SEMANTIC_CONSTRAINT_ONTOLOGY_ALIASES.get(alias_key)
    if exact:
        canonical_key = _text(exact.get("semantic_key"))
        if "value" in exact:
            raw["value"] = deepcopy(exact["value"])
        rule_id = "ontology-alias-v1"
    elif "." in alias_key:
        category, slot = alias_key.split(".", 1)
        canonical_category = SEMANTIC_CONSTRAINT_CATEGORY_ALIASES.get(category)
        if canonical_category and slot:
            canonical_key = f"{canonical_category}.{slot}"
            rule_id = "category-alias-v1"

    if canonical_key:
        raw["semantic_key"] = canonical_key
        raw["normalization_audit"] = {
            "original_key": original_key,
            "canonical_key": canonical_key,
            "rule_id": rule_id,
        }
    return raw


def _bounded_confidence(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


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
    path_mentions = classify_path_mentions(raw)
    path_tokens = [item["requested_path"] for item in path_mentions if item["classification"] in {"input", "reference"}]
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
    for mention in path_mentions:
        if mention["classification"] != "forbidden":
            continue
        locator = mention["requested_path"]
        candidates.append({
            "fact_kind": "artifact",
            "role": generic_artifact_role(locator, raw),
            "direction": "input",
            "locator": locator,
            "artifact_type": "file",
            "format": _artifact_format(locator),
            "cardinality": "unknown",
            "classification": "forbidden",
            "required": False,
            "scope": "task",
            "status": "withdrawn",
            "source_ref": mention.get("source_ref", ""),
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

    # The complete utterance remains in ``user_directives`` as provenance.
    # It is deliberately not promoted to one constraint/decision fact based on
    # words such as "must", "prefer", or "missing".  Consultant compiles those
    # meanings into typed TaskSpec requirements instead.

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


def classify_path_mentions(text: str) -> List[Dict[str, Any]]:
    """Classify slash tokens before they can become active input contracts."""
    mentions: List[Dict[str, Any]] = []
    negative = re.compile(r"(?:禁止|不要|不得|不能使用|不允许|do\s+not\s+use|must\s+not\s+use|never\s+use)", re.IGNORECASE)
    example = re.compile(r"(?:例如|示例|example|e\.g\.)", re.IGNORECASE)
    control_prefixes = ("ask_user/", "blocked_", "stage_blocked/", "finish/", "execute/")
    skill_pattern = re.compile(r"^[a-z][a-z0-9_-]+/[a-z][a-z0-9_.-]+$")
    section_classification = ""
    for line_no, line in enumerate(str(text or "").splitlines(), start=1):
        stripped_line = line.strip().lstrip("#").strip()
        if re.match(r"^(?:输入文件(?:或数据位置)?|输入数据|数据位置|inputs?)\s*[:：]?\s*$", stripped_line, re.IGNORECASE):
            section_classification = "input"
        elif re.match(r"^(?:输出目录|输出文件|预期产物|expected\s+outputs?|outputs?)\s*[:：]?\s*$", stripped_line, re.IGNORECASE):
            section_classification = "output"
        elif re.match(r"^(?:禁止|禁用|forbidden|must\s+not)\s*[:：]?\s*$", stripped_line, re.IGNORECASE):
            section_classification = "forbidden"
        elif re.match(
            r"^(?:允许|依赖规则|停止条件|通用要求|说明|allow(?:ed)?|dependency\s+rules?|stop\s+conditions?|general\s+requirements?)\s*[:：]?\s*$",
            stripped_line,
            re.IGNORECASE,
        ):
            section_classification = "control_token"
        for match in re.finditer(
            r"(?P<path>(?:/|\.{0,2}/|[A-Za-z0-9_.-]+/)[^\s`'\"，,、;；。)）\]】]+)",
            line,
        ):
            token = match.group("path").strip().rstrip(".:。；;，,")
            prefix = line[:match.start()]
            clause_prefix = re.split(r"[。.!！？?；;，,]", prefix)[-1]
            lower = token.lower()
            if not _looks_like_path_mention(token):
                continue
            classification = section_classification or "input"
            if re.search(r"(?:输出目录|output\s*(?:directory|dir))\s*[:：=]?\s*$", clause_prefix, re.IGNORECASE):
                classification = "output"
            elif lower.startswith(control_prefixes) or lower in {"benchmark/hidden/fixture", "benchmark/hidden/fixtures"}:
                classification = "control_token"
            elif skill_pattern.fullmatch(lower) and not re.search(r"\.[a-z0-9]{1,8}$", lower):
                classification = "skill_id"
            elif negative.search(clause_prefix):
                classification = "forbidden"
            elif example.search(clause_prefix):
                classification = "example"
            mentions.append({
                "path_mention_id": "path-" + hashlib.sha256(f"{line_no}|{match.start()}|{token}".encode("utf-8")).hexdigest()[:16],
                "classification": classification,
                "required": classification in {"input", "reference"},
                "resolution_status": "unresolved",
                "requested_path": token,
                "resolved_path": "",
                "source_ref": f"request:line:{line_no}",
            })
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for mention in mentions:
        key = (mention["classification"], mention["requested_path"])
        if key not in seen:
            seen.add(key)
            deduped.append(mention)
    return deduped


def _looks_like_path_mention(token: str) -> bool:
    """Reject prose containing a slash while retaining ordinary filesystem paths."""
    value = _text(token).strip()
    lower = value.lower()
    if not value:
        return False
    if lower in {"benchmark/hidden/fixture", "benchmark/hidden/fixtures"}:
        return True
    if re.fullmatch(r"[a-z][a-z0-9_-]+/[a-z][a-z0-9_.-]+", lower):
        return True
    if value.startswith(("/", "./", "../", "~/")):
        # Chinese prose such as `/目标` and `/验证本地` is not an absolute path.
        if value.startswith("/") and value.count("/") == 1 and re.fullmatch(r"/[\u3400-\u9fff]+", value):
            return False
        return True
    if re.search(r"\.[A-Za-z0-9][A-Za-z0-9.]{0,9}(?:$|[?#])", value):
        return True
    if value.count("/") >= 2:
        return True
    first = lower.split("/", 1)[0]
    return first in {
        "data", "input", "inputs", "output", "outputs", "result", "results",
        "ref", "reference", "references", "annotation", "annotations", "genome",
        "metadata", "benchmarks", "objects", "tables", "plots", "scripts", "task_outputs",
    }


def extract_path_tokens(text: str) -> List[str]:
    """Return only positive input/reference path mentions."""
    return [
        item["requested_path"]
        for item in classify_path_mentions(text)
        if item["classification"] in {"input", "reference"}
    ]


def canonical_input_role(role: str = "", path: str = "") -> str:
    """Normalize broad discovery roles into reusable data roles."""
    normalized = _field_key(role)
    name = _text(path).lower()
    if normalized in {"variant_vcf", "variant_file", "variants", "vcf"}:
        return "variant_vcf"
    if normalized in {"reference_fasta", "reference", "genome_fasta", "fasta"}:
        return "reference_fasta"
    if normalized in {"annotation_gtf", "gtf"}:
        return "annotation_gtf"
    if normalized in {"annotation_gff3", "gff3", "gff"}:
        return "annotation_gff3"
    if normalized in {"annotation_genbank", "genbank"}:
        return "annotation_genbank"
    if normalized == "annotation_file":
        # A discovery audit may report the broad role while the canonical
        # request parser has already specialized the same artifact.  Use the
        # file format when it is decisive so verification updates the existing
        # binding instead of creating an unverified parallel identity.
        if name.endswith(".gtf"):
            return "annotation_gtf"
        if re.search(r"\.gff3?$", name):
            return "annotation_gff3"
        if re.search(r"\.(?:gb|gbk|genbank)$", name):
            return "annotation_genbank"
        return "annotation_file"
    if normalized in {"alignment_bam", "bam"}:
        return "alignment_bam"
    if normalized not in {"", "input", "input_file", "file"}:
        return normalized
    if re.search(r"\.vcf(?:\.gz)?$|\.bcf$", name):
        return "variant_vcf"
    if re.search(r"\.(?:fa|fasta|fna)$", name):
        return "reference_fasta"
    if name.endswith(".gtf"):
        return "annotation_gtf"
    if re.search(r"\.gff3?$", name):
        return "annotation_gff3"
    if re.search(r"\.(?:gb|gbk|genbank)$", name):
        return "annotation_genbank"
    if name.endswith(".bam"):
        return "alignment_bam"
    return generic_artifact_role(path)


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


def _binding_provenance(raw: Mapping[str, Any], fallback_source: str) -> List[Dict[str, str]]:
    """Normalize all known sources for one binding without losing legacy fields."""
    values = raw.get("provenance") if isinstance(raw.get("provenance"), list) else []
    result: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in [*values, raw]:
        if not isinstance(value, Mapping):
            continue
        source_type = _binding_source(_text(value.get("source_type") or value.get("source") or fallback_source))
        source_id = _text(value.get("source_id") or value.get("source_ref"))
        source_ref = _text(value.get("source_ref") or source_id)
        key = (source_type, source_id or source_ref)
        if source_type and key not in seen:
            seen.add(key)
            item = {"source_type": source_type, "source_ref": source_ref}
            if source_id:
                item["source_id"] = source_id
            result.append(item)
    return result


def _resolved_binding_path(
    raw_path: str,
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> tuple[str, str]:
    path = Path(_text(raw_path)).expanduser()
    # Path identity follows the declared project, never a cwd existence search.
    # Preserve symlink/wrapper spelling; access checks resolve targets separately.
    if path.is_absolute():
        return os.path.abspath(path), "/"
    base_value = project_root or base_dir
    if not base_value:
        return str(path), ""
    base = Path(os.path.abspath(Path(base_value).expanduser()))
    return os.path.abspath(base / path), str(base)


def _artifact_identity(direction: str, role: str, canonical_path: str) -> str:
    """Return a stable TaskSpec artifact identity, independent of provenance."""
    payload = json.dumps(
        {
            "direction": _field_key(direction),
            "role": _field_key(role),
            "canonical_path": _text(canonical_path),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "artifact-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _artifact_semantic_handle(direction: str, role: str, fallback: str) -> str:
    slot = re.sub(r"[^a-z0-9_.-]+", "_", _text(role).lower()).strip("_.-")
    return f"{direction}.{slot or fallback}"


def _nonempty_fields(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Prune optional record fields without losing meaningful False values."""
    return {
        str(key): deepcopy(item)
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def artifact_locator(value: Mapping[str, Any] | None) -> str:
    """Read the canonical artifact location with legacy aliases as fallback."""
    raw = dict(value or {})
    return _text(
        raw.get("canonical_path")
        or raw.get("resolved_path")
        or raw.get("path_or_name")
        or raw.get("path")
        or raw.get("filename")
    )


def normalize_input_binding(
    value: Any,
    *,
    source: str = "consultant_inferred",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {"path": value}
    validate_input_binding_declaration(raw, field_path="$")
    if "input_instance" in raw and not any(
        isinstance(raw.get(key), str) and raw[key].strip()
        for key in ("path", "canonical_path", "resolved_path")
    ):
        raise InputBindingContractError(
            "INPUT_BINDING_PATH_MISSING", field_path="$.path",
            actual=None, expected="a non-empty scalar path for this input instance",
        )
    path, resolution_base = _resolved_binding_path(
        _text(raw.get("canonical_path") or raw.get("resolved_path") or raw.get("path")),
        base_dir=base_dir,
        project_root=project_root,
    )
    binding_source = _binding_source(_text(raw.get("source") or source))
    verified = bool(raw.get("verified") or _text(raw.get("status")).lower() == "verified")
    evidence = raw.get("verification_evidence") or raw.get("evidence") or []
    provenance = _binding_provenance(raw, binding_source)
    declared_role = _text(raw.get("role")) or generic_artifact_role(path)
    role = canonical_input_role(declared_role, path)
    artifact_id = _text(raw.get("artifact_id") or raw.get("input_id"))
    if not artifact_id:
        artifact_id = _artifact_identity("input", role, path)
    semantic_handle = _text(raw.get("semantic_handle")) or _artifact_semantic_handle(
        "input", role, "input"
    )
    input_instance = None
    if "input_instance" in raw:
        input_instance = {
            "schema_version": 1,
            "group_id": raw["input_instance"]["group_id"].strip(),
            "member_id": raw["input_instance"]["member_id"].strip(),
        }
        instance_digest = hashlib.sha256(json.dumps(
            {"role": role, **input_instance}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:16]
        # Explicit group/member semantics select the instance. The system
        # forms a stable handle, independent of path and context list order.
        semantic_handle = _artifact_semantic_handle("input", role, "input") + "." + instance_digest
    file_format = _text(raw.get("format") or raw.get("type") or _artifact_format(path))
    path_policy = _field_key(raw.get("path_policy")) or (
        "managed_exact" if path else "model_selectable"
    )
    normalized = _nonempty_fields({
        "artifact_id": artifact_id,
        "semantic_handle": semantic_handle,
        "direction": "input",
        "input_instance": input_instance,
        "role": role,
        "format": file_format,
        "path_policy": path_policy,
        "canonical_path": path,
        "required_basename": Path(path).name if path else "",
        "resolution_base": _text(raw.get("resolution_base") or resolution_base),
        "required": bool(raw.get("required", True)),
        "source": binding_source,
        "source_type": binding_source,
        "source_ref": _text(raw.get("source_ref") or raw.get("source_id")),
        "source_id": _text(raw.get("source_id") or raw.get("source_ref")),
        "provenance": provenance,
        "verified": verified,
        "status": "verified" if verified else _text(raw.get("status") or "declared").lower(),
        "verification_evidence": list(evidence) if isinstance(evidence, list) else [_text(evidence)],
        "verified_at": _text(raw.get("verified_at")),
        "validation_contract_id": _text(
            raw.get("validation_contract_id") or raw.get("validator")
        ),
        "description": _text(raw.get("description")),
        "aliases": list(raw.get("aliases") or []),
    })
    # A legacy identity may still be read during the migration window, but new
    # bindings never create a second identity field.
    if raw.get("input_id"):
        normalized["input_id"] = _text(raw.get("input_id"))
    return normalized


def normalize_output_binding(
    value: Any,
    *,
    source: str = "user_explicit",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {"filename": _text(value)}
    role = _field_key(raw.get("role"))
    path_value = _text(raw.get("canonical_path") or raw.get("path"))
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
        role = _field_key(raw.get("output_id")) or _field_key(filename) or "output"
    source_value = _binding_source(_text(raw.get("source_type") or raw.get("source") or source))
    path_or_name = path_value or filename
    output_id = _field_key(raw.get("output_id") or raw.get("artifact_id") or role)
    artifact_id = _text(raw.get("artifact_id")) or output_id
    semantic_handle = _text(raw.get("semantic_handle")) or _artifact_semantic_handle(
        "output", output_id or role, "output"
    )
    artifact_kind = _field_key(raw.get("artifact_kind") or raw.get("kind"))
    if artifact_kind not in {"file", "directory"}:
        artifact_kind = "directory" if role == "output_dir" else "file"
    file_format = _text(raw.get("format") or raw.get("type"))
    if not file_format:
        file_format = (
            "directory"
            if artifact_kind == "directory"
            else (_artifact_format(path_or_name) if path_or_name else "file")
        )
    root_ref = _field_key(raw.get("root_ref") or raw.get("output_root_ref"))
    path_policy = _field_key(raw.get("path_policy")) or (
        "managed_exact"
        if path_or_name
        else ("planner_materialized" if root_ref else "model_selectable")
    )
    provenance = _binding_provenance(raw, source_value)
    normalized = _nonempty_fields({
        "artifact_id": artifact_id,
        "output_id": output_id,
        "semantic_handle": semantic_handle,
        "direction": "output",
        "role": role,
        "artifact_kind": artifact_kind,
        "format": file_format,
        "path_policy": path_policy,
        "canonical_path": path_or_name,
        "filename": filename,
        "root_ref": root_ref,
        "required_basename": filename or (Path(path_value).name if path_value else ""),
        "required": bool(raw.get("required", True)),
        "validation_contract_id": _text(
            raw.get("validation_contract_id")
            or raw.get("validator")
            or raw.get("validation")
            or ("directory-v1" if artifact_kind == "directory" else "nonempty-file-v1")
        ),
        "source": source_value,
        "source_type": source_value,
        "source_ref": _text(raw.get("source_ref") or raw.get("source_id")),
        "source_id": _text(raw.get("source_id") or raw.get("source_ref")),
        "provenance": provenance,
        "user_visible": bool(raw.get("user_visible", role != "output_dir")),
        "producer_owner": _text(raw.get("producer_owner") or raw.get("producer_hint") or "plan_step"),
        "producer_step_id": _text(raw.get("producer_step_id")),
        # Conditional deliverables are a contract property, never something
        # CompletionGate may infer from plan prose.  Members carrying the same
        # non-empty group are interpreted as a required one-of set.
        "alternative_group": _text(
            raw.get("alternative_group")
            or raw.get("one_of_group")
            or raw.get("requirement_group")
        ),
        "relocatable": bool(raw.get("relocatable", False)),
        "resolution_base": resolution_base,
        "description": _text(raw.get("description")),
        "aliases": list(raw.get("aliases") or []),
    })
    if "resource_kind" in raw:
        kind = raw["resource_kind"]
        if kind not in {"immutable_artifact", "mutable_workspace"}:
            raise ValueError("TASK_RESOURCE_KIND_INVALID: declare immutable_artifact or mutable_workspace")
        if kind == "mutable_workspace" and artifact_kind != "directory":
            raise ValueError("TASK_RESOURCE_KIND_INVALID: mutable_workspace requires an explicit directory")
        normalized["resource_kind"] = kind
        normalized["contract_revision"] = _text(raw.get("contract_revision") or "task-resource-v2")
        normalized["contract_source"] = _text(raw.get("contract_source") or "TaskSpec.output_contracts")
    return normalized


def effective_output_contracts(
    spec: Mapping[str, Any] | None,
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> List[Dict[str, Any]]:
    """Return the single canonical output contract view.

    ``expected_outputs`` is accepted only as a legacy migration source when
    the authoritative ``output_contracts`` collection is absent.
    """
    raw = dict(spec or {})
    values = raw.get("output_contracts")
    migration_source = "output_contracts"
    if not isinstance(values, list) or not values:
        values = raw.get("expected_outputs")
        migration_source = "legacy_expected_outputs"
    values = values if isinstance(values, list) else []
    normalized: List[Dict[str, Any]] = []
    indexes: Dict[str, int] = {}
    for value in values:
        if not isinstance(value, (Mapping, str)):
            continue
        item = normalize_output_binding(
            value,
            source=(
                _text(value.get("source_type") or value.get("source"))
                if isinstance(value, Mapping)
                else migration_source
            ) or migration_source,
            base_dir=base_dir,
            project_root=project_root,
        )
        key = _field_key(item.get("output_id") or item.get("artifact_id") or item.get("role"))
        if key in indexes:
            existing = normalized[indexes[key]]
            existing["required"] = bool(existing.get("required", True) or item.get("required", True))
            existing["provenance"] = _binding_provenance(
                {"provenance": [*(existing.get("provenance") or []), *(item.get("provenance") or [])]},
                _text(existing.get("source_type") or existing.get("source")),
            )
            continue
        indexes[key] = len(normalized)
        if migration_source != "output_contracts":
            item.setdefault("migration_source", migration_source)
        normalized.append(item)
    return normalized


def validate_task_spec_output_contracts(
    spec: Mapping[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Validate Consultant-proposed output semantics before FINISH.

    This validator consumes only typed Consultant state.  It deliberately does
    not scan the user transcript or infer output meaning from path/role words.
    """
    raw = dict(spec or {})
    outputs = effective_output_contracts(raw)
    violations: List[Dict[str, Any]] = []

    typed_output_dir_claim = False
    for field_name in ("semantic_constraints", "requirements"):
        for row in raw.get(field_name, []) or []:
            if not isinstance(row, Mapping):
                continue
            semantic_key = _semantic_key(row.get("semantic_key"))
            value = row.get("value")
            if semantic_key == "output_dir" or semantic_key.endswith(".output_dir"):
                if value not in (None, "", [], {}):
                    typed_output_dir_claim = True

    output_dir = next(
        (
            row for row in outputs
            if _field_key(row.get("output_id") or row.get("role")) == "output_dir"
            or _field_key(row.get("role")) == "output_dir"
        ),
        None,
    )
    if typed_output_dir_claim and (
        output_dir is None
        or _field_key(output_dir.get("artifact_kind")) != "directory"
        or not _text(output_dir.get("canonical_path"))
    ):
        violations.append({
            "code": "TASK_OUTPUT_DIR_NOT_CANONICALIZED",
            "owner": "CONSULTANT_CANDIDATE",
            "route": "consultant_repair",
            "message": (
                "The typed Consultant semantics contain an explicit output_dir, "
                "but the candidate has no canonical directory binding for it."
            ),
            "field": "output_contracts",
        })

    seen_ids: set[str] = set()
    for index, row in enumerate(outputs):
        output_id = _field_key(row.get("output_id"))
        artifact_kind = _field_key(row.get("artifact_kind"))
        file_format = _field_key(row.get("format"))
        path = _text(row.get("canonical_path"))
        root_ref = _field_key(row.get("root_ref"))
        path_policy = _field_key(row.get("path_policy"))
        required_basename = _text(row.get("required_basename"))

        if output_id in seen_ids:
            violations.append({
                "code": "TASK_OUTPUT_ID_DUPLICATE",
                "owner": "CONSULTANT_CANDIDATE",
                "route": "consultant_repair",
                "message": f"output_id {output_id!r} is declared more than once.",
                "field": f"output_contracts[{index}].output_id",
            })
        seen_ids.add(output_id)

        if (
            (artifact_kind == "file" and file_format == "directory")
            or (artifact_kind == "directory" and file_format not in {"", "directory", "dir"})
            or (_field_key(row.get("role")) == "output_dir" and artifact_kind != "directory")
        ):
            violations.append({
                "code": "TASK_OUTPUT_KIND_INCONSISTENT",
                "owner": "CONSULTANT_CANDIDATE",
                "route": "consultant_repair",
                "message": f"Output {output_id!r} has inconsistent file/directory semantics.",
                "field": f"output_contracts[{index}]",
            })

        if path and required_basename and Path(path).name != required_basename:
            violations.append({
                "code": "USER_OUTPUT_PATH_NOT_PRESERVED",
                "owner": "CONSULTANT_CANDIDATE",
                "route": "consultant_repair",
                "message": f"Output {output_id!r} no longer preserves its declared filename.",
                "field": f"output_contracts[{index}].canonical_path",
            })

        if (
            bool(row.get("required", True))
            and artifact_kind == "file"
            and not path
            and not (
                root_ref
                and path_policy in {"planner_materialized", "model_selectable"}
            )
        ):
            violations.append({
                "code": "TASK_OUTPUT_BINDING_INCOMPLETE",
                "owner": "CONSULTANT_CANDIDATE",
                "route": "consultant_repair",
                "message": (
                    f"Required file output {output_id!r} has neither an exact path nor a "
                    "typed Planner-materializable output-root relationship."
                ),
                "field": f"output_contracts[{index}]",
            })
    return violations


def _dedupe_input_aliases(inputs: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse only uniquely resolvable same-role aliases."""
    grouped: Dict[tuple[str, str, Any], List[Dict[str, Any]]] = {}
    instance_paths: Dict[tuple[str, tuple[str, str]], str] = {}
    for item in inputs:
        role = _field_key(item.get("role"))
        instance_key = _input_instance_key(item)
        path = artifact_locator(item)
        if instance_key is not None:
            prior = instance_paths.setdefault((role, instance_key), path)
            if prior != path:
                raise InputBindingContractError(
                    "INPUT_INSTANCE_CONFLICT", field_path="$.inputs",
                    actual={"role": role, "input_instance": deepcopy(item["input_instance"]), "paths": [prior, path]},
                    expected="one path per (role, group_id, member_id); use upsert_input to replace that explicit instance",
                )
        key = (role, Path(path).name.lower(), instance_key)
        grouped.setdefault(key, []).append(item)
    resolved: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []
    for (role, basename, _instance_key), values in grouped.items():
        by_path: Dict[str, Dict[str, Any]] = {}
        for item in values:
            by_path.setdefault(artifact_locator(item), item)
        unique = list(by_path.values())
        if len(unique) <= 1:
            resolved.extend(unique)
            continue
        existing = [item for item in unique if Path(artifact_locator(item)).is_file()]
        if len(existing) == 1:
            chosen = deepcopy(existing[0])
            chosen["aliases"] = [
                artifact_locator(item) for item in unique
                if artifact_locator(item) != artifact_locator(chosen)
            ]
            chosen["resolution_status"] = "resolved"
            resolved.append(chosen)
            audit.append({
                "status": "INPUT_ALIAS_RESOLVED",
                "role": role,
                "basename": basename,
                "resolved_path": artifact_locator(chosen),
                "aliases": chosen["aliases"],
            })
        else:
            resolved.extend(unique)
            audit.append({
                "status": "AMBIGUOUS_INPUT_ALIAS",
                "role": role,
                "basename": basename,
                "candidate_paths": [artifact_locator(item) for item in unique],
            })
    return resolved, audit


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
        if not scan and re.search(r"(?:输出|output|produce|生成|交付|产物|deliverables?|result\s+files?)", stripped, re.IGNORECASE):
            scan = re.split(
                r"(?:输出|output|produce|生成|交付|产物|deliverables?|result\s+files?)",
                stripped,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[-1]
        for match in file_re.finditer(scan):
            name = match.group(1)
            if name not in values:
                values.append(name)
    return values


def extract_request_contracts(
    request: str,
    *,
    source_type: str = "user_explicit",
    source_ref: str = "",
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
            {"path": token, "source": source_type, "source_ref": source_ref, "required": True},
            base_dir=base_dir,
            project_root=project_root,
        )
        inputs.append(binding)
    # Inputs are already machine-verifiable structured bindings.  Fresh
    # writes must not duplicate them as generic natural-language requirements.
    requirements: List[Dict[str, Any]] = []
    expected_outputs = [
        normalize_output_binding(
            {"role": "output_dir", "path": output_dir_raw, "source": source_type, "source_ref": source_ref},
            base_dir=base_dir,
            project_root=project_root,
        )
    ] if output_dir_raw else []
    expected_outputs.extend(
        normalize_output_binding({"filename": filename, "source": source_type, "source_ref": source_ref})
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
    candidate_rows = [
        dict(item) for item in candidates if isinstance(item, Mapping)
    ]
    entity_keys = {
        _field_key(item.get("entity_type"))
        for item in candidate_rows
        if _field_key(item.get("fact_kind")) == "entity"
    }
    values: Dict[str, Any] = {}
    for item in candidate_rows:
        if _field_key(item.get("fact_kind")) == "entity":
            entity_type = _field_key(item.get("entity_type"))
            if entity_type and item.get("value") not in (None, ""):
                values[f"entity.{entity_type}"] = deepcopy(item.get("value"))
            continue
        if _field_key(item.get("fact_kind")) == "relation":
            predicate = _field_key(item.get("predicate"))
            subject = _text(item.get("subject"))
            object_value = _text(item.get("object"))
            if predicate and subject and object_value:
                key = f"relation.{predicate}"
                relation = {"subject": subject, "object": object_value}
                current = values.get(key)
                if current is None:
                    values[key] = [relation]
                elif relation not in current:
                    current.append(relation)
            continue
        if _field_key(item.get("fact_kind")) != "parameter":
            continue
        namespace = _field_key(item.get("namespace"))
        key = _field_key(item.get("key"))
        # Entity assignments such as ``species=maize`` are represented once as
        # typed entity requirements, not again as an untyped parameter alias.
        if not namespace and key in entity_keys:
            continue
        if key:
            values[f"{namespace}.{key}" if namespace else key] = item.get("value")
    return values


def _requirements_from_fact_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    source: str,
    source_ref: str = "",
) -> List[Dict[str, Any]]:
    """Compile current entity/relation facts into the canonical constraint set.

    ``entity_bindings`` and ``relation_bindings`` were historical projection
    tables.  Canonical TaskSpec keeps their *current meaning* as typed
    requirements and leaves superseded rows in Audit.
    """
    # Fresh entity/relation facts stay in the structured parameter projection.
    # This adapter remains only so historical fact tables can still be read.
    if not _text(source).startswith("legacy_"):
        return []
    requirements: List[Dict[str, Any]] = []
    for raw in candidates:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        if _text(item.get("status") or "active").lower() != "active":
            continue
        kind = _field_key(item.get("fact_kind"))
        if kind == "entity":
            entity_type = _field_key(item.get("entity_type"))
            value = item.get("value")
            if not entity_type or value in (None, ""):
                continue
            semantic_key = f"required_behavior.entity.{entity_type}"
            requirements.append(normalize_requirement({
                "requirement_id": semantic_key,
                "constraint_id": semantic_key,
                "role": f"entity_{entity_type}",
                "semantic_key": semantic_key,
                "operator": "eq",
                "value": deepcopy(value),
                "level": "required",
                "strength": "hard",
                "status": "satisfied",
                "source": source,
                "source_ref": _text(item.get("source_ref") or source_ref),
            }, source=source))
        elif kind == "relation":
            predicate = _field_key(item.get("predicate"))
            subject = _text(item.get("subject"))
            object_value = _text(item.get("object"))
            if not predicate or not subject or not object_value:
                continue
            semantic_key = f"required_behavior.relation.{predicate}"
            requirements.append(normalize_requirement({
                "requirement_id": (
                    semantic_key
                    + "."
                    + hashlib.sha256(
                        f"{subject}|{predicate}|{object_value}".encode("utf-8")
                    ).hexdigest()[:12]
                ),
                "role": f"relation_{predicate}",
                "semantic_key": semantic_key,
                "operator": "require",
                "value": {"subject": subject, "object": object_value},
                "level": "required",
                "strength": "hard",
                "status": "satisfied",
                "source": source,
                "source_ref": _text(item.get("source_ref") or source_ref),
            }, source=source))
    return requirements


def _active_legacy_fact_candidates(raw: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Resolve legacy fact tables at the migration boundary only.

    The returned rows are an ephemeral adapter result.  They are never written
    back as ``facts`` or ``*_bindings`` aliases.
    """
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for field in (*FACT_FIELDS.values(), "facts"):
        for value in raw.get(field, []) or []:
            if not isinstance(value, Mapping):
                continue
            item = deepcopy(dict(value))
            identity = _text(item.get("fact_id")) or json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if identity in seen:
                continue
            seen.add(identity)
            if _text(item.get("status") or "active").lower() != "active":
                continue
            if item.get("active") is False:
                continue
            kind = _field_key(item.get("fact_kind"))
            if not kind:
                kind = "artifact" if artifact_locator(item) or _text(item.get("locator")) else "unresolved"
                item["fact_kind"] = kind
            rows.append(item)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in rows:
        grouped.setdefault(_fact_group_key(item), []).append(item)
    active: List[Dict[str, Any]] = []
    task_id = _text(raw.get("task_id"))
    for values in grouped.values():
        values.sort(key=lambda item: _effective_fact_rank(item, task_id), reverse=True)
        if any(_text(item.get("cardinality")).lower() == "one" for item in values):
            active.append(values[0])
            continue
        distinct: set[str] = set()
        for item in values:
            identity = _effective_fact_value_key(item)
            if identity not in distinct:
                distinct.add(identity)
                active.append(item)
    return active


def _apply_canonical_artifact_mutations(
    spec: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    source_ref: str,
) -> TaskSpec:
    """Apply user artifact operations directly to canonical current facts."""
    updated = deepcopy(dict(spec))
    artifact_candidates = [
        deepcopy(dict(item))
        for item in candidates
        if isinstance(item, Mapping) and _field_key(item.get("fact_kind")) == "artifact"
    ]
    if not artifact_candidates:
        return canonical_current_task_spec(updated)

    inputs = [deepcopy(dict(item)) for item in updated.get("inputs", []) if isinstance(item, Mapping)]
    outputs = [deepcopy(dict(item)) for item in updated.get("output_contracts", []) if isinstance(item, Mapping)]

    retain_groups: Dict[tuple[str, str], set[str]] = {}
    for item in artifact_candidates:
        if not item.get("retain_only") or _text(item.get("status") or "active").lower() != "active":
            continue
        direction = _field_key(item.get("direction")) or "input"
        locator = _text(item.get("locator"))
        role = (
            canonical_input_role(_text(item.get("role")), locator)
            if direction == "input"
            else _field_key(item.get("role")) or "output"
        )
        retain_groups.setdefault((direction, role), set()).add(locator)
    for (direction, role), allowed in retain_groups.items():
        if direction == "input":
            inputs = [
                item for item in inputs
                if canonical_input_role(_text(item.get("role")), artifact_locator(item)) != role
                or artifact_locator(item) in allowed
            ]
        else:
            outputs = [
                item for item in outputs
                if _field_key(item.get("role")) != role or artifact_locator(item) in allowed
            ]

    touched_inputs: set[str] = set()
    touched_outputs: set[str] = set()
    for item in artifact_candidates:
        locator = _text(item.get("locator"))
        direction = _field_key(item.get("direction")) or "input"
        status = _text(item.get("status") or "active").lower()
        if status == "withdrawn":
            matched_input_roles = {
                canonical_input_role(_text(value.get("role")), artifact_locator(value))
                for value in inputs
                if artifact_locator(value) == locator
            }
            matched_output_roles = {
                _field_key(value.get("role")) or "output"
                for value in outputs
                if artifact_locator(value) == locator
            }
            if matched_input_roles or matched_output_roles:
                touched_inputs.update(matched_input_roles)
                touched_outputs.update(matched_output_roles)
                inputs = [value for value in inputs if artifact_locator(value) != locator]
                outputs = [value for value in outputs if artifact_locator(value) != locator]
                continue
        if direction == "output":
            role = _field_key(item.get("role")) or "output"
            touched_outputs.add(role)
            if status == "withdrawn":
                outputs = [
                    value for value in outputs
                    if not (
                        _field_key(value.get("role")) == role
                        and artifact_locator(value) == locator
                    )
                ]
                continue
            binding = normalize_output_binding({
                **item,
                "path": locator,
                "source": "user_explicit",
                "source_ref": source_ref,
            }, source="user_explicit")
            if _text(item.get("cardinality")).lower() == "one" or role == "output_dir":
                outputs = [value for value in outputs if _field_key(value.get("role")) != role]
            if not any(artifact_locator(value) == artifact_locator(binding) for value in outputs):
                outputs.append(binding)
            continue

        role = canonical_input_role(_text(item.get("role")), locator)
        touched_inputs.add(role)
        if status == "withdrawn":
            inputs = [
                value for value in inputs
                if not (
                    canonical_input_role(_text(value.get("role")), artifact_locator(value)) == role
                    and artifact_locator(value) == locator
                )
            ]
            continue
        binding = normalize_input_binding({
            **item,
            "path": locator,
            "source": "user_explicit",
            "source_ref": source_ref,
        }, source="user_explicit")
        cardinality = _text(item.get("cardinality")).lower()
        if cardinality == "one" or (cardinality != "many" and role != "contact_matrix"):
            inputs = [
                value for value in inputs
                if canonical_input_role(_text(value.get("role")), artifact_locator(value)) != role
            ]
        if not any(artifact_locator(value) == artifact_locator(binding) for value in inputs):
            inputs.append(binding)

    updated["inputs"] = inputs
    updated["output_contracts"] = outputs
    requirements = [
        deepcopy(dict(item))
        for item in updated.get("requirements", []) or []
        if isinstance(item, Mapping)
        and not (
            _semantic_key(item.get("semantic_key")).startswith("required_input.")
            and _field_key(_semantic_key(item.get("semantic_key")).split(".", 1)[1]) in touched_inputs
        )
        and not (
            _semantic_key(item.get("semantic_key")).startswith("required_output.")
            and _field_key(_semantic_key(item.get("semantic_key")).split(".", 1)[1]) in touched_outputs
        )
    ]
    # Structured artifact rows are the sole fresh authority.  Removing a
    # touched historical alias is safe, but writing a replacement
    # required_input/required_output requirement would recreate the retired
    # duplicate semantic registry.
    updated["requirements"] = requirements
    return canonical_current_task_spec(updated)


def task_spec_revision(spec: Mapping[str, Any] | None) -> int:
    try:
        return max(0, int((spec or {}).get("revision") or 0))
    except (TypeError, ValueError):
        return 0


_FINALIZE_TASK_SPEC_BINDING_FIELDS = (
    "schema_version",
    "task_id",
    "goal",
    "inputs",
    "parameters",
    "output_contracts",
    "confirmed_decisions",
    "unresolved_questions",
    "requirements",
    "capability_intent",
)
_FINALIZE_TASK_SPEC_VOLATILE_FIELDS = {
    "created_at",
    "updated_at",
    "recorded_at",
    "compiled_at",
    "observed_at",
}
_FINALIZE_CONTROLLER_SOURCES = {
    "consultant_inferred",
    "consultant_semantic_compilation",
}


def finalize_task_spec_binding_projection(
    spec: Mapping[str, Any] | None,
    *,
    selected_skill_ids: Iterable[Any] = (),
) -> Dict[str, Any]:
    """Return the immutable pre-FINALIZE business-state projection.

    FINALIZE compilation writes controller-validated semantic mutations back
    into TaskSpec and therefore may advance the semantic revision.  Revision
    identity is stored separately in the protocol/compiled artifact; this
    projection excludes it and controller-derived rows so terminal replay can
    verify the unchanged pre-FINALIZE business semantics.  Any user-owned
    semantic edit still changes the projected content and invalidates it.
    """
    normalized = ensure_task_spec(spec)

    def project(value: Any) -> Any:
        if isinstance(value, Mapping):
            source = _text(
                value.get("source")
                or value.get("source_type")
                or value.get("reason")
            ).casefold()
            if source in _FINALIZE_CONTROLLER_SOURCES:
                return None
            result: Dict[str, Any] = {}
            for key, child in value.items():
                key_text = str(key)
                if key_text in _FINALIZE_TASK_SPEC_VOLATILE_FIELDS:
                    continue
                projected = project(child)
                if projected is not None:
                    result[key_text] = projected
            return result
        if isinstance(value, (list, tuple)):
            rows = [project(item) for item in value]
            rows = [item for item in rows if item is not None]
            return sorted(
                rows,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            )
        return deepcopy(value)

    selected = list(dict.fromkeys(
        _text(item)
        for item in selected_skill_ids or ()
        if _text(item)
    ))
    return {
        "binding_schema_version": 1,
        "selected_skill_ids": selected,
        "task_spec": {
            field: project(normalized.get(field))
            for field in _FINALIZE_TASK_SPEC_BINDING_FIELDS
        },
    }


def finalize_task_spec_binding_hash(
    spec: Mapping[str, Any] | None,
    *,
    selected_skill_ids: Iterable[Any] = (),
) -> str:
    projection = finalize_task_spec_binding_projection(
        spec,
        selected_skill_ids=selected_skill_ids,
    )
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_task_spec(
    request: str,
    *,
    source: str = "user_initial",
    task_id: str = "",
    structured_source: Mapping[str, Any] | None = None,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    raw_submission_ref: str = "",
) -> TaskSpec:
    request_text = _text(request)
    created_at = _now()
    source_message_id = _message_id(request_text, task_id)
    structured = dict(structured_source or {})
    contract_text = _text(structured.get("question")) or request_text
    question_source = _text(structured.get("question_source_type") or "benchmark_question") if structured else "user_explicit"
    question_ref = _text(structured.get("question_source_ref"))
    contracts = extract_request_contracts(
        contract_text,
        source_type=question_source,
        source_ref=question_ref,
        base_dir=base_dir,
        project_root=project_root,
    )
    if structured:
        contract_source = _text(structured.get("contract_source_type") or "benchmark_contract")
        contract_ref = _text(structured.get("contract_source_ref"))
        structured_inputs = [
            normalize_input_binding(
                {
                    **(dict(item) if isinstance(item, Mapping) else {"path": _text(item)}),
                    "source": contract_source,
                    "source_ref": (
                        _text(item.get("source_ref")) if isinstance(item, Mapping) else ""
                    ) or f"{contract_ref}#input_files[{index}]",
                },
                source=contract_source,
                base_dir=base_dir,
                project_root=project_root,
            )
            for index, item in enumerate(structured.get("input_files") or [])
        ]
        structured_outputs = [
            normalize_output_binding(
                {
                    **(dict(item) if isinstance(item, Mapping) else {"filename": _text(item)}),
                    "source": contract_source,
                    "source_type": contract_source,
                    "source_ref": (
                        _text(item.get("source_ref")) if isinstance(item, Mapping) else ""
                    ) or f"{contract_ref}#expected_outputs[{index}]",
                    "required": True,
                },
                source=contract_source,
                base_dir=base_dir,
                project_root=project_root,
            )
            for index, item in enumerate(structured.get("expected_outputs") or [])
        ]
        output_dir = _text(structured.get("output_dir"))
        if output_dir:
            structured_outputs.insert(0, normalize_output_binding(
                {
                    "role": "output_dir",
                    "path": output_dir,
                    "source": contract_source,
                    "source_type": contract_source,
                    "source_ref": f"{contract_ref}#output_dir",
                    "required": True,
                    "user_visible": False,
                },
                source=contract_source,
                base_dir=base_dir,
                project_root=project_root,
            ))
        contracts["inputs"] = _merge_list(contracts.get("inputs", []), structured_inputs)
        contracts["output_contracts"] = _merge_list(contracts.get("output_contracts", []), structured_outputs)
    contracts["inputs"], input_alias_audit = _dedupe_input_aliases(contracts.get("inputs", []))
    candidates = extract_user_fact_candidates(request_text)
    source_refs = [
        value for value in (
            _text(raw_submission_ref),
            _text(structured.get("source_ref")),
            source_message_id,
        )
        if value
    ]
    semantic_requirements = _requirements_from_fact_candidates(
        candidates,
        source="user_explicit",
        source_ref=source_message_id,
    )
    task = {
        "schema_version": TASK_SPEC_SCHEMA_VERSION,
        "canonicalization_version": TASK_CANONICALIZATION_VERSION,
        "task_id": _text(task_id),
        "revision": 1,
        "goal": contract_text,
        "raw_submission_ref": _text(raw_submission_ref),
        "source_refs": sorted(set(source_refs)),
        "inputs": contracts["inputs"],
        "parameters": _parameters_from_candidates(candidates),
        "output_contracts": contracts["output_contracts"],
        "confirmed_decisions": [],
        "unresolved_questions": [],
        "requirements": _merge_list(contracts["requirements"], semantic_requirements),
    }
    return canonical_current_task_spec(task, base_dir=base_dir, project_root=project_root)


def canonical_current_task_spec(
    spec: Mapping[str, Any] | None,
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> TaskSpec:
    """Compile one compact, writable CurrentTaskSpec record.

    Legacy aliases are read here but never written back.  Historical prose,
    migration diagnostics and change logs belong to the append-only audit
    store, not to the current semantic object.
    """
    raw = deepcopy(dict(spec or {}))
    legacy_facts = _active_legacy_fact_candidates(raw)
    inputs = [
        normalize_input_binding(
            item,
            source=_text(item.get("source")) if isinstance(item, Mapping) else "legacy_task_input",
            base_dir=base_dir,
            project_root=project_root,
        )
        for item in raw.get("inputs", []) or []
        if isinstance(item, (Mapping, str))
    ]
    legacy_inputs = [
        normalize_input_binding(
            {
                **item,
                "path": _text(item.get("locator") or artifact_locator(item)),
                "source": _text(item.get("source") or "legacy_task_spec"),
                "source_ref": _text(item.get("source_ref") or item.get("source_message_id")),
            },
            source=_text(item.get("source") or "legacy_task_spec"),
            base_dir=base_dir,
            project_root=project_root,
        )
        for item in legacy_facts
        if _field_key(item.get("fact_kind")) == "artifact"
        and _field_key(item.get("direction") or "input") == "input"
        and _text(item.get("locator") or artifact_locator(item))
    ]
    if legacy_inputs:
        legacy_roles = {
            canonical_input_role(_text(item.get("role")), artifact_locator(item))
            for item in legacy_inputs
        }
        inputs = [
            item for item in inputs
            if canonical_input_role(_text(item.get("role")), artifact_locator(item)) not in legacy_roles
        ] + legacy_inputs
    inputs, _ = _dedupe_input_aliases(inputs)
    requirements = [
        normalize_requirement(item, default_level="unknown", source="legacy_task_spec")
        for item in raw.get("requirements", []) or []
        if isinstance(item, (Mapping, str))
    ]
    requirements = _merge_list(
        requirements,
        _requirements_from_fact_candidates(
            legacy_facts,
            source="legacy_task_spec",
        ),
    )
    outputs = effective_output_contracts(
        raw,
        base_dir=base_dir,
        project_root=project_root,
    )
    legacy_outputs = [
        normalize_output_binding(
            {
                **item,
                "path": _text(item.get("locator") or artifact_locator(item)),
                "source": _text(item.get("source") or "legacy_task_spec"),
                "source_ref": _text(item.get("source_ref") or item.get("source_message_id")),
            },
            source=_text(item.get("source") or "legacy_task_spec"),
            base_dir=base_dir,
            project_root=project_root,
        )
        for item in legacy_facts
        if _field_key(item.get("fact_kind")) == "artifact"
        and _field_key(item.get("direction")) == "output"
        and _text(item.get("locator") or artifact_locator(item))
    ]
    if legacy_outputs:
        legacy_roles = {_field_key(item.get("role")) for item in legacy_outputs}
        outputs = [item for item in outputs if _field_key(item.get("role")) not in legacy_roles] + legacy_outputs
    input_contracts = {
        (
            f"required_input.{canonical_input_role(_text(item.get('role')), artifact_locator(item))}",
            artifact_locator(item),
        )
        for item in inputs
        if artifact_locator(item)
    }
    output_contracts = {
        (f"required_output.{_field_key(item.get('role')) or 'output'}", artifact_locator(item))
        for item in outputs
        if artifact_locator(item)
    }
    # Exact artifact requirements are a deterministic projection of the
    # canonical bindings.  Persisting both copies was one of the main TaskSpec
    # inflation sources; semantic_constraints_from_task_spec() recreates them
    # with stable IDs when a consumer needs the constraint view.
    requirements = [
        item for item in requirements
        if not (
            not item.get("condition")
            and _field_key(item.get("operator")) in {"", "eq"}
            and (
                (_semantic_key(item.get("semantic_key")), _text(item.get("value") or item.get("path")))
                in input_contracts
                or (_semantic_key(item.get("semantic_key")), _text(item.get("value") or item.get("path")))
                in output_contracts
            )
        )
    ]
    source_refs = {
        _text(item) for item in raw.get("source_refs", []) or [] if _text(item)
    }
    for value in [*inputs, *requirements, *outputs]:
        source_ref = _text(value.get("source_ref") or value.get("source_id"))
        if source_ref:
            source_refs.add(source_ref)
        for provenance in value.get("provenance", []) or []:
            if isinstance(provenance, Mapping):
                ref = _text(provenance.get("source_ref") or provenance.get("source_id"))
                if ref:
                    source_refs.add(ref)
    envelope = raw.get("request_envelope")
    if isinstance(envelope, Mapping) and _text(envelope.get("source_ref")):
        source_refs.add(_text(envelope.get("source_ref")))
    for directive in raw.get("user_directives", []) or []:
        if isinstance(directive, Mapping) and _text(directive.get("source_message_id")):
            source_refs.add(_text(directive.get("source_message_id")))
    result: TaskSpec = {
        "schema_version": TASK_SPEC_SCHEMA_VERSION,
        "canonicalization_version": TASK_CANONICALIZATION_VERSION,
        "task_id": _text(raw.get("task_id")),
        "revision": max(1, task_spec_revision(raw)),
        "goal": _text(raw.get("goal")),
        "inputs": inputs,
        "parameters": {
            **deepcopy(dict(raw.get("parameters") or {})),
            **_parameters_from_candidates(legacy_facts),
        },
        "output_contracts": outputs,
        "confirmed_decisions": deepcopy(list(raw.get("confirmed_decisions") or [])),
        "unresolved_questions": deepcopy(list(raw.get("unresolved_questions") or [])),
        "requirements": requirements,
        "source_refs": sorted(source_refs),
    }
    raw_ref = _text(raw.get("raw_submission_ref"))
    if raw_ref:
        result["raw_submission_ref"] = raw_ref
    capability_intent = raw.get("capability_intent")
    if isinstance(capability_intent, Mapping) and capability_intent:
        result["capability_intent"] = deepcopy(dict(capability_intent))
    return result


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
    if old_schema >= TASK_SPEC_SCHEMA_VERSION:
        return canonical_current_task_spec(
            normalized,
            base_dir=base_dir,
            project_root=project_root,
        )
    normalized["schema_version"] = max(TASK_SPEC_SCHEMA_VERSION, old_schema)
    normalized["revision"] = max(1, task_spec_revision(normalized))
    normalized.setdefault("goal", _text(fallback_request))
    normalized.setdefault("request_envelope", {
        "schema_version": 1,
        "request_text": _text(normalized.get("goal") or fallback_request),
        "raw_submission_text": _text(fallback_request or normalized.get("goal")),
        "source_type": "legacy_task_spec",
        "source_ref": "",
        "benchmark_id": "",
        "controls": {},
    })
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
    normalized["inputs"], normalized["input_alias_audit"] = _dedupe_input_aliases(normalized["inputs"])
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
    normalized["output_contracts"] = effective_output_contracts(
        normalized,
        base_dir=base_dir,
        project_root=project_root,
    )
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
    return canonical_current_task_spec(
        normalized,
        base_dir=base_dir,
        project_root=project_root,
    )


def task_spec_is_semantic_empty(spec: Mapping[str, Any] | None) -> bool:
    """Return whether a record carries no accepted task meaning.

    This intentionally ignores record/audit metadata.  Legacy states sometimes
    contain only ``raw_submission_ref`` and ``source_refs``; those records are
    bootstrap evidence, not an initialized canonical TaskSpec.
    """
    if not isinstance(spec, Mapping):
        return True
    return not any((
        _text(spec.get("goal")),
        list(spec.get("inputs") or []),
        dict(spec.get("parameters") or {}),
        list(spec.get("output_contracts") or []),
        list(spec.get("requirements") or []),
        list(spec.get("confirmed_decisions") or []),
        list(spec.get("unresolved_questions") or []),
    ))


def task_spec_is_initialized(spec: Mapping[str, Any] | None) -> bool:
    """Canonical lifecycle predicate used at durable read/commit boundaries."""
    if not isinstance(spec, Mapping) or task_spec_is_semantic_empty(spec):
        return False
    return task_spec_revision(spec) >= 1


def normalize_requirement(
    value: Any,
    *,
    default_level: str = "unknown",
    source: str = "contract",
) -> Dict[str, Any]:
    """Normalize one requirement without interpreting open-world prose.

    Registered keys are retained for explicit typed policies and legacy reads.
    Keyless rows remain open text and gain no ontology/operator/scope fields.
    """
    raw = dict(value) if isinstance(value, Mapping) else {"value": _text(value)}
    semantic_key = _semantic_key(raw.get("semantic_key"))
    if not semantic_key:
        source_value = _binding_source(
            _text(raw.get("source_type") or raw.get("source") or source)
        )
        level = _text(
            raw.get("level") or raw.get("requirement_level") or default_level
        ).lower()
        if level not in REQUIREMENT_LEVELS:
            level = "unknown"
        strength = _field_key(raw.get("strength"))
        if strength not in SEMANTIC_CONSTRAINT_STRENGTHS:
            strength = {
                "required": "required",
                "conditional": "conditional",
                "recommended": "preference",
                "optional_validation": "advisory",
                "known_absent": "advisory",
                "unknown": "ambiguous",
            }.get(level, "ambiguous")
        ambiguity = raw.get("ambiguity", False)
        needs_clarification = bool(
            raw.get("needs_clarification")
            or ambiguity not in (False, None, "", "none", "false", "False")
            or strength == "ambiguous"
        )
        confidence_default = (
            1.0
            if source_value in {"user_explicit", "current_task_user_explicit"}
            else 0.8
        )
        path = _text(raw.get("path") or raw.get("locator"))
        normalized = {
            "requirement_id": _text(
                raw.get("requirement_id") or raw.get("role") or raw.get("name")
            ) or "requirement",
            "value": raw.get("value", path),
            "level": level,
            "strength": strength,
            "confidence": _bounded_confidence(
                raw.get("confidence"), confidence_default
            ),
            "ambiguity": ambiguity,
            "needs_clarification": needs_clarification,
            "unresolved_reason": _text(raw.get("unresolved_reason")),
            "source_text": _text(raw.get("source_text")),
            "source_span": deepcopy(raw.get("source_span")),
            "status": _text(raw.get("status") or "active").lower(),
            "source": source_value,
            "source_type": source_value,
            "source_ref": _text(raw.get("source_ref") or raw.get("source_id")),
            "source_id": _text(raw.get("source_id") or raw.get("source_ref")),
            "provenance": _binding_provenance(raw, source_value),
            "evidence": (
                list(raw.get("evidence") or [])
                if isinstance(raw.get("evidence"), list)
                else []
            ),
            "fact_id": _text(raw.get("fact_id")),
            "owner": _text(raw.get("owner")),
            "repair_target": _text(raw.get("repair_target")),
            "normalization_audit": deepcopy(raw.get("normalization_audit")),
        }
        always_present = {
            "requirement_id", "value", "level", "strength", "confidence",
            "ambiguity", "needs_clarification", "status", "source",
            "source_type", "provenance",
        }
        return {
            key: item
            for key, item in normalized.items()
            if key in always_present or item not in (None, "", [], {})
        }
    # ``role`` is a semantic slot, never the value that happens to occupy it.
    # When a canonical key is present it is the authority for the slot name;
    # requirement_id remains a stable row identity and is intentionally kept
    # separate.  This prevents rows such as ``required_behavior.annotation_format
    # = gtf22`` from being normalized to the nonsensical role ``gtf22``.
    semantic_role = (
        _field_key(semantic_key.split(".", 1)[1])
        if "." in semantic_key
        else ""
    )
    declared_role = _text(raw.get("role") or raw.get("name"))
    role = semantic_role or declared_role
    path = _text(raw.get("path") or raw.get("locator")) or (
        artifact_locator(raw)
        if _field_key(raw.get("direction")) == "input"
        else ""
    )
    requirement_value = raw.get("value", path)
    level = _text(raw.get("level") or raw.get("requirement_level") or default_level).lower()
    if level not in REQUIREMENT_LEVELS:
        level = "unknown"
    requirement_id = _text(raw.get("requirement_id") or role)
    if not requirement_id:
        requirement_id = _text(Path(path).name if path else requirement_value)
    source_value = _binding_source(_text(raw.get("source_type") or raw.get("source") or source))
    category = semantic_key.split(".", 1)[0]
    operator = _field_key(raw.get("operator"))
    if operator not in SEMANTIC_CONSTRAINT_OPERATORS:
        operator = {
            "forbidden_behavior": "forbid",
            "preference": "prefer",
            "conditional_policy": "on_condition",
            "failure_policy": "on_condition",
            "runtime_constraint": "require",
        }.get(category, "eq")
    strength = _field_key(raw.get("strength"))
    if strength not in SEMANTIC_CONSTRAINT_STRENGTHS:
        strength = {
            "required": "hard",
            "conditional": "conditional",
            "recommended": "preference",
            "optional_validation": "advisory",
            "known_absent": "advisory",
            "unknown": "ambiguous",
        }.get(level, "ambiguous")
    ambiguity = raw.get("ambiguity", False)
    needs_clarification = bool(
        raw.get("needs_clarification")
        or ambiguity not in (False, None, "", "none", "false", "False")
        or strength == "ambiguous"
    )
    confidence_default = 1.0 if source_value in {"user_explicit", "current_task_user_explicit"} else 0.8
    enforcement_scope = semantic_constraint_enforcement_scope({
        **raw,
        "semantic_key": semantic_key,
        "strength": strength,
    })
    # Explicit schema projection: do not retain the entire source row and then
    # duplicate it with normalized aliases.  New fields must be deliberately
    # classified as semantic, record/provenance, or runtime evidence before
    # they can enter canonical TaskSpec.
    normalized = {
        "requirement_id": requirement_id or "requirement",
        "constraint_id": _text(raw.get("constraint_id")),
        "role": role or _field_key(requirement_id) or "requirement",
        "path": path,
        "value": requirement_value,
        "level": level,
        "condition": raw.get("condition"),
        "semantic_key": semantic_key,
        "operator": operator,
        "strength": strength,
        "enforcement_scope": enforcement_scope,
        "confidence": _bounded_confidence(raw.get("confidence"), confidence_default),
        "ambiguity": ambiguity,
        "needs_clarification": needs_clarification,
        "unresolved_reason": _text(raw.get("unresolved_reason")),
        "source_text": _text(raw.get("source_text")),
        "source_span": deepcopy(raw.get("source_span")),
        "status": _text(raw.get("status") or "unresolved").lower(),
        "source": source_value,
        "source_type": source_value,
        "source_ref": _text(raw.get("source_ref") or raw.get("source_id")),
        "source_id": _text(raw.get("source_id") or raw.get("source_ref")),
        "provenance": _binding_provenance(raw, source_value),
        "evidence": list(raw.get("evidence") or []) if isinstance(raw.get("evidence"), list) else [],
        "fact_id": _text(raw.get("fact_id")),
        "direction": _text(raw.get("direction")),
        "format": _text(raw.get("format") or raw.get("type")),
        "required": bool(raw.get("required", level == "required")),
        "description": _text(raw.get("description") or raw.get("purpose")),
        "unit": _text(raw.get("unit")),
        "value_type": _text(raw.get("value_type")),
        "cardinality": _text(raw.get("cardinality")),
        "alternatives": deepcopy(raw.get("alternatives")) if isinstance(raw.get("alternatives"), list) else [],
        "present": raw.get("present"),
        "exists": raw.get("exists"),
        "non_empty": raw.get("non_empty"),
        "readable": raw.get("readable"),
        "verified": bool(raw.get("verified", False)),
        "verified_at": _text(raw.get("verified_at")),
        "checked_at": _text(raw.get("checked_at")),
        "verification_evidence": (
            deepcopy(list(raw.get("verification_evidence") or []))
            if isinstance(raw.get("verification_evidence"), list)
            else []
        ),
        "owner": _text(raw.get("owner")),
        "repair_target": _text(raw.get("repair_target")),
        "normalization_audit": deepcopy(raw.get("normalization_audit")),
    }
    always_present = {
        "requirement_id", "role", "path", "value", "level", "semantic_key",
        "operator", "strength", "enforcement_scope", "confidence", "ambiguity",
        "needs_clarification", "status", "source", "source_type", "provenance",
        "required",
    }
    return {
        key: value
        for key, value in normalized.items()
        if key in always_present or value not in (None, "", [], {})
    }


def _constraint_semantic_identity(value: Mapping[str, Any]) -> str:
    semantic_key = _semantic_key(value.get("semantic_key"))
    if not semantic_key:
        payload = {
            "requirement_id": _text(value.get("requirement_id")),
            "value": value.get("value"),
            "strength": _field_key(value.get("strength")),
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    canonical = canonicalize_compiled_constraint(value)
    payload = {
        "semantic_key": _semantic_key(canonical.get("semantic_key")),
        "operator": _field_key(canonical.get("operator")),
        "value": canonical.get("value"),
        "strength": _field_key(canonical.get("strength")),
        "condition": canonical.get("condition"),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _provenance_source_id(value: Mapping[str, Any]) -> str:
    return _text(value.get("source_id") or value.get("source_ref"))


def _provenance_catalog_bindings(values: Any) -> tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Return the catalog's unique, exact source-ID/type bindings.

    A provenance source ID is an identity, not an authorization for the model
    to choose a more privileged source type.  Keep the catalog spelling exact
    (apart from surrounding whitespace) so a supplied catalog is the sole
    authority for both halves of the pair.

    Mapping catalogs remain supported as ``source_id -> source_type`` (or
    ``source_id -> entry``); list catalogs use the public entry shape emitted
    by :func:`semantic_provenance_catalog`.
    """
    if isinstance(values, Mapping):
        if "source_id" in values or "source_ref" in values:
            entries: List[Any] = [values]
        else:
            entries = []
            for source_id, value in values.items():
                if isinstance(value, Mapping):
                    entries.append({**dict(value), "source_id": _text(source_id)})
                else:
                    entries.append({"source_id": _text(source_id), "source_type": value})
    elif isinstance(values, list):
        entries = list(values)
    else:
        entries = []
        if values is not None:
            return {}, [{
                "index": -1,
                "field": "provenance",
                "reason": "provenance catalog must be an object or array",
            }]

    bindings: Dict[str, str] = {}
    errors: List[Dict[str, Any]] = []
    for catalog_index, value in enumerate(entries):
        if not isinstance(value, Mapping):
            errors.append({
                "index": -1,
                "field": "provenance",
                "reason": f"provenance catalog entry {catalog_index} must be an object",
            })
            continue
        source_id = _provenance_source_id(value)
        source_type = _text(value.get("source_type"))
        if not source_id:
            errors.append({
                "index": -1,
                "field": "provenance",
                "reason": f"provenance catalog entry {catalog_index} needs non-empty source_id",
            })
            continue
        if not source_type:
            errors.append({
                "index": -1,
                "field": "provenance",
                "reason": f"provenance catalog source_id {source_id} needs source_type",
            })
            continue
        existing_type = bindings.get(source_id)
        if existing_type is not None and existing_type != source_type:
            errors.append({
                "index": -1,
                "field": "provenance",
                "reason": (
                    f"conflicting provenance catalog source_type for source_id {source_id}: "
                    f"{existing_type} != {source_type}"
                ),
            })
            continue
        bindings[source_id] = source_type
    return bindings, errors


def validate_compiled_constraints(
    values: Any,
    *,
    provenance_catalog: Any = None,
    require_resolvable_provenance: bool = False,
    base_constraints: Iterable[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Validate Consultant output as schema, without interpreting its prose.

    Strict provenance checks are opt-in so legacy controller-owned callers can
    migrate independently.  Consultant finalization enables them and supplies
    the finite, typed source catalog visible to the model.  When a catalog is
    supplied, each source ID is bound to exactly its controller-owned type.
    """
    errors: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []
    inherited_count = 0
    normalization_count = 0
    catalog_supplied = provenance_catalog is not None
    catalog_bindings, catalog_errors = _provenance_catalog_bindings(provenance_catalog)
    catalog_ids = set(catalog_bindings)
    if catalog_supplied:
        errors.extend(catalog_errors)
    base_by_identity = {
        _constraint_semantic_identity(item): dict(item)
        for item in base_constraints or []
        if isinstance(item, Mapping)
    }
    if not isinstance(values, list):
        values = []
        errors.append({"index": -1, "field": "semantic_constraints", "reason": "must be a JSON array"})
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            errors.append({"index": index, "field": "constraint", "reason": "must be a JSON object"})
            continue
        row_error_count = len(errors)
        raw = canonicalize_compiled_constraint(value)
        if raw.get("normalization_audit"):
            normalization_count += 1
        provenance_values = raw.get("provenance") if isinstance(raw.get("provenance"), list) else []
        valid_provenance = [
            item for item in provenance_values
            if isinstance(item, Mapping) and _provenance_source_id(item)
        ]
        if not valid_provenance:
            inherited = base_by_identity.get(_constraint_semantic_identity(raw))
            inherited_values = (
                inherited.get("provenance")
                if isinstance(inherited, Mapping) and isinstance(inherited.get("provenance"), list)
                else []
            )
            inherited_valid = [
                item for item in inherited_values
                if isinstance(item, Mapping) and _provenance_source_id(item)
            ]
            if inherited_valid:
                raw["provenance"] = deepcopy(inherited_valid)
                for field in ("source_type", "source_id", "source_ref", "source_text", "source_span"):
                    if raw.get(field) in (None, "", [], {}) and inherited.get(field) not in (None, "", [], {}):
                        raw[field] = deepcopy(inherited.get(field))
                raw["provenance_inheritance_audit"] = {
                    "rule_id": "exact-semantic-base-provenance-v1",
                    "base_requirement_id": _text(
                        inherited.get("constraint_id") or inherited.get("requirement_id")
                    ),
                }
                provenance_values = raw["provenance"]
                valid_provenance = inherited_valid
                inherited_count += 1
        if valid_provenance:
            primary_provenance = valid_provenance[0]
            if not _text(raw.get("source_id")):
                raw["source_id"] = _provenance_source_id(primary_provenance)
            if not _text(raw.get("source_ref")):
                raw["source_ref"] = _provenance_source_id(primary_provenance)
            if not _text(raw.get("source_type")):
                raw["source_type"] = _text(primary_provenance.get("source_type"))
        semantic_key = _semantic_key(raw.get("semantic_key"))
        operator = _field_key(raw.get("operator"))
        strength = _field_key(raw.get("strength"))
        declared_scope = _field_key(raw.get("enforcement_scope"))
        if semantic_key:
            if not _text(raw.get("constraint_id") or raw.get("requirement_id")):
                errors.append({"index": index, "field": "constraint_id", "reason": "is required"})
            if operator not in SEMANTIC_CONSTRAINT_OPERATORS:
                errors.append({"index": index, "field": "operator", "reason": "unknown or missing operator"})
            if strength not in SEMANTIC_CONSTRAINT_STRENGTHS:
                errors.append({"index": index, "field": "strength", "reason": "unknown or missing strength"})
            if (
                raw.get("enforcement_scope") not in (None, "")
                and declared_scope not in SEMANTIC_CONSTRAINT_ENFORCEMENT_SCOPES
            ):
                errors.append({
                    "index": index,
                    "field": "enforcement_scope",
                    "reason": "unknown enforcement scope",
                })
            raw["enforcement_scope"] = semantic_constraint_enforcement_scope(raw)
        else:
            declared_requirement_id = raw.get("requirement_id")
            if declared_requirement_id not in (None, "") and (
                not isinstance(declared_requirement_id, str)
                or not declared_requirement_id.strip()
            ):
                errors.append({
                    "index": index,
                    "field": "requirement_id",
                    "reason": "must be a non-empty string when supplied",
                })
            if "strength" in raw and strength not in SEMANTIC_CONSTRAINT_STRENGTHS:
                errors.append({
                    "index": index,
                    "field": "strength",
                    "reason": "unknown strength",
                })
            declared_level = _text(raw.get("level")).lower()
            if "level" in raw and declared_level not in REQUIREMENT_LEVELS:
                errors.append({
                    "index": index,
                    "field": "level",
                    "reason": "unknown requirement level",
                })
            if not _text(declared_requirement_id):
                raw["requirement_id"] = f"requirement-{index + 1}"
                normalization_audit = dict(raw.get("normalization_audit") or {})
                normalization_audit["requirement_id_generated"] = True
                raw["normalization_audit"] = normalization_audit
        if "confidence" not in raw and semantic_key:
            errors.append({"index": index, "field": "confidence", "reason": "is required"})
        elif "confidence" in raw:
            try:
                confidence = float(raw.get("confidence"))
            except (TypeError, ValueError):
                confidence = -1.0
            if not 0.0 <= confidence <= 1.0:
                errors.append({"index": index, "field": "confidence", "reason": "must be between 0 and 1"})
        if raw.get("value") in (None, ""):
            errors.append({"index": index, "field": "value", "reason": "is required"})
        elif not semantic_key and not isinstance(raw.get("value"), str):
            errors.append({
                "index": index,
                "field": "value",
                "reason": "open-text requirement value must be a string",
            })
        if require_resolvable_provenance:
            if not valid_provenance:
                errors.append({
                    "index": index,
                    "field": "provenance",
                    "reason": "provenance needs at least one non-empty source_id",
                })
            else:
                if any(not _text(item.get("source_type")) for item in valid_provenance):
                    errors.append({
                        "index": index,
                        "field": "provenance",
                        "reason": "each provenance entry needs source_type",
                    })
        elif not _text(raw.get("source_text")) and not raw.get("provenance") and not _text(raw.get("source_ref") or raw.get("source_id")):
            errors.append({"index": index, "field": "provenance", "reason": "source_text, source_ref, or provenance is required"})
        if valid_provenance and catalog_supplied:
            unknown = sorted({
                _provenance_source_id(item)
                for item in valid_provenance
                if _provenance_source_id(item) not in catalog_ids
            })
            if unknown:
                errors.append({
                    "index": index,
                    "field": "provenance",
                    "reason": "unknown source_id: " + ", ".join(unknown),
                })
            mismatched = sorted({
                (
                    _provenance_source_id(item),
                    _text(item.get("source_type")),
                    catalog_bindings[_provenance_source_id(item)],
                )
                for item in valid_provenance
                if _provenance_source_id(item) in catalog_bindings
                and _text(item.get("source_type"))
                != catalog_bindings[_provenance_source_id(item)]
            })
            if mismatched:
                errors.append({
                    "index": index,
                    "field": "provenance",
                    "reason": "source_type mismatch: " + "; ".join(
                        f"source_id {source_id} expected {expected_type}, got {actual_type}"
                        for source_id, actual_type, expected_type in mismatched
                    ),
                })
        if len(errors) == row_error_count and not semantic_key:
            normalized.append(normalize_requirement({
                **raw,
                "source": raw.get("source_type") or raw.get("source") or "consultant_inferred",
            }, source="consultant_inferred"))
        elif (
            len(errors) == row_error_count
            and semantic_key
            and operator in SEMANTIC_CONSTRAINT_OPERATORS
            and strength in SEMANTIC_CONSTRAINT_STRENGTHS
        ):
            normalized.append(normalize_requirement({
                **raw,
                "requirement_id": _text(raw.get("constraint_id") or raw.get("requirement_id") or semantic_key),
                "role": semantic_key.split(".", 1)[1],
                "semantic_key": semantic_key,
                "operator": operator,
                "strength": strength,
                "level": {
                    "hard": "required",
                    "required": "required",
                    "preference": "recommended",
                    "conditional": "conditional",
                    "advisory": "optional_validation",
                    "ambiguous": "unknown",
                }[strength],
                "status": "unresolved" if raw.get("needs_clarification") else "satisfied",
                "source": raw.get("source_type") or raw.get("source") or "consultant_inferred",
            }, source="consultant_inferred"))
    confidence_values = [float(item.get("confidence") or 0.0) for item in normalized]
    metrics = {
        "constraint_count": len(normalized),
        "hard_count": sum(item.get("strength") in {"hard", "required"} for item in normalized),
        "preference_count": sum(item.get("strength") == "preference" for item in normalized),
        "conditional_count": sum(item.get("strength") == "conditional" for item in normalized),
        "ambiguous_count": sum(bool(item.get("needs_clarification")) for item in normalized),
        "confidence": confidence_values,
        "confidence_distribution": {
            "minimum": min(confidence_values) if confidence_values else None,
            "maximum": max(confidence_values) if confidence_values else None,
            "mean": (sum(confidence_values) / len(confidence_values)) if confidence_values else None,
        },
        "schema_validation": "passed" if not errors else "failed",
        "normalization_count": normalization_count,
        "provenance_inheritance_count": inherited_count,
    }
    return {"valid": not errors, "constraints": normalized, "errors": errors, "metrics": metrics}


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
        instance_key = _input_instance_key(value)
        if instance_key is not None and value.get("role"):
            return "input-instance:" + json.dumps(
                [_field_key(value["role"]), *instance_key],
                ensure_ascii=False, separators=(",", ":"),
            )
        if _text(value.get("requirement_id")):
            return "requirement:" + _text(value.get("requirement_id"))
        if _semantic_key(value.get("semantic_key")):
            return "semantic:" + _semantic_key(value.get("semantic_key"))
        for fields in (
            ("canonical_path", "role"),
            ("path", "role"),
            ("question", "answer"),
            ("text", "kind"),
        ):
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
            combined_provenance = _binding_provenance(
                {"provenance": [*(existing.get("provenance") or []), *(incoming.get("provenance") or [])]},
                _text(existing.get("source_type") or existing.get("source") or incoming.get("source")),
            )
            existing_priority = SOURCE_PRIORITY.get(_binding_source(_text(existing.get("source"))), 0)
            incoming_priority = SOURCE_PRIORITY.get(_binding_source(_text(incoming.get("source"))), 0)
            if existing_priority > incoming_priority:
                same_artifact = bool(
                    _text(existing.get("canonical_path"))
                    and _text(existing.get("canonical_path"))
                    == _text(incoming.get("canonical_path"))
                )
                if same_artifact:
                    for field in (
                        "verified", "status", "verification_evidence", "verified_at",
                        "validation_contract_id",
                    ):
                        value = incoming.get(field)
                        if value not in (None, "", [], {}):
                            existing[field] = deepcopy(value)
            else:
                existing.update(incoming)
            if combined_provenance:
                existing["provenance"] = combined_provenance
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
    base_revision = task_spec_revision(merged)
    before_semantic_hash = task_semantic_hash(merged)

    if _text(updates.get("goal")):
        merged["goal"] = _text(updates["goal"])
    if not isinstance(updates.get("output_contracts"), list) and isinstance(
        updates.get("expected_outputs"), list
    ):
        updates["output_contracts"] = updates["expected_outputs"]
    for field in _CANONICAL_TASK_LIST_FIELDS:
        additions = updates.get(field)
        if isinstance(additions, list):
            prepared: List[Any] = []
            for item in additions:
                if not isinstance(item, Mapping):
                    prepared.append(item)
                    continue
                annotated = deepcopy(dict(item))
                annotated.setdefault("source", source)
                annotated.setdefault("confidence", 1.0 if source.startswith("user") else 0.8)
                annotated.setdefault("created_revision", max(1, base_revision + 1))
                annotated.setdefault("active", True)
                if field == "inputs":
                    annotated = normalize_input_binding(annotated, source=source)
                elif field == "requirements":
                    annotated = normalize_requirement(annotated, source=source)
                elif field == "output_contracts":
                    annotated = normalize_output_binding(annotated, source=source)
                prepared.append(annotated)
            merged[field] = _merge_list(merged.get(field, []), prepared)
    parameters = updates.get("parameters")
    if isinstance(parameters, Mapping):
        merged["parameters"].update(deepcopy(dict(parameters)))

    if isinstance(updates.get("output_contracts"), list):
        merged["output_contracts"] = effective_output_contracts(merged)
    source_refs = set(merged.get("source_refs") or [])
    for value in updates.get("source_refs", []) or []:
        if _text(value):
            source_refs.add(_text(value))
    merged["source_refs"] = sorted(source_refs)
    candidate = canonical_current_task_spec(merged)
    semantic_changed = task_semantic_hash(candidate) != before_semantic_hash
    candidate["revision"] = (
        base_revision + 1 if increment_revision and semantic_changed else base_revision
    )
    return candidate


def merge_compiled_constraints(
    spec: Mapping[str, Any] | None,
    constraints: Iterable[Mapping[str, Any]],
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> TaskSpec:
    """Merge Consultant rows while preserving canonical requirement order."""
    current = ensure_task_spec(
        spec,
        base_dir=base_dir,
        project_root=project_root,
    )
    requirements = [
        deepcopy(dict(item))
        for item in current.get("requirements", []) or []
        if isinstance(item, Mapping)
    ]
    used_ids = {
        _text(item.get("requirement_id"))
        for item in requirements
        if _text(item.get("requirement_id"))
    }
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    for raw in constraints or []:
        if not isinstance(raw, Mapping):
            continue
        prepared = deepcopy(dict(raw))
        semantic_key = _semantic_key(prepared.get("semantic_key"))
        normalization_audit = dict(prepared.get("normalization_audit") or {})
        generated_requirement_id = bool(
            normalization_audit.pop("requirement_id_generated", False)
        )
        if normalization_audit:
            prepared["normalization_audit"] = normalization_audit
        else:
            prepared.pop("normalization_audit", None)
        declared_id = _text(
            prepared.get("requirement_id") or prepared.get("constraint_id")
        )
        replay_match_id = ""
        if generated_requirement_id and not semantic_key:
            prepared["requirement_id"] = declared_id or "requirement-1"
            replay_candidate = normalize_requirement(
                prepared,
                source="consultant_inferred",
            )
            replay_candidate.pop("requirement_id", None)
            replay_candidate.pop("constraint_id", None)
            for existing in requirements:
                if _semantic_key(existing.get("semantic_key")):
                    continue
                existing_candidate = deepcopy(dict(existing))
                existing_candidate.pop("requirement_id", None)
                existing_candidate.pop("constraint_id", None)
                if existing_candidate == replay_candidate:
                    replay_match_id = _text(existing.get("requirement_id"))
                    if replay_match_id:
                        break
        if replay_match_id:
            declared_id = replay_match_id
        if not declared_id or (
            generated_requirement_id
            and not replay_match_id
            and declared_id in used_ids
        ):
            if semantic_key:
                declared_id = semantic_key
            else:
                ordinal = len(used_ids) + 1
                declared_id = f"requirement-{ordinal}"
                while declared_id in used_ids:
                    ordinal += 1
                    declared_id = f"requirement-{ordinal}"
        prepared["requirement_id"] = declared_id
        requirement = normalize_requirement(prepared, source="consultant_inferred")
        semantic_key = _semantic_key(requirement.get("semantic_key"))
        existing_index = next(
            (
                index
                for index, item in enumerate(requirements)
                if _text(item.get("requirement_id")) == declared_id
                or (
                    semantic_key
                    and _semantic_key(item.get("semantic_key")) == semantic_key
                )
            ),
            -1,
        )
        if existing_index >= 0:
            existing = requirements[existing_index]
            combined = _binding_provenance(
                {
                    "provenance": [
                        *(existing.get("provenance") or []),
                        *(requirement.get("provenance") or []),
                    ]
                },
                _text(requirement.get("source_type") or requirement.get("source")),
            )
            if combined:
                requirement["provenance"] = combined
            requirements[existing_index] = requirement
        else:
            requirements.append(requirement)
        used_ids.add(declared_id)
        if not semantic_key:
            continue
        category, slot = semantic_key.split(".", 1)
        # Explicit keyed input/output rows are read-old compatibility only.
        if category == "required_input" and requirement.get("value") not in (None, ""):
            inputs.append(normalize_input_binding(
                {
                    "role": slot,
                    "path": requirement.get("value"),
                    "required": requirement.get("strength") in {"hard", "required"},
                    "source": "consultant_inferred",
                    "source_ref": requirement.get("source_ref"),
                    "provenance": requirement.get("provenance"),
                    "confidence": requirement.get("confidence"),
                },
                source="consultant_inferred",
                base_dir=base_dir,
                project_root=project_root,
            ))
        elif category == "required_output" and requirement.get("value") not in (None, ""):
            output_value = requirement.get("value")
            if isinstance(output_value, Mapping):
                output_raw = dict(output_value)
            elif slot == "output_dir":
                output_raw = {"role": slot, "path": _text(output_value)}
            else:
                output_raw = {"role": slot, "filename": _text(output_value)}
            output_raw.update({
                "source": "consultant_inferred",
                "source_ref": requirement.get("source_ref"),
                "provenance": requirement.get("provenance"),
                "required": requirement.get("strength") in {"hard", "required"},
            })
            outputs.append(normalize_output_binding(
                output_raw,
                source="consultant_inferred",
                base_dir=base_dir,
                project_root=project_root,
            ))
    current["requirements"] = requirements
    merged = merge_task_spec(
        current,
        {
            "inputs": inputs,
            "output_contracts": outputs,
        },
        source="consultant_inferred",
        reason="consultant_semantic_compilation",
        increment_revision=False,
    )
    return merged


def apply_task_spec_mutation_proposal(
    spec: Mapping[str, Any] | None,
    proposal: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    task_id: str = "",
    raw_submission_ref: str = "",
    source_refs: Iterable[str] = (),
) -> TaskSpec:
    """Validate and apply one closed, Controller-owned TaskSpec mutation.

    The LLM may propose only semantic operations.  It cannot supply a full
    TaskSpec, revision, hash, generation, audit ref, or arbitrary merge patch.
    Evidence handles must already have been resolved and stamped into the
    operation payload by the Controller before this function is called.
    """
    payload = deepcopy(dict(proposal or {}))
    required_top_level = {
        "proposal_type",
        "schema_version",
        "operations",
        "unresolved_ambiguities",
        "proposal_evidence_refs",
    }
    if set(payload) != required_top_level:
        raise ValueError(
            "TASKSPEC_MUTATION_STRUCTURE_INVALID: "
            f"missing={sorted(required_top_level - set(payload))}; "
            f"unexpected={sorted(set(payload) - required_top_level)}"
        )
    if payload.get("proposal_type") != "task_spec_mutation":
        raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: invalid proposal_type")
    if int(payload.get("schema_version") or 0) != TASK_SPEC_MUTATION_SCHEMA_VERSION:
        raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: unsupported schema_version")
    operations = payload.get("operations")
    ambiguities = payload.get("unresolved_ambiguities")
    evidence_handles = payload.get("proposal_evidence_refs")
    if not isinstance(operations, list) or not isinstance(ambiguities, list):
        raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: operations/ambiguities must be lists")
    if not isinstance(evidence_handles, list) or not all(
        isinstance(item, str) for item in evidence_handles
    ):
        raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: evidence must use handles")

    was_initialized = task_spec_is_initialized(spec)
    current = ensure_task_spec(
        spec,
        task_id=task_id,
        base_dir=base_dir,
        project_root=project_root,
    )
    if task_id and not _text(current.get("task_id")):
        current["task_id"] = _text(task_id)
    durable_sources = {
        _text(value)
        for value in [*(current.get("source_refs") or []), *list(source_refs or [])]
        if _text(value)
    }
    raw_ref = _text(raw_submission_ref or current.get("raw_submission_ref"))
    if raw_ref:
        current["raw_submission_ref"] = raw_ref
        durable_sources.add(raw_ref)
    current["source_refs"] = sorted(durable_sources)
    base_revision = task_spec_revision(current) if was_initialized else 0
    before_hash = task_semantic_hash(current)
    staged = deepcopy(current)

    allowed_fields = {
        "set_goal": {"op", "value", "reason", "supporting_evidence"},
        "upsert_input": {"op", "input", "reason", "supporting_evidence"},
        "remove_input": {"op", "role", "input_instance", "reason", "supporting_evidence"},
        "upsert_constraint": {"op", "constraint", "reason", "supporting_evidence"},
        "remove_constraint": {
            "op", "requirement_id", "constraint_id", "reason",
            "supporting_evidence",
        },
        "upsert_output_contract": {"op", "output_contract", "reason", "supporting_evidence"},
        "remove_output_contract": {"op", "role", "reason", "supporting_evidence"},
        "confirm_decision": {"op", "decision", "reason", "supporting_evidence"},
        "remove_decision": {"op", "semantic_key", "reason", "supporting_evidence"},
        "mark_ambiguity": {"op", "ambiguity", "reason", "supporting_evidence"},
    }

    def validate_operation(raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: operation must be an object")
        operation = deepcopy(dict(raw))
        kind = _field_key(operation.get("op"))
        if kind not in TASK_SPEC_MUTATION_OPERATIONS:
            raise ValueError(f"TASKSPEC_MUTATION_OPERATION_FORBIDDEN: {kind or '<missing>'}")
        unexpected = sorted(set(operation) - allowed_fields[kind])
        if unexpected:
            raise ValueError(
                "TASKSPEC_MUTATION_STRUCTURE_INVALID: "
                f"operation={kind}; unexpected={unexpected}"
            )
        supporting = operation.get("supporting_evidence", [])
        if not isinstance(supporting, list) or not all(
            isinstance(item, str) for item in supporting
        ):
            raise ValueError(
                "TASKSPEC_MUTATION_STRUCTURE_INVALID: supporting evidence must use handles"
            )
        operation["op"] = kind
        return operation

    normalized_operations = [validate_operation(item) for item in operations]
    normalized_operations.extend(
        validate_operation({
            "op": "mark_ambiguity",
            "ambiguity": deepcopy(dict(item)),
            "reason": "proposal_unresolved_ambiguity",
            "supporting_evidence": [],
        })
        for item in ambiguities
        if isinstance(item, Mapping)
    )
    if len(normalized_operations) != len(operations) + len(ambiguities):
        raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: ambiguity must be an object")

    for operation in normalized_operations:
        kind = operation["op"]
        if kind == "set_goal":
            value = _text(operation.get("value"))
            if not value:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: goal must be non-empty")
            staged["goal"] = value
        elif kind == "upsert_input":
            if not isinstance(operation.get("input"), Mapping):
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: input must be an object")
            role = _field_key(operation["input"].get("role"))
            if not role:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: input role is required")
            # Validate before removing any existing row. A malformed collection
            # is a declaration error, never a partially applied replacement.
            normalized_input = normalize_input_binding(
                operation["input"], source="consultant_inferred",
                base_dir=base_dir, project_root=project_root,
            )
            role = _field_key(normalized_input["role"])
            instance_key = _input_instance_key(normalized_input)
            old_inputs = [deepcopy(dict(item)) for item in staged.get("inputs", []) or [] if isinstance(item, Mapping)]
            same_role = [item for item in old_inputs if _field_key(item.get("role")) == role]
            if instance_key is None and (len(same_role) > 1 or any(_input_instance_key(item) is not None for item in same_role)):
                raise InputBindingContractError(
                    "INPUT_INSTANCE_SELECTOR_REQUIRED", field_path="$.input.input_instance",
                    actual={"role": role, "matching_count": len(same_role)},
                    expected="explicit input_instance selector for this multi-instance role",
                )
            legacy_members = [item for item in same_role if _input_instance_key(item) is None]
            if instance_key is not None and legacy_members:
                raise InputBindingContractError(
                    "INPUT_INSTANCE_LEGACY_ROLE_CONFLICT", field_path="$.input.input_instance",
                    actual={"role": role, "legacy_count": len(legacy_members)},
                    expected="explicitly remove the existing legacy role binding before declaring grouped members; no implicit migration",
                )
            matched_indexes = [index for index, item in enumerate(old_inputs) if
                _field_key(item.get("role")) == role and _input_instance_key(item) == instance_key]
            staged["inputs"] = [
                item for index, item in enumerate(old_inputs) if index not in matched_indexes
            ]
            staged = merge_task_spec(
                staged,
                {"inputs": [normalized_input]},
                source="consultant_inferred",
                reason="consultant_mutation:upsert_input",
                increment_revision=False,
            )
            if matched_indexes:
                # Identical replay must not reorder unrelated rows or create a
                # new semantic revision merely by moving a member to the end.
                replacement = next(item for item in staged["inputs"] if
                    _field_key(item.get("role")) == role and _input_instance_key(item) == instance_key)
                staged["inputs"] = [item for item in staged["inputs"] if item is not replacement]
                staged["inputs"].insert(matched_indexes[0], replacement)
        elif kind == "remove_input":
            role = _field_key(operation.get("role"))
            if not role:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: input role is required")
            if "input_instance" in operation:
                validate_input_binding_declaration({"input_instance": operation["input_instance"]}, field_path="$")
            instance_key = _input_instance_key(operation)
            same_role = [item for item in staged.get("inputs", []) or [] if isinstance(item, Mapping) and _field_key(item.get("role")) == role]
            if instance_key is None and (len(same_role) > 1 or any(_input_instance_key(item) is not None for item in same_role)):
                raise InputBindingContractError(
                    "INPUT_INSTANCE_SELECTOR_REQUIRED", field_path="$.input_instance",
                    actual={"role": role, "matching_count": len(same_role)},
                    expected="explicit input_instance selector for this multi-instance role",
                )
            staged["inputs"] = [
                deepcopy(dict(item))
                for item in staged.get("inputs", []) or []
                if isinstance(item, Mapping) and not (
                    _field_key(item.get("role")) == role and _input_instance_key(item) == instance_key
                )
            ]
            staged["requirements"] = [
                deepcopy(dict(item))
                for item in staged.get("requirements", []) or []
                if not (
                    isinstance(item, Mapping)
                    and _semantic_key(item.get("semantic_key")) == f"required_input.{role}"
                    and not any(_field_key(value.get("role")) == role for value in staged["inputs"])
                )
            ]
        elif kind == "upsert_constraint":
            if not isinstance(operation.get("constraint"), Mapping):
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: constraint must be an object")
            constraint = deepcopy(dict(operation["constraint"]))
            supporting = [
                _text(item)
                for item in operation.get("supporting_evidence", []) or []
                if _text(item)
            ]
            if supporting:
                constraint.setdefault("source_ref", supporting[0])
                constraint.setdefault("source_id", supporting[0])
            declared_key = _semantic_key(constraint.get("semantic_key"))
            typed_policy = bool(
                declared_key.startswith("failure_policy.")
                or declared_key == "forbidden_behavior.runtime_install"
            )
            if not typed_policy:
                constraint = {
                    key: deepcopy(constraint.get(key))
                    for key in (
                        "requirement_id", "value", "strength", "level",
                        "confidence", "ambiguity", "needs_clarification",
                        "unresolved_reason", "source_text", "source_span",
                        "status", "source", "source_type", "source_ref",
                        "source_id", "provenance", "evidence", "fact_id",
                        "owner", "repair_target",
                    )
                    if constraint.get(key) not in (None, "", [], {})
                }
            validation = validate_compiled_constraints([constraint])
            if not validation.get("valid"):
                raise ValueError(
                    "TASKSPEC_MUTATION_CONSTRAINT_INVALID: "
                    + json.dumps(
                        validation.get("errors") or [],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            staged = merge_compiled_constraints(
                staged,
                validation.get("constraints") or [],
                base_dir=base_dir,
                project_root=project_root,
            )
        elif kind == "remove_constraint":
            identifier = _text(
                operation.get("requirement_id") or operation.get("constraint_id")
            )
            if not identifier:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: requirement_id is required")
            staged["requirements"] = [
                deepcopy(dict(item))
                for item in staged.get("requirements", []) or []
                if isinstance(item, Mapping)
                and identifier not in {
                    _text(item.get("constraint_id")),
                    _text(item.get("requirement_id")),
                    _text(item.get("semantic_key")),
                }
            ]
        elif kind == "upsert_output_contract":
            if not isinstance(operation.get("output_contract"), Mapping):
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: output contract must be an object")
            role = _field_key(operation["output_contract"].get("role"))
            if not role:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: output role is required")
            staged["output_contracts"] = [
                deepcopy(dict(item))
                for item in staged.get("output_contracts", []) or []
                if isinstance(item, Mapping) and _field_key(item.get("role")) != role
            ]
            staged = merge_task_spec(
                staged,
                {"output_contracts": [operation["output_contract"]]},
                source="consultant_inferred",
                reason="consultant_mutation:upsert_output_contract",
                increment_revision=False,
            )
        elif kind == "remove_output_contract":
            role = _field_key(operation.get("role"))
            if not role:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: output role is required")
            staged["output_contracts"] = [
                deepcopy(dict(item))
                for item in staged.get("output_contracts", []) or []
                if isinstance(item, Mapping) and _field_key(item.get("role")) != role
            ]
            staged["requirements"] = [
                deepcopy(dict(item))
                for item in staged.get("requirements", []) or []
                if not (
                    isinstance(item, Mapping)
                    and _semantic_key(item.get("semantic_key")) == f"required_output.{role}"
                )
            ]
        elif kind == "confirm_decision":
            if not isinstance(operation.get("decision"), Mapping):
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: decision must be an object")
            semantic_key = _semantic_key(operation["decision"].get("semantic_key"))
            if semantic_key:
                staged["confirmed_decisions"] = [
                    deepcopy(dict(item))
                    for item in staged.get("confirmed_decisions", []) or []
                    if isinstance(item, Mapping)
                    and _semantic_key(item.get("semantic_key")) != semantic_key
                ]
            staged = merge_task_spec(
                staged,
                {"confirmed_decisions": [operation["decision"]]},
                source="consultant_inferred",
                reason="consultant_mutation:confirm_decision",
                increment_revision=False,
            )
        elif kind == "remove_decision":
            semantic_key = _semantic_key(operation.get("semantic_key"))
            if not semantic_key:
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: decision semantic_key is required")
            staged["confirmed_decisions"] = [
                deepcopy(dict(item))
                for item in staged.get("confirmed_decisions", []) or []
                if isinstance(item, Mapping)
                and _semantic_key(item.get("semantic_key")) != semantic_key
            ]
        elif kind == "mark_ambiguity":
            ambiguity = operation.get("ambiguity")
            if not isinstance(ambiguity, Mapping):
                raise ValueError("TASKSPEC_MUTATION_STRUCTURE_INVALID: ambiguity must be an object")
            staged = merge_task_spec(
                staged,
                {"unresolved_questions": [ambiguity]},
                source="consultant_inferred",
                reason="consultant_mutation:mark_ambiguity",
                increment_revision=False,
            )

    candidate = canonical_current_task_spec(
        staged,
        base_dir=base_dir,
        project_root=project_root,
    )
    semantic_changed = task_semantic_hash(candidate) != before_hash
    if not was_initialized and task_spec_is_semantic_empty(candidate):
        raise ValueError("TASKSPEC_BOOTSTRAP_INCOMPLETE: Consultant candidate has no task semantics")
    candidate["revision"] = base_revision + 1 if semantic_changed else base_revision
    if not was_initialized:
        candidate["revision"] = 1
    return candidate


def constraint_mutation_proposal(
    constraints: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build the closed proposal used after Controller constraint validation."""
    return {
        "proposal_type": "task_spec_mutation",
        "schema_version": TASK_SPEC_MUTATION_SCHEMA_VERSION,
        "operations": [
            {
                "op": "upsert_constraint",
                "constraint": deepcopy(dict(item)),
                "reason": "validated_consultant_semantic_delta",
                "supporting_evidence": [],
            }
            for item in constraints or []
            if isinstance(item, Mapping)
        ],
        "unresolved_ambiguities": [],
        "proposal_evidence_refs": [],
    }


def semantic_constraints_from_task_spec(
    spec: Mapping[str, Any] | None,
    *,
    selected_skill_ids: Iterable[Any] = (),
) -> List[Dict[str, Any]]:
    """Return the canonical semantic contract carried by existing TaskSpec rows."""
    normalized = ensure_task_spec(spec)
    constraints: List[Dict[str, Any]] = []
    primary_message_id = next(
        (
            _text(item.get("source_message_id"))
            for item in normalized.get("user_directives", []) or []
            if isinstance(item, Mapping) and _text(item.get("source_message_id"))
        ),
        _text(normalized.get("raw_submission_ref"))
        or next(
            (_text(value) for value in normalized.get("source_refs", []) or [] if _text(value)),
            "",
        ),
    )

    def add(value: Mapping[str, Any]) -> None:
        item = normalize_requirement(value, source=_text(value.get("source") or "task_contract"))
        key = _semantic_key(item.get("semantic_key"))
        if not key or item.get("value") in (None, ""):
            return
        item["semantic_key"] = key
        provenance = [
            deepcopy(entry)
            for entry in item.get("provenance", []) or []
            if isinstance(entry, Mapping) and _provenance_source_id(entry)
        ]
        source_id = _text(item.get("source_id") or item.get("source_ref"))
        if not source_id:
            source_id = primary_message_id
        if source_id and not provenance:
            source_type = _binding_source(
                _text(item.get("source_type") or item.get("source") or "task_contract")
            )
            provenance = [{
                "source_type": source_type,
                "source_id": source_id,
                "source_ref": source_id,
            }]
        if provenance:
            item["provenance"] = provenance
            item["source_id"] = _provenance_source_id(provenance[0])
            item["source_ref"] = _provenance_source_id(provenance[0])
        identity = json.dumps(
            {
                "semantic_key": key,
                "operator": item.get("operator"),
                "value": item.get("value"),
                "condition": item.get("condition"),
                "strength": item.get("strength"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if not _text(item.get("constraint_id")):
            item["constraint_id"] = "constraint:" + hashlib.sha256(
                json.dumps(
                    {
                        "semantic_identity": identity,
                        "provenance": provenance,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        if not any(existing.get("semantic_identity") == identity for existing in constraints):
            item["semantic_identity"] = identity
            constraints.append(item)

    for value in normalized.get("requirements", []) or []:
        if isinstance(value, Mapping):
            add(value)
    # Structured inputs, outputs, parameters, and selected skills remain in
    # their own machine contracts.  Only explicit keyed legacy/typed-policy
    # rows enter this compatibility registry; fresh open text never does.
    for item in constraints:
        item.pop("semantic_identity", None)
    return constraints


def semantic_provenance_catalog(
    spec: Mapping[str, Any] | None,
    *,
    consultation_state: Mapping[str, Any] | None = None,
    selected_skill_ids: Iterable[Any] = (),
) -> List[Dict[str, str]]:
    """Build the finite source-ID catalog accepted during Consultant finalize."""
    normalized = ensure_task_spec(spec)
    entries: Dict[str, Dict[str, str]] = {}

    def add(source_id: Any, source_type: Any, label: Any = "") -> None:
        identifier = _text(source_id)
        if not identifier:
            return
        entries.setdefault(identifier, {
            "source_id": identifier,
            "source_type": _binding_source(_text(source_type) or "task_contract"),
            "label": _text(label)[:240],
        })

    for source_ref in normalized.get("source_refs", []) or []:
        source_text = _text(source_ref)
        source_type = (
            "user_explicit"
            if source_text.startswith(("msg-", "object:sha256:"))
            else "skill_catalog" if source_text.startswith("skill:")
            else "task_contract"
        )
        add(source_ref, source_type, "canonical TaskSpec source")
    add(normalized.get("raw_submission_ref"), "user_explicit", "raw submission object")

    envelope = normalized.get("request_envelope")
    if isinstance(envelope, Mapping):
        add(envelope.get("source_ref"), envelope.get("source_type"), "request envelope")
    for directive in normalized.get("user_directives", []) or []:
        if not isinstance(directive, Mapping):
            continue
        add(
            directive.get("source_message_id"),
            directive.get("source") or "user_explicit",
            directive.get("kind") or "user directive",
        )
    for field in (*FACT_FIELDS.values(), "facts"):
        for fact in normalized.get(field, []) or []:
            if not isinstance(fact, Mapping):
                continue
            add(fact.get("fact_id"), fact.get("source"), fact.get("fact_kind") or field)
            add(fact.get("source_message_id"), fact.get("source"), "fact source message")
    for field, values in (
        ("input", normalized.get("inputs", []) or []),
        ("output", effective_output_contracts(normalized)),
        ("requirement", normalized.get("requirements", []) or []),
    ):
        for value in values:
            if not isinstance(value, Mapping):
                continue
            add(value.get("source_id") or value.get("source_ref"), value.get("source_type") or value.get("source"), field)
            for provenance in value.get("provenance", []) or []:
                if isinstance(provenance, Mapping):
                    add(_provenance_source_id(provenance), provenance.get("source_type"), field)

    controller = dict(consultation_state or {})
    for evidence in controller.get("evidence", []) or []:
        if isinstance(evidence, Mapping):
            add(evidence.get("evidence_id"), "runtime_evidence", "consultation evidence")
    for requirement in (controller.get("requirements") or {}).values():
        if not isinstance(requirement, Mapping):
            continue
        requirement_id = _text(requirement.get("id"))
        if requirement_id and _text(requirement.get("status")) in {"satisfied", "waived"}:
            add(f"requirement:{requirement_id}", "controller_evidence", requirement.get("purpose"))
        evidence_provenance = requirement.get("evidence_provenance")
        if isinstance(evidence_provenance, Mapping):
            skill_id = _text(evidence_provenance.get("skill_id"))
            if skill_id:
                add(f"skill:{skill_id}", "skill_catalog", skill_id)
            for skill_id in evidence_provenance.get("candidate_ids", []) or []:
                add(f"skill:{skill_id}", "skill_catalog", skill_id)
    for skill_id in selected_skill_ids or []:
        value = _text(skill_id)
        if value:
            add(f"skill:{value}", "skill_catalog", value)
    return [entries[key] for key in sorted(entries)]


def canonical_constraint_hash(spec: Mapping[str, Any] | None) -> str:
    """Hash semantic meaning only; raw prompt text and timestamps are provenance."""
    payload = [
        {
            key: deepcopy(item.get(key))
            for key in (
                "semantic_key", "operator", "value", "strength", "condition",
                "needs_clarification", "ambiguity",
            )
            if item.get(key) not in (None, "", [], {})
        }
        for item in semantic_constraints_from_task_spec(spec)
    ]
    payload.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# The semantic and record hash domains are intentionally separate.  In
# particular, source/provenance additions must never invalidate an approved
# execution contract when the task meaning did not change.  Keep this list
# explicit: adding a field here is an approval-sensitive schema change.
_TASK_SEMANTIC_FIELDS = (
    "goal",
    "inputs",
    "parameters",
    "output_contracts",
    "confirmed_decisions",
    "unresolved_questions",
    "requirements",
    "capability_intent",
)
_TASK_NON_SEMANTIC_KEYS = {
    "schema_version",
    "canonicalization_version",
    "task_id",
    "revision",
    "task_revision",
    "task_semantic_hash",
    "task_record_hash",
    "recorded_at",
    "created_at",
    "updated_at",
    "timestamp",
    "timestamps",
    "source",
    "source_id",
    "source_ref",
    "source_refs",
    "source_type",
    "requirement_id",
    "constraint_id",
    "provenance",
    "evidence",
    "evidence_ref",
    "evidence_refs",
    "audit_ref",
    "audit_refs",
    "change_log",
    "migrations_applied",
    "normalization_audit",
    "input_alias_audit",
}
_TASK_RECORD_EXCLUDED_KEYS = {
    "task_semantic_hash",
    "task_record_hash",
    "effective_user_facts",
    "semantic_constraints",
    "projection_cache",
    "derived",
}


def _canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON encoding used by TaskSpec identities."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _task_semantic_value(value: Any) -> Any:
    """Strip provenance/record metadata recursively without summarizing meaning."""
    if isinstance(value, Mapping):
        return {
            str(key): _task_semantic_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _TASK_NON_SEMANTIC_KEYS
            and item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [_task_semantic_value(item) for item in value]
    if isinstance(value, tuple):
        return [_task_semantic_value(item) for item in value]
    return deepcopy(value)


def task_semantic_payload(spec: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Canonical execution semantics for approval and staleness decisions.

    This function deliberately does not call a node projection.  Node views
    may select a closure from this model, but no projection is authoritative
    for the task identity.
    """
    source = dict(spec or {})
    payload = {
        field: _task_semantic_value(source.get(field))
        for field in _TASK_SEMANTIC_FIELDS
        if source.get(field) not in (None, "", [], {})
    }
    return {
        "canonicalization_version": TASK_CANONICALIZATION_VERSION,
        "semantic": payload,
    }


def task_record_payload(spec: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Canonical complete TaskSpec record, excluding derived/cache aliases."""
    return {
        "canonicalization_version": TASK_CANONICALIZATION_VERSION,
        "record": {
            str(key): deepcopy(value)
            for key, value in sorted(dict(spec or {}).items(), key=lambda pair: str(pair[0]))
            if str(key) not in _TASK_RECORD_EXCLUDED_KEYS
        },
    }


def task_semantic_hash(spec: Mapping[str, Any] | None) -> str:
    """Return the correctness-critical TaskSpec semantic identity."""
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(task_semantic_payload(spec))).hexdigest()


def task_record_hash(spec: Mapping[str, Any] | None) -> str:
    """Return the integrity identity for the complete canonical TaskSpec record."""
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(task_record_payload(spec))).hexdigest()


def apply_authorized_task_spec_constraint_repair(
    spec: Mapping[str, Any] | None,
    payload: Mapping[str, Any],
    *,
    allowed_semantic_slots: Iterable[str],
    provenance_catalog: Any,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> TaskSpec:
    """Apply one revision-bound, provenance-checked Consultant repair.

    The payload can mutate only TaskSpec constraint slots named by the
    controller-derived ValidationIssues. It cannot mutate plans, selected
    skills, renderer metadata, validation state, or approval state.
    """
    current = ensure_task_spec(spec, base_dir=base_dir, project_root=project_root)
    patch = dict(payload or {})
    expected_fields = {
        "base_task_spec_revision", "constraints", "needs_user_input", "question",
    }
    unexpected = sorted(set(patch) - expected_fields)
    missing = sorted(expected_fields - set(patch))
    if unexpected or missing:
        raise ValueError(
            "TASKSPEC_REPAIR_STRUCTURE_INVALID: "
            f"missing={missing}; unexpected={unexpected}"
        )
    base_revision = task_spec_revision(current)
    try:
        requested_revision = int(patch.get("base_task_spec_revision"))
    except (TypeError, ValueError) as exc:
        raise ValueError("TASKSPEC_REPAIR_BASE_REVISION_INVALID") from exc
    if requested_revision != base_revision:
        raise ValueError(
            f"TASKSPEC_REPAIR_STALE_REVISION: expected {base_revision}, got {requested_revision}"
        )
    if not isinstance(patch.get("needs_user_input"), bool):
        raise ValueError("TASKSPEC_REPAIR_STRUCTURE_INVALID: needs_user_input must be boolean")
    if not isinstance(patch.get("question"), str):
        raise ValueError("TASKSPEC_REPAIR_STRUCTURE_INVALID: question must be string")
    if patch["needs_user_input"]:
        raise ValueError("TASKSPEC_REPAIR_REQUIRES_USER_INPUT")
    constraints = patch.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        raise ValueError("TASKSPEC_REPAIR_STRUCTURE_INVALID: constraints must be a non-empty array")
    allowed = {_field_key(value) for value in allowed_semantic_slots if _field_key(value)}
    if not allowed:
        raise ValueError("TASKSPEC_REPAIR_SCOPE_EMPTY")
    existing_legacy_keys = {
        _semantic_key(item.get("semantic_key"))
        for item in current.get("requirements", []) or []
        if isinstance(item, Mapping) and _semantic_key(item.get("semantic_key"))
    }
    for index, raw in enumerate(constraints):
        if not isinstance(raw, Mapping):
            raise ValueError(f"TASKSPEC_REPAIR_STRUCTURE_INVALID: constraints[{index}] must be an object")
        semantic_key = _semantic_key(raw.get("semantic_key"))
        slot = semantic_key.split(".", 1)[1] if "." in semantic_key else ""
        if not slot or _field_key(slot) not in allowed:
            raise ValueError(
                f"TASKSPEC_REPAIR_MUTATION_FORBIDDEN: constraints[{index}].semantic_key={semantic_key or '<invalid>'}"
            )
        typed_policy = bool(
            semantic_key.startswith("failure_policy.")
            or semantic_key == "forbidden_behavior.runtime_install"
        )
        if semantic_key not in existing_legacy_keys and not typed_policy:
            raise ValueError(
                "TASKSPEC_REPAIR_GENERIC_ONTOLOGY_RETIRED: "
                f"constraints[{index}].semantic_key={semantic_key or '<invalid>'}"
            )
    validation = validate_compiled_constraints(
        constraints,
        provenance_catalog=provenance_catalog,
        require_resolvable_provenance=True,
        base_constraints=semantic_constraints_from_task_spec(current),
    )
    if not validation.get("valid"):
        raise ValueError(
            "TASKSPEC_REPAIR_CONSTRAINT_INVALID: "
            + json.dumps(validation.get("errors") or [], ensure_ascii=False, sort_keys=True)
        )
    before_hash = canonical_constraint_hash(current)
    repaired = merge_compiled_constraints(
        current,
        validation.get("constraints") or [],
        base_dir=base_dir,
        project_root=project_root,
        metrics={"repair_mode": "authorized_taskspec_constraint_patch"},
    )
    after_hash = canonical_constraint_hash(repaired)
    if before_hash == after_hash:
        raise ValueError("TASKSPEC_REPAIR_NO_OP")
    repaired["revision"] = base_revision + 1
    return canonical_current_task_spec(repaired)


def semantic_install_policy(
    spec: Mapping[str, Any] | None,
    *,
    dependency_key: str = "",
) -> Dict[str, Any]:
    """Resolve install policy from typed constraints only.

    ``dependency_key`` is an ontology slot (for example ``java``), never raw
    user prose.  Runtime code must not recover this policy with language
    keywords after Consultant has compiled the request.
    """
    dependency_slot = _field_key(dependency_key)
    accepted_failure_keys = {"failure_policy.runtime_missing"}
    if dependency_slot:
        accepted_failure_keys.add(f"failure_policy.{dependency_slot}_missing")
    for item in semantic_constraints_from_task_spec(spec):
        semantic_key = str(item.get("semantic_key") or "")
        operator = str(item.get("operator") or "")
        value = _field_key(item.get("value"))
        if (
            semantic_key == "forbidden_behavior.runtime_install"
            and operator == "forbid"
            and item.get("value") not in (False, 0, "false", "False")
        ):
            return {"install_allowed": False, "reason": semantic_key, "constraint": deepcopy(item)}
        if semantic_key in accepted_failure_keys and operator == "on_condition" and value in {"block", "blocked", "stop"}:
            return {"install_allowed": False, "reason": semantic_key, "constraint": deepcopy(item)}
    return {"install_allowed": True, "reason": "", "constraint": {}}


def evaluate_failure_policies(
    spec: Mapping[str, Any] | None,
    observed_conditions: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Apply typed ``failure_policy`` rows to typed runtime conditions."""
    observed = {
        str(key).strip().lower(): value
        for key, value in dict(observed_conditions or {}).items()
        if str(key).strip()
    }
    triggered: List[Dict[str, Any]] = []
    for item in semantic_constraints_from_task_spec(spec):
        semantic_key = str(item.get("semantic_key") or "")
        if not semantic_key.startswith("failure_policy.") or str(item.get("operator") or "") != "on_condition":
            continue
        slot = semantic_key.split(".", 1)[1]
        condition = item.get("condition")
        active = False
        if isinstance(condition, Mapping):
            condition_key = str(condition.get("semantic_key") or condition.get("key") or "").strip().lower()
            expected = condition.get("value", True)
            condition_operator = str(condition.get("operator") or "eq").strip().lower()
            if condition_key in observed:
                active = (
                    observed[condition_key] == expected
                    if condition_operator == "eq"
                    else observed[condition_key] != expected
                )
        elif str(condition or "").strip():
            active = bool(observed.get(str(condition).strip().lower()))
        else:
            active = bool(
                observed.get(slot)
                or observed.get(semantic_key)
                or observed.get(f"runtime_constraint.{slot}")
            )
        policy = _field_key(item.get("value"))
        if active and policy in {"block", "blocked", "stop"}:
            triggered.append(deepcopy(item))
    return {
        "blocked": bool(triggered),
        "triggered": triggered,
        "reason": (
            "Typed failure policy blocked execution: "
            + ", ".join(str(item.get("semantic_key")) for item in triggered)
            if triggered else ""
        ),
    }


def runtime_failure_policy(
    spec: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Resolve the closed runtime-unavailable route from typed TaskSpec rows."""
    unavailable_action = "fail_task"
    allow_substitution = False
    sources: List[Dict[str, Any]] = []
    for item in semantic_constraints_from_task_spec(spec):
        semantic_key = str(item.get("semantic_key") or "")
        value = _field_key(item.get("value"))
        if semantic_key in {
            "failure_policy.runtime_unavailable",
            "failure_policy.runtime_missing",
        }:
            normalized = {
                "fail": "fail_task",
                "failed": "fail_task",
                "stop": "fail_task",
                "block": "block_stage_and_continue_closeout",
                "blocked": "block_stage_and_continue_closeout",
                "continue_closeout": "block_stage_and_continue_closeout",
            }.get(value, value)
            if normalized in {
                "fail_task", "block_stage_and_continue_closeout"
            }:
                unavailable_action = normalized
                sources.append(deepcopy(item))
        elif semantic_key == "failure_policy.allow_substitution":
            allow_substitution = item.get("value") in {
                True, 1, "1", "true", "True", "yes", "allow", "allowed"
            }
            sources.append(deepcopy(item))
    return {
        "runtime_unavailable": unavailable_action,
        "allow_substitution": bool(allow_substitution),
        "source": "typed_task_spec" if sources else "controller_default",
        "constraints": sources,
    }


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
        artifact_locator(item)
        for item in normalized.get("output_contracts", [])
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
        raw_source = _field_key(item.get("source"))
        if raw_source not in {
            "explicit", "user", "user_initial", "user_feedback",
            "user_explicit", "consultant_confirmed", "consultant_selected",
        }:
            # Local, sibling, sidecar, and directory discovery are observable
            # candidates. They become TaskSpec inputs only after an explicit
            # Consultant confirmation, never merely because a path exists.
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


def apply_declarative_task_spec_rules(
    spec: Mapping[str, Any] | None,
    renderer_records: Iterable[Mapping[str, Any]],
) -> TaskSpec:
    """Materialize skill-declared binding rules as structured parameters.

    Conditions operate only on already typed TaskSpec rows.  No natural
    language, filename extension, tool name, or skill-ID branch participates
    in this transition.
    """
    normalized = ensure_task_spec(spec)
    input_roles = {
        _field_key(item.get("role"))
        for item in normalized.get("inputs", []) or []
        if isinstance(item, Mapping) and _field_key(item.get("role"))
    }
    existing_parameters = dict(normalized.get("parameters") or {})
    additions: Dict[str, Any] = {}
    for renderer in renderer_records or []:
        if not isinstance(renderer, Mapping):
            continue
        rules = renderer.get("task_spec_rules") or []
        if isinstance(rules, Mapping):
            rules = [rules]
        for raw_rule in rules:
            if not isinstance(raw_rule, Mapping):
                continue
            all_inputs = {
                _field_key(item) for item in raw_rule.get("when_all_input_roles", []) or []
                if _field_key(item)
            }
            any_inputs = {
                _field_key(item) for item in raw_rule.get("when_any_input_roles", []) or []
                if _field_key(item)
            }
            if all_inputs and not all_inputs.issubset(input_roles):
                continue
            if any_inputs and not any_inputs.intersection(input_roles):
                continue
            semantic_key = _semantic_key(raw_rule.get("semantic_key"))
            if not semantic_key or "value" not in raw_rule:
                continue
            role = _field_key(
                raw_rule.get("role")
                or semantic_key.split(".", 1)[-1]
            )
            if not role or role in existing_parameters or role in additions:
                continue
            additions[role] = deepcopy(raw_rule.get("value"))
    if not additions:
        return normalized
    merged = merge_task_spec(
        normalized,
        {"parameters": additions},
        source="skill_contract_derived",
        reason="declarative_skill_task_spec_rules",
        increment_revision=False,
    )
    merged.setdefault("semantic_rule_audit", []).append({
        "rule_source": "skill_metadata.task_spec_rules",
        "added_parameter_keys": list(additions),
        "recorded_at": _now(),
    })
    return merged


def _binding_rule_sources(
    argument: str,
    invocation_schema: Mapping[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Return declarative sources for one renderer argument.

    Rules live in skill metadata.  The controller interprets only this small
    generic vocabulary and never branches on a skill ID, tool name, or user
    prose.  Older skills without rules continue to use the schema aliases.
    """
    raw_rules = dict((invocation_schema or {}).get("binding_rules") or {})
    raw = raw_rules.get(argument) or {}
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    if not isinstance(raw, Mapping):
        return []
    sources = raw.get("sources") or []
    if isinstance(sources, Mapping):
        sources = [sources]
    return [dict(item) for item in sources if isinstance(item, Mapping)]


def _rule_condition_matches(
    condition: Mapping[str, Any] | None,
    requirements: Mapping[str, Mapping[str, Any]],
    candidates: Mapping[str, List[Dict[str, Any]]],
) -> bool:
    if not condition:
        return True
    role = _field_key(condition.get("role"))
    operator = _field_key(condition.get("operator") or "eq")
    expected = condition.get("value")
    present = bool(candidates.get(role))
    actual = (requirements.get(role) or {}).get("value")
    if operator in {"present", "exists"}:
        return present or actual not in (None, "")
    if operator in {"absent", "missing"}:
        return not present and actual in (None, "")
    if operator in {"in", "one_of"}:
        return actual in list(expected or [])
    return actual == expected


def _projection_missing_detail(
    argument: str,
    meta: Mapping[str, Any],
    invocation_schema: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    rules = dict((invocation_schema or {}).get("binding_rules") or {})
    rule = rules.get(argument) if isinstance(rules.get(argument), Mapping) else {}
    aliases = {_field_key(value) for value in meta.get("aliases", []) or []}
    aliases.add(argument)
    input_role = next(
        (
            role for role, names in INPUT_ROLE_ARGUMENT_CANDIDATES.items()
            if aliases.intersection({_field_key(name) for name in names})
        ),
        "",
    )
    owner = _field_key((rule or {}).get("owner"))
    if owner not in {"planner", "task_spec", "renderer_projection", "external_input", "internal"}:
        owner = "external_input" if input_role else "task_spec"
    repair_target = _text((rule or {}).get("repair_target"))
    if not repair_target:
        repair_target = (
            f"task_spec.inputs[role={input_role}]"
            if input_role
            else f"task_spec.requirements[role={argument}]"
        )
    return {
        "argument": argument,
        "owner": owner,
        "repair_target": repair_target,
        "field_path": f"renderer.invocation.arguments.{argument}",
        "required": True,
    }


def project_task_spec_to_renderer(
    spec: Mapping[str, Any] | None,
    invocation_schema: Mapping[str, Any] | None,
    *,
    task_id: str = "",
) -> Dict[str, Any]:
    """Project structured task bindings into a renderer argument schema."""
    normalized = ensure_task_spec(spec)
    argument_specs = _renderer_argument_specs(invocation_schema)
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for item in normalized.get("inputs", []) or []:
        if not isinstance(item, Mapping):
            continue
        binding = normalize_input_binding(item)
        candidates.setdefault(binding["role"], []).append(binding)
    # Renderer binding is a machine contract.  Read canonical structured
    # inputs/outputs/parameters directly; arbitrary highlighted prose is not a
    # source of renderer arguments.  Explicit keyed rows remain read-old or
    # bounded typed-policy compatibility inputs.
    requirement_rows = [
        normalize_requirement(item)
        for item in semantic_constraints_from_task_spec(normalized)
        if isinstance(item, Mapping)
    ]
    for parameter_key, parameter_value in dict(normalized.get("parameters") or {}).items():
        if parameter_value in (None, ""):
            continue
        role = _field_key(str(parameter_key).rsplit(".", 1)[-1])
        if not role:
            continue
        canonical = canonicalize_compiled_constraint({
            "semantic_key": f"param.{role}",
            "operator": "eq",
            "value": parameter_value,
        })
        requirement_rows.append({
            "requirement_id": f"parameter:{parameter_key}",
            "role": (
                _semantic_key(canonical.get("semantic_key")).split(".", 1)[1]
                if _semantic_key(canonical.get("semantic_key"))
                else role
            ),
            "semantic_key": _semantic_key(canonical.get("semantic_key")),
            "operator": "eq",
            "value": deepcopy(canonical.get("value", parameter_value)),
            "strength": "required",
            "source": "task_spec.parameters",
        })
    requirements = {
        _field_key(item.get("role")): item
        for item in requirement_rows
        if _field_key(item.get("role"))
    }
    outputs = {
        _field_key(item.get("role")): item
        for item in normalized.get("output_contracts", []) or []
        if isinstance(item, Mapping)
    }
    projected: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    unverified: List[str] = []

    def choose(role: str) -> Dict[str, Any]:
        items = list(candidates.get(role, []))
        canonical = requirements.get(role)
        canonical_key = _semantic_key(canonical.get("semantic_key")) if canonical else ""
        canonical_path = _text(
            (canonical or {}).get("path") or (canonical or {}).get("value")
        )
        if canonical_key == f"required_input.{role}" and canonical_path:
            matching = [
                item for item in items
                if artifact_locator(item) == canonical_path
            ]
            if matching:
                items = matching
            else:
                # The compiled requirement is the semantic authority, but a
                # requirement row alone is not a resolved input binding.
                if items:
                    conflicts.append({
                        "role": role,
                        "paths": sorted({artifact_locator(item) for item in items if artifact_locator(item)}),
                        "required_path": canonical_path,
                        "reason": "canonical_requirement_binding_mismatch",
                    })
                return {}
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
            distinct = {artifact_locator(item) for item in tied}
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
                value = artifact_locator(binding)
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
            value = artifact_locator(outputs["output_dir"])
            source_detail = {"kind": "output", "role": "output_dir", "source": outputs["output_dir"].get("source")}
        if value is None:
            for rule in _binding_rule_sources(argument, invocation_schema):
                if not _rule_condition_matches(
                    rule.get("when") if isinstance(rule.get("when"), Mapping) else {},
                    requirements,
                    candidates,
                ):
                    continue
                kind = _field_key(rule.get("kind"))
                role = _field_key(rule.get("role"))
                if kind == "requirement" and requirements.get(role):
                    value = requirements[role].get("value")
                    source_detail = {
                        "kind": "requirement",
                        "role": role,
                        "source": requirements[role].get("source"),
                        "rule_id": _text(rule.get("rule_id")),
                    }
                elif kind == "output" and outputs.get(role):
                    value = artifact_locator(outputs[role]) or outputs[role].get("value")
                    source_detail = {
                        "kind": "output",
                        "role": role,
                        "source": outputs[role].get("source"),
                        "rule_id": _text(rule.get("rule_id")),
                    }
                elif kind == "stable_task_identifier":
                    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", _text(rule.get("prefix") or "biomind")).strip("_.-")
                    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", _text(task_id)).strip("_.-")
                    value = f"{prefix or 'biomind'}_{slug or 'task'}"
                    source_detail = {
                        "kind": "derived",
                        "role": role,
                        "source": "controller_derived_from_skill_contract",
                        "rule_id": _text(rule.get("rule_id")),
                        "stable": True,
                    }
                if value not in (None, ""):
                    break
        if value not in (None, ""):
            projected[argument] = value
            sources[argument] = source_detail

    outdir = projected.pop("outdir", projected.pop("output_dir", ""))
    semantic_claims: List[Dict[str, Any]] = []
    routed_arguments = dict((invocation_schema or {}).get("routed_arguments") or {})
    for requirement in requirement_rows:
        semantic_key = _semantic_key(requirement.get("semantic_key"))
        role = _field_key(requirement.get("role") or requirement.get("requirement_id"))
        if not semantic_key or not role:
            continue
        mapped = role in projected and projected.get(role) == requirement.get("value")
        mapped = mapped or any(
            _field_key(detail.get("role")) == role
            for detail in sources.values()
            if isinstance(detail, Mapping)
        )
        route = routed_arguments.get(role)
        if not isinstance(route, Mapping):
            route = next(
                (
                    value for value in routed_arguments.values()
                    if isinstance(value, Mapping) and _field_key(value.get("selector")) == role
                ),
                None,
            )
        if isinstance(route, Mapping):
            target = dict(route.get("routes") or {}).get(str(requirement.get("value")))
            mapped = mapped or bool(target and _field_key(target) in projected)
        if mapped:
            semantic_claims.append({
                "semantic_key": semantic_key,
                "operator": requirement.get("operator") or "eq",
                "value": deepcopy(requirement.get("value")),
                "strength": "required",
                "confidence": 1.0,
                "source_ref": f"renderer_projection:{role}",
            })
    if outdir:
        semantic_claims.append({
            "semantic_key": "required_output.output_dir",
            "operator": "eq",
            "value": outdir,
            "strength": "required",
            "confidence": 1.0,
            "source_ref": "renderer_projection:output_dir",
        })
    missing = [
        argument for argument, meta in argument_specs.items()
        if meta.get("required") and argument not in projected and not (
            argument in {"outdir", "output_dir"} and outdir
        )
    ]
    missing_details = [
        _projection_missing_detail(argument, argument_specs[argument], invocation_schema)
        for argument in missing
    ]
    status = (
        "NOT_APPLICABLE"
        if not argument_specs
        else "COMPLETE" if not missing and not conflicts else "INCOMPLETE"
    )
    return {
        "status": status,
        "arguments": projected,
        "outdir": outdir,
        "missing_bindings": missing,
        "missing_binding_details": missing_details,
        "conflicts": conflicts,
        "unverified_bindings": sorted(set(unverified)),
        "binding_sources": sources,
        "semantic_claims": semantic_claims,
        "complete": status == "COMPLETE",
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
    candidates = extract_user_fact_candidates(answer)
    if skill_contract:
        candidates = normalize_fact_candidates_with_skill(candidates, skill_contract)
    contracts = extract_request_contracts(
        answer,
        base_dir=base_dir,
        project_root=project_root,
    )
    semantic_requirements = _requirements_from_fact_candidates(
        candidates,
        source="user_explicit",
        source_ref=message_id,
    )
    patch: Dict[str, Any] = {
        "inputs": contracts.get("inputs", []),
        "requirements": _merge_list(contracts.get("requirements", []), semantic_requirements),
        "output_contracts": contracts.get("output_contracts", []),
        "parameters": _parameters_from_candidates(candidates),
        "source_refs": [message_id],
    }
    has_structured_semantics = bool(
        candidates
        or contracts.get("inputs")
        or contracts.get("requirements")
        or contracts.get("output_contracts")
    )
    if change_type != "plan_feedback" and (_text(question) or not has_structured_semantics) and answer:
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
    current = ensure_task_spec(spec)
    before_hash = task_semantic_hash(current)
    updated = merge_task_spec(
        spec,
        patch,
        source="user",
        reason=f"{kind}:{change_type}",
        increment_revision=False,
    )
    updated = _apply_canonical_artifact_mutations(
        updated,
        candidates,
        source_ref=message_id,
    )
    semantic_changed = task_semantic_hash(updated) != before_hash
    updated["revision"] = (
        current_revision + 1
        if increments_revision and semantic_changed
        else current_revision
    )
    return canonical_current_task_spec(updated)


def register_validated_semantic_ambiguity(
    spec: Mapping[str, Any] | None,
    ambiguity: Mapping[str, Any],
    *,
    provenance_catalog: Any,
) -> TaskSpec:
    """Register one controller-validated semantic ambiguity in ``TaskSpec``.

    Finalize ``ask`` responses are allowed to *reference* an existing ambiguity,
    never to create one.  When a schema-valid finish delta discovers a new
    ambiguity, the controller uses this transition first so the ambiguity gets
    a stable ID and a new TaskSpec revision before any user-facing question is
    emitted.
    """
    raw = deepcopy(dict(ambiguity or {}))
    if not _text(raw.get("constraint_id") or raw.get("requirement_id")):
        identity = {
            "semantic_key": _semantic_key(raw.get("semantic_key")),
            "operator": _field_key(raw.get("operator")),
            "value": raw.get("value"),
            "strength": _field_key(raw.get("strength")),
            "condition": raw.get("condition"),
            "provenance": raw.get("provenance") or [],
        }
        raw["constraint_id"] = (
            "ambiguity-"
            + hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:20]
        )
    raw.setdefault("requirement_id", raw.get("constraint_id"))
    raw["needs_clarification"] = bool(raw.get("needs_clarification"))
    validation = validate_compiled_constraints(
        [raw],
        provenance_catalog=provenance_catalog,
        require_resolvable_provenance=True,
    )
    if not validation.get("valid"):
        raise ValueError(
            "Invalid semantic ambiguity: "
            + json.dumps(validation.get("errors") or [], ensure_ascii=False, sort_keys=True)
        )
    normalized = deepcopy(validation["constraints"][0])
    ambiguity_id = _text(
        normalized.get("constraint_id")
        or normalized.get("requirement_id")
        or raw.get("constraint_id")
    )
    normalized["constraint_id"] = ambiguity_id
    normalized["requirement_id"] = ambiguity_id
    normalized["needs_clarification"] = bool(normalized.get("needs_clarification"))
    normalized["ambiguity"] = normalized.get("ambiguity") or True
    normalized["status"] = "unresolved"
    normalized["source"] = "consultant_inferred"
    normalized["source_type"] = "consultant_inferred"
    critical_strengths = {"hard", "required", "conditional", "ambiguous"}
    if not normalized["needs_clarification"]:
        raise ValueError("Semantic ambiguity must set needs_clarification=true")
    if _field_key(normalized.get("strength")) not in critical_strengths:
        raise ValueError("Only a critical semantic ambiguity may be registered")
    unresolved_reason = _text(raw.get("unresolved_reason"))
    if not unresolved_reason:
        raise ValueError("Semantic ambiguity requires unresolved_reason")
    normalized["unresolved_reason"] = unresolved_reason
    if isinstance(raw.get("alternatives"), list):
        normalized["alternatives"] = deepcopy(raw["alternatives"])

    updated = ensure_task_spec(spec)
    requirements = [
        deepcopy(item)
        for item in updated.get("requirements", []) or []
        if isinstance(item, Mapping)
    ]
    existing_index = next(
        (
            index
            for index, item in enumerate(requirements)
            if _text(item.get("constraint_id") or item.get("requirement_id")) == ambiguity_id
        ),
        None,
    )
    if existing_index is not None:
        existing = requirements[existing_index]
        if _semantic_key(existing.get("semantic_key")) != _semantic_key(normalized.get("semantic_key")):
            raise ValueError("Ambiguity ID already belongs to a different semantic key")
        requirements[existing_index] = normalized
    else:
        requirements.append(normalized)

    next_revision = task_spec_revision(updated) + 1
    normalized["created_revision"] = next_revision
    updated["requirements"] = requirements
    unresolved_questions = [
        deepcopy(item)
        for item in updated.get("unresolved_questions", []) or []
        if not (
            isinstance(item, Mapping)
            and _text(item.get("ambiguity_id") or item.get("constraint_id")) == ambiguity_id
        )
    ]
    unresolved_questions.append({
        "ambiguity_id": ambiguity_id,
        "constraint_id": ambiguity_id,
        "semantic_key": normalized.get("semantic_key"),
        "unresolved_reason": unresolved_reason,
        "alternatives": deepcopy(normalized.get("alternatives") or []),
        "needs_clarification": True,
        "strength": normalized.get("strength"),
        "provenance": deepcopy(normalized.get("provenance") or []),
        "created_revision": next_revision,
        "active": True,
    })
    updated["unresolved_questions"] = unresolved_questions
    updated["revision"] = next_revision
    return canonical_current_task_spec(updated)


def resolve_registered_semantic_ambiguity(
    spec: Mapping[str, Any] | None,
    ambiguity_id: str,
    answer: str,
    *,
    question: str = "",
    source_message_id: str = "",
    source_task_id: str = "",
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> TaskSpec:
    """Resolve exactly one previously registered ambiguity from a user answer."""
    identifier = _text(ambiguity_id)
    response = _text(answer)
    if not identifier or not response:
        raise ValueError("Resolving a semantic ambiguity requires its ID and a non-empty answer")
    current = ensure_task_spec(spec)
    matching = [
        item
        for item in current.get("requirements", []) or []
        if isinstance(item, Mapping)
        and _text(item.get("constraint_id") or item.get("requirement_id")) == identifier
    ]
    if len(matching) != 1 or not bool(matching[0].get("needs_clarification")):
        raise ValueError("Ambiguity ID is not an active TaskSpec semantic ambiguity")

    message_id = _text(source_message_id) or _message_id(response, source_task_id)
    updated = apply_user_feedback(
        current,
        response,
        kind="consultant_semantic_answer",
        question=question,
        change_type="requirement_change",
        source_message_id=message_id,
        source_task_id=source_task_id,
        message_origin="user",
        base_dir=base_dir,
        project_root=project_root,
    )
    resolved = False
    for requirement in updated.get("requirements", []) or []:
        if not isinstance(requirement, dict):
            continue
        if _text(requirement.get("constraint_id") or requirement.get("requirement_id")) != identifier:
            continue
        requirement.update({
            "value": response,
            "needs_clarification": False,
            "ambiguity": False,
            "unresolved_reason": "",
            "status": "satisfied",
            "source": "current_task_user_confirmed",
            "source_type": "current_task_user_confirmed",
            "source_id": message_id,
            "source_ref": message_id,
            "resolved_revision": task_spec_revision(updated),
            "resolved_by": message_id,
        })
        if _field_key(requirement.get("strength")) == "ambiguous":
            requirement["strength"] = "required"
            requirement["level"] = "required"
        prior_provenance = [
            deepcopy(item)
            for item in requirement.get("provenance", []) or []
            if isinstance(item, Mapping)
        ]
        answer_provenance = {
            "source_type": "current_task_user_confirmed",
            "source_id": message_id,
            "source_ref": message_id,
        }
        requirement["provenance"] = [
            *prior_provenance,
            *([] if any(_provenance_source_id(item) == message_id for item in prior_provenance) else [answer_provenance]),
        ]
        resolved = True
        break
    if not resolved:
        raise ValueError("Registered ambiguity disappeared during the user-answer transition")
    updated["unresolved_questions"] = [
        deepcopy(item)
        for item in updated.get("unresolved_questions", []) or []
        if not (
            isinstance(item, Mapping)
            and _text(item.get("ambiguity_id") or item.get("constraint_id")) == identifier
        )
    ]
    return updated


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
    primary_source_ref = _text(normalized.get("raw_submission_ref")) or next(
        (_text(value) for value in normalized.get("source_refs", []) or [] if _text(value)),
        "",
    )
    for requirement in normalized.get("requirements", []) or []:
        if not isinstance(requirement, Mapping):
            continue
        semantic_key = _semantic_key(requirement.get("semantic_key"))
        source_ref = _text(requirement.get("source_ref") or requirement.get("source_id") or primary_source_ref)
        if semantic_key.startswith("required_behavior.entity."):
            entity_type = semantic_key.split(".", 2)[2]
            typed["entity"].append({
                "fact_kind": "entity",
                "fact_id": _stable_fact_id(task_id, "canonical_entity", entity_type, requirement.get("value")),
                "entity_type": entity_type,
                "value": deepcopy(requirement.get("value")),
                "cardinality": "one",
                "source": _text(requirement.get("source") or "user_explicit"),
                "source_type": _text(requirement.get("source_type") or requirement.get("source") or "user_explicit"),
                "source_task_id": task_id,
                "source_message_id": source_ref,
                "revision_id": task_spec_revision(normalized),
                "status": "active",
                "confidence": float(requirement.get("confidence") or 1.0),
            })
        elif semantic_key.startswith("required_behavior.relation.") and isinstance(requirement.get("value"), Mapping):
            predicate = semantic_key.split(".", 2)[2]
            relation_value = dict(requirement.get("value") or {})
            subject = _text(relation_value.get("subject"))
            object_value = _text(relation_value.get("object"))
            if subject and object_value:
                typed["relation"].append({
                    "fact_kind": "relation",
                    "fact_id": _stable_fact_id(task_id, "canonical_relation", subject, predicate, object_value),
                    "subject": subject,
                    "predicate": predicate,
                    "object": object_value,
                    "cardinality": "many",
                    "source": _text(requirement.get("source") or "user_explicit"),
                    "source_type": _text(requirement.get("source_type") or requirement.get("source") or "user_explicit"),
                    "source_task_id": task_id,
                    "source_message_id": source_ref,
                    "revision_id": task_spec_revision(normalized),
                    "status": "active",
                    "confidence": float(requirement.get("confidence") or 1.0),
                })
    for raw_key, parameter_value in dict(normalized.get("parameters") or {}).items():
        key_text = _text(raw_key)
        namespace, _, key = key_text.rpartition(".")
        if not key:
            key = namespace
            namespace = ""
        typed["parameter"].append({
            "fact_kind": "parameter",
            "fact_id": _stable_fact_id(task_id, "canonical_parameter", key_text, parameter_value),
            "namespace": namespace,
            "key": key,
            "value": deepcopy(parameter_value),
            "value_type": type(parameter_value).__name__,
            "scope": "task",
            "cardinality": "one",
            "source": "user_explicit",
            "source_type": "user_explicit",
            "source_task_id": task_id,
            "source_message_id": primary_source_ref,
            "revision_id": task_spec_revision(normalized),
            "status": "active",
            "supersedes": [],
            "merge_reason": "canonical_parameter_projection",
            "confidence": 1.0,
        })
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
        locator = artifact_locator(item)
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
    for item in normalized.get("output_contracts", []) or []:
        if not isinstance(item, Mapping):
            continue
        role = _field_key(item.get("role")) or "output"
        locator = artifact_locator(item)
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
        "semantic_constraints": semantic_constraints_from_task_spec(normalized),
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
        "semantic_constraints": deepcopy(list(resolved.get("semantic_constraints") or [])),
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
    include_task_goal: bool = True,
) -> str:
    """Render the shared projection with explicit fact/source boundaries."""
    projection = project_effective_user_facts(
        resolve_effective_user_facts(spec),
        audience=audience,
        step=step,
        max_chars=max_chars,
    )
    refs = [
        {
            key: ref.get(key)
            for key in (
                "fact_id",
                "source_type",
                "source_message_id",
                "source_task_id",
                "source_ref", "source_id",
            )
            if ref.get(key) not in (None, "", [])
        }
        if isinstance(ref, Mapping) else str(ref)[:160]
        for ref in list(projection.get("source_refs") or [])
    ]
    fact_payload = {
        key: deepcopy(value)
        for key, value in projection.items()
        if key not in {"projection_text", "projection_chars", "projection_budget", "source_refs"}
    }
    if not include_task_goal:
        fact_payload.pop("task_goal", None)

    # The durable projection intentionally carries full merge bookkeeping.
    # The LLM-facing form keeps semantic meaning and provenance but removes
    # repetitive timestamps/supersession metadata.  Artifacts are deduplicated
    # by role plus filename, preferring a resolved absolute locator.
    semantic_constraints = [
        item for item in fact_payload.get("semantic_constraints", [])
        if isinstance(item, Mapping)
    ]
    desired_by_role: Dict[tuple[str, str], set[str]] = {}
    for item in semantic_constraints:
        semantic_key = str(item.get("semantic_key") or "")
        if "." not in semantic_key:
            continue
        category, slot = semantic_key.split(".", 1)
        direction = "input" if category == "required_input" else "output" if category == "required_output" else ""
        if not direction:
            continue
        role = str(item.get("role") or slot).strip()
        values: List[Any] = [item.get("value"), item.get("path"), item.get("resolved_path")]
        aliases = item.get("aliases")
        if isinstance(aliases, (list, tuple, set)):
            values.extend(aliases)
        elif aliases:
            values.append(aliases)
        desired_by_role.setdefault((direction, role), set()).update(
            str(value).strip().replace("\\", "/")
            for value in values
            if str(value or "").strip()
        )

    artifact_fields = (
        "fact_id", "direction", "role", "locator", "format", "artifact_type",
        "cardinality", "required", "status", "verified", "source_type",
        "source_message_id", "source_task_id",
    )
    selected_artifacts: Dict[tuple[str, str, str], tuple[int, int, Mapping[str, Any]]] = {}
    has_typed_inputs = any(direction == "input" for direction, _ in desired_by_role)
    for index, item in enumerate(fact_payload.get("active_artifacts", []) or []):
        if not isinstance(item, Mapping):
            continue
        direction = str(item.get("direction") or "").strip()
        role = str(item.get("role") or "").strip()
        locator = str(item.get("locator") or item.get("path") or "").strip()
        normalized = locator.replace("\\", "/").rstrip("/")
        basename = Path(normalized).name.lower() if normalized else ""
        desired = desired_by_role.get((direction, role), set())
        desired_basenames = {Path(value.rstrip("/")).name.lower() for value in desired if value}
        if (
            has_typed_inputs
            and direction == "input"
            and role in {"input", "input_file", "input_dir", "unknown"}
            and normalized not in desired
            and basename not in desired_basenames
        ):
            continue
        identity = basename or normalized.lower() or str(item.get("fact_id") or index)
        key = (direction, role, identity)
        score = 0
        if normalized in desired:
            score += 100
        elif basename and basename in desired_basenames:
            score += 50
        if Path(locator).is_absolute():
            score += 20
        if str(item.get("resolved_path") or "").strip():
            score += 10
        if str(item.get("source_message_id") or "").strip():
            score += 5
        previous = selected_artifacts.get(key)
        if previous is None or score > previous[0]:
            selected_artifacts[key] = (score, index, item)
    fact_payload["active_artifacts"] = [
        {
            key: item.get(key)
            for key in artifact_fields
            if item.get(key) not in (None, "", [])
        }
        for _, _, item in sorted(selected_artifacts.values(), key=lambda value: value[1])
    ]

    semantic_fields = (
        "constraint_id", "requirement_id", "semantic_key", "operator", "value",
        "strength", "level", "confidence", "condition", "ambiguity",
        "needs_clarification", "source_text", "source_type", "source_ref", "source_id",
        "source_span", "provenance", "role", "path", "resolved_path",
        "resolution_status", "status", "required", "verified", "aliases",
    )
    fact_payload["semantic_constraints"] = [
        {
            key: item.get(key)
            for key in semantic_fields
            if item.get(key) not in (None, "", [])
        }
        for item in semantic_constraints
    ]

    fact_fields = (
        "fact_id", "fact_kind", "key", "name", "value", "value_type", "unit",
        "namespace", "subject", "predicate", "object", "relation", "entity_type",
        "required", "status", "source_type", "source_message_id", "source_task_id",
    )
    for field in (
        "active_decisions",
        "active_parameters",
        "active_entities",
        "active_relations",
        "unresolved_facts",
        "inherited_facts",
    ):
        compacted = []
        for item in fact_payload.get(field, []) or []:
            if not isinstance(item, Mapping):
                compacted.append(item)
                continue
            compacted.append({
                key: item.get(key)
                for key in fact_fields
                if item.get(key) not in (None, "", [])
            })
        fact_payload[field] = compacted

    visible_fact_ids = {
        str(item.get("fact_id"))
        for field in (
            "active_artifacts",
            "active_decisions",
            "active_parameters",
            "active_entities",
            "active_relations",
            "unresolved_facts",
            "inherited_facts",
        )
        for item in fact_payload.get(field, []) or []
        if isinstance(item, Mapping) and str(item.get("fact_id") or "").strip()
    }
    refs = [
        ref for ref in refs
        if not isinstance(ref, Mapping)
        or not str(ref.get("fact_id") or "").strip()
        or str(ref.get("fact_id")) in visible_fact_ids
    ]

    def render(payload: Mapping[str, Any], source_refs: List[Any]) -> str:
        return (
            "[EFFECTIVE USER FACTS]\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            + "\n[USER SOURCE REFERENCES]\n"
            + json.dumps(source_refs, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        )

    rendered = render(fact_payload, refs)
    if len(rendered) <= max_chars:
        return rendered

    compact_refs = list(refs)
    fact_payload["projection_truncated"] = True
    fact_payload.setdefault("omitted_sections", []).append("source_reference_excerpts")
    rendered = render(fact_payload, compact_refs)
    while len(rendered) > max_chars and compact_refs:
        compact_refs.pop()
        rendered = render(fact_payload, compact_refs)
    if len(rendered) <= max_chars:
        return rendered

    for field in (
        "active_decisions",
        "active_parameters",
        "active_entities",
        "active_relations",
        "unresolved_facts",
        "inherited_facts",
    ):
        if field in fact_payload and fact_payload[field]:
            fact_payload[field] = []
            fact_payload.setdefault("omitted_sections", []).append(field)
            rendered = render(fact_payload, compact_refs)
            if len(rendered) <= max_chars:
                return rendered

    # Required artifacts, legacy constraints, typed semantic constraints, and
    # their source identifiers are lossless.  If they cannot fit, return an
    # explicit budget error instead of a plausible-looking partial contract.
    fallback = {
        "projection_truncated": True,
        "effective_facts_revision": fact_payload.get("effective_facts_revision", ""),
        "required_contract_counts": {
            field: len(fact_payload.get(field) or [])
            for field in ("active_artifacts", "active_constraints", "semantic_constraints")
        },
        "error": (
            f"{_field_key(audience).upper() or 'NODE'}_CONTEXT_BUDGET_UNSATISFIED"
        ),
    }
    return render(fallback, [])[:max_chars]


def format_planner_task_contract_context(spec: Mapping[str, Any] | None) -> str:
    """Render one lossless highlight list plus structured Planner facts."""
    normalized = ensure_task_spec(spec)
    outputs = effective_output_contracts(normalized)
    highlighted_constraints: List[Dict[str, Any]] = []
    for raw in normalized.get("requirements", []) or []:
        if not isinstance(raw, Mapping):
            continue
        value = raw.get("value")
        if isinstance(value, str):
            text = value
        else:
            text = _text(raw.get("source_text") or raw.get("description"))
            if not text:
                key = _text(raw.get("semantic_key") or raw.get("requirement_id"))
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
        importance = _field_key(raw.get("strength"))
        if importance not in SEMANTIC_CONSTRAINT_STRENGTHS:
            importance = {
                "required": "required",
                "conditional": "conditional",
                "recommended": "preference",
                "optional_validation": "advisory",
                "known_absent": "advisory",
                "unknown": "ambiguous",
            }.get(_field_key(raw.get("level")), "ambiguous")
        source_refs: List[str] = []
        for ref in (raw.get("source_ref"), raw.get("source_id")):
            if _text(ref):
                ref_text = _text(ref)
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
            ref = _text(provenance.get("source_ref") or provenance.get("source_id"))
            if ref:
                display_ref = (
                    "source-ref-" + hashlib.sha256(
                        ref.encode("utf-8")
                    ).hexdigest()[:16]
                    if re.match(
                        r"^(?:sha256:[0-9a-f]{32,}|"
                        r"(?:object|audit|evidence):sha256:[0-9a-f]{16,}|"
                        r"cas:[A-Za-z0-9._:-]+)$",
                        ref,
                        flags=re.IGNORECASE,
                    )
                    else ref
                )
                if display_ref not in source_refs:
                    source_refs.append(display_ref)
        highlighted_constraints.append({
            "text": text,
            "importance": importance,
            "source_refs": source_refs,
        })

    def compact(values: Any, fields: tuple[str, ...]) -> List[Dict[str, Any]]:
        return [
            {
                key: deepcopy(item.get(key))
                for key in fields
                if item.get(key) not in (None, "", [], {})
            }
            for item in values or []
            if isinstance(item, Mapping)
        ]

    output_fields = (
        "artifact_id", "output_id", "semantic_handle", "direction", "role",
        "artifact_kind", "format", "path_policy", "root_ref", "filename",
        "canonical_path", "required_basename", "required", "validation_contract_id",
        "source_type", "source_ref", "source_id", "producer_owner", "producer_step_id",
        "user_visible", "resolution_base", "provenance",
        "resource_kind", "contract_revision", "contract_source",
    )
    compact_outputs = compact(outputs, output_fields)
    categorized: Dict[str, List[Dict[str, Any]]] = {
        "user_requested": [],
        "system_required": [],
        "skill_or_workflow_required": [],
        "optional": [],
    }
    for output in compact_outputs:
        if not bool(output.get("required", True)):
            category = "optional"
        else:
            source = _field_key(output.get("source_type"))
            if source.startswith("system"):
                category = "system_required"
            elif source.startswith("skill") or source.startswith("workflow"):
                category = "skill_or_workflow_required"
            else:
                category = "user_requested"
        categorized[category].append(output)

    contract = {
        "schema_version": TASK_SPEC_SCHEMA_VERSION,
        "task_id": _text(normalized.get("task_id")),
        "revision": task_spec_revision(normalized),
        "goal": _text(normalized.get("goal")),
        "inputs": compact(
            normalized.get("inputs"),
            (
                "artifact_id", "semantic_handle", "direction", "role", "format", "path_policy",
                "canonical_path", "required_basename", "validation_contract_id", "cardinality",
                "required", "verified", "status", "source_type", "source_ref", "source_id",
                "resolution_base", "provenance", "input_instance",
            ),
        ),
        "highlighted_constraints": highlighted_constraints,
        "parameters": deepcopy(dict(normalized.get("parameters") or {})),
        "confirmed_decisions": deepcopy(
            list(normalized.get("confirmed_decisions") or [])
        ),
        "unresolved_questions": list(normalized.get("unresolved_questions") or []),
        "output_contracts": compact_outputs,
        "capability_intent": deepcopy(dict(normalized.get("capability_intent") or {})),
    }
    output_view = {
        "authority": "TaskSpec.output_contracts",
        "required_count": sum(
            1
            for output in compact_outputs
            if bool(output.get("required", True)) and output.get("role") != "output_dir"
        ),
        **categorized,
    }
    return (
        "[CANONICAL TASK CONTRACT]\n"
        + json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        + "\n[CANONICAL REQUIRED OUTPUTS]\n"
        + json.dumps(output_view, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    )


def effective_task_contract(spec: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Project active facts into the legacy contract consumed by QA gates."""
    normalized = ensure_task_spec(spec)
    resolved = resolve_effective_user_facts(normalized)
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = effective_output_contracts(normalized)
    active_output_locators = {
        _text(item.get("locator"))
        for item in resolved["active_artifacts"]
        if _text(item.get("direction")).lower() == "output" and _text(item.get("locator"))
    }
    inactive_output_locators = {
        _text(item.get("locator"))
        for item in resolved["inactive_facts"]
        if (
            _field_key(item.get("fact_kind")) == "artifact"
            and _text(item.get("direction")).lower() == "output"
            and _text(item.get("locator"))
        )
    }
    outputs = [
        item
        for item in outputs
        if (
            _text(item.get("path_or_name") or item.get("path") or item.get("filename"))
            not in inactive_output_locators
            or _text(item.get("path_or_name") or item.get("path") or item.get("filename"))
            in active_output_locators
        )
    ]
    has_canonical_outputs = bool(outputs)
    for artifact in resolved["active_artifacts"]:
        locator = _text(artifact.get("locator"))
        if not locator:
            continue
        if _text(artifact.get("direction")).lower() == "output":
            if not has_canonical_outputs:
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

    active_input_paths = {artifact_locator(item) for item in inputs}
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
    return {
        "effective_facts_revision": resolved["effective_facts_revision"],
        "inputs": sorted(inputs, key=lambda item: (_text(item.get("role")), artifact_locator(item))),
        "outputs": sorted(outputs, key=lambda item: (_text(item.get("role")), artifact_locator(item))),
        "requirements": requirements,
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
    payload = {
        "revision": normalized["revision"],
        "goal": goal,
        "inputs": normalized.get("inputs", []),
        "requirements": normalized.get("requirements", []),
        "parameters": normalized.get("parameters", {}),
        "output_contracts": normalized.get("output_contracts", []),
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
