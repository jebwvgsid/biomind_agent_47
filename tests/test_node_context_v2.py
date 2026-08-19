from __future__ import annotations

import json
import unittest

from agent_core.node_context import (
    ContextCapacityExceeded,
    ControlMetadataLeakError,
    InvocationProtocolError,
    InvocationRegistry,
    LLMPayload,
    SemanticHandleError,
    build_node_context,
    evaluate_node_staleness,
    resolve_response_handles,
)
from agent_core.task_spec import task_record_hash, task_semantic_hash


def _task_spec() -> dict:
    return {
        "schema_version": 4,
        "task_id": "task-internal-uuid",
        "revision": 3,
        "goal": "Annotate the VCF and write the required report.",
        "inputs": [
            {
                "input_id": "artifact-input-uuid",
                "role": "variant_vcf",
                "path": "/data/input sample.vcf.gz",
                "format": "vcf.gz",
                "required": True,
                "source_ref": "object:sha256:" + "1" * 64,
            }
        ],
        "output_contracts": [
            {
                "output_id": "artifact-output-uuid",
                "role": "annotated_vcf",
                "path": "/output/aa.vcf.gz",
                "format": "vcf.gz",
                "required": True,
                "provenance": [{"source_ref": "audit:sha256:" + "2" * 64}],
            }
        ],
        "requirements": [
            {
                "id": "constraint-internal-uuid",
                "semantic_key": "required_behavior.annotation_format",
                "operator": "eq",
                "value": "GTF2.2",
                "strength": "hard",
                "source_ref": "audit:sha256:" + "3" * 64,
            }
        ],
        "change_log": [{"recorded_at": "2026-08-10T00:00:00", "source": "user"}],
    }


def _plan() -> dict:
    return {
        "plan_id": "plan-internal-uuid",
        "steps": [
            {
                "step_id": "step-1",
                "title": "Annotate variants",
                "action": "Run the annotation workflow",
                "inputs": ["artifact-input-uuid"],
                "outputs": ["artifact-output-uuid"],
                "dependencies": [],
                "success_criteria": ["Annotated VCF validates"],
                "status": "ready",
            }
        ],
        "validation_status": "valid",
        "updated_at": "2026-08-10T00:00:00",
    }


class TaskIdentityTests(unittest.TestCase):
    def test_provenance_only_change_changes_record_not_semantic_hash(self) -> None:
        first = _task_spec()
        second = _task_spec()
        second["change_log"].append({"recorded_at": "later", "source": "controller"})
        second["requirements"][0]["source_ref"] = "audit:sha256:" + "9" * 64
        self.assertEqual(task_semantic_hash(first), task_semantic_hash(second))
        self.assertNotEqual(task_record_hash(first), task_record_hash(second))

    def test_semantic_change_changes_both_hashes(self) -> None:
        first = _task_spec()
        second = _task_spec()
        second["requirements"][0]["value"] = "GFF3"
        self.assertNotEqual(task_semantic_hash(first), task_semantic_hash(second))
        self.assertNotEqual(task_record_hash(first), task_record_hash(second))

    def test_key_order_is_canonical(self) -> None:
        first = _task_spec()
        second = json.loads(json.dumps(first, sort_keys=True))
        self.assertEqual(task_semantic_hash(first), task_semantic_hash(second))
        self.assertEqual(task_record_hash(first), task_record_hash(second))


class NodeContextBoundaryTests(unittest.TestCase):
    def _context(self, node_type: str = "generate", *, trace: bool = False):
        return build_node_context(
            node_type=node_type,
            task_spec=_task_spec(),
            plan_state=_plan(),
            state_generation=42,
            current_step_id="step-1",
            approval_ref="object:sha256:" + "a" * 64,
            approval_applicability="applicable",
            execution_epoch=5,
            attempt_id="attempt-internal-uuid",
            trace_enabled=trace,
            runtime={"progress": "not_started", "state_generation": 42},
            evidence=[
                {
                    "evidence_id": "evidence-internal-uuid",
                    "excerpt": "The executable is available.",
                    "ref": "evidence:sha256:" + "b" * 64,
                }
            ],
            allowed_operations=["propose_action"],
        )

    def test_binding_has_control_metadata_but_payload_does_not(self) -> None:
        context = self._context(trace=True)
        binding = context.binding.to_dict()
        payload = context.llm_payload.to_dict()
        rendered = context.llm_payload.to_json()
        self.assertEqual(binding["state_generation"], 42)
        self.assertTrue(binding["task_semantic_hash"].startswith("sha256:"))
        self.assertTrue(binding["approval_ref"].startswith("object:sha256:"))
        for prohibited in (
            "task_semantic_hash", "plan_semantic_hash", "state_generation",
            "approval_ref", "object:sha256:", "evidence:sha256:",
        ):
            self.assertNotIn(prohibited, rendered)
        self.assertEqual(payload["task"]["inputs"][0]["handle"], "input.variant_vcf")
        self.assertEqual(payload["task"]["outputs"][0]["handle"], "output.annotated_vcf")

    def test_trace_is_optional_and_does_not_change_payload(self) -> None:
        without = self._context(trace=False)
        with_trace = self._context(trace=True)
        self.assertIsNone(without.projection_trace)
        self.assertIsNotNone(with_trace.projection_trace)
        self.assertEqual(without.llm_payload.to_dict(), with_trace.llm_payload.to_dict())

    def test_semantic_handle_round_trip_and_unknown_rejection(self) -> None:
        context = self._context()
        resolved = resolve_response_handles(
            context.binding,
            ["output.annotated_vcf"],
            object_type="artifact",
            direction="output",
            usage="declare_output",
        )
        self.assertEqual(resolved[0]["stable_identity"], "artifact-output-uuid")
        with self.assertRaisesRegex(SemanticHandleError, "SEMANTIC_HANDLE_UNKNOWN"):
            resolve_response_handles(context.binding, ["output.typo"])

    def test_control_metadata_leak_fails_closed(self) -> None:
        with self.assertRaises(ControlMetadataLeakError):
            LLMPayload({"task": {"task_semantic_hash": "sha256:" + "f" * 64}})

    def test_current_user_message_uses_invocation_local_handle(self) -> None:
        context = build_node_context(
            node_type="consultant",
            task_spec=_task_spec(),
            evidence=[{
                "evidence_id": "message-internal-id",
                "kind": "user_message",
                "excerpt": "Use the GTF in the data directory.",
            }],
        )
        self.assertEqual(context.llm_payload.to_dict()["evidence"][0]["handle"], "U1")
        self.assertNotIn("message-internal-id", context.llm_payload.to_json())

    def test_capacity_failure_does_not_truncate_semantics(self) -> None:
        with self.assertRaises(ContextCapacityExceeded):
            build_node_context(
                node_type="planner",
                task_spec=_task_spec(),
                plan_state=_plan(),
                max_payload_chars=50,
            )

    def test_generate_keeps_full_task_and_focuses_compatibility_closure(self) -> None:
        task = _task_spec()
        task["inputs"].append({
            "artifact_id": "unrelated-input",
            "semantic_handle": "input.unrelated",
            "role": "unrelated",
            "canonical_path": "/data/unrelated.txt",
        })
        task["output_contracts"].append({
            "artifact_id": "unrelated-output",
            "semantic_handle": "output.unrelated",
            "role": "unrelated",
            "canonical_path": "/output/unrelated.txt",
        })
        task["requirements"].append({
            "constraint_id": "unrelated-constraint",
            "semantic_key": "required_input.unrelated",
            "operator": "eq",
            "value": "/data/unrelated.txt",
            "strength": "hard",
        })
        context = build_node_context(
            node_type="generate",
            task_spec=task,
            plan_state=_plan(),
            current_step_id="step-1",
            trace_enabled=True,
        )
        rendered = context.llm_payload.to_json()
        self.assertIn("input.unrelated", rendered)
        self.assertIn("output.unrelated", rendered)
        self.assertIn("/data/unrelated.txt", rendered)
        self.assertNotIn("unrelated-constraint", rendered)
        compatibility_task = context.llm_payload.to_dict()["task"]
        self.assertNotIn(
            "input.unrelated",
            json.dumps(compatibility_task, ensure_ascii=False),
        )
        self.assertEqual(
            context.projection_trace.selection_policy,
            "full-task-with-current-step-focus-v1",
        )
        self.assertIn(
            "input.unrelated",
            context.projection_trace.excluded_handles_with_reason,
        )

    def test_semantic_nodes_receive_one_identical_ordered_highlight_list(self) -> None:
        task = _task_spec()
        task["requirements"].append({
            "requirement_id": "requirement-2",
            "value": "Do not rename contigs before annotation.",
            "strength": "required",
            "source_ref": "U2",
        })
        highlights = []
        for node_type in ("planner", "planner_repair", "generate", "recovery"):
            context = build_node_context(
                node_type=node_type,
                task_spec=task,
                plan_state=_plan(),
                current_step_id="step-1",
            )
            highlights.append(
                context.llm_payload.to_dict()["task"]["highlighted_constraints"]
            )

        self.assertTrue(all(value == highlights[0] for value in highlights[1:]))
        self.assertEqual(
            [item["text"] for item in highlights[0]],
            ["GTF2.2", "Do not rename contigs before annotation."],
        )
        self.assertTrue(all(
            set(item) == {"text", "importance", "source_refs"}
            for item in highlights[0]
        ))
        self.assertEqual(highlights[0][1]["source_refs"], ["U2"])


class NodeSpecificStalenessTests(unittest.TestCase):
    def test_planner_ignores_record_and_generation_only_change(self) -> None:
        binding = build_node_context(
            node_type="planner", task_spec=_task_spec(), plan_state=_plan(), state_generation=3,
        ).binding
        decision = evaluate_node_staleness(binding, {
            "state_generation": 4,
            "task_record_hash": "sha256:" + "c" * 64,
            "task_semantic_hash": binding.task_semantic_hash,
        })
        self.assertTrue(decision.usable)
        self.assertEqual(decision.code, "REVALIDATE_ON_CURRENT_RECORD")
        self.assertEqual(
            set(decision.ignored_record_changes),
            {"state_generation", "task_record_hash"},
        )

    def test_planner_rejects_semantic_change(self) -> None:
        binding = build_node_context(
            node_type="planner", task_spec=_task_spec(), plan_state=_plan(),
        ).binding
        decision = evaluate_node_staleness(binding, {"task_semantic_hash": "sha256:" + "d" * 64})
        self.assertFalse(decision.usable)
        self.assertEqual(decision.code, "STALE_TASK_SEMANTICS")

    def test_planner_repair_uses_violation_set_not_validation_timestamp(self) -> None:
        binding = build_node_context(
            node_type="planner_repair",
            task_spec=_task_spec(),
            plan_state=_plan(),
            violation_set_identity="violations-v1",
        ).binding
        current = {
            "task_semantic_hash": binding.task_semantic_hash,
            "plan_semantic_hash": binding.plan_semantic_hash,
            "violation_set_identity": "violations-v1",
            "validation_timestamp": "later",
        }
        self.assertTrue(evaluate_node_staleness(binding, current).usable)
        current["violation_set_identity"] = "violations-v2"
        self.assertEqual(
            evaluate_node_staleness(binding, current).code,
            "STALE_PLAN_REPAIR_BASE",
        )

    def test_generate_ignores_other_telemetry_but_checks_step_and_epoch(self) -> None:
        binding = build_node_context(
            node_type="generate",
            task_spec=_task_spec(),
            plan_state=_plan(),
            current_step_id="step-1",
            approval_ref="approval-immutable-ref",
            approval_applicability="applicable",
            execution_epoch=5,
        ).binding
        current = {
            "task_semantic_hash": binding.task_semantic_hash,
            "plan_semantic_hash": binding.plan_semantic_hash,
            "approval_ref": binding.approval_ref,
            "approval_applicability": "applicable",
            "step_id": "step-1",
            "execution_epoch": "5",
            "state_generation": 99,
            "other_step_telemetry": "changed",
        }
        self.assertTrue(evaluate_node_staleness(binding, current).usable)
        current["execution_epoch"] = "6"
        self.assertEqual(
            evaluate_node_staleness(binding, current).code,
            "STALE_GENERATE_CONTEXT",
        )

    def test_recovery_checks_failure_and_attempt(self) -> None:
        binding = build_node_context(
            node_type="recovery",
            task_spec=_task_spec(),
            plan_state=_plan(),
            failure_fingerprint="failure-a",
            attempt_id="attempt-a",
        ).binding
        current = {
            "task_semantic_hash": binding.task_semantic_hash,
            "plan_semantic_hash": binding.plan_semantic_hash,
            "failure_fingerprint": "failure-b",
            "attempt_id": "attempt-a",
        }
        self.assertEqual(
            evaluate_node_staleness(binding, current).code,
            "STALE_RECOVERY_CONTEXT",
        )


class InvocationRegistryTests(unittest.TestCase):
    def _binding(self, suffix: str):
        return build_node_context(
            node_type="planner",
            task_spec=_task_spec(),
            plan_state=_plan(),
            invocation_id=f"inv-{suffix}",
        ).binding

    def test_duplicate_and_late_response_are_rejected(self) -> None:
        registry = InvocationRegistry(max_entries=3)
        binding = self._binding("one")
        registry.register(binding)
        self.assertEqual(registry.consume(binding.invocation_id), binding)
        with self.assertRaisesRegex(InvocationProtocolError, "INVOCATION_NOT_ACTIVE"):
            registry.consume(binding.invocation_id)
        with self.assertRaisesRegex(InvocationProtocolError, "INVOCATION_UNKNOWN"):
            registry.consume("inv-missing")

    def test_stale_signature_is_deduplicated(self) -> None:
        registry = InvocationRegistry(max_entries=3)
        first = self._binding("one")
        registry.register(first, stale_signature="same-stale-domain")
        with self.assertRaisesRegex(InvocationProtocolError, "ALREADY_ACTIVE"):
            registry.register(self._binding("two"), stale_signature="same-stale-domain")
        registry.terminate(first.invocation_id, status="stale")
        self.assertTrue(registry.mark_stale_signature_handled("same-stale-domain"))
        self.assertFalse(registry.mark_stale_signature_handled("same-stale-domain"))
        with self.assertRaisesRegex(InvocationProtocolError, "ALREADY_HANDLED"):
            registry.register(self._binding("three"), stale_signature="same-stale-domain")

    def test_registry_is_bounded_without_evicting_active_calls(self) -> None:
        registry = InvocationRegistry(max_entries=2)
        one = self._binding("one")
        two = self._binding("two")
        registry.register(one)
        registry.consume(one.invocation_id)
        registry.register(two)
        registry.consume(two.invocation_id)
        registry.register(self._binding("three"))
        self.assertLessEqual(registry.size, 2)


if __name__ == "__main__":
    unittest.main()
