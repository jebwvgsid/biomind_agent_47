"""Offline checks for Plan actions before and after resource binding."""

from copy import deepcopy
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_core.action_runtime import generic_execution_capability
from agent_core.step_effects import (
    compile_step_effect,
    independent_step_effect_diagnostics,
)


class PlanActionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.registry = {
            "framework.generic.execute": generic_execution_capability(),
        }

    def ask_step(self, **updates):
        return {
            "step_id": "confirm_ploidy",
            "action": "ask_user",
            "tool": "ask_user",
            "resource_uses": [],
            "outputs": [],
            **updates,
        }

    def test_builtin_ask_user_needs_no_execution_capability_or_output(self):
        step = self.ask_step()
        self.assertEqual(independent_step_effect_diagnostics({"steps": [step]}), [])
        compiled = compile_step_effect(step, self.registry)
        self.assertEqual(compiled["issues"], [])
        self.assertTrue(compiled["complete"])
        self.assertEqual(compiled["effect"]["explicit_side_effects"], ["user_input_requested"])

    def test_wrong_executor_has_actionable_diagnostic_without_resource_binding(self):
        step = self.ask_step(
            execution_capability_id="framework.generic.execute", operation="execute",
        )
        candidate = {
            "proposed_artifacts": [{"local_ref": "plan.conflicting_output"}],
            "steps": [{"step_id": "unrelated", "action": "execute"}, step],
        }
        original = deepcopy(candidate)
        with patch("agent_core.plan_bindings.compile_plan_resource_bindings", side_effect=AssertionError(
            "Independent diagnostics must not manufacture or resolve resource bindings"
        )):
            issues = independent_step_effect_diagnostics(candidate, self.registry)
        self.assertEqual(candidate, original)
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["code"], "STEP_EFFECT_PROTOCOL_INVALID")
        self.assertEqual(issue["violation_code"], "PLAN_ACTION_CAPABILITY_MISMATCH")
        self.assertEqual(issue["field_path"], "$.steps[1].execution_capability_id")
        self.assertEqual(issue["step_id"], "confirm_ploidy")
        self.assertEqual(issue["expected"]["operation"], "omit")
        self.assertEqual(issue["actual"]["execution_capability_id"], "framework.generic.execute")
        compiled = compile_step_effect(step, self.registry, step_index=1)
        self.assertEqual(compiled["issues"], issues)
        self.assertEqual(compiled["receipt"]["status"], "protocol_invalid")

    def test_bare_invented_event_does_not_hide_wrong_executor(self):
        step = self.ask_step(
            execution_capability_id="framework.generic.execute", operation="execute",
            explicit_side_effects=["request_user_decision"],
        )
        issues = independent_step_effect_diagnostics({"steps": [step]}, self.registry)
        self.assertEqual([issue["violation_code"] for issue in issues], [
            "PLAN_ACTION_CAPABILITY_MISMATCH", "EXPLICIT_SIDE_EFFECT_UNSUPPORTED",
        ])
        self.assertEqual(issues[1]["actual"], ["request_user_decision"])
        self.assertEqual(compile_step_effect(step, self.registry)["issues"], issues)

    def test_declared_interaction_capability_is_not_blanket_rejected(self):
        capability = {
            "capability_id": "domain.review.ask_user",
            "operation": "ask_user",
            "explicit_side_effects": [{
                "effect_id": "domain_answer",
                "evidence_contract": {"required_fields": ["answer"]},
            }],
        }
        registry = {capability["capability_id"]: capability}
        step = self.ask_step(
            execution_capability_id=capability["capability_id"], operation="ask_user",
        )
        self.assertEqual(independent_step_effect_diagnostics({"steps": [step]}, registry), [])
        self.assertEqual(compile_step_effect(step, registry)["issues"], [])

    def test_unknown_capability_remains_for_authoritative_capability_validation(self):
        step = self.ask_step(
            execution_capability_id="unresolved.interaction", operation="ask_user",
            explicit_side_effects=["domain_specific_answer"],
        )
        self.assertEqual(independent_step_effect_diagnostics({"steps": [step]}, {}), [])

    def test_typed_inline_effect_on_generic_execution_remains_legal(self):
        step = {
            "step_id": "record_result", "action": "execute", "tool": "python",
            "execution_capability_id": "framework.generic.execute", "operation": "execute",
            "outputs": [],
            "explicit_side_effects": [{
                "effect_id": "scoped_record",
                "evidence_contract": {"required_fields": ["receipt"]},
            }],
        }
        self.assertEqual(independent_step_effect_diagnostics({"steps": [step]}, self.registry), [])
        self.assertEqual(compile_step_effect(step, self.registry)["issues"], [])

    def test_domain_effect_support_is_not_invented_without_registry(self):
        step = {
            "step_id": "prepare", "action": "execute",
            "execution_capability_id": "domain.prepare", "operation": "prepare",
            "explicit_side_effects": ["domain_owned_event"],
        }
        self.assertEqual(independent_step_effect_diagnostics({"steps": [step]}), [])

    def test_real_wgs_domain_execution_still_compiles(self):
        from agent_core.skill_manager import SkillManager

        skill_id = "genomics/wgs-short-read-mapping"
        source = Path(__file__).resolve().parents[1] / "skills" / skill_id / "SKILL.md"
        with TemporaryDirectory(prefix="plan-action-contract-") as directory:
            skills = Path(directory) / "skills"
            destination = skills / skill_id / "SKILL.md"
            destination.parent.mkdir(parents=True)
            shutil.copyfile(source, destination)
            registry = SkillManager(str(skills)).get_execution_capability_registry([skill_id])
        capability_id = "genomics.wgs.short.read.mapping.execute"
        step = {
            "step_id": "mapping", "action": "execute",
            "execution_capability_id": capability_id,
            "operation": registry[capability_id]["operation"],
            "skill_ids": [skill_id],
            "outputs": ["plan.alignment", "plan.alignment_index"],
        }
        self.assertEqual(independent_step_effect_diagnostics({"steps": [step]}, registry), [])
        self.assertEqual(compile_step_effect(step, registry)["issues"], [])

    def test_malformed_structure_stays_with_schema_validator(self):
        for candidate in (None, {}, {"steps": "invalid"}, {"steps": [None]}):
            self.assertEqual(independent_step_effect_diagnostics(candidate), [])


if __name__ == "__main__":
    unittest.main()
