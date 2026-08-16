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
from bio_agents import _inject_managed_action_environment
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

    def test_materialized_env_reaches_real_subprocess(self) -> None:
        code, violation = _inject_managed_action_environment(
            'printf "%s\\n" "$FASTA"',
            "Bash",
            {"SNPEFF_JAR": "/verified/runtime/snpEff.jar"},
            {"FASTA": self.fasta_path},
        )
        self.assertEqual(violation, {})

        completed = subprocess.run(
            ["bash", "-lc", code],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.splitlines()[-1], self.fasta_path)

    def test_action_env_cannot_override_controller_runtime(self) -> None:
        code, violation = _inject_managed_action_environment(
            'printf "%s\\n" "$SNPEFF_JAR"',
            "Bash",
            {"SNPEFF_JAR": "/verified/runtime/snpEff.jar"},
            {"SNPEFF_JAR": "/model/value.jar"},
        )

        self.assertEqual(code, 'printf "%s\\n" "$SNPEFF_JAR"')
        self.assertEqual(violation["code"], "MANAGED_ACTION_ENV_KEY_CONFLICT")
        self.assertEqual(violation["env_keys"], ["SNPEFF_JAR"])
        self.assertNotIn("/model/value.jar", str(violation))

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

    def test_generate_prompt_contains_one_env_repair_example_and_runtime_rule(self) -> None:
        prompt = PromptManager.get_generate_v2_prompt({"task": {}, "current_step": {}})
        self.assertIn('FASTA="{input:reference_fasta}"; tool "$FASTA"', prompt)
        self.assertIn('"env":{"FASTA":"{input:reference_fasta}"}', prompt)
        self.assertIn("Do not emit `{runtime:*}`", prompt)
        self.assertIn("$SNPEFF_JAR", prompt)


if __name__ == "__main__":
    unittest.main()
