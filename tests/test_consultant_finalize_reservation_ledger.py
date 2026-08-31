import json
import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_core.context_lineage import ContextLineageStore, SnapshotConflictError


class FinalizeReservationLedgerTests(unittest.TestCase):
    task_id = "task-reservation-ledger"
    context_id = "finalize-context-1"
    replay_scope_id = "root-lineage-1"

    def _store(self, root: str) -> ContextLineageStore:
        return ContextLineageStore(root, self.task_id)

    def _budget(
        self,
        store: ContextLineageStore,
        *,
        budget_id: str = "budget-1",
        max_provider_calls: int = 8,
        max_tokens: int = 20_000,
        max_cost: int = 20_000,
        max_wall: int = 120_000,
        max_items: int = 100,
        max_batches: int = 20,
        max_depth: int = 8,
        timeout: int = 10_000,
    ):
        started = datetime.now(timezone.utc)
        return store.persist_finalize_work_budget({
            "work_budget_id": budget_id,
            "finalize_context_id": self.context_id,
            "replay_scope_id": self.replay_scope_id,
            "budget_policy_id": "finite-policy-v1",
            "max_resolution_items": max_items,
            "max_created_batches": max_batches,
            "max_provider_calls": max_provider_calls,
            "max_repartition_depth": max_depth,
            "max_total_completion_tokens": max_tokens,
            "max_total_estimated_cost_microusd": max_cost,
            "max_wall_time_ms": max_wall,
            "run_started_at": started.isoformat(),
            "absolute_deadline_at": (
                started + timedelta(milliseconds=max_wall)
            ).isoformat(),
            "per_call_timeout_ms": timeout,
        })

    def _hold(
        self,
        store: ContextLineageStore,
        budget,
        *,
        suffix: str,
        wire: str = "wire-1",
        protocol: str = "protocol-1",
        retry_ref=None,
        retry_hash=None,
        completion_tokens: int = 1_000,
        cost: int = 100,
        wall: int = 10_000,
        timeout: int = 10_000,
        items: int = 1,
        batches: int = 1,
        depth: int = 0,
        lease_owner: str = "worker-a",
    ):
        return store.hold_finalize_budget_reservation(
            replay_scope_id=self.replay_scope_id,
            finalize_context_id=self.context_id,
            attempt_id=f"attempt-{suffix}",
            call_id=f"call-{suffix}",
            protocol_request_hash=protocol,
            wire_request_hash=wire,
            work_budget_ref=budget["work_budget_ref"],
            work_budget_hash=budget["work_budget_hash"],
            reserved_completion_tokens=completion_tokens,
            reserved_cost_microusd=cost,
            reserved_wall_time_ms=wall,
            timeout_ms=timeout,
            batch_id=f"batch-{suffix}",
            request_intent_hash=f"intent-{suffix}",
            lease_owner=lease_owner,
            retry_authorization_ref=retry_ref,
            retry_authorization_hash=retry_hash,
            resolution_item_count=items,
            created_batch_count=batches,
            repartition_depth=depth,
        )

    @staticmethod
    def _protected_evidence(root: str, name: str = "response.json"):
        path = Path(root) / name
        payload = b'{"response":"protected"}\n'
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        return str(path), hashlib.sha256(payload).hexdigest()

    def test_work_budget_and_generic_artifact_are_immutable_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store)
            self.assertEqual(
                store.lookup_finalize_work_budget(
                    work_budget_hash=budget["work_budget_hash"]
                ),
                budget,
            )
            self.assertTrue(budget["protected_artifact_hash"])
            # Re-entry cannot reset the run clock or counters: an equivalent
            # policy for the same replay scope resolves to the original budget.
            self.assertEqual(self._budget(store, budget_id="budget-2"), budget)
            with self.assertRaises(SnapshotConflictError):
                self._budget(
                    store,
                    budget_id="budget-conflict",
                    max_provider_calls=9,
                )
            with self.assertRaises(ValueError):
                store.persist_finalize_work_budget({
                    **{
                        key: value
                        for key, value in budget.items()
                        if key not in {
                            "work_budget_id",
                            "work_budget_hash",
                            "work_budget_ref",
                            "protected_artifact_hash",
                        }
                    },
                    "work_budget_id": "invalid-float",
                    "max_provider_calls": 1.5,
                })

            catalog = store.persist_finalize_controller_artifact(
                "candidate_catalog",
                {
                    "candidate_catalog_id": "catalog-1",
                    "candidate_catalog_hash": "domain-catalog-hash",
                    "finalize_context_id": self.context_id,
                    "ordered_resolution_ids": ["resolution-1"],
                },
            )
            self.assertEqual(catalog["candidate_catalog_hash"], "domain-catalog-hash")
            self.assertTrue(catalog["protected_artifact_hash"])
            self.assertEqual(
                store.lookup_finalize_controller_artifact(
                    "candidate_catalog",
                    artifact_hash="domain-catalog-hash",
                ),
                catalog,
            )
            path = Path(catalog["candidate_catalog_ref"])
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["ordered_resolution_ids"] = ["forged"]
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(SnapshotConflictError):
                store.lookup_finalize_controller_artifact(
                    "candidate_catalog", artifact_id="catalog-1"
                )

    def test_hold_is_atomic_unique_and_idempotent_across_store_instances(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store)
            first = self._hold(store, budget, suffix="same")
            replay = self._hold(self._store(root), budget, suffix="same")
            self.assertEqual(replay, first)

            def competing(suffix):
                contender = self._store(root)
                try:
                    return self._hold(contender, budget, suffix=suffix)
                except SnapshotConflictError as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(competing, ("other-a", "other-b")))
            self.assertTrue(all(isinstance(value, SnapshotConflictError) for value in results))
            active = store.list_finalize_budget_reservations(status="held")
            self.assertEqual([row["reservation_id"] for row in active], [first["reservation_id"]])

    def test_stable_identity_hash_and_cas_record_hash_across_transitions(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store)
            held = self._hold(store, budget, suffix="stable")
            dispatched = store.dispatch_finalize_budget_reservation(
                held["reservation_id"],
                expected_reservation_hash=held["reservation_hash"],
                expected_protected_record_hash=held["protected_record_hash"],
                lease_owner="worker-a",
                lease_epoch=held["lease_epoch"],
            )
            self.assertEqual(dispatched["reservation_hash"], held["reservation_hash"])
            self.assertNotEqual(
                dispatched["protected_record_hash"], held["protected_record_hash"]
            )
            self.assertEqual(dispatched["record_revision"], 2)
            self.assertEqual(
                store.dispatch_finalize_budget_reservation(
                    held["reservation_id"],
                    expected_reservation_hash=held["reservation_hash"],
                    expected_protected_record_hash=held["protected_record_hash"],
                    lease_owner="worker-a",
                    lease_epoch=held["lease_epoch"],
                ),
                dispatched,
            )
            with self.assertRaises(SnapshotConflictError):
                store.release_finalize_budget_reservation(
                    held["reservation_id"],
                    dispatch_never_began=True,
                    release_reason="too late",
                )

    def test_released_never_dispatched_counts_zero_and_allows_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store, max_provider_calls=1)
            held = self._hold(store, budget, suffix="release")
            released = store.release_finalize_budget_reservation(
                held["reservation_id"],
                dispatch_never_began=True,
                release_reason="controller cancelled before adapter",
                expected_protected_record_hash=held["protected_record_hash"],
            )
            self.assertEqual(released["status"], "released")
            replacement = self._hold(store, budget, suffix="replacement")
            self.assertEqual(replacement["lease_epoch"], 2)
            aggregate = store.aggregate_finalize_budget_reservations(
                work_budget_ref=budget["work_budget_ref"],
                work_budget_hash=budget["work_budget_hash"],
                finalize_context_id=self.context_id,
            )
            self.assertEqual(aggregate["totals"]["provider_calls"], 1)
            self.assertTrue(Path(aggregate["reservation_ledger_ref"]).is_file())

    def test_complete_truncated_and_unknown_history_block_same_wire(self):
        for index, (provider, integrity) in enumerate((
            ("succeeded", "complete"),
            ("succeeded", "truncated"),
            ("succeeded", "unknown"),
            ("outcome_unknown", "unknown"),
        )):
            with self.subTest(integrity=integrity), tempfile.TemporaryDirectory() as root:
                store = self._store(root)
                budget = self._budget(store)
                held = self._hold(store, budget, suffix=f"terminal-{index}")
                dispatched = store.dispatch_finalize_budget_reservation(
                    held["reservation_id"]
                )
                artifact_ref = artifact_hash = ""
                if provider == "succeeded":
                    artifact_ref, artifact_hash = self._protected_evidence(root)
                store.consume_finalize_budget_reservation(
                    held["reservation_id"],
                    provider_status=provider,
                    completion_integrity=integrity,
                    reconciled_artifact_ref=artifact_ref,
                    reconciled_artifact_hash=artifact_hash,
                    expected_protected_record_hash=dispatched["protected_record_hash"],
                )
                with self.assertRaises(SnapshotConflictError):
                    self._hold(
                        store,
                        budget,
                        suffix=f"blind-retry-{index}",
                        wire="wire-1",
                    )

    def test_transient_missing_requires_single_use_durable_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store)
            held = self._hold(store, budget, suffix="first")
            dispatched = store.dispatch_finalize_budget_reservation(held["reservation_id"])
            failed = store.consume_finalize_budget_reservation(
                held["reservation_id"],
                provider_status="transient_failed",
                completion_integrity="missing",
                retry_safe=True,
                expected_protected_record_hash=dispatched["protected_record_hash"],
            )
            with self.assertRaises(SnapshotConflictError):
                self._hold(store, budget, suffix="unapproved")

            authorization = store.persist_finalize_retry_authorization({
                "retry_authorization_id": "retry-auth-1",
                "finalize_context_id": self.context_id,
                "replay_scope_id": self.replay_scope_id,
                "wire_request_hash": "wire-1",
                "prior_reservation_id": failed["reservation_id"],
                "prior_reservation_hash": failed["reservation_hash"],
                "provider_status": "transient_failed",
                "completion_integrity": "missing",
                "retry_safe": True,
                "no_completion_proven": True,
            })
            retry = self._hold(
                store,
                budget,
                suffix="approved",
                retry_ref=authorization["retry_authorization_ref"],
                retry_hash=authorization["retry_authorization_hash"],
            )
            self.assertNotEqual(retry["attempt_lineage_hash"], failed["attempt_lineage_hash"])
            self.assertEqual(retry["retry_authorization_hash"], authorization["retry_authorization_hash"])

    def test_actual_usage_replaces_reservation_and_overage_blocks_later_hold(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store, max_tokens=1_000, max_provider_calls=3)
            first = self._hold(
                store,
                budget,
                suffix="actual-one",
                wire="wire-actual-one",
                completion_tokens=800,
            )
            dispatched = store.dispatch_finalize_budget_reservation(first["reservation_id"])
            artifact_ref, artifact_hash = self._protected_evidence(root, "actual-one.json")
            store.consume_finalize_budget_reservation(
                first["reservation_id"],
                provider_status="succeeded",
                completion_integrity="complete",
                actual_completion_tokens=100,
                actual_cost_microusd=0,
                actual_wall_time_ms=1,
                reconciled_artifact_ref=artifact_ref,
                reconciled_artifact_hash=artifact_hash,
                expected_protected_record_hash=dispatched["protected_record_hash"],
            )
            second = self._hold(
                store,
                budget,
                suffix="actual-two",
                wire="wire-actual-two",
                completion_tokens=800,
            )
            dispatched_second = store.dispatch_finalize_budget_reservation(
                second["reservation_id"]
            )
            artifact_ref, artifact_hash = self._protected_evidence(root, "actual-two.json")
            store.consume_finalize_budget_reservation(
                second["reservation_id"],
                provider_status="succeeded",
                completion_integrity="complete",
                actual_completion_tokens=1_100,
                reconciled_artifact_ref=artifact_ref,
                reconciled_artifact_hash=artifact_hash,
                expected_protected_record_hash=dispatched_second["protected_record_hash"],
            )
            aggregate = store.aggregate_finalize_budget_reservations(
                work_budget_ref=budget["work_budget_ref"],
                work_budget_hash=budget["work_budget_hash"],
                finalize_context_id=self.context_id,
            )
            self.assertTrue(aggregate["budget_exceeded"])
            self.assertIn("completion_tokens", aggregate["exceeded_fields"])
            with self.assertRaises(SnapshotConflictError):
                self._hold(
                    store,
                    budget,
                    suffix="blocked-overage",
                    wire="wire-new",
                    completion_tokens=1,
                )

    def test_reconcile_required_is_monotonic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store)
            held = self._hold(store, budget, suffix="reconcile")
            dispatched = store.dispatch_finalize_budget_reservation(held["reservation_id"])
            pending = store.mark_finalize_reservation_reconcile_required(
                held["reservation_id"],
                reason="provider outcome could not be proven",
                expected_protected_record_hash=dispatched["protected_record_hash"],
            )
            consumed = store.reconcile_finalize_budget_reservation(
                held["reservation_id"],
                provider_status="outcome_unknown",
                completion_integrity="unknown",
                expected_protected_record_hash=pending["protected_record_hash"],
            )
            replay = store.reconcile_finalize_budget_reservation(
                held["reservation_id"],
                provider_status="outcome_unknown",
                completion_integrity="unknown",
                expected_protected_record_hash=pending["protected_record_hash"],
            )
            self.assertEqual(replay, consumed)
            with self.assertRaises(SnapshotConflictError):
                store.reconcile_finalize_budget_reservation(
                    held["reservation_id"],
                    provider_status="outcome_unknown",
                    completion_integrity="unknown",
                    actual_completion_tokens=0,
                )

    def test_tampered_ledger_generation_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            budget = self._budget(store)
            held = self._hold(store, budget, suffix="tamper")
            path = Path(held["reservation_ref"])
            value = json.loads(path.read_text(encoding="utf-8"))
            value["reserved_completion_tokens"] = 0
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(SnapshotConflictError):
                store.lookup_finalize_budget_reservation(held["reservation_id"])
            with self.assertRaises(SnapshotConflictError):
                store.aggregate_finalize_budget_reservations(
                    work_budget_ref=budget["work_budget_ref"],
                    work_budget_hash=budget["work_budget_hash"],
                    finalize_context_id=self.context_id,
                )


if __name__ == "__main__":
    unittest.main()
