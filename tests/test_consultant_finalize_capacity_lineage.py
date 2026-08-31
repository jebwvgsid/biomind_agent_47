import json
import tempfile
import unittest
from pathlib import Path

from agent_core.context_lineage import ContextLineageStore, SnapshotConflictError


class FinalizeCapacityLineageTests(unittest.TestCase):
    def _claimed_store(self, root: str, *, attempt_id: str = "attempt-1"):
        store = ContextLineageStore(root, "task-finalize-v4")
        store.begin_finalize_attempt(
            attempt_id=attempt_id,
            call_id=f"call-{attempt_id}",
            call_kind="decision_batch",
            canonical_request_hash=f"request-{attempt_id}",
        )
        return store

    def test_controller_artifacts_are_immutable_hash_bound_and_independently_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            store = ContextLineageStore(root, "task-finalize-v4")
            coverage = store.persist_finalize_coverage_artifact({
                "coverage_artifact_id": "coverage-1",
                "finalize_context_id": "finalize-context-1",
                "dispositions": [
                    {
                        "directive_id": "directive-1",
                        "disposition": "deterministic_constraint",
                    }
                ],
                "api_key": "must-not-be-persisted",
            })
            self.assertEqual(coverage["api_key"], "[REDACTED]")
            self.assertNotIn(
                "must-not-be-persisted",
                Path(coverage["coverage_artifact_ref"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                store.lookup_finalize_coverage_artifact(
                    coverage_artifact_hash=coverage["coverage_artifact_hash"]
                ),
                coverage,
            )

            capacity = store.persist_finalize_capacity_profile({
                "profile_id": "capacity-1",
                "finalize_context_id": "finalize-context-1",
                "requested_output_tokens": 100_000,
                "provider_output_cap": 100_000,
                "provider_context_limit": 200_000,
                "context_remaining_tokens": 150_000,
                "effective_output_tokens": 100_000,
                "safety_headroom_tokens": 2_048,
                "minimum_canonical_output_tokens": 100,
                "maximum_canonical_output_tokens": 5_000,
                "canonical_admissible": True,
                "effective_token_cap_verified": True,
                "raw_wire_bound_mode": "token_cap_only",
                "raw_completion_guaranteed": False,
            })
            self.assertEqual(
                store.lookup_finalize_capacity_profile(
                    profile_id="capacity-1"
                )["profile_hash"],
                capacity["profile_hash"],
            )

            compiled = store.persist_finalize_compiled_contract({
                "compiled_contract_id": "compiled-1",
                "finalize_context_id": "finalize-context-1",
                "coverage_artifact_ref": coverage["coverage_artifact_ref"],
                "coverage_artifact_hash": coverage["coverage_artifact_hash"],
                "compile_mode": "controller_deterministic",
                "compiled_contract": {"constraints": []},
            })
            self.assertEqual(
                store.lookup_finalize_compiled_contract(
                    compiled_contract_hash=compiled["compiled_contract_hash"]
                ),
                compiled,
            )

            # Replaying the same immutable artifact is idempotent; changing its
            # payload under the same ID is a durable conflict.
            self.assertEqual(
                store.persist_finalize_capacity_profile({
                    key: value
                    for key, value in capacity.items()
                    if key not in {"profile_hash", "capacity_profile_ref"}
                }),
                capacity,
            )
            changed = {
                key: value
                for key, value in capacity.items()
                if key not in {"profile_hash", "capacity_profile_ref"}
            }
            changed["effective_output_tokens"] = 99_999
            with self.assertRaises(SnapshotConflictError):
                store.persist_finalize_capacity_profile(changed)

    def test_effective_wire_settings_are_credential_free_and_bound_to_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._claimed_store(root)
            wire = store.persist_finalize_effective_wire_settings(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
                settings={
                    "api_surface": "chat_completions",
                    "provider_id": "deepseek",
                    "model": "deepseek-v4-pro",
                    "output_token_parameter": "max_tokens",
                    "output_token_value": 100_000,
                    "stream_mode": False,
                    "sanitized_payload_keys": [
                        "max_tokens",
                        "messages",
                        "model",
                        "response_format",
                        "stream",
                    ],
                    "authorization": "Bearer secret-value",
                },
            )
            self.assertEqual(wire["output_token_parameter"], "max_tokens")
            self.assertEqual(wire["authorization"], "[REDACTED]")
            persisted_text = Path(wire["effective_wire_settings_ref"]).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("secret-value", persisted_text)
            loaded = store.lookup_finalize_effective_wire_settings(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
            )
            self.assertEqual(loaded, wire)
            status = store.list_finalize_attempts()[0]
            self.assertEqual(
                status["effective_wire_settings_hash"],
                wire["effective_wire_settings_hash"],
            )

    def test_length_partial_is_durable_disjoint_and_reconciles_without_reinvoke(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._claimed_store(root)
            store.persist_finalize_effective_wire_settings(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
                settings={
                    "output_token_parameter": "max_tokens",
                    "output_token_value": 100_000,
                    "stream_mode": False,
                },
            )
            exact_partial = '{"action_type":"resolve_batch","resolutions":['
            partial = store.persist_finalize_partial_response(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
                raw_partial_response=exact_partial,
                finish_reason="length",
                usage={
                    "prompt_tokens": 25_162,
                    "completion_tokens": 8_192,
                    "total_tokens": 33_354,
                },
                provider_metadata={"response_id": "response-1"},
                content_available=True,
            )
            self.assertEqual(partial["provider_status"], "succeeded")
            self.assertEqual(partial["completion_integrity"], "truncated")
            self.assertEqual(partial["protocol_status"], "not_validated")
            self.assertEqual(partial["error_code"], "FINALIZE_RESPONSE_TRUNCATED")
            self.assertEqual(partial["usage"]["completion_tokens"], 8_192)
            self.assertTrue(Path(partial["partial_response_artifact_ref"]).is_file())

            # Partial content is available only from the typed partial lookup.
            self.assertIsNone(
                store.lookup_finalize_response_artifact(
                    attempt_id="attempt-1",
                    call_id="call-attempt-1",
                    canonical_request_hash="request-attempt-1",
                )
            )
            loaded = store.lookup_finalize_partial_response(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
            )
            self.assertEqual(loaded["raw_partial_response"], exact_partial)
            self.assertEqual(
                loaded["partial_response_artifact_hash"],
                partial["partial_response_artifact_hash"],
            )
            reconciliation = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
            )
            self.assertEqual(reconciliation["decision"], "reuse_partial_response")
            self.assertTrue(
                reconciliation["partial_response_reused_from_checkpoint"]
            )

            with self.assertRaises(SnapshotConflictError):
                store.persist_finalize_response(
                    attempt_id="attempt-1",
                    call_id="call-attempt-1",
                    canonical_request_hash="request-attempt-1",
                    raw_response="{}",
                )
            with self.assertRaises(SnapshotConflictError):
                store.record_finalize_attempt_failure(
                    attempt_id="attempt-1",
                    call_id="call-attempt-1",
                    canonical_request_hash="request-attempt-1",
                    provider_status="fatal_failed",
                    error_code="FINALIZE_PROVIDER_FATAL",
                    error_owner="provider",
                )

    def test_partial_content_unavailable_uses_null_not_empty_hash(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._claimed_store(root, attempt_id="attempt-no-content")
            partial = store.persist_finalize_partial_response(
                attempt_id="attempt-no-content",
                call_id="call-attempt-no-content",
                canonical_request_hash="request-attempt-no-content",
                raw_partial_response=None,
                finish_reason="length",
                usage={"prompt_tokens": 10, "completion_tokens": 20},
                content_available=False,
                content_unavailable_reason="sdk_completion_content_missing",
            )
            self.assertIsNone(partial["response_content_hash"])
            self.assertIsNone(partial["raw_partial_response_ref"])
            loaded = store.lookup_finalize_partial_response(
                attempt_id="attempt-no-content"
            )
            self.assertIsNone(loaded["raw_partial_response"])
            self.assertEqual(loaded["usage"]["total_tokens"], 30)

    def test_partial_manifest_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._claimed_store(root)
            partial = store.persist_finalize_partial_response(
                attempt_id="attempt-1",
                call_id="call-attempt-1",
                canonical_request_hash="request-attempt-1",
                raw_partial_response="partial",
                finish_reason="length",
                usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            )
            manifest_path = Path(partial["partial_response_artifact_ref"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["usage"]["completion_tokens"] = 3
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(SnapshotConflictError):
                store.lookup_finalize_partial_response(attempt_id="attempt-1")


if __name__ == "__main__":
    unittest.main()
