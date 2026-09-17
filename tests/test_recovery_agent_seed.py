from __future__ import annotations

import copy
import json
import unittest

from agent_core.task_spec import (
    canonical_current_task_spec,
    task_record_hash,
    task_semantic_hash,
)
from agent_core.workflow.recovery_agent import (
    RECOVERY_SEED_HARD_LIMIT_CHARS,
    RECOVERY_SYSTEM_PROMPT,
    RecoveryAgentError,
    project_recovery_seed_for_provider,
    recovery_seed_capacity,
    recovery_seed_digest,
)
from agent_core.workflow.recovery_projection import (
    build_causal_failure_record,
    project_recovery_seed,
)
from tests.test_recovery_projection import _durable, _execute_failure


def _fixture(*, with_skill: bool = True):
    durable = _durable(with_skill=with_skill)
    record = build_causal_failure_record(
        "execute_outcome", _execute_failure(), durable, task_id="task-recovery"
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    projection = project_recovery_seed(
        {
            "task_id": "task-recovery",
            "active_failure_packet_ref": record["packet_ref"],
            "failure_packet": record["failure_packet"],
            "failure_source": record["failure_source"],
        },
        durable,
    )
    assert projection is not None
    return durable, projection.recovery_seed


def _replace_current_task(durable: dict, task: dict) -> None:
    canonical = canonical_current_task_spec(task)
    durable["current_task"]["task_spec"] = canonical
    durable["current_task"]["task_semantic_hash"] = task_semantic_hash(canonical)
    durable["current_task"]["task_record_hash"] = task_record_hash(canonical)


class RecoveryProviderSeedTests(unittest.TestCase):
    def test_system_is_byte_stable_and_contains_no_invocation_facts(self) -> None:
        first = RECOVERY_SYSTEM_PROMPT.encode("utf-8")
        second = RECOVERY_SYSTEM_PROMPT.encode("utf-8")
        self.assertEqual(first, second)
        for forbidden in (
            "task-recovery", "plan-recovery", "step-1", "attempt-1",
            "action-1", "state_generation", "current route",
        ):
            self.assertNotIn(forbidden, RECOVERY_SYSTEM_PROMPT)

    def test_projection_is_minimal_deterministic_and_does_not_mutate_seed(self) -> None:
        durable, seed = _fixture()
        before = seed.to_dict()
        manifest = [{
            "ref_id": "execution-1",
            "kind": "execution",
            "task_id": "task-recovery",
            "plan_id": "plan-recovery",
            "step_id": "step-1",
            "backend": "recovery_evidence",
            "digest": "sha256:" + "a" * 64,
            "size_hint": 42,
            "scope": {"access": "current_step"},
        }]
        first = project_recovery_seed_for_provider(
            seed, durable, inspection_manifest=manifest
        )
        second = project_recovery_seed_for_provider(
            seed, durable, inspection_manifest=copy.deepcopy(manifest)
        )
        self.assertEqual(first, second)
        self.assertEqual(seed.to_dict(), before)
        encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("stdout", encoded)
        self.assertNotIn("stderr", encoded)
        self.assertNotIn("loaded_skill_documents", encoded)
        self.assertNotIn("source_path", encoded)
        self.assertNotIn("task_spec_ref", encoded)
        self.assertNotIn("plan_spec_ref", encoded)
        self.assertNotIn("task_semantic_hash", encoded)
        self.assertNotIn("sha256:" + "a" * 64, encoded)
        self.assertEqual(
            first["selected_skill_manifest"]["skills"][0]["skill_id"],
            "genomics/example",
        )
        self.assertEqual(
            first["failure_packet"]["source_outcome_ref"],
            seed.failure_packet.source_outcome_ref,
        )
        self.assertEqual(
            first["inspection_manifest"][0]["digest"], "a" * 64
        )
        self.assertTrue(recovery_seed_capacity(first)["within_hard_limit"])
        self.assertEqual(recovery_seed_digest(seed), recovery_seed_digest(seed.to_dict()))

    def test_projection_keeps_current_hard_constraint_semantics(self) -> None:
        durable, seed = _fixture(with_skill=False)
        task = copy.deepcopy(durable["current_task"]["task_spec"])
        task["requirements"] = [{
            "semantic_key": "runtime_constraint.environment",
            "text": "Use the approved environment.",
            "strength": "hard",
            "status": "active",
        }, {
            "semantic_key": "preference.display",
            "text": "Use colourful output.",
            "strength": "preference",
            "status": "active",
        }]
        _replace_current_task(durable, task)
        projected = project_recovery_seed_for_provider(seed, durable)
        constraints = projected["task_semantic_slice"]["hard_constraints"]
        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0]["strength"], "hard")

    def test_large_seed_preserves_char_telemetry_without_preempting_complete_budget(self) -> None:
        durable, seed = _fixture(with_skill=False)
        task = copy.deepcopy(durable["current_task"]["task_spec"])
        task["goal"] = "G" * (RECOVERY_SEED_HARD_LIMIT_CHARS + 1)
        _replace_current_task(durable, task)
        projected = project_recovery_seed_for_provider(seed, durable)
        self.assertFalse(recovery_seed_capacity(projected)["within_hard_limit"])
        self.assertIn(task["goal"], json.dumps(projected, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
