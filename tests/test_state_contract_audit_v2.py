from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.state_contract_store import (
    StateContractIntegrityError,
    StateContractStore,
)
from agent_core.task_spec import create_task_spec
from scripts.audit_state_contract_consumers import build_report


class StateContractAuditStoreTests(unittest.TestCase):
    def test_consumer_inventory_has_no_authoritative_alias_reader(self) -> None:
        report = build_report()

        self.assertTrue(report["summary"]["passed"], report["violations"])
        self.assertEqual(report["summary"]["authoritative_legacy_readers"], 0)
        self.assertEqual(report["summary"]["forbidden_new_writes"], 0)

    def test_content_addressed_object_and_event_chain_verify(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = StateContractStore(root, "task-a")
            raw_ref = store.put_object("raw_user_submission", {"text": "run"})
            first = store.append_event(
                event_type="task_mutation",
                payload={"generation_before": 0, "generation_after": 1},
            )
            index = store.next_index(
                {}, event_ref=first, event_type="task_mutation", raw_submission_ref=raw_ref
            )
            second = store.append_event(
                event_type="runtime_event",
                payload={"generation_before": 1, "generation_after": 2},
                previous_event_ref=first,
            )
            index = store.next_index(index, event_ref=second, event_type="runtime_event")
            self.assertEqual(
                store.verify_chain(index, expected_generation=2)["event_count"],
                2,
            )
            self.assertEqual(
                store.read_object(raw_ref, expected_type="raw_user_submission"),
                {"text": "run"},
            )

    def test_plan_commit_keeps_only_bounded_index_and_one_taskspec(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = StateContractStore(root, "task-a")
            raw_ref = store.put_object("raw_user_submission", {"text": "run"})
            state = {
                "task_id": "task-a",
                "task_spec": create_task_spec(
                    "run", task_id="task-a", raw_submission_ref=raw_ref
                ),
                "steps": [],
                "state_generation": 0,
            }
            path = save_plan_state(root, "task-a", state)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("task_spec", persisted)
            self.assertNotIn("task_spec", persisted.get("task_state") or {})
            canonical = persisted["current_task"]["task_spec"]
            for alias in (
                "expected_outputs", "facts", "user_directives", "change_log",
                "request_envelope", "effective_user_facts",
            ):
                self.assertNotIn(alias, canonical)
            self.assertLessEqual(len(persisted["audit_index"]["latest_by_type"]), 5)
            self.assertEqual(
                store.verify_chain(
                    persisted["audit_index"],
                    expected_generation=persisted["state_generation"],
                )["event_count"],
                1,
            )

            loaded = load_plan_state(root, "task-a")
            loaded["runtime_state"]["status"] = "running"
            save_plan_state(root, "task-a", loaded)
            reloaded = load_plan_state(root, "task-a")
            self.assertEqual(
                store.verify_chain(
                    reloaded["audit_index"],
                    expected_generation=reloaded["state_generation"],
                )["event_count"],
                2,
            )

    def test_legacy_task_history_is_externalized_before_alias_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            legacy = {
                "schema_version": 4,
                "task_id": "task-legacy",
                "revision": 3,
                "goal": "annotate variants",
                "request_envelope": {"request_text": "raw legacy request"},
                "requirements": [],
                "inputs": [],
                "expected_outputs": [{"filename": "annotated.vcf"}],
                "output_contracts": [{"filename": "annotated.vcf"}],
                "change_log": [{"revision": 2, "reason": "user correction"}],
                "user_directives": [{"text": "keep SNPs only"}],
            }
            path = save_plan_state(
                root,
                "task-legacy",
                {
                    "task_id": "task-legacy",
                    "task_spec": legacy,
                    "steps": [],
                    "state_generation": 0,
                },
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            canonical = persisted["current_task"]["task_spec"]
            self.assertNotIn("change_log", canonical)
            self.assertNotIn("user_directives", canonical)
            self.assertNotIn("expected_outputs", canonical)
            store = StateContractStore(root, "task-legacy")
            migration_refs = [
                ref for ref in canonical["source_refs"]
                if ref.startswith("object:sha256:")
                and ref != canonical.get("raw_submission_ref")
            ]
            self.assertEqual(len(migration_refs), 1)
            migration = store.read_object(
                migration_refs[0], expected_type="legacy_task_spec_migration"
            )
            migrated = migration["legacy_records"]["task_spec"]
            self.assertEqual(migrated["change_log"][0]["revision"], 2)
            self.assertEqual(migrated["request_envelope_ref"], canonical["raw_submission_ref"])

    def test_generation_chain_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = StateContractStore(root, "task-a")
            first = store.append_event(
                event_type="state_created",
                payload={"generation_before": 0, "generation_after": 1},
            )
            second = store.append_event(
                event_type="runtime_event",
                payload={"generation_before": 7, "generation_after": 8},
                previous_event_ref=first,
            )
            index = store.next_index(
                store.next_index({}, event_ref=first, event_type="state_created"),
                event_ref=second,
                event_type="runtime_event",
            )
            with self.assertRaises(StateContractIntegrityError):
                store.verify_chain(index)

    def test_tampered_event_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = StateContractStore(root, "task-a")
            event_ref = store.append_event(event_type="state_created", payload={})
            index = store.next_index({}, event_ref=event_ref, event_type="state_created")
            digest = event_ref.rsplit(":", 1)[-1]
            path = Path(root) / "task_outputs" / "task-a" / "state_contract" / "audit_events" / f"{digest}.json"
            path.write_text('{"tampered":true}', encoding="utf-8")
            with self.assertRaises(StateContractIntegrityError):
                store.verify_chain(index)


if __name__ == "__main__":
    unittest.main()
