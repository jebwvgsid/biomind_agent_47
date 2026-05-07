from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .patch_validator import DreamPatchValidator
from .utils import DEFAULT_PROJECT_ID, clip_text, extract_json_object, normalize_list, now_iso, safe_id

try:
    from langchain_core.messages import HumanMessage
except ImportError:
    class HumanMessage:  # type: ignore[no-redef]
        def __init__(self, content: str):
            self.content = content

logger = logging.getLogger("BioMind.MarkdownMemory")


LONG_TERM_SECTIONS = [
    "User Preferences",
    "Project Facts",
    "Important Paths",
    "Reusable Fixes",
    "Important Decisions",
    "Deprecated / Superseded",
]

TASK_SECTIONS = [
    "Goal",
    "Current Status",
    "Key Files and Directories",
    "Commands and Results",
    "Errors and Fixes",
    "Artifacts",
    "Open Questions",
    "Timeline",
]


@dataclass
class MemoryFiles:
    root: Path
    long_term: Path
    task_index: Path
    tasks_dir: Path
    logs_dir: Path
    history_dir: Path
    dreams_dir: Path
    snapshots_dir: Path
    pending_events: Path
    dream_log: Path
    latest_snapshot: Path


def _now() -> str:
    return now_iso()


def _safe_task_id(task_id: str) -> str:
    return safe_id(task_id)


def _content_of(message: Any) -> str:
    return str(getattr(message, "content", message))


def _message_role(message: Any) -> str:
    name = type(message).__name__.replace("Message", "")
    return name or "Message"


def _clip(text: str, limit: int) -> str:
    return clip_text(text, limit)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    return extract_json_object(text)


def _normalize_bullets(value: Any) -> List[str]:
    return normalize_list(value, strip_bullets=True)


class MarkdownMemoryManager:
    """Nanobot-style Markdown memory managed by LLM-generated patches."""

    def __init__(
        self,
        llm: Any,
        memory_dir: str | Path,
        max_long_term_chars: int = 12_000,
        max_task_chars: int = 10_000,
        max_log_event_chars: int = 4_000,
        max_search_candidates: int = 8,
        core_store: Any = None,
        core_project_id: str = DEFAULT_PROJECT_ID,
    ):
        root = Path(memory_dir)
        self.files = MemoryFiles(
            root=root,
            long_term=root / "MEMORY.md",
            task_index=root / "TASKS.md",
            tasks_dir=root / "tasks",
            logs_dir=root / "logs",
            history_dir=root / "history",
            dreams_dir=root / "dreams",
            snapshots_dir=root / "dreams" / "snapshots",
            pending_events=root / "history" / "PENDING.md",
            dream_log=root / "dreams" / "DREAM_LOG.md",
            latest_snapshot=root / "dreams" / "LATEST_SNAPSHOT.txt",
        )
        self.llm = llm
        self.max_long_term_chars = max_long_term_chars
        self.max_task_chars = max_task_chars
        self.max_log_event_chars = max_log_event_chars
        self.max_search_candidates = max_search_candidates
        self.core_store = core_store
        self.core_project_id = safe_id(core_project_id)
        self.patch_validator = DreamPatchValidator(core_store) if core_store is not None else None
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.files.root.mkdir(parents=True, exist_ok=True)
        self.files.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.files.logs_dir.mkdir(parents=True, exist_ok=True)
        self.files.history_dir.mkdir(parents=True, exist_ok=True)
        self.files.dreams_dir.mkdir(parents=True, exist_ok=True)
        self.files.snapshots_dir.mkdir(parents=True, exist_ok=True)
        if not self.files.long_term.exists():
            self.files.long_term.write_text(self._initial_long_term(), encoding="utf-8")
        if not self.files.task_index.exists():
            self.files.task_index.write_text(self._initial_task_index(), encoding="utf-8")
        if not self.files.pending_events.exists():
            self.files.pending_events.write_text("# Pending Memory Events\n", encoding="utf-8")
        if not self.files.dream_log.exists():
            self.files.dream_log.write_text("# Dream Log\n", encoding="utf-8")

    @staticmethod
    def _initial_long_term() -> str:
        sections = "\n\n".join(f"## {name}\n- None recorded yet." for name in LONG_TERM_SECTIONS)
        return (
            "# BioMind Long-term Memory\n\n"
            "This file is maintained by the memory curator. Current user input, "
            "verified filesystem state, and latest observations override stale memory.\n\n"
            f"{sections}\n"
        )

    @staticmethod
    def _initial_task_index() -> str:
        return (
            "# BioMind Task Index\n\n"
            "| Task ID | Status | Updated | Goal | Summary |\n"
            "| --- | --- | --- | --- | --- |\n"
        )

    def task_path(self, task_id: str) -> Path:
        return self.files.tasks_dir / f"{_safe_task_id(task_id)}.md"

    def log_path(self, task_id: str) -> Path:
        return self.files.logs_dir / f"{_safe_task_id(task_id)}.md"

    def task_exists(self, task_id: str) -> bool:
        return self.task_path(task_id).exists()

    def initialize_task(self, task_id: str, goal: str = "", status: str = "running") -> None:
        task_path = self.task_path(task_id)
        if not task_path.exists():
            sections = "\n\n".join(
                f"## {name}\n{goal.strip() if name == 'Goal' and goal.strip() else '- None recorded yet.'}"
                for name in TASK_SECTIONS
            )
            task_path.write_text(
                f"# Task Memory: {_safe_task_id(task_id)}\n\n"
                f"Created: {_now()}\n"
                f"Status: {status}\n\n"
                f"{sections}\n",
                encoding="utf-8",
            )
        if not self.log_path(task_id).exists():
            self.log_path(task_id).write_text(
                f"# Full Process Log: {_safe_task_id(task_id)}\n\nCreated: {_now()}\n",
                encoding="utf-8",
            )
        self._upsert_task_index(task_id, status=status, goal=goal)

    def read_long_term(self, max_chars: Optional[int] = None) -> str:
        return _clip(self.files.long_term.read_text(encoding="utf-8"), max_chars or self.max_long_term_chars)

    def read_task_memory(self, task_id: str, max_chars: Optional[int] = None) -> str:
        path = self.task_path(task_id)
        if not path.exists():
            return ""
        return _clip(path.read_text(encoding="utf-8"), max_chars or self.max_task_chars)

    def build_prompt_context(self, task_id: str, query: str = "") -> Dict[str, str]:
        related = self.lexical_retrieve_context(task_id, query) if query else ""
        long_term = self.lexical_retrieve_long_term(query) if query else ""
        return {
            "long_term_memory": long_term,
            "task_memory": self.read_task_memory(task_id),
            "related_memory": related,
        }

    async def build_prompt_context_async(self, task_id: str, query: str = "") -> Dict[str, str]:
        related = await self.aretrieve_context(task_id, query) if query else ""
        long_term = await self.aretrieve_long_term(query) if query else ""
        return {
            "long_term_memory": long_term,
            "task_memory": self.read_task_memory(task_id),
            "related_memory": related,
        }

    def append_event(
        self,
        task_id: str,
        stage: str,
        messages: Optional[List[Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Fast hot-path memory write: append event logs without calling the LLM."""
        self.initialize_task(task_id)
        payload = payload or {}
        messages = messages or []
        event_text = self._format_event(stage, messages, payload)
        event_id = f"{_safe_task_id(task_id)}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        self._append_log(task_id, stage, event_text)
        self._append_pending_event(event_id, task_id, stage, event_text)
        return event_id

    async def observe_event(
        self,
        task_id: str,
        stage: str,
        messages: Optional[List[Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.append_event(task_id, stage, messages=messages, payload=payload)

    async def finalize_task(
        self,
        task_id: str,
        status: str = "completed",
        messages: Optional[List[Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        final_payload = {"status": status}
        if payload:
            final_payload.update(payload)
        self.append_event(task_id, "finalize", messages=messages, payload=final_payload)
        self._set_task_status(task_id, status)
        self._upsert_task_index(task_id, status=status, goal=self._task_goal(task_id))

    def apply_patch(self, task_id: str, patch: Dict[str, Any]) -> None:
        long_term = patch.get("long_term", {})
        if isinstance(long_term, dict):
            for section, items in long_term.items():
                if section in LONG_TERM_SECTIONS:
                    self._append_section_bullets(self.files.long_term, section, _normalize_bullets(items))

        task_patch = patch.get("task", {})
        if isinstance(task_patch, dict):
            has_task_updates = False
            for section, value in task_patch.items():
                if section in {"Goal", "Current Status"}:
                    has_task_updates = bool(str(value).strip())
                else:
                    has_task_updates = bool(_normalize_bullets(value))
                if has_task_updates:
                    break
            if has_task_updates:
                self.initialize_task(task_id)
            task_file = self.task_path(task_id)
            for section, value in task_patch.items():
                if section not in TASK_SECTIONS:
                    continue
                if section in {"Goal", "Current Status"}:
                    text = str(value).strip() if value is not None else ""
                    if text and text.lower() not in {"none", "n/a", "no update"}:
                        self._replace_section_body(task_file, section, text)
                else:
                    self._append_section_bullets(task_file, section, _normalize_bullets(value))

        task_status = str(patch.get("task_status", "")).strip().lower()
        if task_status in {"running", "completed", "cancelled", "interrupted", "failed"}:
            self._set_task_status(task_id, task_status)
            self._upsert_task_index(task_id, status=task_status, goal=self._task_goal(task_id))

    async def curate_pending(self, task_id: Optional[str] = None, limit_chars: int = 14_000) -> str:
        """Run the Dream layer: batch-curate pending events into Markdown memory."""
        pending_text = self.files.pending_events.read_text(encoding="utf-8")
        if not self._pending_has_events(pending_text):
            return "No pending memory events to dream about."

        pending_excerpt = _clip(pending_text, limit_chars)
        task_ids = self._task_ids_from_pending(pending_excerpt)
        default_task_id = task_id or (task_ids[0] if task_ids else "default")
        snapshot_dir = self._snapshot_memory(task_ids or [default_task_id])
        prompt = self._dream_prompt(pending_excerpt, task_ids, default_task_id)

        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
        except Exception as exc:
            logger.warning("Markdown memory dream failed: %s", exc)
            return f"Dream failed: {exc}"

        patch = _extract_json_object(_content_of(response))
        if not patch:
            return "Dream failed: curator returned invalid JSON."
        if self.patch_validator:
            patch = self.patch_validator.sanitize_patch(patch)
            warnings = self.patch_validator.validate_patch(patch)
            for warning in warnings:
                logger.warning("Markdown memory dream patch warning: %s", warning)

        self._apply_dream_patch(default_task_id, patch)
        self._apply_core_memory_patch(patch)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        processed = self.files.history_dir / f"processed_{ts}.md"
        processed.write_text(pending_text, encoding="utf-8")
        self.files.pending_events.write_text("# Pending Memory Events\n", encoding="utf-8")

        summary = str(patch.get("dream_summary", "") or patch.get("log_summary", "") or "Memory dream completed.")
        self._append_dream_log(summary, snapshot_dir, processed)
        return (
            f"Dream completed.\n"
            f"Summary: {summary}\n"
            f"Snapshot: {snapshot_dir}\n"
            f"Processed events: {processed}"
        )

    def read_dream_log(self, max_chars: int = 12_000) -> str:
        return _clip(self.files.dream_log.read_text(encoding="utf-8"), max_chars)

    def restore_latest_snapshot(self) -> str:
        if not self.files.latest_snapshot.exists():
            return "No memory snapshot recorded."
        snapshot_dir = Path(self.files.latest_snapshot.read_text(encoding="utf-8").strip())
        if not snapshot_dir.exists():
            return f"Snapshot not found: {snapshot_dir}"

        memory_snapshot = snapshot_dir / "MEMORY.md"
        task_index_snapshot = snapshot_dir / "TASKS.md"
        tasks_snapshot = snapshot_dir / "tasks"
        if memory_snapshot.exists():
            shutil.copy2(memory_snapshot, self.files.long_term)
        if task_index_snapshot.exists():
            shutil.copy2(task_index_snapshot, self.files.task_index)
        if self.core_store:
            for name in ("USER_PROFILE.md", "AGENT_MEMORY.md"):
                source = snapshot_dir / name
                if source.exists():
                    shutil.copy2(source, self.files.root / name)
            projects_snapshot = snapshot_dir / "projects"
            if projects_snapshot.exists():
                shutil.copytree(projects_snapshot, self.files.root / "projects", dirs_exist_ok=True)
        if tasks_snapshot.exists():
            for source in tasks_snapshot.glob("*.md"):
                shutil.copy2(source, self.files.tasks_dir / source.name)

        message = f"Restored Markdown memory snapshot: {snapshot_dir}"
        self._append_dream_log(message, snapshot_dir, None)
        return message

    def lexical_retrieve_long_term(self, query: str, top_k: int = 3) -> str:
        candidates = self._find_candidates(
            query,
            task_id=None,
            include_long_term=True,
            include_current_task=False,
            include_all_tasks=False,
        )
        return self._format_retrieved("### Retrieved Long-term Markdown Memory", candidates[:top_k])

    async def aretrieve_long_term(self, query: str, top_k: int = 3) -> str:
        candidates = self._find_candidates(
            query,
            task_id=None,
            include_long_term=True,
            include_current_task=False,
            include_all_tasks=False,
        )
        if not candidates:
            return ""
        selected = await self._llm_rerank(query, candidates[: max(top_k, self.max_search_candidates)], top_k)
        return self._format_retrieved("### Retrieved Long-term Markdown Memory", selected or candidates[:top_k])

    def lexical_retrieve_context(self, task_id: str, query: str, top_k: int = 4) -> str:
        query = query.strip()
        candidates = self._find_candidates(
            query,
            task_id,
            include_long_term=False,
            include_current_task=False,
            include_all_tasks=True,
        )
        if not candidates:
            return ""
        return self._format_retrieved("### Retrieved Related Task Memory", candidates[:top_k])

    def retrieve_context(self, task_id: str, query: str, top_k: int = 4) -> str:
        """Backward-compatible lexical fallback. Prefer aretrieve_context()."""
        return self.lexical_retrieve_context(task_id, query, top_k=top_k)

    async def aretrieve_context(self, task_id: str, query: str, top_k: int = 4) -> str:
        query = query.strip()
        candidates = self._find_candidates(
            query,
            task_id,
            include_long_term=False,
            include_current_task=False,
            include_all_tasks=True,
        )
        if not candidates:
            return ""
        candidates = candidates[: max(top_k, self.max_search_candidates)]
        selected = await self._llm_rerank(query, candidates, top_k=top_k)
        if not selected:
            selected = candidates[:top_k]
        return self._format_retrieved("### Retrieved Related Task Memory", selected[:top_k])

    def search(self, query: str, task_id: Optional[str] = None, top_k: int = 6) -> str:
        candidates = self._find_candidates(query, task_id)
        if not candidates:
            return "No matching Markdown memory found."
        return self._format_search(candidates[:top_k])

    async def asearch(self, query: str, task_id: Optional[str] = None, top_k: int = 6) -> str:
        candidates = self._find_candidates(query, task_id)
        if not candidates:
            return "No matching Markdown memory found."
        candidates = candidates[: max(top_k, self.max_search_candidates)]
        selected = await self._llm_rerank(query, candidates, top_k=top_k)
        if not selected:
            selected = candidates[:top_k]
        return self._format_search(selected[:top_k])

    def status_text(self, task_id: Optional[str] = None) -> str:
        lines = [
            f"Markdown memory root: {self.files.root}",
            f"Long-term memory: {self.files.long_term}",
            f"Task index: {self.files.task_index}",
            f"Pending events: {self.files.pending_events}",
            f"Dream log: {self.files.dream_log}",
        ]
        if task_id:
            lines.append(f"Task memory: {self.task_path(task_id)}")
            lines.append(f"Task log: {self.log_path(task_id)}")
        return "\n".join(lines)

    def _format_event(self, stage: str, messages: List[Any], payload: Dict[str, Any]) -> str:
        recent = messages[-6:] if messages else []
        message_lines = [
            f"[{_message_role(message)}] {_clip(_content_of(message), 1200)}"
            for message in recent
        ]
        payload_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return (
            f"Stage: {stage}\n"
            f"Timestamp: {_now()}\n\n"
            f"Payload:\n{_clip(payload_text, 2500)}\n\n"
            f"Recent external messages:\n" + ("\n\n".join(message_lines) if message_lines else "None")
        )

    def _append_log(self, task_id: str, stage: str, event_text: str) -> None:
        path = self.log_path(task_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## {_now()} - {stage}\n\n")
            handle.write("```text\n")
            handle.write(_clip(event_text, self.max_log_event_chars))
            handle.write("\n```\n")

    def _append_pending_event(self, event_id: str, task_id: str, stage: str, event_text: str) -> None:
        with self.files.pending_events.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n\n## Event {event_id}\n\n"
                f"- task_id: {_safe_task_id(task_id)}\n"
                f"- stage: {stage}\n"
                f"- timestamp: {_now()}\n\n"
                "```text\n"
            )
            handle.write(_clip(event_text, self.max_log_event_chars))
            handle.write("\n```\n")

    @staticmethod
    def _pending_has_events(text: str) -> bool:
        return bool(re.search(r"^## Event ", text, flags=re.MULTILINE))

    @staticmethod
    def _task_ids_from_pending(text: str) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        for match in re.finditer(r"^- task_id:\s*(.+?)\s*$", text, flags=re.MULTILINE):
            task_id = _safe_task_id(match.group(1).strip())
            if task_id not in seen:
                seen.add(task_id)
                ordered.append(task_id)
        return ordered

    def _dream_prompt(self, pending_text: str, task_ids: List[str], default_task_id: str) -> str:
        task_contexts = []
        for tid in task_ids[:6]:
            task_contexts.append(f"### {tid}\n{self.read_task_memory(tid, 2500)}")
        return f"""You are BioMind's Dream memory curator.
Batch-process pending external events into Markdown memory. Do not infer hidden reasoning.

Rules:
- Long-term memory is only reusable facts, user preferences, durable paths, fixes, and decisions.
- Task memory is an auditable summary: goal, current status, key files, commands/results, errors/fixes, artifacts, open questions.
- Full logs already exist; do not copy noisy details.
- Current user input, verified filesystem state, and latest observations override stale memory.
- Skills are procedural knowledge; memory is factual knowledge.
- Do not store long workflows, SOPs, script templates, or step-by-step methods as durable memory.
- If a reusable procedure appears, summarize it in task memory and mention a concise skill_suggestion instead of writing the procedure into long-term/core memory.

Return JSON only:
{{
  "user_profile": {{
    "Preferences": [],
    "Communication": [],
    "Workflow": []
  }},
  "agent_memory": {{
    "Environment Facts": [],
    "Operational Notes": [],
    "Project Notes": [],
    "Reusable Fixes": [],
    "Important Decisions": [],
    "Deprecated / Superseded": []
  }},
  "projects": {{
    "{self.core_project_id}": {{}}
  }},
  "long_term": {{
    "User Preferences": [],
    "Project Facts": [],
    "Important Paths": [],
    "Reusable Fixes": [],
    "Important Decisions": [],
    "Deprecated / Superseded": []
  }},
  "tasks": {{
    "{default_task_id}": {{
      "Goal": "",
      "Current Status": "",
      "Key Files and Directories": [],
      "Commands and Results": [],
      "Errors and Fixes": [],
      "Artifacts": [],
      "Open Questions": []
    }}
  }},
  "task_status": {{"{default_task_id}": "running"}},
  "skill_suggestions": [],
  "dream_summary": ""
}}

Known task IDs: {", ".join(task_ids) if task_ids else default_task_id}

Current long-term memory excerpt:
{self.read_long_term(3500)}

Current task memory excerpts:
{chr(10).join(task_contexts) if task_contexts else self.read_task_memory(default_task_id, 2500)}

Pending events:
{pending_text}
"""

    def _apply_dream_patch(self, default_task_id: str, patch: Dict[str, Any]) -> None:
        self.apply_patch(default_task_id, patch)

        tasks = patch.get("tasks", {})
        if isinstance(tasks, dict):
            for raw_task_id, task_patch in tasks.items():
                task_id = _safe_task_id(str(raw_task_id))
                if isinstance(task_patch, dict):
                    self.initialize_task(task_id)
                    self.apply_patch(task_id, {"task": task_patch})

        statuses = patch.get("task_status", {})
        if isinstance(statuses, dict):
            for raw_task_id, status in statuses.items():
                status_text = str(status).strip().lower()
                if status_text in {"running", "completed", "cancelled", "interrupted", "failed"}:
                    task_id = _safe_task_id(str(raw_task_id))
                    self.initialize_task(task_id)
                    self._set_task_status(task_id, status_text)
                    self._upsert_task_index(task_id, status=status_text, goal=self._task_goal(task_id))

    def _apply_core_memory_patch(self, patch: Dict[str, Any]) -> None:
        if not self.core_store:
            return
        try:
            if self.patch_validator:
                conflicts = self.patch_validator.detect_conflicts(patch)
                for conflict in conflicts:
                    logger.warning("Core memory dream conflict: %s", conflict)
            self.core_store.apply_patch(patch, default_project_id=self.core_project_id)
        except Exception as exc:
            logger.warning("Core memory dream apply failed: %s", exc)

    def _snapshot_memory(self, task_ids: Iterable[str]) -> Path:
        snapshot_dir = self.files.snapshots_dir / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_tasks = snapshot_dir / "tasks"
        snapshot_tasks.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.files.long_term, snapshot_dir / "MEMORY.md")
        shutil.copy2(self.files.task_index, snapshot_dir / "TASKS.md")
        if self.core_store:
            for name in ("USER_PROFILE.md", "AGENT_MEMORY.md"):
                source = self.files.root / name
                if source.exists():
                    shutil.copy2(source, snapshot_dir / name)
            projects_source = self.files.root / "projects"
            if projects_source.exists():
                shutil.copytree(projects_source, snapshot_dir / "projects", dirs_exist_ok=True)
        for task_id in task_ids:
            task_path = self.task_path(task_id)
            if task_path.exists():
                shutil.copy2(task_path, snapshot_tasks / task_path.name)
        self.files.latest_snapshot.write_text(str(snapshot_dir), encoding="utf-8")
        return snapshot_dir

    def _append_dream_log(self, summary: str, snapshot_dir: Optional[Path], processed: Optional[Path]) -> None:
        with self.files.dream_log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## {_now()}\n\n")
            handle.write(f"Summary: {summary}\n")
            if snapshot_dir:
                handle.write(f"Snapshot: {snapshot_dir}\n")
            if processed:
                handle.write(f"Processed events: {processed}\n")

    @staticmethod
    def _format_retrieved(title: str, items: List[Dict[str, str]]) -> str:
        if not items:
            return ""
        parts = [title]
        for item in items:
            parts.append(f"\n[{item['source']}]\n{item['text'].strip()}")
        return "\n".join(parts)

    @staticmethod
    def _format_search(items: List[Dict[str, str]]) -> str:
        return "\n\n".join(f"[{item['source']}]\n{item['text'].strip()}" for item in items)

    def _find_candidates(
        self,
        query: str,
        task_id: Optional[str] = None,
        include_long_term: bool = True,
        include_current_task: bool = True,
        include_all_tasks: bool = True,
    ) -> List[Dict[str, str]]:
        query_words = {
            token.lower()
            for token in re.findall(r"[A-Za-z0-9_./:-]{3,}", query)
        }
        files = []
        current_task_path = self.task_path(task_id) if task_id else None
        if include_long_term:
            files.append(self.files.long_term)
        if include_current_task and task_id:
            files.append(self.task_path(task_id))
        if include_all_tasks:
            for path in sorted(self.files.tasks_dir.glob("*.md")):
                if current_task_path and not include_current_task and path == current_task_path:
                    continue
                files.append(path)
        seen_files: set[Path] = set()
        candidates: List[Dict[str, str]] = []
        for path in files:
            if path in seen_files or not path.exists():
                continue
            seen_files.add(path)
            candidates.extend(self._file_candidates(path, query_words))
        candidates.sort(key=lambda item: int(item["score"]), reverse=True)
        return candidates

    def _file_candidates(self, path: Path, query_words: set[str]) -> List[Dict[str, str]]:
        text = path.read_text(encoding="utf-8")
        chunks = self._split_chunks(text)
        candidates: List[Dict[str, str]] = []
        for index, chunk in enumerate(chunks):
            lower = chunk.lower()
            score = sum(1 for word in query_words if word in lower)
            if not query_words and index < 2:
                score = 1
            if score > 0:
                candidates.append({
                    "source": f"{path.name}#{index + 1}",
                    "text": _clip(chunk, 1800),
                    "score": str(score),
                })
        return candidates

    @staticmethod
    def _split_chunks(text: str) -> List[str]:
        matches = list(re.finditer(r"^## .*$", text, flags=re.MULTILINE))
        if not matches:
            return [_clip(text, 1800)] if text.strip() else []
        chunks: List[str] = []
        for idx, match in enumerate(matches):
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            chunk = text[match.start() : end].strip()
            if chunk:
                chunks.append(chunk)
        return chunks

    async def _llm_rerank(self, query: str, candidates: List[Dict[str, str]], top_k: int) -> List[Dict[str, str]]:
        numbered = []
        for idx, item in enumerate(candidates, start=1):
            numbered.append(f"{idx}. [{item['source']}]\n{_clip(item['text'], 900)}")
        prompt = f"""Select the most relevant Markdown memory chunks for the current task.
Return JSON only: {{"selected": [1, 2]}}.
Select at most {top_k} chunks. Return an empty list if none are relevant.

Query:
{query}

Candidates:
{chr(10).join(numbered)}
"""
        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
        except Exception as exc:
            logger.warning("Markdown memory rerank failed: %s", exc)
            return candidates[:top_k]
        data = _extract_json_object(_content_of(response))
        if not data or not isinstance(data.get("selected"), list):
            return candidates[:top_k]
        selected: List[Dict[str, str]] = []
        for raw_index in data["selected"]:
            try:
                index = int(raw_index) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidates):
                selected.append(candidates[index])
        return selected[:top_k]

    def _append_section_bullets(self, path: Path, section: str, bullets: Iterable[str]) -> None:
        bullets = [bullet.strip() for bullet in bullets if bullet and bullet.strip()]
        if not bullets:
            return
        text = path.read_text(encoding="utf-8")
        existing = {line.strip().lower() for line in text.splitlines()}
        new_lines = []
        for bullet in bullets:
            rendered = f"- {bullet}"
            if rendered.lower() not in existing:
                new_lines.append(rendered)
        if not new_lines:
            return
        self._ensure_section(path, section)
        text = path.read_text(encoding="utf-8")
        pattern = self._section_pattern(section)
        match = pattern.search(text)
        if not match:
            return
        body = match.group("body")
        body_lines = [line for line in body.splitlines() if line.strip() != "- None recorded yet."]
        if body_lines and body_lines[-1].strip():
            body_lines.append("")
        body_lines.extend(new_lines)
        replacement = f"## {section}\n" + "\n".join(body_lines).rstrip() + "\n"
        text = text[: match.start()] + replacement + text[match.end() :]
        path.write_text(text, encoding="utf-8")

    def _replace_section_body(self, path: Path, section: str, body: str) -> None:
        self._ensure_section(path, section)
        text = path.read_text(encoding="utf-8")
        pattern = self._section_pattern(section)
        replacement = f"## {section}\n{body.strip()}\n"
        match = pattern.search(text)
        if not match:
            return
        path.write_text(text[: match.start()] + replacement + text[match.end() :], encoding="utf-8")

    @staticmethod
    def _section_pattern(section: str) -> re.Pattern[str]:
        escaped = re.escape(section)
        return re.compile(
            rf"^## {escaped}\n(?P<body>.*?)(?=^## |\Z)",
            flags=re.DOTALL | re.MULTILINE,
        )

    def _ensure_section(self, path: Path, section: str) -> None:
        text = path.read_text(encoding="utf-8")
        if re.search(rf"^## {re.escape(section)}\s*$", text, flags=re.MULTILINE):
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## {section}\n- None recorded yet.\n")

    def _set_task_status(self, task_id: str, status: str) -> None:
        path = self.task_path(task_id)
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        if re.search(r"^Status: .*$", text, flags=re.MULTILINE):
            text = re.sub(r"^Status: .*$", f"Status: {status}", text, count=1, flags=re.MULTILINE)
        else:
            text = text.replace("\n\n", f"\nStatus: {status}\n\n", 1)
        path.write_text(text, encoding="utf-8")

    def _task_goal(self, task_id: str) -> str:
        task = self.read_task_memory(task_id, 4000)
        match = self._section_pattern("Goal").search(task)
        if not match:
            return ""
        goal = match.group("body").strip()
        return "" if goal == "- None recorded yet." else goal.splitlines()[0][:120]

    def _upsert_task_index(self, task_id: str, status: str, goal: str = "") -> None:
        self._ensure_layout()
        safe_id = _safe_task_id(task_id)
        goal = (goal or self._task_goal(task_id) or "").replace("|", "\\|").replace("\n", " ")[:160]
        summary = str(self.task_path(task_id)).replace("|", "\\|")
        line = f"| {safe_id} | {status} | {_now()} | {goal} | {summary} |"
        text = self.files.task_index.read_text(encoding="utf-8")
        rows = text.splitlines()
        prefix = f"| {safe_id} |"
        replaced = False
        for idx, row in enumerate(rows):
            if row.startswith(prefix):
                rows[idx] = line
                replaced = True
                break
        if not replaced:
            rows.append(line)
        self.files.task_index.write_text("\n".join(rows).rstrip() + "\n", encoding="utf-8")
