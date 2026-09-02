from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_core.managed_artifacts import (
    MANAGED_EXACT,
    ManagedArtifactBinding,
    managed_action_fingerprint,
    materialize_managed_action,
)
from agent_core.action_runtime import build_exact_action_environment
from bio_agents import BioAnalystAgent
from prompts.manager import PromptManager


class ManagedActionEnvV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fasta_path = str(Path(self.temp.name) / "reference with spaces.fa")
        self.binding = ManagedArtifactBinding(
            artifact_id="reference-fasta",
            semantic_handle="input.reference_fasta",
            direction="input",
            format="fasta",
            path_policy=MANAGED_EXACT,
            canonical_path=self.fasta_path,
            required_basename="reference with spaces.fa",
        )

    def test_embedded_placeholder_remains_rejected_with_minimal_repair_hint(self) -> None:
        _, violations = materialize_managed_action(
            {
                "type": "execute",
                "argv": ["bash", "-lc", 'tool "{input:reference_fasta}"'],
                "env": {},
            },
            {"input.reference_fasta": self.binding},
        )

        payload = violations[0].to_dict()
        self.assertEqual(payload["code"], "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED")
        self.assertEqual(payload["field_path"], "argv[2]")
        self.assertEqual(payload["offending_placeholders"], ["{input:reference_fasta}"])
        self.assertEqual(
            payload["repair_hint"]["strategy"],
            "move_embedded_managed_placeholders_to_env",
        )

    def test_complete_env_placeholder_materializes_path_with_spaces(self) -> None:
        materialized, violations = materialize_managed_action(
            {
                "type": "execute",
                "argv": ["bash", "-lc", 'tool "$FASTA"'],
                "env": {"FASTA": "{input:reference_fasta}"},
            },
            {"input.reference_fasta": self.binding},
        )

        self.assertEqual(violations, [])
        self.assertEqual(materialized["env"]["FASTA"], self.fasta_path)

    def test_runtime_placeholder_is_rejected_in_argv_and_env(self) -> None:
        for action in (
            {
                "type": "execute",
                "argv": ["bash", "-lc", 'java -jar "{runtime:snpeff_jar}"'],
                "env": {},
            },
            {
                "type": "execute",
                "argv": ["bash", "-lc", 'java -jar "$SNPEFF_JAR"'],
                "env": {"SNPEFF_JAR": "{runtime:snpeff_jar}"},
            },
        ):
            with self.subTest(action=action):
                _, violations = materialize_managed_action(action, {})
                self.assertEqual(
                    violations[0].code,
                    "MANAGED_RUNTIME_PLACEHOLDER_UNSUPPORTED",
                )

    def test_validated_action_env_reaches_real_subprocess(self) -> None:
        effective_env = build_exact_action_environment(
            {"PATH": "/usr/bin:/bin"},
            action_environment={"FASTA": self.fasta_path},
        )

        completed = subprocess.run(
            ["bash", "-lc", 'printf "%s\\n" "$FASTA"'],
            check=True,
            capture_output=True,
            text=True,
            env=effective_env,
        )
        self.assertEqual(completed.stdout.splitlines()[-1], self.fasta_path)

    def test_exact_environment_has_no_controller_runtime_injection(self) -> None:
        effective_env = build_exact_action_environment(
            {
                "PATH": "/usr/bin:/bin",
                "SNPEFF_JAR": "/ambient/value.jar",
            },
            action_environment={"SNPEFF_JAR": "/action/value.jar"},
        )

        self.assertEqual(effective_env["SNPEFF_JAR"], "/action/value.jar")
        self.assertNotIn("/verified/runtime", str(effective_env))

    def test_env_change_changes_existing_candidate_fingerprint(self) -> None:
        first = {
            "type": "execute",
            "argv": ["bash", "-lc", 'tool "$FASTA"'],
            "env": {"FASTA": "/first/reference.fa"},
        }
        second = {**first, "env": {"FASTA": "/second/reference.fa"}}
        self.assertNotEqual(
            managed_action_fingerprint(first),
            managed_action_fingerprint(second),
        )

    def test_generate_prompt_requires_concrete_paths_and_no_artifact_env(self) -> None:
        prompt = PromptManager.get_generate_v2_prompt({"task": {}, "current_step": {}})
        self.assertIn('"/data/reference.fasta"', prompt)
        self.assertIn("Never emit an artifact placeholder", prompt)
        self.assertIn("Do not emit `{runtime:*}`", prompt)
        self.assertIn(
            "Use only the concrete runtime resources supplied for the current action.",
            prompt,
        )
        self.assertNotIn("For snpEff", prompt)
        self.assertNotIn("SNPEFF_JAR", prompt)
        self.assertNotIn('"produced_outputs": [', prompt)
        self.assertNotIn('"expected_evidence": [', prompt)
        self.assertIn("Controller validates input/output coverage", prompt)
        self.assertNotIn('"outputs":["output.semantic_handle"]', prompt)

    def test_execute_parser_discards_legacy_duplicate_output_identity(self) -> None:
        action = BioAnalystAgent._structured_action_from_object({
            "action_type": "execute",
            "argv": ["tool", "{output:reference_fasta}"],
            "produced_outputs": [{
                "output_id": "output.reference_fasta",
                "path": "/model-invented/reference.fa",
            }],
            "expected_evidence": ["artifact.output.reference_fasta"],
        })

        self.assertEqual(action["type"], "execute")
        self.assertEqual(action["argv"], ["tool", "{output:reference_fasta}"])
        self.assertNotIn("produced_outputs", action)
        self.assertNotIn("expected_evidence", action)


if __name__ == "__main__":
    unittest.main()
