from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .patch_validator import redact_secrets, sanitize_value
from .session_archive import ArchiveHit, SessionArchive
from .utils import clip_text, now_iso, safe_id


TASK_SECTIONS = [
    "Goal",
    "Current Status",
    "Key Events",
    "Commands and Results",
    "Errors and Fixes",
    "Artifacts",
    "Curated Claims",
    "Open Questions",
]


class TaskSummaryRenderer:
    """Render human-readable task reports from SessionArchive and optional curated claims."""

    def __init__(
        self,
        memory_dir: str | Path,
        session_archive: SessionArchive,
        wiki_memory_manager: Any = None,
        max_task_chars: int = 10_000,
        max_search_candidates: int = 8,
    ):
        self.root = Path(memory_dir)
        self.tasks_dir = self.root / "tasks"
        self.task_index = self.root / "TASKS.md"
        self.session_archive = session_archive
        self.wiki_memory_manager = wiki_memory_manager
        self.max_task_chars = max_task_chars
        self.max_search_candidates = max_search_candidates
        self.ensure_layout()

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        if not self.task_index.exists():
            self.task_index.write_text(self._initial_task_index(), encoding="utf-8")

    @staticmethod
    def _initial_task_index() -> str:
        return (
            "# BioMind Task Summary Index\n\n"
            "Derived from SessionArchive events and optional curated claims. Deleting task summary files does not delete memory.\n\n"
            "| Task ID | Status | Updated | Goal | Summary |\n"
            "| --- | --- | --- | --- | --- |\n"
        )

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{safe_id(task_id)}.md"

    def task_exists(self, task_id: str) -> bool:
        if self.task_path(task_id).exists():
            return True
        return bool(self.session_archive.events_for_task(task_id, limit=1))

    def initialize_task(
        self,
        task_id: str,
        goal: str = "",
        status: str = "running",
        *,
        authoritative_new_task: bool = False,
    ) -> None:
        """Initialize one task from a single request-scoped archive snapshot.

        A newly accepted task is authoritative evidence that no historical
        lookup is needed.  Resume paths take one snapshot and reuse it for the
        report and index instead of issuing duplicate queries.
        """
        task_id = safe_id(task_id)
        events = (
            []
            if authoritative_new_task
            else self.session_archive.events_for_task(
                task_id,
                limit=300,
                ascending=False,
            )
        )
        if not authoritative_new_task: events = list(reversed(events))
        if not self.task_path(task_id).exists():
            self.write_task_summary(
                task_id,
                fallback_goal=goal,
                fallback_status=status,
                events_snapshot=events,
            )
        else:
            self._upsert_task_index(
                task_id,
                status=self._task_status(events, status),
                goal=self._task_goal(events, goal),
                summary=self._summary(events),
            )

    def read_task_summary(self, task_id: str, max_chars: Optional[int] = None) -> str:
        path = self.task_path(task_id)
        if path.exists():
            return clip_text(path.read_text(encoding="utf-8"), max_chars or self.max_task_chars)
        return clip_text(self.render_task_summary(task_id), max_chars or self.max_task_chars)

    def read_task_memory(self, task_id: str, max_chars: Optional[int] = None) -> str:
        return self.read_task_summary(task_id, max_chars=max_chars)

    def render_task_summary(
        self,
        task_id: str,
        fallback_goal: str = "",
        fallback_status: str = "running",
        *,
        events_snapshot: Optional[Sequence[ArchiveHit]] = None,
    ) -> str:
        task_id = safe_id(task_id)
        events = list(events_snapshot) if events_snapshot is not None else list(
            self.session_archive.events_for_task(
                task_id,
                limit=300,
                ascending=False,
            )
        )
        if events_snapshot is None: events.reverse()
        status = self._task_status(events, fallback_status)
        goal = self._task_goal(events, fallback_goal)
        updated = events[-1].timestamp if events else now_iso()
        claims = self._claims_for_events(events)
        lines = [
            f"# Task Summary: {task_id}",
            "",
            "Snapshot of committed SessionArchive events (latest 300) and optional curated claims. Pending observer writes may not yet appear; this report is not the memory source of truth.",
            "",
            f"- Updated: {updated}",
            f"- Status: {status}",
            f"- Event count: {len(events)}",
            "",
            "## Goal",
            goal or "- None recorded yet.",
            "",
            "## Current Status",
            self._current_status(events, status),
            "",
            "## Key Events",
            self._event_bullets(events),
            "",
            "## Commands and Results",
            self._command_bullets(events),
            "",
            "## Errors and Fixes",
            self._error_bullets(events),
            "",
            "## Artifacts",
            self._artifact_bullets(events),
            "",
            "## Curated Claims",
            self._claim_bullets(claims),
            "",
            "## Open Questions",
            self._open_question_bullets(events),
            "",
            "## Source Events",
            self._source_event_table(events),
            "",
        ]
        return "\n".join(lines)

    def write_task_summary(
        self,
        task_id: str,
        fallback_goal: str = "",
        fallback_status: str = "running",
        *,
        events_snapshot: Optional[Sequence[ArchiveHit]] = None,
    ) -> Path:
        task_id = safe_id(task_id)
        events = list(events_snapshot) if events_snapshot is not None else list(
            self.session_archive.events_for_task(
                task_id,
                limit=300,
                ascending=False,
            )
        )
        if events_snapshot is None: events.reverse()
        text = self.render_task_summary(
            task_id,
            fallback_goal=fallback_goal,
            fallback_status=fallback_status,
            events_snapshot=events,
        )
        path = self.task_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._upsert_task_index(
            task_id,
            status=self._task_status(events, fallback_status),
            goal=self._task_goal(events, fallback_goal),
            summary=self._summary(events),
        )
        return path

    async def build_prompt_context_async(self, task_id: str, query: str = "") -> Dict[str, str]:
        related = ""
        if query.strip():
            hits = self.session_archive.search(query, task_id=task_id, limit=4)
            related = self.session_archive.format_hits(hits, title="Retrieved Related Session Events")
        return {
            "long_term_memory": "",
            "task_memory": self.read_task_summary(task_id),
            "related_memory": related,
        }

    async def asearch(self, query: str, task_id: Optional[str] = None, top_k: int = 6) -> str:
        hits = self.session_archive.search(query, task_id=task_id, limit=top_k)
        return self.session_archive.format_hits(hits, title=f"Session Archive Search | {query}")

    def status_text(self, task_id: Optional[str] = None) -> str:
        stats = self.session_archive.stats()
        lines = [
            f"Task summary root: {self.root}",
            f"Task summaries: {self.tasks_dir}",
            f"Task index: {self.task_index}",
            f"SessionArchive: {stats.get('db_path')}",
            f"Archive events: {stats.get('events')} | tasks: {stats.get('tasks')} | wiki pending: {stats.get('wiki_pending')}",
        ]
        if task_id:
            lines.append(f"Task summary: {self.task_path(task_id)}")
        return "\n".join(lines)

    def _task_status(self, events: Sequence[ArchiveHit], fallback: str) -> str:
        for event in reversed(events):
            status = str(event.metadata.get("status") or "").strip().lower()
            if event.event_type == "task_finalized" and status:
                return status
            if event.stage == "finalize" and status:
                return status
        if any(event.event_type == "execution_finished" and event.returncode not in (None, 0) for event in events):
            return "failed"
        return fallback if events else "not_started"

    def _task_goal(self, events: Sequence[ArchiveHit], fallback: str) -> str:
        for event in events:
            for key in ("goal", "user_query", "prompt", "request"):
                value = event.metadata.get(key)
                if str(value or "").strip():
                    return clip_text(redact_secrets(value), 600)
        for event in events:
            if event.event_type == "user_message" or "HumanMessage" in event.content:
                return clip_text(event.summary or event.content, 600)
        return fallback.strip()

    def _current_status(self, events: Sequence[ArchiveHit], status: str) -> str:
        if not events:
            return f"- {status}"
        latest = events[-1]
        summary = latest.summary.strip() or latest.content.strip() or latest.event_type
        return f"- {status}: {clip_text(summary, 800)}"

    def _event_bullets(self, events: Sequence[ArchiveHit]) -> str:
        if not events:
            return "- None recorded yet."
        selected = self._select_key_events(events)
        return "\n".join(
            f"- `{event.id}` {event.timestamp} `{event.stage}/{event.event_type}`: {clip_text(event.summary or event.content, 500)}"
            for event in selected
        )

    def _select_key_events(self, events: Sequence[ArchiveHit]) -> List[ArchiveHit]:
        priorities = {
            "user_message",
            "plan_created",
            "execution_finished",
            "artifact_created",
            "supervisor_verdict",
            "task_finalized",
            "error_detected",
        }
        selected = [event for event in events if event.event_type in priorities or event.stage in {"planner", "execute", "supervisor", "finalize"}]
        if len(selected) < 8:
            selected = list(events)
        return selected[-18:]

    def _command_bullets(self, events: Sequence[ArchiveHit]) -> str:
        bullets: List[str] = []
        for event in events:
            if event.event_type != "execution_finished" and event.stage != "execute":
                continue
            command = event.metadata.get("command") or event.metadata.get("code") or event.metadata.get("script_file") or event.summary
            result = event.metadata.get("returncode", event.returncode)
            bullets.append(f"- `{event.id}` {clip_text(command, 260)} -> returncode `{result}`")
        return "\n".join(bullets[-20:]) if bullets else "- None recorded yet."

    def _error_bullets(self, events: Sequence[ArchiveHit]) -> str:
        bullets: List[str] = []
        for event in events:
            is_error = event.event_type == "error_detected" or event.returncode not in (None, 0)
            if not is_error:
                continue
            stderr = event.metadata.get("stderr") or event.metadata.get("error") or event.summary or event.content
            bullets.append(f"- `{event.id}` {clip_text(stderr, 500)}")
        return "\n".join(bullets[-12:]) if bullets else "- None recorded yet."

    def _artifact_bullets(self, events: Sequence[ArchiveHit]) -> str:
        artifacts: List[str] = []
        for event in events:
            artifacts.extend(event.paths)
            artifacts.extend(str(item) for item in event.task_semantics.get("output_artifacts", []) if str(item).strip())
            artifacts.extend(self._metadata_values(event.metadata, {"artifact", "artifacts", "output_path", "output_paths", "output_file", "plan_file", "script_file"}))
        artifacts = self._dedupe(artifacts)
        return "\n".join(f"- {item}" for item in artifacts[:40]) if artifacts else "- None recorded yet."

    def _claim_bullets(self, claims: Sequence[Dict[str, Any]]) -> str:
        if not claims:
            return "- No curated claims currently cite this task's SessionArchive events."
        lines = []
        for claim in claims[:20]:
            evidence = ", ".join(str(item) for item in claim.get("evidence", [])[:3])
            lines.append(
                f"- {clip_text(claim.get('text', ''), 400)} "
                f"(page: {claim.get('page', 'unknown')}; evidence: {evidence})"
            )
        return "\n".join(lines)

    def _open_question_bullets(self, events: Sequence[ArchiveHit]) -> str:
        questions: List[str] = []
        for event in events:
            questions.extend(self._metadata_values(event.metadata, {"open_questions", "question", "questions"}))
        questions = self._dedupe(questions)
        return "\n".join(f"- {item}" for item in questions[:12]) if questions else "- None recorded yet."

    def _source_event_table(self, events: Sequence[ArchiveHit]) -> str:
        if not events:
            return "| ID | Time | Stage/Event | Summary |\n| --- | --- | --- | --- |\n"
        lines = ["| ID | Time | Stage/Event | Summary |", "| --- | --- | --- | --- |"]
        for event in events[-40:]:
            summary = clip_text(event.summary or event.content, 180).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{event.id}` | {event.timestamp} | `{event.stage}/{event.event_type}` | {summary} |")
        return "\n".join(lines)

    def _claims_for_events(self, events: Sequence[ArchiveHit]) -> List[Dict[str, Any]]:
        if not self.wiki_memory_manager or not events:
            return []
        event_refs = {event.evidence_ref for event in events}
        try:
            claims = self.wiki_memory_manager.read_claims_index(events[-1].project_id)
        except Exception:
            return []
        matched = []
        for claim in claims:
            evidence = claim.get("evidence") or []
            refs = {self._evidence_ref(item) for item in evidence}
            if refs & event_refs:
                matched.append(claim)
        return matched

    @staticmethod
    def _evidence_ref(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("source_id") or item.get("source") or item.get("ref") or "")
        return str(item)

    def _summary(self, events: Sequence[ArchiveHit]) -> str:
        if not events:
            return "No events recorded."
        return clip_text(events[-1].summary or events[-1].content or events[-1].event_type, 240)

    def _upsert_task_index(self, task_id: str, status: str, goal: str = "", summary: str = "") -> None:
        task_id = safe_id(task_id)
        rows: List[str] = []
        existing = self.task_index.read_text(encoding="utf-8") if self.task_index.exists() else self._initial_task_index()
        for line in existing.splitlines():
            if not line.startswith("| ") or line.startswith("| Task ID ") or line.startswith("| --- "):
                rows.append(line)
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and cells[0] == task_id:
                continue
            rows.append(line)
        row = "| " + " | ".join([
            task_id,
            status,
            now_iso(),
            self._table_cell(goal),
            self._table_cell(summary),
        ]) + " |"
        rows.append(row)
        self.task_index.write_text("\n".join(rows).rstrip() + "\n", encoding="utf-8")

    @staticmethod
    def _metadata_values(value: Any, keys: set[str]) -> List[str]:
        found: List[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) in keys:
                    found.extend(TaskSummaryRenderer._flatten_values(item))
                elif isinstance(item, (dict, list)):
                    found.extend(TaskSummaryRenderer._metadata_values(item, keys))
        elif isinstance(value, list):
            for item in value:
                found.extend(TaskSummaryRenderer._metadata_values(item, keys))
        return found

    @staticmethod
    def _flatten_values(value: Any) -> List[str]:
        if isinstance(value, dict):
            if value.get("type") == "large_text_ref":
                return [str(value.get("path") or value.get("preview") or "large_text_ref")]
            return [json.dumps(sanitize_value(value), ensure_ascii=False, default=str)]
        if isinstance(value, (list, tuple, set)):
            result: List[str] = []
            for item in value:
                result.extend(TaskSummaryRenderer._flatten_values(item))
            return result
        text = redact_secrets(value).strip()
        return [text] if text else []

    @staticmethod
    def _dedupe(items: Iterable[Any]) -> List[str]:
        seen: set[str] = set()
        result: List[str] = []
        for item in items:
            text = redact_secrets(item).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _table_cell(value: Any) -> str:
        text = clip_text(redact_secrets(value), 120).replace("|", "\\|").replace("\n", " ")
        return text or "-"
