import json
import tempfile
import unittest
from pathlib import Path


from agent_core.knowledge_conflicts import (
    ClaimRegistry,
    ConflictResolver,
    KnowledgeConflictDetector,
    KnowledgeConflictStore,
    PlanRevisionManager,
)


class KnowledgeConflictTests(unittest.TestCase):
    @staticmethod
    def _semantic(semantic_key, value, operator="eq"):
        return {
            "semantic_key": semantic_key,
            "fact_key": semantic_key,
            "operator": operator,
            "value": value,
        }

    def test_generic_task_contract_prose_does_not_create_keyword_conflict(self):
        registry = ClaimRegistry()
        registry.register(
            "Task may have missing inputs and should write a report when complete",
            source_type="task_contract",
            strength="required",
            scope="task",
            scope_id="task",
        )
        registry.register(
            "Plan checks inputs and then writes a report",
            source_type="plan",
            strength="required",
            scope="stage",
            scope_id="step-1",
        )

        conflicts = KnowledgeConflictDetector.detect(registry)

        self.assertEqual(conflicts, [])

    def test_claim_registry_tracks_strength_and_supersession(self):
        registry = ClaimRegistry()
        old = registry.register_user_constraint("must use the declared reference")
        new = registry.register(
            proposition="must use the verified reference",
            source_type="artifact_validation",
            strength="observation",
            scope="artifact",
            scope_id="reference",
            supersedes_claim_id=old.claim_id,
        )
        self.assertTrue(registry.get(new.claim_id).active)
        self.assertEqual(registry.get(old.claim_id).status, "superseded")

    def test_latest_observation_supersedes_obsolete_fact(self):
        registry = ClaimRegistry()
        old = registry.register_observation(
            "runtime launcher is ann",
            "runtime_argv",
            scope="command",
            scope_id="step-1",
            attributes={"fact_key": "launcher", "value": "ann"},
        )
        new = registry.register_observation(
            "runtime launcher is build",
            "runtime_argv",
            scope="command",
            scope_id="step-1",
            attributes={"fact_key": "launcher", "value": "build"},
        )
        self.assertFalse(old.active)
        self.assertTrue(new.active)

    def test_user_plan_conflict_requires_decision(self):
        registry = ClaimRegistry()
        registry.register_user_constraint(
            "required_behavior.database_source eq custom_genome",
            attributes=self._semantic("required_behavior.database_source", "custom_genome"),
        )
        registry.register_plan_assumption(
            "required_behavior.database_source eq preset",
            scope="stage",
            scope_id="step-1",
            attributes=self._semantic("required_behavior.database_source", "preset"),
        )
        conflicts = KnowledgeConflictDetector.detect(registry)
        self.assertEqual(conflicts[0].conflict_type, "USER_PLAN_CONFLICT")
        self.assertTrue(conflicts[0].blocking)
        self.assertEqual(ConflictResolver.resolve(conflicts[0], registry).decision, "request_user_decision")

    def test_user_recovery_conflict_never_allows_constraint_removal(self):
        registry = ClaimRegistry()
        registry.register_user_constraint(
            "required_behavior.required_option eq true",
            attributes=self._semantic("required_behavior.required_option", True),
        )
        registry.register(
            "required_behavior.required_option eq false",
            source_type="recovery_proposal",
            strength="inference",
            scope="stage",
            scope_id="step-1",
            attributes=self._semantic("required_behavior.required_option", False),
        )
        conflict = KnowledgeConflictDetector.detect(registry)[0]
        decision = ConflictResolver.resolve(conflict, registry)
        self.assertEqual(conflict.conflict_type, "USER_RECOVERY_CONFLICT")
        self.assertEqual(decision.decision, "require_probe")
        self.assertTrue(any("required_option" in item for item in decision.forbidden_changes))

    def test_plan_runtime_and_environment_conflicts_are_distinct(self):
        registry = ClaimRegistry()
        registry.register("subcommand=build", source_type="plan", strength="required", scope="stage", scope_id="step-1", attributes=self._semantic("runtime_constraint.subcommand", "build"))
        registry.register("subcommand=ann", source_type="runtime_argv", strength="observation", scope="stage", scope_id="step-1", attributes=self._semantic("runtime_constraint.subcommand", "ann"))
        registry.register("environment=env_a", source_type="plan_assumption", strength="assumption", scope="environment", scope_id="env_a", attributes=self._semantic("runtime_constraint.environment", "env_a"))
        registry.register("environment=env_b", source_type="environment_probe", strength="observation", scope="environment", scope_id="env_a", attributes=self._semantic("runtime_constraint.environment", "env_b"))
        types = {item.conflict_type for item in KnowledgeConflictDetector.detect(registry)}
        self.assertIn("PLAN_RUNTIME_CONFLICT", types)
        self.assertIn("PLAN_ENVIRONMENT_CONFLICT", types)

    def test_plan_runtime_conflict_requires_probe_before_replanning(self):
        registry = ClaimRegistry()
        registry.register("subcommand=build", source_type="plan", strength="required", scope="stage", scope_id="step-1", attributes=self._semantic("runtime_constraint.subcommand", "build"))
        registry.register("subcommand=ann", source_type="runtime_argv", strength="observation", scope="stage", scope_id="step-1", attributes=self._semantic("runtime_constraint.subcommand", "ann"))
        conflict = next(item for item in KnowledgeConflictDetector.detect(registry) if item.conflict_type == "PLAN_RUNTIME_CONFLICT")
        decision = ConflictResolver.resolve(conflict, registry)
        self.assertEqual(decision.decision, "require_probe")
        self.assertFalse(decision.requires_user_approval)

    def test_runtime_atomic_facts_do_not_conflict_with_generic_step_title(self):
        registry = ClaimRegistry()
        registry.register(
            "Step 1: verify environment | Skills: none | Done: runtime confirmed",
            source_type="plan",
            strength="required",
            scope="stage",
            scope_id="step-1",
        )
        registry.register_observation(
            "Runtime command: tool --version",
            "runtime_argv",
            scope="command",
            scope_id="step-1",
            attributes={"fact_key": "execution_command", "value": "tool --version"},
        )
        registry.register_observation(
            "Runtime return code: 0",
            "runtime_output",
            scope="command",
            scope_id="step-1",
            attributes={"fact_key": "returncode", "value": 0},
        )

        conflicts = KnowledgeConflictDetector.detect(registry)
        self.assertFalse(
            any(item.conflict_type == "PLAN_RUNTIME_CONFLICT" for item in conflicts),
            [item.to_dict() for item in conflicts],
        )

    def test_full_plan_document_is_not_an_atomic_runtime_claim(self):
        registry = ClaimRegistry()
        registry.register_plan_assumption(
            "# Detailed plan\n" + ("build database, annotate output, and write report\n" * 100),
            scope="stage",
            scope_id="step-1",
        )
        registry.register_observation(
            "Runtime command: bash /task_outputs/task/step_21.sh",
            "runtime_argv",
            scope="command",
            scope_id="step-1",
            attributes={
                "fact_key": "execution_command",
                "value": "bash /task_outputs/task/step_21.sh",
                "step_ids": ["step-1"],
            },
        )

        conflicts = KnowledgeConflictDetector.detect(registry)

        self.assertFalse(
            any(item.conflict_type == "PLAN_RUNTIME_CONFLICT" for item in conflicts),
            [item.to_dict() for item in conflicts],
        )

    def test_artifact_and_stage_contract_conflicts(self):
        registry = ClaimRegistry()
        registry.register("output_format=A", source_type="plan", strength="required", scope="artifact", scope_id="out", attributes=self._semantic("required_output.result_format", "A"))
        registry.register("output_format=B", source_type="artifact_validation", strength="observation", scope="artifact", scope_id="out", attributes=self._semantic("required_output.result_format", "B"))
        registry.register("output_format=C", source_type="upstream_stage", strength="observation", scope="artifact", scope_id="out", attributes=self._semantic("required_output.result_format", "C"))
        types = {item.conflict_type for item in KnowledgeConflictDetector.detect(registry)}
        self.assertIn("PLAN_ARTIFACT_CONFLICT", types)
        self.assertIn("STAGE_CONTRACT_CONFLICT", types)

    def test_tool_skill_and_recovery_conflicts_require_probe(self):
        registry = ClaimRegistry()
        registry.register("option=format_x", source_type="skill_document", strength="required", scope="command", scope_id="step-1", attributes=self._semantic("runtime_constraint.option_format", "format_x"))
        registry.register("option=format_y", source_type="runtime_output", strength="observation", scope="command", scope_id="step-1", attributes=self._semantic("runtime_constraint.option_format", "format_y"))
        registry.register("option=format_x", source_type="tool_help", strength="observation", scope="command", scope_id="step-1", attributes=self._semantic("runtime_constraint.option_format", "format_x"))
        registry.register("option=format_y", source_type="runtime_output", strength="observation", scope="command", scope_id="step-1", attributes=self._semantic("runtime_constraint.option_format", "format_y"), deduplicate=False)
        types = {item.conflict_type for item in KnowledgeConflictDetector.detect(registry)}
        self.assertIn("SKILL_RUNTIME_CONFLICT", types)
        self.assertIn("TOOL_BEHAVIOR_CONFLICT", types)

    def test_stale_plan_and_deduplication(self):
        registry = ClaimRegistry()
        registry.register(
            "plan state was captured",
            source_type="plan_assumption",
            strength="assumption",
            scope="stage",
            scope_id="step-1",
            attributes={"fingerprint": "old", "observed_fingerprint": "new", "step_ids": ["step-1"]},
        )
        first = KnowledgeConflictDetector.detect_stale_plan(registry)
        second = KnowledgeConflictDetector.detect_stale_plan(registry)
        self.assertEqual(first[0].conflict_type, "STALE_PLAN")
        self.assertEqual(first[0].fingerprint, second[0].fingerprint)

    def test_plan_revision_preserves_constraints_and_invalidates_downstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {
                "task_id": "task",
                "plan_version": "plan_v1",
                "user_constraints": ["must use custom reference"],
                "steps": [
                    {"step_id": "step-1", "title": "build", "depends_on": []},
                    {"step_id": "step-2", "title": "annotate", "depends_on": ["step-1"]},
                    {"step_id": "step-3", "title": "report", "depends_on": ["step-2"]},
                ],
            }
            manager = PlanRevisionManager(state, root, task_id="task")
            revision = manager.revise(
                new_plan_state={"steps": [{"step_id": "step-1", "title": "build with direct launcher"}]},
                trigger_conflict_ids=["c1"],
                revision_scope="single_step",
                changed_step_ids=["step-1"],
                preserved_constraints=["must use custom reference"],
                reason="launcher mismatch",
            )
            self.assertEqual(revision.status, "applied", revision.to_dict())
            self.assertEqual(revision.parent_plan_version, "plan_v1")
            self.assertEqual(revision.new_plan_version, "plan_v2")
            self.assertEqual(revision.invalidated_step_ids, ["step-2", "step-3"])
            self.assertTrue((root / "plans" / "plan_v2.json").exists())
            self.assertTrue((root / "plans" / "plan_v1_to_plan_v2.diff.json").exists())
            self.assertEqual(manager.plan_state["user_constraints"], ["must use custom reference"])

            allowed, reason = manager.assert_step_executable("step-1")
            self.assertFalse(allowed)
            self.assertIn("revalidation", reason.lower())
            allowed, reason = manager.assert_step_executable("step-2")
            self.assertFalse(allowed)
            manager.mark_step_revalidated("step-1")
            allowed, reason = manager.assert_step_executable("step-2")
            self.assertFalse(allowed)
            manager.mark_step_revalidated("step-2")
            allowed, reason = manager.assert_step_executable("step-2")
            self.assertTrue(allowed, reason)

    def test_plan_revision_rejects_constraint_drift_and_oscillation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "task_id": "task",
                "user_constraints": ["must preserve option"],
                "steps": [{"step_id": "step-1", "title": "run"}],
            }
            manager = PlanRevisionManager(state, tmp, task_id="task")
            removed = manager.revise(
                new_plan_state={"steps": [{"step_id": "step-1", "title": "run without option"}], "user_constraints": []},
                revision_scope="single_step",
                changed_step_ids=["step-1"],
                preserved_constraints=["must preserve option"],
                reason="unsafe change",
            )
            self.assertEqual(removed.status, "blocked")
            self.assertIn("hard constraints", removed.blocked_reason)
            applied = manager.revise(
                new_plan_state={"steps": [{"step_id": "step-1", "title": "run with option"}], "user_constraints": ["must preserve option"]},
                revision_scope="single_step",
                changed_step_ids=["step-1"],
                preserved_constraints=["must preserve option"],
                reason="safe change",
            )
            self.assertEqual(applied.status, "applied")
            repeated = manager.revise(
                new_plan_state={"steps": [{"step_id": "step-1", "title": "run with option"}], "user_constraints": ["must preserve option"]},
                revision_scope="single_step",
                changed_step_ids=["step-1"],
                preserved_constraints=["must preserve option"],
                reason="safe change",
            )
            self.assertEqual(repeated.status, "blocked")
            self.assertIn("OSCILLATION", repeated.blocked_reason)

    def test_conflict_store_writes_audit_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ClaimRegistry()
            registry.register_user_constraint("must use reference")
            store = KnowledgeConflictStore(tmp, "task")
            store.save_registry(registry)
            self.assertTrue((Path(tmp) / "conflicts" / "claims.jsonl").exists())
            store.save_summary(conflicts=[], resolutions=[], plan_state={"plan_version": "plan_v1"})
            summary = json.loads((Path(tmp) / "conflicts" / "conflict_summary.json").read_text())
            self.assertEqual(summary["active_plan_version"], "plan_v1")




if __name__ == "__main__":
    unittest.main()
