import asyncio
import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.context_lineage import (
    ContextLineageStore,
    SnapshotConflictError,
    build_decision_lineage_bundle,
    canonical_finalize_request_hash,
    finalize_response_content_hash,
    record_finalize_response_persistence_failure,
)
from agent_core.plan_tracker import (
    build_plan_state,
    record_provenance_event,
    save_plan_state,
)
from agent_core.recovery import ExecutionRecord, RecoveryStateStore
from agent_core.router import classify_task_mode
from bio_agents import BioAnalystAgent


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content=self.content)


class ContextLineageSnapshotTests(unittest.TestCase):
    def test_snapshot_preserves_exact_message_roles_and_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            snapshot = store.start_call(
                [
                    SystemMessage(content="system rules"),
                    HumanMessage(content="user request"),
                    AIMessage(content="observable prior answer"),
                ],
                {"node_name": "consultant", "call_kind": "consultant"},
            )
            request = json.loads(Path(snapshot["request_payload_ref"]).read_text(encoding="utf-8"))
            self.assertEqual([row["role"] for row in request["messages"]], ["system", "human", "assistant"])
            self.assertEqual(
                [row["content"] for row in request["messages"]],
                ["system rules", "user request", "observable prior answer"],
            )

    def test_redaction_removes_credentials_but_keeps_paths_and_arguments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            snapshot = store.start_call(
                [HumanMessage(content=(
                    "Authorization: Bearer secret-token\n"
                    "DEEPSEEK_API_KEY=sk-secret-value\n"
                    "/public/home/liusiqi/data/genome.gtf --gtf22"
                ))],
                {
                    "node_name": "generate",
                    "call_kind": "generate",
                    "runtime_audit": {"password": "plain-secret", "env": "bio_gatk"},
                },
            )
            request_text = Path(snapshot["request_payload_ref"]).read_text(encoding="utf-8")
            manifest = json.loads(Path(snapshot["snapshot_ref"]).read_text(encoding="utf-8"))
            self.assertNotIn("secret-token", request_text)
            self.assertNotIn("sk-secret-value", request_text)
            self.assertNotIn("plain-secret", json.dumps(manifest))
            self.assertIn("/public/home/liusiqi/data/genome.gtf", request_text)
            self.assertIn("--gtf22", request_text)
            self.assertTrue(manifest["redaction_manifest"])

    def test_replay_is_idempotent_and_conflicting_explicit_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            metadata = {
                "call_id": "fixed-call",
                "node_name": "planner",
                "call_kind": "planner_initial",
            }
            first = store.start_call([HumanMessage(content="plan this")], metadata)
            second = store.start_call([HumanMessage(content="plan this")], metadata)
            self.assertEqual(first["call_id"], second["call_id"])
            self.assertEqual(len(list(store.root.glob("*/manifest.json"))), 1)
            with self.assertRaises(SnapshotConflictError):
                store.start_call([HumanMessage(content="different prompt")], metadata)

    def test_shared_agent_wrapper_snapshots_the_actual_final_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            agent.llm = _FakeLLM("generated action")
            agent.llm_timeout_seconds = 5
            agent.llm_readonly_timeout_seconds = 5
            agent._latest_llm_call_refs = {}
            messages = [SystemMessage(content="final system"), HumanMessage(content="final user")]
            response = asyncio.run(agent._ainvoke_llm(
                messages,
                stage="generate",
                task_id="task-a",
                task_mode="managed",
                trace_metadata={"call_kind": "generate", "step_id": "step-1"},
            ))
            self.assertEqual(response.content, "generated action")
            snapshots = ContextLineageStore(tmpdir, "task-a").list_snapshots()
            self.assertEqual(len(snapshots), 1)
            request = json.loads(Path(snapshots[0]["request_payload_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(request["messages"], [
                {"role": "system", "content": "final system"},
                {"role": "human", "content": "final user"},
            ])
            self.assertEqual(snapshots[0]["status"], "completed")
            self.assertEqual(agent._latest_llm_call_refs["task-a"][0]["call_kind"], "generate")

    def test_task_router_uses_the_same_snapshot_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = _FakeLLM(json.dumps({
                "schema_version": 1,
                "request_kind": "execute",
                "execution_scope": "managed",
                "managed_intent_explicit": True,
                "retrieval_mode": "kb",
                "confidence": 0.9,
                "reason": "workflow",
            }))
            decision = asyncio.run(classify_task_mode(
                llm,
                "run analysis",
                data_path=tmpdir,
                task_id="task-a",
            ))
            self.assertEqual(decision["task_mode"], "managed")
            snapshot = ContextLineageStore(tmpdir, "task-a").list_snapshots()[0]
            self.assertEqual(snapshot["call_kind"], "task_router")


class FinalizeArtifactTests(unittest.TestCase):
    def _request_hash(self, *, messages=None, **changes):
        values = {
            "finalize_context_id": "context-1",
            "wire_schema_version": 2,
            "provider_id": "deepseek",
            "endpoint_id": "https://user:password@example.test/v1?api_key=secret",
            "model": "deepseek-chat",
            "temperature": 0.0,
            "structured_output_mode": "strict_schema",
            "provider_parameters": {
                "top_p": 1,
                "max_output_tokens": 4096,
                "schema_id": "ConsultantFinalizeResult.v2",
                "schema_hash": "schema-hash",
                "provider_response_options": {
                    "thinking": "disabled",
                    "extra_body_allowlist": {},
                },
            },
        }
        values.update(changes)
        return canonical_finalize_request_hash(
            messages or [
                {"role": "system", "content": "policy", "id": "ignored-system-id"},
                {"role": "human", "content": "context", "id": "ignored-human-id"},
            ],
            **values,
        )

    def test_canonical_hash_excludes_message_and_trace_metadata(self):
        first = self._request_hash()
        second = self._request_hash(messages=[
            {"role": "system", "content": "policy", "id": "different", "timestamp": "later"},
            {"role": "human", "content": "context", "id": "different", "trace_id": "trace"},
        ])
        self.assertEqual(first, second)
        self.assertNotEqual(first, self._request_hash(model="different-model"))
        self.assertNotEqual(first, self._request_hash(finalize_context_id="context-2"))
        self.assertNotEqual(first, self._request_hash(temperature=0.2))

    def test_response_hash_preserves_exact_whitespace(self):
        self.assertNotEqual(
            finalize_response_content_hash("answer"),
            finalize_response_content_hash("answer\n"),
        )

    def test_intent_response_and_protocol_outcome_are_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            intent = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(intent["provider_status"], "in_flight")
            artifact = store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response="not json\n",
            )
            self.assertEqual(artifact["provider_status"], "succeeded")
            self.assertEqual(artifact["protocol_status"], "not_validated")
            self.assertNotIn("raw_response", artifact)
            outcome = store.record_finalize_protocol_outcome(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                protocol_status="wire_invalid",
                error_code="FINALIZE_RESPONSE_NOT_JSON",
                error_owner="controller_model_protocol",
            )
            self.assertEqual(outcome["provider_status"], "succeeded")
            self.assertEqual(outcome["protocol_status"], "wire_invalid")
            replay = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(replay["decision"], "reuse_response")
            self.assertEqual(replay["raw_response"], "not json\n")
            self.assertEqual(replay["protocol_status"], "wire_invalid")
            self.assertTrue(replay["response_reused_from_checkpoint"])

    def test_protected_response_is_exact_and_diagnostic_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            raw = "DEEPSEEK_API_KEY=sk-secret-value\n" + ("x" * 30)
            artifact = store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response=raw,
            )
            loaded = store.lookup_finalize_response_artifact(attempt_id="attempt-1")
            self.assertEqual(loaded["raw_response"], raw)
            projection = store.finalize_diagnostic_projection(loaded, max_excerpt_chars=20)
            self.assertNotIn("sk-secret-value", json.dumps(projection))
            self.assertTrue(projection["response_excerpt_truncated"])
            self.assertFalse(projection["exact_content_in_projection"])
            self.assertEqual(oct(Path(artifact["response_artifact_ref"]).stat().st_mode & 0o777), "0o600")

    def test_lookup_by_canonical_hash_recovers_missing_checkpoint_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            found = store.lookup_finalize_attempt(request_hash)
            self.assertEqual(found["attempt_id"], "attempt-1")
            self.assertEqual(found["provider_status"], "in_flight")
            self.assertEqual(found["intent"]["canonical_request_hash"], request_hash)

    def test_in_flight_without_artifact_never_blindly_retries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.release_finalize_provider_lease("attempt-1")
            result = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(result["decision"], "blocked_internal_provider")
            self.assertEqual(result["error_code"], "FINALIZE_PROVIDER_OUTCOME_UNKNOWN")

    def test_only_certified_transient_failure_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.record_finalize_attempt_failure(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                provider_status="transient_failed",
                error_code="FINALIZE_PROVIDER_TRANSIENT",
                error_owner="provider",
                retry_safe=True,
            )
            result = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(result["decision"], "safe_transport_retry")
            self.assertEqual(result["response_artifact_ref"], "")

    def test_succeeded_status_without_artifact_is_integrity_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            status = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            status["provider_status"] = "succeeded"
            Path(status["attempt_status_ref"]).write_text(json.dumps(status), encoding="utf-8")
            result = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(result["error_code"], "FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            self.assertEqual(result["error_owner"], "controller_storage")

    def test_finalize_writes_are_reentrant_without_timestamp_conflicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            first_status = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            intent_path = Path(first_status["intent_ref"])
            intent_before = intent_path.read_bytes()
            second_status = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            self.assertTrue(first_status["claim_acquired"])
            self.assertFalse(second_status["claim_acquired"])
            self.assertTrue(second_status["claim_reused"])
            self.assertEqual(
                {key: value for key, value in first_status.items() if not key.startswith("claim_")},
                {key: value for key, value in second_status.items() if not key.startswith("claim_")},
            )
            self.assertEqual(intent_before, intent_path.read_bytes())

            first_artifact = store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response='{"answer": 1}',
            )
            manifest_path = Path(first_artifact["response_manifest_ref"])
            manifest_before = manifest_path.read_bytes()
            second_artifact = store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response='{"answer": 1}',
            )
            self.assertEqual(first_artifact, second_artifact)
            self.assertEqual(manifest_before, manifest_path.read_bytes())

            first_outcome = store.record_finalize_protocol_outcome(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                protocol_status="valid",
            )
            outcome_path = Path(first_outcome["protocol_outcome_ref"])
            outcome_before = outcome_path.read_bytes()
            second_outcome = store.record_finalize_protocol_outcome(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                protocol_status="valid",
            )
            self.assertEqual(first_outcome, second_outcome)
            self.assertEqual(outcome_before, outcome_path.read_bytes())

            failure_hash = self._request_hash(finalize_context_id="context-failure")
            store.begin_finalize_attempt(
                attempt_id="attempt-failure",
                call_id="call-failure",
                call_kind="initial",
                canonical_request_hash=failure_hash,
            )
            first_failure = store.record_finalize_attempt_failure(
                attempt_id="attempt-failure",
                call_id="call-failure",
                canonical_request_hash=failure_hash,
                provider_status="fatal_failed",
                error_code="FINALIZE_PROVIDER_FATAL",
                error_owner="provider",
                error_details={"kind": "input_rejected"},
            )
            failure_path = Path(first_failure["failure_artifact_ref"])
            failure_before = failure_path.read_bytes()
            second_failure = store.record_finalize_attempt_failure(
                attempt_id="attempt-failure",
                call_id="call-failure",
                canonical_request_hash=failure_hash,
                provider_status="fatal_failed",
                error_code="FINALIZE_PROVIDER_FATAL",
                error_owner="provider",
                error_details={"kind": "input_rejected"},
            )
            self.assertEqual(first_failure, second_failure)
            self.assertEqual(failure_before, failure_path.read_bytes())

    def test_response_persistence_cannot_claim_protocol_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            with self.assertRaises(ValueError):
                store.persist_finalize_response(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                    raw_response="{}",
                    protocol_status="valid",
                )

    def test_structured_object_is_separate_protected_artifact_and_replays(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            parsed = {"action": "finish", "nested": {"value": 1}}
            artifact = store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response="provider did not expose JSON text",
                structured_object=parsed,
            )
            self.assertNotIn("structured_object", artifact)
            structured_path = Path(artifact["structured_object_artifact_ref"])
            self.assertEqual(oct(structured_path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(
                oct(Path(artifact["response_artifact_ref"]).stat().st_mode & 0o777),
                "0o600",
            )
            replay = store.lookup_finalize_response_artifact(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(replay["raw_response"], "provider did not expose JSON text")
            self.assertEqual(replay["structured_object"], parsed)
            self.assertFalse(replay["structured_object_is_exact_response"])

    def test_lookup_rejects_orphan_and_identity_or_hash_corruption(self):
        mutations = {
            "attempt_id": lambda value: value.update({"attempt_id": "wrong-attempt"}),
            "call_id": lambda value: value.update({"call_id": "wrong-call"}),
            "request_hash": lambda value: value.update({"canonical_request_hash": "wrong-hash"}),
            "raw_ref": lambda value: value.update({"response_artifact_ref": "/tmp/wrong"}),
            "raw_hash": lambda value: value.update({"response_content_hash": "0" * 64}),
            "structured_hash": lambda value: value.update({"structured_object_hash": "f" * 64}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                store = ContextLineageStore(tmpdir, "task-a")
                request_hash = self._request_hash()
                store.begin_finalize_attempt(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    call_kind="initial",
                    canonical_request_hash=request_hash,
                )
                artifact = store.persist_finalize_response(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                    raw_response="raw",
                    structured_object={"action": "finish"},
                )
                manifest_path = Path(artifact["response_manifest_ref"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(
                    SnapshotConflictError,
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                ):
                    store.lookup_finalize_response_artifact(attempt_id="attempt-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            status = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            raw_path = Path(status["attempt_status_ref"]).parent / "response.raw.txt"
            raw_path.write_text("orphan", encoding="utf-8")
            raw_path.chmod(0o600)
            with self.assertRaisesRegex(
                SnapshotConflictError,
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            ):
                store.lookup_finalize_response_artifact(attempt_id="attempt-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            artifact = store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response="raw",
            )
            Path(artifact["response_artifact_ref"]).unlink()
            with self.assertRaisesRegex(
                SnapshotConflictError,
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            ):
                store.lookup_finalize_response_artifact(attempt_id="attempt-1")

    def test_safe_transport_retry_is_claimed_at_most_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.record_finalize_attempt_failure(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                provider_status="transient_failed",
                error_code="FINALIZE_PROVIDER_TRANSIENT",
                error_owner="provider",
                retry_safe=True,
            )
            store.begin_finalize_attempt(
                attempt_id="attempt-2",
                call_id="call-2",
                call_kind="transport_retry",
                canonical_request_hash=request_hash,
            )
            store.record_finalize_attempt_failure(
                attempt_id="attempt-2",
                call_id="call-2",
                canonical_request_hash=request_hash,
                provider_status="transient_failed",
                error_code="FINALIZE_PROVIDER_TRANSIENT",
                error_owner="provider",
                retry_safe=True,
            )
            result = store.reconcile_finalize_attempt(
                attempt_id="attempt-2",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(result["decision"], "blocked_internal_provider")
            self.assertTrue(result["transport_retry_exhausted"])
            self.assertFalse(result["retry_safe"])
            with self.assertRaises(SnapshotConflictError):
                store.begin_finalize_attempt(
                    attempt_id="attempt-3",
                    call_id="call-3",
                    call_kind="transport_retry",
                    canonical_request_hash=request_hash,
                )

    def test_canonical_hash_claim_is_atomic_across_competing_attempts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            barrier = threading.Barrier(2)

            def claim(index):
                barrier.wait()
                try:
                    store.begin_finalize_attempt(
                        attempt_id=f"attempt-{index}",
                        call_id=f"call-{index}",
                        call_kind="initial",
                        canonical_request_hash=request_hash,
                    )
                except SnapshotConflictError:
                    return "conflict"
                return "claimed"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, (1, 2)))
            self.assertEqual(sorted(results), ["claimed", "conflict"])
            self.assertEqual(len(store.list_finalize_attempts()), 1)

    def test_same_attempt_reentry_does_not_reacquire_provider_call_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            barrier = threading.Barrier(2)

            def claim():
                barrier.wait()
                return store.begin_finalize_attempt(
                    attempt_id="attempt-shared",
                    call_id="call-shared",
                    call_kind="initial",
                    canonical_request_hash=request_hash,
                )["claim_acquired"]

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: claim(), (1, 2)))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(len(store.list_finalize_attempts()), 1)

    def test_in_flight_unknown_reconciliation_is_durable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            status = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.release_finalize_provider_lease("attempt-1")
            first = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            marker_path = Path(status["attempt_status_ref"]).parent / "reconciliation.json"
            marker_before = marker_path.read_bytes()
            second = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            durable_status = json.loads(
                Path(status["attempt_status_ref"]).read_text(encoding="utf-8")
            )
            self.assertEqual(first, second)
            self.assertEqual(marker_before, marker_path.read_bytes())
            self.assertEqual(durable_status["provider_status"], "outcome_unknown")
            self.assertEqual(
                durable_status["error_code"],
                "FINALIZE_PROVIDER_OUTCOME_UNKNOWN",
            )

    def test_live_provider_lease_prevents_premature_unknown_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            owner = ContextLineageStore(tmpdir, "task-a")
            observer = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            status = owner.begin_finalize_attempt(
                attempt_id="attempt-live",
                call_id="call-live",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )

            in_progress = observer.reconcile_finalize_attempt(
                attempt_id="attempt-live",
                call_id="call-live",
                canonical_request_hash=request_hash,
            )
            attempt_dir = Path(status["attempt_status_ref"]).parent
            self.assertEqual(in_progress["decision"], "attempt_in_progress")
            self.assertEqual(in_progress["provider_status"], "in_flight")
            self.assertFalse((attempt_dir / "reconciliation.json").exists())

            owner.persist_finalize_response(
                attempt_id="attempt-live",
                call_id="call-live",
                canonical_request_hash=request_hash,
                raw_response='{"action_type":"finish"}',
            )
            replay = observer.reconcile_finalize_attempt(
                attempt_id="attempt-live",
                call_id="call-live",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(replay["decision"], "reuse_response")
            self.assertEqual(replay["raw_response"], '{"action_type":"finish"}')
            self.assertFalse((attempt_dir / "reconciliation.json").exists())

    def test_protocol_outcome_ignores_worker_local_replay_observation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response='{"action_type":"finish"}',
            )
            owner = store.record_finalize_protocol_outcome(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                protocol_status="valid",
                validation_summary={
                    "constraint_count": 1,
                    "response_reused_from_checkpoint": False,
                },
            )
            observer = store.record_finalize_protocol_outcome(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                protocol_status="valid",
                validation_summary={
                    "constraint_count": 1,
                    "response_reused_from_checkpoint": True,
                },
            )
            self.assertEqual(owner, observer)
            self.assertNotIn(
                "response_reused_from_checkpoint",
                owner["validation_summary"],
            )

    def test_live_provider_lease_covers_failure_record_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            owner = ContextLineageStore(tmpdir, "task-a")
            observer = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            status = owner.begin_finalize_attempt(
                attempt_id="attempt-live-failure",
                call_id="call-live-failure",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            before_commit = observer.reconcile_finalize_attempt(
                attempt_id="attempt-live-failure",
                call_id="call-live-failure",
                canonical_request_hash=request_hash,
            )
            attempt_dir = Path(status["attempt_status_ref"]).parent
            self.assertEqual(before_commit["decision"], "attempt_in_progress")
            self.assertFalse((attempt_dir / "reconciliation.json").exists())

            owner.record_finalize_attempt_failure(
                attempt_id="attempt-live-failure",
                call_id="call-live-failure",
                canonical_request_hash=request_hash,
                provider_status="transient_failed",
                error_code="FINALIZE_PROVIDER_TRANSIENT",
                error_owner="provider",
                retry_safe=True,
            )
            after_commit = observer.reconcile_finalize_attempt(
                attempt_id="attempt-live-failure",
                call_id="call-live-failure",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(after_commit["decision"], "safe_transport_retry")
            self.assertFalse((attempt_dir / "reconciliation.json").exists())

    def test_provider_success_storage_failure_is_not_provider_failure_or_retryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            marker = record_finalize_response_persistence_failure(
                store,
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                response_content_hash=finalize_response_content_hash("returned"),
                error_details={"operation": "response artifact commit"},
            )
            replay = store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            self.assertEqual(marker["provider_status"], "succeeded")
            self.assertEqual(marker["protocol_status"], "not_validated")
            self.assertEqual(marker["error_owner"], "controller_storage")
            self.assertFalse(marker["retry_safe"])
            self.assertEqual(replay["decision"], "blocked_internal_contract")
            self.assertEqual(replay["provider_status"], "succeeded")
            self.assertEqual(
                replay["error_code"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertFalse((
                Path(marker["response_persistence_failure_ref"]).parent
                / "failed_attempt.json"
            ).exists())
            with self.assertRaises(SnapshotConflictError):
                store.begin_finalize_attempt(
                    attempt_id="attempt-2",
                    call_id="call-2",
                    call_kind="initial",
                    canonical_request_hash=request_hash,
                )

    def test_terminal_records_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.record_finalize_attempt_failure(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                provider_status="transient_failed",
                error_code="FINALIZE_PROVIDER_TRANSIENT",
                error_owner="provider",
                retry_safe=True,
            )
            with self.assertRaises(SnapshotConflictError):
                store.record_finalize_response_persistence_failure(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            begin = store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            raw_path = Path(begin["attempt_status_ref"]).parent / "response.raw.txt"
            raw_path.write_text("partial", encoding="utf-8")
            raw_path.chmod(0o600)
            with self.assertRaises(SnapshotConflictError):
                store.record_finalize_response_persistence_failure(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.release_finalize_provider_lease("attempt-1")
            store.reconcile_finalize_attempt(
                attempt_id="attempt-1",
                canonical_request_hash=request_hash,
            )
            with self.assertRaises(SnapshotConflictError):
                store.record_finalize_attempt_failure(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                    provider_status="transient_failed",
                    error_code="FINALIZE_PROVIDER_TRANSIENT",
                    error_owner="provider",
                    retry_safe=True,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.record_finalize_response_persistence_failure(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
            )
            with self.assertRaises(SnapshotConflictError):
                store.persist_finalize_response(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                    raw_response="returned",
                )
            with self.assertRaises(SnapshotConflictError):
                store.record_finalize_attempt_failure(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                    provider_status="fatal_failed",
                    error_code="FINALIZE_PROVIDER_FATAL",
                    error_owner="provider",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            store.begin_finalize_attempt(
                attempt_id="attempt-1",
                call_id="call-1",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.persist_finalize_response(
                attempt_id="attempt-1",
                call_id="call-1",
                canonical_request_hash=request_hash,
                raw_response="returned",
            )
            with self.assertRaises(SnapshotConflictError):
                store.record_finalize_response_persistence_failure(
                    attempt_id="attempt-1",
                    call_id="call-1",
                    canonical_request_hash=request_hash,
                )

    def test_idempotent_reentry_repairs_stale_status_projection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            begin = store.begin_finalize_attempt(
                attempt_id="attempt-response",
                call_id="call-response",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.persist_finalize_response(
                attempt_id="attempt-response",
                call_id="call-response",
                canonical_request_hash=request_hash,
                raw_response="{}",
            )
            status_path = Path(begin["attempt_status_ref"])
            stale = json.loads(status_path.read_text(encoding="utf-8"))
            stale.update({
                "provider_status": "in_flight",
                "response_content_hash": "",
                "response_artifact_ref": "",
                "response_manifest_ref": "",
            })
            status_path.write_text(json.dumps(stale), encoding="utf-8")
            store.persist_finalize_response(
                attempt_id="attempt-response",
                call_id="call-response",
                canonical_request_hash=request_hash,
                raw_response="{}",
            )
            repaired = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["provider_status"], "succeeded")
            self.assertTrue(repaired["response_artifact_ref"])

            store.record_finalize_protocol_outcome(
                attempt_id="attempt-response",
                call_id="call-response",
                canonical_request_hash=request_hash,
                protocol_status="valid",
            )
            stale = json.loads(status_path.read_text(encoding="utf-8"))
            stale["protocol_status"] = "not_validated"
            stale.pop("protocol_outcome_ref", None)
            status_path.write_text(json.dumps(stale), encoding="utf-8")
            store.record_finalize_protocol_outcome(
                attempt_id="attempt-response",
                call_id="call-response",
                canonical_request_hash=request_hash,
                protocol_status="valid",
            )
            repaired = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["protocol_status"], "valid")
            self.assertTrue(repaired["protocol_outcome_ref"])

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            begin = store.begin_finalize_attempt(
                attempt_id="attempt-failure",
                call_id="call-failure",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            store.record_finalize_attempt_failure(
                attempt_id="attempt-failure",
                call_id="call-failure",
                canonical_request_hash=request_hash,
                provider_status="fatal_failed",
                error_code="FINALIZE_PROVIDER_FATAL",
                error_owner="provider",
            )
            status_path = Path(begin["attempt_status_ref"])
            stale = json.loads(status_path.read_text(encoding="utf-8"))
            stale["provider_status"] = "in_flight"
            status_path.write_text(json.dumps(stale), encoding="utf-8")
            store.record_finalize_attempt_failure(
                attempt_id="attempt-failure",
                call_id="call-failure",
                canonical_request_hash=request_hash,
                provider_status="fatal_failed",
                error_code="FINALIZE_PROVIDER_FATAL",
                error_owner="provider",
            )
            repaired = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["provider_status"], "fatal_failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            request_hash = self._request_hash()
            begin = store.begin_finalize_attempt(
                attempt_id="attempt-storage",
                call_id="call-storage",
                call_kind="initial",
                canonical_request_hash=request_hash,
            )
            kwargs = {
                "attempt_id": "attempt-storage",
                "call_id": "call-storage",
                "canonical_request_hash": request_hash,
                "error_details": {"operation": "commit"},
            }
            store.record_finalize_response_persistence_failure(**kwargs)
            status_path = Path(begin["attempt_status_ref"])
            stale = json.loads(status_path.read_text(encoding="utf-8"))
            stale["provider_status"] = "in_flight"
            status_path.write_text(json.dumps(stale), encoding="utf-8")
            store.record_finalize_response_persistence_failure(**kwargs)
            repaired = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["provider_status"], "succeeded")
            self.assertEqual(repaired["error_owner"], "controller_storage")


class DecisionLineageTests(unittest.TestCase):
    def _record_call(self, store, kind, request, response, **metadata):
        snapshot = store.start_call(
            [HumanMessage(content=request)],
            {"node_name": kind, "call_kind": kind, **metadata},
        )
        return store.finish_call(snapshot["call_id"], AIMessage(content=response))

    def test_bundle_proves_wrong_skill_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            self._record_call(
                store,
                "consultant",
                "User asks for compartment calling. Candidates: tad-calling, compartment-calling",
                '{"selected_skills":["genomics/tad-calling"]}',
                selected_skill_ids=["genomics/tad-calling"],
                candidate_skill_ids=["genomics/tad-calling", "genomics/compartment-calling"],
                user_source_refs=[{"text": "请做区室鉴定", "message_id": "u1"}],
            )
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            consult = bundle["segments"]["task_user_consult"]["calls"][0]
            self.assertIn("compartment calling", consult["request"]["messages"][0]["content"])
            self.assertEqual(consult["manifest"]["selected_skill_ids"], ["genomics/tad-calling"])
            self.assertIn("genomics/compartment-calling", consult["manifest"]["candidate_skill_ids"])

    def test_bundle_keeps_planner_initial_and_repair_calls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            self._record_call(store, "planner_initial", "constraint: do not overwrite", "initial plan")
            self._record_call(store, "planner_repair", "repair while preserving constraint", "repaired plan")
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            calls = bundle["segments"]["planner"]["calls"]
            self.assertEqual([row["manifest"]["call_kind"] for row in calls], ["planner_initial", "planner_repair"])
            self.assertIn("do not overwrite", calls[0]["request"]["messages"][0]["content"])

    def test_bundle_records_generated_and_executed_script_difference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generated = root / "generated.sh"
            executed = root / "executed.sh"
            generated.write_text("echo generated\n", encoding="utf-8")
            executed.write_text("echo executed\n", encoding="utf-8")
            plan = build_plan_state(
                "task-a",
                f"- [>] Step 1: run | Action: EXECUTE | Outputs: [{root / 'out.tsv'}] | Done: output exists",
            )
            action = record_provenance_event(plan, "action_bound", {"action": {
                "action_id": "action-a",
                "step_id": "step-1",
                "type": "execute",
                "generated_script_path": str(generated),
            }})
            attempt = record_provenance_event(plan, "attempt_started", {
                "action_id": action["action_id"],
                "executed_script_path": str(executed),
            })
            save_plan_state(tmpdir, "task-a", plan)
            RecoveryStateStore(tmpdir, "task-a").save_execution_record(ExecutionRecord.from_mapping({
                "execution_id": "exec-a",
                "attempt_id": attempt["attempt_id"],
                "action_id": action["action_id"],
                "step_id": "step-1",
                "script_path": str(executed),
                "code": executed.read_text(encoding="utf-8"),
                "exit_code": 0,
                "execution_status": "succeeded",
            }))
            bundle = build_decision_lineage_bundle(tmpdir, "task-a", "step-1", attempt["attempt_id"])
            scripts = bundle["execution_lineage"]["scripts"]
            self.assertNotEqual(scripts[0]["generated_hash"], scripts[0]["executed_hash"])
            self.assertIn("-echo generated", scripts[0]["diff"])
            self.assertIn("+echo executed", scripts[0]["diff"])

    def test_renderer_lineage_does_not_require_generate_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"schema_version": 3, "actions": []}), encoding="utf-8")
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: renderer composite | Action: EXECUTE | Done: manifest actions complete",
            )
            provenance = plan["provenance_state"]
            provenance["manifests"]["manifest-a"] = {
                "manifest_id": "manifest-a",
                "manifest_path": str(manifest_path),
                "renderer_id": "renderer/example",
                "plan_step_id": "step-1",
            }
            save_plan_state(tmpdir, "task-a", plan)
            ContextLineageStore(tmpdir, "task-a").record_event(
                "renderer_invocation",
                {
                    "step_id": "step-1",
                    "renderer_id": "renderer/example",
                    "manifest_path": str(manifest_path),
                },
            )
            bundle = build_decision_lineage_bundle(tmpdir, "task-a", "step-1")
            self.assertEqual(bundle["segments"]["generate_renderer"]["calls"], [])
            self.assertEqual(bundle["renderer_lineage"]["manifests"][0]["renderer_id"], "renderer/example")
            self.assertEqual(bundle["trace_status"], "available")

    def test_segment_plan_references_whole_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextLineageStore(tmpdir, "task-a")
            self._record_call(store, "consultant", "consult prompt", "consult output")
            self._record_call(store, "planner_initial", "planner prompt", "planner output")
            self._record_call(store, "generate", "generate prompt", "generate output")
            first_event = store.record_event(
                "controller_route",
                {"from_node": "consultant", "to_node": "planner", "step_id": "step-1"},
            )
            second_event = store.record_event(
                "controller_route",
                {"from_node": "consultant", "to_node": "planner", "step_id": "step-1"},
            )
            bundle = build_decision_lineage_bundle(tmpdir, "task-a")
            self.assertEqual(first_event["event_id"], second_event["event_id"])
            self.assertEqual(len(bundle["controller_lineage"]), 1)
            segment_plan = bundle["segment_plan"]
            self.assertEqual([row["segment"] for row in segment_plan], [
                "task_user_consult", "planner", "generate_renderer", "execute_validate",
            ])
            for row in segment_plan:
                self.assertIn("snapshot_refs", row)
                self.assertIn("hash", row)


if __name__ == "__main__":
    unittest.main()
