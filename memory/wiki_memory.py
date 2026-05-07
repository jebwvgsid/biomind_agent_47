from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .patch_validator import redact_secrets, sanitize_value
from .utils import (
    DEFAULT_PROJECT_ID,
    clip_text,
    extract_json_object,
    normalize_list,
    now_iso,
    query_terms,
    safe_id,
)

try:
    from langchain_core.messages import HumanMessage
except ImportError:
    class HumanMessage:  # type: ignore[no-redef]
        def __init__(self, content: str):
            self.content = content


logger = logging.getLogger("BioMind.WikiMemory")


ALLOWED_PAGE_TYPES = {
    "project",
    "method",
    "dataset",
    "tool",
    "decision",
    "issue",
    "artifact",
    "query",
    "source",
    "synthesis",
}

TYPE_DIRS = {
    "project": "projects",
    "method": "methods",
    "dataset": "datasets",
    "tool": "tools",
    "decision": "decisions",
    "issue": "issues",
    "artifact": "artifacts",
    "query": "queries",
    "source": "sources",
    "synthesis": "synthesis",
}

ALLOWED_OPS = {
    "create_page",
    "append_section",
    "merge_section_items",
    "update_frontmatter",
    "update_index",
    "append_log",
    "flag_review",
}

LIST_FRONTMATTER_KEYS = {"sources", "aliases", "tags", "supersedes"}


def _now() -> str:
    return now_iso()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _safe_id(value: Any, default: str = "default") -> str:
    return safe_id(value, default=default, strip_edges=True)


def _slug(value: Any, default: str = "page") -> str:
    text = redact_secrets(value).strip().lower()
    text = re.sub(r"[\s/\\:]+", "-", text)
    text = re.sub(r"[^a-z0-9_.\-\u4e00-\u9fff]+", "", text)
    text = text.strip(".-_")
    return text[:96] or default


def _content_of(message: Any) -> str:
    return redact_secrets(getattr(message, "content", message))


def _role_of(message: Any) -> str:
    name = type(message).__name__.replace("Message", "")
    return name or "Message"


def _clip(text: Any, limit: int) -> str:
    return clip_text(text, limit, transform=redact_secrets)


def _extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    return extract_json_object(text)


def _ensure_list(value: Any) -> List[str]:
    return normalize_list(value, transform=redact_secrets, unique=True)


def _terms(query: str) -> List[str]:
    return query_terms(query, pattern=r"[\w./:\\\-\u4e00-\u9fff]{2,}")


@dataclass
class WikiSearchHit:
    path: str
    title: str
    page_type: str
    score: float
    text: str
    sources: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "type": self.page_type,
            "score": self.score,
            "text": self.text,
            "sources": list(self.sources),
        }


class WikiMemoryManager:
    """Project-scoped LLM Wiki memory with immutable raw evidence."""

    def __init__(
        self,
        llm: Any,
        memory_dir: str | Path,
        max_inline_chars: int = 4_000,
        max_search_candidates: int = 8,
        default_project_id: str = DEFAULT_PROJECT_ID,
    ):
        self.llm = llm
        self.root = Path(memory_dir)
        self.raw_dir = self.root / "raw"
        self.conversations_dir = self.raw_dir / "conversations"
        self.large_outputs_dir = self.raw_dir / "large_outputs"
        self.pending_file = self.raw_dir / "pending_wiki.jsonl"
        self.processed_file = self.raw_dir / "processed_wiki_events.json"
        self.wiki_dir = self.root / "wiki"
        self.review_dir = self.wiki_dir / "review"
        self.review_pending = self.review_dir / "PENDING.md"
        self.index_file = self.wiki_dir / "index.md"
        self.log_file = self.wiki_dir / "log.md"
        self.overview_file = self.wiki_dir / "overview.md"
        self.purpose_file = self.root / "purpose.md"
        self.schema_file = self.root / "schema.md"
        self.db_path = self.wiki_dir / "wiki_index.sqlite3"
        self.max_inline_chars = max_inline_chars
        self.max_search_candidates = max_search_candidates
        self.default_project_id = _safe_id(default_project_id)
        self.fts_enabled = False
        self.fts_tokenizer = "none"
        self.ensure_layout()
        self._init_db()

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        self.large_outputs_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        for folder in {
            *TYPE_DIRS.values(),
            "review",
        }:
            (self.wiki_dir / folder).mkdir(parents=True, exist_ok=True)

        if not self.purpose_file.exists():
            self.purpose_file.write_text(self._initial_purpose(), encoding="utf-8")
        if not self.schema_file.exists():
            self.schema_file.write_text(self._initial_schema(), encoding="utf-8")
        if not self.pending_file.exists():
            self.pending_file.write_text("", encoding="utf-8")
        if not self.processed_file.exists():
            self.processed_file.write_text("[]\n", encoding="utf-8")
        if not self.index_file.exists():
            self.index_file.write_text("# Wiki Index\n\nNo wiki pages indexed yet.\n", encoding="utf-8")
        if not self.log_file.exists():
            self.log_file.write_text("# Wiki Log\n", encoding="utf-8")
        if not self.overview_file.exists():
            self.overview_file.write_text("# Wiki Overview\n\nNo wiki pages created yet.\n", encoding="utf-8")
        if not self.review_pending.exists():
            self.review_pending.write_text("# Pending Wiki Review\n", encoding="utf-8")

    @staticmethod
    def _initial_purpose() -> str:
        return (
            "# BioMind Wiki Purpose\n\n"
            "This wiki preserves project knowledge that should be reusable across sessions.\n\n"
            "## Goals\n"
            "- Track project facts, methods, datasets, tools, decisions, issues, artifacts, and useful answers.\n"
            "- Keep raw evidence immutable and generated pages source-traceable.\n"
            "- Prefer append and merge updates over destructive rewrites.\n\n"
            "## Current Questions\n"
            "- None recorded yet.\n"
        )

    @staticmethod
    def _initial_schema() -> str:
        page_types = ", ".join(sorted(ALLOWED_PAGE_TYPES))
        ops = ", ".join(sorted(ALLOWED_OPS))
        return (
            "# BioMind Wiki Schema\n\n"
            f"Allowed page types: {page_types}.\n"
            f"Allowed patch operations: {ops}.\n\n"
            "## Patch Rules\n"
            "- Every page must include YAML-style frontmatter with title, type, project_id, status, created_at, "
            "updated_at, sources, aliases, tags, and confidence.\n"
            "- Existing pages are updated by appending sections, merging bullets, or updating frontmatter; do not overwrite pages.\n"
            "- Low-confidence, contradictory, or stale facts must be flagged for review instead of applied.\n"
            "- Page paths must stay inside wiki/ and must not contain path traversal.\n\n"
            "## Source Rules\n"
            "- Cite raw event IDs, session archive IDs, task IDs, artifact paths, or raw file hashes in sources[].\n"
        )

    @staticmethod
    def migrate_legacy_runtime_memory(legacy_dir: str | Path, target_dir: str | Path) -> List[str]:
        """Copy old ignored runtime memory files into the new data root without deleting or overwriting."""
        legacy = Path(legacy_dir)
        target = Path(target_dir)
        copied: List[str] = []
        if not legacy.exists():
            return copied
        target.mkdir(parents=True, exist_ok=True)

        for name in ("MEMORY.md", "TASKS.md", "USER_PROFILE.md", "AGENT_MEMORY.md"):
            source = legacy / name
            dest = target / name
            if source.exists() and source.is_file() and not dest.exists():
                shutil.copy2(source, dest)
                copied.append(str(dest))

        for name in ("projects", "session_prompts", "logs", "history", "dreams"):
            source_dir = legacy / name
            dest_dir = target / name
            if not source_dir.exists() or not source_dir.is_dir():
                continue
            for source in source_dir.rglob("*"):
                if not source.is_file():
                    continue
                rel = source.relative_to(source_dir)
                dest = dest_dir / rel
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                copied.append(str(dest))
        return copied

    def append_raw_event(
        self,
        task_id: str,
        session_id: Optional[str],
        stage: str,
        event_type: str,
        messages: Optional[List[Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        source_refs: Optional[Sequence[str]] = None,
    ) -> str:
        messages = messages or []
        payload = payload or {}
        session_id = session_id or task_id
        project_id = self._resolve_project_id(project_id)
        event_seed = {
            "task_id": task_id,
            "session_id": session_id,
            "project_id": project_id,
            "stage": stage,
            "event_type": event_type,
            "messages": [
                {"role": _role_of(message), "content": _content_of(message)}
                for message in messages[-8:]
            ],
            "payload": sanitize_value(payload),
            "source_refs": list(source_refs or []),
        }
        event_hash = hashlib.sha256(_json(event_seed).encode("utf-8")).hexdigest()
        event_id = f"{_safe_id(task_id)}-{event_hash[:16]}"
        raw_path = self.conversations_dir / f"{_safe_id(session_id)}.jsonl"

        if self._raw_event_exists(raw_path, event_id):
            return event_id

        record = {
            "event_id": event_id,
            "event_hash": event_hash,
            "timestamp": _now(),
            **event_seed,
        }
        record = self._externalize_large_values(record, event_id)

        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        self._append_pending_event({
            "event_id": event_id,
            "event_hash": event_hash,
            "timestamp": record["timestamp"],
            "task_id": _safe_id(task_id),
            "session_id": _safe_id(session_id),
            "project_id": _safe_id(project_id),
            "stage": redact_secrets(stage),
            "event_type": redact_secrets(event_type),
            "raw_file": str(raw_path),
            "source_refs": list(source_refs or []),
        })
        return event_id

    async def ingest_pending(self, project_id: Optional[str] = None, limit: int = 20) -> str:
        project_id = self._resolve_project_id(project_id)
        pending = [
            item for item in self._read_pending_events()
            if str(item.get("project_id") or project_id) == _safe_id(project_id)
        ]
        processed = self._read_processed_events()
        pending = [item for item in pending if str(item.get("event_id")) not in processed]
        if not pending:
            return "No pending wiki events to ingest."
        if self.llm is None:
            return "Wiki ingest unavailable: no LLM configured."

        batch = pending[: max(1, limit)]
        raw_records = self._load_raw_records(batch)
        event_text = "\n\n".join(
            f"### {record.get('event_id')}\n{json.dumps(record, ensure_ascii=False, indent=2, default=str)}"
            for record in raw_records
        )
        query = "\n".join(
            str(record.get("payload", ""))[:500] for record in raw_records
        )
        related_pages = self.format_hits(self.search(query, project_id=project_id, top_k=4))

        analysis_prompt = f"""You are BioMind's wiki analyst.
Analyze pending raw evidence before wiki patch generation.

Purpose:
{self.purpose_file.read_text(encoding="utf-8")}

Schema:
{self.schema_file.read_text(encoding="utf-8")}

Index:
{_clip(self.index_file.read_text(encoding="utf-8"), 5000)}

Relevant pages:
{related_pages}

Pending raw evidence:
{event_text}

Return a concise analysis of durable project knowledge, conflicts, and suggested wiki updates.
"""
        analysis_response = await self.llm.ainvoke([HumanMessage(content=analysis_prompt)])
        analysis = _content_of(analysis_response)

        patch_prompt = f"""Generate a strict JSON wiki patch for BioMind.
Return JSON only with this shape: {{"operations": [{{"op": "append_log", "message": "..."}}], "ingest_summary": "..."}}.

Allowed operations: {", ".join(sorted(ALLOWED_OPS))}.
Allowed page types: {", ".join(sorted(ALLOWED_PAGE_TYPES))}.
All paths must stay inside wiki/.
Prefer append_section or merge_section_items for existing pages.
Route conflicts, stale facts, and low confidence updates to flag_review.
Every applied page update must include sources with raw event IDs.

Analysis:
{analysis}

Raw event IDs:
{", ".join(str(item.get("event_id")) for item in batch)}
"""
        patch_response = await self.llm.ainvoke([HumanMessage(content=patch_prompt)])
        patch = _extract_json_object(_content_of(patch_response)) or _extract_json_object(analysis)
        if not patch:
            return "Wiki ingest failed: curator returned invalid JSON."

        applied = self.apply_patch(patch, project_id=project_id)
        processed.update(str(item.get("event_id")) for item in batch)
        self._write_processed_events(processed)
        self._rewrite_pending_events(processed)
        summary = str(patch.get("ingest_summary") or patch.get("summary") or "Wiki ingest completed.")
        return f"Wiki ingest completed.\nSummary: {summary}\nOperations applied: {applied}"

    def apply_patch(self, patch: Dict[str, Any], project_id: Optional[str] = None) -> int:
        project_id = self._resolve_project_id(project_id)
        sanitized = sanitize_value(patch)
        if not isinstance(sanitized, dict):
            self._flag_review("Invalid Wiki Patch", "Patch was not a JSON object.", {}, project_id)
            return 0
        operations = sanitized.get("operations", [])
        if isinstance(operations, dict):
            operations = [operations]
        if not isinstance(operations, list):
            self._flag_review("Invalid Wiki Patch", "Patch operations were not a list.", sanitized, project_id)
            return 0

        applied = 0
        for operation in operations:
            if not isinstance(operation, dict):
                self._flag_review("Invalid Wiki Operation", "Operation was not an object.", {"operation": operation}, project_id)
                continue
            op_name = str(operation.get("op") or operation.get("type") or "").strip()
            if op_name not in ALLOWED_OPS:
                self._flag_review("Unknown Wiki Operation", f"Unknown operation: {op_name}", operation, project_id)
                continue
            if self._operation_requires_review(operation):
                self._flag_review(
                    str(operation.get("title") or "Wiki Operation Requires Review"),
                    str(operation.get("reason") or "Operation was marked low-confidence or conflicting."),
                    operation,
                    project_id,
                )
                continue
            try:
                if op_name == "create_page":
                    self._op_create_page(operation, project_id)
                elif op_name == "append_section":
                    self._op_append_section(operation, project_id)
                elif op_name == "merge_section_items":
                    self._op_merge_section_items(operation, project_id)
                elif op_name == "update_frontmatter":
                    self._op_update_frontmatter(operation, project_id)
                elif op_name == "update_index":
                    self._rebuild_index_page()
                elif op_name == "append_log":
                    self._append_log(str(operation.get("message") or operation.get("content") or "Wiki patch applied."))
                elif op_name == "flag_review":
                    self._flag_review(
                        str(operation.get("title") or "Wiki Review Item"),
                        str(operation.get("body") or operation.get("reason") or ""),
                        operation,
                        project_id,
                    )
                applied += 1
            except Exception as exc:
                logger.warning("Wiki patch operation failed: %s", exc)
                self._flag_review("Wiki Patch Error", str(exc), operation, project_id)

        self._rebuild_index_page()
        self._rebuild_search_index()
        return applied

    def search(self, query: str, project_id: Optional[str] = None, top_k: int = 6) -> List[Dict[str, Any]]:
        project_id = self._resolve_project_id(project_id)
        self._rebuild_search_index()
        query = query.strip()
        if not query:
            hits = self._recent_pages(project_id, top_k)
        else:
            hits = self._fts_search(query, project_id, top_k * 2)
            if not hits:
                hits = self._lexical_search(query, project_id, top_k * 2)
        expanded = self._expand_hits(hits, project_id, top_k)
        return [hit.as_dict() for hit in expanded[:top_k]]

    def build_prompt_context(
        self,
        task_id: str,
        project_id: Optional[str] = None,
        query: str = "",
        top_k: int = 5,
    ) -> str:
        hits = self.search(query, project_id=project_id, top_k=top_k)
        if not hits:
            return ""
        return self.format_hits(hits, title="Retrieved Wiki Memory")

    def lint(self, project_id: Optional[str] = None) -> str:
        project_id = self._resolve_project_id(project_id)
        pages = self._all_pages(project_id=project_id)
        by_link = self._link_lookup(pages)
        issues: List[str] = []
        for page in pages:
            path = str(page["path"])
            text = self._page_path(path).read_text(encoding="utf-8")
            for link in self._wikilinks(text):
                if self._normalize_link(link) not in by_link:
                    issues.append(f"Broken wikilink in {path}: [[{link}]]")
            if not page.get("sources") and path not in {"index.md", "overview.md", "log.md"}:
                issues.append(f"Missing sources in {path}")
        if not issues:
            return f"Wiki lint passed for project {project_id}. Pages checked: {len(pages)}."
        return "Wiki lint found issues:\n" + "\n".join(f"- {issue}" for issue in issues)

    def status_text(self, project_id: Optional[str] = None) -> str:
        project_id = self._resolve_project_id(project_id)
        page_count = len(self._all_pages(project_id=project_id))
        raw_count = sum(1 for _ in self.conversations_dir.glob("*.jsonl"))
        pending_count = len(self._read_pending_events())
        return "\n".join([
            f"Wiki memory root: {self.root}",
            f"Wiki pages: {page_count}",
            f"Raw conversation files: {raw_count}",
            f"Pending wiki events: {pending_count}",
            f"FTS enabled: {self.fts_enabled} ({self.fts_tokenizer})",
            f"Review queue: {self.review_pending}",
        ])

    def format_hits(self, hits: Sequence[Dict[str, Any]], title: str = "Wiki Search") -> str:
        if not hits:
            return "No matching wiki pages found."
        parts = [f"### {title}"]
        for item in hits:
            sources = ", ".join(_ensure_list(item.get("sources"))) or "none"
            parts.append(
                f"\n[{item.get('path')}] {item.get('title')} | {item.get('type')} | score={float(item.get('score', 0)):.2f}\n"
                f"sources: {sources}\n"
                f"{str(item.get('text') or '').strip()}"
            )
        return "\n".join(parts)

    def _resolve_project_id(self, project_id: Optional[str] = None) -> str:
        return _safe_id(project_id or self.default_project_id)

    def read_page(self, identifier: str) -> str:
        path = self._find_page(identifier)
        if not path:
            return f"Wiki page not found: {identifier}"
        return path.read_text(encoding="utf-8")

    def read_review_queue(self) -> str:
        return self.review_pending.read_text(encoding="utf-8")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    path TEXT PRIMARY KEY,
                    title TEXT,
                    page_type TEXT,
                    project_id TEXT,
                    content TEXT,
                    sources_json TEXT,
                    links_json TEXT,
                    updated_at TEXT
                )
                """
            )
            self._ensure_fts(conn)
            conn.commit()

    def _ensure_fts(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'wiki_pages_fts'"
        ).fetchone()
        if existing:
            self.fts_enabled = True
            self.fts_tokenizer = "existing"
            return
        for tokenizer in ("trigram", "unicode61"):
            try:
                conn.execute(
                    f"""
                    CREATE VIRTUAL TABLE wiki_pages_fts
                    USING fts5(path, title, body, search_text, tokenize = '{tokenizer}')
                    """
                )
                self.fts_enabled = True
                self.fts_tokenizer = tokenizer
                return
            except sqlite3.OperationalError:
                continue
        self.fts_enabled = False
        self.fts_tokenizer = "none"

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _raw_event_exists(self, raw_path: Path, event_id: str) -> bool:
        if not raw_path.exists():
            return False
        return f'"event_id": "{event_id}"' in raw_path.read_text(encoding="utf-8", errors="replace")

    def _externalize_large_values(self, value: Any, event_id: str, key_path: str = "record") -> Any:
        if isinstance(value, str):
            value = redact_secrets(value)
            if len(value) <= self.max_inline_chars:
                return value
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            safe_key = _slug(key_path.replace(".", "_"), default="large")
            path = self.large_outputs_dir / f"{event_id}_{safe_key}_{digest}.txt"
            if not path.exists():
                path.write_text(value, encoding="utf-8")
            return {
                "type": "large_text_ref",
                "path": str(path),
                "chars": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "preview": value[:600],
            }
        if isinstance(value, list):
            return [
                self._externalize_large_values(item, event_id, f"{key_path}.{idx}")
                for idx, item in enumerate(value)
            ]
        if isinstance(value, dict):
            return {
                str(redact_secrets(key)): self._externalize_large_values(item, event_id, f"{key_path}.{key}")
                for key, item in value.items()
            }
        return value

    def _append_pending_event(self, event: Dict[str, Any]) -> None:
        existing_ids = {str(item.get("event_id")) for item in self._read_pending_events()}
        if str(event.get("event_id")) in existing_ids:
            return
        with self.pending_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _read_pending_events(self) -> List[Dict[str, Any]]:
        if not self.pending_file.exists():
            return []
        events: List[Dict[str, Any]] = []
        for line in self.pending_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                events.append(data)
        return events

    def _read_processed_events(self) -> set[str]:
        if not self.processed_file.exists():
            return set()
        try:
            data = json.loads(self.processed_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return set()
        if not isinstance(data, list):
            return set()
        return {str(item) for item in data}

    def _write_processed_events(self, processed: Iterable[str]) -> None:
        ordered = sorted({str(item) for item in processed if str(item).strip()})
        self.processed_file.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _rewrite_pending_events(self, processed: set[str]) -> None:
        remaining = [
            item for item in self._read_pending_events()
            if str(item.get("event_id")) not in processed
        ]
        self.pending_file.write_text(
            "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in remaining),
            encoding="utf-8",
        )

    def _load_raw_records(self, pending: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        wanted = {str(item.get("event_id")) for item in pending}
        for item in pending:
            raw_file = Path(str(item.get("raw_file") or ""))
            if not raw_file.exists():
                continue
            for line in raw_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(record.get("event_id")) in wanted:
                    records.append(record)
        return records

    def _operation_requires_review(self, operation: Dict[str, Any]) -> bool:
        confidence = str(operation.get("confidence") or "").strip().lower()
        if confidence == "low":
            return True
        return bool(operation.get("requires_review") or operation.get("conflict") or operation.get("stale"))

    def _op_create_page(self, operation: Dict[str, Any], default_project_id: str) -> None:
        title = str(operation.get("title") or "Untitled Wiki Page").strip()
        page_type = str(operation.get("type") or operation.get("page_type") or "synthesis").strip().lower()
        if page_type not in ALLOWED_PAGE_TYPES:
            page_type = "synthesis"
        path = self._resolve_operation_path(operation, title=title, page_type=page_type)
        content = self._operation_text(operation)
        sources = _ensure_list(operation.get("sources"))
        frontmatter = self._default_frontmatter(
            title=title,
            page_type=page_type,
            project_id=str(operation.get("project_id") or default_project_id),
            sources=sources,
            aliases=_ensure_list(operation.get("aliases")),
            tags=_ensure_list(operation.get("tags")),
            confidence=str(operation.get("confidence") or "medium"),
            status=str(operation.get("status") or "active"),
        )
        extra_fm = operation.get("frontmatter")
        if isinstance(extra_fm, dict):
            frontmatter.update({str(key): sanitize_value(value) for key, value in extra_fm.items()})
            frontmatter["sources"] = self._merge_lists(frontmatter.get("sources"), sources)
        if path.exists():
            self._append_section_to_page(path, "Updates", content, sources=sources)
            return
        body = f"# {title}\n\n{content.strip() or '- None recorded yet.'}\n"
        self._write_page(path, frontmatter, body)
        self._append_log(f"Created wiki page: {self._relative_page_path(path)}")

    def _op_append_section(self, operation: Dict[str, Any], default_project_id: str) -> None:
        path = self._resolve_operation_path(operation)
        if not path.exists():
            title = str(operation.get("title") or path.stem.replace("-", " ").title())
            self._write_page(
                path,
                self._default_frontmatter(
                    title=title,
                    page_type=str(operation.get("type") or "synthesis"),
                    project_id=str(operation.get("project_id") or default_project_id),
                    sources=_ensure_list(operation.get("sources")),
                ),
                f"# {title}\n",
            )
        self._append_section_to_page(
            path,
            str(operation.get("section") or "Updates"),
            self._operation_text(operation),
            sources=_ensure_list(operation.get("sources")),
        )

    def _op_merge_section_items(self, operation: Dict[str, Any], default_project_id: str) -> None:
        items = _ensure_list(operation.get("items") or operation.get("content"))
        body = "\n".join(f"- {item}" for item in items)
        merged = dict(operation)
        merged["content"] = body
        self._op_append_section(merged, default_project_id)

    def _op_update_frontmatter(self, operation: Dict[str, Any], default_project_id: str) -> None:
        path = self._resolve_operation_path(operation)
        if not path.exists():
            raise ValueError(f"Cannot update frontmatter for missing page: {path}")
        text = path.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        values = operation.get("values") or operation.get("frontmatter") or {}
        if not isinstance(values, dict):
            raise ValueError("update_frontmatter requires values/frontmatter object")
        for key, value in sanitize_value(values).items():
            key = str(key)
            if key in LIST_FRONTMATTER_KEYS:
                frontmatter[key] = self._merge_lists(frontmatter.get(key), value)
            else:
                frontmatter[key] = value
        frontmatter["project_id"] = str(frontmatter.get("project_id") or default_project_id)
        frontmatter["updated_at"] = _now()
        self._write_page(path, frontmatter, body)

    def _resolve_operation_path(
        self,
        operation: Dict[str, Any],
        title: Optional[str] = None,
        page_type: Optional[str] = None,
    ) -> Path:
        raw_path = str(operation.get("path") or operation.get("page") or "").strip()
        if not raw_path:
            inferred_type = page_type or str(operation.get("type") or operation.get("page_type") or "synthesis")
            folder = TYPE_DIRS.get(inferred_type, "synthesis")
            raw_path = f"{folder}/{_slug(title or operation.get('title') or 'page')}.md"
        return self._safe_wiki_path(raw_path)

    def _safe_wiki_path(self, raw_path: str) -> Path:
        raw = redact_secrets(raw_path).replace("\\", "/").strip()
        raw = re.sub(r"^/+","", raw)
        if raw.startswith("wiki/"):
            raw = raw[5:]
        if not raw or raw.endswith("/"):
            raise ValueError("Wiki page path is empty.")
        parts = [part for part in raw.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError(f"Unsafe wiki page path: {raw_path}")
        if any(":" in part for part in parts):
            raise ValueError(f"Unsafe wiki page path: {raw_path}")
        if not parts[-1].endswith(".md"):
            parts[-1] = parts[-1] + ".md"
        path = (self.wiki_dir / Path(*parts)).resolve()
        try:
            path.relative_to(self.wiki_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"Wiki page path escapes wiki directory: {raw_path}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _page_path(self, rel_path: str) -> Path:
        return self._safe_wiki_path(rel_path)

    def _relative_page_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.wiki_dir.resolve()).as_posix()

    def _operation_text(self, operation: Dict[str, Any]) -> str:
        content = operation.get("content")
        if content is None:
            content = operation.get("body")
        if isinstance(content, list):
            return "\n".join(f"- {redact_secrets(item).strip()}" for item in content if str(item).strip())
        if isinstance(content, dict):
            return json.dumps(sanitize_value(content), ensure_ascii=False, indent=2, default=str)
        return redact_secrets(content or "").strip()

    def _default_frontmatter(
        self,
        title: str,
        page_type: str,
        project_id: str,
        sources: Optional[Sequence[str]] = None,
        aliases: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        confidence: str = "medium",
        status: str = "active",
    ) -> Dict[str, Any]:
        timestamp = _now()
        return {
            "title": redact_secrets(title).strip(),
            "type": page_type if page_type in ALLOWED_PAGE_TYPES else "synthesis",
            "project_id": _safe_id(project_id),
            "status": redact_secrets(status or "active"),
            "created_at": timestamp,
            "updated_at": timestamp,
            "sources": _ensure_list(sources),
            "aliases": _ensure_list(aliases),
            "tags": _ensure_list(tags),
            "confidence": redact_secrets(confidence or "medium"),
        }

    def _append_section_to_page(self, path: Path, section: str, content: str, sources: Sequence[str]) -> None:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        frontmatter, body = self._split_frontmatter(text)
        frontmatter["updated_at"] = _now()
        frontmatter["sources"] = self._merge_lists(frontmatter.get("sources"), sources)
        content = content.strip()
        if not content:
            content = "- No details provided."
        pattern = re.compile(
            rf"^## {re.escape(section)}\n(?P<body>.*?)(?=^## |\Z)",
            flags=re.DOTALL | re.MULTILINE,
        )
        match = pattern.search(body)
        if match:
            existing_lines = {line.strip().lower() for line in match.group("body").splitlines()}
            new_lines = [line for line in content.splitlines() if line.strip().lower() not in existing_lines]
            if not new_lines:
                self._write_page(path, frontmatter, body)
                return
            section_body = match.group("body").rstrip()
            replacement = f"## {section}\n{section_body}\n\n" + "\n".join(new_lines).rstrip() + "\n"
            body = body[: match.start()] + replacement + body[match.end() :]
        else:
            body = body.rstrip() + f"\n\n## {section}\n{content}\n"
        self._write_page(path, frontmatter, body)
        self._append_log(f"Updated {self._relative_page_path(path)} section: {section}")

    def _write_page(self, path: Path, frontmatter: Dict[str, Any], body: str) -> None:
        frontmatter = dict(frontmatter)
        frontmatter.setdefault("updated_at", _now())
        rendered = self._render_frontmatter(frontmatter) + "\n" + body.strip() + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")

    def _split_frontmatter(self, text: str) -> Tuple[Dict[str, Any], str]:
        if not text.startswith("---\n"):
            return {}, text
        end = text.find("\n---", 4)
        if end < 0:
            return {}, text
        raw = text[4:end].strip()
        body = text[end + 4 :].lstrip("\n")
        frontmatter: Dict[str, Any] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value.strip('"')
            frontmatter[key] = parsed
        return frontmatter, body

    def _render_frontmatter(self, frontmatter: Dict[str, Any]) -> str:
        lines = ["---"]
        for key in [
            "title",
            "type",
            "project_id",
            "status",
            "created_at",
            "updated_at",
            "sources",
            "aliases",
            "tags",
            "confidence",
            "supersedes",
        ]:
            if key not in frontmatter:
                continue
            value = frontmatter[key]
            if key in LIST_FRONTMATTER_KEYS:
                value = _ensure_list(value)
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
        for key in sorted(k for k in frontmatter if k not in {line.split(':', 1)[0] for line in lines[1:]}):
            lines.append(f"{key}: {json.dumps(frontmatter[key], ensure_ascii=False, default=str)}")
        lines.append("---")
        return "\n".join(lines)

    @staticmethod
    def _merge_lists(existing: Any, new_values: Any) -> List[str]:
        merged: List[str] = []
        seen: set[str] = set()
        for item in [*_ensure_list(existing), *_ensure_list(new_values)]:
            if item not in seen:
                seen.add(item)
                merged.append(item)
        return merged

    def _flag_review(self, title: str, body: str, payload: Dict[str, Any], project_id: str) -> None:
        with self.review_pending.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n\n## {_now()} - {redact_secrets(title).strip() or 'Review Item'}\n\n"
                f"- project_id: {_safe_id(project_id)}\n\n"
            )
            if body:
                handle.write(redact_secrets(body).strip() + "\n\n")
            handle.write("```json\n")
            handle.write(json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, default=str))
            handle.write("\n```\n")

    def _append_log(self, message: str) -> None:
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## {_now()}\n\n{redact_secrets(message).strip()}\n")

    def _rebuild_index_page(self) -> None:
        pages = self._all_pages(include_system=False)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for page in pages:
            groups.setdefault(str(page.get("type") or "unknown"), []).append(page)
        lines = ["# Wiki Index", ""]
        if not pages:
            lines.append("No wiki pages indexed yet.")
        for page_type in sorted(groups):
            lines.append(f"## {page_type.title()}")
            for page in sorted(groups[page_type], key=lambda item: str(item.get("title", "")).lower()):
                lines.append(f"- [[{page['path']}|{page['title']}]]")
            lines.append("")
        self.index_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        counts = ", ".join(f"{key}: {len(value)}" for key, value in sorted(groups.items())) or "none"
        self.overview_file.write_text(
            "# Wiki Overview\n\n"
            f"Updated: {_now()}\n\n"
            f"Pages: {len(pages)}\n\n"
            f"Page types: {counts}\n",
            encoding="utf-8",
        )

    def _all_pages(self, project_id: Optional[str] = None, include_system: bool = False) -> List[Dict[str, Any]]:
        pages: List[Dict[str, Any]] = []
        skip = set()
        if not include_system:
            skip = {
                self.index_file.resolve(),
                self.log_file.resolve(),
                self.overview_file.resolve(),
                self.review_pending.resolve(),
            }
        for path in sorted(self.wiki_dir.rglob("*.md")):
            if path.resolve() in skip:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            frontmatter, body = self._split_frontmatter(text)
            page_project = str(frontmatter.get("project_id") or "")
            if project_id and page_project and page_project != _safe_id(project_id):
                continue
            title = str(frontmatter.get("title") or self._heading_title(body) or path.stem)
            pages.append({
                "path": self._relative_page_path(path),
                "title": title,
                "type": str(frontmatter.get("type") or "unknown"),
                "project_id": page_project,
                "content": body,
                "sources": _ensure_list(frontmatter.get("sources")),
                "links": self._wikilinks(body),
                "updated_at": str(frontmatter.get("updated_at") or ""),
            })
        return pages

    @staticmethod
    def _heading_title(body: str) -> str:
        match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    def _rebuild_search_index(self) -> None:
        pages = self._all_pages(include_system=False)
        with self._connect() as conn:
            conn.execute("DELETE FROM wiki_pages")
            if self.fts_enabled:
                try:
                    conn.execute("DELETE FROM wiki_pages_fts")
                except sqlite3.Error:
                    self.fts_enabled = False
            for page in pages:
                sources_json = json.dumps(page["sources"], ensure_ascii=False)
                links_json = json.dumps(page["links"], ensure_ascii=False)
                search_text = "\n".join([
                    str(page["title"]),
                    str(page["type"]),
                    str(page["path"]),
                    str(page["content"]),
                    " ".join(page["sources"]),
                    " ".join(page["links"]),
                ])
                conn.execute(
                    """
                    INSERT OR REPLACE INTO wiki_pages
                    (path, title, page_type, project_id, content, sources_json, links_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page["path"],
                        page["title"],
                        page["type"],
                        page["project_id"],
                        page["content"],
                        sources_json,
                        links_json,
                        page["updated_at"],
                    ),
                )
                if self.fts_enabled:
                    try:
                        conn.execute(
                            "INSERT INTO wiki_pages_fts(path, title, body, search_text) VALUES (?, ?, ?, ?)",
                            (page["path"], page["title"], page["content"], search_text),
                        )
                    except sqlite3.Error:
                        self.fts_enabled = False
            conn.commit()

    def _fts_search(self, query: str, project_id: str, limit: int) -> List[WikiSearchHit]:
        if not self.fts_enabled:
            return []
        match_query = self._fts_query(query)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT p.*, bm25(wiki_pages_fts) AS rank
                    FROM wiki_pages_fts
                    JOIN wiki_pages p ON p.path = wiki_pages_fts.path
                    WHERE wiki_pages_fts MATCH ?
                      AND (p.project_id = ? OR p.project_id = '')
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match_query, _safe_id(project_id), limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [self._hit_from_row(row, score=max(0.1, 10.0 - abs(float(row["rank"] or 0.0)))) for row in rows]

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = _terms(query)
        if not tokens:
            return '""'
        quoted = []
        for token in tokens[:8]:
            quoted.append(f'"{token.replace(chr(34), chr(34) + chr(34))}"')
        return " OR ".join(quoted)

    def _lexical_search(self, query: str, project_id: str, limit: int) -> List[WikiSearchHit]:
        terms = _terms(query)
        hits: List[WikiSearchHit] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wiki_pages WHERE project_id = ? OR project_id = ''",
                (_safe_id(project_id),),
            ).fetchall()
        for row in rows:
            haystack = "\n".join([
                str(row["title"] or ""),
                str(row["page_type"] or ""),
                str(row["path"] or ""),
                str(row["content"] or ""),
                str(row["sources_json"] or ""),
                str(row["links_json"] or ""),
            ]).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                if any(term in str(row["title"] or "").lower() for term in terms):
                    score += 5
                hits.append(self._hit_from_row(row, float(score)))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    def _recent_pages(self, project_id: str, limit: int) -> List[WikiSearchHit]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wiki_pages WHERE project_id = ? OR project_id = '' ORDER BY updated_at DESC LIMIT ?",
                (_safe_id(project_id), limit),
            ).fetchall()
        return [self._hit_from_row(row, 1.0) for row in rows]

    @staticmethod
    def _hit_from_row(row: sqlite3.Row, score: float) -> WikiSearchHit:
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except json.JSONDecodeError:
            sources = []
        return WikiSearchHit(
            path=str(row["path"] or ""),
            title=str(row["title"] or ""),
            page_type=str(row["page_type"] or ""),
            score=float(score),
            text=_clip(str(row["content"] or ""), 1600),
            sources=_ensure_list(sources),
        )

    def _expand_hits(self, hits: List[WikiSearchHit], project_id: str, top_k: int) -> List[WikiSearchHit]:
        if not hits:
            return []
        by_path = {hit.path: hit for hit in hits}
        pages = self._all_pages(project_id=project_id)
        lookup = self._link_lookup(pages)
        seed_sources = {source for hit in hits for source in hit.sources}
        seed_links: set[str] = set()
        for hit in hits:
            page_path = self._page_path(hit.path)
            if page_path.exists():
                seed_links.update(self._wikilinks(page_path.read_text(encoding="utf-8")))
        for link in seed_links:
            page = lookup.get(self._normalize_link(link))
            if page and page["path"] not in by_path:
                by_path[page["path"]] = WikiSearchHit(
                    path=page["path"],
                    title=page["title"],
                    page_type=page["type"],
                    score=0.75,
                    text=_clip(page["content"], 1200),
                    sources=page["sources"],
                )
        if seed_sources:
            for page in pages:
                if page["path"] in by_path:
                    continue
                if seed_sources & set(page["sources"]):
                    by_path[page["path"]] = WikiSearchHit(
                        path=page["path"],
                        title=page["title"],
                        page_type=page["type"],
                        score=0.6,
                        text=_clip(page["content"], 1200),
                        sources=page["sources"],
                    )
        ordered = list(by_path.values())
        ordered.sort(key=lambda hit: hit.score, reverse=True)
        return ordered[: max(top_k, len(hits))]

    def _link_lookup(self, pages: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        lookup: Dict[str, Dict[str, Any]] = {}
        for page in pages:
            keys = {
                page["path"],
                page["path"].removesuffix(".md"),
                page["title"],
                Path(page["path"]).stem,
            }
            for key in keys:
                lookup[self._normalize_link(key)] = page
        return lookup

    @staticmethod
    def _wikilinks(text: str) -> List[str]:
        links: List[str] = []
        for match in re.finditer(r"\[\[([^\]]+)\]\]", text):
            label = match.group(1).split("|", 1)[0].strip()
            if label:
                links.append(label)
        return links

    @staticmethod
    def _normalize_link(link: str) -> str:
        return link.replace("\\", "/").strip().lower().removesuffix(".md")

    def _find_page(self, identifier: str) -> Optional[Path]:
        try:
            path = self._safe_wiki_path(identifier)
            if path.exists():
                return path
        except ValueError:
            pass
        needle = self._normalize_link(identifier)
        for page in self._all_pages(include_system=True):
            keys = {
                page["path"],
                page["path"].removesuffix(".md"),
                page["title"],
                Path(page["path"]).stem,
            }
            if needle in {self._normalize_link(key) for key in keys}:
                return self._page_path(page["path"])
        return None
