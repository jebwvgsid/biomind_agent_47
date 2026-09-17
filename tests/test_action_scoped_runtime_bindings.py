from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.action_runtime import (
    ActionRuntimeContractError,
    assert_current_action_runtime_scope,
    build_runtime_binding_registry,
    capability_contract_hash,
    project_action_runtime_for_provider,
    resolve_action_runtime_bindings,
)
from agent_core.plan_tracker import semantic_plan_hash
from agent_core.skill_manager import SkillManager


class ActionScopedRuntimeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        java_home = root / "jdk"
        java_bin = java_home / "bin" / "java"
        java_bin.parent.mkdir(parents=True)
        java_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        java_bin.chmod(0o755)
        tool_bin = root / "env" / "bin" / "tool"
        tool_bin.parent.mkdir(parents=True)
        tool_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool_bin.chmod(0o755)
        jar = root / "snpEff.jar"
        jar.write_bytes(b"jar")
        self.paths = {
            "java_home": str(java_home),
            "java_bin": str(java_bin),
            "tool_bin": str(tool_bin),
            "jar": str(jar),
        }
        self.state = {
            "runtime_audit": [{
                "runtime_audit_id": "audit-demo-1",
                "skill_id": "demo/skill",
                "status": "ready",
                "resolved_runtime_paths": {
                    "RESOLVED_JAVA_HOME": self.paths["java_home"],
                    "RESOLVED_SNPEFF_JAR": self.paths["jar"],
                    "RESOLVED_TOOL_BIN": self.paths["tool_bin"],
                },
            }]
        }
        self.capabilities = {
            "demo.check": {
                "capability_id": "demo.check",
                "selected_skill_id": "demo/skill",
                "required_runtime_binding_ids": [],
                "optional_runtime_binding_ids": [],
                "required_artifact_binding_ids": ["artifact.reference_fasta"],
                "execution_guidance": [],
            },
            "demo.run": {
                "capability_id": "demo.run",
                "selected_skill_id": "demo/skill",
                "required_runtime_binding_ids": ["runtime.tool.bin"],
                "optional_runtime_binding_ids": [],
                "required_artifact_binding_ids": [],
                "execution_guidance": [],
            },
        }

    def _resolve(self, capability_id: str, **overrides):
        contract = self.capabilities.get(capability_id) or {}
        kwargs = {
            "selected_skill_id": "demo/skill",
            "execution_capability_id": capability_id,
            "approved_capability_contract_hash": (
                capability_contract_hash(contract) if contract else ""
            ),
            "capability_registry": self.capabilities,
            "ready_runtime_binding_registry": build_runtime_binding_registry(self.state),
            "current_action_identity": {
                "action_id": "action-1",
                "plan_id": "plan-1",
                "step_id": "step-1",
            },
        }
        kwargs.update(overrides)
        return resolve_action_runtime_bindings(**kwargs)

    def test_same_skill_capability_without_runtime_never_receives_other_binding(self):
        check = self._resolve("demo.check")
        run = self._resolve("demo.run")

        self.assertEqual(check["effective_runtime_bindings"], {})
        self.assertEqual(run["effective_runtime_bindings"], {
            "TOOL_BIN": self.paths["tool_bin"],
        })
        self.assertNotIn("JAVA_BIN", check["effective_runtime_bindings"])
        self.assertNotIn("SNPEFF_JAR", check["effective_runtime_bindings"])

    def test_registry_has_canonical_ids_without_resolved_aliases(self):
        registry = build_runtime_binding_registry(self.state)

        self.assertEqual(registry["runtime.java.bin"]["env_name"], "JAVA_BIN")
        self.assertEqual(registry["runtime.snpeff.jar"]["env_name"], "SNPEFF_JAR")
        self.assertEqual(registry["runtime.tool.bin"]["env_name"], "TOOL_BIN")
        self.assertNotIn("RESOLVED_JAVA_HOME", str(registry))
        self.assertNotIn("RESOLVED_SNPEFF_JAR", str(registry))

    def test_unknown_capability_reports_available_ids(self):
        with self.assertRaises(ActionRuntimeContractError) as raised:
            self._resolve("demo.unknown")

        diagnostic = raised.exception.to_diagnostic()
        self.assertEqual(diagnostic["code"], "UNKNOWN_EXECUTION_CAPABILITY")
        self.assertEqual(diagnostic["received_id"], "demo.unknown")
        self.assertEqual(
            diagnostic["available_capability_ids"],
            ["demo.check", "demo.run"],
        )

    def test_missing_capability_never_falls_back_to_registry(self):
        with self.assertRaises(ActionRuntimeContractError) as raised:
            self._resolve("")

        self.assertEqual(
            raised.exception.code,
            "ACTION_CAPABILITY_SCOPE_MISSING",
        )

    def test_required_unknown_runtime_is_unavailable_and_not_guessed(self):
        capabilities = {
            "demo.missing": {
                "capability_id": "demo.missing",
                "selected_skill_id": "demo/skill",
                "required_runtime_binding_ids": ["runtime.absent.bin"],
                "optional_runtime_binding_ids": [],
                "required_artifact_binding_ids": [],
                "execution_guidance": [],
            }
        }
        with self.assertRaises(ActionRuntimeContractError) as raised:
            resolve_action_runtime_bindings(
                selected_skill_id="demo/skill",
                execution_capability_id="demo.missing",
                approved_capability_contract_hash=capability_contract_hash(
                    capabilities["demo.missing"]
                ),
                capability_registry=capabilities,
                ready_runtime_binding_registry=build_runtime_binding_registry(self.state),
                current_action_identity={},
            )

        self.assertEqual(
            raised.exception.code,
            "REQUIRED_RUNTIME_BINDING_UNAVAILABLE",
        )

    def test_planner_cannot_create_optional_binding_id(self):
        with self.assertRaises(ActionRuntimeContractError) as raised:
            self._resolve(
                "demo.check",
                requested_optional_ids=["runtime.snpeff.jar"],
            )

        self.assertEqual(raised.exception.code, "UNDECLARED_RUNTIME_BINDING")

    def test_provider_projection_has_exact_current_value_and_no_unrelated_bindings(self):
        projection = project_action_runtime_for_provider(self._resolve("demo.run"))
        rendered = str(projection)

        self.assertIn("runtime.tool.bin", rendered)
        self.assertIn(self.paths["tool_bin"], rendered)
        self.assertNotIn("$TOOL_BIN", rendered)
        self.assertNotIn("runtime.java.bin", rendered)
        self.assertNotIn("runtime.snpeff.jar", rendered)

    def test_source_text_cannot_grant_runtime_binding(self):
        source = "# $SNPEFF_JAR is documentation only\ncat <<'EOF'\n$JAVA_BIN\nEOF\n"
        resolution = self._resolve("demo.check")

        self.assertIn("$SNPEFF_JAR", source)
        self.assertEqual(resolution["effective_runtime_bindings"], {})

    def test_runtime_value_digest_change_invalidates_manifest(self):
        first = self._resolve("demo.run")
        changed_state = {
            "runtime_audit": [{
                "runtime_audit_id": "audit-demo-1",
                "skill_id": "demo/skill",
                "status": "ready",
                "resolved_runtime_paths": {
                    "RESOLVED_TOOL_BIN": self.paths["tool_bin"] + ".replacement",
                },
            }]
        }
        second = self._resolve(
            "demo.run",
            ready_runtime_binding_registry=build_runtime_binding_registry(changed_state),
        )

        self.assertNotEqual(
            first["runtime_binding_manifest_hash"],
            second["runtime_binding_manifest_hash"],
        )

    def test_sensitive_value_is_not_persisted_in_manifest(self):
        registry = build_runtime_binding_registry(self.state)
        secret = "synthetic-sensitive-value"
        registry["runtime.tool.bin"] = {
            **registry["runtime.tool.bin"],
            "value": secret,
            "value_digest": "sha256:synthetic",
            "sensitivity": "secret",
        }
        resolution = self._resolve(
            "demo.run", ready_runtime_binding_registry=registry
        )

        self.assertEqual(resolution["effective_runtime_bindings"]["TOOL_BIN"], secret)
        self.assertNotIn(secret, str(resolution["runtime_binding_manifest"]))

    def test_legacy_pending_action_requires_regeneration(self):
        with self.assertRaises(ActionRuntimeContractError) as raised:
            assert_current_action_runtime_scope({
                "action_id": "legacy-action",
                "step_id": "step-1",
                "runtime_snapshot": {"bindings": {"JAVA_BIN": "/legacy/java"}},
            })

        self.assertEqual(
            raised.exception.code,
            "LEGACY_RUNTIME_SCOPE_REQUIRES_REGENERATION",
        )

    def test_capability_and_contract_hash_change_plan_semantic_identity(self):
        base = {
            "steps": [{
                "step_id": "step-1",
                "title": "Run",
                "action": "execute",
                "purpose": "analysis",
                "tool": "tool",
                "execution_capability_id": "demo.run",
                "capability_contract_hash": "sha256:first",
                "inputs": [],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["exit zero"],
                "skill_ids": ["demo/skill"],
                "status": "pending",
                "risk_level": "low",
            }]
        }
        changed = {"steps": [{**base["steps"][0], "capability_contract_hash": "sha256:second"}]}

        self.assertNotEqual(semantic_plan_hash(base), semantic_plan_hash(changed))


class SnpeffCapabilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manager = SkillManager(str(Path(__file__).parents[1] / "skills"))

    def test_current_inspect_and_execute_keep_explicit_optional_runtime_scope(self):
        registry = self.manager.get_execution_capability_registry([
            "genomics/snpeff-annotation"
        ])

        check = registry["genomics.snpeff_annotation.inspect"]
        build = registry["genomics.snpeff_annotation.execute"]
        self.assertEqual(check["required_runtime_binding_ids"], [])
        self.assertEqual(build["required_runtime_binding_ids"], [])
        self.assertEqual(check["optional_runtime_binding_ids"], ["runtime.java.bin", "runtime.snpeff.jar"])
        self.assertEqual(build["optional_runtime_binding_ids"], ["runtime.java.bin", "runtime.snpeff.jar"])
        self.assertEqual(check["operation"], "inspect")
        self.assertEqual(build["operation"], "produce_files")
        self.assertEqual(build["required_artifact_binding_ids"], [])
        self.assertNotIn("runtime.snpeff.config", build["required_runtime_binding_ids"])
        frozen_hash = capability_contract_hash(build)
        resolved = resolve_action_runtime_bindings(selected_skill_id="genomics/snpeff-annotation",
            execution_capability_id=build["capability_id"], approved_capability_contract_hash=frozen_hash,
            capability_registry=registry, ready_runtime_binding_registry={}, current_action_identity={})
        self.assertEqual(resolved["effective_runtime_bindings"], {})
        self.assertEqual(resolved["runtime_binding_manifest"]["bindings"], [])
        changed = {**registry, build["capability_id"]: {
            **build,
            "required_runtime_binding_ids": ["runtime.java.bin"],
            "optional_runtime_binding_ids": ["runtime.snpeff.jar"],
        }}
        with self.assertRaises(ActionRuntimeContractError) as raised:
            resolve_action_runtime_bindings(selected_skill_id="genomics/snpeff-annotation",
                execution_capability_id=build["capability_id"], approved_capability_contract_hash=frozen_hash,
                capability_registry=changed, ready_runtime_binding_registry={}, current_action_identity={})
        self.assertEqual(raised.exception.code, "RUNTIME_BINDING_SNAPSHOT_DRIFT")
        self.assertEqual(build["required_runtime_binding_ids"], [])

    def test_plan_validation_rejects_unknown_and_missing_capabilities(self):
        base = {
            "action": "execute",
            "status": "pending",
            "skill_ids": ["genomics/snpeff-annotation"],
        }
        issues = self.manager.validate_plan_execution_capabilities(
            {"steps": [
                {**base, "step_id": "missing"},
                {
                    **base,
                    "step_id": "unknown",
                    "execution_capability_id": "planner.invented.binding",
                },
            ]},
            ["genomics/snpeff-annotation"],
        )

        self.assertEqual(
            [item["code"] for item in issues],
            ["ACTION_CAPABILITY_SCOPE_MISSING", "UNKNOWN_EXECUTION_CAPABILITY"],
        )
        self.assertIn(
            "genomics.snpeff_annotation.inspect",
            issues[0]["available_capability_ids"],
        )
        self.assertNotIn("genomics.snpeff_annotation.check_contig_compatibility", issues[0]["available_capability_ids"])
        self.assertNotIn("genomics.snpeff_annotation.build_database", issues[0]["available_capability_ids"])


if __name__ == "__main__":
    unittest.main()
