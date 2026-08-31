from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .patch_validator import redact_secrets, sanitize_value
from .utils import DEFAULT_PROJECT_ID, now_iso, query_terms, safe_id


ARCHIVE_SCHEMA_VERSION = 3
ARCHIVE_BUSY_TIMEOUT_SECONDS = 15.0
TASK_EVENT_LOOKUP_INDEX = "idx_session_events_task_id_id"


logger = logging.getLogger(__name__)


def _now() -> str:
    return now_iso()


def _clip(text: Any, limit: int = 3_000) -> str:
    value = redact_secrets(text)
    if len(value) <= limit:
        return value
    skipped = len(value) - limit
    head = max(1, int(limit * 0.4))
    tail = max(1, limit - head)
    return f"{value[:head]}\n... [Skipped {skipped} chars] ...\n{value[-tail:]}"


def _json(value: Any) -> str:
    return json.dumps(sanitize_value(value), ensure_ascii=False, default=str)


def _loads_json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _loads_list(value: Any) -> List[str]:
    parsed = _loads_json(value, [])
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _loads_dict(value: Any) -> Dict[str, Any]:
    parsed = _loads_json(value, {})
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class ArchiveHit:
    id: int
    task_id: str
    session_id: str
    project_id: str
    stage: str
    event_type: str
    timestamp: str
    summary: str
    content: str
    metadata: Dict[str, Any]
    paths: List[str]
    tags: List[str]
    source_refs: List[str]
    task_semantics: Dict[str, Any]
    large_refs: List[Dict[str, Any]]
    content_hash: str
    severity: str
    returncode: Optional[int]
    score: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row, score: float = 0.0) -> "ArchiveHit":
        keys = set(row.keys())
        return cls(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"] or ""),
            project_id=str(row["project_id"] or DEFAULT_PROJECT_ID) if "project_id" in keys else DEFAULT_PROJECT_ID,
            stage=str(row["stage"]),
            event_type=str(row["event_type"]),
            timestamp=str(row["timestamp"]),
            summary=str(row["summary"] or ""),
            content=str(row["content"] or ""),
            metadata=_loads_dict(row["metadata_json"]),
            paths=_loads_list(row["paths_json"]),
            tags=_loads_list(row["tags_json"]),
            source_refs=_loads_list(row["source_refs_json"]) if "source_refs_json" in keys else [],
            task_semantics=_loads_dict(row["task_semantics_json"]) if "task_semantics_json" in keys else {},
            large_refs=_loads_json(row["large_refs_json"], []) if "large_refs_json" in keys else [],
            content_hash=str(row["content_hash"] or "") if "content_hash" in keys else "",
            severity=str(row["severity"] or "") if "severity" in keys else "",
            returncode=row["returncode"],
            score=score,
        )

    @property
    def evidence_ref(self) -> str:
        return f"session_archive:{self.id}"

    def as_wiki_record(self) -> Dict[str, Any]:
        return {
            "event_id": str(self.id),
            "event_ref": self.evidence_ref,
            "content_hash": self.content_hash,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "stage": self.stage,
            "event_type": self.event_type,
            "summary": self.summary,
            "content": self.content,
            "metadata": self.metadata,
            "payload": self.metadata,
            "paths": self.paths,
            "tags": self.tags,
            "returncode": self.returncode,
            "severity": self.severity,
            "source_refs": [self.evidence_ref, *self.source_refs],
            "task_semantics": self.task_semantics,
            "large_refs": self.large_refs,
        }


class SessionArchive:
    """SQLite/FTS-backed source of truth for cross-session process events."""

    def __init__(
        self,
        db_path: str | Path,
        max_inline_chars: int = 4_000,
        busy_timeout_seconds: float = ARCHIVE_BUSY_TIMEOUT_SECONDS,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.large_outputs_dir = self.db_path.parent / "archive_large_outputs"
        self.large_outputs_dir.mkdir(parents=True, exist_ok=True)
        self.max_inline_chars = max_inline_chars
        self.busy_timeout_seconds = max(0.1, float(busy_timeout_seconds))
        self.busy_timeout_ms = max(100, int(self.busy_timeout_seconds * 1000))
        self.fts_enabled = False
        self.fts_tokenizer = "none"
        self._init_db()

    def append_event(
        self,
        task_id: str,
        session_id: Optional[str],
        stage: str,
        event_type: str,
        content: str,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        paths: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        severity: Optional[str] = None,
        returncode: Optional[int] = None,
        project_id: Optional[str] = None,
        task_semantics: Optional[Dict[str, Any]] = None,
        source_refs: Optional[Sequence[str]] = None,
    ) -> int:
        task_id = safe_id(task_id or "default")
        session_id = safe_id(session_id or task_id)
        project_id = safe_id(project_id or DEFAULT_PROJECT_ID)
        stage = redact_secrets(stage).strip() or "event"
        event_type = redact_secrets(event_type).strip() or f"{stage}_event"
        metadata = sanitize_value(metadata or {})
        task_semantics = sanitize_value(task_semantics or {})
        paths = [redact_secrets(path) for path in (paths or []) if str(path).strip()]
        tags = [redact_secrets(tag) for tag in (tags or []) if str(tag).strip()]
        source_refs = [redact_secrets(ref) for ref in (source_refs or []) if str(ref).strip()]
        content = redact_secrets(content or stage)
        summary = redact_secrets(summary or content)

        hash_seed = {
            "task_id": task_id,
            "session_id": session_id,
            "project_id": project_id,
            "stage": stage,
            "event_type": event_type,
            "content": content,
            "summary": summary,
            "metadata": metadata,
            "paths": paths,
            "tags": tags,
            "severity": severity,
            "returncode": returncode,
            "task_semantics": task_semantics,
            "source_refs": source_refs,
        }
        content_hash = hashlib.sha256(_json(hash_seed).encode("utf-8")).hexdigest()

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM session_events WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            ).fetchone()
            if existing:
                return int(existing["id"])

        large_refs: List[Dict[str, Any]] = []
        content = self._externalize_text(content, content_hash, "content", large_refs)
        summary = self._externalize_text(summary, content_hash, "summary", large_refs, limit=1_200)
        metadata = self._externalize_large_values(metadata, content_hash, "metadata", large_refs)
        task_semantics = self._externalize_large_values(task_semantics, content_hash, "task_semantics", large_refs)

        search_text = "\n".join([
            task_id,
            session_id,
            project_id,
            stage,
            event_type,
            summary,
            content,
            " ".join(paths),
            " ".join(tags),
            _json(metadata),
            _json(task_semantics),
        ])
        timestamp = _now()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO session_events (
                    task_id, session_id, project_id, stage, event_type, timestamp,
                    content, summary, metadata_json, paths_json, tags_json,
                    source_refs_json, task_semantics_json, large_refs_json,
                    content_hash, severity, returncode, search_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    session_id,
                    project_id,
                    stage,
                    event_type,
                    timestamp,
                    content,
                    summary,
                    _json(metadata),
                    _json(paths),
                    _json(tags),
                    _json(source_refs),
                    _json(task_semantics),
                    _json(large_refs),
                    content_hash,
                    severity,
                    returncode,
                    search_text,
                ),
            )
            event_id = int(cursor.lastrowid)
            if self.fts_enabled:
                conn.execute(
                    """
                    INSERT INTO session_events_fts (
                        rowid, task_id, stage, event_type, content,
                        summary, paths, tags, search_text
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        task_id,
                        stage,
                        event_type,
                        content,
                        summary,
                        " ".join(paths),
                        " ".join(tags),
                        search_text,
                    ),
                )
            conn.commit()
        return event_id

    def search(
        self,
        query: str,
        task_id: Optional[str] = None,
        event_types: Optional[Sequence[str]] = None,
        limit: int = 8,
        project_id: Optional[str] = None,
        pending_for_wiki_only: bool = False,
    ) -> List[ArchiveHit]:
        query = query.strip()
        if not query:
            if pending_for_wiki_only:
                return self.pending_for_wiki(project_id=project_id, limit=limit, task_id=task_id)
            return self.recent_events(task_id=task_id, limit=limit, project_id=project_id)

        hits = self._fts_search(query, task_id, event_types, limit, project_id, pending_for_wiki_only)
        if hits:
            return hits
        return self._lexical_search(query, task_id, event_types, limit, project_id, pending_for_wiki_only)

    def search_pending_for_wiki(
        self,
        query: str,
        task_id: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 8,
    ) -> List[ArchiveHit]:
        return self.search(
            query,
            task_id=task_id,
            project_id=project_id,
            limit=limit,
            pending_for_wiki_only=True,
        )

    def recent_events(
        self,
        task_id: Optional[str] = None,
        limit: int = 8,
        project_id: Optional[str] = None,
    ) -> List[ArchiveHit]:
        filters, params = self._filters(task_id=task_id, event_types=None, project_id=project_id)
        sql = "SELECT * FROM session_events"
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ArchiveHit.from_row(row) for row in rows]

    def events_for_task(self, task_id: str, limit: int = 200, ascending: bool = True) -> List[ArchiveHit]:
        order = "ASC" if ascending else "DESC"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM session_events WHERE task_id = ? ORDER BY id {order} LIMIT ?",
                (safe_id(task_id), limit),
            ).fetchall()
        return [ArchiveHit.from_row(row) for row in rows]

    def resume_events(self, task_id: str, limit: int = 8) -> List[ArchiveHit]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_events WHERE task_id = ? ORDER BY id DESC LIMIT 200",
                (safe_id(task_id),),
            ).fetchall()
        hits = [ArchiveHit.from_row(row) for row in rows]
        last_success_id = next(
            (
                hit.id for hit in hits
                if hit.event_type == "execution_finished" and hit.returncode == 0
            ),
            None,
        )

        def score(hit: ArchiveHit) -> float:
            priority = {
                "plan_created": 100,
                "artifact_created": 95,
                "supervisor_verdict": 90,
                "user_message": 85,
                "task_finalized": 80,
                "error_detected": 80,
            }.get(hit.event_type, 30)
            if hit.event_type == "execution_finished" and hit.returncode not in (None, 0):
                priority += 70
            if last_success_id is not None and hit.id == last_success_id:
                priority += 50
            if hit.event_type == "execution_finished" and hit.returncode == 0 and not hit.summary.strip():
                priority -= 20
            if hit.stage in {"execute", "supervisor"}:
                priority += 5
            return priority + (hit.id / 1_000_000)

        hits.sort(key=score, reverse=True)
        return hits[:limit]

    def pending_for_wiki(
        self,
        project_id: Optional[str] = None,
        limit: int = 20,
        task_id: Optional[str] = None,
    ) -> List[ArchiveHit]:
        project_id = safe_id(project_id or DEFAULT_PROJECT_ID)
        filters = ["COALESCE(e.project_id, ?) = ?", "w.event_id IS NULL"]
        params: List[Any] = [DEFAULT_PROJECT_ID, project_id]
        if task_id:
            filters.append("e.task_id = ?")
            params.append(safe_id(task_id))
        params.append(max(1, limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM session_events e
                LEFT JOIN wiki_ingest_events w
                  ON w.event_id = e.id AND w.status = 'processed'
                WHERE {" AND ".join(filters)}
                ORDER BY e.id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [ArchiveHit.from_row(row) for row in rows]

    def pending_for_wiki_count(
        self,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> int:
        project_id = safe_id(project_id or DEFAULT_PROJECT_ID)
        filters = ["COALESCE(e.project_id, ?) = ?", "w.event_id IS NULL"]
        params: List[Any] = [DEFAULT_PROJECT_ID, project_id]
        if task_id:
            filters.append("e.task_id = ?")
            params.append(safe_id(task_id))
        with self._connect() as conn:
            return int(conn.execute(
                f"""
                SELECT COUNT(*)
                FROM session_events e
                LEFT JOIN wiki_ingest_events w
                  ON w.event_id = e.id AND w.status = 'processed'
                WHERE {" AND ".join(filters)}
                """,
                params,
            ).fetchone()[0])

    def mark_wiki_ingested(
        self,
        event_ids: Sequence[int | str],
        project_id: Optional[str] = None,
        status: str = "processed",
        summary: str = "",
        error: str = "",
    ) -> None:
        project_id = safe_id(project_id or DEFAULT_PROJECT_ID)
        status = redact_secrets(status).strip() or "processed"
        now = _now()
        with self._connect() as conn:
            for raw_id in event_ids:
                try:
                    event_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """
                    INSERT INTO wiki_ingest_events (
                        event_id, project_id, status, summary, error, ingested_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        project_id=excluded.project_id,
                        status=excluded.status,
                        summary=excluded.summary,
                        error=excluded.error,
                        ingested_at=excluded.ingested_at
                    """,
                    (event_id, project_id, status, redact_secrets(summary), redact_secrets(error), now),
                )
            conn.commit()

    def format_events_for_wiki_prompt(
        self,
        events: Sequence[ArchiveHit],
        total_limit: int = 50_000,
    ) -> str:
        if not events:
            return "No SessionArchive events could be loaded for wiki ingest."
        per_event_limit = max(1_200, total_limit // max(1, len(events)))
        chunks: List[str] = []
        used = 0
        for event in events:
            remaining = total_limit - used
            if remaining < 600:
                break
            chunk = self._event_prompt_preview(event, min(per_event_limit, remaining))
            chunks.append(chunk)
            used += len(chunk) + 2
        omitted = len(events) - len(chunks)
        if omitted > 0:
            chunks.append(f"### Omitted archive events\n{omitted} event(s) omitted because the prompt budget was reached.")
        return _clip("\n\n".join(chunks), total_limit)

    def format_hits(self, hits: Sequence[ArchiveHit], title: str = "Session Archive Search") -> str:
        if not hits:
            return "No matching session archive events found."
        parts = [f"### {title}"]
        for hit in hits:
            paths = f"\npaths: {', '.join(hit.paths)}" if hit.paths else ""
            project = f" | project={hit.project_id}" if hit.project_id else ""
            summary = hit.summary.strip() or hit.content.strip()
            parts.append(
                f"\n[{hit.id}] {hit.timestamp} | {hit.task_id} | {hit.stage}/{hit.event_type}{project}"
                f"{paths}\n{summary.strip()}"
            )
        return "\n".join(parts)

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
            tasks = conn.execute("SELECT COUNT(DISTINCT task_id) FROM session_events").fetchone()[0]
            pending = conn.execute(
                """
                SELECT COUNT(*)
                FROM session_events e
                LEFT JOIN wiki_ingest_events w
                  ON w.event_id = e.id AND w.status = 'processed'
                WHERE w.event_id IS NULL
                """
            ).fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "events": total,
            "tasks": tasks,
            "wiki_pending": pending,
            "fts_enabled": self.fts_enabled,
            "fts_tokenizer": self.fts_tokenizer,
        }

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    session_id TEXT,
                    project_id TEXT,
                    stage TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    content TEXT,
                    summary TEXT,
                    metadata_json TEXT,
                    paths_json TEXT,
                    tags_json TEXT,
                    source_refs_json TEXT,
                    task_semantics_json TEXT,
                    large_refs_json TEXT,
                    content_hash TEXT,
                    severity TEXT,
                    returncode INTEGER,
                    search_text TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_ingest_events (
                    event_id INTEGER PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT,
                    error TEXT,
                    ingested_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES session_events(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_session_events_content_hash ON session_events(content_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_events_project_id ON session_events(project_id)")
            task_index_exists = any(
                str(row[1]) == TASK_EVENT_LOOKUP_INDEX
                for row in conn.execute(
                    "PRAGMA index_list('session_events')"
                ).fetchall()
            )
            migration_started = time.perf_counter()
            if not task_index_exists:
                logger.info(
                    "SessionArchive migration: creating task lookup index %s "
                    "before the interactive prompt.",
                    TASK_EVENT_LOOKUP_INDEX,
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                f"{TASK_EVENT_LOOKUP_INDEX} ON session_events(task_id, id)"
            )
            if not task_index_exists:
                logger.info(
                    "SessionArchive migration complete: %s (%.3fs).",
                    TASK_EVENT_LOOKUP_INDEX,
                    time.perf_counter() - migration_started,
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_ingest_events_project_status ON wiki_ingest_events(project_id, status)")
            self._ensure_fts(conn)
            current_schema = conn.execute(
                "SELECT version FROM schema_version WHERE component = 'session_archive'"
            ).fetchone()
            if (
                current_schema is None
                or int(current_schema["version"]) != ARCHIVE_SCHEMA_VERSION
            ):
                conn.execute(
                    """
                    INSERT INTO schema_version (component, version, updated_at)
                    VALUES ('session_archive', ?, ?)
                    ON CONFLICT(component) DO UPDATE SET
                        version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (ARCHIVE_SCHEMA_VERSION, _now()),
                )
            conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(session_events)").fetchall()}
        additions = {
            "project_id": "TEXT",
            "source_refs_json": "TEXT",
            "task_semantics_json": "TEXT",
            "large_refs_json": "TEXT",
            "content_hash": "TEXT",
        }
        for column, column_type in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE session_events ADD COLUMN {column} {column_type}")

    def _ensure_fts(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_events_fts'"
        ).fetchone()
        if existing:
            self.fts_enabled = True
            self.fts_tokenizer = "existing"
            return

        for tokenizer in ("trigram", "unicode61"):
            try:
                conn.execute(
                    f"""
                    CREATE VIRTUAL TABLE session_events_fts
                    USING fts5(
                        task_id, stage, event_type, content,
                        summary, paths, tags, search_text,
                        tokenize = '{tokenizer}'
                    )
                    """
                )
                self.fts_enabled = True
                self.fts_tokenizer = tokenizer
                return
            except sqlite3.OperationalError:
                continue
        self.fts_enabled = False
        self.fts_tokenizer = "none"

    def _fts_search(
        self,
        query: str,
        task_id: Optional[str],
        event_types: Optional[Sequence[str]],
        limit: int,
        project_id: Optional[str],
        pending_for_wiki_only: bool = False,
    ) -> List[ArchiveHit]:
        if not self.fts_enabled:
            return []
        match_query = self._fts_query(query)
        filters, params = self._filters(task_id, event_types, project_id, table_alias="e")
        if pending_for_wiki_only:
            filters.append("w.event_id IS NULL")
        sql = (
            "SELECT e.*, bm25(session_events_fts) AS score "
            "FROM session_events_fts "
            "JOIN session_events e ON e.id = session_events_fts.rowid "
        )
        if pending_for_wiki_only:
            sql += (
                "LEFT JOIN wiki_ingest_events w "
                "ON w.event_id = e.id AND w.status = 'processed' "
            )
        sql += (
            "WHERE session_events_fts MATCH ?"
        )
        sql_params: List[Any] = [match_query]
        if filters:
            sql += " AND " + " AND ".join(filters)
            sql_params.extend(params)
        sql += " ORDER BY score LIMIT ?"
        sql_params.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, sql_params).fetchall()
        except sqlite3.Error:
            return []
        return [ArchiveHit.from_row(row, score=float(row["score"] or 0.0)) for row in rows]

    def _lexical_search(
        self,
        query: str,
        task_id: Optional[str],
        event_types: Optional[Sequence[str]],
        limit: int,
        project_id: Optional[str],
        pending_for_wiki_only: bool = False,
    ) -> List[ArchiveHit]:
        filters, params = self._filters(task_id, event_types, project_id, table_alias="e")
        sql = "SELECT e.* FROM session_events e"
        if pending_for_wiki_only:
            sql += (
                " LEFT JOIN wiki_ingest_events w"
                " ON w.event_id = e.id AND w.status = 'processed'"
            )
            filters.append("w.event_id IS NULL")
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY id DESC LIMIT 500"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        terms = self._terms(query)
        hits: List[ArchiveHit] = []
        for row in rows:
            haystack = "\n".join([
                str(row["search_text"] or ""),
                str(row["summary"] or ""),
                str(row["content"] or ""),
            ]).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                hits.append(ArchiveHit.from_row(row, score=float(score)))
        hits.sort(key=lambda hit: (hit.score, hit.id), reverse=True)
        return hits[:limit]

    @staticmethod
    def _filters(
        task_id: Optional[str],
        event_types: Optional[Sequence[str]],
        project_id: Optional[str] = None,
        table_alias: str = "",
    ) -> tuple[List[str], List[Any]]:
        prefix = f"{table_alias}." if table_alias else ""
        filters: List[str] = []
        params: List[Any] = []
        if task_id:
            filters.append(f"{prefix}task_id = ?")
            params.append(safe_id(task_id))
        if project_id:
            filters.append(f"COALESCE({prefix}project_id, ?) = ?")
            params.extend([DEFAULT_PROJECT_ID, safe_id(project_id)])
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            filters.append(f"{prefix}event_type IN ({placeholders})")
            params.extend(event_types)
        return filters, params

    def _externalize_text(
        self,
        value: str,
        event_hash: str,
        key_path: str,
        refs: List[Dict[str, Any]],
        limit: Optional[int] = None,
    ) -> str:
        limit = limit or self.max_inline_chars
        if len(value) <= limit:
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        path = self.large_outputs_dir / f"{event_hash[:16]}_{safe_id(key_path)}_{digest[:12]}.txt"
        if not path.exists():
            path.write_text(value, encoding="utf-8")
        refs.append({
            "type": "large_text_ref",
            "key_path": key_path,
            "path": str(path),
            "chars": len(value),
            "sha256": digest,
            "preview": value[:600],
        })
        return _clip(value, limit)

    def _externalize_large_values(
        self,
        value: Any,
        event_hash: str,
        key_path: str,
        refs: List[Dict[str, Any]],
    ) -> Any:
        if isinstance(value, str):
            clean = redact_secrets(value)
            if len(clean) <= self.max_inline_chars:
                return clean
            digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
            path = self.large_outputs_dir / f"{event_hash[:16]}_{safe_id(key_path)}_{digest[:12]}.txt"
            if not path.exists():
                path.write_text(clean, encoding="utf-8")
            ref = {
                "type": "large_text_ref",
                "key_path": key_path,
                "path": str(path),
                "chars": len(clean),
                "sha256": digest,
                "preview": clean[:600],
            }
            refs.append(ref)
            return ref
        if isinstance(value, list):
            processed = [
                self._externalize_large_values(item, event_hash, f"{key_path}.{idx}", refs)
                for idx, item in enumerate(value)
            ]
            json_text = _json(processed)
            if len(json_text) > self.max_inline_chars:
                return self._externalize_json_value(processed, event_hash, key_path, refs, json_text)
            return processed
        if isinstance(value, dict):
            processed = {
                str(redact_secrets(key)): self._externalize_large_values(item, event_hash, f"{key_path}.{key}", refs)
                for key, item in value.items()
            }
            json_text = _json(processed)
            if key_path not in {"metadata", "task_semantics"} and len(json_text) > self.max_inline_chars:
                return self._externalize_json_value(processed, event_hash, key_path, refs, json_text)
            return processed
        return value

    def _externalize_json_value(
        self,
        value: Any,
        event_hash: str,
        key_path: str,
        refs: List[Dict[str, Any]],
        json_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        json_text = json_text if json_text is not None else _json(value)
        digest = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
        path = self.large_outputs_dir / f"{event_hash[:16]}_{safe_id(key_path)}_{digest[:12]}.json"
        if not path.exists():
            path.write_text(json_text, encoding="utf-8")
        ref = {
            "type": "large_json_ref",
            "key_path": key_path,
            "path": str(path),
            "chars": len(json_text),
            "sha256": digest,
            "preview": json_text[:600],
        }
        refs.append(ref)
        return ref

    def _event_prompt_preview(self, event: ArchiveHit, limit: int) -> str:
        record = event.as_wiki_record()
        metadata = {
            key: sanitize_value(record.get(key))
            for key in (
                "event_ref",
                "timestamp",
                "task_id",
                "session_id",
                "project_id",
                "stage",
                "event_type",
                "returncode",
                "paths",
                "source_refs",
            )
            if record.get(key) not in (None, "", [])
        }
        preview = {
            "metadata": metadata,
            "summary": event.summary,
            "content_preview": _clip(event.content, max(500, limit // 3)),
            "metadata_json_preview": _clip(_json(event.metadata), max(500, limit // 3)),
            "task_semantics": event.task_semantics,
            "large_refs": event.large_refs,
            "note": "Payloads are clipped previews; cite the session_archive event ref for source evidence.",
        }
        text = json.dumps(preview, ensure_ascii=False, indent=2, default=str)
        return _clip(f"### {event.evidence_ref}\n{text}", limit)

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w./:\\-]{2,}", query, flags=re.UNICODE)
        if not tokens:
            tokens = [query]
        quoted = []
        for token in tokens[:8]:
            token = token.replace('"', '""')
            quoted.append(f'"{token}"')
        return " OR ".join(quoted)

    @staticmethod
    def _terms(query: str) -> List[str]:
        return query_terms(query)

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_seconds,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            yield conn
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold():
                raise sqlite3.OperationalError(
                    "SessionArchive database remained locked for "
                    f"{self.busy_timeout_seconds:g}s: {self.db_path}. "
                    "Another BioMind process may be writing, or a suspended "
                    "Ctrl-Z process may still hold a transaction."
                ) from exc
            raise
        finally:
            conn.close()
