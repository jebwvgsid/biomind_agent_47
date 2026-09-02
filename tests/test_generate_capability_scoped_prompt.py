from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from agent_core.action_runtime import (
    ActionRuntimeContractError,
    build_runtime_binding_registry,
    capability_contract_hash,
    project_action_capability_context_for_provider,
    project_action_runtime_for_provider,
    resolve_action_runtime_bindings,
    serialize_action_capability_instructions,
)
from agent_core.skill_manager import SkillManager
from prompts.manager import PromptManager


class GenerateCapabilityScopedPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manager = SkillManager(str(Path(__file__).parents[1] / "skills"))
        cls.private_java = "/private/controller/runtime/java"
        cls.private_jar = "/private/controller/runtime/snpEff.jar"
        cls.snpeff_registry = cls.manager.get_execution_capability_registry([
            "genomics/snpeff-annotation"
        ])
        cls.snpeff_runtime_registry = build_runtime_binding_registry({
            "runtime_audit": [{
                "runtime_audit_id": "audit-snpeff-private",
                "skill_id": "genomics/snpeff-annotation",
                "status": "ready",
                "resolved_runtime_paths": {
                    "RESOLVED_JAVA_BIN": cls.private_java,
                    "RESOLVED_SNPEFF_JAR": cls.private_jar,
                },
            }]
        })

    @staticmethod
    def _global_prompt(protocol: str, *, corrective: bool = False) -> str:
        feedback = (
            {
                "action_repair": {
                    "repair_mode": "corrective_script",
                    "repair_stage": "post_execution_recovery",
                    "previous_script": "tool --check-input",
                    "repair_instruction": "Preserve the valid check.",
                }
            }
            if corrective else {}
        )
        return PromptManager.get_generate_v2_prompt({
            "semantic_history": {
                "task_spec": {"goal": "perform one unrelated analysis step"},
            },
            "current_focus": {
                "current_step": {"step_id": "step-1", "title": "Run step"},
                "runtime": {"generate_output_contract": protocol},
                "feedback": feedback,
            },
        })

    @staticmethod
    def _prompt_from_resolution(
        resolution: dict,
        *,
        goal: str,
        protocol: str = "script_block_v1",
        artifact_environment: list[dict] | None = None,
    ) -> str:
        capability_context = project_action_capability_context_for_provider(
            resolution,
            artifact_environment or [],
        )
        return PromptManager.get_generate_v2_prompt(
            {
                "semantic_history": {"task_spec": {"goal": goal}},
                "current_focus": {
                    "current_step": {"step_id": "step-1", "title": goal},
                    "runtime": {
                        "generate_output_contract": protocol,
                        **capability_context,
                    },
                },
            },
            capability_instructions=serialize_action_capability_instructions(
                capability_context,
                script_protocol=(
                    "Use only current-action runtime and artifact shell references."
                ),
            ),
        )

    def _resolve_snpeff(self, capability_id: str) -> dict:
        contract = self.snpeff_registry[capability_id]
        return resolve_action_runtime_bindings(
            selected_skill_id="genomics/snpeff-annotation",
            execution_capability_id=capability_id,
            approved_capability_contract_hash=capability_contract_hash(contract),
            capability_registry=self.snpeff_registry,
            ready_runtime_binding_registry=self.snpeff_runtime_registry,
            current_action_identity={"plan_id": "plan-1", "step_id": "step-1"},
        )

    def test_global_generate_rules_are_tool_agnostic_for_both_protocols(self) -> None:
        required_rules = (
            "Use only the concrete runtime resources supplied for the current action.",
            "Emit required executable, environment, database and tool paths explicitly",
            "Use only the concrete artifact paths supplied for the current action.",
            "Do not request or use bindings outside the current action capability.",
        )
        for protocol in ("semantic_action_v2", "script_block_v1"):
            with self.subTest(protocol=protocol):
                prompt = self._global_prompt(protocol)
                for rule in required_rules:
                    self.assertIn(rule, prompt)
                self.assertNotIn("For snpEff", prompt)
                self.assertNotIn("SNPEFF_JAR", prompt)
                self.assertNotRegex(
                    prompt,
                    r"\b(?:snpEff|Java|GATK|BWA|samtools|bcftools)\b",
                )

    def test_corrective_generate_uses_the_same_tool_agnostic_rules(self) -> None:
        prompt = self._global_prompt("script_block_v1", corrective=True)

        self.assertIn("This is Corrective Generate", prompt)
        self.assertIn(
            "Use only the concrete runtime resources supplied for the current action.",
            prompt,
        )
        self.assertNotIn("SNPEFF_JAR", prompt)
        self.assertNotIn("For snpEff", prompt)

    def test_snpeff_build_gets_only_current_capability_runtime_guidance(self) -> None:
        capability_id = "genomics.snpeff_annotation.build_database"
        resolution = self._resolve_snpeff(capability_id)
        prompt = self._prompt_from_resolution(
            resolution,
            goal="build the approved custom annotation database",
        )
        global_prompt = self._global_prompt("script_block_v1")

        self.assertIn(capability_id, prompt)
        self.assertIn(self.private_java, prompt)
        self.assertIn(self.private_jar, prompt)
        self.assertIn("Invoke snpEff using the supplied concrete Java and JAR paths", prompt)
        self.assertNotIn("$JAVA_BIN", global_prompt)
        self.assertNotIn("$SNPEFF_JAR", global_prompt)

    def test_contig_check_has_no_annotation_runtime_reference(self) -> None:
        capability_id = (
            "genomics.snpeff_annotation.check_contig_compatibility"
        )
        prompt = self._prompt_from_resolution(
            self._resolve_snpeff(capability_id),
            goal="compare current input contig identifiers",
        )

        self.assertIn(capability_id, prompt)
        self.assertIn("this capability has no runtime resources", prompt)
        self.assertNotIn("JAVA_BIN", prompt)
        self.assertNotIn("SNPEFF_JAR", prompt)
        self.assertNotIn("SNPEFF_BIN", prompt)
        self.assertNotIn("Invoke snpEff", prompt)
        self.assertNotIn("Build only the approved", prompt)
        self.assertNotIn("Annotate the approved VCF", prompt)

    def test_skill_manager_projection_contains_only_selected_capability(self) -> None:
        capability_id = "genomics.snpeff_annotation.build_database"
        projection = project_action_runtime_for_provider(
            self._resolve_snpeff(capability_id)
        )
        rendered = json.dumps(projection, sort_keys=True)

        self.assertEqual(
            projection["current_action_capability"]["capability_id"],
            capability_id,
        )
        self.assertEqual(
            {
                (row["binding_id"], row["value"])
                for row in projection["current_action_runtime_bindings"]
            },
            {
                ("runtime.java.bin", self.private_java),
                ("runtime.snpeff.jar", self.private_jar),
            },
        )
        self.assertNotIn("check_contig_compatibility", rendered)
        self.assertNotIn("annotate_variants", rendered)
        self.assertIn(self.private_java, rendered)
        self.assertIn(self.private_jar, rendered)
        self.assertNotIn("capability_contract_hash", rendered)
        self.assertNotIn("contract_hash", rendered)
        self.assertNotRegex(rendered, r"sha256:[0-9a-f]{64}")

    def test_internal_binding_and_llm_capability_view_are_independent(self) -> None:
        capability_id = "genomics.snpeff_annotation.build_database"
        resolution = self._resolve_snpeff(capability_id)
        internal_before = json.loads(json.dumps(resolution, sort_keys=True))

        projection = project_action_capability_context_for_provider(resolution)
        instructions = serialize_action_capability_instructions(
            projection,
            script_protocol="Use current-action references only.",
        )

        self.assertRegex(
            resolution["capability_contract_hash"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(resolution, internal_before)
        self.assertTrue(projection["current_action_capability"])
        self.assertTrue(instructions)
        for prohibited in (
            "capability_contract_hash",
            "contract_hash",
            "binding_hash",
            "registry_hash",
            "registry_digest",
            "contract_digest",
            "revision_hash",
            "signature",
            "nonce",
            "authorization_token",
        ):
            self.assertNotIn(prohibited, instructions)
        self.assertNotRegex(instructions, r"sha256:[0-9a-f]{64}")

        projection["current_action_capability"]["execution_guidance"].append(
            "LLM-only mutation"
        )
        self.assertNotIn(
            "LLM-only mutation",
            resolution["capability_contract"]["execution_guidance"],
        )

    def test_config_and_data_directory_remain_artifact_bindings(self) -> None:
        artifact_environment = [
            {
                "semantic_handle": "plan.snpeff_config",
                "env_var": "BIOMIND_PLAN_SNPEFF_CONFIG",
                "placeholder": "/private/task/snpEff.config",
                "direction": "input",
            },
            {
                "semantic_handle": "plan.snpeff_data_dir",
                "env_var": "BIOMIND_PLAN_SNPEFF_DATA_DIR",
                "placeholder": "/private/task/data",
                "direction": "input",
            },
        ]
        resolution = self._resolve_snpeff(
            "genomics.snpeff_annotation.build_database"
        )
        prompt = self._prompt_from_resolution(
            resolution,
            goal="build the approved custom annotation database",
            artifact_environment=artifact_environment,
        )
        runtime_ids = {
            row["binding_id"]
            for row in project_action_runtime_for_provider(resolution)[
                "current_action_runtime_bindings"
            ]
        }

        self.assertIn("artifact.snpeff_config", prompt)
        self.assertIn("artifact.snpeff_data_dir", prompt)
        self.assertNotIn("runtime.snpeff.config", runtime_ids)
        self.assertNotIn("runtime.snpeff.data_dir", runtime_ids)
        self.assertNotIn("/private/task", prompt)

    def test_invalid_capability_fails_without_skill_wide_guidance_fallback(self) -> None:
        with self.assertRaises(ActionRuntimeContractError) as raised:
            resolve_action_runtime_bindings(
                selected_skill_id="genomics/snpeff-annotation",
                execution_capability_id="genomics.snpeff_annotation.unknown",
                approved_capability_contract_hash="",
                capability_registry=self.snpeff_registry,
                ready_runtime_binding_registry=self.snpeff_runtime_registry,
                current_action_identity={},
            )

        self.assertEqual(raised.exception.code, "UNKNOWN_EXECUTION_CAPABILITY")
        self.assertNotIn(
            "Resolve and verify the pinned snpEff JAR",
            self._global_prompt("script_block_v1"),
        )

    def test_wgs_only_prompt_has_no_annotation_guidance_leakage(self) -> None:
        registry = self.manager.get_execution_capability_registry([
            "genomics/wgs-variant-calling"
        ])
        capability_id = next(iter(registry))
        resolution = resolve_action_runtime_bindings(
            selected_skill_id="genomics/wgs-variant-calling",
            execution_capability_id=capability_id,
            approved_capability_contract_hash=capability_contract_hash(
                registry[capability_id]
            ),
            capability_registry=registry,
            ready_runtime_binding_registry={},
            current_action_identity={"plan_id": "wgs-plan", "step_id": "call"},
        )
        prompt = self._prompt_from_resolution(
            resolution,
            goal="process WGS reads and call variants only",
        )

        self.assertNotIn("snpEff", prompt)
        self.assertNotIn("SNPEFF_JAR", prompt)
        self.assertNotRegex(prompt, r"\bANN\b")
        self.assertNotIn("custom snpEff database", prompt)


if __name__ == "__main__":
    unittest.main()
