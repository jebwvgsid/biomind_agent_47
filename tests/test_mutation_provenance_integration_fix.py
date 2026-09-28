"""Focused regressions for Execute mutation-intent provenance registration.

The module deliberately keeps the real workspace journal, PlanTracker reducer,
prelaunch validators, HITL checkpoint, TypedExecute adapter, and durable state
store.  Only provider responses and the external process launcher are replaced.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

# These guards must be installed before importing any repository module.  The
# acceptance command additionally starts from an env-i process.
os.environ["BIOMIND_DISABLE_DOTENV"] = "1"
os.environ["LOG_LEVEL"] = "CRITICAL"
sys.dont_write_bytecode = True

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent_core.plan_tracker import (
    load_plan_state,
    record_provenance_event,
    save_plan_state,
)
from agent_core.workspace_attestation import (
    WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
    WorkspaceAttestationError,
    finalize_mutation_attestation,
    load_mutation_journal,
    persist_mutation_journal,
    prepare_mutation_intents,
    start_mutation_attestation,
    validate_runtime_mutation_receipt,
    validate_workspace_action_admission,
)
from tests import test_generate_session_1788681718_production_path as generate_fixture
from tests.test_workspace_binding_attestation import (
    _durable_workspace_state,
    _valid_action,
)


class MutationProvenanceReducerIntegrationTests(unittest.TestCase):
    """Exercise the real journal loader and PlanTracker mutation reducer."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="biomind-mutation-provenance-reducer-"
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _case(
        self,
        *,
        suffix: str,
        with_mutation: bool = True,
    ) -> dict[str, Any]:
        task_id = f"task-mutation-registration-{suffix}"
        case_root = self.root / suffix
        data_path = case_root / "durable"
        state, step, capability, _binding, rows = _durable_workspace_state(
            case_root,
            task_id=task_id,
        )
        provenance = state["provenance_state"]
        provenance["actions"] = {}
        provenance["attempts"] = {}
        provenance["attempt_counters"] = {}

        action_digest = "sha256:" + (suffix.encode("utf-8").hex() + "0" * 64)[:64]
        action = {
            **_valid_action(rows),
            "action_id": f"action-{suffix}",
            "action_hash": action_digest,
            "step_id": step["step_id"],
            "plan_id": state["plan_id"],
            "plan_version": state["plan_version"],
            "command_role": "analysis_execution",
        }
        state["task_state"]["pending_action"] = deepcopy(action)
        record_provenance_event(state, "action_bound", {"action": action})
        attempt = record_provenance_event(
            state,
            "attempt_created",
            {"action_id": action["action_id"]},
        )
        save_plan_state(
            data_path,
            task_id,
            state,
            allow_canonical_task_write=True,
            step_effect_capability_registry={
                capability["capability_id"]: capability
            },
        )

        effective_capability = deepcopy(capability)
        if not with_mutation:
            effective_capability["workspace_mutations"] = []
        admission = validate_workspace_action_admission(
            action,
            capability_contract=effective_capability,
            workspace_bindings=rows,
        )
        self.assertEqual(
            admission.status,
            "passed" if with_mutation else "not_applicable",
        )
        intents = prepare_mutation_intents(
            admission,
            task_id=task_id,
            plan_id=state["plan_id"],
            plan_revision=state["plan_revision"],
            step_id=step["step_id"],
            action_id=action["action_id"],
            action_digest=action_digest,
            action=action,
        )
        journal_path = (
            data_path
            / "task_outputs"
            / task_id
            / "attempts"
            / attempt["attempt_id"]
            / "mutation_intent.json"
        )
        persist_mutation_journal(
            journal_path,
            start_mutation_attestation(
                intents,
                attempt_id=attempt["attempt_id"],
            ),
        )
        journal = load_mutation_journal(journal_path)
        payload = {
            "attempt_id": attempt["attempt_id"],
            "journal_path": str(journal_path.resolve()),
            "journal_digest": journal["journal_digest"],
            "intent_ids": [
                item["intent_id"] for item in journal.get("intents", [])
            ],
        }
        return {
            "task_id": task_id,
            "data_path": data_path,
            "state": state,
            "step": step,
            "capability": capability,
            "rows": rows,
            "action": action,
            "attempt": attempt,
            "intents": intents,
            "journal_path": journal_path,
            "journal": journal,
            "payload": payload,
        }

    def test_journal_write_survives_registration_commit_gap_and_replays_once(
        self,
    ) -> None:
        case = self._case(suffix="commit-gap")

        # The action and attempt are already durable here.  This deliberately
        # models only the narrower registration-commit gap: reducer success
        # existed in memory, its state commit was lost, and the real journal
        # remained available for recovery replay.
        transient = load_plan_state(case["data_path"], case["task_id"])
        transient_result = record_provenance_event(
            transient,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            case["payload"],
        )
        self.assertEqual(
            transient_result["journal_path"],
            str(case["journal_path"].resolve()),
        )
        self.assertNotIn(
            "workspace_mutation_intent_registration",
            load_plan_state(case["data_path"], case["task_id"])[
                "provenance_state"
            ]["attempts"][case["attempt"]["attempt_id"]],
        )

        recovered = load_plan_state(case["data_path"], case["task_id"])
        replayed = record_provenance_event(
            recovered,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            case["payload"],
        )
        save_plan_state(case["data_path"], case["task_id"], recovered)
        durable = load_plan_state(case["data_path"], case["task_id"])
        registration = durable["provenance_state"]["attempts"][
            case["attempt"]["attempt_id"]
        ]["workspace_mutation_intent_registration"]
        self.assertEqual(registration, replayed)
        self.assertEqual(registration["intent_ids"], case["payload"]["intent_ids"])
        self.assertTrue(registration["registration_digest"].startswith("sha256:"))

        before_duplicate = deepcopy(durable)
        duplicate = record_provenance_event(
            durable,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            case["payload"],
        )
        self.assertEqual(duplicate, registration)
        self.assertEqual(durable, before_duplicate)

    def test_orphan_journal_with_empty_durable_attempts_reconstructs_scope(
        self,
    ) -> None:
        case = self._case(suffix="attempt-allocation-commit-gap")

        # Model the earlier cross-medium window exactly: the journal survived,
        # while action/attempt allocation and registration never reached the
        # durable PlanState.  Recovery must derive the same attempt identity
        # from the existing action counter state, verify the orphan journal,
        # and register it without deleting or rewriting the file.
        before_journal_bytes = case["journal_path"].read_bytes()
        pre_attempt = load_plan_state(case["data_path"], case["task_id"])
        pre_attempt["provenance_state"]["actions"] = {}
        pre_attempt["provenance_state"]["attempts"] = {}
        pre_attempt["provenance_state"]["attempt_counters"] = {}
        save_plan_state(case["data_path"], case["task_id"], pre_attempt)
        self.assertEqual(
            load_plan_state(case["data_path"], case["task_id"])[
                "provenance_state"
            ]["attempts"],
            {},
        )

        recovered = load_plan_state(case["data_path"], case["task_id"])
        record_provenance_event(
            recovered,
            "action_bound",
            {"action": deepcopy(case["action"])},
        )
        reconstructed = record_provenance_event(
            recovered,
            "attempt_created",
            {"action_id": case["action"]["action_id"]},
        )
        self.assertEqual(
            reconstructed["attempt_id"], case["attempt"]["attempt_id"]
        )
        registration = record_provenance_event(
            recovered,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            case["payload"],
        )
        save_plan_state(case["data_path"], case["task_id"], recovered)

        durable = load_plan_state(case["data_path"], case["task_id"])
        self.assertEqual(
            durable["provenance_state"]["attempts"][
                reconstructed["attempt_id"]
            ]["workspace_mutation_intent_registration"],
            registration,
        )
        self.assertEqual(case["journal_path"].read_bytes(), before_journal_bytes)

    def test_empty_and_nonempty_journals_register_without_fake_receipts(self) -> None:
        for suffix, with_mutation, expected_status, expected_count in (
            ("empty", False, "not_applicable", 0),
            ("nonempty", True, "attempt_started", 1),
        ):
            with self.subTest(suffix=suffix):
                case = self._case(
                    suffix=suffix,
                    with_mutation=with_mutation,
                )
                state = load_plan_state(case["data_path"], case["task_id"])
                registration = record_provenance_event(
                    state,
                    WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
                    case["payload"],
                )
                self.assertEqual(registration["journal_status"], expected_status)
                self.assertEqual(len(registration["intent_ids"]), expected_count)
                self.assertEqual(case["journal"]["receipts"], [])
                self.assertEqual(
                    state["provenance_state"]["attempts"][
                        case["attempt"]["attempt_id"]
                    ]["status"],
                    "CREATED",
                )

    def test_finalized_journal_replay_is_idempotent_but_conflict_is_rejected(
        self,
    ) -> None:
        case = self._case(suffix="finalized-replay")
        state = load_plan_state(case["data_path"], case["task_id"])
        original = record_provenance_event(
            state,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            case["payload"],
        )

        formal = Path(case["rows"][0]["formal_concrete_path"])
        (formal / "data" / "genomes").mkdir(parents=True)
        (formal / "data" / "genomes" / "fixture_v1.fa").write_text(
            ">chr1\nA\n",
            encoding="utf-8",
        )
        finalized = finalize_mutation_attestation(
            case["journal"],
            process_exit_code=0,
        )
        persist_mutation_journal(case["journal_path"], finalized)
        finalized_payload = {
            **case["payload"],
            "journal_digest": finalized["journal_digest"],
        }
        before_replay = deepcopy(state)
        replay = record_provenance_event(
            state,
            WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
            finalized_payload,
        )
        self.assertEqual(replay, original)
        self.assertEqual(state, before_replay)

        conflicting_capability = deepcopy(case["capability"])
        conflicting_capability["workspace_mutations"][0]["evidence_contract"][
            "allow_idempotent_revalidation"
        ] = False
        conflicting_admission = validate_workspace_action_admission(
            case["action"],
            capability_contract=conflicting_capability,
            workspace_bindings=case["rows"],
        )
        self.assertEqual(conflicting_admission.status, "passed")
        conflicting_intents = prepare_mutation_intents(
            conflicting_admission,
            task_id=case["task_id"],
            plan_id=state["plan_id"],
            plan_revision=state["plan_revision"],
            step_id=case["step"]["step_id"],
            action_id=case["action"]["action_id"],
            action_digest=case["action"]["action_hash"],
            action=case["action"],
        )
        conflicting = start_mutation_attestation(
            conflicting_intents,
            attempt_id=case["attempt"]["attempt_id"],
        )
        persist_mutation_journal(case["journal_path"], conflicting)
        conflict_payload = {
            **case["payload"],
            "journal_digest": conflicting["journal_digest"],
            "intent_ids": [
                item["intent_id"] for item in conflicting["intents"]
            ],
        }
        before_conflict = deepcopy(state)
        with self.assertRaises(WorkspaceAttestationError) as raised:
            record_provenance_event(
                state,
                WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
                conflict_payload,
            )
        self.assertEqual(raised.exception.code, "MUTATION_INTENT_REGISTRATION_CONFLICT")
        self.assertEqual(state, before_conflict)

    def test_unknown_attempt_and_malformed_payload_do_not_mutate_state(self) -> None:
        case = self._case(suffix="negative-payload")
        state = load_plan_state(case["data_path"], case["task_id"])
        before = deepcopy(state)
        unknown = {**case["payload"], "attempt_id": "attempt-unknown"}
        with self.assertRaises(WorkspaceAttestationError) as raised_unknown:
            record_provenance_event(
                state,
                WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
                unknown,
            )
        self.assertEqual(
            raised_unknown.exception.code,
            "MUTATION_INTENT_ATTEMPT_UNKNOWN",
        )
        self.assertEqual(state, before)

        malformed = {**case["payload"], "intent_ids": tuple(case["payload"]["intent_ids"])}
        with self.assertRaises(WorkspaceAttestationError) as raised_malformed:
            record_provenance_event(
                state,
                WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
                malformed,
            )
        self.assertEqual(
            raised_malformed.exception.code,
            "MUTATION_INTENT_EVENT_PAYLOAD_INVALID",
        )
        self.assertEqual(state, before)




if __name__ == "__main__":
    unittest.main()
