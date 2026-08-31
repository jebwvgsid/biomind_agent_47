import tempfile
import unittest

from agent_core.plan_tracker import build_plan_state, save_plan_state
from agent_core.task_spec import create_task_spec
from agent_core.wiki_lifecycle import (
    WikiLifecycleStore,
    evaluate_wiki_eligibility,
    terminal_snapshot_projection,
)
from bio_agents import BioAnalystAgent


def _snapshot(*, task_status="completed", terminal_status="completed", error=""):
    return terminal_snapshot_projection(
        task_id="wiki-task",
        task_status=task_status,
        terminal_status=terminal_status,
        managed_task=True,
        compiled_contract_verified=True,
        waiting_business_question=False,
        plan_state={
            "plan_revision": 2,
            "semantic_plan_hash": "semantic-plan-hash",
            "steps": [{"step_id": "step-1", "status": "done"}],
        },
        consultation_state={
            "finalize_protocol": {
                "schema_version": 4,
                "wire_schema_version": 3,
                "finalize_status": "blocked" if error else "compiled",
                "terminal_error_code": error,
                "compile_outcome": {
                    "compiled_contract_ref": "/protected/contract.json",
                    "compiled_contract_hash": "contract-hash",
                },
            }
        },
    )


class WikiLifecycleTests(unittest.TestCase):
    def test_terminal_verification_prefers_durable_canonical_task_spec(self):
        with tempfile.TemporaryDirectory() as root:
            plan = build_plan_state(
                "wiki-task",
                "- [>] Step 1: work | Action: EXECUTE | Done: validated output",
            )
            canonical = create_task_spec("canonical goal", task_id="wiki-task")
            plan["current_task"] = {"task_spec": canonical}
            save_plan_state(root, "wiki-task", plan)

            captured = {}
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = root

            def verify(**kwargs):
                captured["task_spec"] = kwargs["task_spec"]
                return {"compiled_contract_ref": "object://compiled"}

            agent._load_verified_finalize_v4_contract = verify
            agent._wiki_terminal_snapshot(
                "wiki-task",
                values={
                    "task_spec": create_task_spec(
                        "stale checkpoint goal",
                        task_id="wiki-task",
                    ),
                    "consultation_state": {
                        "finalize_protocol": {
                            "schema_version": 4,
                            "wire_schema_version": 3,
                            "finalize_status": "compiled",
                        },
                    },
                },
            )

        self.assertEqual(captured["task_spec"]["goal"], "canonical goal")

    def test_blocked_task_is_ineligible_and_denial_is_durable(self):
        snapshot = _snapshot(
            task_status="blocked",
            terminal_status="blocked",
            error="FINALIZE_RESPONSE_TRUNCATED",
        )
        decision = evaluate_wiki_eligibility(snapshot, decision_kind="offer")
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason"], "task_status_blocked")
        with tempfile.TemporaryDirectory() as root:
            stored = WikiLifecycleStore(root, "wiki-task").persist_decision(decision)
            self.assertTrue(stored["decision_ref"].endswith(".json"))

    def test_completed_snapshot_prepares_exactly_one_outbox_item(self):
        decision = evaluate_wiki_eligibility(_snapshot(), decision_kind="ingest")
        self.assertTrue(decision["eligible"])
        with tempfile.TemporaryDirectory() as root:
            store = WikiLifecycleStore(root, "wiki-task")
            decision = store.persist_decision(decision)
            first = store.prepare_outbox(
                eligibility_decision=decision,
                project_id="project-1",
                operation="archive_ingest",
                content_hash="content-hash",
            )
            second = store.prepare_outbox(
                eligibility_decision=decision,
                project_id="project-1",
                operation="archive_ingest",
                content_hash="content-hash",
            )
            self.assertEqual(first["outbox_id"], second["outbox_id"])
            dispatched = store.transition_outbox(first["outbox_id"], status="dispatched")
            succeeded = store.transition_outbox(
                first["outbox_id"],
                status="succeeded",
                destination_result={"acknowledged": True},
            )
            self.assertEqual(dispatched["status"], "dispatched")
            self.assertEqual(succeeded["status"], "succeeded")
            with self.assertRaises(ValueError):
                store.transition_outbox(first["outbox_id"], status="dispatched")

    def test_waiting_or_missing_planner_fails_closed(self):
        waiting = terminal_snapshot_projection(
            task_id="wiki-task",
            task_status="completed",
            terminal_status="completed",
            managed_task=True,
            waiting_business_question=True,
            plan_state={},
            consultation_state={
                "finalize_protocol": {
                    "schema_version": 4,
                    "compile_outcome": {
                        "compiled_contract_ref": "/contract.json",
                        "compiled_contract_hash": "hash",
                    },
                }
            },
        )
        decision = evaluate_wiki_eligibility(waiting, decision_kind="ingest")
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason"], "planner_not_committed")

    def test_initialized_plan_version_without_planner_commit_is_denied(self):
        snapshot = terminal_snapshot_projection(
            task_id="wiki-task",
            task_status="completed",
            terminal_status="completed",
            managed_task=True,
            waiting_business_question=False,
            plan_state={"plan_version": "plan_v1", "plan_revision": 1, "steps": []},
            consultation_state={
                "finalize_protocol": {
                    "schema_version": 4,
                    "finalize_status": "compiled",
                    "compile_outcome": {
                        "compiled_contract_ref": "/contract.json",
                        "compiled_contract_hash": "hash",
                    },
                }
            },
        )
        self.assertFalse(snapshot["planner_committed"])
        decision = evaluate_wiki_eligibility(snapshot, decision_kind="offer")
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason"], "planner_not_committed")

    def test_noncompiled_or_unverified_contract_is_denied(self):
        not_compiled = _snapshot()
        not_compiled["finalize_status"] = "resolving"
        decision = evaluate_wiki_eligibility(
            not_compiled, decision_kind="offer"
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason"], "finalize_not_compiled")

        unverified = _snapshot()
        unverified["compiled_contract_verified"] = False
        decision = evaluate_wiki_eligibility(
            unverified, decision_kind="ingest"
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason"], "compiled_contract_unverified")


if __name__ == "__main__":
    unittest.main()
