from __future__ import annotations

import copy
import unittest

from agent_core.plan_contract import StateCASConflict
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.recovery_runtime import RecoveryRuntime
from tests.test_recovery_projection import _durable, _execute_failure


class _Store:
    def __init__(self, durable: dict, *, conflict: str = "") -> None:
        self.value = copy.deepcopy(durable)
        self.conflict = conflict
        self.save_count = 0

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        if self.conflict == "before":
            raise StateCASConflict("fixture conflict")
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1
        if self.conflict == "after":
            raise StateCASConflict("fixture replay")


def _directive(
    *,
    action: str = "retry_step",
    directive_id: str = "directive-1",
    task_id: str = "task-recovery",
    target_step_id: str = "step-1",
    summary: str = "The generated action failed.",
    instruction: str = "Regenerate only the failed action.",
    evidence_refs=("execution-1",),
) -> dict:
    value = {
        "schema_version": 1,
        "directive_id": directive_id,
        "task_id": task_id,
        "action": action,
        "summary": summary,
        "reason_code": "GENERATED_ACTION_FAILED",
        "evidence_refs": list(evidence_refs),
    }
    if action in {"retry_step", "revalidate"}:
        value["target_step_id"] = target_step_id
    if action in {"retry_step", "revise_plan", "reconsult"}:
        value["instruction"] = instruction
    if action == "ask_user":
        value["question"] = "Which approved input should be used?"
        value["requested_slots"] = ["approved_input"]
    return value


def _bind(store: _Store, *, source: dict | None = None) -> dict:
    source = copy.deepcopy(source or _execute_failure())
    record = build_causal_failure_record(
        "execute_outcome", source, store.value, task_id="task-recovery"
    )
    task_state = dict(store.value.get("task_state") or {})
    packets = dict(task_state.get("causal_failure_packets") or {})
    packets[record["packet_ref"]] = record
    task_state["causal_failure_packets"] = packets
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    store.value["task_state"] = task_state
    return {
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
    }


class RecoveryRuntimeDirectiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_directive_is_admitted_and_replayed_exactly_once(self) -> None:
        store = _Store(_durable())
        state = {
            **_bind(store),
            "recovery_directive": _directive(),
            "recovery_archive_ref": "phase-archive:fixture",
        }
        runtime = RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        first = await runtime.run(state)
        second = await runtime.run(state)
        self.assertEqual(first, second)
        self.assertEqual(first["recovery_outcome"]["type"], "candidate_repair_required")
        self.assertEqual(store.save_count, 1)
        receipt = store.value["task_state"]["typed_recovery_receipts"]["directive-1"]
        self.assertEqual(receipt["recovery_archive_ref"], "phase-archive:fixture")
        self.assertTrue(receipt["directive_digest"].startswith("sha256:"))

    async def test_runtime_routes_by_action_not_diagnosis_words(self) -> None:
        store = _Store(_durable())
        state = {
            **_bind(store),
            "recovery_directive": _directive(
                summary="Planner consultant supervisor generate route words.",
            ),
        }
        result = await RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run(state)
        self.assertEqual(result["recovery_outcome"]["type"], "candidate_repair_required")

    async def test_missing_extra_and_nested_route_fields_fail_closed(self) -> None:
        for update in (
            {"instruction": None},
            {"unknown_field": "x"},
            {"metadata": {"next_step": "generate"}},
            {"metadata": {"target_route": "planner"}},
            {"dispatch_route": "generate"},
        ):
            store = _Store(_durable())
            directive = _directive()
            if update.get("instruction") is None:
                directive.pop("instruction")
            else:
                directive.update(update)
            result = await RecoveryRuntime(
                data_path="/fixture", loader=store.load, saver=store.save
            ).run({**_bind(store), "recovery_directive": directive})
            with self.subTest(update=update):
                self.assertEqual(result["recovery_outcome"]["type"], "internal_protocol_error")
                self.assertEqual(store.save_count, 0)

    async def test_evidence_must_be_declared_or_inspected(self) -> None:
        store = _Store(_durable())
        state = _bind(store)
        result = await RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({
            **state,
            "recovery_directive": _directive(evidence_refs=("evidence:unknown",)),
        })
        self.assertEqual(
            result["recovery_outcome"]["payload"]["error_code"],
            "RECOVERY_DIRECTIVE_EVIDENCE_SCOPE_INVALID",
        )

        store = _Store(_durable())
        state = _bind(store)
        accepted = await RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({
            **state,
            "recovery_inspected_refs": ["evidence:inspected"],
            "recovery_directive": _directive(evidence_refs=("evidence:inspected",)),
        })
        self.assertEqual(accepted["recovery_outcome"]["type"], "candidate_repair_required")

    async def test_wrong_task_step_and_stale_packet_fail_closed(self) -> None:
        for directive in (
            _directive(task_id="other-task"),
            _directive(target_step_id="step-other"),
        ):
            store = _Store(_durable())
            result = await RecoveryRuntime(
                data_path="/fixture", loader=store.load, saver=store.save
            ).run({**_bind(store), "recovery_directive": directive})
            self.assertEqual(result["recovery_outcome"]["type"], "internal_protocol_error")

        store = _Store(_durable())
        state = _bind(store)
        store.value["current_step_id"] = "step-other"
        result = await RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({**state, "recovery_directive": _directive()})
        self.assertEqual(result["recovery_outcome"]["type"], "internal_protocol_error")

    async def test_budget_and_no_progress_are_deterministic(self) -> None:
        store = _Store(_durable())
        runtime = RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save, repair_limit=2
        )
        outcomes = []
        for ordinal in (1, 2, 3):
            source = _execute_failure()
            source["payload"]["incident_id"] = "incident-budget"
            source["payload"]["failure_fingerprint"] = f"failure-{ordinal}"
            source["payload"]["execution_record"]["failure_fingerprint"] = f"failure-{ordinal}"
            outcomes.append(await runtime.run({
                **_bind(store, source=source),
                "recovery_directive": _directive(
                    directive_id=f"directive-{ordinal}",
                    summary=f"Changed diagnosis {ordinal}",
                ),
            }))
        self.assertEqual(
            [item["recovery_outcome"]["type"] for item in outcomes],
            ["candidate_repair_required", "candidate_repair_required", "blocked"],
        )

        store = _Store(_durable())
        runtime = RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        source = _execute_failure()
        source["payload"]["incident_id"] = "incident-no-progress"
        first = await runtime.run({
            **_bind(store, source=source),
            "recovery_directive": _directive(directive_id="progress-1"),
        })
        second = await runtime.run({
            **_bind(store, source=source),
            "recovery_directive": _directive(directive_id="progress-2"),
        })
        self.assertEqual(first["recovery_outcome"]["type"], "candidate_repair_required")
        self.assertEqual(second["recovery_outcome"]["type"], "blocked")
        self.assertEqual(
            second["recovery_outcome"]["payload"]["reason_code"],
            "RECOVERY_NO_PROGRESS",
        )

    async def test_cas_conflict_and_cas_replay_receipt(self) -> None:
        store = _Store(_durable(), conflict="before")
        result = await RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({**_bind(store), "recovery_directive": _directive()})
        self.assertEqual(
            result["recovery_outcome"]["payload"]["error_code"],
            "RECOVERY_RECEIPT_CAS_CONFLICT",
        )

        store = _Store(_durable(), conflict="after")
        result = await RecoveryRuntime(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({**_bind(store), "recovery_directive": _directive()})
        self.assertEqual(result["recovery_outcome"]["type"], "candidate_repair_required")


if __name__ == "__main__":
    unittest.main()
