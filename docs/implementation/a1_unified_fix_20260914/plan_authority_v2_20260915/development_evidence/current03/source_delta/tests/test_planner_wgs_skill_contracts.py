"""P06: declared WGS effects and diagnostic fidelity at production leaf seams.

Only the two real Skill documents are loaded, from a temporary catalog. These
tests do not run a provider, a scientific command, or an end-to-end graph.
"""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest

from agent_core.action_runtime import (
    capability_contract_hash,
    normalize_capability_contract,
    project_action_runtime_for_provider,
    stable_digest,
)
from agent_core.skill_manager import SkillManager
from agent_core.step_effects import compile_step_effect, match_step_effect_evidence


SKILLS = (
    "genomics/wgs-short-read-mapping",
    "genomics/wgs-gatk-snp-indel",
)
MAPPING = "genomics.wgs.short.read.mapping"
GATK = "genomics.wgs.gatk.snp.indel"


class PlannerWGSSkillContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wgs-skill-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = Path(__file__).resolve().parents[1]
        for skill in SKILLS:
            destination = self.root / "skills" / skill / "SKILL.md"
            destination.parent.mkdir(parents=True)
            shutil.copyfile(source / "skills" / skill / "SKILL.md", destination)
        self.manager = SkillManager(str(self.root / "skills"))
        self.registry = self.manager.get_execution_capability_registry(SKILLS)

    def _step(self, capability, **updates):
        contract = self.registry.get(capability, {})
        return {
            "step_id": "step.offline",
            "action": "execute",
            "execution_capability_id": capability,
            "operation": contract.get("operation", ""),
            "skill_ids": [contract["selected_skill_id"]] if contract else [],
            "inputs": ["input.approved_reference"],
            "outputs": [],
            **updates,
        }

    def test_formal_declarations_reach_registry_semantic_and_runtime_views(self):
        self.assertEqual(set(self.registry), {
            MAPPING + ".execute", MAPPING + ".inspect", MAPPING + ".prepare_reference",
            GATK + ".execute", GATK + ".inspect",
        })
        semantic, _ = self.manager.get_semantic_execution_capabilities(list(SKILLS))
        projected = {
            item["capability_id"]: item
            for skill in semantic for item in skill["execution_capabilities"]
        }
        for capability_id, contract in self.registry.items():
            self.assertIn("SKILL.md#generate.execution_capabilities", contract["contract_source"])
            self.assertTrue(contract["contract_revision"].endswith("-effects-v1"))
            self.assertEqual(projected[capability_id], contract)
            self.assertEqual(contract["workspace_mutations"], [])
            provider = project_action_runtime_for_provider({"capability_contract": contract})
            self.assertEqual(provider["current_action_capability"]["contract_source"], contract["contract_source"])
            self.assertEqual(provider["current_action_capability"]["contract_revision"], contract["contract_revision"])
        for skill in SKILLS:
            self.assertEqual(self.manager.get_skill_meta(skill)["version"], "1.1.0")

    def test_legacy_contract_hash_is_unchanged_when_revision_was_not_declared(self):
        raw = {"capability_id": "example.execute", "selected_skill_id": "example"}
        legacy = {
            "schema_version": 1, **raw,
            "required_runtime_binding_ids": [], "optional_runtime_binding_ids": [],
            "required_artifact_binding_ids": [], "execution_guidance": [],
        }
        self.assertEqual(normalize_capability_contract(raw), legacy)
        self.assertEqual(capability_contract_hash(raw), stable_digest(legacy))
        versioned = {**raw, "contract_source": "declared", "contract_revision": "1"}
        self.assertNotEqual(capability_contract_hash(raw), capability_contract_hash(versioned))
        self.assertNotEqual(capability_contract_hash(versioned), capability_contract_hash({
            **versioned, "contract_revision": "2",
        }))

    def test_produce_files_binds_only_step_outputs_not_semantic_effect_placeholder(self):
        for capability in (MAPPING + ".execute", GATK + ".execute"):
            step = self._step(capability, outputs=["plan.sample_alignment", "plan.alignment_index"])
            result = compile_step_effect(step, self.registry)
            self.assertEqual(result["issues"], [])
            self.assertEqual(set(result["effect"]["produces_artifacts"]), set(step["outputs"]))
            self.assertNotIn("generated_artifact", result["effect"]["produces_artifacts"])
            self.assertTrue(any(
                row["source"] == self.registry[capability]["contract_source"]
                for row in result["receipt"]["normalizations"]
            ) or not result["receipt"]["normalizations"])
            missing = compile_step_effect(self._step(capability), self.registry)
            self.assertIn("PLAN_STEP_ARTIFACT_EFFECT_UNBOUND", [x["code"] for x in missing["issues"]])

    def test_inspect_requires_actual_typed_value_without_fabricating_success(self):
        for capability in (MAPPING + ".inspect", GATK + ".inspect"):
            result = compile_step_effect(self._step(capability), self.registry)
            self.assertEqual(result["issues"], [])
            self.assertEqual(result["effect"]["produces_artifacts"], [])
            self.assertFalse(match_step_effect_evidence(result["effect"], {
                "produces_observations": [{"observation_id": "validation_observation"}],
            })["complete"])
            observed = {"produces_observations": [{
                "observation_id": "validation_observation", "value": False,
            }]}
            matched = match_step_effect_evidence(result["effect"], observed)
            self.assertTrue(matched["complete"])
            # The structural evidence matcher must retain the adverse fact;
            # scientific validation still owns interpretation of success.
            self.assertFalse(matched["matched_effects"]["produces_observations"][0]["value"])

    def test_supported_reference_effect_retains_evidence_requirements(self):
        result = compile_step_effect(self._step(
            MAPPING + ".prepare_reference", outputs=["plan.reference_index_bundle"],
        ), self.registry)
        self.assertEqual(result["issues"], [])
        declared = result["effect"]["explicit_side_effects"][0]
        self.assertEqual(declared["identity"], "create_reference_indexes")
        self.assertEqual(declared["evidence_contract"]["required_fields"], [
            "reference_resource_ref", "index_artifacts",
        ])
        incomplete = {
            "produces_artifacts": ["plan.reference_index_bundle"],
            "explicit_side_effects": [{"effect_id": "create_reference_indexes"}],
        }
        self.assertFalse(match_step_effect_evidence(result["effect"], incomplete)["complete"])
        complete = deepcopy(incomplete)
        complete["explicit_side_effects"][0]["evidence"] = {
            "reference_resource_ref": "input.approved_reference",
            "index_artifacts": ["plan.reference_index_bundle"],
        }
        self.assertTrue(match_step_effect_evidence(result["effect"], complete)["complete"])

    def test_inline_typed_side_effect_remains_legal_without_a_static_event_name(self):
        legacy = normalize_capability_contract({
            "capability_id": "generic.execute", "selected_skill_id": "generic",
        })
        step = self._step("generic.execute", skill_ids=["generic"], explicit_side_effects=[{
            "effect_id": "an_explicit_scoped_effect",
            "evidence_contract": {"required_fields": ["receipt"]},
        }])
        result = compile_step_effect(step, {"generic.execute": legacy})
        self.assertEqual(result["issues"], [])
        self.assertFalse(match_step_effect_evidence(result["effect"], {
            "explicit_side_effects": ["an_explicit_scoped_effect"],
        })["complete"])

    def test_unsupported_side_effect_keeps_subcode_objects_and_contract_requirement(self):
        step = self._step("", explicit_side_effects=["undeclared_effect"])
        result = compile_step_effect(step, {}, step_index=4)
        issue = next(x for x in result["issues"] if x.get("violation_code") == "EXPLICIT_SIDE_EFFECT_UNSUPPORTED")
        self.assertEqual(issue["code"], "STEP_EFFECT_PROTOCOL_INVALID")
        self.assertEqual(issue["step_id"], step["step_id"])
        self.assertEqual(issue["field_path"], "$.steps[4].explicit_side_effects")
        self.assertEqual(issue["unsupported_effect_ids"], ["undeclared_effect"])
        self.assertEqual(issue["actual"], ["undeclared_effect"])
        self.assertIn("supported_forms", issue["expected"])
        self.assertEqual(issue["contract_source"], "agent_core.step_effects.compile_step_effect")
        self.assertEqual(issue["contract_revision"], "step-effect-schema-1")

    def test_closed_capability_rejects_workspace_mutation_with_source_and_revision(self):
        capability = MAPPING + ".execute"
        result = compile_step_effect(self._step(
            capability, outputs=["plan.alignment"],
            workspace_mutations=[{"workspace_ref": "output.container", "mutation": "append"}],
        ), self.registry)
        issue = next(x for x in result["issues"] if x.get("violation_code") == "STEP_EFFECT_CAPABILITY_MISMATCH")
        self.assertEqual(issue["unsupported_effects"], {"workspace_mutations": ["output.container"]})
        self.assertEqual(issue["expected"], {"workspace_mutations": []})
        self.assertEqual(issue["contract_source"], self.registry[capability]["contract_source"])
        self.assertEqual(issue["contract_revision"], self.registry[capability]["contract_revision"])


if __name__ == "__main__":
    unittest.main()
