import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from memory.session_archive import SessionArchive
from memory.wiki_memory import WikiMemoryManager, WikiSearchHit


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.responses:
            return _Response(self.responses.pop(0))
        return _Response("{}")


def _minimal_analysis(**overrides):
    data = {
        "source_summary": {
            "title": "Test source",
            "source_type": "conversation",
            "main_topic": "test",
            "one_sentence_summary": "Test source summary.",
        },
        "entities": [],
        "concepts": [],
        "claims": [],
        "conflicts": [],
        "wiki_patch_plan": [],
        "research_queries": [],
    }
    data.update(overrides)
    return data


def _new_archive(manager: WikiMemoryManager) -> SessionArchive:
    return SessionArchive(manager.root / "session_archive.sqlite3", max_inline_chars=manager.max_inline_chars)


def _append_archive_event(
    archive: SessionArchive,
    manager: WikiMemoryManager,
    task_id: str,
    stage: str,
    event_type: str,
    payload=None,
    messages=None,
    project_id=None,
    source_refs=None,
) -> int:
    payload = dict(payload or {})
    messages = messages or []
    semantics = manager.infer_task_semantics(task_id, stage=stage, messages=messages, payload=payload)
    if semantics:
        payload["task_semantics"] = semantics
    content = "\n\n".join([
        *(str(getattr(message, "content", message)) for message in messages[-4:]),
        "payload: " + json.dumps(payload, ensure_ascii=False, default=str),
    ])
    return archive.append_event(
        task_id=task_id,
        session_id=task_id,
        project_id=project_id or manager.default_project_id,
        stage=stage,
        event_type=event_type,
        content=content,
        summary=str(payload.get("summary") or content[:800]),
        metadata=payload,
        task_semantics=semantics,
        source_refs=source_refs or [f"task:{task_id}"],
    )


class WikiMemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_layout_and_archive_large_output_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".biomind" / "memory"
            manager = WikiMemoryManager(None, root, max_inline_chars=80)
            archive = _new_archive(manager)

            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_1",
                stage="execute",
                event_type="execution_finished",
                payload={
                    "api_key": "sk-1234567890abcdef",
                    "stdout": "A" * 200,
                },
            )

            self.assertTrue((root / "purpose.md").exists())
            self.assertTrue((root / "schema.md").exists())
            self.assertTrue((root / "wiki" / "index.md").exists())
            self.assertFalse((root / "raw" / "pending_wiki.jsonl").exists())

            event = archive.recent_events(task_id="session_1", limit=1)[0]
            self.assertEqual(event.id, event_id)
            self.assertEqual(event.project_id, "default")
            self.assertIn("sk-[REDACTED]", json.dumps(event.metadata, ensure_ascii=False))
            self.assertNotIn("sk-1234567890abcdef", json.dumps(event.metadata, ensure_ascii=False))
            self.assertIn("large_text_ref", json.dumps(event.metadata, ensure_ascii=False))
            ref_path = Path(event.metadata["stdout"]["path"])
            self.assertTrue(ref_path.exists())
            self.assertEqual(ref_path.read_text(encoding="utf-8"), "A" * 200)
            self.assertEqual(len(archive.pending_for_wiki("default")), 1)

            purpose = (root / "purpose.md").read_text(encoding="utf-8")
            self.assertIn("bioinformatics analysis agent", purpose)
            self.assertIn("not a generic chat summary", purpose)
            self.assertIn("What Belongs In This Wiki", purpose)
            self.assertIn("Evidence Policy", purpose)
            self.assertIn("Core stores user preferences", purpose)

    async def test_patch_appends_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Initial method note.",
                            "sources": ["raw:event-1"],
                        },
                        {
                            "op": "merge_section_items",
                            "path": "methods/alpha.md",
                            "section": "Findings",
                            "items": ["Use SCTransform normalization."],
                            "sources": ["raw:event-2"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Second note should append, not overwrite.",
                            "sources": ["raw:event-3"],
                        },
                    ]
                },
                project_id=project_id,
            )

            page = (Path(tmpdir) / "memory" / "wiki" / "methods" / "alpha.md").read_text(encoding="utf-8")
            self.assertIn("Initial method note.", page)
            self.assertIn("Use SCTransform normalization.", page)
            self.assertIn("Second note should append", page)
            self.assertIn('"raw:event-1"', page)
            self.assertIn('"raw:event-2"', page)
            self.assertIn('"raw:event-3"', page)

    async def test_prompt_context_includes_wiki_purpose(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")

            context = manager.build_prompt_context("session_1", "project_alpha", "unmatched query")

            self.assertIn("<wiki_purpose>", context)
            self.assertIn("## Mission", context)
            self.assertIn("## Current Questions", context)
            self.assertIn("bioinformatics analysis agent", context)

    async def test_create_page_accepts_claims_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "decisions/dmr-direction.md",
                            "title": "DMR Direction",
                            "type": "decision",
                            "content": "DMR direction convention.",
                            "sources": ["raw:event-1"],
                            "claims": [
                                {
                                    "id": "claim.dmr.direction",
                                    "text": "Negative meth_diff means the treatment group is more methylated.",
                                    "confidence": "high",
                                    "evidence": ["raw:event-1"],
                                }
                            ],
                        }
                    ]
                },
                project_id=project_id,
            )

            page = manager.read_page("decisions/dmr-direction.md")
            frontmatter, _ = manager._split_frontmatter(page)
            self.assertEqual(frontmatter["claims"][0]["id"], "claim.dmr.direction")
            self.assertEqual(frontmatter["claims"][0]["evidence"], ["raw:event-1"])
            self.assertIn('"confidence": "high"', page)

    async def test_upsert_claims_updates_by_id_and_appends_new_claims(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "upsert_claims",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "claims": [
                                {
                                    "id": "claim.alpha",
                                    "text": "Use the alpha workflow.",
                                    "evidence": ["raw:event-1"],
                                }
                            ],
                        }
                    ]
                },
                project_id=project_id,
            )

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "upsert_claims",
                            "path": "methods/alpha.md",
                            "claims": [
                                {
                                    "id": "claim.alpha",
                                    "text": "Use the revised alpha workflow.",
                                    "evidence": ["session_archive:42"],
                                },
                                {
                                    "id": "claim.beta",
                                    "text": "Use beta validation after alpha.",
                                    "evidence": ["task:session_1"],
                                },
                            ],
                        }
                    ]
                },
                project_id=project_id,
            )

            frontmatter, _ = manager._split_frontmatter(manager.read_page("methods/alpha.md"))
            claims = {claim["id"]: claim for claim in frontmatter["claims"]}
            self.assertEqual(len(claims), 2)
            self.assertEqual(claims["claim.alpha"]["text"], "Use the revised alpha workflow.")
            self.assertEqual(claims["claim.beta"]["evidence"], ["task:session_1"])

    async def test_invalid_claims_are_sent_to_review_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "upsert_claims",
                            "path": "decisions/review.md",
                            "title": "Review Claims",
                            "claims": [
                                {"id": "claim.no_evidence", "text": "Missing evidence."},
                                {
                                    "id": "claim.low",
                                    "text": "Low confidence.",
                                    "confidence": "low",
                                    "evidence": ["raw:event-1"],
                                },
                                {
                                    "id": "claim.stale",
                                    "text": "Stale fact.",
                                    "stale": True,
                                    "evidence": ["raw:event-2"],
                                },
                            ],
                        }
                    ]
                },
                project_id="project_alpha",
            )

            review = manager.read_review_queue()
            self.assertIn("Wiki claim missing evidence", review)
            self.assertIn("Low-confidence wiki claim", review)
            self.assertIn("marked as stale", review)

    async def test_compile_digest_writes_project_scoped_digest_and_claims_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Alpha method.",
                            "claims": [
                                {
                                    "id": "claim.alpha",
                                    "text": "Alpha is project specific.",
                                    "confidence": "high",
                                    "evidence": ["artifact:/tmp/alpha.txt"],
                                }
                            ],
                        }
                    ]
                },
                project_id="project_alpha",
            )
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/beta.md",
                            "title": "Beta Method",
                            "type": "method",
                            "content": "Beta method.",
                            "claims": [
                                {
                                    "id": "claim.beta",
                                    "text": "Beta is project specific.",
                                    "confidence": "high",
                                    "evidence": ["raw:event-beta"],
                                }
                            ],
                        }
                    ]
                },
                project_id="project_beta",
            )

            result = manager.compile_digest("project_alpha")
            digest = json.loads(manager.digest_path("project_alpha").read_text(encoding="utf-8"))
            claim_lines = manager.claims_index_path("project_alpha").read_text(encoding="utf-8").splitlines()
            project_yaml = (Path(tmpdir) / "memory" / "projects" / "project_alpha.yaml").read_text(encoding="utf-8")

            self.assertIn("Wiki digest compiled", result)
            self.assertEqual(digest["project_id"], "project_alpha")
            self.assertEqual(digest["claim_count"], 1)
            self.assertIn("claim.alpha", claim_lines[0])
            self.assertNotIn("claim.beta", "\n".join(claim_lines))
            self.assertIn("wiki_digest_path", project_yaml)
            self.assertIn("last_compiled_at", project_yaml)
            self.assertIn("methods/alpha.md", project_yaml)
            self.assertIn("community_count", digest)
            self.assertIn("top_communities", digest)
            self.assertTrue(manager.community_path("project_alpha").exists())

    async def test_wiki_digest_sync_removes_non_index_core_project_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            manager = WikiMemoryManager(None, root)
            project_yaml = root / "projects" / "project_alpha.yaml"
            project_yaml.parent.mkdir(parents=True, exist_ok=True)
            project_yaml.write_text(
                "project_id: project_alpha\n"
                "updated_at: old\n"
                "facts:\n"
                "  project_root: \"/data/project_alpha\"\n"
                "  paths.gff: \"/data/project_alpha.gff3\"\n",
                encoding="utf-8",
            )
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "datasets/alpha.md",
                            "title": "Alpha Dataset",
                            "type": "dataset",
                            "content": "Alpha dataset summary.",
                            "claims": [
                                {
                                    "id": "claim.alpha.gff",
                                    "text": "The Alpha GFF is /data/project_alpha.gff3.",
                                    "confidence": "high",
                                    "evidence": ["raw:event-1"],
                                }
                            ],
                        }
                    ]
                },
                project_id="project_alpha",
            )

            project_yaml_text = project_yaml.read_text(encoding="utf-8")
            self.assertIn("project_root", project_yaml_text)
            self.assertIn("wiki_digest_path", project_yaml_text)
            self.assertIn("active_dataset_pages", project_yaml_text)
            self.assertNotIn("paths.gff", project_yaml_text)

    async def test_wiki_lint_reports_invalid_existing_claims(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "decisions/manual.md",
                            "title": "Manual Claims",
                            "type": "decision",
                            "content": "Manual claim page.",
                            "sources": ["raw:event-1"],
                            "frontmatter": {
                                "claims": [
                                    {
                                        "id": "claim.manual",
                                        "text": "Manual claim without evidence.",
                                        "confidence": "high",
                                        "evidence": [],
                                    }
                                ]
                            },
                        }
                    ]
                },
                project_id="project_alpha",
            )

            result = manager.lint("project_alpha")

            self.assertIn("Wiki claim missing evidence", result)

    async def test_path_traversal_is_rejected_to_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")

            applied = manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "../outside.md",
                            "title": "Unsafe",
                            "content": "Should not be written.",
                        }
                    ]
                },
                project_id="project_alpha",
            )

            self.assertEqual(applied, 0)
            self.assertFalse((Path(tmpdir) / "outside.md").exists())
            review = manager.read_review_queue()
            self.assertIn("Unsafe wiki page path", review)

    async def test_archive_append_is_idempotent_by_event_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            archive = _new_archive(manager)

            first = _append_archive_event(
                archive,
                manager,
                task_id="session_2",
                project_id="project_alpha",
                stage="planner",
                event_type="planner_event",
                payload={"plan": "Run one step."},
            )
            second = _append_archive_event(
                archive,
                manager,
                task_id="session_2",
                project_id="project_alpha",
                stage="planner",
                event_type="planner_event",
                payload={"plan": "Run one step."},
            )

            self.assertEqual(first, second)
            self.assertEqual(len(archive.pending_for_wiki("project_alpha")), 1)

    async def test_ingest_from_archive_applies_llm_patch_and_marks_processed(self):
        patch = {
            "operations": [
                {
                    "op": "create_page",
                    "path": "decisions/use-wiki.md",
                    "title": "Use Wiki Memory",
                    "type": "decision",
                    "content": "BioMind should use a source-traceable wiki layer.",
                    "sources": ["session_archive:placeholder"],
                }
            ],
            "ingest_summary": "Added wiki memory decision.",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_3",
                stage="finalize",
                event_type="task_finalized",
                payload={"summary": "Use wiki memory."},
                project_id="project_alpha",
            )
            patch["operations"][0]["sources"] = [f"session_archive:{event_id}"]

            result = await manager.ingest_from_archive(archive)

            self.assertIn("Wiki ingest completed.", result)
            self.assertEqual(archive.pending_for_wiki("project_alpha"), [])
            page = manager.read_page("decisions/use-wiki.md")
            self.assertIn("source-traceable wiki layer", page)

    async def test_ingest_clips_large_archive_events_before_llm_call(self):
        patch = {"operations": [{"op": "append_log", "message": "ok"}]}
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            _append_archive_event(
                archive,
                manager,
                task_id="session_large",
                stage="execute",
                event_type="execution_finished",
                payload={"summary": "large numeric output", "values": list(range(100_000))},
                project_id="project_alpha",
            )

            result = await manager.ingest_from_archive(archive)

            analysis_prompt = manager.llm.calls[0][0].content
            self.assertIn("Wiki ingest completed.", result)
            self.assertLess(len(analysis_prompt), 120_000)
            self.assertIn("metadata_json_preview", analysis_prompt)

    async def test_ingest_llm_error_returns_message_and_keeps_events_pending(self):
        class FailingLLM:
            async def ainvoke(self, messages):
                raise RuntimeError("input length should be [1, 258048]")

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                FailingLLM(),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_llm_error",
                stage="finalize",
                event_type="task_finalized",
                payload={"summary": "Needs later ingest."},
                project_id="project_alpha",
            )

            result = await manager.ingest_from_archive(archive)

            self.assertIn("Wiki ingest failed during analysis LLM call", result)
            self.assertEqual([event.id for event in archive.pending_for_wiki("project_alpha")], [event_id])
            self.assertIn("Wiki Ingest LLM Error", manager.read_review_queue())

    async def test_ingest_prompt_reads_purpose_and_requires_purpose_review(self):
        patch = {"operations": [{"op": "append_log", "message": "ok"}]}
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            _append_archive_event(
                archive,
                manager,
                task_id="session_purpose",
                stage="planner",
                event_type="planner_event",
                payload={"summary": "A new project question emerged."},
                project_id="project_alpha",
            )

            await manager.ingest_from_archive(archive)

            analysis_prompt = manager.llm.calls[0][0].content
            patch_prompt = manager.llm.calls[1][0].content
            self.assertIn("Purpose:", analysis_prompt)
            self.assertIn("## Mission", analysis_prompt)
            self.assertIn("## Current Questions", analysis_prompt)
            self.assertIn("bioinformatics analysis agent", analysis_prompt)
            self.assertIn("Do not update purpose.md directly", patch_prompt)
            self.assertIn("Purpose Update Suggestion", patch_prompt)

    async def test_schema_defines_page_type_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")

            schema = manager.schema_file.read_text(encoding="utf-8")

            self.assertIn("## Page Type Boundaries", schema)
            self.assertIn("tool = a concrete software package", schema)
            self.assertIn("method = an analysis method", schema)
            self.assertIn("dataset = a named dataset", schema)
            self.assertIn("entity = a biological or project object", schema)
            self.assertIn("concept = an abstract biological or analytical idea", schema)
            self.assertIn("Prefer the concrete page type first", schema)
            self.assertIn("related[] instead of duplicating their content", schema)
            self.assertIn("## Skill Selection Memory Rules", schema)
            self.assertIn("Frontmatter `skills[]` is only a search/graph index", schema)
            self.assertIn("one canonical section named `## Skill Selection`", schema)

    async def test_ingest_analysis_prompt_prioritizes_concrete_page_types(self):
        patch = {"operations": [{"op": "append_log", "message": "ok"}]}
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            _append_archive_event(
                archive,
                manager,
                task_id="session_type_boundaries",
                stage="planner",
                event_type="planner_event",
                payload={"summary": "Seurat SCTransform and an Alpha dataset were discussed."},
                project_id="project_alpha",
            )

            await manager.ingest_from_archive(archive)

            analysis_prompt = manager.llm.calls[0][0].content
            self.assertIn("Page type boundary rules:", analysis_prompt)
            self.assertIn("tools belong in tool pages", analysis_prompt)
            self.assertIn("analysis techniques belong in method pages", analysis_prompt)
            self.assertIn("data collections belong in dataset pages", analysis_prompt)
            self.assertIn("plan a tool page update instead of an entity page", analysis_prompt)
            self.assertIn("plan a method page update instead of a concept page", analysis_prompt)
            self.assertIn("plan a dataset page update instead of an entity page", analysis_prompt)

    async def test_patch_prompt_avoids_duplicate_entity_and_concept_pages(self):
        patch = {"operations": [{"op": "append_log", "message": "ok"}]}
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            _append_archive_event(
                archive,
                manager,
                task_id="session_duplicate_boundaries",
                stage="planner",
                event_type="planner_event",
                payload={"summary": "STAR alignment workflow and STAR tool were discussed."},
                project_id="project_alpha",
            )

            await manager.ingest_from_archive(archive)

            patch_prompt = manager.llm.calls[1][0].content
            self.assertIn("Before creating entity/concept pages", patch_prompt)
            self.assertIn("update an existing or new tool, method, or dataset page", patch_prompt)
            self.assertIn("Avoid duplicate entity/concept pages", patch_prompt)
            self.assertIn("link to those pages with related[]", patch_prompt)

    async def test_ingest_prompts_use_canonical_skill_selection_rules(self):
        patch = {"operations": [{"op": "append_log", "message": "ok"}]}
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            _append_archive_event(
                archive,
                manager,
                task_id="session_skill_selection",
                stage="planner",
                event_type="planner_event",
                payload={
                    "summary": "QC first, preprocessing later.",
                    "selected_skills": ["scrna/qc/filter"],
                    "active_skills": ["scrna/qc/filter"],
                    "future_skills": ["scrna/preprocess/basic"],
                    "candidate_skills": ["scrna/de/contrast"],
                    "skill_selection_source": "consultant_finish",
                },
                project_id="project_alpha",
            )

            await manager.ingest_from_archive(archive)

            analysis_prompt = manager.llm.calls[0][0].content
            patch_prompt = manager.llm.calls[1][0].content
            for prompt in (analysis_prompt, patch_prompt):
                self.assertIn("Skill selection memory rules:", prompt)
                self.assertIn("one canonical section named `## Skill Selection`", prompt)
                self.assertIn("Do not create separate `Used Skills`", prompt)
                self.assertIn("Frontmatter `skills[]` is only a search/graph index", prompt)
            self.assertIn("plan a method or decision page update", analysis_prompt)
            self.assertIn("Do not write separate `Used Skills`", patch_prompt)

    async def test_ingest_creates_fallback_source_summary_when_missing(self):
        patch = {"operations": [{"op": "append_log", "message": "no source page"}]}
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_source",
                stage="execute",
                event_type="execution_finished",
                payload={"stdout": "assembled contigs"},
                source_refs=["task:session_source"],
                project_id="project_alpha",
            )

            result = await manager.ingest_from_archive(archive)

            page = manager.read_page(f"sources/session_archive-{event_id}.md")
            frontmatter, body = manager._split_frontmatter(page)
            self.assertIn("Source summaries created: 1", result)
            self.assertEqual(frontmatter["type"], "source")
            self.assertIn(f"session_archive:{event_id}", frontmatter["sources"])
            self.assertNotIn("claims", frontmatter)
            self.assertIn("assembled contigs", body)

    async def test_ingest_does_not_duplicate_existing_source_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), "{}"]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_existing_source",
                stage="finalize",
                event_type="task_finalized",
                payload={"summary": "Existing source summary."},
                project_id="project_alpha",
            )
            patch = {
                "operations": [
                    {
                        "op": "create_page",
                        "path": "sources/manual-source.md",
                        "title": "Manual Source",
                        "type": "source",
                        "content": "Manual source summary.",
                        "sources": [f"session_archive:{event_id}"],
                    }
                ]
            }
            manager.llm.responses = [json.dumps(_minimal_analysis()), json.dumps(patch)]

            result = await manager.ingest_from_archive(archive)

            source_pages = [
                page for page in manager._all_pages(project_id="project_alpha")
                if page["type"] == "source"
            ]
            self.assertIn("Source summaries created: 0", result)
            self.assertEqual(len(source_pages), 1)
            self.assertEqual(source_pages[0]["path"], "sources/manual-source.md")
            self.assertIn("Wiki page not found", manager.read_page(f"sources/session_archive-{event_id}.md"))

    async def test_purpose_update_suggestion_goes_to_review_queue(self):
        patch = {
            "operations": [
                {
                    "op": "flag_review",
                    "title": "Purpose Update Suggestion",
                    "body": "Add a current question about genome assembly QC.",
                }
            ],
            "ingest_summary": "Suggested purpose update.",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(_minimal_analysis()), json.dumps(patch)]),
                Path(tmpdir) / "memory",
                default_project_id="project_alpha",
            )
            archive = _new_archive(manager)
            _append_archive_event(
                archive,
                manager,
                task_id="session_review",
                stage="consultant",
                event_type="consultant_event",
                payload={"summary": "Need genome assembly QC question."},
                project_id="project_alpha",
            )

            await manager.ingest_from_archive(archive)

            review = manager.read_review_queue()
            self.assertIn("Purpose Update Suggestion", review)
            self.assertIn("genome assembly QC", review)

    async def test_search_and_prompt_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/sctransform.md",
                            "title": "SCTransform Method",
                            "type": "method",
                            "content": "Use SCTransform normalization for single-cell analysis.",
                            "sources": ["raw:event-1"],
                        }
                    ]
                },
                project_id=project_id,
            )

            hits = manager.search("SCTransform normalization", project_id=project_id)
            context = manager.build_prompt_context("session_4", project_id, "SCTransform")

            self.assertEqual(hits[0]["title"], "SCTransform Method")
            self.assertIn("Retrieved Wiki Memory", context)
            self.assertIn("SCTransform normalization", context)

    async def test_legacy_wiki_pages_schema_adds_content_hash_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            wiki_dir = root / "wiki"
            wiki_dir.mkdir(parents=True)
            db_path = wiki_dir / "wiki_index.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE wiki_pages (
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
                conn.commit()
            finally:
                conn.close()

            manager = WikiMemoryManager(None, root)

            with manager._connect() as conn:
                columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()}
            self.assertIn("content_hash", columns)

    async def test_search_index_sync_writes_hash_and_skips_unchanged_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            page_path = manager.wiki_dir / "methods" / "alpha.md"
            manager._write_page(
                page_path,
                {
                    "title": "Alpha Method",
                    "type": "method",
                    "project_id": "project_alpha",
                    "sources": ["raw:alpha"],
                },
                "Alpha normalization details.",
            )
            original_upsert = manager._upsert_search_page
            upserted_paths = []

            def spy_upsert(conn, page):
                upserted_paths.append(str(page.get("path") or ""))
                return original_upsert(conn, page)

            manager._upsert_search_page = spy_upsert

            hits = manager.search("Alpha normalization", project_id="project_alpha")

            self.assertEqual(hits[0]["path"], "methods/alpha.md")
            self.assertEqual(upserted_paths, ["methods/alpha.md"])
            expected_hash = hashlib.sha256(page_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            with manager._connect() as conn:
                row = conn.execute("SELECT content_hash FROM wiki_pages WHERE path = ?", ("methods/alpha.md",)).fetchone()
                fts_count = 0
                if manager.fts_enabled:
                    fts_count = conn.execute(
                        "SELECT COUNT(*) FROM wiki_pages_fts WHERE path = ?",
                        ("methods/alpha.md",),
                    ).fetchone()[0]
            self.assertEqual(row["content_hash"], expected_hash)
            if manager.fts_enabled:
                self.assertEqual(fts_count, 1)

            upserted_paths.clear()
            manager.search("Alpha normalization", project_id="project_alpha")

            self.assertEqual(upserted_paths, [])

    async def test_search_index_sync_refreshes_changed_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            page_path = manager.wiki_dir / "methods" / "alpha.md"
            manager._write_page(
                page_path,
                {
                    "title": "Alpha Method",
                    "type": "method",
                    "project_id": "project_alpha",
                    "sources": ["raw:alpha"],
                },
                "Alpha normalization details.",
            )
            manager.search("Alpha normalization", project_id="project_alpha")
            with manager._connect() as conn:
                old_hash = conn.execute(
                    "SELECT content_hash FROM wiki_pages WHERE path = ?",
                    ("methods/alpha.md",),
                ).fetchone()["content_hash"]

            original_upsert = manager._upsert_search_page
            upserted_paths = []

            def spy_upsert(conn, page):
                upserted_paths.append(str(page.get("path") or ""))
                return original_upsert(conn, page)

            manager._upsert_search_page = spy_upsert
            manager._write_page(
                page_path,
                {
                    "title": "Alpha Revised Method",
                    "type": "method",
                    "project_id": "project_alpha",
                    "sources": ["raw:alpha", "raw:beta"],
                },
                "Beta correction details.",
            )

            hits = manager.search("Beta correction", project_id="project_alpha")

            self.assertEqual(upserted_paths, ["methods/alpha.md"])
            self.assertEqual(hits[0]["title"], "Alpha Revised Method")
            with manager._connect() as conn:
                row = conn.execute(
                    "SELECT content_hash, content, sources_json FROM wiki_pages WHERE path = ?",
                    ("methods/alpha.md",),
                ).fetchone()
            self.assertNotEqual(row["content_hash"], old_hash)
            self.assertIn("Beta correction details", row["content"])
            self.assertIn("raw:beta", row["sources_json"])

    async def test_search_index_sync_removes_deleted_pages_and_embeddings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            page_path = manager.wiki_dir / "methods" / "alpha.md"
            manager._write_page(
                page_path,
                {
                    "title": "Alpha Method",
                    "type": "method",
                    "project_id": "project_alpha",
                    "sources": ["raw:alpha"],
                },
                "Alpha normalization details.",
            )
            manager.search("Alpha normalization", project_id="project_alpha")
            with manager._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO wiki_embeddings
                    (path, project_id, model, dimensions, content_hash, vector_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("methods/alpha.md", "project_alpha", "test-model", 3, "old", "[1, 0, 0]", "now"),
                )
                conn.commit()

            page_path.unlink()
            hits = manager.search("Alpha normalization", project_id="project_alpha")

            self.assertEqual(hits, [])
            with manager._connect() as conn:
                page_count = conn.execute(
                    "SELECT COUNT(*) FROM wiki_pages WHERE path = ?",
                    ("methods/alpha.md",),
                ).fetchone()[0]
                embedding_count = conn.execute(
                    "SELECT COUNT(*) FROM wiki_embeddings WHERE path = ?",
                    ("methods/alpha.md",),
                ).fetchone()[0]
                fts_count = 0
                if manager.fts_enabled:
                    fts_count = conn.execute(
                        "SELECT COUNT(*) FROM wiki_pages_fts WHERE path = ?",
                        ("methods/alpha.md",),
                    ).fetchone()[0]
            self.assertEqual(page_count, 0)
            self.assertEqual(embedding_count, 0)
            if manager.fts_enabled:
                self.assertEqual(fts_count, 0)

    async def test_search_index_sync_uses_lexical_fallback_when_fts_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.fts_enabled = False
            manager._write_page(
                manager.wiki_dir / "methods" / "lexical.md",
                {
                    "title": "Lexical Method",
                    "type": "method",
                    "project_id": "project_alpha",
                    "sources": ["raw:lexical"],
                },
                "Lexical fallback token.",
            )

            hits = manager.search("fallback token", project_id="project_alpha")

            self.assertEqual(hits[0]["path"], "methods/lexical.md")

    async def test_entity_and_concept_pages_are_created_and_searchable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "entities/tp53.md",
                            "title": "TP53",
                            "type": "entity",
                            "entity_type": "gene",
                            "aliases": ["p53"],
                            "related": ["concepts/immune-evasion.md"],
                            "content": "TP53 is a tumor suppressor gene linked to immune evasion evidence.",
                            "sources": ["raw:entity"],
                        },
                        {
                            "op": "create_page",
                            "path": "concepts/immune-evasion.md",
                            "title": "Immune Evasion",
                            "type": "concept",
                            "concept_type": "biological_process",
                            "definition": "Tumor mechanisms that avoid immune detection.",
                            "content": "Immune evasion is relevant to tumor microenvironment analysis.",
                            "sources": ["raw:concept"],
                        },
                    ]
                },
                project_id="project_alpha",
            )

            hits = manager.search("p53 immune evasion", project_id="project_alpha", top_k=5)
            by_path = {hit["path"]: hit for hit in hits}
            entity_page = manager.read_page("entities/tp53.md")
            concept_page = manager.read_page("concepts/immune-evasion.md")

            self.assertIn("entities/tp53.md", by_path)
            self.assertIn("concepts/immune-evasion.md", by_path)
            self.assertIn('type: "entity"', entity_page)
            self.assertIn('entity_type: "gene"', entity_page)
            self.assertIn('type: "concept"', concept_page)
            self.assertIn('concept_type: "biological_process"', concept_page)

    async def test_invalid_entity_and_concept_types_are_normalized_to_other(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "entities/mystery.md",
                            "title": "Mystery Entity",
                            "type": "entity",
                            "entity_type": "unsupported-kind",
                            "content": "Mystery entity.",
                            "sources": ["raw:mystery"],
                        },
                        {
                            "op": "create_page",
                            "path": "concepts/mystery.md",
                            "title": "Mystery Concept",
                            "type": "concept",
                            "concept_type": "unsupported-kind",
                            "content": "Mystery concept.",
                            "sources": ["raw:mystery"],
                        },
                    ]
                },
                project_id="project_alpha",
            )

            self.assertIn('entity_type: "other"', manager.read_page("entities/mystery.md"))
            self.assertIn('concept_type: "other"', manager.read_page("concepts/mystery.md"))

    async def test_ingest_uses_analysis_json_for_generation_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            analysis = {
                "source_summary": {
                    "title": "TP53 evidence",
                    "source_type": "conversation",
                    "main_topic": "TP53",
                    "one_sentence_summary": "TP53 appears in immune evasion context.",
                },
                "entities": [
                    {
                        "name": "TP53",
                        "entity_type": "gene",
                        "aliases": ["p53"],
                        "role": "central",
                        "evidence": ["raw:event"],
                        "suggested_page": "entities/tp53.md",
                    }
                ],
                "concepts": [],
                "claims": [],
                "conflicts": [],
                "wiki_patch_plan": [
                    {
                        "action": "create",
                        "path": "entities/tp53.md",
                        "type": "entity",
                        "reason": "New durable entity.",
                    }
                ],
                "research_queries": [
                    {
                        "query": "TP53 immune evasion glioblastoma",
                        "reason": "Need deeper evidence.",
                        "priority": "medium",
                        "trigger": "new_entity",
                        "sources": ["raw:event"],
                        "related_pages": ["entities/tp53.md"],
                    }
                ],
            }
            patch = {
                "operations": [
                    {
                        "op": "create_page",
                        "path": "entities/tp53.md",
                        "title": "TP53",
                        "type": "entity",
                        "entity_type": "gene",
                        "content": "TP53 entity page.",
                        "sources": ["raw:event"],
                    },
                    {
                        "op": "add_research_queries",
                        "queries": [
                            {
                                "query": "TP53 immune evasion glioblastoma",
                                "reason": "Need deeper evidence.",
                                "sources": ["raw:event"],
                                "related_pages": ["entities/tp53.md"],
                            }
                        ],
                    },
                ],
                "ingest_summary": "Created TP53 entity.",
            }
            manager = WikiMemoryManager(
                _FakeLLM([json.dumps(analysis), json.dumps(patch)]),
                Path(tmpdir) / "memory",
            )
            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_json",
                stage="execute",
                event_type="observation",
                payload={"summary": "TP53 appears in immune evasion context."},
                project_id="project_alpha",
            )
            analysis["entities"][0]["evidence"] = [f"session_archive:{event_id}"]
            analysis["research_queries"][0]["sources"] = [f"session_archive:{event_id}"]

            result = await manager.ingest_from_archive(archive, project_id="project_alpha")

            second_prompt = str(manager.llm.calls[1][0].content)
            self.assertIn("Wiki ingest completed.", result)
            self.assertEqual(len(manager.llm.calls), 2)
            self.assertIn("Analysis JSON:", second_prompt)
            self.assertIn('"entities"', second_prompt)
            self.assertIn("add_research_queries", second_prompt)
            self.assertIn("TP53 entity page.", manager.read_page("entities/tp53.md"))
            self.assertTrue(manager.research_queries_file.read_text(encoding="utf-8").strip())

    async def test_invalid_analysis_json_prevents_patch_application(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                _FakeLLM(["not json", json.dumps({"operations": [{"op": "append_log", "message": "should not run"}]})]),
                Path(tmpdir) / "memory",
            )
            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_bad",
                stage="execute",
                event_type="observation",
                payload={"summary": "Bad analysis should stop ingest."},
                project_id="project_alpha",
            )

            result = await manager.ingest_from_archive(archive, project_id="project_alpha")

            self.assertIn("analyst returned invalid JSON", result)
            self.assertEqual(len(manager.llm.calls), 1)
            self.assertIn("Invalid Wiki Analysis", manager.read_review_queue())
            self.assertEqual([event.id for event in archive.pending_for_wiki("project_alpha")], [event_id])

    async def test_add_research_queries_writes_deduped_queue_and_reviews_invalid_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "add_research_queries",
                            "queries": [
                                {
                                    "query": "TP53 mutation glioblastoma immune microenvironment",
                                    "reason": "Need deeper evidence.",
                                    "priority": "high",
                                    "trigger": "knowledge_gap",
                                    "sources": ["raw:query"],
                                    "related_pages": ["entities/tp53.md"],
                                },
                                {
                                    "query": "TP53 mutation glioblastoma immune microenvironment",
                                    "reason": "Duplicate query.",
                                    "sources": ["raw:query"],
                                },
                                {
                                    "query": "Missing reason and source",
                                },
                            ],
                        }
                    ]
                },
                project_id="project_alpha",
            )

            records = [
                json.loads(line)
                for line in manager.research_queries_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "pending")
            self.assertEqual(records[0]["priority"], "high")
            self.assertEqual(records[0]["related_pages"], ["entities/tp53.md"])
            self.assertIn("Invalid Research Query", manager.read_review_queue())

    async def test_digest_and_overview_include_entity_and_concept_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "entities/tp53.md",
                            "title": "TP53",
                            "type": "entity",
                            "entity_type": "gene",
                            "content": "TP53 entity.",
                            "sources": ["raw:entity"],
                        },
                        {
                            "op": "create_page",
                            "path": "concepts/immune-evasion.md",
                            "title": "Immune Evasion",
                            "type": "concept",
                            "concept_type": "biological_process",
                            "content": "Immune evasion concept.",
                            "sources": ["raw:concept"],
                        },
                    ]
                },
                project_id="project_alpha",
            )

            manager.compile_digest("project_alpha")
            digest = json.loads(manager.digest_path("project_alpha").read_text(encoding="utf-8"))
            overview = manager.overview_file.read_text(encoding="utf-8")

            self.assertEqual(digest["entity_count"], 1)
            self.assertEqual(digest["concept_count"], 1)
            self.assertEqual(len(digest["knowledge_pages"]), 2)
            self.assertIn("Entities: 1", overview)
            self.assertIn("Concepts: 1", overview)

    async def test_graph_expansion_connects_source_entity_concept_method(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "sources/tp53-source.md",
                            "title": "TP53 Source",
                            "type": "source",
                            "content": "Unique seed evidence links [[entities/tp53.md]] and [[concepts/immune-evasion.md]].",
                            "sources": ["raw:shared"],
                        },
                        {
                            "op": "create_page",
                            "path": "entities/tp53.md",
                            "title": "TP53",
                            "type": "entity",
                            "entity_type": "gene",
                            "related": ["concepts/immune-evasion.md"],
                            "content": "TP53 relates to [[concepts/immune-evasion.md]].",
                            "sources": ["raw:shared"],
                        },
                        {
                            "op": "create_page",
                            "path": "concepts/immune-evasion.md",
                            "title": "Immune Evasion",
                            "type": "concept",
                            "concept_type": "biological_process",
                            "related": ["methods/checkpoint-analysis.md"],
                            "content": "Immune evasion links to [[methods/checkpoint-analysis.md]].",
                            "sources": ["raw:shared"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/checkpoint-analysis.md",
                            "title": "Checkpoint Analysis",
                            "type": "method",
                            "content": "Checkpoint analysis method.",
                            "sources": ["raw:shared"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/unrelated.md",
                            "title": "Unrelated",
                            "type": "method",
                            "content": "Unrelated method.",
                            "sources": ["raw:other"],
                        },
                    ]
                },
                project_id="project_alpha",
            )

            hits = manager.search("Unique seed evidence", project_id="project_alpha", top_k=6)
            by_path = {hit["path"]: hit for hit in hits}

            self.assertIn("sources/tp53-source.md", by_path)
            self.assertIn("entities/tp53.md", by_path)
            self.assertIn("concepts/immune-evasion.md", by_path)
            self.assertIn("methods/checkpoint-analysis.md", by_path)
            self.assertNotIn("methods/unrelated.md", by_path)

    async def test_graph_expansion_uses_direct_links_and_type_affinity_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "datasets/project-alpha-reads.md",
                            "title": "Project Alpha Reads",
                            "type": "dataset",
                            "content": "Project Alpha sequencing input links to [[methods/hifiasm-hic.md]].",
                            "sources": ["raw:seed"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/hifiasm-hic.md",
                            "title": "Hifiasm Hi-C Method",
                            "type": "method",
                            "content": "Run hifiasm with Hi-C phasing.",
                            "sources": ["raw:method"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/unrelated-method.md",
                            "title": "Unrelated Method",
                            "type": "method",
                            "content": "A different method without links or shared evidence.",
                            "sources": ["raw:other"],
                        },
                    ]
                },
                project_id=project_id,
            )

            hits = manager.search("Project Alpha sequencing", project_id=project_id, top_k=5)
            by_path = {hit["path"]: hit for hit in hits}

            self.assertIn("datasets/project-alpha-reads.md", by_path)
            self.assertIn("methods/hifiasm-hic.md", by_path)
            self.assertNotIn("methods/unrelated-method.md", by_path)
            self.assertGreater(by_path["methods/hifiasm-hic.md"]["score"], 3.0)

    async def test_graph_expansion_prioritizes_source_overlap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "datasets/project-alpha-hifi.md",
                            "title": "Project Alpha HiFi",
                            "type": "dataset",
                            "content": "Project Alpha HiFi reads are the seed evidence.",
                            "sources": ["raw:shared-a", "raw:shared-b"],
                        },
                        {
                            "op": "create_page",
                            "path": "decisions/project-alpha-strategy.md",
                            "title": "Project Alpha Strategy",
                            "type": "decision",
                            "content": "Choose a chromosome-scale assembly strategy.",
                            "sources": ["raw:shared-a", "raw:shared-b"],
                        },
                        {
                            "op": "create_page",
                            "path": "datasets/other-dataset.md",
                            "title": "Other Dataset",
                            "type": "dataset",
                            "content": "Same page type but unrelated evidence.",
                            "sources": ["raw:other"],
                        },
                    ]
                },
                project_id=project_id,
            )

            hits = manager.search("Project Alpha HiFi", project_id=project_id, top_k=5)
            by_path = {hit["path"]: hit for hit in hits}

            self.assertIn("decisions/project-alpha-strategy.md", by_path)
            self.assertNotIn("datasets/other-dataset.md", by_path)
            self.assertGreaterEqual(by_path["decisions/project-alpha-strategy.md"]["score"], 4.0)

    async def test_graph_expansion_uses_adamic_adar_common_neighbors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "datasets/project-alpha-reads.md",
                            "title": "Project Alpha Reads",
                            "type": "dataset",
                            "content": (
                                "Project Alpha seed links to [[methods/assembly-bridge-a.md]] "
                                "and [[methods/assembly-bridge-b.md]]."
                            ),
                            "sources": ["raw:seed"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/assembly-bridge-a.md",
                            "title": "Assembly Bridge A",
                            "type": "method",
                            "content": "Bridge page links related decisions.",
                            "sources": ["raw:bridge"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/assembly-bridge-b.md",
                            "title": "Assembly Bridge B",
                            "type": "method",
                            "content": "Second bridge page links related decisions.",
                            "sources": ["raw:bridge-b"],
                        },
                        {
                            "op": "create_page",
                            "path": "decisions/scaffolding-policy.md",
                            "title": "Scaffolding Policy",
                            "type": "decision",
                            "content": (
                                "This decision links back to [[methods/assembly-bridge-a.md]] "
                                "and [[methods/assembly-bridge-b.md]]."
                            ),
                            "sources": ["raw:decision"],
                        },
                    ]
                },
                project_id=project_id,
            )

            hits = manager.search("Project Alpha seed", project_id=project_id, top_k=5)
            by_path = {hit["path"]: hit for hit in hits}

            self.assertIn("methods/assembly-bridge-a.md", by_path)
            self.assertIn("methods/assembly-bridge-b.md", by_path)
            self.assertIn("decisions/scaffolding-policy.md", by_path)
            self.assertGreater(by_path["decisions/scaffolding-policy.md"]["score"], 1.5)

    async def test_graph_type_affinity_supports_bioinformatics_page_pairs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")

            self.assertEqual(manager._type_affinity("dataset", "dataset"), 1.0)
            self.assertEqual(manager._type_affinity("dataset", "method"), 0.8)
            self.assertEqual(manager._type_affinity("method", "decision"), 0.8)
            self.assertEqual(manager._type_affinity("issue", "method"), 0.7)
            self.assertEqual(manager._type_affinity("artifact", "dataset"), 0.6)
            self.assertEqual(manager._type_affinity("source", "decision"), 0.5)
            self.assertEqual(manager._type_affinity("dataset", "issue"), 0.0)

    async def test_archive_events_include_inferred_task_semantics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            archive = _new_archive(manager)

            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_1",
                stage="planner",
                event_type="planner_event",
                messages=["Call TADs for leaf.cool at 40kb with hicFindTADs."],
                payload={
                    "selected_skills": ["3d-dna/tad-calling"],
                    "workflow_id": "hic_tad_analysis",
                    "resolution": 40000,
                    "plan_file": "task_outputs/session_1/plan.md",
                },
                project_id="project_alpha",
            )

            archive_event = archive.recent_events(task_id="session_1", limit=1)[0]
            semantics = archive_event.task_semantics

            self.assertEqual(archive_event.id, event_id)
            self.assertEqual(semantics["task_type"], "chromatin_contact_tad")
            self.assertEqual(semantics["workflow_id"], "hic_tad_analysis")
            self.assertIn("3d-dna/tad-calling", semantics["skills"])
            self.assertIn("40000", semantics["resolutions"])
            self.assertIn("leaf", semantics["samples"])
            self.assertEqual(archive_event.metadata["task_semantics"]["analysis_type"], "tad_calling")

    async def test_source_summary_pages_preserve_task_semantic_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            record = {
                "event_id": "session_1-abc",
                "event_type": "planner_event",
                "stage": "planner",
                "task_id": "session_1",
                "session_id": "session_1",
                "timestamp": "2026-05-19T10:00:00",
                "source_refs": ["task:session_1"],
                "payload": {
                    "task_semantics": {
                        "task_id": "session_1",
                        "task_title": "leaf_tad_calling_20260519",
                        "task_type": "chromatin_contact_tad",
                        "workflow_id": "hic_tad_analysis",
                        "analysis_type": "tad_calling",
                        "skills": ["3d-dna/tad-calling"],
                        "samples": ["leaf"],
                        "resolutions": ["40000"],
                    }
                },
            }

            created = manager._ensure_source_summary_pages([record], "project_alpha")
            page = manager.read_page("sources/session_1-abc.md")
            frontmatter, _body = manager._split_frontmatter(page)

            self.assertEqual(created, 1)
            self.assertEqual(frontmatter["task_type"], "chromatin_contact_tad")
            self.assertEqual(frontmatter["workflow_id"], "hic_tad_analysis")
            self.assertEqual(frontmatter["analysis_type"], "tad_calling")
            self.assertEqual(frontmatter["skills"], ["3d-dna/tad-calling"])
            self.assertEqual(frontmatter["samples"], ["leaf"])

    async def test_compile_communities_groups_pages_by_task_semantics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            shared_semantics = {
                "task_type": "chromatin_contact_tad",
                "workflow_id": "hic_tad_analysis",
                "analysis_type": "tad_calling",
                "skills": ["3d-dna/tad-calling"],
                "resolutions": ["40000"],
            }
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/leaf-tad.md",
                            "title": "Leaf TAD Calling",
                            "type": "method",
                            "content": "Leaf TAD calling.",
                            "sources": ["raw:leaf"],
                            **shared_semantics,
                            "samples": ["leaf"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/root-tad.md",
                            "title": "Root TAD Calling",
                            "type": "method",
                            "content": "Root TAD calling.",
                            "sources": ["raw:root"],
                            **shared_semantics,
                            "samples": ["root"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/scrna-qc.md",
                            "title": "scRNA QC",
                            "type": "method",
                            "content": "Single-cell QC.",
                            "sources": ["raw:scrna"],
                            "task_type": "single_cell_rnaseq",
                            "analysis_type": "scrna_analysis",
                            "skills": ["scrna/qc/filter"],
                        },
                    ]
                },
                project_id=project_id,
            )

            edges = manager._page_graph_edges(manager._all_pages(project_id=project_id))
            communities = manager.compile_communities(project_id)
            pages = communities["pages"]

            self.assertIn(("methods/leaf-tad.md", "methods/root-tad.md"), edges)
            self.assertEqual(
                pages["methods/leaf-tad.md"]["community_id"],
                pages["methods/root-tad.md"]["community_id"],
            )
            self.assertNotEqual(
                pages["methods/leaf-tad.md"]["community_id"],
                pages["methods/scrna-qc.md"]["community_id"],
            )

    async def test_compile_communities_groups_related_pages_and_excludes_queries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "sources/source-a.md",
                            "title": "Source A",
                            "type": "source",
                            "content": "Source A summarizes Alpha reads.",
                            "sources": ["raw:a"],
                        },
                        {
                            "op": "create_page",
                            "path": "datasets/alpha-reads.md",
                            "title": "Alpha Reads",
                            "type": "dataset",
                            "content": "Alpha reads link to [[methods/alpha-assembly.md]].",
                            "sources": ["raw:a"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/alpha-assembly.md",
                            "title": "Alpha Assembly",
                            "type": "method",
                            "content": "Alpha assembly uses the same raw evidence.",
                            "sources": ["raw:a"],
                        },
                        {
                            "op": "create_page",
                            "path": "datasets/beta-reads.md",
                            "title": "Beta Reads",
                            "type": "dataset",
                            "content": "Beta reads link to [[methods/beta-assembly.md]].",
                            "sources": ["raw:b"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/beta-assembly.md",
                            "title": "Beta Assembly",
                            "type": "method",
                            "content": "Beta assembly uses the beta raw evidence.",
                            "sources": ["raw:b"],
                        },
                        {
                            "op": "create_page",
                            "path": "queries/transient-answer.md",
                            "title": "Transient Answer",
                            "type": "query",
                            "content": "Saved chat answer should not shape communities.",
                            "sources": ["raw:a"],
                        },
                    ]
                },
                project_id=project_id,
            )

            communities = manager.compile_communities(project_id)
            pages = communities["pages"]
            alpha_community = pages["datasets/alpha-reads.md"]["community_id"]
            beta_community = pages["datasets/beta-reads.md"]["community_id"]
            alpha_record = next(
                item for item in communities["communities"]
                if item["community_id"] == alpha_community
            )

            self.assertTrue(manager.community_path(project_id).exists())
            self.assertNotIn("queries/transient-answer.md", pages)
            self.assertEqual(alpha_community, pages["methods/alpha-assembly.md"]["community_id"])
            self.assertEqual(alpha_community, pages["sources/source-a.md"]["community_id"])
            self.assertEqual(beta_community, pages["methods/beta-assembly.md"]["community_id"])
            self.assertNotEqual(alpha_community, beta_community)
            self.assertNotEqual(alpha_record["core_pages"][0]["type"], "source")

    async def test_page_graph_edges_do_not_connect_type_only_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Alpha method.",
                            "sources": ["raw:a"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/beta.md",
                            "title": "Beta Method",
                            "type": "method",
                            "content": "Beta method.",
                            "sources": ["raw:b"],
                        },
                    ]
                },
                project_id=project_id,
            )

            edges = manager._page_graph_edges(manager._all_pages(project_id=project_id))
            communities = manager.compile_communities(project_id)

            self.assertEqual(edges, {})
            self.assertNotEqual(
                communities["pages"]["methods/alpha.md"]["community_id"],
                communities["pages"]["methods/beta.md"]["community_id"],
            )

    async def test_low_cohesion_community_warning_uses_internal_density(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            pages = [
                {
                    "path": f"methods/star-{index}.md",
                    "title": f"Star {index}",
                    "type": "method",
                    "sources": [],
                    "content": "",
                }
                for index in range(16)
            ]
            edges = {
                ("methods/star-0.md", f"methods/star-{index}.md"): 3.0
                for index in range(1, 16)
            }

            with mock.patch(
                "networkx.algorithms.community.louvain_communities",
                side_effect=RuntimeError("force fallback"),
            ):
                communities = manager._detect_page_communities(pages, edges)

            low_cohesion = [
                item for item in communities["communities"]
                if "low_cohesion" in item["warnings"]
            ]
            self.assertAlmostEqual(
                manager._community_cohesion({page["path"] for page in pages}, edges),
                15 / 120,
            )
            self.assertEqual(len(low_cohesion), 1)

    async def test_search_community_bonus_does_not_expand_below_graph_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(None, Path(tmpdir) / "memory")
            project_id = "project_alpha"
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "datasets/seed.md",
                            "title": "Seed Dataset",
                            "type": "dataset",
                            "content": "Seed dataset links to [[methods/bridge.md]].",
                            "sources": ["raw:seed"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/bridge.md",
                            "title": "Bridge Method",
                            "type": "method",
                            "content": "Bridge method links to [[projects/downstream.md]].",
                            "sources": ["raw:bridge"],
                        },
                        {
                            "op": "create_page",
                            "path": "projects/downstream.md",
                            "title": "Downstream Project",
                            "type": "project",
                            "content": "Downstream project is only transitively related.",
                            "sources": ["raw:downstream"],
                        },
                    ]
                },
                project_id=project_id,
            )

            hits = manager.search("Seed dataset", project_id=project_id, top_k=5)
            by_path = {hit["path"]: hit for hit in hits}

            self.assertIn("methods/bridge.md", by_path)
            self.assertNotIn("projects/downstream.md", by_path)
            self.assertGreater(by_path["methods/bridge.md"]["score"], 3.8)

    async def test_embedding_sync_writes_sqlite_cache_and_skips_unchanged_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                None,
                Path(tmpdir) / "memory",
                embedding_config={"enabled": True, "api_key": "test-key", "dimensions": 3},
            )
            calls = []

            def fake_embed(text):
                calls.append(text)
                return [1.0, 0.0, 0.0]

            manager._embed_text = fake_embed
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/genome-assembly.md",
                            "title": "Genome Assembly",
                            "type": "method",
                            "content": "Chromosome-scale genome assembly with Hi-C scaffolding.",
                            "sources": ["raw:assembly"],
                        }
                    ]
                },
                project_id="project_alpha",
            )

            manager.compile_digest("project_alpha")

            with manager._connect() as conn:
                rows = conn.execute("SELECT * FROM wiki_embeddings").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["path"], "methods/genome-assembly.md")
            self.assertEqual(len(calls), 1)

    async def test_vector_search_recalls_semantic_hit_without_keyword_overlap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                None,
                Path(tmpdir) / "memory",
                embedding_config={"enabled": True, "api_key": "test-key", "dimensions": 3},
            )

            def fake_embed(text):
                lowered = text.lower()
                if "chromosome" in lowered or "基因组" in lowered:
                    return [1.0, 0.0, 0.0]
                return [0.0, 1.0, 0.0]

            manager._embed_text = fake_embed
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/hic-scaffolding.md",
                            "title": "Hi-C Scaffolding",
                            "type": "method",
                            "content": "Chromosome-scale assembly scaffolding and phasing.",
                            "sources": ["raw:hifi"],
                        },
                        {
                            "op": "create_page",
                            "path": "methods/single-cell-qc.md",
                            "title": "Single Cell QC",
                            "type": "method",
                            "content": "Single-cell quality control and filtering.",
                            "sources": ["raw:scrna"],
                        },
                    ]
                },
                project_id="project_alpha",
            )

            hits = manager.search("如何做基因组组装", project_id="project_alpha", top_k=3)

            self.assertEqual(hits[0]["path"], "methods/hic-scaffolding.md")

    async def test_rrf_fuse_prioritizes_hits_seen_by_lexical_and_vector(self):
        shared = WikiSearchHit("methods/shared.md", "Shared", "method", 9.0, "shared", [])
        lexical_only = WikiSearchHit("methods/lexical.md", "Lexical", "method", 8.0, "lexical", [])
        vector_only = WikiSearchHit("methods/vector.md", "Vector", "method", 0.9, "vector", [])

        fused = WikiMemoryManager._rrf_fuse(
            [[shared, lexical_only], [shared, vector_only]],
            limit=3,
        )

        self.assertEqual(fused[0].path, "methods/shared.md")
        self.assertGreater(fused[0].score, fused[1].score)

    async def test_embedding_failure_falls_back_to_lexical_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                None,
                Path(tmpdir) / "memory",
                embedding_config={"enabled": True, "api_key": "test-key", "dimensions": 3},
            )
            manager._embed_text = lambda _text: None
            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "methods/alpha.md",
                            "title": "Alpha Method",
                            "type": "method",
                            "content": "Alpha normalization method.",
                            "sources": ["raw:alpha"],
                        }
                    ]
                },
                project_id="project_alpha",
            )

            hits = manager.search("Alpha normalization", project_id="project_alpha")

            self.assertEqual(hits[0]["path"], "methods/alpha.md")

    async def test_embedding_response_dimension_mismatch_disables_vector_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                None,
                Path(tmpdir) / "memory",
                embedding_config={
                    "enabled": True,
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "dimensions": 3,
                },
            )

            class _FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps({"data": [{"embedding": [1.0, 0.0]}]}).encode("utf-8")

            with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()):
                self.assertIsNone(manager._embed_text("dimension mismatch"))

    async def test_constructor_default_project_is_used_when_project_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = WikiMemoryManager(
                None,
                Path(tmpdir) / "memory",
                default_project_id="project_beta",
            )

            archive = _new_archive(manager)
            event_id = _append_archive_event(
                archive,
                manager,
                task_id="session_beta",
                stage="execute",
                event_type="execution_finished",
                payload={"summary": "Beta project note."},
            )
            archive_event = archive.recent_events(task_id="session_beta", limit=1)[0]
            self.assertEqual(event_id, archive_event.id)
            self.assertEqual(archive_event.project_id, "project_beta")

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "projects/beta.md",
                            "title": "Beta Project",
                            "type": "project",
                            "content": "Beta project note.",
                            "sources": ["raw:event-1"],
                        }
                    ]
                }
            )

            page = manager.read_page("projects/beta.md")
            self.assertIn('"project_beta"', page)
            self.assertEqual(manager.search("Beta project note")[0]["title"], "Beta Project")

    async def test_constructor_accepts_external_wiki_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = WikiMemoryManager(
                None,
                root / "memory",
                wiki_dir=root / "visible_wiki",
            )

            manager.apply_patch(
                {
                    "operations": [
                        {
                            "op": "create_page",
                            "path": "tools/hicexplorer.md",
                            "title": "HiCExplorer",
                            "type": "tool",
                            "content": "Run via conda.",
                            "sources": ["raw:hictest"],
                        }
                    ]
                }
            )

            self.assertTrue((root / "visible_wiki" / "tools" / "hicexplorer.md").exists())
            self.assertFalse((root / "memory" / "wiki" / "tools" / "hicexplorer.md").exists())
            self.assertIn(str(root / "visible_wiki"), manager.status_text())

    async def test_migrate_wiki_pages_copies_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "old" / "wiki"
            target = root / "new" / "wiki"
            (source / "tools").mkdir(parents=True)
            (target / "tools").mkdir(parents=True)
            (source / "tools" / "hicexplorer.md").write_text("old page", encoding="utf-8")
            (source / "index.md").write_text("old index", encoding="utf-8")
            (target / "index.md").write_text("new index", encoding="utf-8")

            copied = WikiMemoryManager.migrate_wiki_pages(source, target)

            self.assertIn(str(target / "tools" / "hicexplorer.md"), copied)
            self.assertEqual((target / "tools" / "hicexplorer.md").read_text(encoding="utf-8"), "old page")
            self.assertEqual((target / "index.md").read_text(encoding="utf-8"), "new index")

    async def test_legacy_runtime_memory_migration_is_non_destructive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "memory"
            target = Path(tmpdir) / ".biomind" / "memory"
            (legacy / "session_prompts").mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "MEMORY.md").write_text("legacy memory", encoding="utf-8")
            (legacy / "session_prompts" / "a.prompt.md").write_text("legacy prompt", encoding="utf-8")
            (target / "MEMORY.md").write_text("existing memory", encoding="utf-8")

            copied = WikiMemoryManager.migrate_legacy_runtime_memory(legacy, target)

            self.assertIn(str(target / "session_prompts" / "a.prompt.md"), copied)
            self.assertEqual((target / "MEMORY.md").read_text(encoding="utf-8"), "existing memory")
            self.assertEqual((target / "session_prompts" / "a.prompt.md").read_text(encoding="utf-8"), "legacy prompt")


if __name__ == "__main__":
    unittest.main()
