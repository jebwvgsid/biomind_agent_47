"""Capability readiness must agree with evidence, including empty and failed observations."""
import unittest
from agent_core.consultation_state import route_runtime_preflight
from agent_core.action_runtime import ActionRuntimeContractError, resolve_action_runtime_bindings

class RuntimeReadinessProjectionTests(unittest.TestCase):
    def test_schema2_required_fact_missing_remains_unknown(self):
        for audits in ({}, {"tool": {"status": "unknown"}}, {"tool": {"status": "ready", "evidence_status": "unchecked"}}):
            with self.subTest(audits=audits):
                result = route_runtime_preflight({}, audits, schema_version=2, required_binding_ids=["runtime.tool.bin"])
                self.assertEqual(result["code"], "RUNTIME_EVIDENCE_PENDING")
                self.assertEqual(result["required_binding_availability"], {"runtime.tool.bin": "UNKNOWN"})
                self.assertEqual(result["route"], "planner")

    def test_independent_negative_conflict_and_no_requirement(self):
        for availability, code in (("AVAILABLE", "RUNTIME_READY"), ("UNAVAILABLE", "RUNTIME_DEPENDENCY_CONFIRMED_UNAVAILABLE"), ("AMBIGUOUS", "RUNTIME_EVIDENCE_CONFLICT")):
            with self.subTest(availability=availability):
                result = route_runtime_preflight({}, {}, schema_version=2, required_binding_ids=["runtime.tool.bin"], binding_evaluations={"runtime.tool.bin": {"availability": availability}})
                self.assertEqual(result["code"], code)
        self.assertEqual(route_runtime_preflight({}, {}, schema_version=2)["code"], "RUNTIME_READY")
        self.assertEqual(route_runtime_preflight({}, {})["code"], "RUNTIME_READY")

    def test_resolver_classifies_lookup_miss_without_losing_required_contract(self):
        contract = {"capability_id": "tool.run", "selected_skill_id": "tool", "required_runtime_binding_ids": ["runtime.tool.bin"]}
        for registry, availability in (({}, "UNKNOWN"), ({"runtime.tool.bin": {"status": "unavailable"}}, "UNAVAILABLE"), ({"runtime.tool.bin": {"status": "ambiguous"}}, "AMBIGUOUS")):
            with self.subTest(availability=availability):
                with self.assertRaises(ActionRuntimeContractError) as caught:
                    resolve_action_runtime_bindings(selected_skill_id="tool", execution_capability_id="tool.run", approved_capability_contract_hash="", capability_registry={"tool.run": contract}, ready_runtime_binding_registry=registry, current_action_identity={"step_id": "s"})
                self.assertEqual(caught.exception.diagnostic["availability"], availability)
                self.assertEqual(caught.exception.diagnostic["failure_scope"], "runtime_fact")
                self.assertTrue(caught.exception.diagnostic["required"])

    def test_prelaunch_revalidates_paths_and_retains_accepted_manifest(self):
        import tempfile
        from pathlib import Path
        from copy import deepcopy
        from agent_core.action_runtime import validate_runtime_paths_prelaunch
        with tempfile.TemporaryDirectory(prefix="runtime-prelaunch-") as root:
            path = Path(root) / "tool"
            path.write_text("fixture only; never executed")
            path.chmod(0o755)
            resolution = {"effective_runtime_bindings": {"TOOL_BIN": str(path)},
                          "runtime_binding_records": [{"binding_id": "runtime.tool.bin", "env_name": "TOOL_BIN", "value_kind": "executable_path", "value_digest": "accepted"}],
                          "runtime_binding_manifest_hash": "original-approval"}
            original = deepcopy(resolution)
            validate_runtime_paths_prelaunch(resolution)
            path.chmod(0o644)
            with self.assertRaises(ActionRuntimeContractError) as caught:
                validate_runtime_paths_prelaunch(resolution)
            self.assertEqual(caught.exception.code, "RUNTIME_BINDING_PRELAUNCH_PATH_INVALID")
            self.assertEqual(resolution, original)
