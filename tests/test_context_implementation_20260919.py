"""Offline regressions for separate capacities, wire protection and frozen seed."""
import asyncio
import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from agent_core.workflow import context_compaction as cc
from tests.test_schema2_context_compaction import ContextCompactionFixture, _CodepointEstimator


class BudgetAndPrefixTests(unittest.TestCase):
    def test_input_cap_is_not_a_total_window(self):
        policy = cc.ContextBudgetPolicy(application_context_limit_tokens=1_000_000,
            output_reserve_tokens=50_000, estimation_margin_tokens=4096)
        model = SimpleNamespace(profile={"max_input_tokens": 256_000, "context_window": 1_000_000})
        resolved = cc.model_context_policy(policy, model, resolve_output=False)
        self.assertEqual(resolved.safe_input_tokens, 251904)
        self.assertEqual(resolved.known_provider_context_limit_tokens, 1_000_000)
        smaller = cc.model_context_policy(policy,
            SimpleNamespace(profile={"context_window": 200_000}), resolve_output=False)
        self.assertEqual(smaller.safe_input_tokens, 145904)
        self.assertEqual(smaller.trigger_tokens, 143856)
        self.assertEqual(cc.model_context_policy(policy, object(), resolve_output=False), policy)
        output_bounded = cc.model_context_policy(policy,
            SimpleNamespace(profile={"max_output_tokens":16000}), resolve_output=False)
        self.assertEqual(output_bounded.output_reserve_tokens, 16000)
        self.assertEqual(output_bounded.known_provider_output_limit_tokens, 16000)

    def test_default_is_application_target_not_remote_capability(self):
        policy = cc.ContextBudgetPolicy()
        self.assertEqual(policy.trigger_tokens, 256_000)
        self.assertEqual(policy.output_reserve_tokens, 50_000)
        self.assertIsNone(policy.known_provider_context_limit_tokens)
        self.assertIn("not_verified", policy.context_capability_source)






class FrozenExecutionEnvironmentTests(unittest.TestCase):
    def test_snapshot_survives_ambient_drift_and_preserves_explicit_bindings(self):
        from agent_core.action_runtime import freeze_execution_environment, build_exact_action_environment
        ambient = {"PATH":"/usr/bin:/bin", "LC_ALL":"C.UTF-8", "TMPDIR":"/tmp/task", "API_KEY":"never copied"}
        frozen = freeze_execution_environment(ambient, {"REFERENCE_FASTA":"/data/reference.fa", "RUNTIME_PYTHON":"/runtime/bin/python"})
        ambient["PATH"] = "/different/path"
        launch = build_exact_action_environment({}, action_environment=frozen,
            controller_identity={"BIOMIND_TASK_ID":"task-1"})
        self.assertEqual(launch["PATH"], "/usr/bin:/bin")
        self.assertEqual(launch["REFERENCE_FASTA"], "/data/reference.fa")
        self.assertEqual(launch["RUNTIME_PYTHON"], "/runtime/bin/python")
        self.assertNotIn("API_KEY", launch)

    def test_approved_environment_drift_is_rejected(self):
        from agent_core.action_runtime import build_action_approval_identity, validate_action_approval_identity, ActionRuntimeContractError
        args = dict(generated_script_sha256="code", capability_contract_hash_value="capability",
            runtime_env_manifest_hash="runtime", artifact_env_manifest_hash="artifact",
            execution_policy_manifest_hash="policy", working_directory="/tmp")
        approved = build_action_approval_identity(**args, execution_environment={"PATH":"/usr/bin:/bin"})
        observed = build_action_approval_identity(**args, execution_environment={"PATH":"/unapproved/bin"})
        with self.assertRaisesRegex(ActionRuntimeContractError, "ACTION_APPROVAL_IDENTITY_DRIFT"):
            validate_action_approval_identity(approved, observed)
        # Explicit legacy callers retain their existing identity schema/hash.
        legacy = build_action_approval_identity(**args)
        self.assertNotIn("execution_environment_hash", legacy)
        validate_action_approval_identity(legacy, legacy)

    def test_relative_path_and_invalid_tmpdir_fail_closed(self):
        from agent_core.action_runtime import freeze_execution_environment, ActionRuntimeContractError
        for supplied in ({"PATH":"/bin:."}, {"PATH":"/bin", "TMPDIR":"relative"}):
            with self.assertRaises(ActionRuntimeContractError):
                freeze_execution_environment(supplied)




if __name__ == "__main__":
    unittest.main()
