import tempfile
import unittest
import json
from copy import deepcopy
from pathlib import Path
from unittest import mock

from agent_core.skill_access import (
    PROPOSE_SKILL_BINDING,
    REQUEST_SKILL_CONTEXT,
    SEARCH_SKILLS,
    SkillAccessError,
    SkillAccessService,
    activate_reviewed_binding,
    apply_confirmed_task_skill_change,
    apply_plan_impact_review,
    new_skill_access_state,
    parse_strict_json_object,
    parse_typed_skill_decision,
    project_skill_context,
    review_binding_proposal,
    stale_node_result,
    validate_plan_impact_decision,
    validate_plan_impact_proposal_compatibility,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import canonical_current_task_spec, task_semantic_hash
from agent_core.skill_access import authorised_task_skill_selection
from prompts.manager import PromptManager


class ProgressiveSkillAccessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.skills_root = self.tmp_path / "skills"

        self.alpha_dir = self.skills_root / "genomics" / "alpha-annotation"
        self.alpha_dir.mkdir(parents=True)
        self.alpha_text = (
            "---\n"
            "name: Alpha Variant Annotation\n"
            "description: alpha_marker variant annotation workflow\n"
            "metadata:\n"
            "  aliases: [alpha_marker]\n"
            "  inputs: [vcf]\n"
            "---\n"
            "# Overview\n"
            "Alpha immutable overview.\n"
            "## Input requirements\n"
            "Alpha exact input line.\n"
            "## Procedure\n"
            "Run alpha_marker with the verified database.\n"
            "## Notes\n"
            "Keep the output deterministic.\n"
        )
        (self.alpha_dir / "SKILL.md").write_text(
            self.alpha_text,
            encoding="utf-8",
        )
        references = self.alpha_dir / "references"
        references.mkdir()
        (references / "guide.txt").write_text(
            "resource first line\nresource second line\n",
            encoding="utf-8",
        )

        self.beta_dir = self.skills_root / "transcriptomics" / "beta-expression"
        self.beta_dir.mkdir(parents=True)
        self.beta_text = (
            "---\n"
            "name: Beta Expression Analysis\n"
            "description: beta_marker differential expression workflow\n"
            "metadata:\n"
            "  aliases: [beta_marker]\n"
            "  inputs: [count_matrix]\n"
            "---\n"
            "# Workflow\n"
            "Use beta_marker for expression analysis.\n"
        )
        (self.beta_dir / "SKILL.md").write_text(
            self.beta_text,
            encoding="utf-8",
        )

        self.manager = SkillManager(str(self.skills_root))
        self.service = SkillAccessService(self.manager)

    @staticmethod
    def runtime(**overrides):
        envelope = {
            "caller_node": "generate",
            "caller_invocation_id": "generate-001",
            "task_revision": 12,
            "plan_revision": 7,
            "authoritative_user_turn_revision": 3,
            "current_step_id": "step-2",
            "attempt_id": "attempt-1",
        }
        envelope.update(overrides)
        return envelope

    def test_prompts_publish_the_exact_progressive_skill_protocol(self):
        protocol = PromptManager._skill_decision_protocol(
            binding_boundary="Test boundary."
        )
        for decision in (
            REQUEST_SKILL_CONTEXT,
            SEARCH_SKILLS,
            PROPOSE_SKILL_BINDING,
        ):
            self.assertIn(decision, protocol)
        for operation in (
            "headings",
            "section",
            "search",
            "range",
            "resource",
            "full",
        ):
            self.assertIn(operation, protocol)
        self.assertIn("Search does not activate", protocol)
        self.assertIn("PROPOSE_SKILL_BINDING is a proposal, not a commit", protocol)
        self.assertIn(
            "SEARCH_SKILLS is distinct from an ordinary semantic search action",
            protocol,
        )
        self.assertNotIn('"action_type":"REQUEST_SKILL_CONTEXT"', protocol)
        self.assertNotIn('"action_type":"SEARCH_SKILLS"', protocol)
        self.assertNotIn('"action_type":"PROPOSE_SKILL_BINDING"', protocol)
        self.assertIn("exactly one raw\nJSON object", protocol)

        impact_payload = {
            "impact_kind": "taskspec_semantic_change",
            "binding_decision_allowed": False,
            "base_task_revision": 12,
            "new_task_revision": 13,
            "base_plan_revision": 7,
        }
        impact_prompt = PromptManager.get_plan_impact_review_prompt(
            impact_payload
        )
        self.assertIn('"base_task_revision": 12', impact_prompt)
        self.assertIn('"new_task_revision": 13', impact_prompt)
        self.assertIn('"base_plan_revision": 7', impact_prompt)
        self.assertIn('"binding_decision_allowed": false', impact_prompt)
        self.assertIn(
            "a TaskSpec-only\n  semantic change must be handled by another impact decision",
            impact_prompt,
        )

        planner_system = PromptManager.get_planner_v2_system_prompt()
        # Default v3 planning delegates internal revision identities to the host.
        self.assertIn("submit_draft_file", planner_system)
        self.assertIn("finish_task", planner_system)
        self.assertNotIn("new_plan_revision", planner_system)
        planner_human = json.loads(
            PromptManager.get_planner_v2_human_prompt({
                "semantic_history": {},
                "current_focus": {
                    "planning": {"mode": "plan_impact_review"}
                },
            })
        )
        self.assertIn(
            "new_task_revision",
            planner_human["current_focus"]["planning"]["output_fields"],
        )

    def active_state(self, skill_id="genomics/alpha-annotation"):
        state, snapshot, created = self.service.ensure_snapshot(
            new_skill_access_state(),
            skill_id,
            scope="task",
            status="active",
            selected_at_task_revision=12,
        )
        self.assertTrue(created)
        return state, snapshot

    def context(self, state, requests, **runtime_overrides):
        return self.service.serve_context_request(
            state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "need exact fixture evidence",
                "requests": requests,
            },
            runtime_envelope=self.runtime(**runtime_overrides),
        )

    def search(self, state, query, gap="missing annotation capability"):
        return self.service.search_skills(
            state,
            {
                "decision": SEARCH_SKILLS,
                "query": query,
                "capability_gap": gap,
                "desired_outcome": "find a compatible local Skill",
                "requested_scope": "task",
                "relation_hint": "replace",
            },
            runtime_envelope=self.runtime(),
        )

    def test_snapshot_keeps_immutable_bytes_when_live_skill_drifts(self):
        state, snapshot = self.active_state()
        original_hash = snapshot["content_hash"]
        original_snapshot_id = snapshot["snapshot_id"]

        (self.alpha_dir / "SKILL.md").write_text(
            self.alpha_text.replace(
                "Alpha exact input line.",
                "MUTATED live input line.",
            ),
            encoding="utf-8",
        )

        state, result = self.context(
            state,
            [{
                "skill_id": "genomics/alpha-annotation",
                "operation": "section",
                "snapshot_id": original_snapshot_id,
                "heading": "Input requirements",
            }],
        )
        excerpt = result["results"][0]
        self.assertIn("Alpha exact input line.", excerpt["exact_text"])
        self.assertNotIn("MUTATED", excerpt["exact_text"])
        self.assertEqual(excerpt["snapshot_id"], original_snapshot_id)
        self.assertEqual(state["snapshots"][original_snapshot_id]["content_hash"], original_hash)

        state, reused, created = self.service.ensure_snapshot(
            state,
            "genomics/alpha-annotation",
            scope="task",
            status="active",
            selected_at_task_revision=12,
        )
        self.assertFalse(created)
        self.assertEqual(reused["exact_text"], self.alpha_text)
        self.assertEqual(reused["content_hash"], original_hash)

    def test_search_never_downgrades_active_ref_and_catalog_refresh_is_new_candidate(self):
        state, active_snapshot = self.active_state()

        state, same_revision = self.search(state, "alpha_marker")
        same_candidate = next(
            item for item in same_revision["candidates"]
            if item["skill_id"] == "genomics/alpha-annotation"
        )
        self.assertEqual(
            same_candidate["candidate_snapshot_id"],
            active_snapshot["snapshot_id"],
        )
        active_ref = next(
            item for item in state["selected_skill_refs"]
            if item["snapshot_id"] == active_snapshot["snapshot_id"]
        )
        self.assertEqual(active_ref["status"], "active")

        changed_text = self.alpha_text.replace(
            "Alpha immutable overview.",
            "Alpha refreshed catalog overview.",
        )
        (self.alpha_dir / "SKILL.md").write_text(
            changed_text,
            encoding="utf-8",
        )
        refreshed_service = SkillAccessService(
            SkillManager(str(self.skills_root))
        )
        state, refreshed = refreshed_service.search_skills(
            state,
            {
                "decision": SEARCH_SKILLS,
                "query": "alpha_marker",
                "capability_gap": "inspect a refreshed local catalog entry",
                "desired_outcome": "return a new immutable candidate",
                "requested_scope": "task",
                "relation_hint": "supplement",
            },
            runtime_envelope=self.runtime(
                caller_invocation_id="generate-refresh"
            ),
        )
        refreshed_candidate = next(
            item for item in refreshed["candidates"]
            if item["skill_id"] == "genomics/alpha-annotation"
        )
        self.assertNotEqual(
            refreshed_candidate["candidate_snapshot_id"],
            active_snapshot["snapshot_id"],
        )
        refs = {
            item["snapshot_id"]: item
            for item in state["selected_skill_refs"]
            if item["skill_id"] == "genomics/alpha-annotation"
        }
        self.assertEqual(refs[active_snapshot["snapshot_id"]]["status"], "active")
        self.assertEqual(
            refs[refreshed_candidate["candidate_snapshot_id"]]["status"],
            "candidate",
        )

        state, context_result = refreshed_service.serve_context_request(
            state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "verify the refreshed snapshot linkage",
                "requests": [{
                    "skill_id": "genomics/alpha-annotation",
                    "operation": "section",
                    "heading": "Overview",
                }],
            },
            runtime_envelope=self.runtime(
                caller_invocation_id="generate-refresh"
            ),
        )
        self.assertIn(
            "refreshed catalog overview",
            context_result["results"][0]["exact_text"],
        )

    def test_heading_section_search_range_and_full_narrowing(self):
        state, snapshot = self.active_state()

        state, headings_result = self.context(
            state,
            [{
                "skill_id": "genomics/alpha-annotation",
                "operation": "headings",
            }],
        )
        heading_names = {
            item["heading"]
            for item in headings_result["results"][0]["heading_index"]
        }
        self.assertIn("Input requirements", heading_names)
        self.assertIn("Procedure", heading_names)

        state, section_result = self.context(
            state,
            [{
                "skill_id": "genomics/alpha-annotation",
                "operation": "section",
                "heading": "Procedure",
            }],
        )
        section = section_result["results"][0]
        self.assertEqual(section["operation"], "section")
        self.assertIn("Run alpha_marker", section["exact_text"])
        self.assertNotIn("Alpha exact input line", section["exact_text"])

        state, search_result = self.context(
            state,
            [{
                "skill_id": "genomics/alpha-annotation",
                "operation": "search",
                "query": "verified database",
            }],
        )
        search_hit = search_result["results"][0]
        self.assertIn("verified database", search_hit["exact_text"])
        self.assertTrue(search_hit["matched_ranges"])

        target_line = snapshot["exact_text"].splitlines().index(
            "Alpha exact input line."
        ) + 1
        state, range_result = self.context(
            state,
            [{
                "skill_id": "genomics/alpha-annotation",
                "operation": "range",
                "start_line": target_line,
                "end_line": target_line,
            }],
        )
        selected_range = range_result["results"][0]
        self.assertEqual(selected_range["exact_text"], "Alpha exact input line.\n")
        self.assertEqual(selected_range["start_line"], target_line)
        self.assertEqual(selected_range["end_line"], target_line)

        # Full document reads use their own cap plus the host context allowance.
        with mock.patch("agent_core.skill_access.SKILL_FULL_READ_MAX_CHARS", 32):
            _, full_result = self.context(
                state,
                [{
                    "skill_id": "genomics/alpha-annotation",
                    "operation": "full",
                }],
            )
        self.assertEqual(full_result["status"], "PARTIAL")
        full_item = full_result["results"][0]
        self.assertEqual(full_item["status"], "PARTIAL")
        end = full_item['source_end_char']
        self.assertGreater(end, 0)
        self.assertLessEqual(end, 32)
        self.assertEqual(full_item["exact_text"], snapshot["exact_text"][:end])
        self.assertFalse(full_item['ends_mid_line'])
        self.assertEqual(full_item["next_request"]["char_offset"], end)

    def test_context_request_uses_cache_then_returns_no_progress_without_failure_counts(self):
        state, _ = self.active_state()
        state["generate_failure_count"] = 4
        state["execution_retry_count"] = 2
        decision = [{
            "skill_id": "genomics/alpha-annotation",
            "operation": "section",
            "heading": "Input requirements",
        }]

        with mock.patch("agent_core.skill_access.MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION", 2):
            state, first = self.context(state, decision)
            state, second = self.context(state, decision)
            state, third = self.context(state, decision)

        self.assertEqual(first["status"], "COMPLETE")
        self.assertFalse(first["cache_hit"])
        self.assertEqual(second["status"], "COMPLETE")
        self.assertTrue(second["cache_hit"])
        self.assertTrue(second["results"][0]["cache_hit"])
        self.assertEqual(third["status"], "SKILL_CONTEXT_NO_PROGRESS")
        self.assertEqual(third["results"], [])
        self.assertIn("repeated_request_hash", third)
        self.assertEqual(state["generate_failure_count"], 4)
        self.assertEqual(state["execution_retry_count"], 2)
        self.assertNotIn("generate_failure_count", third)
        self.assertNotIn("execution_retry_count", third)
        self.assertNotIn(third["status"], {"TASK_BLOCKED", "EXECUTION_FAILURE"})

    def test_search_distinguishes_matches_no_match_and_incomplete_catalog(self):
        state = new_skill_access_state()
        state, matches = self.search(state, "alpha_marker")
        self.assertEqual(matches["status"], "MATCHES_FOUND")
        self.assertTrue(matches["catalog_complete"])
        alpha = next(
            candidate
            for candidate in matches["candidates"]
            if candidate["skill_id"] == "genomics/alpha-annotation"
        )
        self.assertTrue(alpha["candidate_snapshot_id"].startswith("skill-snapshot-"))
        alpha_ref = next(
            ref
            for ref in state["selected_skill_refs"]
            if ref["skill_id"] == "genomics/alpha-annotation"
        )
        self.assertEqual(alpha_ref["status"], "candidate")
        self.assertFalse(any(
            ref["status"] == "active" for ref in state["selected_skill_refs"]
        ))

        state, no_match = self.search(
            state,
            "zzzz_no_fixture_can_match_zzzz",
            gap="a deliberately absent capability",
        )
        self.assertEqual(no_match["status"], "NO_MATCH")
        self.assertTrue(no_match["catalog_complete"])
        self.assertEqual(no_match["candidates"], [])

        self.manager.catalog_complete = False
        self.manager.catalog_errors = [{
            "code": "SKILL_PROVIDER_UNAVAILABLE",
            "path": "fixture-provider",
        }]
        _, incomplete = self.search(
            state,
            "beta_marker",
            gap="provider-backed catalog cannot be trusted",
        )
        self.assertEqual(incomplete["status"], "CATALOG_INCOMPLETE")
        self.assertFalse(incomplete["catalog_complete"])
        self.assertEqual(incomplete["candidates"], [])
        self.assertEqual(
            incomplete["provider_errors"][0]["code"],
            "SKILL_PROVIDER_UNAVAILABLE",
        )
        self.assertNotIn(
            incomplete["status"],
            {"NO_MATCH", "USER_INPUT_MISSING", "TASK_BLOCKED"},
        )

    def test_typed_parser_accepts_only_one_complete_top_level_object(self):
        decision = {
            "decision": REQUEST_SKILL_CONTEXT,
            "reason": "need exact text",
            "requests": [{
                "skill_id": "genomics/alpha-annotation",
                "operation": "headings",
            }],
        }
        encoded = json.dumps(decision)
        self.assertEqual(parse_typed_skill_decision(encoded), decision)
        self.assertEqual(
            parse_typed_skill_decision(f"```json\n{encoded}\n```"),
            decision,
        )
        self.assertIsNone(parse_typed_skill_decision(f"prefix {encoded}"))
        self.assertIsNone(parse_typed_skill_decision(f"{encoded} trailing"))
        self.assertIsNone(parse_typed_skill_decision(json.dumps({"wrapper": decision})))

        for operation in ("execute", "activate", "install"):
            with self.subTest(operation=operation):
                invalid = deepcopy(decision)
                invalid["requests"][0]["operation"] = operation
                with self.assertRaisesRegex(
                    SkillAccessError, "SKILL_DECISION_SCHEMA_INVALID"
                ):
                    parse_typed_skill_decision(json.dumps(invalid))

        headings_with_query = deepcopy(decision)
        headings_with_query["requests"][0]["query"] = "unexpected"
        with self.assertRaisesRegex(
            SkillAccessError, "SKILL_DECISION_SCHEMA_INVALID"
        ):
            parse_typed_skill_decision(json.dumps(headings_with_query))

    def test_catalog_marks_invalid_entries_incomplete_and_revision_tracks_content(self):
        missing_frontmatter = self.skills_root / "broken" / "missing-frontmatter"
        missing_frontmatter.mkdir(parents=True)
        (missing_frontmatter / "SKILL.md").write_text("# no metadata\n", encoding="utf-8")
        missing_name = self.skills_root / "broken" / "missing-name"
        missing_name.mkdir(parents=True)
        (missing_name / "SKILL.md").write_text(
            "---\ndescription: missing name\n---\n# Body\n",
            encoding="utf-8",
        )
        invalid_yaml = self.skills_root / "broken" / "invalid-yaml"
        invalid_yaml.mkdir(parents=True)
        (invalid_yaml / "SKILL.md").write_text(
            "---\nname: [unterminated\n---\n# Body\n",
            encoding="utf-8",
        )
        unreadable = self.skills_root / "broken" / "invalid-utf8"
        unreadable.mkdir(parents=True)
        (unreadable / "SKILL.md").write_bytes(b"\xff\xfe")

        incomplete = SkillManager(str(self.skills_root))
        status = incomplete.catalog_status()
        self.assertFalse(status["catalog_complete"])
        codes = {item["code"] for item in status["catalog_errors"]}
        self.assertIn("SKILL_CATALOG_FRONTMATTER_MISSING", codes)
        self.assertIn("SKILL_CATALOG_NAME_MISSING", codes)
        self.assertIn("SKILL_CATALOG_ENTRY_INVALID", codes)

        original_revision = self.manager.catalog_status()["catalog_revision"]
        (self.alpha_dir / "SKILL.md").write_text(
            self.alpha_text.replace("Alpha immutable overview.", "Changed overview."),
            encoding="utf-8",
        )
        refreshed = SkillManager(str(self.skills_root))
        content_revision = refreshed.catalog_status()["catalog_revision"]
        self.assertNotEqual(content_revision, original_revision)
        alpha_meta = refreshed.get_skill_meta("genomics/alpha-annotation")
        alpha_meta["aliases"] = ["ranking_alias_changed"]
        for index, item in enumerate(refreshed.discovered_skills):
            if item.get("_skill_id") == "genomics/alpha-annotation":
                refreshed.discovered_skills[index] = alpha_meta
                break
        self.assertNotEqual(
            refreshed.catalog_status()["catalog_revision"],
            content_revision,
        )

    def test_search_revision_and_candidate_snapshot_failures_are_typed_results(self):
        initial = self.manager.catalog_status()
        changed = {**initial, "catalog_revision": initial["catalog_revision"] + "-changed"}
        with mock.patch.object(
            self.manager,
            "catalog_status",
            side_effect=[initial, changed],
        ):
            state, changed_result = self.search(
                new_skill_access_state(), "alpha_marker"
            )
        self.assertEqual(changed_result["status"], "CATALOG_INCOMPLETE")
        self.assertEqual(changed_result["code"], "SKILL_CATALOG_REVISION_CHANGED")
        self.assertEqual(changed_result["candidates"], [])
        self.assertEqual(state["selected_skill_refs"], [])

        manager = SkillManager(str(self.skills_root))
        service = SkillAccessService(manager)
        (self.alpha_dir / "SKILL.md").unlink()
        _, failed = service.search_skills(
            new_skill_access_state(),
            {
                "decision": SEARCH_SKILLS,
                "query": "alpha_marker",
                "capability_gap": "candidate source became unavailable",
                "desired_outcome": "return a typed error",
                "requested_scope": "task",
                "relation_hint": "supplement",
            },
            runtime_envelope=self.runtime(),
        )
        self.assertEqual(failed["status"], "SEARCH_ERROR")
        self.assertEqual(failed["code"], "SKILL_SEARCH_CANDIDATE_SNAPSHOT_ERROR")
        self.assertEqual(failed["candidates"], [])

    def test_search_round_budget_is_shared_by_all_callers_on_the_step(self):
        state = new_skill_access_state()
        with mock.patch("agent_core.skill_access.MAX_SKILL_SEARCH_ROUNDS_PER_STEP", 1):
            state, first = self.search(state, "alpha_marker")
            state, second = self.service.search_skills(
                state,
                {
                    "decision": SEARCH_SKILLS,
                    "query": "beta_marker",
                    "capability_gap": "another caller requests a second search",
                    "desired_outcome": "exercise the shared step budget",
                    "requested_scope": "task",
                    "relation_hint": "supplement",
                },
                runtime_envelope=self.runtime(
                    caller_node="recovery",
                    caller_invocation_id="recovery-001",
                ),
            )
        self.assertEqual(first["status"], "MATCHES_FOUND")
        self.assertEqual(second["status"], "SKILL_SEARCH_NO_PROGRESS")
        self.assertEqual(second["rounds"], 2)

    def test_step_refs_are_target_bound_filtered_and_not_upgraded_by_task_migration(self):
        state, _, _ = self.service.ensure_snapshot(
            new_skill_access_state(),
            "genomics/alpha-annotation",
            scope="step",
            status="active",
            selected_at_task_revision=12,
            target_step_id="step-2",
        )
        current = project_skill_context(
            state, caller_node="generate", current_step_id="step-2"
        )
        other = project_skill_context(
            state, caller_node="generate", current_step_id="step-3"
        )
        self.assertEqual(current["selected_skill_refs"][0]["target_step_id"], "step-2")
        self.assertEqual(other["selected_skill_refs"], [])

        migrated = self.service.ensure_selected_skill_refs(
            state,
            ["genomics/alpha-annotation"],
            task_revision=13,
            scope="task",
        )
        step_refs = [
            item for item in migrated["selected_skill_refs"]
            if item.get("scope") == "step"
        ]
        task_refs = [
            item for item in migrated["selected_skill_refs"]
            if item.get("scope") == "task"
        ]
        self.assertEqual(len(step_refs), 1)
        self.assertEqual(step_refs[0]["target_step_id"], "step-2")
        self.assertEqual(step_refs[0]["status"], "active")
        self.assertEqual(len(task_refs), 1)
        self.assertEqual(task_refs[0]["status"], "active")

    def test_projection_rejects_snapshot_ref_excerpt_and_resource_linkage_tampering(self):
        state, snapshot = self.active_state()
        state, context_result = self.context(
            state,
            [{
                "skill_id": snapshot["skill_id"],
                "operation": "section",
                "heading": "Procedure",
            }],
        )
        valid = project_skill_context(
            state, caller_node="generate", current_step_id="step-2"
        )
        self.assertEqual(len(valid["relevant_skill_excerpts"]), 1)

        bad_ref = dict(state)
        bad_ref["selected_skill_refs"] = [
            dict(item) for item in state["selected_skill_refs"]
        ]
        bad_ref["selected_skill_refs"][0]["source_path"] = "skills/wrong/SKILL.md"
        with self.assertRaises(SkillAccessError) as caught:
            project_skill_context(
                bad_ref, caller_node="generate", current_step_id="step-2"
            )
        self.assertEqual(caught.exception.code, "SKILL_REFERENCE_LINKAGE_INVALID")

        bad_heading = dict(state)
        bad_heading["snapshots"] = dict(state["snapshots"])
        bad_heading["snapshots"][snapshot["snapshot_id"]] = dict(
            state["snapshots"][snapshot["snapshot_id"]]
        )
        bad_heading["snapshots"][snapshot["snapshot_id"]]["heading_index"] = []
        with self.assertRaises(SkillAccessError) as caught:
            project_skill_context(
                bad_heading, caller_node="generate", current_step_id="step-2"
            )
        self.assertEqual(caught.exception.code, "SKILL_SNAPSHOT_LINKAGE_INVALID")

        excerpt_id = context_result["results"][0]["excerpt_id"]
        bad_excerpt = dict(state)
        bad_excerpt["excerpts"] = dict(state["excerpts"])
        bad_excerpt["excerpts"][excerpt_id] = dict(state["excerpts"][excerpt_id])
        bad_excerpt["excerpts"][excerpt_id]["start_line"] += 1
        with self.assertRaises(SkillAccessError) as caught:
            project_skill_context(
                bad_excerpt, caller_node="generate", current_step_id="step-2"
            )
        self.assertEqual(caught.exception.code, "SKILL_EXCERPT_LINKAGE_INVALID")

        state, resource_result = self.context(
            state,
            [{
                "skill_id": snapshot["skill_id"],
                "operation": "resource",
                "resource_path": "references/guide.txt",
            }],
        )
        resource_excerpt_id = resource_result["results"][0]["excerpt_id"]
        state["excerpts"][resource_excerpt_id]["resource_content_hash"] = "tampered"
        with self.assertRaises(SkillAccessError) as caught:
            project_skill_context(
                state, caller_node="generate", current_step_id="step-2"
            )
        self.assertEqual(caught.exception.code, "SKILL_EXCERPT_LINKAGE_INVALID")

    def test_candidate_evidence_is_visible_only_to_explicit_review_projection(self):
        state, _ = self.active_state()
        state, search_result = self.search(state, "beta_marker")
        beta = next(
            item for item in search_result["candidates"]
            if item["skill_id"] == "transcriptomics/beta-expression"
        )
        state, context_result = self.context(
            state,
            [{
                "skill_id": beta["skill_id"],
                "operation": "section",
                "heading": "Workflow",
            }],
        )
        excerpt_id = context_result["results"][0]["excerpt_id"]

        ordinary = project_skill_context(
            state,
            caller_node="planner",
            current_step_id="step-2",
        )
        self.assertNotIn(
            beta["candidate_snapshot_id"],
            {item["snapshot_id"] for item in ordinary["selected_skill_refs"]},
        )
        self.assertFalse(any(
            item.get("snapshot_id") == beta["candidate_snapshot_id"]
            for item in ordinary["relevant_skill_excerpts"]
        ))

        review = project_skill_context(
            state,
            caller_node="planner",
            current_step_id="step-2",
            include_candidate_snapshot_ids=[beta["candidate_snapshot_id"]],
            include_excerpt_ids=[excerpt_id],
        )
        candidate_ref = next(
            item for item in review["selected_skill_refs"]
            if item["snapshot_id"] == beta["candidate_snapshot_id"]
        )
        self.assertEqual(candidate_ref["status"], "candidate")
        self.assertEqual(
            [
                item["excerpt_id"]
                for item in review["relevant_skill_excerpts"]
                if item["snapshot_id"] == beta["candidate_snapshot_id"]
            ],
            [excerpt_id],
        )

    def test_candidate_disposition_stays_in_closed_ref_status_vocabulary(self):
        state, _ = self.active_state()
        state, search_result = self.search(state, "beta_marker")
        beta = next(
            item for item in search_result["candidates"]
            if item["skill_id"] == "transcriptomics/beta-expression"
        )
        candidate_ref = next(
            item for item in state["selected_skill_refs"]
            if item["snapshot_id"] == beta["candidate_snapshot_id"]
        )
        candidate_ref["candidate_disposition"] = "rejected"
        candidate_ref["rejection_reason"] = "fixture rejection"

        projected = project_skill_context(
            state,
            caller_node="generate",
            current_step_id="step-2",
        )
        self.assertEqual(
            [item["skill_id"] for item in projected["selected_skill_refs"]],
            ["genomics/alpha-annotation"],
        )
        self.assertEqual(candidate_ref["status"], "candidate")

        state, _snapshot, created = self.service.ensure_snapshot(
            state,
            beta["skill_id"],
            scope="task",
            status="candidate",
            selected_at_task_revision=12,
        )
        self.assertFalse(created)
        refreshed = next(
            item for item in state["selected_skill_refs"]
            if item["snapshot_id"] == beta["candidate_snapshot_id"]
        )
        self.assertEqual(refreshed["status"], "candidate")
        self.assertNotIn("candidate_disposition", refreshed)

    def test_binding_requires_same_invocation_excerpt_and_enforces_caller_scope(self):
        state, search_result = self.search(new_skill_access_state(), "alpha_marker")
        alpha = search_result["candidates"][0]
        state, context_result = self.context(
            state,
            [{
                "skill_id": alpha["skill_id"],
                "operation": "section",
                "heading": "Procedure",
            }],
        )
        excerpt_id = context_result["results"][0]["excerpt_id"]
        proposal = {
            "decision": PROPOSE_SKILL_BINDING,
            "candidate_skill_id": alpha["skill_id"],
            "candidate_snapshot_id": alpha["candidate_snapshot_id"],
            "requested_scope": "task",
            "relation": "supplement",
            "target_step_id": "step-2",
            "justification": "the candidate supplies exact required behavior",
            "evidence_refs": [excerpt_id],
        }
        with self.assertRaises(SkillAccessError) as caught:
            self.service.propose_binding(
                state,
                {**proposal, "justification": ""},
                runtime_envelope=self.runtime(),
            )
        self.assertEqual(caught.exception.code, "SKILL_DECISION_SCHEMA_INVALID")
        with self.assertRaises(SkillAccessError) as caught:
            self.service.propose_binding(
                state,
                proposal,
                runtime_envelope=self.runtime(caller_invocation_id="generate-other"),
            )
        self.assertEqual(caught.exception.code, "SKILL_BINDING_EVIDENCE_REQUIRED")

        forbidden = {
            **proposal,
            "requested_scope": "current_step",
            "relation": "replace",
        }
        with self.assertRaises(SkillAccessError) as caught:
            self.service.propose_binding(
                state, forbidden, runtime_envelope=self.runtime()
            )
        self.assertEqual(
            caught.exception.code,
            "SKILL_BINDING_CALLER_PERMISSION_DENIED",
        )

    def test_resource_access_blocks_absolute_parent_and_outside_symlink_paths(self):
        outside = self.tmp_path / "outside-secret.txt"
        outside.write_text("must not be exposed\n", encoding="utf-8")
        escape = self.alpha_dir / "references" / "escape.txt"
        escape.symlink_to(outside)
        state, _ = self.active_state()

        for resource_path in (
            "/etc/passwd",
            "../../outside-secret.txt",
            "references/escape.txt",
        ):
            with self.subTest(resource_path=resource_path):
                with self.assertRaises(SkillAccessError) as caught:
                    self.context(
                        state,
                        [{
                            "skill_id": "genomics/alpha-annotation",
                            "operation": "resource",
                            "resource_path": resource_path,
                        }],
                    )
                self.assertEqual(caught.exception.classification, "INTERNAL_PROTOCOL")
                self.assertEqual(caught.exception.code, "SKILL_RESOURCE_OUT_OF_SCOPE")

    def test_legacy_reference_and_script_loaders_cannot_follow_outside_symlinks(self):
        outside_reference = self.tmp_path / "outside-reference.txt"
        outside_reference.write_text("must not be exposed\n", encoding="utf-8")
        outside_script = self.tmp_path / "outside-operation.py"
        outside_script.write_text("print('must not bind')\n", encoding="utf-8")
        (self.alpha_dir / "references" / "escape.txt").symlink_to(
            outside_reference
        )
        scripts = self.alpha_dir / "scripts"
        scripts.mkdir()
        (scripts / "escape.py").symlink_to(outside_script)

        manager = SkillManager(str(self.skills_root))
        meta = manager.get_skill_meta("genomics/alpha-annotation")

        self.assertNotIn("references/escape.txt", meta.get("_references", []))
        self.assertNotIn("scripts/escape.py", meta.get("_scripts", []))
        self.assertEqual(
            manager.load_reference(
                "genomics/alpha-annotation", "references/escape.txt"
            ),
            "",
        )
        self.assertEqual(
            manager.load_script(
                "genomics/alpha-annotation", "scripts/escape.py"
            ),
            "",
        )
        self.assertEqual(
            manager.load_reference(
                "genomics/alpha-annotation",
                str(self.alpha_dir / "references" / "guide.txt"),
            ),
            "",
        )
        _capabilities, operation_bindings = (
            manager.get_semantic_execution_capabilities([
                "genomics/alpha-annotation"
            ])
        )
        self.assertNotIn(str(outside_script.resolve()), operation_bindings.values())

    def test_binding_requires_review_then_activates_and_supersedes_replaced_skill(self):
        state, beta_snapshot, _ = self.service.ensure_snapshot(
            new_skill_access_state(),
            "transcriptomics/beta-expression",
            scope="task",
            status="active",
            selected_at_task_revision=12,
        )
        state, search_result = self.search(state, "alpha_marker")
        alpha = next(
            candidate
            for candidate in search_result["candidates"]
            if candidate["skill_id"] == "genomics/alpha-annotation"
        )
        state, context_result = self.context(
            state,
            [{
                "skill_id": alpha["skill_id"],
                "operation": "section",
                "heading": "Procedure",
            }],
        )
        excerpt_id = context_result["results"][0]["excerpt_id"]

        state, proposal = self.service.propose_binding(
            state,
            {
                "decision": PROPOSE_SKILL_BINDING,
                "candidate_skill_id": alpha["skill_id"],
                "candidate_snapshot_id": alpha["candidate_snapshot_id"],
                "requested_scope": "current_step",
                "relation": "supplement",
                "target_step_id": "step-2",
                "justification": "authoritative fixture method change",
                "evidence_refs": ["user-turn-42", excerpt_id],
            },
            runtime_envelope=self.runtime(),
        )
        self.assertEqual(proposal["status"], "pending_review")
        self.assertEqual(proposal["review_route"], "consultant_skill_resolution")
        with self.assertRaises(SkillAccessError) as caught:
            activate_reviewed_binding(state, proposal["proposal_id"], task_revision=13)
        self.assertEqual(caught.exception.code, "SKILL_BINDING_REVIEW_REQUIRED")

        state, reviewed = review_binding_proposal(
            state,
            proposal["proposal_id"],
            {
                "decision": "ROUTE_TASK_LEVEL_CHANGE",
                "proposal_id": proposal["proposal_id"],
                "candidate_skill_id": alpha["skill_id"],
                "relation": "replace",
                "reason": "the user explicitly selected the replacement",
                "task_fields_affected": ["capability_intent.selected_skill_ids"],
            },
        )
        self.assertEqual(reviewed["status"], "approved_for_task_commit")
        self.assertEqual(reviewed["proposed_requested_scope"], "step")
        self.assertEqual(reviewed["requested_scope"], "task")
        self.assertEqual(reviewed["proposed_relation"], "supplement")
        self.assertEqual(reviewed["relation"], "replace")

        state, active_ids = activate_reviewed_binding(
            state,
            proposal["proposal_id"],
            task_revision=13,
        )
        self.assertEqual(active_ids, ["genomics/alpha-annotation"])
        refs_by_snapshot = {
            ref["snapshot_id"]: ref for ref in state["selected_skill_refs"]
        }
        self.assertEqual(refs_by_snapshot[alpha["candidate_snapshot_id"]]["status"], "active")
        self.assertEqual(refs_by_snapshot[beta_snapshot["snapshot_id"]]["status"], "superseded")
        self.assertEqual(
            refs_by_snapshot[beta_snapshot["snapshot_id"]]["superseded_by"],
            "genomics/alpha-annotation",
        )
        self.assertEqual(
            state["binding_proposals"][proposal["proposal_id"]]["status"],
            "activated_pending_plan_impact",
        )

    def test_reviewed_task_selection_is_exact_and_allows_scope_promotion(self):
        state, _ = self.active_state()
        state, search_result = self.service.search_skills(
            state,
            {
                "decision": SEARCH_SKILLS,
                "query": "beta_marker",
                "capability_gap": "the reviewed change affects the task",
                "desired_outcome": "find the replacement capability",
                "requested_scope": "current_step",
                "relation_hint": "replace",
            },
            runtime_envelope=self.runtime(),
        )
        beta = next(
            item for item in search_result["candidates"]
            if item["skill_id"] == "transcriptomics/beta-expression"
        )
        promoted = {
            "candidate_skill_id": beta["skill_id"],
            "candidate_snapshot_id": beta["candidate_snapshot_id"],
            "candidate_ref_scope": "step",
            "candidate_ref_target_step_id": "step-2",
            "requested_scope": "task",
            "relation": "replace",
        }

        self.assertEqual(
            authorised_task_skill_selection(
                state, promoted
            ),
            ["transcriptomics/beta-expression"],
        )

        supplement = {**promoted, "relation": "supplement"}
        self.assertEqual(
            authorised_task_skill_selection(
                state, supplement
            ),
            [
                "genomics/alpha-annotation",
                "transcriptomics/beta-expression",
            ],
        )

    def test_confirmed_task_skill_change_advances_revision_and_semantic_hash_once(self):
        original = canonical_current_task_spec({
            "task_id": "task-fixture",
            "revision": 12,
            "goal": "annotate variants",
            "inputs": [],
            "parameters": {},
            "output_contracts": [],
            "confirmed_decisions": [],
            "unresolved_questions": [],
            "requirements": [],
        })
        original_hash = task_semantic_hash(original)

        changed = apply_confirmed_task_skill_change(
            original,
            ["genomics/alpha-annotation"],
            source_ref="user-turn-42",
        )
        self.assertEqual(changed["revision"], 13)
        self.assertNotEqual(task_semantic_hash(changed), original_hash)
        self.assertEqual(
            changed["capability_intent"]["selected_skill_ids"],
            ["genomics/alpha-annotation"],
        )
        self.assertIn("user-turn-42", changed["source_refs"])

        unchanged = apply_confirmed_task_skill_change(
            changed,
            ["genomics/alpha-annotation"],
            source_ref="user-turn-42",
        )
        self.assertEqual(unchanged["revision"], 13)
        self.assertEqual(task_semantic_hash(unchanged), task_semantic_hash(changed))

    def test_replan_suffix_carries_passed_prefix_and_retains_superseded_history(self):
        old_steps = [
            {
                "step_id": "step-1",
                "title": "validate inputs",
                "status": "passed",
                "evidence_refs": ["artifact-input-validation"],
            },
            {"step_id": "step-2", "title": "prepare alpha config", "status": "running"},
            {"step_id": "step-3", "title": "build alpha database", "status": "pending"},
            {"step_id": "step-4", "title": "annotate", "status": "pending"},
        ]
        plan = {
            "plan_revision": 7,
            "plan_version": "plan_v7",
            "current_step_id": "step-2",
            "steps": old_steps,
            "superseded_steps": [],
            "plan_status": "approved",
            "approval_status": "approved",
            "can_generate": True,
        }
        decision = {
            "decision": "REPLAN_SUFFIX",
            "base_task_revision": 13,
            "new_task_revision": 13,
            "base_plan_revision": 7,
            "carry_forward": [{
                "step_id": "step-1",
                "evidence_refs": ["artifact-input-validation"],
            }],
            "supersede": ["step-2", "step-3", "step-4"],
            "replacement_suffix": [
                {"step_id": "step-5", "title": "prepare beta config", "status": "pending"},
                {"step_id": "step-6", "title": "annotate with beta", "status": "pending"},
            ],
            "reason": "the selected task-level Skill changed",
        }

        replanned = apply_plan_impact_review(
            plan,
            decision,
            new_task_revision=13,
            valid_skill_snapshot_ids=set(),
        )
        self.assertEqual(replanned["plan_revision"], 8)
        self.assertEqual(replanned["plan_version"], "plan_v8")
        self.assertEqual(replanned["generated_from_task_revision"], 13)
        self.assertEqual(
            [step["step_id"] for step in replanned["steps"]],
            ["step-1", "step-5", "step-6"],
        )
        self.assertEqual(replanned["steps"][0]["status"], "passed")
        self.assertEqual(replanned["current_step_id"], "step-5")
        superseded = {
            step["step_id"]: step for step in replanned["superseded_steps"]
        }
        self.assertEqual(set(superseded), {"step-2", "step-3", "step-4"})
        self.assertTrue(all(step["status"] == "superseded" for step in superseded.values()))
        self.assertTrue(all(
            step["superseded_at_plan_revision"] == 8
            for step in superseded.values()
        ))
        self.assertTrue({"step-5", "step-6"}.isdisjoint(superseded))
        self.assertEqual(replanned["approval_status"], "awaiting_review")
        self.assertFalse(replanned["can_generate"])

    def test_plan_impact_requires_explicit_old_and_new_task_revisions(self):
        accepted = validate_plan_impact_decision(
            {
                "decision": "REJECT_PROPOSAL",
                "base_task_revision": 12,
                "new_task_revision": 13,
                "base_plan_revision": 7,
                "reason": "The candidate does not satisfy the declared goal.",
            },
            base_task_revision=12,
            new_task_revision=13,
            base_plan_revision=7,
        )
        self.assertEqual(accepted["base_task_revision"], 12)
        self.assertEqual(accepted["new_task_revision"], 13)

        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_SCHEMA_INVALID"
        ):
            validate_plan_impact_decision(
                {
                    "decision": "REJECT_PROPOSAL",
                    "base_task_revision": 12,
                    "base_plan_revision": 7,
                    "reason": "Missing the current authority revision.",
                },
                base_task_revision=12,
                new_task_revision=13,
                base_plan_revision=7,
            )

        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_SCHEMA_INVALID"
        ):
            parse_strict_json_object(
                '{"decision":"REJECT_PROPOSAL"} trailing prose',
                error_code="PLAN_IMPACT_SCHEMA_INVALID",
            )

    def test_plan_impact_rejects_bind_and_resume_for_task_replacement(self):
        decision = {
            "decision": "BIND_AND_RESUME",
            "base_task_revision": 12,
            "new_task_revision": 13,
            "base_plan_revision": 7,
            "reason": "Fixture decision.",
            "skill_refs": [{"snapshot_id": "candidate-snapshot"}],
        }
        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_BIND_SCOPE_INVALID"
        ):
            validate_plan_impact_proposal_compatibility(
                decision,
                {
                    "requested_scope": "task",
                    "relation": "replace",
                    "base_step_id": "step-2",
                },
                current_step_id="step-2",
            )

        state, search_result = self.search(
            new_skill_access_state(), "beta_marker"
        )
        beta = next(
            item for item in search_result["candidates"]
            if item["skill_id"] == "transcriptomics/beta-expression"
        )
        planner_runtime = self.runtime(
            caller_node="planner",
            caller_invocation_id="planner-1",
        )
        state, context_result = self.service.serve_context_request(
            state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "Need exact candidate evidence.",
                "requests": [{
                    "skill_id": beta["skill_id"],
                    "operation": "section",
                    "heading": "Workflow",
                }],
            },
            runtime_envelope=planner_runtime,
        )
        with self.assertRaisesRegex(
            SkillAccessError, "SKILL_BINDING_CALLER_PERMISSION_DENIED"
        ):
            self.service.propose_binding(
                state,
                {
                    "decision": PROPOSE_SKILL_BINDING,
                    "candidate_skill_id": beta["skill_id"],
                    "candidate_snapshot_id": beta["candidate_snapshot_id"],
                    "requested_scope": "task",
                    "relation": "replace",
                    "justification": "Planner cannot own a task replacement.",
                    "evidence_refs": [
                        context_result["results"][0]["excerpt_id"]
                    ],
                },
                runtime_envelope=planner_runtime,
            )

    def test_bind_and_resume_is_additive_and_replan_rows_are_strict_objects(self):
        old_ref = {
            "skill_id": "genomics/alpha-annotation",
            "snapshot_id": "snapshot-alpha",
            "source_path": "skills/genomics/alpha-annotation/SKILL.md",
            "content_hash": "a" * 64,
            "scope": "task",
            "status": "active",
        }
        replacement_ref = {
            "skill_id": "transcriptomics/beta-expression",
            "snapshot_id": "snapshot-beta",
            "source_path": "skills/transcriptomics/beta-expression/SKILL.md",
            "content_hash": "b" * 64,
            "scope": "task",
            "status": "active",
        }
        plan = {
            "plan_revision": 7,
            "current_step_id": "step-2",
            "steps": [{
                "step_id": "step-2",
                "status": "running",
                "skill_ids": [old_ref["skill_id"]],
                "skill_refs": [old_ref],
            }],
        }
        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_BIND_NOT_ADDITIVE"
        ):
            apply_plan_impact_review(
                plan,
                {
                    "decision": "BIND_AND_RESUME",
                    "base_task_revision": 12,
                    "new_task_revision": 12,
                    "base_plan_revision": 7,
                    "reason": "A supplement cannot silently remove an old ref.",
                    "skill_refs": [replacement_ref],
                },
                base_task_revision=12,
                new_task_revision=12,
                valid_skill_snapshot_ids={"snapshot-alpha", "snapshot-beta"},
                valid_skill_refs=[old_ref, replacement_ref],
            )

        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_SCHEMA_INVALID"
        ):
            validate_plan_impact_decision(
                {
                    "decision": "REPLAN_SUFFIX",
                    "base_task_revision": 12,
                    "new_task_revision": 13,
                    "base_plan_revision": 7,
                    "reason": "Malformed rows must not be filtered away.",
                    "carry_forward": ["step-1"],
                    "supersede": ["step-2"],
                    "replacement_suffix": [{"step_id": "step-3"}],
                },
                base_task_revision=12,
                new_task_revision=13,
                base_plan_revision=7,
            )

    def test_plan_skill_refs_must_be_canonical_and_match_skill_ids(self):
        canonical_ref = {
            "skill_id": "transcriptomics/beta-expression",
            "snapshot_id": "snapshot-beta",
            "source_path": "skills/transcriptomics/beta-expression/SKILL.md",
            "content_hash": "b" * 64,
            "scope": "task",
            "status": "active",
        }
        stale_projection = {
            **canonical_ref,
            "scope": "step",
            "status": "candidate",
            "target_step_id": "step-2",
        }
        plan = {
            "plan_revision": 7,
            "current_step_id": "step-2",
            "steps": [{"step_id": "step-2", "status": "running"}],
        }
        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_SKILL_REFERENCE_INVALID"
        ):
            apply_plan_impact_review(
                plan,
                {
                    "decision": "BIND_AND_RESUME",
                    "base_task_revision": 12,
                    "new_task_revision": 12,
                    "base_plan_revision": 7,
                    "reason": "A stale pre-promotion ref is not canonical.",
                    "skill_refs": [stale_projection],
                },
                base_task_revision=12,
                new_task_revision=12,
                valid_skill_snapshot_ids={"snapshot-beta"},
                valid_skill_refs=[canonical_ref],
            )

        suffix_plan = {
            "plan_revision": 7,
            "current_step_id": "step-2",
            "steps": [
                {
                    "step_id": "step-1",
                    "status": "passed",
                    "evidence_refs": ["evidence-1"],
                },
                {"step_id": "step-2", "status": "running"},
            ],
        }
        with self.assertRaisesRegex(
            SkillAccessError, "PLAN_IMPACT_SKILL_REFERENCE_INVALID"
        ):
            apply_plan_impact_review(
                suffix_plan,
                {
                    "decision": "REPLAN_SUFFIX",
                    "base_task_revision": 12,
                    "new_task_revision": 13,
                    "base_plan_revision": 7,
                    "reason": "skill_ids must equal the canonical ref IDs.",
                    "carry_forward": [{
                        "step_id": "step-1",
                        "evidence_refs": ["evidence-1"],
                    }],
                    "supersede": ["step-2"],
                    "replacement_suffix": [{
                        "step_id": "step-3",
                        "status": "pending",
                        "skill_refs": [canonical_ref],
                        "skill_ids": [],
                    }],
                },
                base_task_revision=12,
                new_task_revision=13,
                valid_skill_snapshot_ids={"snapshot-beta"},
                valid_skill_refs=[canonical_ref],
            )

    def test_replan_suffix_rejects_noncontiguous_carry_forward(self):
        plan = {
            "plan_revision": 7,
            "steps": [
                {
                    "step_id": "step-1",
                    "status": "passed",
                    "evidence_refs": ["evidence-1"],
                },
                {
                    "step_id": "step-2",
                    "status": "passed",
                    "evidence_refs": ["evidence-2"],
                },
                {"step_id": "step-3", "status": "running"},
            ],
        }
        decision = {
            "decision": "REPLAN_SUFFIX",
            "base_task_revision": 12,
            "new_task_revision": 13,
            "base_plan_revision": 7,
            "carry_forward": [{
                "step_id": "step-2",
                "evidence_refs": ["evidence-2"],
            }],
            "supersede": ["step-1", "step-3"],
            "replacement_suffix": [{"step_id": "step-4"}],
            "reason": "Only a non-prefix step was carried forward.",
        }
        with self.assertRaisesRegex(
            SkillAccessError,
            "PLAN_IMPACT_CARRY_FORWARD_NOT_CONTIGUOUS_PREFIX",
        ):
            apply_plan_impact_review(
                plan,
                decision,
                base_task_revision=12,
                new_task_revision=13,
                valid_skill_snapshot_ids=set(),
            )

    def test_stale_node_result_discards_old_revisions_without_counting_failure(self):
        self.assertIsNone(stale_node_result(
            request_task_revision=13,
            request_plan_revision=8,
            current_task_revision=13,
            current_plan_revision=8,
            caller_node="generate",
            caller_invocation_id="generate-current",
        ))

        stale = stale_node_result(
            request_task_revision=12,
            request_plan_revision=7,
            current_task_revision=13,
            current_plan_revision=8,
            caller_node="generate",
            caller_invocation_id="generate-old",
        )
        self.assertEqual(stale["decision_result"], "STALE_NODE_RESULT")
        self.assertEqual(stale["status"], "discarded")
        self.assertEqual(
            stale["changed_preconditions"],
            ["TASK_REVISION_MISMATCH", "PLAN_REVISION_MISMATCH"],
        )
        self.assertFalse(stale["counts_as_failure"])
        self.assertFalse(stale["consumes_execution_retry"])
        self.assertFalse(stale["execute"])


if __name__ == "__main__":
    unittest.main()
