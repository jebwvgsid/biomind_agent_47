from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .patch_validator import flatten_dict, redact_secrets
from .utils import DEFAULT_PROJECT_ID, clip_text, normalize_list, now_iso, query_terms, safe_id


USER_SECTIONS = ["Preferences", "Communication", "Workflow"]
AGENT_SECTIONS = [
    "Environment Facts",
    "Operational Notes",
    "Project Notes",
    "Reusable Fixes",
    "Important Decisions",
    "Deprecated / Superseded",
]


def _now() -> str:
    return now_iso()


def _clip(text: str, limit: int) -> str:
    return clip_text(text, limit)


def _json_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


@dataclass
class MemorySnapshot:
    project_id: str
    loaded_at: str
    user_profile: str
    agent_memory: str
    project_memory: str

    def prompt_text(self, max_chars: int = 9000) -> str:
        body = (
            f"### Frozen core memory snapshot\n"
            f"- project_id: {self.project_id}\n"
            f"- loaded_at: {self.loaded_at}\n\n"
            f"#### USER_PROFILE.md\n{self.user_profile.strip() or 'No user profile recorded.'}\n\n"
            f"#### AGENT_MEMORY.md\n{self.agent_memory.strip() or 'No agent memory recorded.'}\n\n"
            f"#### projects/{self.project_id}.yaml\n{self.project_memory.strip() or 'No project memory recorded.'}\n"
        )
        return _clip(body, max_chars)


class CoreMemoryStore:
    """Small, stable Markdown/YAML memory that can be frozen into prompts."""

    def __init__(
        self,
        memory_dir: str | Path,
        user_limit: int = 2_000,
        agent_limit: int = 4_000,
        project_limit: int = 8_000,
        default_project_id: str = DEFAULT_PROJECT_ID,
    ):
        self.root = Path(memory_dir)
        self.user_profile = self.root / "USER_PROFILE.md"
        self.agent_memory = self.root / "AGENT_MEMORY.md"
        self.projects_dir = self.root / "projects"
        self.legacy_memory = self.root / "MEMORY.md"
        self.user_limit = user_limit
        self.agent_limit = agent_limit
        self.project_limit = project_limit
        self.default_project_id = self._safe_project_id(default_project_id)
        self._snapshots: Dict[str, MemorySnapshot] = {}
        self.ensure_layout()

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        if not self.user_profile.exists():
            self.user_profile.write_text(self._initial_user_profile(), encoding="utf-8")
        if not self.agent_memory.exists():
            self.agent_memory.write_text(self._initial_agent_memory(), encoding="utf-8")
        self._migrate_legacy_memory()

    def ensure_project(self, project_id: Optional[str] = None) -> Path:
        project_id = self._resolve_project_id(project_id)
        path = self.project_path(project_id)
        if not path.exists():
            path.write_text(
                f"project_id: {project_id}\n"
                f"updated_at: {_now()}\n"
                "facts: {}\n",
                encoding="utf-8",
            )
        return path

    def project_path(self, project_id: Optional[str] = None) -> Path:
        return self.projects_dir / f"{self._resolve_project_id(project_id)}.yaml"

    def load_snapshot(self, project_id: Optional[str] = None, snapshot_id: Optional[str] = None) -> MemorySnapshot:
        project_id = self._resolve_project_id(project_id)
        cache_key = self._snapshot_cache_key(project_id, snapshot_id)
        cached = self._snapshots.get(cache_key)
        if cached:
            return cached
        self.ensure_project(project_id)
        snapshot = MemorySnapshot(
            project_id=project_id,
            loaded_at=_now(),
            user_profile=_clip(self.user_profile.read_text(encoding="utf-8"), self.user_limit),
            agent_memory=_clip(self.agent_memory.read_text(encoding="utf-8"), self.agent_limit),
            project_memory=_clip(self.project_path(project_id).read_text(encoding="utf-8"), self.project_limit),
        )
        self._snapshots[cache_key] = snapshot
        return snapshot

    def reload_snapshot(self, project_id: Optional[str] = None, snapshot_id: Optional[str] = None) -> MemorySnapshot:
        project_id = self._resolve_project_id(project_id)
        self._snapshots.pop(self._snapshot_cache_key(project_id, snapshot_id), None)
        return self.load_snapshot(project_id, snapshot_id=snapshot_id)

    def read_core(self, project_id: Optional[str] = None, max_chars: int = 9000, snapshot_id: Optional[str] = None) -> str:
        return self.load_snapshot(project_id, snapshot_id=snapshot_id).prompt_text(max_chars=max_chars)

    def apply_patch(self, patch: Dict[str, Any], default_project_id: Optional[str] = None) -> None:
        fallback_project_id = self._resolve_project_id(default_project_id)
        legacy = patch.get("long_term")
        if isinstance(legacy, dict):
            self._apply_legacy_long_term(legacy)

        user_profile = patch.get("user_profile")
        if isinstance(user_profile, dict):
            for section, items in user_profile.items():
                self._append_section_bullets(self.user_profile, str(section), self._normalize_items(items))
        elif user_profile is not None:
            self._append_section_bullets(self.user_profile, "Preferences", self._normalize_items(user_profile))

        agent_memory = patch.get("agent_memory")
        if isinstance(agent_memory, dict):
            for section, items in agent_memory.items():
                self._append_section_bullets(self.agent_memory, str(section), self._normalize_items(items))
        elif agent_memory is not None:
            self._append_section_bullets(self.agent_memory, "Operational Notes", self._normalize_items(agent_memory))

        projects = patch.get("projects")
        if isinstance(projects, dict):
            for raw_project_id, project_patch in projects.items():
                project_id = str(raw_project_id).strip() or fallback_project_id
                facts = flatten_dict(project_patch)
                self.upsert_project_facts(project_id, facts)

        # Existing snapshots are intentionally frozen. New sessions use a new
        # snapshot_id, and explicit reload_snapshot() refreshes a chosen cache key.

    def read_project_facts(self, project_id: Optional[str] = None) -> Dict[str, str]:
        path = self.ensure_project(project_id)
        facts: Dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
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

    def upsert_project_facts(self, project_id: Optional[str], facts: Dict[str, Any]) -> None:
        project_id = self._resolve_project_id(project_id)
        existing = self.read_project_facts(project_id)
        for key, value in facts.items():
            clean_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key).strip())
            if clean_key:
                existing[clean_key] = redact_secrets(value)

        lines = [
            f"project_id: {project_id}",
            f"updated_at: {_now()}",
            "facts:",
        ]
        if existing:
            for key in sorted(existing):
                lines.append(f"  {key}: {_json_scalar(existing[key])}")
        else:
            lines[-1] = "facts: {}"
        self.project_path(project_id).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def search(self, query: str, top_k: int = 4, project_id: Optional[str] = None) -> List[Dict[str, str]]:
        project_id = self._resolve_project_id(project_id)
        terms = self._query_terms(query)
        files = [self.user_profile, self.agent_memory, self.project_path(project_id)]
        candidates: List[Dict[str, str]] = []
        for path in files:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for idx, chunk in enumerate(self._split_chunks(text), start=1):
                lower = chunk.lower()
                score = sum(1 for term in terms if term and term in lower)
                if score:
                    candidates.append({
                        "source": f"{path.name}#{idx}",
                        "text": _clip(chunk, 1600),
                        "score": str(score),
                    })
        candidates.sort(key=lambda item: int(item["score"]), reverse=True)
        return candidates[:top_k]

    def status_text(self, project_id: Optional[str] = None) -> str:
        project_id = self._resolve_project_id(project_id)
        project_path = self.project_path(project_id)
        safe_project = self._safe_project_id(project_id)
        loaded = [
            snapshot.loaded_at
            for key, snapshot in self._snapshots.items()
            if key.startswith(f"{safe_project}::")
        ]
        return "\n".join([
            f"Core memory root: {self.root}",
            f"USER_PROFILE.md: {self._size(self.user_profile)} chars / target {self.user_limit}",
            f"AGENT_MEMORY.md: {self._size(self.agent_memory)} chars / target {self.agent_limit}",
            f"projects/{project_path.name}: {self._size(project_path)} chars / target {self.project_limit}",
            f"Frozen snapshots loaded: {', '.join(loaded) if loaded else 'not loaded'}",
        ])

    def _apply_legacy_long_term(self, legacy: Dict[str, Any]) -> None:
        section_map = {
            "User Preferences": (self.user_profile, "Preferences"),
            "Project Facts": (self.agent_memory, "Project Notes"),
            "Important Paths": (self.agent_memory, "Project Notes"),
            "Reusable Fixes": (self.agent_memory, "Reusable Fixes"),
            "Important Decisions": (self.agent_memory, "Important Decisions"),
            "Deprecated / Superseded": (self.agent_memory, "Deprecated / Superseded"),
        }
        for section, items in legacy.items():
            target, target_section = section_map.get(str(section), (self.agent_memory, "Operational Notes"))
            self._append_section_bullets(target, target_section, self._normalize_items(items))

    def _migrate_legacy_memory(self) -> None:
        if not self.legacy_memory.exists():
            return
        marker = "<!-- migrated_from_MEMORY.md -->"
        agent_text = self.agent_memory.read_text(encoding="utf-8")
        if marker in agent_text:
            return
        legacy_text = self.legacy_memory.read_text(encoding="utf-8")
        useful_lines = [
            line for line in legacy_text.splitlines()
            if line.strip() and "- None recorded yet." not in line
        ]
        if len("\n".join(useful_lines).strip()) < 80:
            return
        with self.agent_memory.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n\n{marker}\n"
                "## Legacy Markdown Memory Migration\n"
                "Review and condense these migrated notes during Dream.\n\n"
                "```text\n"
                f"{_clip(redact_secrets(legacy_text), 3000)}\n"
                "```\n"
            )

    @staticmethod
    def _initial_user_profile() -> str:
        sections = "\n\n".join(f"## {section}\n- None recorded yet." for section in USER_SECTIONS)
        return (
            "# BioMind User Profile\n\n"
            "Stable user preferences only. Keep this file short; current user input overrides it.\n\n"
            f"{sections}\n"
        )

    @staticmethod
    def _initial_agent_memory() -> str:
        sections = "\n\n".join(f"## {section}\n- None recorded yet." for section in AGENT_SECTIONS)
        return (
            "# BioMind Agent Memory\n\n"
            "Stable operating facts and reusable experience only. Do not store task logs here.\n\n"
            f"{sections}\n"
        )

    @staticmethod
    def _normalize_items(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            raw = value
        elif isinstance(value, dict):
            raw = [f"{key}: {item}" for key, item in flatten_dict(value).items()]
        else:
            raw = [value]
        return normalize_list(raw, transform=redact_secrets, strip_bullets=True)

    def _append_section_bullets(self, path: Path, section: str, bullets: Iterable[str]) -> None:
        bullets = [redact_secrets(bullet).strip() for bullet in bullets if str(bullet).strip()]
        if not bullets:
            return
        self._ensure_section(path, section)
        text = path.read_text(encoding="utf-8")
        existing = {line.strip().lower() for line in text.splitlines()}
        new_lines = []
        for bullet in bullets:
            rendered = f"- {bullet}"
            if rendered.lower() not in existing:
                new_lines.append(rendered)
        if not new_lines:
            return
        pattern = self._section_pattern(section)
        match = pattern.search(text)
        if not match:
            return
        body_lines = [
            line for line in match.group("body").splitlines()
            if line.strip() != "- None recorded yet."
        ]
        if body_lines and body_lines[-1].strip():
            body_lines.append("")
        body_lines.extend(new_lines)
        replacement = f"## {section}\n" + "\n".join(body_lines).rstrip() + "\n"
        path.write_text(text[: match.start()] + replacement + text[match.end() :], encoding="utf-8")

    def _ensure_section(self, path: Path, section: str) -> None:
        text = path.read_text(encoding="utf-8")
        if re.search(rf"^## {re.escape(section)}\s*$", text, flags=re.MULTILINE):
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## {section}\n- None recorded yet.\n")

    @staticmethod
    def _section_pattern(section: str) -> re.Pattern[str]:
        return re.compile(
            rf"^## {re.escape(section)}\n(?P<body>.*?)(?=^## |\Z)",
            flags=re.DOTALL | re.MULTILINE,
        )

    @staticmethod
    def _split_chunks(text: str) -> List[str]:
        matches = list(re.finditer(r"^## .*$", text, flags=re.MULTILINE))
        if not matches:
            return [_clip(text, 1600)] if text.strip() else []
        chunks: List[str] = []
        for idx, match in enumerate(matches):
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            chunk = text[match.start() : end].strip()
            if chunk:
                chunks.append(chunk)
        return chunks

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        return query_terms(query)

    @staticmethod
    def _safe_project_id(project_id: str) -> str:
        return safe_id(project_id)

    def _resolve_project_id(self, project_id: Optional[str] = None) -> str:
        return self._safe_project_id(project_id or self.default_project_id)

    @classmethod
    def _snapshot_cache_key(cls, project_id: str, snapshot_id: Optional[str]) -> str:
        safe_snapshot = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(snapshot_id or "default").strip())
        return f"{cls._safe_project_id(project_id)}::{safe_snapshot or 'default'}"

    @staticmethod
    def _size(path: Path) -> int:
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8"))
