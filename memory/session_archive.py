from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .patch_validator import redact_secrets, sanitize_value
from .utils import now_iso, query_terms


ARCHIVE_SCHEMA_VERSION = 1


def _now() -> str:
    return now_iso()


def _clip_middle(text: Any, head: int = 1200, tail: int = 1800) -> str:
    value = redact_secrets(text)
    limit = head + tail
    if len(value) <= limit:
        return value
    skipped = len(value) - limit
    return f"{value[:head]}\n... [Skipped {skipped} chars] ...\n{value[-tail:]}"


def _json(value: Any) -> str:
    return json.dumps(sanitize_value(value), ensure_ascii=False, default=str)


@dataclass
class ArchiveHit:
    id: int
    task_id: str
    stage: str
    event_type: str
    timestamp: str
    summary: str
    content: str
    paths: List[str]
    tags: List[str]
    returncode: Optional[int]
    score: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row, score: float = 0.0) -> "ArchiveHit":
        paths = _loads_list(row["paths_json"])
        tags = _loads_list(row["tags_json"])
        return cls(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            stage=str(row["stage"]),
            event_type=str(row["event_type"]),
            timestamp=str(row["timestamp"]),
            summary=str(row["summary"] or ""),
            content=str(row["content"] or ""),
            paths=paths,
            tags=tags,
            returncode=row["returncode"],
            score=score,
        )


def _loads_list(value: Any) -> List[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


class SessionArchive:
    """SQLite FTS-backed archive for noisy cross-session event history."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
    ) -> int:
        content = _clip_middle(content)
        summary = _clip_middle(summary or content, head=600, tail=600)
        metadata = sanitize_value(metadata or {})
        paths = [redact_secrets(path) for path in (paths or []) if str(path).strip()]
        tags = [redact_secrets(tag) for tag in (tags or []) if str(tag).strip()]
        search_text = "\n".join([
            task_id,
            session_id or "",
            stage,
            event_type,
            summary,
            content,
            " ".join(paths),
            " ".join(tags),
            _json(metadata),
        ])
        timestamp = _now()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO session_events (
                    task_id, session_id, stage, event_type, timestamp,
                    content, summary, metadata_json, paths_json, tags_json,
                    severity, returncode, search_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    session_id,
                    stage,
                    event_type,
                    timestamp,
                    content,
                    summary,
                    _json(metadata),
                    _json(paths),
                    _json(tags),
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
    ) -> List[ArchiveHit]:
        query = query.strip()
        if not query:
            return self.recent_events(task_id=task_id, limit=limit)

        hits = self._fts_search(query, task_id, event_types, limit)
        if hits:
            return hits
        return self._lexical_search(query, task_id, event_types, limit)

    def recent_events(self, task_id: Optional[str] = None, limit: int = 8) -> List[ArchiveHit]:
        sql = "SELECT * FROM session_events"
        params: List[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ArchiveHit.from_row(row) for row in rows]

    def resume_events(self, task_id: str, limit: int = 8) -> List[ArchiveHit]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_events WHERE task_id = ? ORDER BY id DESC LIMIT 200",
                (task_id,),
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

    def format_hits(self, hits: Sequence[ArchiveHit], title: str = "Session Archive Search") -> str:
        if not hits:
            return "No matching session archive events found."
        parts = [f"### {title}"]
        for hit in hits:
            paths = f"\npaths: {', '.join(hit.paths)}" if hit.paths else ""
            summary = hit.summary.strip() or hit.content.strip()
            parts.append(
                f"\n[{hit.id}] {hit.timestamp} | {hit.task_id} | {hit.stage}/{hit.event_type}"
                f"{paths}\n{summary.strip()}"
            )
        return "\n".join(parts)

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
            tasks = conn.execute("SELECT COUNT(DISTINCT task_id) FROM session_events").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "events": total,
            "tasks": tasks,
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
                    stage TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    content TEXT,
                    summary TEXT,
                    metadata_json TEXT,
                    paths_json TEXT,
                    tags_json TEXT,
                    severity TEXT,
                    returncode INTEGER,
                    search_text TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_fts(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO schema_version (component, version, updated_at)
                VALUES ('session_archive', ?, ?)
                """,
                (ARCHIVE_SCHEMA_VERSION, _now()),
            )
            conn.commit()

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
    ) -> List[ArchiveHit]:
        if not self.fts_enabled:
            return []
        match_query = self._fts_query(query)
        filters, params = self._filters(task_id, event_types, table_alias="e")
        sql = (
            "SELECT e.*, bm25(session_events_fts) AS score "
            "FROM session_events_fts "
            "JOIN session_events e ON e.id = session_events_fts.rowid "
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
    ) -> List[ArchiveHit]:
        filters, params = self._filters(task_id, event_types)
        sql = "SELECT * FROM session_events"
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
        table_alias: str = "",
    ) -> tuple[List[str], List[Any]]:
        prefix = f"{table_alias}." if table_alias else ""
        filters: List[str] = []
        params: List[Any] = []
        if task_id:
            filters.append(f"{prefix}task_id = ?")
            params.append(task_id)
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            filters.append(f"{prefix}event_type IN ({placeholders})")
            params.extend(event_types)
        return filters, params

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
