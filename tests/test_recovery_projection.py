from __future__ import annotations

import copy
import hashlib
import json
import unittest

from agent_core.plan_tracker import plan_record_hash, semantic_plan_hash
from agent_core.task_spec import create_task_spec, task_record_hash, task_semantic_hash
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    InstallSubagentOutcome,
    VerificationOutcome,
)
from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder,
    build_causal_failure_record,
    project_recovery_seed,
)
from agent_core.workflow.recovery_runtime import TypedRecovery
from agent_core.workflow.stage_contracts import RecoverySeed, StageContractError


def _durable(*, with_skill: bool = False) -> dict:
    task = create_task_spec(
        "Run the typed Recovery projection fixture.", task_id="task-recovery"
    )
    steps = [
        {
            "step_id": "step-0",
            "title": "Prepare input",
            "status": "passed",
            "evidence_refs": ["evidence:prepared"],
            "artifacts": [{"artifact_id": "artifact:prepared"}],
        },
        {
            "step_id": "step-1",
            "title": "Run analysis",
            "status": "running",
            "dependencies": ["step-0"],
            "success_criteria": ["The command exits successfully."],
        },
    ]
    plan = {
        "plan_id": "plan-recovery",
        "plan_version": "plan-v1",
        "plan_revision": 1,
        "generated_from_task_revision": 1,
        "base_task_semantic_hash": task_semantic_hash(task),
        "steps": copy.deepcopy(steps),
    }
    skill_refs = []
    if with_skill:
        skill_refs.append({
            "skill_id": "genomics/example",
            "snapshot_id": "snapshot-example",
            "content_hash": "sha256:example",
            "source_path": "skills/genomics/example/SKILL.md",
            "scope": "task",
            "status": "active",
            "selected_at_task_revision": 1,
        })
    return {
        "schema_version": 10,
        "workflow_routing_schema_version": 2,
        "task_id": "task-recovery",
        "plan_id": "plan-recovery",
        "plan_version": "plan-v1",
        "plan_revision": 1,
        "state_generation": 4,
        "current_step_id": "step-1",
        "current_task": {
            "task_spec": task,
            "task_revision": 1,
            "task_semantic_hash": task_semantic_hash(task),
            "task_record_hash": task_record_hash(task),
        },
        "current_plan": {
            "plan_spec": plan,
            "plan_revision": 1,
            "plan_semantic_hash": "sha256:" + semantic_plan_hash(plan),
            "plan_record_hash": plan_record_hash(plan),
            "base_task_revision": 1,
            "base_task_semantic_hash": task_semantic_hash(task),
        },
        "task_state": {
            "workflow_routing_schema_version": 2,
            "skill_access_state": {"selected_skill_refs": skill_refs},
            "typed_step_commit_receipts": {
                "commit-step-0": {
                    "outcome": {"payload": {"completed_step_id": "step-0"}},
                },
            },
        },
        "steps": steps,
    }


def _execute_failure(*, nested_context: bool = False) -> dict:
    execution = {
        "task_id": "task-recovery",
        "plan_id": "plan-recovery",
        "plan_version": "plan-v1",
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "execution_id": "execution-1",
        "returncode": 17,
        "failure_fingerprint": "failure-runtime-1",
        "stdout": "large raw output",
        "stderr": "large raw error",
    }
    if nested_context:
        execution["metadata"] = {"nested": {"messages": ["do not project"]}}
    return ExecuteOutcome(
        schema_version=1,
        type="execution_failed",
        summary="The analysis command failed.",
        scope={
            "task_id": "task-recovery",
            "plan_id": "plan-recovery",
            "plan_version": "plan-v1",
            "step_id": "step-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
        },
        payload={
            "execution_record": execution,
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "return_code": 17,
            "failure_fingerprint": "failure-runtime-1",
            "progress_snapshot": {},
        },
        evidence_refs=("execution-1",),
    ).to_dict()


def _verification_rejected() -> dict:
    return VerificationOutcome(
        schema_version=1,
        type="rejected",
        summary="The approved plan contract was violated.",
        scope={
            "task_id": "task-recovery",
            "plan_id": "plan-recovery",
            "plan_version": "plan-v1",
            "step_id": "step-1",
            "validation_id": "validation-1",
        },
        payload={
            "validation_evidence": {"validation_id": "validation-1"},
            "completion_gate": {"passed": False},
            "contract_violations": [{
                "code": "PLAN_OUTPUT_MISSING",
                "message": "Output required by the plan is absent.",
            }],
            "failure_owner": "PLAN",
        },
        evidence_refs=("validation-1",),
    ).to_dict()


def _commit_conflict() -> dict:
    return CommitStepOutcome(
        schema_version=1,
        type="commit_conflict",
        summary="The durable generation changed.",
        scope={"task_id": "task-recovery", "step_id": "step-1"},
        payload={
            "expected_state_generation": 3,
            "current_state_generation": 4,
            "no_partial_mutation": True,
        },
    ).to_dict()


def _install_failure() -> dict:
    return InstallSubagentOutcome(
        schema_version=1,
        type="installation_failed",
        summary="Installation failed.",
        scope={"task_id": "task-recovery", "step_id": "step-1"},
        payload={
            "status": "failed",
            "evidence": [{"step_id": "step-1", "status": "failed"}],
        },
    ).to_dict()


class _MemoryStore:
    def __init__(self, value: dict):
        self.value = copy.deepcopy(value)

    def load(self, _data_path: str, _task_id: str) -> dict:
        return copy.deepcopy(self.value)

    def save(self, _data_path: str, _task_id: str, candidate: dict) -> None:
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1


def _causal_fixture(
    source_kind: str,
    source: dict,
    *,
    durable: dict | None = None,
) -> tuple[dict, dict, dict]:
    durable = copy.deepcopy(durable or _durable())
    record = build_causal_failure_record(
        source_kind, source, durable, task_id="task-recovery"
    )
    task_state = dict(durable.get("task_state") or {})
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    state = {
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": copy.deepcopy(record["failure_packet"]),
        "failure_source": copy.deepcopy(record["failure_source"]),
    }
    return state, durable, record


class RecoveryProjectionTests(unittest.TestCase):
    def test_execution_failure_becomes_compact_recovery_seed(self) -> None:
        outcome = _execute_failure()
        state, durable, record = _causal_fixture("execute_outcome", outcome)
        projection = project_recovery_seed(state, durable)
        self.assertIsNotNone(projection)
        assert projection is not None
        packet = projection.failure_packet
        self.assertEqual(packet.failure_type, "execution_failed")
        self.assertEqual(packet.failure_owner, "GENERATED_ACTION")
        self.assertEqual(packet.attempt_ref, "attempt-1")
        self.assertEqual(packet.action_ref, "action-1")
        self.assertEqual(packet.failure_fingerprint, "failure-runtime-1")
        self.assertEqual(packet.evidence_refs, ("execution-1",))
        self.assertEqual(packet.source, "execute_outcome")
        self.assertEqual(packet.source_outcome_ref, record["source_outcome_ref"])
        self.assertEqual(projection.recovery_seed.current_step["step_id"], "step-1")
        self.assertEqual(
            projection.recovery_seed.dependency_outcomes[0]["outcome_ref"],
            "commit-step-0",
        )
        self.assertNotIn("execution_record", projection.source["payload"])

        wire = projection.recovery_seed.to_dict()
        self.assertEqual(RecoverySeed.from_mapping(wire).to_dict(), wire)
        serialized = json.dumps(wire, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "stdout", "stderr", "large raw output", "large raw error",
            "messages", "transcript",
        ):
            self.assertNotIn(forbidden, serialized)
        expected = hashlib.sha256(json.dumps(
            {"kind": "execute_outcome", "source": outcome},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()[:24]
        self.assertEqual(projection.source_fingerprint, expected)

    def test_nested_conversation_payload_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            StageContractError, "STAGE_CONTEXT_LEAK_FORBIDDEN"
        ):
            build_causal_failure_record(
                "execute_outcome",
                _execute_failure(nested_context=True),
                _durable(),
                task_id="task-recovery",
            )

    def test_cross_step_and_wrong_plan_packets_are_rejected(self) -> None:
        cross_step = _execute_failure()
        cross_step["scope"]["step_id"] = "step-0"
        with self.assertRaisesRegex(StageContractError, "STAGE_SCOPE_MISMATCH"):
            build_causal_failure_record(
                "execute_outcome", cross_step, _durable(),
                task_id="task-recovery",
            )
        wrong_plan = _execute_failure()
        wrong_plan["scope"]["plan_version"] = "stale-plan"
        with self.assertRaisesRegex(StageContractError, "STAGE_SCOPE_MISMATCH"):
            build_causal_failure_record(
                "execute_outcome", wrong_plan, _durable(),
                task_id="task-recovery",
            )

    def test_packet_plan_record_identity_is_revalidated(self) -> None:
        state, durable, _ = _causal_fixture("execute_outcome", _execute_failure())
        durable["current_plan"]["plan_record_hash"] = "sha256:corrupt"
        with self.assertRaisesRegex(StageContractError, "STAGE_REFERENCE_INVALID"):
            project_recovery_seed(state, durable)

    def test_retained_same_step_outcomes_cannot_override_packet(self) -> None:
        state, durable, record = _causal_fixture(
            "execute_outcome", _execute_failure()
        )
        state["verification_outcome"] = _verification_rejected()
        state["commit_step_outcome"] = _commit_conflict()
        projection = project_recovery_seed(state, durable)
        assert projection is not None
        self.assertEqual(projection.source_kind, "execute_outcome")
        self.assertEqual(
            projection.failure_packet.failure_id,
            record["failure_packet"]["failure_id"],
        )

    def test_retained_outcome_without_packet_is_not_authority(self) -> None:
        self.assertIsNone(project_recovery_seed({
            "task_id": "task-recovery",
            "execute_outcome": _execute_failure(),
            "verification_outcome": _verification_rejected(),
        }, _durable()))

    def test_stale_edge_packet_cannot_override_new_active_packet(self) -> None:
        old_state, durable, _ = _causal_fixture(
            "execute_outcome", _execute_failure()
        )
        new_record = build_causal_failure_record(
            "completion_gate_outcome", _verification_rejected(), durable,
            task_id="task-recovery",
        )
        durable["task_state"]["causal_failure_packets"][
            new_record["packet_ref"]
        ] = new_record
        durable["task_state"]["active_failure_packet_ref"] = new_record[
            "packet_ref"
        ]
        with self.assertRaisesRegex(
            StageContractError, "RECOVERY_CAUSAL_PACKET_STALE"
        ):
            project_recovery_seed(old_state, durable)

    def test_binder_persists_install_packet_for_resume(self) -> None:
        store = _MemoryStore(_durable())
        update = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        ).bind(
            {"task_id": "task-recovery"},
            "install_subagent_outcome",
            _install_failure(),
        )
        self.assertTrue(update["active_failure_packet_ref"])
        projection = project_recovery_seed(
            {"task_id": "task-recovery"}, store.value
        )
        assert projection is not None
        self.assertEqual(projection.source_kind, "install_subagent_outcome")
        self.assertEqual(
            projection.failure_packet.error_code, "INSTALLATION_FAILED"
        )

    def test_completion_gate_and_commit_have_exact_causal_packets(self) -> None:
        gate_state, gate_durable, _ = _causal_fixture(
            "completion_gate_outcome", _verification_rejected()
        )
        gate = project_recovery_seed(gate_state, gate_durable)
        assert gate is not None
        self.assertEqual(gate.source_kind, "completion_gate_outcome")
        self.assertEqual(gate.failure_packet.failure_owner, "PLAN")

        commit_state, commit_durable, _ = _causal_fixture(
            "commit_step_outcome", _commit_conflict()
        )
        commit = project_recovery_seed(commit_state, commit_durable)
        assert commit is not None
        self.assertEqual(commit.failure_packet.error_code, "COMMIT_CONFLICT")

    def test_completion_failure_packet_carries_step_archive_inspection_ref(self) -> None:
        store = _MemoryStore(_durable())
        archive_ref = "phase-archive:sha256:" + "c" * 64
        update = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        ).bind(
            {
                "task_id": "task-recovery",
                "step_worker_outcome": {
                    "schema_version": 1,
                    "type": "completion_claimed",
                    "summary": "worker claims completion",
                    "evidence_refs": [],
                    "artifact_refs": [],
                    "binding_refs": [],
                    "effect_refs": [],
                    "blocker": None,
                    "worker_session_ref": "worker-session:fixture",
                    "archive_ref": archive_ref,
                },
            },
            "completion_gate_outcome",
            _verification_rejected(),
        )
        self.assertIn(archive_ref, update["failure_packet"]["inspection_refs"])

    def test_pre_execution_rejection_has_unambiguous_identity(self) -> None:
        outcome = ExecuteOutcome(
            schema_version=1,
            type="pre_execution_rejected",
            summary="Dependency verification rejected the candidate.",
            scope={
                "task_id": "task-recovery",
                "plan_id": "plan-recovery",
                "plan_version": "plan-v1",
                "step_id": "step-1",
            },
            payload={
                "violation_owner": "DEPENDENCY",
                "violation_code": "DEPENDENCY_EVIDENCE_STALE",
                "candidate_fingerprint": "candidate-1",
                "command_started": False,
                "budget_receipt": {"consumed": False},
            },
        ).to_dict()
        state, durable, _ = _causal_fixture("execute_outcome", outcome)
        projection = project_recovery_seed(state, durable)
        assert projection is not None
        self.assertEqual(projection.failure_packet.failure_owner, "DEPENDENCY")
        self.assertEqual(
            projection.failure_packet.error_code, "DEPENDENCY_EVIDENCE_STALE"
        )
        self.assertEqual(
            projection.failure_packet.failure_fingerprint, "candidate-1"
        )

    def test_skill_projection_is_manifest_only(self) -> None:
        state, durable, _ = _causal_fixture(
            "execute_outcome", _execute_failure(),
            durable=_durable(with_skill=True),
        )
        projection = project_recovery_seed(state, durable)
        assert projection is not None
        refs = projection.recovery_seed.selected_skill_bundle.to_dict()["skills"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["snapshot_id"], "snapshot-example")
        self.assertNotIn("status", refs[0])
        self.assertNotIn("selected_at_task_revision", refs[0])


class RecoveryRuntimeProjectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self, source_kind: str, source: dict
    ) -> tuple[dict, _MemoryStore, dict]:
        state, durable, _ = _causal_fixture(source_kind, source)
        store = _MemoryStore(durable)
        result = await TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run(state)
        return result, store, state

    async def test_execute_semantics_do_not_regress(self) -> None:
        result, _, _ = await self._run("execute_outcome", _execute_failure())
        self.assertEqual(
            result["recovery_outcome"]["type"], "candidate_repair_required"
        )
        self.assertEqual(
            result["recovery_outcome"]["payload"]["failure_owner"],
            "GENERATED_ACTION",
        )

    async def test_completion_gate_requests_plan_revision(self) -> None:
        result, _, _ = await self._run(
            "completion_gate_outcome", _verification_rejected()
        )
        self.assertEqual(
            result["recovery_outcome"]["type"], "plan_revision_required"
        )
        self.assertEqual(
            result["recovery_outcome"]["payload"]["contract_violations"],
            [{
                "code": "PLAN_OUTPUT_MISSING",
                "message": "Output required by the plan is absent.",
            }],
        )

    async def test_commit_requests_plan_revision(self) -> None:
        result, _, _ = await self._run(
            "commit_step_outcome", _commit_conflict()
        )
        self.assertEqual(
            result["recovery_outcome"]["type"], "plan_revision_required"
        )

    async def test_resume_valid_packet_and_consumed_replay_are_idempotent(self) -> None:
        state, durable, _ = _causal_fixture(
            "execute_outcome", _execute_failure()
        )
        store = _MemoryStore(durable)
        recovery = TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        resumed = await recovery.run({"task_id": "task-recovery"})
        replayed = await recovery.run(state)
        self.assertEqual(resumed, replayed)
        self.assertEqual(
            store.value["task_state"]["active_failure_packet_ref"], ""
        )

    async def test_resume_missing_packet_fails_closed(self) -> None:
        store = _MemoryStore(_durable())
        result = await TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({"task_id": "task-recovery"})
        self.assertEqual(
            result["recovery_outcome"]["type"], "internal_protocol_error"
        )
        self.assertEqual(
            result["recovery_outcome"]["payload"]["error_code"],
            "RECOVERY_CAUSAL_PACKET_MISSING",
        )

    async def test_install_failure_enters_bounded_repair(self) -> None:
        result, _, _ = await self._run(
            "install_subagent_outcome", _install_failure()
        )
        self.assertEqual(
            result["recovery_outcome"]["type"], "candidate_repair_required"
        )


if __name__ == "__main__":
    unittest.main()
