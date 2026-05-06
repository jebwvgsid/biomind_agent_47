from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional


DEFAULT_PROJECT_ID = os.getenv("BIOMIND_PROJECT_ID", "default")
NO_UPDATE_VALUES = {"none", "n/a", "no update"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clip_text(text: Any, limit: int, transform: Optional[Callable[[Any], str]] = None) -> str:
    value = transform(text) if transform else str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    value = str(text).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, re.DOTALL)
    if fenced:
        value = fenced.group(1)
    else:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            value = value[start : end + 1]
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def safe_id(value: Any, default: str = "default", strip_edges: bool = False) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    if strip_edges:
        safe = safe.strip("._-")
    return safe or default


def normalize_list(
    value: Any,
    *,
    transform: Optional[Callable[[Any], str]] = None,
    strip_bullets: bool = False,
    unique: bool = False,
) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw: Iterable[Any] = value
    elif isinstance(value, tuple):
        raw = value
    else:
        raw = [value]

    result: List[str] = []
    seen: set[str] = set()
    for item in raw:
        text = transform(item) if transform else str(item)
        text = text.strip()
        if strip_bullets:
            text = re.sub(r"^\s*[-*]\s+", "", text)
        if not text or text.lower() in NO_UPDATE_VALUES:
            continue
        if unique:
            if text in seen:
                continue
            seen.add(text)
        result.append(text)
    return result


def query_terms(query: str, pattern: str = r"[\w./:\\-]{2,}") -> List[str]:
    query = query.strip().lower()
    candidates = [query] if query else []
    candidates.extend(re.findall(pattern, query, flags=re.UNICODE))
    seen: set[str] = set()
    ordered: List[str] = []
    for term in candidates:
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered
