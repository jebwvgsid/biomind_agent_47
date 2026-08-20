from __future__ import annotations

import json
import unittest

from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
    project_script_artifact_environment_for_provider,
    project_script_artifact_repair_violations,
    project_script_capabilities_for_provider,
    script_env_reference,
)
from agent_core.managed_artifacts import (
    ManagedArtifactBinding,
    materialize_managed_action,
)
from bio_agents import BioAnalystAgent
from prompts.manager import PromptManager


class GenerateScriptArtifactRepairConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = ManagedArtifactBinding(
            artifact_id="variant_vcf",
            semantic_handle="input.variant_vcf",
            direction="input",
            format="vcf",
            path_policy="managed_exact",
            canonical_path="/data/all.filtered.snp.vcf",
            required_basename="all.filtered.snp.vcf",
        )
        self.bindings = {self.binding.semantic_handle: self.binding}
        self.internal_environment = [{
            "semantic_handle": "input.variant_vcf",
            "direction": "input",
            "env_var": "BIOMIND_INPUT_VARIANT_VCF",
            "placeholder": "{input:variant_vcf}",
            "required": True,
            "format": "vcf",
            "artifact_kind": "file",
        }]

    def _adapt(self, source: str) -> dict:
        action = adapt_script_block_to_action(
            parse_executable_script_block(f"<shell>{source}</shell>"),
            self.internal_environment,
        )
        action["controller_artifact_environment"] = list(
            self.internal_environment
        )
        return action

    def test_provider_environment_hides_controller_binding(self) -> None:
        provider_environment = project_script_artifact_environment_for_provider(
            self.internal_environment
        )

        self.assertEqual(len(provider_environment), 1)
        self.assertEqual(
            provider_environment[0]["env_var"],
            "BIOMIND_INPUT_VARIANT_VCF",
        )
        self.assertNotIn("placeholder", provider_environment[0])
        self.assertNotIn("canonical_path", provider_environment[0])
        provider_capabilities = project_script_capabilities_for_provider([{
            "handle": "capability.C1",
            "operations": [{
                "handle": "operation.C1.O1",
                "placeholder": "{operation:operation.C1.O1}",
            }],
        }])
        self.assertNotIn(
            "{operation:operation.C1.O1}",
            json.dumps(provider_capabilities),
        )
        self.assertEqual(
            script_env_reference("BIOMIND_INPUT_VARIANT_VCF", "bash"),
            "$BIOMIND_INPUT_VARIANT_VCF",
        )
        self.assertEqual(
            script_env_reference("BIOMIND_INPUT_VARIANT_VCF", "python"),
            'os.environ["BIOMIND_INPUT_VARIANT_VCF"]',
        )
        self.assertEqual(
            script_env_reference("BIOMIND_INPUT_VARIANT_VCF", "r"),
            'Sys.getenv("BIOMIND_INPUT_VARIANT_VCF")',
        )

    def test_literal_fallback_projects_one_coherent_corrective_request(self) -> None:
        invalid_action = self._adapt(
            'VCF="${BIOMIND_INPUT_VARIANT_VCF:-all.filtered.snp.vcf}"\n'
            'tool "$VCF"'
        )
        _, violations = materialize_managed_action(
            invalid_action,
            self.bindings,
        )
        violation_payload = [item.to_dict() for item in violations]

        self.assertIn(
            "MANAGED_ARTIFACT_HANDLE_REQUIRED",
            {item["code"] for item in violation_payload},
        )
        literal_violation = next(
            item
            for item in violation_payload
            if item["code"] == "MANAGED_ARTIFACT_HANDLE_REQUIRED"
        )
        self.assertIn("BIOMIND_INPUT_VARIANT_VCF", literal_violation["message"])
        self.assertNotIn("use {input:variant_vcf}", literal_violation["message"])

        projection = BioAnalystAgent._action_repair_projection(
            {
                "recovery_context": {
                    "validation": {"violations": violation_payload},
                    "action_contract": {
                        "structured_candidate": invalid_action,
                    },
                },
            },
            {"type": "action_format_repair"},
            {"kind": "FORMAT_REPAIR"},
        )
        projection_json = json.dumps(projection, ensure_ascii=False)

        self.assertEqual(
            projection["previous_candidate"],
            {
                "protocol": "script_block_v1",
                "language": "bash",
                "script": invalid_action["code"],
            },
        )
        self.assertNotIn("{input:variant_vcf}", projection_json)
        repair_hint = projection["exact_violations"][0]["repair_hint"]
        self.assertEqual(
            repair_hint["provider_reference"],
            "$BIOMIND_INPUT_VARIANT_VCF",
        )
        self.assertIn("remove", repair_hint["rule"].lower())

        prompt = PromptManager.get_generate_v2_prompt({
            "semantic_history": {
                "task_spec": {
                    "goal": "annotate variants",
                    "output_contracts": [{
                        "handle": "output.annotated_vcf",
                        "placeholder": "{output:annotated_vcf}",
                    }],
                },
            },
            "current_focus": {
                "current_step": {
                    "inputs": ["input.variant_vcf"],
                    "outputs": [],
                },
                "runtime": {
                    "generate_output_contract": "script_block_v1",
                    "artifact_environment": (
                        project_script_artifact_environment_for_provider(
                            self.internal_environment
                        )
                    ),
                },
                "feedback": {
                    "recovery_action": {
                        "type": "ACTION_FORMAT_REPAIR",
                        "failure_owner": "GENERATED_ACTION",
                        "reason": "use {input:variant_vcf}",
                        "violation": {
                            "proposed": "{input:variant_vcf}",
                            "expected": "/data/all.filtered.snp.vcf",
                        },
                    },
                    "action_repair": projection,
                },
            },
        })

        self.assertIn("$BIOMIND_INPUT_VARIANT_VCF", prompt)
        self.assertIn("Remove literal", prompt)
        self.assertNotIn("{input:variant_vcf}", prompt)
        self.assertNotIn("{output:annotated_vcf}", prompt)
        self.assertNotIn("Use the required semantic handle placeholder", prompt)

        corrected_action = self._adapt(
            'VCF="$BIOMIND_INPUT_VARIANT_VCF"\ntool "$VCF"'
        )
        materialized, corrected_violations = materialize_managed_action(
            corrected_action,
            self.bindings,
        )
        self.assertEqual(corrected_violations, [])
        self.assertEqual(
            materialized["env"]["BIOMIND_INPUT_VARIANT_VCF"],
            "/data/all.filtered.snp.vcf",
        )
        self.assertEqual(materialized["code"], corrected_action["code"])

    def test_embedded_binding_remains_rejected_but_repairs_to_env_reference(self) -> None:
        invalid_action = self._adapt(
            'VCF="${BIOMIND_INPUT_VARIANT_VCF:-{input:variant_vcf}}"\n'
            'tool "$VCF"'
        )
        _, violations = materialize_managed_action(
            invalid_action,
            self.bindings,
        )
        payload = [item.to_dict() for item in violations]
        codes = {item["code"] for item in payload}

        self.assertIn("MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED", codes)
        self.assertIn("MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL", codes)

        projected = project_script_artifact_repair_violations(
            payload,
            self.internal_environment,
            language="bash",
        )
        projected_json = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn("{input:variant_vcf}", projected_json)
        self.assertTrue(projected)
        self.assertTrue(all(
            item["repair_hint"].get("provider_reference")
            == "$BIOMIND_INPUT_VARIANT_VCF"
            for item in projected
        ))


if __name__ == "__main__":
    unittest.main()
