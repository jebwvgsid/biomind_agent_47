from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.action_runtime import (
    ActionRuntimeContractError,
    build_action_approval_identity,
    build_action_execution_environment,
    build_artifact_env_manifest,
    build_execution_policy_manifest,
    build_runtime_binding_registry,
    capability_contract_hash,
    resolve_action_runtime_bindings,
    validate_action_approval_identity,
)
from agent_core.middleware import (
    HITLMiddleware,
    build_code_review_provenance,
    render_code_review_provenance,
)


class ExecutionProvenanceSeparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        tool = root / "bin" / "tool"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
        self.tool = str(tool)
        self.contract = {
            "capability_id": "demo.execute",
            "selected_skill_id": "demo/skill",
            "required_runtime_binding_ids": ["runtime.tool.bin"],
            "optional_runtime_binding_ids": [],
            "required_artifact_binding_ids": ["artifact.input"],
            "execution_guidance": [],
        }
        self.action_identity = {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "step_id": "step-1",
        }
        self.resolution = resolve_action_runtime_bindings(
            selected_skill_id="demo/skill",
            execution_capability_id="demo.execute",
            approved_capability_contract_hash=capability_contract_hash(
                self.contract
            ),
            capability_registry={"demo.execute": self.contract},
            ready_runtime_binding_registry=build_runtime_binding_registry({
                "runtime_audit": [{
                    "runtime_audit_id": "audit-1",
                    "skill_id": "demo/skill",
                    "status": "ready",
                    "resolved_runtime_paths": {
                        "RESOLVED_TOOL_BIN": self.tool,
                    },
                }]
            }),
            current_action_identity=self.action_identity,
        )
        self.artifacts = [{
            "semantic_handle": "input.reference_fasta",
            "env_var": "BIOMIND_INPUT_REFERENCE_FASTA",
            "placeholder": str(root / "reference.fa"),
            "direction": "input",
            "artifact_kind": "file",
        }]
        self.artifact_manifest = build_artifact_env_manifest(
            self.artifacts,
            action_identity=self.action_identity,
            selected_skill_id="demo/skill",
            execution_capability_id="demo.execute",
            capability_contract_hash_value=capability_contract_hash(
                self.contract
            ),
        )
        self.policy_manifest = build_execution_policy_manifest(
            action_identity=self.action_identity,
            working_directory=self.temp.name,
            language="Bash",
        )

    def test_code_review_has_four_owned_sections_and_no_combined_export_script(self):
        source = '"$TOOL_BIN" "$BIOMIND_INPUT_REFERENCE_FASTA"\n'
        provenance = build_code_review_provenance(
            source,
            generated_script_sha256=hashlib.sha256(source.encode()).hexdigest(),
            artifact_env_manifest=self.artifact_manifest,
            runtime_env_manifest=self.resolution["runtime_binding_manifest"],
            execution_policy_manifest=self.policy_manifest,
        )
        rendered = render_code_review_provenance(provenance)

        self.assertEqual(len(provenance["sections"]), 4)
        for heading in (
            "A. MODEL-GENERATED ACTION",
            "B. MANAGED ARTIFACT BINDINGS",
            "C. CONTROLLER-PROVIDED RUNTIME BINDINGS",
            "D. EXECUTION POLICY",
        ):
            self.assertIn(heading, rendered)
        self.assertNotIn(self.tool, rendered)
        self.assertNotIn(self.artifacts[0]["placeholder"], rendered)
        self.assertNotIn("export TOOL_BIN", provenance["sections"][0]["content"])

    def test_code_review_risk_reasons_are_source_labeled(self):
        source = "mkdir output"
        provenance = build_code_review_provenance(
            source,
            generated_script_sha256=hashlib.sha256(source.encode()).hexdigest(),
            artifact_env_manifest=self.artifact_manifest,
            runtime_env_manifest=self.resolution["runtime_binding_manifest"],
            execution_policy_manifest=self.policy_manifest,
        )
        middleware = HITLMiddleware(auto_approve_safe=False)
        captured = {}

        def approve(payload):
            captured.update(payload)
            return "y"

        with patch("agent_core.middleware.interrupt", side_effect=approve):
            action, code = middleware.review_code(
                source,
                "Bash",
                provenance=provenance,
            )

        self.assertEqual((action, code), ("approve", source))
        self.assertIn("MODEL_SCRIPT:", str(captured["risk"]["risky_operations"]))
        self.assertEqual(
            captured["provenance"]["sections"][2]["risk_source"],
            "RUNTIME_ENV",
        )
        self.assertNotIn(self.tool, captured["content"])

    def test_approval_identity_detects_runtime_artifact_policy_and_cwd_drift(self):
        generated_hash = hashlib.sha256(b"echo ok\n").hexdigest()
        approved = build_action_approval_identity(
            generated_script_sha256=generated_hash,
            capability_contract_hash_value=capability_contract_hash(self.contract),
            runtime_env_manifest_hash=self.resolution[
                "runtime_binding_manifest_hash"
            ],
            artifact_env_manifest_hash=self.artifact_manifest["manifest_hash"],
            execution_policy_manifest_hash=self.policy_manifest["manifest_hash"],
            working_directory=self.temp.name,
        )
        cases = {
            "runtime_env_manifest_hash": "sha256:runtime-drift",
            "artifact_env_manifest_hash": "sha256:artifact-drift",
            "execution_policy_manifest_hash": "sha256:policy-drift",
            "working_directory": str(Path(self.temp.name) / "other"),
        }
        for field, replacement in cases.items():
            observed = build_action_approval_identity(
                generated_script_sha256=generated_hash,
                capability_contract_hash_value=capability_contract_hash(
                    self.contract
                ),
                runtime_env_manifest_hash=(
                    replacement
                    if field == "runtime_env_manifest_hash"
                    else self.resolution["runtime_binding_manifest_hash"]
                ),
                artifact_env_manifest_hash=(
                    replacement
                    if field == "artifact_env_manifest_hash"
                    else self.artifact_manifest["manifest_hash"]
                ),
                execution_policy_manifest_hash=(
                    replacement
                    if field == "execution_policy_manifest_hash"
                    else self.policy_manifest["manifest_hash"]
                ),
                working_directory=(
                    replacement if field == "working_directory" else self.temp.name
                ),
            )
            with self.subTest(field=field):
                with self.assertRaises(ActionRuntimeContractError) as raised:
                    validate_action_approval_identity(approved, observed)
                expected = (
                    "RUNTIME_BINDING_SNAPSHOT_DRIFT"
                    if field == "runtime_env_manifest_hash"
                    else "ACTION_APPROVAL_IDENTITY_DRIFT"
                )
                self.assertEqual(raised.exception.code, expected)

    def test_manifests_cover_value_digests_without_raw_values(self):
        runtime_manifest = self.resolution["runtime_binding_manifest"]
        rendered_runtime = str(runtime_manifest)
        rendered_artifact = str(self.artifact_manifest)

        self.assertIn("value_digest", rendered_runtime)
        self.assertIn("value_digest", rendered_artifact)
        self.assertNotIn(self.tool, rendered_runtime)
        self.assertNotIn(self.artifacts[0]["placeholder"], rendered_artifact)
        self.assertEqual(runtime_manifest["scope"], "current_action")
        self.assertEqual(self.artifact_manifest["scope"], "current_action")
        self.assertEqual(self.policy_manifest["scope"], "current_action")

    def test_execution_environment_removes_other_capability_runtime_names(self):
        effective = build_action_execution_environment(
            {
                "PATH": "/usr/bin:/bin",
                "JAVA_BIN": "/ambient/java",
                "RESOLVED_SNPEFF_JAR": "/ambient/snpEff.jar",
                "KEEP_BASE": "base-value",
            },
            runtime_bindings={"TOOL_BIN": self.tool},
            artifact_bindings={
                "BIOMIND_INPUT_REFERENCE_FASTA": self.artifacts[0]["placeholder"],
            },
            controller_bindings={"BIOMIND_ACTION_ID": "action-1"},
            managed_runtime_env_names={
                "JAVA_BIN",
                "RESOLVED_SNPEFF_JAR",
                "TOOL_BIN",
            },
        )

        self.assertEqual(effective["TOOL_BIN"], self.tool)
        self.assertEqual(effective["KEEP_BASE"], "base-value")
        self.assertNotIn("JAVA_BIN", effective)
        self.assertNotIn("RESOLVED_SNPEFF_JAR", effective)


if __name__ == "__main__":
    unittest.main()
