from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .patch_validator import redact_secrets, sanitize_value
from .policy import (
    build_wiki_index_fields,
    filter_core_project_facts,
    lint_wiki_claims,
    read_simple_project_facts,
    render_project_facts,
)
from .utils import (
    DEFAULT_PROJECT_ID,
    clip_text,
    extract_json_object,
    normalize_list,
    now_iso,
    query_terms,
    safe_id,
)
from .task_semantics import (
    infer_task_semantics as shared_infer_task_semantics,
    merge_task_semantics as shared_merge_task_semantics,
    normalize_task_semantics as shared_normalize_task_semantics,
)

try:
    from langchain_core.messages import HumanMessage
except ImportError:
    class HumanMessage:  # type: ignore[no-redef]
        def __init__(self, content: str):
            self.content = content


logger = logging.getLogger("BioMind.WikiMemory")


class NullWikiMemoryManager:
    """No-op wiki manager used when BioMind wiki memory is disabled."""

    enabled = False

    def __init__(self, *args: Any, **kwargs: Any):
        self.root = Path("")
        self.wiki_dir = Path("")
        self.overview_file = Path("/dev/null")
        self.default_project_id = str(kwargs.get("default_project_id") or DEFAULT_PROJECT_ID)

    def build_prompt_context(self, *args: Any, **kwargs: Any) -> str:
        return ""

    async def ingest_from_archive(self, *args: Any, **kwargs: Any) -> str:
        return self.status_text()

    def compile_digest(self, *args: Any, **kwargs: Any) -> str:
        return self.status_text()

    def digest_path(self, project_id: Optional[str] = None) -> Path:
        return Path("/dev/null")

    def read_claims_index(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def lint(self, project_id: Optional[str] = None) -> str:
        return self.status_text()

    def status_text(self, project_id: Optional[str] = None) -> str:
        return "Wiki memory is disabled (set BIOMIND_WIKI_ENABLED=true to enable it)."

    def format_hits(self, hits: Sequence[Dict[str, Any]], title: str = "Wiki Search") -> str:
        return self.status_text()

    def search(self, query: str, project_id: Optional[str] = None, top_k: int = 6) -> List[Dict[str, Any]]:
        return []

    def sync_project_index_fields(self, project_id: Optional[str] = None) -> str:
        return self.status_text()

    def read_page(self, identifier: str) -> str:
        return self.status_text()

    def read_review_queue(self) -> str:
        return self.status_text()


ALLOWED_PAGE_TYPES = {
    "concept",
    "project",
    "method",
    "dataset",
    "entity",
    "tool",
    "decision",
    "issue",
    "artifact",
    "query",
    "source",
    "synthesis",
}

TYPE_DIRS = {
    "concept": "concepts",
    "project": "projects",
    "method": "methods",
    "dataset": "datasets",
    "entity": "entities",
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
    "upsert_claims",
    "update_frontmatter",
    "update_index",
    "add_research_queries",
    "append_log",
    "flag_review",
}

TASK_SEMANTIC_LIST_KEYS = {
    "skills",
    "samples",
    "output_artifacts",
    "output_dirs",
    "resolutions",
}
TASK_SEMANTIC_SCALAR_KEYS = {
    "task_id",
    "task_title",
    "task_type",
    "workflow",
    "workflow_id",
    "analysis_type",
    "task_date",
}
TASK_SEMANTIC_FRONTMATTER_KEYS = TASK_SEMANTIC_LIST_KEYS | TASK_SEMANTIC_SCALAR_KEYS
LIST_FRONTMATTER_KEYS = {"sources", "aliases", "tags", "supersedes", "related"} | TASK_SEMANTIC_LIST_KEYS
ANALYSIS_REQUIRED_KEYS = {
    "source_summary": dict,
    "entities": list,
    "concepts": list,
    "claims": list,
    "conflicts": list,
    "wiki_patch_plan": list,
    "research_queries": list,
}
VALID_ENTITY_TYPES = {
    "gene",
    "protein",
    "cultivar",
    "species",
    "sample",
    "tissue",
    "cell_type",
    "disease",
    "tool",
    "dataset",
    "path",
    "parameter",
    "trait",
    "other",
}
VALID_CONCEPT_TYPES = {
    "method",
    "mechanism",
    "metric",
    "threshold",
    "workflow",
    "file_format",
    "analysis_strategy",
    "biological_process",
    "other",
}
PAGE_TYPE_BOUNDARY_RULES = (
    "## Page Type Boundaries\n"
    "- tool = a concrete software package, command-line tool, library, database, or external service.\n"
    "- method = an analysis method, workflow, technical procedure, or parameter strategy.\n"
    "- dataset = a named dataset, sample collection, input data source, or reusable data bundle.\n"
    "- entity = a biological or project object referenced across pages, such as a gene, sample, species, tissue, cell_type, trait, path, or parameter.\n"
    "- concept = an abstract biological or analytical idea, such as a mechanism, metric, threshold, workflow, file_format, or biological_process.\n"
    "- Prefer the concrete page type first: tools belong in tool pages, analysis techniques belong in method pages, and data collections belong in dataset pages.\n"
    "- Create entity or concept pages only when the item is cross-page knowledge that does not fit a more specific existing type.\n"
    "- Link entity/concept pages to tool, method, and dataset pages with related[] instead of duplicating their content.\n"
)
SKILL_SELECTION_MEMORY_RULES = (
    "## Skill Selection Memory Rules\n"
    "- When archive evidence includes selected_skills, active_skills, future_skills, candidate_skills, "
    "selected_skills_raw, workflow_handoff, workflow_stages, or skill_selection_source, treat it as reusable "
    "skill-choice evidence for similar future tasks.\n"
    "- Store reusable skill-choice knowledge in exactly one canonical section named `## Skill Selection` on the "
    "most specific method or decision page for that task/workflow. Do not create separate `Used Skills`, "
    "`Selected Skills`, `Skills Used`, `Rejected Skills`, or similar duplicate sections.\n"
    "- Frontmatter `skills[]` is only a search/graph index of active or selected skill IDs; do not explain "
    "skill roles, rejected skills, or transition reasons in frontmatter.\n"
    "- In `## Skill Selection`, use one table with columns `skill_id | role | phase | reason | evidence`. "
    "Allowed roles are active, future, candidate, rejected, added_later, or avoided.\n"
    "- Record later additions by updating the existing `## Skill Selection` row to role=added_later or by adding "
    "one `## Skill Transitions` table only when a skill changed phase. Do not repeat the same skill in another "
    "used-skills list.\n"
    "- Source pages under sources/ preserve raw evidence only; reusable skill-selection patterns belong in "
    "method or decision pages.\n"
    "- Merge by canonical skill_id and evidence ref: update existing rows/fields instead of appending duplicates.\n"
)
CLAIM_EVIDENCE_PREFIXES = ("raw:", "session_archive:", "task:", "artifact:")
VALID_CLAIM_CONFIDENCE = {"medium", "high"}
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
RRF_K = 60
RRF_SCORE_SCALE = 300.0
INGEST_EVENT_TEXT_CHAR_LIMIT = 80_000
INGEST_ANALYSIS_JSON_CHAR_LIMIT = 80_000


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
    """Project-scoped LLM Wiki memory curated from SessionArchive evidence."""

    def __init__(
        self,
        llm: Any,
        memory_dir: str | Path,
        max_inline_chars: int = 4_000,
        max_search_candidates: int = 8,
        default_project_id: str = DEFAULT_PROJECT_ID,
        embedding_config: Optional[Dict[str, Any]] = None,
        wiki_dir: Optional[str | Path] = None,
    ):
        self.llm = llm
        self.root = Path(memory_dir)
        self.raw_dir = self.root / "raw"
        self.conversations_dir = self.raw_dir / "conversations"
        self.wiki_dir = Path(wiki_dir) if wiki_dir else self.root / "wiki"
        self.review_dir = self.wiki_dir / "review"
        self.review_pending = self.review_dir / "PENDING.md"
        self.index_file = self.wiki_dir / "index.md"
        self.log_file = self.wiki_dir / "log.md"
        self.overview_file = self.wiki_dir / "overview.md"
        self.research_queries_file = self.wiki_dir / "research_queries.jsonl"
        self.cache_dir = self.wiki_dir / "cache"
        self.purpose_file = self.root / "purpose.md"
        self.schema_file = self.root / "schema.md"
        self.db_path = self.wiki_dir / "wiki_index.sqlite3"
        self.max_inline_chars = max_inline_chars
        self.max_search_candidates = max_search_candidates
        self.default_project_id = _safe_id(default_project_id)
        self.embedding_config = dict(embedding_config or {})
        self.fts_enabled = False
        self.fts_tokenizer = "none"
        self.ensure_layout()
        self._init_db()

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for folder in {
            *TYPE_DIRS.values(),
            "review",
        }:
            (self.wiki_dir / folder).mkdir(parents=True, exist_ok=True)

        if not self.purpose_file.exists():
            self.purpose_file.write_text(self._initial_purpose(), encoding="utf-8")
        if not self.schema_file.exists():
            self.schema_file.write_text(self._initial_schema(), encoding="utf-8")
        if not self.index_file.exists():
            self.index_file.write_text("# Wiki Index\n\nNo wiki pages indexed yet.\n", encoding="utf-8")
        if not self.log_file.exists():
            self.log_file.write_text("# Wiki Log\n", encoding="utf-8")
        if not self.overview_file.exists():
            self.overview_file.write_text("# Wiki Overview\n\nNo wiki pages created yet.\n", encoding="utf-8")
        if not self.review_pending.exists():
            self.review_pending.write_text("# Pending Wiki Review\n", encoding="utf-8")
        if not self.research_queries_file.exists():
            self.research_queries_file.write_text("", encoding="utf-8")

    @staticmethod
    def _initial_purpose() -> str:
        return (
            "# BioMind Wiki Purpose\n\n"
            "This Wiki is the long-term, evidence-backed memory for a bioinformatics analysis agent. "
            "It is not a generic chat summary. It preserves reusable project knowledge that helps the agent "
            "continue, audit, reproduce, and improve bioinformatics work across sessions.\n\n"
            "## Mission\n"
            "- Preserve durable bioinformatics project knowledge with traceable evidence.\n"
            "- Help the agent recover context across sessions without re-reading full logs.\n"
            "- Track datasets, input files, methods, parameters, decisions, issues, artifacts, and conclusions.\n"
            "- Support reproducible analysis by linking claims to SessionArchive events or artifacts.\n"
            "- Keep Core memory small: Core stores user preferences, stable operating habits, and Wiki indexes only.\n\n"
            "## What Belongs In This Wiki\n"
            "- Dataset facts: sample names, sequencing files, metadata, genome versions, annotations.\n"
            "- Method facts: tools, workflows, parameters, commands that shaped the analysis.\n"
            "- Decisions: why a method, parameter, reference, filtering rule, or Skill selection was chosen.\n"
            "- Issues and fixes: errors, failed attempts, validated repairs, caveats.\n"
            "- Artifacts: output files, reports, assemblies, plots, tables, and evaluation results.\n"
            "- Conclusions: evidence-backed biological or technical findings.\n\n"
            "## What Does Not Belong Here\n"
            "- Generic chat summaries without reusable project value.\n"
            "- Unverified guesses or facts without evidence.\n"
            "- Full noisy execution logs; keep those in raw/session archives.\n"
            "- Procedural SOPs that should become reusable Skills.\n"
            "- Duplicate Skill lists; reusable Skill choices belong in one canonical Skill Selection section.\n"
            "- Temporary thoughts that only matter within the current turn.\n\n"
            "## Current Questions\n"
            "- Which project facts are needed for future continuation?\n"
            "- Which inputs, parameters, and outputs must be remembered for reproducibility?\n"
            "- Which decisions or issues should be preserved because they affect later analysis?\n\n"
            "## Evolving Hypotheses\n"
            "- None recorded yet.\n\n"
            "## Evidence Policy\n"
            "Every durable factual claim should cite evidence using session_archive:, task:, or artifact: references. "
            "Historical raw: references may exist in older pages, but new ingest should cite session_archive:<id>. "
            "If evidence is missing, conflicting, low-confidence, or stale, route the item to review instead of storing it as a claim.\n"
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
            "updated_at, sources, aliases, tags, confidence, and optional claims[].\n"
            "- Task-linked pages may include task semantic frontmatter: task_id, task_title, task_type, "
            "workflow_id, analysis_type, skills[], samples[], resolutions[], output_artifacts[], and output_dirs[]. "
            "These fields are used for graph/community linkage and should be preserved when present in archive evidence.\n"
            "- Entity pages should use type=entity and may include entity_type and related[]. "
            "Concept pages should use type=concept and may include concept_type, definition, and related[].\n"
            "- Each claim must include id, text, status, confidence, evidence[], supersedes[], "
            "review_required, and updated_at.\n"
            "- Claim evidence must reference session_archive:, task:, or artifact: sources. Preserve historical raw: references when editing old pages, but do not create new raw: evidence.\n"
            "- Existing pages are updated by appending sections, merging bullets, or updating frontmatter; do not overwrite pages.\n"
            "- Low-confidence, contradictory, stale, or evidence-free claims must be flagged for review instead of applied.\n"
            "- Page paths must stay inside wiki/ and must not contain path traversal.\n\n"
            "## Source Rules\n"
            "- Cite SessionArchive IDs, task IDs, or artifact paths in sources[].\n"
            "- Use add_research_queries for machine-readable follow-up search queries that need later deep research.\n"
            "\n"
            f"{PAGE_TYPE_BOUNDARY_RULES}"
            "\n"
            f"{SKILL_SELECTION_MEMORY_RULES}"
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

    @staticmethod
    def migrate_wiki_pages(source_wiki_dir: str | Path, target_wiki_dir: str | Path) -> List[str]:
        """Copy wiki files into a configured wiki directory without deleting or overwriting."""
        source = Path(source_wiki_dir).resolve()
        target = Path(target_wiki_dir).resolve()
        copied: List[str] = []
        if source == target or not source.exists() or not source.is_dir():
            return copied
        target.mkdir(parents=True, exist_ok=True)
        for source_file in source.rglob("*"):
            if not source_file.is_file():
                continue
            rel = source_file.relative_to(source)
            dest = target / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, dest)
            copied.append(str(dest))
        return copied

    def infer_task_semantics(
        self,
        task_id: str,
        stage: str = "",
        messages: Optional[List[Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Infer compact task-level metadata for wiki indexing and graph edges."""
        return shared_infer_task_semantics(task_id, stage=stage, messages=messages, payload=payload)

    @classmethod
    def _normalize_task_semantics(cls, value: Any) -> Dict[str, Any]:
        return shared_normalize_task_semantics(value)

    @classmethod
    def _merge_task_semantics(cls, *items: Any) -> Dict[str, Any]:
        return shared_merge_task_semantics(*items)

    @staticmethod
    def _nested_get(value: Any, path: Sequence[str]) -> Any:
        current = value
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    async def ingest_from_archive(
        self,
        session_archive: Any,
        project_id: Optional[str] = None,
        limit: int = 20,
        task_id: Optional[str] = None,
    ) -> str:
        project_id = self._resolve_project_id(project_id)
        pending = session_archive.pending_for_wiki(
            project_id=project_id,
            limit=max(1, limit),
            task_id=task_id,
        )
        if not pending:
            return "No pending wiki events to ingest."
        if self.llm is None:
            return "Wiki ingest unavailable: no LLM configured."

        batch = pending[: max(1, limit)]
        archive_records = [event.as_wiki_record() for event in batch]
        event_text = session_archive.format_events_for_wiki_prompt(batch, total_limit=INGEST_EVENT_TEXT_CHAR_LIMIT)
        query = "\n".join(
            " ".join([
                str(record.get("summary", ""))[:500],
                str(record.get("content", ""))[:500],
                str(record.get("metadata", ""))[:500],
            ])
            for record in archive_records
        )
        related_pages = self.format_hits(self.search(query, project_id=project_id, top_k=4))

        analysis_prompt = f"""You are BioMind's wiki analyst.
Analyze pending SessionArchive evidence before wiki patch generation. Return strict JSON only.
Do not output chain-of-thought or markdown fences.

Purpose:
{self.read_purpose(max_chars=5000)}

Schema:
{self.schema_file.read_text(encoding="utf-8")}

Index:
{_clip(self.index_file.read_text(encoding="utf-8"), 5000)}

Relevant pages:
{related_pages}

Page type boundary rules:
{PAGE_TYPE_BOUNDARY_RULES}

Skill selection memory rules:
{SKILL_SELECTION_MEMORY_RULES}

Pending SessionArchive evidence:
{event_text}

Return JSON with exactly these top-level keys:
{{
  "source_summary": {{"title": "", "source_type": "", "main_topic": "", "one_sentence_summary": ""}},
  "entities": [
    {{"name": "", "entity_type": "gene|protein|species|sample|tissue|cell_type|disease|path|parameter|trait|other", "aliases": [], "role": "central|supporting|peripheral", "evidence": [], "suggested_page": "entities/example.md"}}
  ],
  "concepts": [
    {{"name": "", "concept_type": "mechanism|metric|threshold|workflow|file_format|analysis_strategy|biological_process|other", "definition": "", "importance": "", "evidence": [], "suggested_page": "concepts/example.md"}}
  ],
  "claims": [
    {{"claim": "", "confidence": "high|medium|low", "evidence": [], "related_entities": [], "related_concepts": []}}
  ],
  "conflicts": [
    {{"description": "", "conflicts_with_page": "", "evidence": [], "severity": "high|medium|low", "needs_review": true}}
  ],
  "wiki_patch_plan": [
    {{"action": "create|update|append|review", "path": "", "type": "", "reason": ""}}
  ],
  "research_queries": [
    {{"query": "", "reason": "", "priority": "high|medium|low", "trigger": "conflict|missing_evidence|knowledge_gap|new_entity", "sources": [], "related_pages": []}}
  ]
}}

Priority rules:
- If an item is a named software/package/database/CLI tool, plan a tool page update instead of an entity page.
- If an item is an analysis technique, workflow, procedure, or parameter strategy, plan a method page update instead of a concept page.
- If an item is a named dataset or sample collection, plan a dataset page update instead of an entity page.
- Only include entities/concepts that should be referenced by multiple pages and do not fit tool, method, or dataset.
- If archive evidence contains skill selection fields, plan a method or decision page update with one canonical
  `## Skill Selection` section. Do not plan duplicate sections for used/selected/rejected skills.
"""
        try:
            analysis_response = await self.llm.ainvoke([HumanMessage(content=analysis_prompt)])
        except Exception as exc:
            return self._ingest_llm_error("analysis", exc, batch, project_id)
        analysis = _content_of(analysis_response)
        analysis_json = _extract_json_object(analysis)
        analysis_errors = self._validate_analysis_json(analysis_json)
        if analysis_errors:
            self._flag_review(
                "Invalid Wiki Analysis",
                "Wiki analysis JSON was invalid; patch generation was skipped.\n" + "\n".join(f"- {issue}" for issue in analysis_errors),
                {
                    "event_ids": [f"session_archive:{event.id}" for event in batch],
                    "analysis_preview": _clip(analysis, 4000),
                },
                project_id,
            )
            return "Wiki ingest failed: analyst returned invalid JSON."

        patch_prompt = f"""Generate a strict JSON wiki patch for BioMind.
Return JSON only with this shape: {{"operations": [{{"op": "append_log", "message": "..."}}], "ingest_summary": "..."}}.
Do not output chain-of-thought or markdown fences.

Allowed operations: {", ".join(sorted(ALLOWED_OPS))}.
Allowed page types: {", ".join(sorted(ALLOWED_PAGE_TYPES))}.
All paths must stay inside wiki/.
Prefer append_section or merge_section_items for existing pages.
Page type boundary rules:
{PAGE_TYPE_BOUNDARY_RULES}
Skill selection memory rules:
{SKILL_SELECTION_MEMORY_RULES}
Before creating entity/concept pages, check whether the information should update an existing or new tool, method, or dataset page instead.
Use type=entity pages under entities/ for important genes, samples, tissues, species, paths, parameters, or traits that are cross-page objects.
Use type=concept pages under concepts/ for important mechanisms, metrics, thresholds, workflows, file formats, strategies, or biological processes that are cross-page ideas.
Avoid duplicate entity/concept pages for named tools, analysis methods, or datasets; link to those pages with related[] instead of copying their content.
When writing reusable skill-choice memory, update only `## Skill Selection` and optional `## Skill Transitions`.
Do not write separate `Used Skills`, `Selected Skills`, `Rejected Skills`, or narrative duplicates.
Route conflicts, stale facts, and low confidence updates to flag_review.
Every applied page update must include sources with session_archive event refs.
Use upsert_claims for durable factual claims. Every claim must include evidence[] using session_archive:, task:, or artifact: refs.
Use type=source pages to summarize archive evidence when useful.
Use add_research_queries for every useful follow-up search from analysis_json.research_queries.
Do not update purpose.md directly. If the archive evidence suggests changes to goals, current questions, scope, or evolving hypotheses,
emit a flag_review operation titled "Purpose Update Suggestion" with the suggested purpose changes in the body.
Preserve task_semantics from archive events in page frontmatter when a page represents or summarizes that task context. Use fields such as
task_title, task_type, workflow_id, analysis_type, skills, samples, resolutions, output_artifacts, and output_dirs.

Analysis JSON:
{_clip(json.dumps(analysis_json, ensure_ascii=False, indent=2, default=str), INGEST_ANALYSIS_JSON_CHAR_LIMIT)}

SessionArchive event refs:
{", ".join(f"session_archive:{event.id}" for event in batch)}
"""
        try:
            patch_response = await self.llm.ainvoke([HumanMessage(content=patch_prompt)])
        except Exception as exc:
            return self._ingest_llm_error("patch", exc, batch, project_id)
        patch = _extract_json_object(_content_of(patch_response))
        if not patch:
            return "Wiki ingest failed: curator returned invalid JSON."

        applied = self.apply_patch(patch, project_id=project_id)
        source_summaries = self._ensure_source_summary_pages(archive_records, project_id)
        if source_summaries:
            self._rebuild_index_page()
            self._sync_search_index()
            self.compile_digest(project_id)
        summary = str(patch.get("ingest_summary") or patch.get("summary") or "Wiki ingest completed.")
        session_archive.mark_wiki_ingested(
            [event.id for event in batch],
            project_id=project_id,
            summary=summary,
        )
        return (
            f"Wiki ingest completed.\n"
            f"Summary: {summary}\n"
            f"Operations applied: {applied}\n"
            f"Source summaries created: {source_summaries}"
        )

    def _ingest_llm_error(
        self,
        stage: str,
        exc: Exception,
        batch: Sequence[Any],
        project_id: str,
    ) -> str:
        message = redact_secrets(str(exc))
        event_ids = [self._ingest_event_ref(item) for item in batch]
        self._flag_review(
            "Wiki Ingest LLM Error",
            f"Wiki ingest failed during {stage} LLM call.\n\n{message}",
            {"stage": stage, "event_ids": event_ids, "error": message},
            project_id,
        )
        logger.warning("Wiki ingest %s LLM call failed: %s", stage, message)
        return f"Wiki ingest failed during {stage} LLM call: {message}"

    @staticmethod
    def _ingest_event_ref(item: Any) -> str:
        event_id = getattr(item, "id", None)
        if event_id is not None:
            return f"session_archive:{event_id}"
        if isinstance(item, dict):
            return str(item.get("event_ref") or item.get("event_id") or "")
        return str(item)

    @staticmethod
    def _validate_analysis_json(data: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(data, dict):
            return ["analysis was not a JSON object"]
        issues: List[str] = []
        for key, expected_type in ANALYSIS_REQUIRED_KEYS.items():
            if key not in data:
                issues.append(f"missing key: {key}")
                continue
            if not isinstance(data.get(key), expected_type):
                issues.append(f"{key} must be {expected_type.__name__}")
        return issues

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
                elif op_name == "upsert_claims":
                    self._op_upsert_claims(operation, project_id)
                elif op_name == "update_frontmatter":
                    self._op_update_frontmatter(operation, project_id)
                elif op_name == "update_index":
                    self._rebuild_index_page()
                elif op_name == "add_research_queries":
                    self._op_add_research_queries(operation, project_id)
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
        self._sync_search_index()
        if applied:
            self.compile_digest(project_id)
        return applied

    def search(self, query: str, project_id: Optional[str] = None, top_k: int = 6) -> List[Dict[str, Any]]:
        project_id = self._resolve_project_id(project_id)
        self._sync_search_index()
        query = query.strip()
        if not query:
            hits = self._recent_pages(project_id, top_k)
        else:
            lexical_hits = self._fts_search(query, project_id, top_k * 2)
            if not lexical_hits:
                lexical_hits = self._lexical_search(query, project_id, top_k * 2)
            vector_hits: List[WikiSearchHit] = []
            if self._embedding_enabled():
                vector_hits = self._vector_search(query, project_id, top_k * 2)
            hits = self._rrf_fuse([lexical_hits, vector_hits], top_k * 2) if vector_hits else lexical_hits
        expanded = self._expand_hits(hits, project_id, top_k)
        return [hit.as_dict() for hit in expanded[:top_k]]

    def build_prompt_context(
        self,
        task_id: str,
        project_id: Optional[str] = None,
        query: str = "",
        top_k: int = 5,
    ) -> str:
        parts: List[str] = []
        purpose = self.read_purpose()
        if purpose:
            parts.append(f"<wiki_purpose>\n{purpose}\n</wiki_purpose>")
        hits = self.search(query, project_id=project_id, top_k=top_k)
        if hits:
            parts.append(self.format_hits(hits, title="Retrieved Wiki Memory"))
        return "\n\n".join(parts)

    def read_purpose(self, max_chars: int = 3_000) -> str:
        if not self.purpose_file.exists():
            return ""
        return _clip(self.purpose_file.read_text(encoding="utf-8", errors="replace"), max_chars)

    def compile_digest(self, project_id: Optional[str] = None) -> str:
        project_id = self._resolve_project_id(project_id)
        pages = self._all_pages(project_id=project_id)
        claims = self._flatten_claims(pages, project_id)
        communities = self.compile_communities(project_id)
        self._sync_page_embeddings(project_id, pages)
        review_count = self._review_count(project_id)
        page_groups: Dict[str, List[Dict[str, str]]] = {}
        for page in pages:
            page_groups.setdefault(str(page.get("type") or "unknown"), []).append({
                "path": str(page.get("path") or ""),
                "title": str(page.get("title") or ""),
                "updated_at": str(page.get("updated_at") or ""),
            })
        for group in page_groups.values():
            group.sort(key=lambda item: item["title"].lower())

        high_confidence = [
            {
                "id": claim["id"],
                "text": claim["text"],
                "page": claim["page"],
                "evidence": claim["evidence"][:3],
            }
            for claim in claims
            if str(claim.get("confidence")).lower() == "high"
            and str(claim.get("status")).lower() not in {"deprecated", "rejected", "superseded"}
        ][:8]
        community_items = communities.get("communities") if isinstance(communities, dict) else []
        if not isinstance(community_items, list):
            community_items = []
        page_metrics = communities.get("pages") if isinstance(communities, dict) else {}
        if not isinstance(page_metrics, dict):
            page_metrics = {}
        low_cohesion = [
            item for item in community_items
            if isinstance(item, dict) and "low_cohesion" in _ensure_list(item.get("warnings"))
        ]
        bridge_pages = [
            {
                "path": str(path),
                "title": str(info.get("title") or ""),
                "type": str(info.get("type") or ""),
                "bridge_score": int(info.get("bridge_score") or 0),
            }
            for path, info in page_metrics.items()
            if isinstance(info, dict) and int(info.get("bridge_score") or 0) >= 2
        ]
        bridge_pages.sort(key=lambda item: (-item["bridge_score"], item["title"].lower(), item["path"]))
        knowledge_pages = [
            {
                "path": str(page.get("path") or ""),
                "title": str(page.get("title") or ""),
                "type": str(page.get("type") or ""),
                "updated_at": str(page.get("updated_at") or ""),
            }
            for page in pages
            if str(page.get("type") or "").lower() in {"entity", "concept"}
        ][:12]
        top_communities = [
            {
                "community_id": str(item.get("community_id") or ""),
                "size": int(item.get("size") or 0),
                "cohesion": float(item.get("cohesion") or 0.0),
                "core_pages": item.get("core_pages") or [],
                "warnings": _ensure_list(item.get("warnings")),
            }
            for item in community_items[:5]
            if isinstance(item, dict)
        ]

        digest = {
            "project_id": project_id,
            "updated_at": _now(),
            "page_count": len(pages),
            "claim_count": len(claims),
            "review_count": review_count,
            "research_query_count": self._research_query_count(project_id),
            "community_count": len(community_items),
            "low_cohesion_count": len(low_cohesion),
            "entity_count": len(page_groups.get("entity", [])),
            "concept_count": len(page_groups.get("concept", [])),
            "bridge_pages": bridge_pages[:5],
            "top_communities": top_communities,
            "page_types": {key: len(value) for key, value in sorted(page_groups.items())},
            "pages": page_groups,
            "knowledge_pages": knowledge_pages,
            "high_confidence_claims": high_confidence,
            "usage": "Use wiki search/page retrieval for evidence; this digest is only a compact index.",
        }

        compiled_at = str(digest["updated_at"])
        cache_dir = self.cache_project_dir(project_id)
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.digest_path(project_id).write_text(
            json.dumps(digest, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        with self.claims_index_path(project_id).open("w", encoding="utf-8") as handle:
            for claim in claims:
                handle.write(json.dumps(claim, ensure_ascii=False, default=str) + "\n")
        synced = self.sync_project_index_fields(project_id, pages=pages, compiled_at=compiled_at)
        return (
            f"Wiki digest compiled for project {project_id}: "
            f"{len(pages)} page(s), {len(claims)} claim(s), {review_count} review item(s).\n"
            f"{synced}"
        )

    def sync_project_index_fields(
        self,
        project_id: Optional[str] = None,
        pages: Optional[Sequence[Dict[str, Any]]] = None,
        compiled_at: Optional[str] = None,
    ) -> str:
        project_id = self._resolve_project_id(project_id)
        pages = list(pages) if pages is not None else self._all_pages(project_id=project_id)
        compiled_at = compiled_at or _now()
        project_path = self.root / "projects" / f"{project_id}.yaml"
        project_path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_simple_project_facts(project_path)
        existing, rejected = filter_core_project_facts(existing)
        if rejected:
            self._append_log(
                "Removed non-index project facts from Core project memory during wiki digest sync: "
                + ", ".join(sorted(rejected))
            )
        existing.update(build_wiki_index_fields(project_id, self.digest_path(project_id), pages, compiled_at))
        project_path.write_text(
            render_project_facts(project_id, existing, updated_at=compiled_at),
            encoding="utf-8",
        )
        return f"Synced project wiki index fields: {project_path}"

    def cache_project_dir(self, project_id: Optional[str] = None) -> Path:
        return self.cache_dir / self._resolve_project_id(project_id)

    def digest_path(self, project_id: Optional[str] = None) -> Path:
        return self.cache_project_dir(project_id) / "agent-digest.json"

    def claims_index_path(self, project_id: Optional[str] = None) -> Path:
        return self.cache_project_dir(project_id) / "claims.jsonl"

    def community_path(self, project_id: Optional[str] = None) -> Path:
        return self.cache_project_dir(project_id) / "communities.json"

    def read_claims_index(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        project_id = self._resolve_project_id(project_id)
        path = self.claims_index_path(project_id)
        if not path.exists():
            return self._flatten_claims(self._all_pages(project_id=project_id), project_id)
        claims: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                claims.append(data)
        return claims

    def read_communities(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        project_id = self._resolve_project_id(project_id)
        path = self.community_path(project_id)
        if not path.exists():
            return self.compile_communities(project_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return self.compile_communities(project_id)
        if not isinstance(data, dict):
            return self.compile_communities(project_id)
        return data

    def compile_communities(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        project_id = self._resolve_project_id(project_id)
        pages = [
            page for page in self._all_pages(project_id=project_id)
            if str(page.get("type") or "").lower() != "query"
        ]
        edges = self._page_graph_edges(pages)
        community_data = self._detect_page_communities(pages, edges)
        community_data.update({
            "project_id": project_id,
            "updated_at": _now(),
            "edge_threshold": 1.5,
            "page_count": len(pages),
            "edge_count": len(edges),
        })
        cache_dir = self.cache_project_dir(project_id)
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.community_path(project_id).write_text(
            json.dumps(community_data, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return community_data

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
        issues.extend(lint_wiki_claims(self._flatten_claims(pages, project_id)))
        communities = self.compile_communities(project_id)
        community_items = communities.get("communities") if isinstance(communities, dict) else []
        page_metrics = communities.get("pages") if isinstance(communities, dict) else {}
        if isinstance(page_metrics, dict):
            for path, info in sorted(page_metrics.items()):
                if not isinstance(info, dict):
                    continue
                page_type = str(info.get("type") or "")
                if int(info.get("degree") or 0) == 0 and page_type != "query":
                    issues.append(f"Isolated wiki page in graph: {path}")
                bridge_score = int(info.get("bridge_score") or 0)
                if bridge_score >= 2:
                    issues.append(f"Bridge wiki page connects {bridge_score} communities: {path}")
        if isinstance(community_items, list):
            for item in community_items:
                if not isinstance(item, dict):
                    continue
                if "low_cohesion" in _ensure_list(item.get("warnings")):
                    issues.append(
                        f"Low-cohesion wiki community {item.get('community_id')}: "
                        f"size={item.get('size')}, cohesion={float(item.get('cohesion') or 0.0):.2f}"
                    )
        if not issues:
            return f"Wiki lint passed for project {project_id}. Pages checked: {len(pages)}."
        return "Wiki lint found issues:\n" + "\n".join(f"- {issue}" for issue in issues)

    def status_text(self, project_id: Optional[str] = None) -> str:
        project_id = self._resolve_project_id(project_id)
        pages = self._all_pages(project_id=project_id)
        page_count = len(pages)
        claim_count = len(self._flatten_claims(pages, project_id))
        review_count = self._review_count(project_id)
        legacy_raw_count = sum(1 for _ in self.conversations_dir.glob("*.jsonl")) if self.conversations_dir.exists() else 0
        research_count = self._research_query_count(project_id)
        digest_path = self.digest_path(project_id)
        return "\n".join([
            f"Wiki memory root: {self.root}",
            f"Wiki pages dir: {self.wiki_dir}",
            f"Wiki pages: {page_count}",
            f"Wiki claims: {claim_count}",
            f"Research queries: {research_count}",
            f"Legacy raw conversation files: {legacy_raw_count}",
            "Pending wiki events: stored in SessionArchive",
            f"FTS enabled: {self.fts_enabled} ({self.fts_tokenizer})",
            f"Embedding enabled: {self._embedding_enabled()} ({self._embedding_model()}, dim={self._embedding_dimensions()})",
            f"Review queue: {self.review_pending}",
            f"Review items: {review_count}",
            f"Digest: {digest_path} ({'exists' if digest_path.exists() else 'missing'})",
            f"Claims index: {self.claims_index_path(project_id)}",
            f"Research query queue: {self.research_queries_file}",
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
                    content_hash TEXT,
                    updated_at TEXT
                )
                """
            )
            self._ensure_wiki_pages_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_embeddings (
                    path TEXT,
                    project_id TEXT,
                    model TEXT,
                    dimensions INTEGER,
                    content_hash TEXT,
                    vector_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (path, project_id, model, dimensions)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_wiki_embeddings_project_model
                ON wiki_embeddings(project_id, model, dimensions)
                """
            )
            self._ensure_fts(conn)
            conn.commit()

    @staticmethod
    def _ensure_wiki_pages_columns(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()
        }
        if "content_hash" not in columns:
            conn.execute("ALTER TABLE wiki_pages ADD COLUMN content_hash TEXT")

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

    def _ensure_source_summary_pages(self, raw_records: Sequence[Dict[str, Any]], project_id: str) -> int:
        project_id = self._resolve_project_id(project_id)
        pages = self._all_pages(project_id=project_id)
        created = 0
        for record in raw_records:
            event_id = str(record.get("event_id") or "").strip()
            event_ref = str(record.get("event_ref") or "").strip()
            if not event_ref and event_id:
                event_ref = f"session_archive:{event_id}" if event_id.isdigit() else f"raw:{event_id}"
            if not event_id or not event_ref:
                continue
            if self._source_summary_exists(event_id, event_ref, pages):
                continue

            title = f"Source Summary: {event_ref}"
            path_key = event_ref if event_ref.startswith("session_archive:") else event_id
            path = self._safe_wiki_path(f"sources/{_slug(path_key, default='event')}.md")
            task_semantics = self._merge_task_semantics(
                record.get("task_semantics"),
                self._nested_get(record, ("payload", "task_semantics")),
                self._nested_get(record, ("metadata", "task_semantics")),
            )
            if path.exists():
                text = path.read_text(encoding="utf-8")
                frontmatter, body = self._split_frontmatter(text)
                frontmatter["type"] = "source"
                frontmatter["project_id"] = project_id
                frontmatter["sources"] = self._merge_lists(frontmatter.get("sources"), [event_ref])
                frontmatter.update(self._merge_task_semantics(frontmatter, task_semantics))
                frontmatter["updated_at"] = _now()
                if not body.strip():
                    body = self._source_summary_body(record, title)
                self._write_page(path, frontmatter, body)
            else:
                frontmatter = self._default_frontmatter(
                    title=title,
                    page_type="source",
                    project_id=project_id,
                    sources=[event_ref],
                    confidence="medium",
                )
                frontmatter.update(task_semantics)
                self._write_page(
                    path,
                    frontmatter,
                    self._source_summary_body(record, title),
                )
            created += 1
            self._append_log(f"Created fallback source summary: {self._relative_page_path(path)}")
            page_record = {
                "path": self._relative_page_path(path),
                "title": title,
                "type": "source",
                "project_id": project_id,
                "content": self._source_summary_body(record, title),
                "sources": [event_ref],
                "claims": [],
                "links": [],
                "updated_at": _now(),
            }
            page_record.update(task_semantics)
            pages.append(page_record)
        return created

    @staticmethod
    def _source_summary_exists(event_id: str, event_ref: str, pages: Sequence[Dict[str, Any]]) -> bool:
        path_key = event_ref if str(event_ref).startswith("session_archive:") else event_id
        expected_path = f"sources/{_slug(path_key or event_id, default='event')}.md"
        for page in pages:
            if str(page.get("type") or "").lower() != "source":
                continue
            sources = set(_ensure_list(page.get("sources")))
            if event_ref in sources or event_id in sources:
                return True
            if str(page.get("path") or "") == expected_path:
                return True
            if event_id and event_id in str(page.get("content") or ""):
                return True
        return False

    def _source_summary_body(self, record: Dict[str, Any], title: str) -> str:
        payload = record.get("payload") or record.get("metadata")
        payload_text = json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, default=str)
        messages = record.get("messages")
        if isinstance(messages, list):
            message_text = json.dumps(sanitize_value(messages[-3:]), ensure_ascii=False, indent=2, default=str)
        else:
            message_text = "[]"
        source_refs = _ensure_list(record.get("source_refs"))
        content_preview = str(record.get("content") or "")
        lines = [
            f"# {title}",
            "",
            "## Event",
            f"- event_id: {redact_secrets(record.get('event_id') or '')}",
            f"- event_ref: {redact_secrets(record.get('event_ref') or '')}",
            f"- event_type: {redact_secrets(record.get('event_type') or '')}",
            f"- stage: {redact_secrets(record.get('stage') or '')}",
            f"- task_id: {redact_secrets(record.get('task_id') or '')}",
            f"- session_id: {redact_secrets(record.get('session_id') or '')}",
            f"- project_id: {redact_secrets(record.get('project_id') or '')}",
            f"- timestamp: {redact_secrets(record.get('timestamp') or '')}",
            f"- source_refs: {', '.join(source_refs) if source_refs else 'none'}",
            "",
            "## Summary",
            redact_secrets(record.get("summary") or "- None recorded."),
            "",
            "## Content Preview",
            "```text",
            _clip(content_preview, 1200),
            "```",
            "",
            "## Payload Preview",
            "```json",
            _clip(payload_text, 1200),
            "```",
            "",
            "## Recent Message Preview",
            "```json",
            _clip(message_text, 1200),
            "```",
        ]
        return "\n".join(lines)

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
        frontmatter.update(self._task_semantics_from_operation(operation))
        for key in ("entity_type", "concept_type", "definition"):
            if operation.get(key) is not None:
                frontmatter[key] = sanitize_value(operation.get(key))
        if operation.get("related") is not None:
            frontmatter["related"] = self._merge_lists(frontmatter.get("related"), operation.get("related"))
        extra_fm = operation.get("frontmatter")
        if isinstance(extra_fm, dict):
            frontmatter.update({str(key): sanitize_value(value) for key, value in extra_fm.items()})
            frontmatter["sources"] = self._merge_lists(frontmatter.get("sources"), sources)
        self._normalize_page_frontmatter(frontmatter)
        claims = self._operation_claims(operation)
        if path.exists():
            self._merge_page_task_semantics(path, operation, default_project_id)
            self._append_section_to_page(path, "Updates", content, sources=sources)
            if claims:
                self._upsert_page_claims(path, claims, default_project_id, fallback_evidence=sources)
            return
        if claims:
            frontmatter["claims"] = []
        body = f"# {title}\n\n{content.strip() or '- None recorded yet.'}\n"
        self._write_page(path, frontmatter, body)
        if claims:
            self._upsert_page_claims(path, claims, default_project_id, fallback_evidence=sources)
        self._append_log(f"Created wiki page: {self._relative_page_path(path)}")

    def _op_append_section(self, operation: Dict[str, Any], default_project_id: str) -> None:
        path = self._resolve_operation_path(operation)
        if not path.exists():
            title = str(operation.get("title") or path.stem.replace("-", " ").title())
            self._write_page(
                path,
                {
                    **self._default_frontmatter(
                    title=title,
                    page_type=str(operation.get("type") or "synthesis"),
                    project_id=str(operation.get("project_id") or default_project_id),
                    sources=_ensure_list(operation.get("sources")),
                    ),
                    **self._task_semantics_from_operation(operation),
                },
                f"# {title}\n",
            )
        else:
            self._merge_page_task_semantics(path, operation, default_project_id)
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

    def _op_upsert_claims(self, operation: Dict[str, Any], default_project_id: str) -> None:
        path = self._resolve_operation_path(operation)
        if not path.exists():
            title = str(operation.get("title") or path.stem.replace("-", " ").title())
            page_type = str(operation.get("type") or operation.get("page_type") or "synthesis")
            self._write_page(
                path,
                {
                    **self._default_frontmatter(
                    title=title,
                    page_type=page_type,
                    project_id=str(operation.get("project_id") or default_project_id),
                    sources=_ensure_list(operation.get("sources")),
                    ),
                    **self._task_semantics_from_operation(operation),
                },
                f"# {title}\n",
            )
        else:
            self._merge_page_task_semantics(path, operation, default_project_id)
        claims = self._operation_claims(operation)
        if not claims:
            raise ValueError("upsert_claims requires a claims list or claim object")
        self._upsert_page_claims(
            path,
            claims,
            str(operation.get("project_id") or default_project_id),
            fallback_evidence=_ensure_list(operation.get("sources")),
        )

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
        self._normalize_page_frontmatter(frontmatter)
        self._write_page(path, frontmatter, body)

    def _task_semantics_from_operation(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        raw_semantics = operation.get("task_semantics")
        direct = {
            key: operation.get(key)
            for key in TASK_SEMANTIC_FRONTMATTER_KEYS
            if operation.get(key) is not None
        }
        if operation.get("workflow") is not None and operation.get("workflow_id") is None:
            direct["workflow_id"] = operation.get("workflow")
        return self._merge_task_semantics(raw_semantics, direct)

    def _merge_page_task_semantics(self, path: Path, operation: Dict[str, Any], default_project_id: str) -> None:
        incoming = self._task_semantics_from_operation(operation)
        if not incoming:
            return
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        frontmatter, body = self._split_frontmatter(text)
        current = {
            key: frontmatter.get(key)
            for key in TASK_SEMANTIC_FRONTMATTER_KEYS
            if frontmatter.get(key) is not None
        }
        merged = self._merge_task_semantics(current, incoming)
        if not merged:
            return
        for key, value in merged.items():
            frontmatter[key] = value
        frontmatter["project_id"] = str(frontmatter.get("project_id") or default_project_id)
        frontmatter["updated_at"] = _now()
        self._normalize_page_frontmatter(frontmatter)
        self._write_page(path, frontmatter, body)

    @staticmethod
    def _normalize_page_frontmatter(frontmatter: Dict[str, Any]) -> None:
        if str(frontmatter.get("type") or "").lower() == "entity":
            entity_type = str(frontmatter.get("entity_type") or "other").strip().lower()
            frontmatter["entity_type"] = entity_type if entity_type in VALID_ENTITY_TYPES else "other"
        if str(frontmatter.get("type") or "").lower() == "concept":
            concept_type = str(frontmatter.get("concept_type") or "other").strip().lower()
            frontmatter["concept_type"] = concept_type if concept_type in VALID_CONCEPT_TYPES else "other"

    def _op_add_research_queries(self, operation: Dict[str, Any], default_project_id: str) -> None:
        queries = operation.get("queries")
        if isinstance(queries, dict):
            queries = [queries]
        elif queries is None:
            queries = operation.get("query")
            if queries is not None:
                queries = [{"query": queries}]
        if not isinstance(queries, list):
            raise ValueError("add_research_queries requires a queries list")

        existing_ids = self._research_query_ids()
        records: List[Dict[str, Any]] = []
        for raw_query in queries:
            if not isinstance(raw_query, dict):
                raw_query = {"query": raw_query}
            query = redact_secrets(raw_query.get("query") or "").strip()
            reason = redact_secrets(raw_query.get("reason") or "").strip()
            sources = _ensure_list(raw_query.get("sources") or operation.get("sources"))
            if not query or not reason or not sources:
                self._flag_review(
                    "Invalid Research Query",
                    "Research queries require query, reason, and at least one source.",
                    {"query": raw_query, "operation": operation},
                    default_project_id,
                )
                continue
            query_id = self._research_query_id(default_project_id, query)
            if query_id in existing_ids:
                continue
            existing_ids.add(query_id)
            records.append({
                "id": query_id,
                "project_id": _safe_id(default_project_id),
                "query": query,
                "reason": reason,
                "priority": self._normalize_priority(raw_query.get("priority")),
                "trigger": redact_secrets(raw_query.get("trigger") or "knowledge_gap").strip() or "knowledge_gap",
                "sources": sources,
                "related_pages": _ensure_list(raw_query.get("related_pages") or raw_query.get("pages")),
                "status": redact_secrets(raw_query.get("status") or "pending").strip().lower() or "pending",
                "created_at": _now(),
            })
        if not records:
            return
        with self.research_queries_file.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._append_log(f"Added {len(records)} wiki research query item(s).")

    def _research_query_ids(self) -> set[str]:
        if not self.research_queries_file.exists():
            return set()
        ids: set[str] = set()
        for line in self.research_queries_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("id"):
                ids.add(str(record["id"]))
        return ids

    def _research_query_count(self, project_id: Optional[str] = None) -> int:
        project_id = self._resolve_project_id(project_id)
        if not self.research_queries_file.exists():
            return 0
        count = 0
        for line in self.research_queries_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(record.get("project_id") or project_id) == project_id:
                count += 1
        return count

    @staticmethod
    def _research_query_id(project_id: str, query: str) -> str:
        digest = hashlib.sha256(f"{query.strip().lower()}\n{_safe_id(project_id)}".encode("utf-8")).hexdigest()
        return f"rq-{digest[:16]}"

    @staticmethod
    def _normalize_priority(value: Any) -> str:
        priority = str(value or "medium").strip().lower()
        return priority if priority in {"high", "medium", "low"} else "medium"

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

    @staticmethod
    def _operation_claims(operation: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_claims = operation.get("claims")
        if raw_claims is None:
            raw_claims = operation.get("claim")
        if raw_claims is None:
            return []
        if isinstance(raw_claims, dict):
            raw_claims = [raw_claims]
        if not isinstance(raw_claims, list):
            return []
        return [claim for claim in raw_claims if isinstance(claim, dict)]

    def _upsert_page_claims(
        self,
        path: Path,
        claims: Sequence[Dict[str, Any]],
        project_id: str,
        fallback_evidence: Optional[Sequence[str]] = None,
    ) -> int:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        frontmatter, body = self._split_frontmatter(text)
        frontmatter.setdefault("claims", [])
        existing_claims = frontmatter.get("claims")
        if not isinstance(existing_claims, list):
            existing_claims = []

        by_id: Dict[str, Dict[str, Any]] = {}
        ordered_ids: List[str] = []
        for existing in existing_claims:
            if not isinstance(existing, dict):
                continue
            claim_id = str(existing.get("id") or "").strip()
            if not claim_id:
                continue
            by_id[claim_id] = sanitize_value(existing)
            ordered_ids.append(claim_id)

        changed = 0
        rel_path = self._relative_page_path(path)
        for raw_claim in claims:
            normalized, review_reason = self._normalize_claim(raw_claim, rel_path, fallback_evidence)
            if review_reason:
                self._flag_review(
                    "Wiki Claim Requires Review",
                    review_reason,
                    {"path": rel_path, "claim": raw_claim},
                    project_id,
                )
                continue
            claim_id = str(normalized["id"])
            if claim_id not in by_id:
                ordered_ids.append(claim_id)
                changed += 1
            elif by_id[claim_id] != normalized:
                changed += 1
            by_id[claim_id] = normalized

        if changed:
            frontmatter["claims"] = [by_id[claim_id] for claim_id in ordered_ids if claim_id in by_id]
            frontmatter["updated_at"] = _now()
            self._write_page(path, frontmatter, body)
            self._append_log(f"Upserted {changed} claim(s) for {rel_path}")
        return changed

    def _normalize_claim(
        self,
        raw_claim: Dict[str, Any],
        page_path: str,
        fallback_evidence: Optional[Sequence[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        text = redact_secrets(raw_claim.get("text") or raw_claim.get("claim") or "").strip()
        if not text:
            return {}, "Wiki claim missing text."

        confidence = str(raw_claim.get("confidence") or "medium").strip().lower()
        if confidence == "low":
            return {}, "Low-confidence wiki claim must be reviewed before applying."
        if confidence not in VALID_CLAIM_CONFIDENCE:
            confidence = "medium"

        if raw_claim.get("review_required") or raw_claim.get("requires_review"):
            return {}, "Wiki claim was marked review_required."
        if raw_claim.get("conflict"):
            return {}, "Wiki claim was marked as conflicting."
        if raw_claim.get("stale"):
            return {}, "Wiki claim was marked as stale."

        raw_evidence = raw_claim.get("evidence")
        if raw_evidence is None:
            raw_evidence = raw_claim.get("sources")
        evidence = self._normalize_evidence(raw_evidence, fallback_evidence)
        if not evidence:
            return {}, "Wiki claim missing evidence."
        if not all(self._valid_evidence_ref(item) for item in evidence):
            return {}, "Wiki claim evidence must reference session_archive:, task:, artifact:, or a historical raw: source."

        claim_id = str(raw_claim.get("id") or "").strip()
        if not claim_id:
            claim_id = f"claim.{_slug(text, default='claim')}"

        normalized = {
            "id": redact_secrets(claim_id),
            "text": text,
            "status": redact_secrets(str(raw_claim.get("status") or "active").strip().lower() or "active"),
            "confidence": confidence,
            "evidence": evidence,
            "supersedes": _ensure_list(raw_claim.get("supersedes")),
            "review_required": False,
            "updated_at": str(raw_claim.get("updated_at") or _now()),
        }
        return normalized, ""

    def _normalize_evidence(self, raw_evidence: Any, fallback_evidence: Optional[Sequence[str]]) -> List[Any]:
        source = raw_evidence if raw_evidence not in (None, "") else fallback_evidence
        if source is None:
            return []
        if isinstance(source, dict):
            source = [source]
        elif not isinstance(source, list):
            source = [source]

        evidence: List[Any] = []
        seen: set[str] = set()
        for item in source:
            clean = sanitize_value(item)
            if isinstance(clean, dict):
                key = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    evidence.append(clean)
            else:
                value = redact_secrets(clean).strip()
                if value and value not in seen:
                    seen.add(value)
                    evidence.append(value)
        return evidence

    @staticmethod
    def _valid_evidence_ref(item: Any) -> bool:
        if isinstance(item, dict):
            ref = str(item.get("source_id") or item.get("source") or item.get("ref") or "").strip()
        else:
            ref = str(item).strip()
        return bool(ref) and ref.startswith(CLAIM_EVIDENCE_PREFIXES)

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
            "entity_type",
            "concept_type",
            "definition",
            "status",
            "created_at",
            "updated_at",
            "sources",
            "aliases",
            "tags",
            "related",
            "confidence",
            "task_id",
            "task_title",
            "task_type",
            "workflow",
            "workflow_id",
            "analysis_type",
            "task_date",
            "skills",
            "samples",
            "resolutions",
            "output_artifacts",
            "output_dirs",
            "claims",
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
        entity_count = len(groups.get("entity", []))
        concept_count = len(groups.get("concept", []))
        self.overview_file.write_text(
            "# Wiki Overview\n\n"
            f"Updated: {_now()}\n\n"
            f"Pages: {len(pages)}\n\n"
            f"Entities: {entity_count}\n\n"
            f"Concepts: {concept_count}\n\n"
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
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
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
                "entity_type": str(frontmatter.get("entity_type") or ""),
                "concept_type": str(frontmatter.get("concept_type") or ""),
                "definition": str(frontmatter.get("definition") or ""),
                "content": body,
                "sources": _ensure_list(frontmatter.get("sources")),
                "aliases": _ensure_list(frontmatter.get("aliases")),
                "tags": _ensure_list(frontmatter.get("tags")),
                "related": _ensure_list(frontmatter.get("related")),
                "task_id": str(frontmatter.get("task_id") or ""),
                "task_title": str(frontmatter.get("task_title") or ""),
                "task_type": str(frontmatter.get("task_type") or ""),
                "workflow": str(frontmatter.get("workflow") or frontmatter.get("workflow_id") or ""),
                "workflow_id": str(frontmatter.get("workflow_id") or frontmatter.get("workflow") or ""),
                "analysis_type": str(frontmatter.get("analysis_type") or ""),
                "task_date": str(frontmatter.get("task_date") or ""),
                "skills": _ensure_list(frontmatter.get("skills")),
                "samples": _ensure_list(frontmatter.get("samples")),
                "resolutions": _ensure_list(frontmatter.get("resolutions")),
                "output_artifacts": _ensure_list(frontmatter.get("output_artifacts")),
                "output_dirs": _ensure_list(frontmatter.get("output_dirs")),
                "claims": self._frontmatter_claims(frontmatter.get("claims")),
                "links": self._wikilinks(body),
                "content_hash": content_hash,
                "updated_at": str(frontmatter.get("updated_at") or ""),
            })
        return pages

    @staticmethod
    def _frontmatter_claims(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [claim for claim in value if isinstance(claim, dict)]

    def _flatten_claims(self, pages: Sequence[Dict[str, Any]], project_id: str) -> List[Dict[str, Any]]:
        flattened: List[Dict[str, Any]] = []
        for page in pages:
            for claim in self._frontmatter_claims(page.get("claims")):
                evidence = self._normalize_evidence(claim.get("evidence"), None)
                flattened.append({
                    "project_id": _safe_id(project_id),
                    "page": str(page.get("path") or ""),
                    "page_title": str(page.get("title") or ""),
                    "page_type": str(page.get("type") or ""),
                    "id": str(claim.get("id") or ""),
                    "text": redact_secrets(claim.get("text") or ""),
                    "status": str(claim.get("status") or ""),
                    "confidence": str(claim.get("confidence") or ""),
                    "evidence": evidence,
                    "supersedes": _ensure_list(claim.get("supersedes")),
                    "review_required": bool(claim.get("review_required")),
                    "updated_at": str(claim.get("updated_at") or page.get("updated_at") or ""),
                })
        flattened.sort(key=lambda item: (item["page"], item["id"]))
        return flattened

    def _review_count(self, project_id: str) -> int:
        if not self.review_pending.exists():
            return 0
        text = self.review_pending.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return 0
        wanted = f"- project_id: {_safe_id(project_id)}"
        blocks = re.split(r"(?=^## )", text, flags=re.MULTILINE)
        return sum(1 for block in blocks if block.startswith("## ") and wanted in block)

    @staticmethod
    def _heading_title(body: str) -> str:
        match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _search_page_text(page: Dict[str, Any]) -> str:
        return "\n".join([
            str(page.get("title") or ""),
            str(page.get("type") or ""),
            str(page.get("path") or ""),
            str(page.get("entity_type") or ""),
            str(page.get("concept_type") or ""),
            str(page.get("definition") or ""),
            str(page.get("task_id") or ""),
            str(page.get("task_title") or ""),
            str(page.get("task_type") or ""),
            str(page.get("workflow_id") or page.get("workflow") or ""),
            str(page.get("analysis_type") or ""),
            str(page.get("task_date") or ""),
            str(page.get("content") or ""),
            json.dumps(page.get("claims", []), ensure_ascii=False, default=str),
            " ".join(_ensure_list(page.get("sources"))),
            " ".join(_ensure_list(page.get("aliases"))),
            " ".join(_ensure_list(page.get("tags"))),
            " ".join(_ensure_list(page.get("related"))),
            " ".join(_ensure_list(page.get("links"))),
            " ".join(_ensure_list(page.get("skills"))),
            " ".join(_ensure_list(page.get("samples"))),
            " ".join(_ensure_list(page.get("resolutions"))),
            " ".join(_ensure_list(page.get("output_artifacts"))),
            " ".join(_ensure_list(page.get("output_dirs"))),
        ])

    def _upsert_search_page(self, conn: sqlite3.Connection, page: Dict[str, Any]) -> None:
        path = str(page.get("path") or "")
        if not path:
            return
        sources_json = json.dumps(_ensure_list(page.get("sources")), ensure_ascii=False)
        links_json = json.dumps(_ensure_list(page.get("links")), ensure_ascii=False)
        conn.execute(
            """
            INSERT OR REPLACE INTO wiki_pages
            (path, title, page_type, project_id, content, sources_json, links_json, content_hash, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                str(page.get("title") or ""),
                str(page.get("type") or ""),
                str(page.get("project_id") or ""),
                str(page.get("content") or ""),
                sources_json,
                links_json,
                str(page.get("content_hash") or ""),
                str(page.get("updated_at") or ""),
            ),
        )
        if self.fts_enabled:
            try:
                conn.execute("DELETE FROM wiki_pages_fts WHERE path = ?", (path,))
                conn.execute(
                    "INSERT INTO wiki_pages_fts(path, title, body, search_text) VALUES (?, ?, ?, ?)",
                    (
                        path,
                        str(page.get("title") or ""),
                        str(page.get("content") or ""),
                        self._search_page_text(page),
                    ),
                )
            except sqlite3.Error:
                self.fts_enabled = False

    def _delete_search_page(self, conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM wiki_pages WHERE path = ?", (path,))
        conn.execute("DELETE FROM wiki_embeddings WHERE path = ?", (path,))
        if self.fts_enabled:
            try:
                conn.execute("DELETE FROM wiki_pages_fts WHERE path = ?", (path,))
            except sqlite3.Error:
                self.fts_enabled = False

    def _sync_search_index(self) -> None:
        pages = self._all_pages(include_system=False)
        current = {
            str(page.get("path") or ""): page
            for page in pages
            if str(page.get("path") or "")
        }
        with self._connect() as conn:
            indexed = {
                str(row["path"]): str(row["content_hash"] or "")
                for row in conn.execute("SELECT path, content_hash FROM wiki_pages").fetchall()
            }
            for path, page in current.items():
                content_hash = str(page.get("content_hash") or "")
                if indexed.get(path) == content_hash:
                    continue
                self._upsert_search_page(conn, page)
            for path in sorted(set(indexed) - set(current)):
                self._delete_search_page(conn, path)
            conn.commit()

    def _rebuild_search_index(self) -> None:
        pages = self._all_pages(include_system=False)
        current_paths = {str(page.get("path") or "") for page in pages if str(page.get("path") or "")}
        with self._connect() as conn:
            conn.execute("DELETE FROM wiki_pages")
            if self.fts_enabled:
                try:
                    conn.execute("DELETE FROM wiki_pages_fts")
                except sqlite3.Error:
                    self.fts_enabled = False
            for page in pages:
                self._upsert_search_page(conn, page)
            for row in conn.execute("SELECT DISTINCT path FROM wiki_embeddings").fetchall():
                path = str(row["path"] or "")
                if path and path not in current_paths:
                    conn.execute("DELETE FROM wiki_embeddings WHERE path = ?", (path,))
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

    def _embedding_enabled(self) -> bool:
        enabled = self.embedding_config.get("enabled", False)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        return bool(enabled) and bool(self._embedding_api_key())

    def _embedding_api_key(self) -> str:
        return str(self.embedding_config.get("api_key") or "").strip()

    def _embedding_base_url(self) -> str:
        return str(self.embedding_config.get("base_url") or DEFAULT_EMBEDDING_BASE_URL).rstrip("/")

    def _embedding_model(self) -> str:
        return str(self.embedding_config.get("model") or DEFAULT_EMBEDDING_MODEL).strip()

    def _embedding_dimensions(self) -> int:
        try:
            dimensions = int(self.embedding_config.get("dimensions") or DEFAULT_EMBEDDING_DIMENSIONS)
        except (TypeError, ValueError):
            dimensions = DEFAULT_EMBEDDING_DIMENSIONS
        return max(1, dimensions)

    def _embedding_timeout(self) -> float:
        try:
            timeout = float(self.embedding_config.get("timeout") or 30)
        except (TypeError, ValueError):
            timeout = 30.0
        return max(1.0, timeout)

    def _embed_text(self, text: str) -> Optional[List[float]]:
        if not self._embedding_enabled() or not text.strip():
            return None
        payload = {
            "model": self._embedding_model(),
            "input": redact_secrets(text)[:12_000],
            "dimensions": self._embedding_dimensions(),
            "encoding_format": "float",
        }
        request = urllib.request.Request(
            f"{self._embedding_base_url()}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._embedding_api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._embedding_timeout()) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("Wiki embedding request failed; falling back to lexical search: %s", exc)
            return None
        vector = None
        if isinstance(data, dict):
            items = data.get("data")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                vector = items[0].get("embedding")
        if not isinstance(vector, list):
            logger.warning("Wiki embedding response did not include an embedding vector.")
            return None
        try:
            values = [float(item) for item in vector]
        except (TypeError, ValueError):
            logger.warning("Wiki embedding response included non-numeric vector values.")
            return None
        if len(values) != self._embedding_dimensions():
            logger.warning(
                "Wiki embedding dimension mismatch: expected %s, got %s.",
                self._embedding_dimensions(),
                len(values),
            )
            return None
        return values

    def _sync_page_embeddings(self, project_id: str, pages: Sequence[Dict[str, Any]]) -> int:
        project_id = self._resolve_project_id(project_id)
        if not self._embedding_enabled():
            return 0
        model = self._embedding_model()
        dimensions = self._embedding_dimensions()
        rows: Dict[str, str] = {}
        with self._connect() as conn:
            for row in conn.execute(
                """
                SELECT path, content_hash
                FROM wiki_embeddings
                WHERE project_id = ? AND model = ? AND dimensions = ?
                """,
                (project_id, model, dimensions),
            ).fetchall():
                rows[str(row["path"])] = str(row["content_hash"] or "")

        updated = 0
        for page in pages:
            path = str(page.get("path") or "")
            if not path:
                continue
            text = self._embedding_page_text(page)
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if rows.get(path) == content_hash:
                continue
            vector = self._embed_text(text)
            with self._connect() as conn:
                if vector is None:
                    conn.execute(
                        """
                        DELETE FROM wiki_embeddings
                        WHERE path = ? AND project_id = ? AND model = ? AND dimensions = ?
                        """,
                        (path, project_id, model, dimensions),
                    )
                    conn.commit()
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO wiki_embeddings
                    (path, project_id, model, dimensions, content_hash, vector_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        path,
                        project_id,
                        model,
                        dimensions,
                        content_hash,
                        json.dumps(vector, ensure_ascii=False),
                        _now(),
                    ),
                )
                conn.commit()
            updated += 1
        return updated

    def _vector_search(self, query: str, project_id: str, limit: int) -> List[WikiSearchHit]:
        project_id = self._resolve_project_id(project_id)
        if not query.strip() or not self._embedding_enabled():
            return []
        self._sync_page_embeddings(project_id, self._all_pages(project_id=project_id))
        query_vector = self._embed_text(query)
        if query_vector is None:
            return []
        rows: List[sqlite3.Row] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, e.vector_json
                FROM wiki_embeddings e
                JOIN wiki_pages p ON p.path = e.path
                WHERE e.project_id = ?
                  AND e.model = ?
                  AND e.dimensions = ?
                  AND (p.project_id = ? OR p.project_id = '')
                """,
                (project_id, self._embedding_model(), self._embedding_dimensions(), project_id),
            ).fetchall()
        hits: List[WikiSearchHit] = []
        for row in rows:
            try:
                vector = [float(item) for item in json.loads(row["vector_json"] or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            score = self._cosine_similarity(query_vector, vector)
            if score <= 0:
                continue
            hits.append(self._hit_from_row(row, score=score))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    @staticmethod
    def _rrf_fuse(rankings: Sequence[Sequence[WikiSearchHit]], limit: int) -> List[WikiSearchHit]:
        scores: Dict[str, float] = {}
        hits_by_path: Dict[str, WikiSearchHit] = {}
        for ranking in rankings:
            for index, hit in enumerate(ranking, start=1):
                if not hit.path:
                    continue
                scores[hit.path] = scores.get(hit.path, 0.0) + (1.0 / (RRF_K + index))
                hits_by_path.setdefault(hit.path, hit)
        fused: List[WikiSearchHit] = []
        for path, score in scores.items():
            hit = hits_by_path[path]
            fused.append(WikiSearchHit(
                path=hit.path,
                title=hit.title,
                page_type=hit.page_type,
                score=score * RRF_SCORE_SCALE,
                text=hit.text,
                sources=list(hit.sources),
            ))
        fused.sort(key=lambda hit: (-hit.score, hit.title.lower(), hit.path))
        return fused[:limit]

    @staticmethod
    def _embedding_page_text(page: Dict[str, Any]) -> str:
        return redact_secrets(WikiMemoryManager._search_page_text(page))

    @staticmethod
    def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if not left_norm or not right_norm:
            return 0.0
        return dot / (left_norm * right_norm)

    def _expand_hits(self, hits: List[WikiSearchHit], project_id: str, top_k: int) -> List[WikiSearchHit]:
        if not hits:
            return []
        by_path = {hit.path: hit for hit in hits}
        pages = self._all_pages(project_id=project_id)
        graph_scores = self._graph_related_scores(hits, pages)
        pages_by_path = {str(page.get("path") or ""): page for page in pages}
        community_bonus = self._community_expansion_bonus(hits, graph_scores, project_id)
        for path, graph_score in graph_scores.items():
            if graph_score < 1.5 or path in by_path:
                continue
            page = pages_by_path.get(path)
            if not page:
                continue
            by_path[path] = WikiSearchHit(
                path=path,
                title=str(page.get("title") or ""),
                page_type=str(page.get("type") or ""),
                score=graph_score + community_bonus.get(path, 0.0),
                text=_clip(str(page.get("content") or ""), 1200),
                sources=_ensure_list(page.get("sources")),
            )
        ordered = list(by_path.values())
        ordered.sort(key=lambda hit: hit.score, reverse=True)
        return ordered[: max(top_k, len(hits))]

    def _graph_related_scores(
        self,
        seed_hits: Sequence[WikiSearchHit],
        pages: Sequence[Dict[str, Any]],
    ) -> Dict[str, float]:
        seed_paths = {hit.path for hit in seed_hits}
        if not seed_paths:
            return {}

        pages_by_path = {str(page.get("path") or ""): page for page in pages}
        neighbors = self._page_neighbors(pages)
        outlinks = self._page_outlinks(pages)
        scores: Dict[str, float] = {}
        for candidate_path, candidate in pages_by_path.items():
            if not candidate_path or candidate_path in seed_paths:
                continue

            candidate_sources = set(_ensure_list(candidate.get("sources")))
            candidate_type = str(candidate.get("type") or "")
            candidate_neighbors = neighbors.get(candidate_path, set())

            direct_signal = 0.0
            source_signal = 0.0
            adamic_adar_signal = 0.0
            type_signal = 0.0
            semantic_signal = 0.0

            for seed_path in seed_paths:
                seed = pages_by_path.get(seed_path)
                if not seed:
                    continue
                seed_neighbors = neighbors.get(seed_path, set())

                direct_signal_pair = self._direct_link_signal(seed_path, candidate_path, outlinks)
                if direct_signal_pair:
                    direct_signal = max(direct_signal, direct_signal_pair)

                shared_sources = set(_ensure_list(seed.get("sources"))) & candidate_sources
                if shared_sources:
                    source_signal = max(source_signal, min(1.0, len(shared_sources) / 2.0))

                common_neighbors = seed_neighbors & candidate_neighbors
                if common_neighbors:
                    aa = 0.0
                    for neighbor in common_neighbors:
                        degree = len(neighbors.get(neighbor, set()))
                        if degree > 0:
                            aa += 1.0 / math.log(degree + 1.0)
                    adamic_adar_signal = max(adamic_adar_signal, aa)

                type_signal = max(type_signal, self._type_affinity(str(seed.get("type") or ""), candidate_type))
                semantic_signal = max(semantic_signal, self._task_semantic_signal(seed, candidate))

            score = (
                direct_signal * 3.0
                + source_signal * 4.0
                + adamic_adar_signal * 1.5
                + type_signal
                + semantic_signal
            )
            if score > 0:
                scores[candidate_path] = score
        return scores

    def _community_expansion_bonus(
        self,
        seed_hits: Sequence[WikiSearchHit],
        graph_scores: Dict[str, float],
        project_id: str,
    ) -> Dict[str, float]:
        if not seed_hits or not graph_scores:
            return {}
        try:
            communities = self.read_communities(project_id)
        except Exception as exc:
            logger.debug("Unable to read wiki communities for expansion bonus: %s", exc)
            return {}
        page_metrics = communities.get("pages") if isinstance(communities, dict) else {}
        community_items = communities.get("communities") if isinstance(communities, dict) else []
        if not isinstance(page_metrics, dict) or not isinstance(community_items, list):
            return {}
        cohesion_by_id = {
            str(item.get("community_id") or ""): float(item.get("cohesion") or 0.0)
            for item in community_items
            if isinstance(item, dict)
        }
        seed_communities = set()
        for hit in seed_hits:
            info = page_metrics.get(hit.path)
            if not isinstance(info, dict):
                continue
            community_id = str(info.get("community_id") or "")
            if community_id and cohesion_by_id.get(community_id, 0.0) >= 0.15:
                seed_communities.add(community_id)
        if not seed_communities:
            return {}
        bonuses: Dict[str, float] = {}
        for path, graph_score in graph_scores.items():
            if graph_score < 1.5:
                continue
            info = page_metrics.get(path)
            if not isinstance(info, dict):
                continue
            community_id = str(info.get("community_id") or "")
            if community_id in seed_communities and cohesion_by_id.get(community_id, 0.0) >= 0.15:
                bonuses[path] = 0.75
        return bonuses

    def _page_graph_edges(self, pages: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], float]:
        graph_pages = [
            page for page in pages
            if str(page.get("type") or "").lower() != "query" and str(page.get("path") or "")
        ]
        pages_by_path = {str(page.get("path") or ""): page for page in graph_pages}
        paths = sorted(pages_by_path)
        neighbors = self._page_neighbors(graph_pages)
        outlinks = self._page_outlinks(graph_pages)
        edges: Dict[Tuple[str, str], float] = {}
        for index, left_path in enumerate(paths):
            for right_path in paths[index + 1:]:
                score = self._page_pair_graph_score(
                    left_path,
                    right_path,
                    pages_by_path,
                    neighbors,
                    outlinks,
                )
                if score >= 1.5:
                    edges[(left_path, right_path)] = round(score, 6)
        return edges

    def _page_pair_graph_score(
        self,
        left_path: str,
        right_path: str,
        pages_by_path: Dict[str, Dict[str, Any]],
        neighbors: Dict[str, set[str]],
        outlinks: Dict[str, set[str]],
    ) -> float:
        left = pages_by_path.get(left_path)
        right = pages_by_path.get(right_path)
        if not left or not right:
            return 0.0
        direct_signal = self._direct_link_signal(left_path, right_path, outlinks)
        shared_sources = set(_ensure_list(left.get("sources"))) & set(_ensure_list(right.get("sources")))
        source_signal = min(1.0, len(shared_sources) / 2.0) if shared_sources else 0.0
        adamic_adar_signal = self._adamic_adar_signal(
            neighbors.get(left_path, set()) & neighbors.get(right_path, set()),
            neighbors,
        )
        type_signal = self._type_affinity(str(left.get("type") or ""), str(right.get("type") or ""))
        semantic_signal = self._task_semantic_signal(left, right)
        return (
            direct_signal * 3.0
            + source_signal * 4.0
            + adamic_adar_signal * 1.5
            + type_signal
            + semantic_signal
        )

    @staticmethod
    def _task_semantic_signal(left: Dict[str, Any], right: Dict[str, Any]) -> float:
        score = 0.0

        scalar_weights = {
            "task_id": 1.4,
            "task_type": 0.6,
            "workflow_id": 1.1,
            "analysis_type": 0.8,
        }
        for key, weight in scalar_weights.items():
            left_value = WikiMemoryManager._semantic_scalar(left, key)
            right_value = WikiMemoryManager._semantic_scalar(right, key)
            if left_value and left_value == right_value:
                score += weight

        list_weights = {
            "skills": (0.8, 1.6),
            "samples": (0.6, 1.2),
            "resolutions": (0.5, 0.8),
            "output_artifacts": (1.2, 1.8),
            "output_dirs": (0.8, 1.2),
        }
        for key, (per_item, cap) in list_weights.items():
            overlap = WikiMemoryManager._semantic_overlap(left.get(key), right.get(key))
            if overlap:
                score += min(cap, per_item * len(overlap))

        return min(4.0, score)

    @staticmethod
    def _semantic_scalar(page: Dict[str, Any], key: str) -> str:
        if key == "workflow_id":
            value = page.get("workflow_id") or page.get("workflow")
        else:
            value = page.get(key)
        return redact_secrets(value or "").strip().lower()

    @staticmethod
    def _semantic_overlap(left: Any, right: Any) -> set[str]:
        left_values = {item.lower() for item in _ensure_list(left)}
        right_values = {item.lower() for item in _ensure_list(right)}
        return {item for item in left_values & right_values if item}

    def _detect_page_communities(
        self,
        pages: Sequence[Dict[str, Any]],
        edges: Dict[Tuple[str, str], float],
    ) -> Dict[str, Any]:
        pages_by_path = {
            str(page.get("path") or ""): page
            for page in pages
            if str(page.get("path") or "")
        }
        paths = sorted(pages_by_path)
        adjacency = self._edge_adjacency(paths, edges)
        raw_communities: List[set[str]] = []
        algorithm = "connected_components"
        if paths:
            try:
                import networkx as nx  # type: ignore[import-not-found]
                from networkx.algorithms.community import louvain_communities  # type: ignore[import-not-found]

                graph = nx.Graph()
                graph.add_nodes_from(paths)
                for (left, right), weight in edges.items():
                    graph.add_edge(left, right, weight=float(weight))
                if edges:
                    raw_communities = [
                        {str(path) for path in community}
                        for community in louvain_communities(graph, weight="weight", seed=0)
                    ]
                else:
                    raw_communities = [{path} for path in paths]
                algorithm = "networkx_louvain"
            except Exception as exc:
                logger.debug("Falling back from Louvain community detection: %s", exc)
                raw_communities = self._connected_components(paths, adjacency)
        if not raw_communities and paths:
            raw_communities = self._connected_components(paths, adjacency)

        known_paths = set(paths)
        communities = [set(community) & known_paths for community in raw_communities if community]
        assigned = set().union(*communities) if communities else set()
        for path in sorted(known_paths - assigned):
            communities.append({path})
        communities = [community for community in communities if community]
        communities.sort(key=lambda community: (-len(community), sorted(community)[0]))

        community_by_path: Dict[str, str] = {}
        for index, members in enumerate(communities, start=1):
            community_id = f"c{index:03d}"
            for path in members:
                community_by_path[path] = community_id

        degree = {path: len(adjacency.get(path, {})) for path in paths}
        weighted_degree = {
            path: round(sum(adjacency.get(path, {}).values()), 6)
            for path in paths
        }
        page_records: Dict[str, Dict[str, Any]] = {}
        for path in paths:
            page = pages_by_path[path]
            own_community = community_by_path.get(path, "")
            neighbor_communities = sorted({
                community_by_path.get(neighbor, "")
                for neighbor in adjacency.get(path, {})
                if community_by_path.get(neighbor, "") and community_by_path.get(neighbor, "") != own_community
            })
            page_records[path] = {
                "community_id": own_community,
                "title": str(page.get("title") or ""),
                "type": str(page.get("type") or ""),
                "degree": degree[path],
                "weighted_degree": weighted_degree[path],
                "bridge_score": len(neighbor_communities),
                "neighbor_communities": neighbor_communities,
            }

        community_records: List[Dict[str, Any]] = []
        for index, members in enumerate(communities, start=1):
            community_id = f"c{index:03d}"
            cohesion = self._community_cohesion(members, edges)
            type_counts: Dict[str, int] = {}
            for path in members:
                page_type = str(pages_by_path[path].get("type") or "unknown")
                type_counts[page_type] = type_counts.get(page_type, 0) + 1
            warnings: List[str] = []
            if len(members) == 1 and degree[next(iter(members))] == 0:
                warnings.append("isolated_page")
            if len(members) >= 3 and cohesion < 0.15:
                warnings.append("low_cohesion")
            core_paths = sorted(
                members,
                key=lambda path: (
                    1 if str(pages_by_path[path].get("type") or "").lower() in {"source", "query"} else 0,
                    -weighted_degree[path],
                    -degree[path],
                    str(pages_by_path[path].get("title") or "").lower(),
                    path,
                ),
            )[:3]
            community_records.append({
                "community_id": community_id,
                "size": len(members),
                "cohesion": round(cohesion, 6),
                "type_counts": dict(sorted(type_counts.items())),
                "core_pages": [
                    {
                        "path": path,
                        "title": str(pages_by_path[path].get("title") or ""),
                        "type": str(pages_by_path[path].get("type") or ""),
                        "weighted_degree": weighted_degree[path],
                    }
                    for path in core_paths
                ],
                "warnings": warnings,
            })

        community_records.sort(
            key=lambda item: (-int(item.get("size") or 0), str(item.get("community_id") or ""))
        )
        return {
            "algorithm": algorithm,
            "communities": community_records,
            "pages": page_records,
        }

    @staticmethod
    def _edge_adjacency(
        paths: Sequence[str],
        edges: Dict[Tuple[str, str], float],
    ) -> Dict[str, Dict[str, float]]:
        adjacency: Dict[str, Dict[str, float]] = {path: {} for path in paths}
        for (left, right), weight in edges.items():
            if left not in adjacency or right not in adjacency:
                continue
            adjacency[left][right] = float(weight)
            adjacency[right][left] = float(weight)
        return adjacency

    @staticmethod
    def _connected_components(
        paths: Sequence[str],
        adjacency: Dict[str, Dict[str, float]],
    ) -> List[set[str]]:
        components: List[set[str]] = []
        seen: set[str] = set()
        for path in sorted(paths):
            if path in seen:
                continue
            stack = [path]
            component: set[str] = set()
            seen.add(path)
            while stack:
                current = stack.pop()
                component.add(current)
                for neighbor in sorted(adjacency.get(current, {})):
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    stack.append(neighbor)
            components.append(component)
        return components

    @staticmethod
    def _community_cohesion(
        members: Iterable[str],
        edges: Dict[Tuple[str, str], float],
    ) -> float:
        member_set = set(members)
        size = len(member_set)
        if size < 2:
            return 0.0
        internal_edges = sum(
            1 for left, right in edges
            if left in member_set and right in member_set
        )
        possible_edges = size * (size - 1) / 2.0
        return internal_edges / possible_edges if possible_edges else 0.0

    @staticmethod
    def _adamic_adar_signal(
        common_neighbors: Iterable[str],
        neighbors: Dict[str, set[str]],
    ) -> float:
        score = 0.0
        for neighbor in common_neighbors:
            degree = len(neighbors.get(neighbor, set()))
            if degree > 0:
                score += 1.0 / math.log(degree + 1.0)
        return score

    def _page_neighbors(self, pages: Sequence[Dict[str, Any]]) -> Dict[str, set[str]]:
        outlinks = self._page_outlinks(pages)
        neighbors: Dict[str, set[str]] = {
            str(page.get("path") or ""): set(outlinks.get(str(page.get("path") or ""), set()))
            for page in pages
        }
        for source_path, targets in outlinks.items():
            for target_path in targets:
                if not target_path or target_path == source_path:
                    continue
                neighbors.setdefault(source_path, set()).add(target_path)
                neighbors.setdefault(target_path, set()).add(source_path)
        return neighbors

    def _page_outlinks(self, pages: Sequence[Dict[str, Any]]) -> Dict[str, set[str]]:
        lookup = self._link_lookup(pages)
        outlinks: Dict[str, set[str]] = {str(page.get("path") or ""): set() for page in pages}
        for page in pages:
            source_path = str(page.get("path") or "")
            if not source_path:
                continue
            for link in self._wikilinks(str(page.get("content") or "")):
                target = lookup.get(self._normalize_link(link))
                if not target:
                    continue
                target_path = str(target.get("path") or "")
                if not target_path or target_path == source_path:
                    continue
                outlinks.setdefault(source_path, set()).add(target_path)
            for related in _ensure_list(page.get("related")):
                target = lookup.get(self._normalize_link(related))
                if not target:
                    continue
                target_path = str(target.get("path") or "")
                if not target_path or target_path == source_path:
                    continue
                outlinks.setdefault(source_path, set()).add(target_path)
        return outlinks

    @staticmethod
    def _direct_link_signal(
        left_path: str,
        right_path: str,
        outlinks: Dict[str, set[str]],
    ) -> float:
        left_links_right = right_path in outlinks.get(left_path, set())
        right_links_left = left_path in outlinks.get(right_path, set())
        if left_links_right and right_links_left:
            return 1.5
        if left_links_right or right_links_left:
            return 1.0
        return 0.0

    @staticmethod
    def _type_affinity(left_type: str, right_type: str) -> float:
        left = str(left_type or "").lower()
        right = str(right_type or "").lower()
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        pair = frozenset({left, right})
        affinity = {
            frozenset({"dataset", "method"}): 0.8,
            frozenset({"entity", "concept"}): 0.9,
            frozenset({"entity", "source"}): 0.8,
            frozenset({"concept", "method"}): 0.8,
            frozenset({"concept", "decision"}): 0.7,
            frozenset({"concept", "source"}): 0.7,
            frozenset({"entity", "dataset"}): 0.6,
            frozenset({"entity", "method"}): 0.6,
            frozenset({"method", "decision"}): 0.8,
            frozenset({"issue", "method"}): 0.7,
            frozenset({"artifact", "dataset"}): 0.6,
            frozenset({"artifact", "method"}): 0.6,
            frozenset({"source", "dataset"}): 0.5,
            frozenset({"source", "method"}): 0.5,
            frozenset({"source", "decision"}): 0.5,
        }
        return affinity.get(pair, 0.0)

    def _link_lookup(self, pages: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        lookup: Dict[str, Dict[str, Any]] = {}
        for page in pages:
            keys = {
                page["path"],
                page["path"].removesuffix(".md"),
                page["title"],
                Path(page["path"]).stem,
            }
            keys.update(_ensure_list(page.get("aliases")))
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
