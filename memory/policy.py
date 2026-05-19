from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .patch_validator import redact_secrets


CORE_PROJECT_INDEX_KEYS = {
    "active_project",
    "project_root",
    "root",
    "wiki_digest_path",
    "primary_pages",
    "active_dataset_pages",
    "decision_pages",
    "last_compiled_at",
}

CORE_PROJECT_INDEX_PREFIXES = ("wiki_",)
CORE_PROJECT_INDEX_SUFFIXES = ("_pages", "_page_refs", "_digest_path")

DETAIL_KEY_TERMS = {
    "dataset",
    "datasets",
    "sample",
    "samples",
    "read",
    "reads",
    "fastq",
    "fq",
    "hifi",
    "hic",
    "illumina",
    "bam",
    "sam",
    "vcf",
    "gff",
    "gff3",
    "gtf",
    "fasta",
    "fa",
    "parameter",
    "parameters",
    "params",
    "threshold",
    "result",
    "results",
    "conclusion",
    "decision",
    "method",
    "workflow",
    "script",
}

PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/]|/[A-Za-z0-9_.\-/]+|\\\\|\.f(?:ast)?q(?:\.gz)?\b|"
    r"\.(?:bam|sam|cram|vcf|bcf|gff3?|gtf|fa|fasta|fna|bed|bw|bigwig|hic|cool|mcool|tsv|csv|xlsx)\b)"
)


@dataclass(frozen=True)
class MemoryPolicyDecision:
    allowed: bool
    destination: str
    reason: str


def json_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def read_simple_project_facts(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    facts: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s{2}([A-Za-z0-9_.-]+):\s*(.*?)\s*$", line)
        if not match:
            continue
        key, raw_value = match.groups()
        raw_value = raw_value.strip()
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        facts[key] = str(value)
    return facts


def render_project_facts(project_id: str, facts: Dict[str, Any], updated_at: str) -> str:
    lines = [
        f"project_id: {project_id}",
        f"updated_at: {updated_at}",
        "facts:",
    ]
    if facts:
        for key in sorted(facts):
            lines.append(f"  {key}: {json_scalar(facts[key])}")
    else:
        lines[-1] = "facts: {}"
    return "\n".join(lines).rstrip() + "\n"


def classify_project_fact(key: str, value: Any) -> MemoryPolicyDecision:
    clean_key = sanitize_project_fact_key(key)
    key_l = clean_key.lower()
    value_text = redact_secrets(value).strip()

    if is_core_project_index_key(key_l):
        return MemoryPolicyDecision(True, "core_project_index", "Stable project index field.")

    key_terms = set(re.split(r"[_.-]+", key_l))
    if key_terms & DETAIL_KEY_TERMS:
        return MemoryPolicyDecision(
            False,
            "wiki_claim",
            "Detailed project fact should live in Wiki with evidence.",
        )
    if is_path_like(value_text):
        return MemoryPolicyDecision(
            False,
            "wiki_claim",
            "Path-like project fact should live in Wiki unless it is project_root.",
        )
    if len(value_text) > 180:
        return MemoryPolicyDecision(
            False,
            "wiki_claim",
            "Long project fact should live in Wiki or task memory.",
        )
    return MemoryPolicyDecision(
        False,
        "wiki_claim",
        "Project facts default to Wiki; Core stores only stable index fields.",
    )


def sanitize_project_fact_key(key: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key).strip())


def is_core_project_index_key(key: str) -> bool:
    key = key.lower()
    if key in CORE_PROJECT_INDEX_KEYS:
        return True
    return key.startswith(CORE_PROJECT_INDEX_PREFIXES) or key.endswith(CORE_PROJECT_INDEX_SUFFIXES)


def is_path_like(value: Any) -> bool:
    return bool(PATH_PATTERN.search(redact_secrets(value)))


def filter_core_project_facts(facts: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, str]]:
    allowed: Dict[str, Any] = {}
    rejected: Dict[str, str] = {}
    for key, value in facts.items():
        clean_key = sanitize_project_fact_key(key)
        if not clean_key:
            continue
        decision = classify_project_fact(clean_key, value)
        if decision.allowed:
            allowed[clean_key] = redact_secrets(value)
        else:
            rejected[clean_key] = decision.reason
    return allowed, rejected


def build_wiki_index_fields(
    project_id: str,
    digest_path: Path,
    pages: Sequence[Dict[str, Any]],
    compiled_at: str,
) -> Dict[str, str]:
    project_pages = _page_paths_by_type(pages, {"project", "synthesis"})[:8]
    if not project_pages:
        project_pages = [str(page.get("path") or "") for page in pages[:8] if str(page.get("path") or "")]
    return {
        "wiki_digest_path": str(digest_path),
        "primary_pages": ", ".join(project_pages),
        "active_dataset_pages": ", ".join(_page_paths_by_type(pages, {"dataset"})[:12]),
        "decision_pages": ", ".join(_page_paths_by_type(pages, {"decision"})[:12]),
        "last_compiled_at": compiled_at,
    }


def _page_paths_by_type(pages: Sequence[Dict[str, Any]], page_types: set[str]) -> List[str]:
    paths = [
        str(page.get("path") or "")
        for page in pages
        if str(page.get("type") or "").lower() in page_types and str(page.get("path") or "")
    ]
    return sorted(dict.fromkeys(paths))


def lint_core_wiki_boundary(
    project_id: str,
    core_facts: Dict[str, str],
    wiki_claims: Sequence[Dict[str, Any]],
) -> List[str]:
    issues: List[str] = []
    active_claims = [
        claim for claim in wiki_claims
        if str(claim.get("status", "active")).lower() not in {"deprecated", "rejected", "superseded"}
        and str(claim.get("confidence", "")).lower() in {"high", "medium"}
    ]

    for key, value in sorted(core_facts.items()):
        decision = classify_project_fact(key, value)
        if not decision.allowed:
            issues.append(
                f"Core fact should live in Wiki for project {project_id}: {key}={value!r} ({decision.reason})"
            )
        if is_path_like(value):
            conflicts = _path_conflicts(key, value, active_claims)
            issues.extend(conflicts)
    return issues


def _path_conflicts(key: str, value: str, claims: Sequence[Dict[str, Any]]) -> List[str]:
    key_tokens = {token for token in re.split(r"[_.-]+", key.lower()) if len(token) >= 3}
    core_paths = set(_extract_path_tokens(value))
    if not key_tokens or not core_paths:
        return []
    conflicts: List[str] = []
    for claim in claims:
        text = redact_secrets(claim.get("text") or "")
        text_l = text.lower()
        if not any(token in text_l for token in key_tokens):
            continue
        claim_paths = set(_extract_path_tokens(text))
        if claim_paths and not (claim_paths & core_paths):
            conflicts.append(
                "Core/Wiki path conflict: "
                f"core {key}={value!r} vs claim {claim.get('id', 'unknown')} "
                f"path(s)={', '.join(sorted(claim_paths))}"
            )
    return conflicts


def _extract_path_tokens(text: Any) -> List[str]:
    value = redact_secrets(text)
    return [match.group(0).rstrip(".,;") for match in PATH_PATTERN.finditer(value)]


def lint_wiki_claims(claims: Sequence[Dict[str, Any]]) -> List[str]:
    issues: List[str] = []
    for claim in claims:
        claim_id = str(claim.get("id") or "unknown")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append(f"Wiki claim missing evidence: {claim_id}")
        confidence = str(claim.get("confidence") or "").lower()
        if confidence == "low":
            issues.append(f"Wiki claim is low confidence and should be reviewed: {claim_id}")
        if claim.get("review_required") or claim.get("stale"):
            issues.append(f"Wiki claim requires review: {claim_id}")
    return issues

