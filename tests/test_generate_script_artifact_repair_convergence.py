from __future__ import annotations

import json
import unittest

from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
    project_script_artifact_environment_for_provider,
    project_script_capabilities_for_provider,
    script_env_reference,
)
from agent_core.managed_artifacts import (
    ManagedArtifactBinding,
    materialize_managed_action,
)


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

    def test_literal_fallback_is_diagnostic_only_when_binding_materializes(self) -> None:
        action = self._adapt(
            'VCF="${BIOMIND_INPUT_VARIANT_VCF:-all.filtered.snp.vcf}"\n'
            'tool "$VCF"'
        )
        materialized, violations = materialize_managed_action(
            action,
            self.bindings,
        )
        self.assertEqual(violations, [])
        self.assertEqual(
            materialized["env"]["BIOMIND_INPUT_VARIANT_VCF"],
            "/data/all.filtered.snp.vcf",
        )
        diagnostics = materialized.get("source_diagnostics") or []
        self.assertTrue(diagnostics)
        self.assertTrue(all(item["authoritative"] is False for item in diagnostics))
        self.assertTrue(all(item["effect"] == "diagnostic_only" for item in diagnostics))

    def test_raw_placeholder_is_diagnostic_only_when_typed_env_is_valid(self) -> None:
        action = self._adapt(
            'VCF="${BIOMIND_INPUT_VARIANT_VCF:-{input:variant_vcf}}"\n'
            'tool "$VCF"'
        )
        materialized, violations = materialize_managed_action(
            action,
            self.bindings,
        )
        self.assertEqual(violations, [])
        self.assertEqual(
            materialized["env"]["BIOMIND_INPUT_VARIANT_VCF"],
            "/data/all.filtered.snp.vcf",
        )
        self.assertIn(
            "MANAGED_PLACEHOLDER_IN_FREE_SOURCE",
            {item["code"] for item in materialized["source_diagnostics"]},
        )

    def test_raw_source_occurrences_never_become_hard_violations(self) -> None:
        cases = (
            '# source was all.filtered.snp.vcf\ntool "$BIOMIND_INPUT_VARIANT_VCF"',
            'echo "Using provided all.filtered.snp.vcf"\ntool "$BIOMIND_INPUT_VARIANT_VCF"',
            "printf 'Reference all.filtered.snp.vcf validated\\n'\ntool \"$BIOMIND_INPUT_VARIANT_VCF\"",
            'cat <<EOF\nCustom input from all.filtered.snp.vcf\nEOF\ntool "$BIOMIND_INPUT_VARIANT_VCF"',
            'DESCRIPTION="all.filtered.snp.vcf"\ntool "$BIOMIND_INPUT_VARIANT_VCF"',
            'tool all.filtered.snp.vcf\ntool "$BIOMIND_INPUT_VARIANT_VCF"',
        )
        for source in cases:
            with self.subTest(source=source.splitlines()[0]):
                materialized, violations = materialize_managed_action(
                    self._adapt(source),
                    self.bindings,
                )
                self.assertEqual(violations, [])
                self.assertTrue(materialized.get("source_diagnostics"))
                self.assertTrue(all(
                    not item["authoritative"]
                    for item in materialized["source_diagnostics"]
                ))

    def test_session_1787622048_prepare_snpeff_config_candidates_are_execute_eligible(self) -> None:
        bindings = {
            "input.reference_fasta": ManagedArtifactBinding(
                artifact_id="reference_fasta",
                semantic_handle="input.reference_fasta",
                direction="input",
                format="fasta",
                path_policy="managed_exact",
                canonical_path="/data/reference/genome.fasta",
                required_basename="genome.fasta",
            ),
            "input.annotation_gtf": ManagedArtifactBinding(
                artifact_id="annotation_gtf",
                semantic_handle="input.annotation_gtf",
                direction="input",
                format="gtf",
                path_policy="managed_exact",
                canonical_path="/data/reference/genome.gtf",
                required_basename="genome.gtf",
            ),
            "output.snpeff_config": ManagedArtifactBinding(
                artifact_id="plan_artifact.snpeff_config",
                semantic_handle="output.snpeff_config",
                direction="output",
                format="config",
                path_policy="managed_exact",
                canonical_path="/work/prepare_snpeff_config/snpeff.config",
                required_basename="snpeff.config",
            ),
        }
        environment = [
            {
                "semantic_handle": handle,
                "direction": binding.direction,
                "env_var": env_name,
                "placeholder": binding.placeholder,
                "required": True,
            }
            for handle, binding, env_name in (
                (
                    "input.reference_fasta",
                    bindings["input.reference_fasta"],
                    "BIOMIND_INPUT_REFERENCE_FASTA",
                ),
                (
                    "input.annotation_gtf",
                    bindings["input.annotation_gtf"],
                    "BIOMIND_INPUT_ANNOTATION_GTF",
                ),
                (
                    "output.snpeff_config",
                    bindings["output.snpeff_config"],
                    "BIOMIND_OUTPUT_PLAN_ARTIFACT_SNPEFF_CONFIG",
                ),
            )
        ]
        candidates = (
            (
                'FASTA="$BIOMIND_INPUT_REFERENCE_FASTA"\n'
                'GTF="$BIOMIND_INPUT_ANNOTATION_GTF"\n'
                'OUT="$BIOMIND_OUTPUT_PLAN_ARTIFACT_SNPEFF_CONFIG"\n'
                'cat > "$OUT" <<EOF\n'
                'Custom genome from provided genome.fasta and genome.gtf\nEOF'
            ),
            (
                'test -s "$BIOMIND_INPUT_REFERENCE_FASTA"\n'
                'test -s "$BIOMIND_INPUT_ANNOTATION_GTF"\n'
                'ln -sfn "$BIOMIND_INPUT_REFERENCE_FASTA" reference/genome.fasta\n'
                'ln -sfn "$BIOMIND_INPUT_ANNOTATION_GTF" reference/genome.gtf\n'
                'cat > "$BIOMIND_OUTPUT_PLAN_ARTIFACT_SNPEFF_CONFIG" <<EOF\n'
                'Custom genome from provided genome.fasta and genome.gtf\nEOF'
            ),
        )
        for index, source in enumerate(candidates, start=1):
            with self.subTest(candidate=index):
                action = adapt_script_block_to_action(
                    parse_executable_script_block(f"<shell>{source}</shell>"),
                    environment,
                )
                action["controller_artifact_environment"] = list(environment)
                materialized, violations = materialize_managed_action(action, bindings)
                self.assertEqual(violations, [])
                self.assertEqual(set(materialized["managed_artifact_handles"]), set(bindings))
                self.assertTrue(materialized.get("source_diagnostics"))


if __name__ == "__main__":
    unittest.main()
