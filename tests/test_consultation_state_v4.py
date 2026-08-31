import unittest

from agent_core.consultation_state import (
    commit_finalize_v4_transition,
    finalize_protocol_runner,
    initialize_finalize_protocol_v4,
    legacy_finalize_migration_eligibility,
    new_finalize_protocol,
    validate_finalize_protocol_v4,
)


def _state():
    return initialize_finalize_protocol_v4(
        {},
        finalize_context_id="finalize-v4-context",
        base_contract_hash="base-hash",
        replay_scope_id="task-lineage-1",
        source_task_spec_revision=3,
    )


class FinalizeProtocolV4Tests(unittest.TestCase):
    def test_runner_dispatch_keeps_legacy_and_v4_separate(self):
        legacy = new_finalize_protocol("legacy-context", "legacy-hash")
        self.assertEqual(finalize_protocol_runner(legacy), "legacy_v2")
        current = _state()["finalize_protocol"]
        self.assertEqual(finalize_protocol_runner(current), "v4")
        with self.assertRaises(ValueError):
            finalize_protocol_runner({"schema_version": 4, "wire_schema_version": 2})

    def test_deterministic_compile_has_no_provider_attempt(self):
        state = _state()
        state = commit_finalize_v4_transition(state, {
            "transition": "coverage_ready",
            "coverage_artifact_ref": "/protected/coverage.json",
            "coverage_artifact_hash": "coverage-hash",
            "expected_resolution_ids": [],
        })
        state = commit_finalize_v4_transition(state, {
            "transition": "compiled",
            "compile_mode": "controller_deterministic",
            "coverage_artifact_ref": "/protected/coverage.json",
            "coverage_artifact_hash": "coverage-hash",
            "compiled_contract_ref": "/protected/contract.json",
            "compiled_contract_hash": "contract-hash",
        })
        protocol = validate_finalize_protocol_v4(state["finalize_protocol"])
        self.assertEqual(protocol["finalize_status"], "compiled")
        self.assertEqual(protocol["provider_status"], "not_called")
        self.assertEqual(protocol["completion_integrity"], "not_called")
        self.assertEqual(protocol["protocol_status"], "valid")
        self.assertEqual(protocol["attempts"], [])

    def test_truncation_is_succeeded_and_never_outcome_unknown(self):
        state = _state()
        state = commit_finalize_v4_transition(state, {
            "transition": "coverage_ready",
            "coverage_artifact_ref": "/protected/coverage.json",
            "coverage_artifact_hash": "coverage-hash",
            "candidate_catalog_ref": "/protected/catalog.json",
            "candidate_catalog_hash": "catalog-hash",
            "expected_resolution_ids": ["resolution-1"],
        })
        state = commit_finalize_v4_transition(state, {
            "transition": "batch_created",
            "batch": {
                "batch_id": "batch-1",
                "batch_hash": "batch-hash",
                "item_ids": ["resolution-1"],
                "status": "pending",
            },
        })
        state = commit_finalize_v4_transition(state, {
            "transition": "request_started",
            "attempt": {
                "attempt_id": "attempt-1",
                "call_id": "call-1",
                "call_kind": "decision_batch",
                "decision_batch_id": "batch-1",
                "decision_batch_hash": "batch-hash",
                "request_intent_ref": "/protected/intent.json",
                "request_intent_hash": "intent-hash",
                "protocol_request_hash": "protocol-hash",
                "wire_request_hash": "wire-hash",
                "capacity_profile_ref": "/protected/capacity.json",
                "capacity_profile_hash": "capacity-hash",
                "response_schema_ref": "/protected/schema.json",
                "response_schema_hash": "schema-hash",
                "effective_wire_settings_ref": "/protected/wire.json",
                "effective_wire_settings_hash": "wire-settings-hash",
            },
        })
        state = commit_finalize_v4_transition(state, {
            "transition": "response_truncated",
            "attempt_id": "attempt-1",
            "finish_reason": "length",
            "usage": {
                "prompt_tokens": 25_162,
                "completion_tokens": 8_192,
                "total_tokens": 33_354,
            },
            "partial_response_artifact_ref": "/protected/partial.json",
            "partial_response_artifact_hash": "partial-hash",
        })
        protocol = validate_finalize_protocol_v4(state["finalize_protocol"])
        attempt = protocol["attempts"][0]
        self.assertEqual(attempt["provider_status"], "succeeded")
        self.assertEqual(attempt["completion_integrity"], "truncated")
        self.assertEqual(attempt["protocol_status"], "not_validated")
        self.assertEqual(protocol["aggregate"]["used_completion_tokens"], 8_192)
        with self.assertRaises(ValueError):
            commit_finalize_v4_transition(state, {
                "transition": "compiled",
                "compile_mode": "model_batched",
                "coverage_artifact_ref": "/protected/coverage.json",
                "coverage_artifact_hash": "coverage-hash",
                "compiled_contract_ref": "/protected/contract.json",
                "compiled_contract_hash": "contract-hash",
            })

    def test_migration_matrix_never_rewrites_inflight_legacy_attempt(self):
        legacy = new_finalize_protocol("legacy-context", "legacy-hash")
        self.assertTrue(legacy_finalize_migration_eligibility(legacy)["eligible"])
        legacy["status"] = "invoking"
        legacy["attempts"] = [{"attempt_id": "attempt-1"}]
        decision = legacy_finalize_migration_eligibility(
            legacy,
            explicit_resume=True,
            task_spec_revised=True,
        )
        self.assertFalse(decision["eligible"])
        self.assertIn("pinned", decision["reason"])


if __name__ == "__main__":
    unittest.main()

