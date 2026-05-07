from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .patch_validator import redact_secrets, sanitize_value
from .utils import now_iso, query_terms


def _now() -> str:
    return now_iso()


@dataclass
class ArtifactHit:
    id: int
    task_id: str
    artifact_type: str
    key: str
    value: str
    path: str
    timestamp: str
    score: float = 0.0


class ArtifactIndex:
    """Searchable artifact facade over the existing artifacts table."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def register_artifact(
        self,
        task_id: str,
        path: str,
        artifact_type: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        path = redact_secrets(path)
        description = redact_secrets(description or os.path.basename(path) or artifact_type)
        metadata_text = json.dumps(sanitize_value(metadata or {}), ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (task_id, step_index, type, key, value, path, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    -1,
                    artifact_type,
                    description,
                    metadata_text,
                    path,
                    _now(),
                ),
            )
            conn.commit()

    def search_artifacts(
        self,
        query: str,
        task_id: Optional[str] = None,
        limit: int = 8,
    ) -> List[ArtifactHit]:
        query = query.strip().lower()
        params: List[Any] = []
        filters: List[str] = []
        if task_id:
            filters.append("task_id = ?")
            params.append(task_id)
        sql = "SELECT * FROM artifacts"
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY id DESC LIMIT 500"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        terms = self._terms(query)
        hits: List[ArtifactHit] = []
        for row in rows:
            haystack = " ".join(
                str(row[key] or "") for key in ("task_id", "type", "key", "value", "path")
            ).lower()
            score = sum(1 for term in terms if term in haystack) if terms else 1
            if score:
                hits.append(self._from_row(row, float(score)))
        hits.sort(key=lambda hit: (hit.score, hit.id), reverse=True)
        return hits[:limit]

    def format_hits(self, hits: Sequence[ArtifactHit], title: str = "Artifact Search") -> str:
        if not hits:
            return "No matching artifacts found."
        parts = [f"### {title}"]
        for hit in hits:
            parts.append(
                f"\n[{hit.id}] {hit.timestamp} | {hit.task_id} | {hit.artifact_type}\n"
                f"{hit.key}: {hit.path or hit.value}"
            )
        return "\n".join(parts)

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        return {"artifacts": total, "db_path": str(self.db_path)}

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    step_index INTEGER,
                    type TEXT,
                    key TEXT,
                    value TEXT,
                    path TEXT,
                    timestamp TEXT
                )
                """
            )
            conn.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row, score: float) -> ArtifactHit:
        return ArtifactHit(
            id=int(row["id"]),
            task_id=str(row["task_id"] or ""),
            artifact_type=str(row["type"] or ""),
            key=str(row["key"] or ""),
            value=str(row["value"] or ""),
            path=str(row["path"] or ""),
            timestamp=str(row["timestamp"] or ""),
            score=score,
        )

    @staticmethod
    def _terms(query: str) -> List[str]:
        if not query:
            return []
        terms = query_terms(query)
        terms.extend(part for part in query.replace("\\", "/").split("/") if len(part) >= 2)
        terms.extend(part for part in query.split() if len(part) >= 2)
        seen: set[str] = set()
        ordered: List[str] = []
        for term in terms:
            if term and term not in seen:
                seen.add(term)
                ordered.append(term)
        return ordered

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
