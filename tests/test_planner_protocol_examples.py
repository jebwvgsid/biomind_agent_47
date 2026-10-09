"""Compile the actual Planner prompt examples and selected WGS contracts offline."""
from copy import deepcopy
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from agent_core.action_runtime import generic_execution_capability
from agent_core.plan_bindings import compile_plan_resource_bindings
from agent_core.plan_contract import (
    candidate_artifact_ref_keys,
    parse_planner_outcome_envelope,
    PlannerCandidateError,
    structured_plan_candidate_diagnostics,
)
from agent_core.skill_manager import SkillManager
from agent_core.step_effects import compile_plan_dataflow, compile_plan_step_effects
from prompts.manager import PromptManager


SKILLS = (
    "genomics/wgs-short-read-mapping",
    "genomics/wgs-gatk-snp-indel",
)


def prompt_example(tag):
    prompt = PromptManager.get_planner_v2_system_prompt()
    match = re.search(rf'<{tag} schema_version="1">\s*(.*?)\s*</{tag}>', prompt, re.S)
    if match is None:
        raise AssertionError(f"Missing complete Planner example: {tag}")
    return parse_planner_outcome_envelope(match.group(1), allow_legacy_plan=False).plan_candidate_body()


class PlannerProtocolExampleTests(unittest.TestCase):
    def compile_example(self, candidate, registry, *, existing_output=False):
        self.assertEqual(structured_plan_candidate_diagnostics(candidate), [])
        handles = {"output.final_record": "artifact-final-record"} if existing_output else {}
        availability = {"artifact-final-record": "not_yet_available"} if existing_output else {}
        arguments = {
            "candidate_scope_id": "protocol-example-scope",
            "authoritative_handles": handles,
            "availability_origins": availability,
        }
        keys, _usage = candidate_artifact_ref_keys(
            candidate, candidate_scope_id=arguments["candidate_scope_id"],
            authoritative_handles=handles,
        )
        bindings = compile_plan_resource_bindings(
            candidate, **arguments, capability_registry=registry,
            require_declared_references=True,
        )
        compiled, receipt, issues = compile_plan_step_effects(
            candidate, registry, resource_bindings=bindings, allow_legacy_untyped=False,
        )
        self.assertEqual(issues, [])
        self.assertTrue(receipt["effect_complete"])
        _compiled_flow, _receipt, flow_issues = compile_plan_dataflow(
            compiled, registry, resource_bindings=bindings,
            compile_effect_contracts=False, allow_legacy_untyped=False,
        )
        self.assertEqual(flow_issues, [])
        return compiled, keys

    def test_generic_observation_example_still_passes_real_compilers(self):
        capability = generic_execution_capability()
        candidate = prompt_example("generic_plan_example")
        compiled, _keys = self.compile_example(candidate, {capability["capability_id"]: capability})
        check = compiled["steps"][1]
        self.assertEqual(check["outputs"], [])
        self.assertTrue(check["step_effect"]["produces_observations"])

    def test_mixed_example_reuses_authoritative_output_and_builtin_question(self):
        capability = generic_execution_capability()
        candidate = prompt_example("existing_output_plan_example")
        original = deepcopy(candidate)
        compiled, keys = self.compile_example(
            candidate, {capability["capability_id"]: capability}, existing_output=True,
        )
        self.assertEqual(candidate, original)
        self.assertEqual(keys["output.final_record"].kind, "authoritative")
        self.assertEqual(keys["plan.record_draft"].kind, "candidate_local")
        question = compiled["steps"][0]
        self.assertEqual(question["step_effect"]["explicit_side_effects"], ["user_input_requested"])
        self.assertEqual(question["outputs"], [])
        self.assertFalse(question.get("execution_capability_id"))
        self.assertFalse(question.get("operation"))
        self.assertEqual(compiled["steps"][1]["dependencies"], [question["step_id"]])
        self.assertEqual(compiled["steps"][2]["outputs"], ["output.final_record"])

    def test_shadowing_the_examples_existing_output_still_fails_closed(self):
        candidate = prompt_example("existing_output_plan_example")
        shadow = deepcopy(candidate["proposed_artifacts"][0])
        shadow["local_ref"] = "plan.final_record"
        shadow["semantic_view"]["role"] = "final_record"
        candidate["proposed_artifacts"].append(shadow)
        with self.assertRaises(PlannerCandidateError) as caught:
            candidate_artifact_ref_keys(
                candidate, candidate_scope_id="protocol-example-scope",
                authoritative_handles={"output.final_record": "artifact-final-record"},
                authoritative_bindings={"output.final_record": {"direction": "output"}},
            )
        self.assertEqual(caught.exception.code, "TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL")

    def test_selected_wgs_file_capabilities_support_existing_and_new_outputs(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="planner-protocol-skills-") as temporary:
            root = Path(temporary) / "skills"
            for skill in SKILLS:
                target = root / skill / "SKILL.md"
                target.parent.mkdir(parents=True)
                shutil.copyfile(source / "skills" / skill / "SKILL.md", target)
            manager = SkillManager(str(root))
            registry = manager.get_execution_capability_registry(SKILLS)
            for skill in SKILLS:
                with self.subTest(skill=skill):
                    capability = next(value for value in registry.values()
                        if value.get("selected_skill_id") == skill and value.get("operation") == "produce_files")
                    candidate = prompt_example("existing_output_plan_example")
                    for step in candidate["steps"]:
                        if step["action"] == "execute":
                            step.update(execution_capability_id=capability["capability_id"],
                                operation=capability["operation"], tool=skill, skill_ids=[skill])
                    compiled, _keys = self.compile_example(candidate, registry, existing_output=True)
                    self.assertEqual(compiled["steps"][1]["step_effect"]["produces_artifacts"], ["plan.record_draft"])
                    self.assertEqual(compiled["steps"][2]["step_effect"]["produces_artifacts"], ["output.final_record"])


if __name__ == "__main__":
    unittest.main()
