from __future__ import annotations

import tempfile
import unittest

from agent_core.workflow.phase_archive import (
    PhaseArchiveCollisionError,
    PhaseArchiveStore,
)
from agent_core.workflow.step_worker import (
    StepOutcome,
    StepWorkerContractError,
    archive_step_worker_attempt,
)


def _request() -> dict:
    return {
        "schema_version": 1,
        "task_id": "task-step-archive",
        "plan_id": "plan-step-archive",
        "plan_version": "plan-v1",
        "step_id": "step-1",
        "step_goal": "Run the offline fixture.",
        "completion_contract": {"success_criteria": ["fixture passes"]},
        "selected_skill_ids": [],
        "resolved_inputs": [],
        "accepted_bindings": [],
        "available_artifacts": [],
        "workspace": "/fixture",
        "attempt_ordinal": 1,
    }


class StepWorkerArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="step-worker-archive-")
        self.addCleanup(self.temp.cleanup)
        self.store = PhaseArchiveStore(self.temp.name)

    def test_private_attempt_is_archived_and_only_ref_crosses_boundary(self) -> None:
        outcome = StepOutcome(
            schema_version=1,
            type="completion_claimed",
            summary="fixture completed",
            evidence_refs=("evidence:step-1",),
            artifact_refs=("artifact:step-1",),
            worker_session_ref="worker-session:abc",
        )
        bound = archive_step_worker_attempt(
            self.store,
            task_id="task-step-archive",
            request=_request(),
            outcome=outcome,
            attempt_ordinal=1,
            backend="typed_stub",
            local_events=(
                {"role": "assistant", "content": "inspect fixture"},
                {"role": "tool", "content": "fixture is ready"},
            ),
        )

        self.assertTrue(bound.archive_ref.startswith("phase-archive:sha256:"))
        self.assertEqual(bound.worker_session_ref, "worker-session:abc")
        self.assertNotEqual(bound.archive_ref, bound.worker_session_ref)
        public = bound.to_dict()
        self.assertNotIn("transcript", public)
        self.assertNotIn("request", public)

        record = self.store.read(bound.archive_ref)
        transcript = record.to_dict()["transcript"]
        self.assertEqual(transcript[0]["type"], "step_request")
        self.assertEqual(transcript[-1]["type"], "step_outcome")
        self.assertEqual(transcript[1]["content"], "inspect fixture")
        self.assertEqual(record.to_dict()["artifacts"], ["artifact:step-1"])
        self.assertEqual(record.to_dict()["evidence"], ["evidence:step-1"])

    def test_same_attempt_is_content_idempotent_but_cannot_be_overwritten(self) -> None:
        outcome = StepOutcome(
            1,
            "completion_claimed",
            "fixture completed",
            worker_session_ref="worker-session:stable",
        )
        first = archive_step_worker_attempt(
            self.store,
            task_id="task-step-archive",
            request=_request(),
            outcome=outcome,
            attempt_ordinal=1,
            backend="typed_stub",
        )
        second = archive_step_worker_attempt(
            self.store,
            task_id="task-step-archive",
            request=_request(),
            outcome=outcome,
            attempt_ordinal=1,
            backend="typed_stub",
        )
        self.assertEqual(first.archive_ref, second.archive_ref)

        changed = StepOutcome(
            1,
            "needs_replan",
            "different terminal fact",
            worker_session_ref="worker-session:stable",
        )
        with self.assertRaises(PhaseArchiveCollisionError):
            archive_step_worker_attempt(
                self.store,
                task_id="task-step-archive",
                request=_request(),
                outcome=changed,
                attempt_ordinal=1,
                backend="typed_stub",
            )

    def test_backend_cannot_supply_boundary_owned_archive_ref(self) -> None:
        backend_bound = StepOutcome(
            1,
            "completion_claimed",
            "backend attempted ownership",
            archive_ref="phase-archive:sha256:" + "a" * 64,
        )
        with self.assertRaisesRegex(
            StepWorkerContractError, "STEP_WORKER_ARCHIVE_AUTHORITY_VIOLATION"
        ):
            archive_step_worker_attempt(
                self.store,
                task_id="task-step-archive",
                request=_request(),
                outcome=backend_bound,
                attempt_ordinal=1,
                backend="typed_stub",
            )

    def test_worker_session_and_archive_refs_cannot_alias(self) -> None:
        ref = "phase-archive:sha256:" + "b" * 64
        with self.assertRaisesRegex(
            StepWorkerContractError, "STEP_WORKER_OUTCOME_PROTOCOL_ERROR"
        ):
            StepOutcome(
                1,
                "completion_claimed",
                "aliased refs",
                worker_session_ref=ref,
                archive_ref=ref,
            )


if __name__ == "__main__":
    unittest.main()
