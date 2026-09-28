from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import HumanMessage

from agent_core.skill_access import (
    REQUEST_SKILL_CONTEXT,
    SkillAccessError,
    SkillAccessService,
    new_skill_access_state,
    record_provider_visible_materials,
    selected_skill_materials_for_carry,
)
from agent_core.skill_manager import SkillManager
from prompts.manager import (
    INHERITED_SKILL_MATERIAL_POLICY_V1,
    PromptManager,
)


class SkillMaterialContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skills_root = Path(self._tmp.name) / "skills"
        self._invocation_ordinal = 0

        self.skill_texts = {
            "fixture/full": self._write_skill(
                "fixture/full",
                "Full Carry",
                "FULL_SKILL_BODY_SENTINEL",
            ),
            "fixture/partial": self._write_skill(
                "fixture/partial",
                "Partial Carry",
                "PARTIAL_SKILL_BODY_SENTINEL",
            ),
            "fixture/resources": self._write_skill(
                "fixture/resources",
                "Resource Carry",
                "RESOURCE_SKILL_BODY_SENTINEL",
                resources={
                    "scripts/run.sh": "IDENTICAL_RESOURCE_BODY\necho shared\n",
                    "examples/run.sh": "IDENTICAL_RESOURCE_BODY\necho shared\n",
                    "templates/job.yaml": "threads: 8\nmode: exact-template\n",
                },
            ),
            "fixture/task": self._write_skill(
                "fixture/task", "Task Scope", "TASK_SCOPE_SENTINEL"
            ),
            "fixture/superseded": self._write_skill(
                "fixture/superseded",
                "Superseded Scope",
                "SUPERSEDED_SENTINEL",
            ),
            "fixture/step-match": self._write_skill(
                "fixture/step-match",
                "Matching Step Scope",
                "MATCHING_STEP_SENTINEL",
            ),
            "fixture/step-other": self._write_skill(
                "fixture/step-other",
                "Other Step Scope",
                "OTHER_STEP_SENTINEL",
            ),
        }
        self.service = SkillAccessService(SkillManager(str(self.skills_root)))

    def _write_skill(
        self,
        skill_id: str,
        name: str,
        marker: str,
        *,
        resources: dict[str, str] | None = None,
    ) -> str:
        directory = self.skills_root / skill_id
        directory.mkdir(parents=True, exist_ok=True)
        text = (
            "---\n"
            f"name: {name}\n"
            f"description: Offline fixture for {skill_id}\n"
            "---\n"
            "# Overview\n"
            f"Overview for {skill_id}.\n"
            "## Procedure\n"
            f"{marker}\n"
            "Preserve this exact procedure line.\n"
            "## Notes\n"
            "No semantic filtering is permitted.\n"
        )
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        for relative, body in (resources or {}).items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return text

    def _runtime(self, *, current_step_id: str = "step-2") -> dict[str, object]:
        self._invocation_ordinal += 1
        return {
            "caller_node": "consultant",
            "caller_invocation_id": f"consultant-{self._invocation_ordinal:03d}",
            "task_revision": 7,
            "plan_revision": 3,
            "authoritative_user_turn_revision": 2,
            "current_step_id": current_step_id,
        }

    def _activate(
        self,
        state: dict,
        skill_id: str,
    ) -> tuple[dict, dict]:
        state, snapshot, _created = self.service.ensure_snapshot(
            state,
            skill_id,
            scope="task",
            status="active",
            selected_at_task_revision=7,
        )
        return state, snapshot

    def _record_full(
        self,
        state: dict,
        skill_id: str,
        *,
        event_id: str,
    ) -> tuple[dict, dict, dict]:
        state, snapshot = self._activate(state, skill_id)
        state, documents = self.service.load_skill_documents(
            state,
            [skill_id],
            task_revision=7,
            request_id=f"load-{skill_id}",
        )
        descriptor = {
            "kind": "skill_document",
            "snapshot_id": snapshot["snapshot_id"],
            "content_hash": snapshot["content_hash"],
        }
        state = record_provider_visible_materials(
            state,
            [descriptor],
            phase="consultant",
            invocation_id="consultant-full",
            event_id=event_id,
        )
        return state, snapshot, documents[0]

    def _record_excerpts(
        self,
        state: dict,
        skill_id: str,
        requests: list[dict[str, object]],
        *,
        event_id: str,
    ) -> tuple[dict, list[dict]]:
        state, _snapshot = self._activate(state, skill_id)
        state, result = self.service.serve_context_request(
            state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "exercise exact carry coverage",
                "requests": requests,
            },
            runtime_envelope=self._runtime(),
        )
        excerpts = [
            dict(item)
            for item in result["results"]
            if isinstance(item, dict) and item.get("excerpt_id")
        ]
        self.assertEqual(len(excerpts), len(requests))
        state = record_provider_visible_materials(
            state,
            [
                {
                    "kind": "excerpt",
                    "excerpt_id": item["excerpt_id"],
                    "content_hash": item["content_hash"],
                }
                for item in excerpts
            ],
            phase="consultant",
            invocation_id="consultant-excerpts",
            event_id=event_id,
        )
        return state, excerpts

    def test_full_partial_and_script_example_template_sources_carry_exactly(self) -> None:
        state = new_skill_access_state()
        state, full_snapshot, _document = self._record_full(
            state, "fixture/full", event_id="provider-event-full"
        )
        state, partial = self._record_excerpts(
            state,
            "fixture/partial",
            [{
                "skill_id": "fixture/partial",
                "operation": "section",
                "heading": "Procedure",
            }],
            event_id="provider-event-partial",
        )
        state, resources = self._record_excerpts(
            state,
            "fixture/resources",
            [
                {
                    "skill_id": "fixture/resources",
                    "operation": "resource",
                    "resource_path": path,
                }
                for path in (
                    "scripts/run.sh",
                    "examples/run.sh",
                    "templates/job.yaml",
                )
            ],
            event_id="provider-event-resources",
        )

        # Replaying one business event is idempotent, while a new event for the
        # same source remains auditable without duplicating the source body.
        full_descriptor = {
            "kind": "skill_document",
            "snapshot_id": full_snapshot["snapshot_id"],
            "content_hash": full_snapshot["content_hash"],
        }
        state = record_provider_visible_materials(
            state,
            [full_descriptor],
            phase="consultant",
            invocation_id="consultant-full",
            event_id="provider-event-full",
        )
        state = record_provider_visible_materials(
            state,
            [full_descriptor],
            phase="consultant",
            invocation_id="consultant-full",
            event_id="provider-event-full-2",
        )

        carried = selected_skill_materials_for_carry(
            state, current_step_id="step-2"
        )
        by_skill = {}
        for item in carried:
            by_skill.setdefault(item["skill_id"], []).append(item)

        full = by_skill["fixture/full"]
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0]["source_kind"], "skill_document")
        self.assertEqual(full[0]["coverage"], {"kind": "full"})
        self.assertEqual(full[0]["body"], self.skill_texts["fixture/full"])
        full_events = state["provider_visible_materials"][
            full[0]["material_id"]
        ]["visibility_events"]
        self.assertEqual(
            [item["event_id"] for item in full_events],
            ["provider-event-full", "provider-event-full-2"],
        )

        partial_rows = by_skill["fixture/partial"]
        self.assertEqual(len(partial_rows), 1)
        self.assertEqual(
            partial_rows[0]["source_kind"], "skill_document_excerpt"
        )
        self.assertEqual(partial_rows[0]["body"], partial[0]["exact_text"])
        self.assertEqual(partial_rows[0]["coverage"]["kind"], "range")

        resource_rows = {
            item["resource_path"]: item
            for item in by_skill["fixture/resources"]
        }
        self.assertEqual(
            set(resource_rows),
            {"scripts/run.sh", "examples/run.sh", "templates/job.yaml"},
        )
        expected_resources = {
            item["resource_path"]: item["exact_text"] for item in resources
        }
        for path, item in resource_rows.items():
            self.assertEqual(item["source_kind"], "skill_resource")
            self.assertEqual(item["body"], expected_resources[path])
            self.assertEqual(item["coverage"]["kind"], "range")

        script = resource_rows["scripts/run.sh"]
        example = resource_rows["examples/run.sh"]
        self.assertEqual(script["body"], example["body"])
        self.assertEqual(script["content_hash"], example["content_hash"])
        self.assertNotEqual(script["material_id"], example["material_id"])
        self.assertNotEqual(script["source_ref"], example["source_ref"])

    def test_carry_filters_superseded_and_nonmatching_step_scope(self) -> None:
        state = new_skill_access_state()
        snapshots = {}
        for skill_id in (
            "fixture/task",
            "fixture/superseded",
            "fixture/step-match",
            "fixture/step-other",
        ):
            state, snapshot, _document = self._record_full(
                state, skill_id, event_id=f"visible-{skill_id}"
            )
            snapshots[skill_id] = snapshot

        for ref in state["selected_skill_refs"]:
            skill_id = ref["skill_id"]
            if skill_id == "fixture/superseded":
                ref["status"] = "superseded"
                ref["superseded_at_task_revision"] = 8
            elif skill_id == "fixture/step-match":
                ref["scope"] = "step"
                ref["target_step_id"] = "step-2"
            elif skill_id == "fixture/step-other":
                ref["scope"] = "step"
                ref["target_step_id"] = "step-9"

        step_2 = selected_skill_materials_for_carry(
            state, current_step_id="step-2"
        )
        self.assertEqual(
            {item["skill_id"] for item in step_2},
            {"fixture/task", "fixture/step-match"},
        )
        step_9 = selected_skill_materials_for_carry(
            state, current_step_id="step-9"
        )
        self.assertEqual(
            {item["skill_id"] for item in step_9},
            {"fixture/task", "fixture/step-other"},
        )
        self.assertNotIn(
            snapshots["fixture/superseded"]["snapshot_id"],
            {item["snapshot_id"] for item in [*step_2, *step_9]},
        )



    def test_shared_material_policy_is_stable_in_consultant_and_planner(self) -> None:
        prompts = (
            PromptManager.get_consultant_v2_prompt({}),
            PromptManager.get_consultant_discovery_escalation_prompt({}),
            PromptManager.get_planner_v2_system_prompt(),
        )
        for prompt in prompts:
            self.assertIn(INHERITED_SKILL_MATERIAL_POLICY_V1, prompt)
            self.assertIn("Selection does not mean the whole Skill directory", prompt)
            self.assertIn("summarized or externalized", prompt)
            self.assertIn("never as authority", prompt)


if __name__ == "__main__":
    unittest.main()
