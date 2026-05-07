from __future__ import annotations

import re
from typing import Any, Dict, List


SECRET_PATTERNS = [
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bragflow-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|pwd|cookie|access[_-]?key)"
        r"\s*[:=]\s*)([\"']?)([^\s,\"';`]+)([\"']?)"
    ),
]


def redact_secrets(text: Any) -> str:
    """Mask common secret shapes before text is written to durable memory."""
    value = str(text)
    value = SECRET_PATTERNS[0].sub("[REDACTED PRIVATE KEY]", value)
    value = SECRET_PATTERNS[1].sub(r"\1[REDACTED]", value)
    value = SECRET_PATTERNS[2].sub("sk-[REDACTED]", value)
    value = SECRET_PATTERNS[3].sub("ragflow-[REDACTED]", value)

    def _mask_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(4)}"

    return SECRET_PATTERNS[4].sub(_mask_assignment, value)


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)
    if isinstance(value, dict):
        return {str(redact_secrets(key)): sanitize_value(item) for key, item in value.items()}
    return value


def flatten_dict(value: Any, prefix: str = "") -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}
    if not isinstance(value, dict):
        if prefix:
            flattened[prefix] = value
        return flattened
    for raw_key, raw_item in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(raw_item, dict):
            flattened.update(flatten_dict(raw_item, path))
        else:
            flattened[path] = raw_item
    return flattened


class DreamPatchValidator:
    """Validate and sanitize LLM-generated memory patches before applying them."""

    def __init__(self, core_store: Any):
        self.core_store = core_store

    def sanitize_patch(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = sanitize_value(patch)
        return sanitized if isinstance(sanitized, dict) else {}

    def validate_patch(self, patch: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        if not isinstance(patch, dict):
            return ["Dream patch is not a JSON object."]
        if not any(key in patch for key in ("user_profile", "agent_memory", "projects", "tasks", "task", "long_term")):
            warnings.append("Dream patch has no recognized memory targets.")

        operations = patch.get("operations")
        if operations is not None and not isinstance(operations, list):
            warnings.append("Dream patch operations must be a list when provided.")

        confidence = str(patch.get("confidence", "")).strip().lower()
        if confidence and confidence not in {"low", "medium", "high"}:
            warnings.append(f"Unknown confidence value: {confidence}")
        return warnings

    def detect_conflicts(self, patch: Dict[str, Any]) -> List[str]:
        conflicts: List[str] = []
        projects = patch.get("projects")
        if not isinstance(projects, dict):
            return conflicts

        for raw_project_id, project_patch in projects.items():
            project_id = str(raw_project_id).strip() or "default"
            existing = self.core_store.read_project_facts(project_id)
            for key, value in flatten_dict(project_patch).items():
                new_value = str(value).strip()
                old_value = str(existing.get(key, "")).strip()
                if old_value and new_value and old_value != new_value:
                    conflicts.append(
                        f"{project_id}.{key}: existing={old_value!r}, patch={new_value!r}"
                    )
        return conflicts

    def redact_secrets(self, text: Any) -> str:
        return redact_secrets(text)
