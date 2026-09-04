from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import unittest
import tempfile

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphBubbleUp

from agent_core.node_context import ControlMetadataLeakError
from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from agent_core.workflow.outcomes import ConsultantOutcome
from agent_core.workflow.phase_subgraph import (
    archive_writer_for_store,
    build_scoped_phase_node,
    manifest_only_skill_context,
    PhaseLoopError,
    PhaseProviderContext,
    phase_provider_context,
    project_phase_seed_for_provider,
)
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.stage_contracts import StageContractError


def _consultant_outcome(kind: str, *, task_id: str = "task-1") -> dict:
    payload = {
        "consultation_continues": {
            "phase": "consulting",
            "completion_decision": "needs_reasoning",
        },
        "planning_ready": {
            "task_revision": 1,
            "contract_status": "canonical_task_ready",
        },
    }[kind]
    return ConsultantOutcome(
        schema_version=1,
        type=kind,
        summary=f"{kind} summary",
        scope={"task_id": task_id},
        payload=payload,
    ).to_dict()


class ScopedPhaseNodeTests(unittest.IsolatedAsyncioTestCase):
    def test_consultant_and_planner_seed_projection_is_exact_allowlist(
        self,
    ) -> None:
        digest_a = "sha256:" + "a" * 64
        object_a = "object:sha256:" + "b" * 64
        common = {
            "schema_version": 1,
            "phase": "consultant",
            "task_id": "task-private",
            "task_spec_ref": {
                "task_id": "task-private",
                "task_revision": 7,
                "task_semantic_hash": digest_a,
                "task_record_hash": digest_a,
                "task_spec_ref": "plan-state:task-private#current_task.task_spec",
            },
            "selected_skill_bundle": {
                "bundle_id": "skill-bundle-private",
                "task_revision": 7,
                "snapshot_id": "snapshot-private",
                "skills": [{
                    "skill_id": "genomics/snpeff-annotation",
                    "scope": "task",
                    "content_hash": digest_a,
                    "content_ref": object_a,
                    "source_path": "/private/SKILL.md",
                    "target_step_id": "step-private",
                }],
            },
        }
        consultant_seed = {
            **common,
            "request_summary": "Annotate the supplied VCF.",
            "input_manifest": [{
                "input_id": "variants",
                "role": "variant_vcf",
                "path": "/work/input.vcf",
                "format": "vcf",
                "required": True,
                "availability": "available",
                "content_ref": object_a,
            }],
            "output_manifest": [{
                "output_id": "annotated",
                "role": "annotated_vcf",
                "path": "/work/annotated.vcf",
                "format": "vcf",
                "required": True,
                "path_policy": "exact",
                "root_ref": "output_dir",
                "content_ref": object_a,
            }],
            "authoritative_user_turn_revision": 5,
            "conversation_evidence_ref": object_a,
        }
        planner_seed = {
            **common,
            "phase": "planner",
            "consultant_archive_ref": "phase-archive:" + digest_a,
            "consultation_handoff": {
                "handoff_id": "handoff-private",
                "handoff_protocol_version": 1,
                "agent_initiated": True,
                "summary": "Inputs and outputs are resolved.",
                "goal_interpretation": "Annotate the supplied VCF.",
                "input_files": [{
                    "input_id": "variants",
                    "role": "variant_vcf",
                    "path": "/work/input.vcf",
                    "format": "vcf",
                    "required": True,
                    "availability": "available",
                    "content_ref": object_a,
                }],
                "required_outputs": [{
                    "output_id": "annotated",
                    "role": "annotated_vcf",
                    "path": "/work/annotated.vcf",
                    "format": "vcf",
                    "required": True,
                    "path_policy": "exact",
                    "root_ref": "output_dir",
                    "content_ref": object_a,
                }],
                "selected_skills": [
                    "genomics/snpeff-annotation",
                    {
                        "skill_id": "reporting/basic",
                        "scope": "task",
                        "snapshot_id": "snapshot-private",
                        "source_path": "/private/report/SKILL.md",
                    },
                ],
                "capability_decisions": ["Use the selected annotation Skill."],
                "runtime_requirements": ["Use the supplied FASTA and GTF."],
                "resolved_ambiguities": ["Use a custom database."],
                "unresolved_questions": ["Confirm organism label if needed."],
                "database_strategy": "Build from the supplied GTF.",
                "provider_response": "private raw response",
                "opaque_controller_token": object_a,
            },
        }
        consultant_before = deepcopy(consultant_seed)
        planner_before = deepcopy(planner_seed)

        consultant = project_phase_seed_for_provider(consultant_seed)
        planner = project_phase_seed_for_provider(planner_seed)

        self.assertEqual(consultant_seed, consultant_before)
        self.assertEqual(planner_seed, planner_before)
        self.assertEqual(consultant, {
            "schema_version": 1,
            "request_summary": "Annotate the supplied VCF.",
            "input_manifest": [{
                "input_id": "variants",
                "role": "variant_vcf",
                "path": "/work/input.vcf",
                "format": "vcf",
                "required": True,
                "availability": "available",
            }],
            "output_manifest": [{
                "output_id": "annotated",
                "role": "annotated_vcf",
                "path": "/work/annotated.vcf",
                "format": "vcf",
                "required": True,
                "path_policy": "exact",
                "root_ref": "output_dir",
            }],
            "selected_skill_manifest": {
                "manifest_only": True,
                "skills": [{
                    "skill_id": "genomics/snpeff-annotation",
                    "scope": "task",
                }],
            },
        })
        self.assertEqual(planner, {
            "schema_version": 1,
            "selected_skill_manifest": {
                "manifest_only": True,
                "skills": [{
                    "skill_id": "genomics/snpeff-annotation",
                    "scope": "task",
                }],
            },
            "consultation_handoff": {
                "summary": "Inputs and outputs are resolved.",
                "goal_interpretation": "Annotate the supplied VCF.",
                "input_files": [{
                    "input_id": "variants",
                    "role": "variant_vcf",
                    "path": "/work/input.vcf",
                    "format": "vcf",
                    "required": True,
                    "availability": "available",
                }],
                "required_outputs": [{
                    "output_id": "annotated",
                    "role": "annotated_vcf",
                    "path": "/work/annotated.vcf",
                    "format": "vcf",
                    "required": True,
                    "path_policy": "exact",
                    "root_ref": "output_dir",
                }],
                "selected_skills": [
                    "genomics/snpeff-annotation",
                    {"skill_id": "reporting/basic", "scope": "task"},
                ],
                "capability_decisions": ["Use the selected annotation Skill."],
                "runtime_requirements": ["Use the supplied FASTA and GTF."],
                "resolved_ambiguities": ["Use a custom database."],
                "unresolved_questions": ["Confirm organism label if needed."],
                "database_strategy": "Build from the supplied GTF.",
            },
        })
        messages = [
            HumanMessage(content=json.dumps(value, ensure_ascii=False))
            for value in (consultant, planner)
        ]
        assert_v2_llm_messages_clean(messages, stage="test.phase_seed")
        self.assertNotIn("sha256:", json.dumps(
            [consultant, planner], ensure_ascii=False
        ))

    async def test_full_seed_binds_identity_and_archive_but_not_provider_wire(
        self,
    ) -> None:
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        object_a = "object:sha256:" + "c" * 64
        object_b = "object:sha256:" + "d" * 64
        base = {
            "schema_version": 1,
            "phase": "consultant",
            "task_id": "task-1",
            "request_summary": "Annotate the supplied VCF.",
            "input_manifest": [{
                "role": "variant_vcf",
                "path": "/work/input.vcf",
                "content_ref": object_a,
            }],
            "selected_skill_bundle": {
                "skills": [{
                    "skill_id": "genomics/snpeff-annotation",
                    "scope": "task",
                    "snapshot_id": "snapshot-a",
                }],
            },
            "task_spec_ref": {
                "task_semantic_hash": digest_a,
                "task_record_hash": digest_a,
            },
            "conversation_evidence_ref": object_a,
        }
        seed_a = deepcopy(base)
        seed_b = deepcopy(base)
        seed_b["task_spec_ref"] = {
            "task_semantic_hash": digest_b,
            "task_record_hash": digest_b,
        }
        seed_b["conversation_evidence_ref"] = object_b
        seed_b["input_manifest"][0]["content_ref"] = object_b
        seed_b["selected_skill_bundle"]["skills"][0]["snapshot_id"] = (
            "snapshot-b"
        )
        wires: list[list[dict]] = []

        async def turn(state):
            context = phase_provider_context(state)
            wire = context.prepare_messages("stable policy", "semantic suffix")
            wires.append([provider_visible_payload(item) for item in wire])
            response = AIMessage(content="handoff")
            context.record_assistant_response(response)
            return {
                "messages": [response],
                "consultant_outcome": _consultant_outcome("planning_ready"),
            }

        with tempfile.TemporaryDirectory() as root:
            store = PhaseArchiveStore(root)

            def node_for(seed):
                return build_scoped_phase_node(
                    phase_name="consultant",
                    turn_handler=turn,
                    outcome_field="consultant_outcome",
                    outcome_contract=ConsultantOutcome,
                    continue_types=[],
                    seed_factory=lambda _state: deepcopy(seed),
                    provider_seed_projector=project_phase_seed_for_provider,
                    archive_writer=archive_writer_for_store(store),
                )

            result_a = await node_for(seed_a)({"task_id": "task-1"})
            result_b = await node_for(seed_b)({"task_id": "task-1"})

            audit_a = result_a["phase_context_audit"]["consultant"]
            audit_b = result_b["phase_context_audit"]["consultant"]
            epoch_a = audit_a["epochs"][0]
            epoch_b = audit_b["epochs"][0]
            self.assertEqual(wires[0], wires[1])
            self.assertEqual(
                epoch_a["provider_seed_contract_sha256"],
                epoch_b["provider_seed_contract_sha256"],
            )
            self.assertNotEqual(
                epoch_a["seed_contract_sha256"],
                epoch_b["seed_contract_sha256"],
            )
            self.assertNotEqual(audit_a["invocation_id"], audit_b["invocation_id"])
            ref_a = result_a["phase_archive_refs"]["consultant"]
            ref_b = result_b["phase_archive_refs"]["consultant"]
            self.assertNotEqual(ref_a, ref_b)
            record_a = store.read(ref_a).to_dict()
            record_b = store.read(ref_b).to_dict()
            self.assertEqual(record_a["metadata"]["seed_contract"], seed_a)
            self.assertEqual(record_b["metadata"]["seed_contract"], seed_b)
            transcript = json.dumps(
                [record_a["transcript"], record_b["transcript"]],
                ensure_ascii=False,
            )
            for private_value in (
                digest_a, digest_b, object_a, object_b,
                "snapshot-a", "snapshot-b",
            ):
                self.assertNotIn(private_value, transcript)

    async def test_provider_projection_boundary_fails_closed(self) -> None:
        digest = "sha256:" + "a" * 64
        turn_calls = 0

        async def turn(_state):
            nonlocal turn_calls
            turn_calls += 1
            return {"consultant_outcome": _consultant_outcome("planning_ready")}

        unsafe = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=[],
            seed_factory=lambda _state: {"task_semantic_hash": digest},
            provider_seed_projector=lambda seed: seed,
        )
        unsafe_result = await unsafe({"task_id": "task-1"})
        self.assertEqual(
            unsafe_result["consultant_outcome"]["type"], "harness_error"
        )
        self.assertEqual(
            unsafe_result["consultant_outcome"]["payload"]["error_code"],
            "LLM_CONTROL_METADATA_LEAK",
        )
        self.assertEqual(
            unsafe_result["consultant_outcome"]["payload"]["exception_class"],
            "ControlMetadataLeakError",
        )
        self.assertEqual(turn_calls, 0)

        with self.assertRaises(ControlMetadataLeakError):
            project_phase_seed_for_provider({
                "consultation_handoff": {
                    "runtime_requirements": [{
                        "content_ref": "object:sha256:" + "b" * 64,
                    }],
                },
            })

        invalid = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=[],
            seed_factory=lambda _state: {"request_summary": "safe"},
            provider_seed_projector=lambda _seed: [],
        )
        invalid_result = await invalid({"task_id": "task-1"})
        self.assertEqual(
            invalid_result["consultant_outcome"]["type"], "harness_error"
        )
        self.assertEqual(
            invalid_result["consultant_outcome"]["payload"]["error_code"],
            "PHASE_PROVIDER_SEED_INVALID",
        )
        self.assertEqual(turn_calls, 0)

        bubble = GraphBubbleUp()

        def bubbling_projector(_seed):
            raise bubble

        bubbling = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=[],
            seed_factory=lambda _state: {"request_summary": "safe"},
            provider_seed_projector=bubbling_projector,
        )
        with self.assertRaises(GraphBubbleUp) as raised:
            await bubbling({"task_id": "task-1"})
        self.assertIs(raised.exception, bubble)
        self.assertEqual(turn_calls, 0)

        cancellation = asyncio.CancelledError()

        def cancelling_projector(_seed):
            raise cancellation

        cancelling = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=[],
            seed_factory=lambda _state: {"request_summary": "safe"},
            provider_seed_projector=cancelling_projector,
        )
        with self.assertRaises(asyncio.CancelledError) as cancelled:
            await cancelling({"task_id": "task-1"})
        self.assertIs(cancelled.exception, cancellation)
        self.assertEqual(turn_calls, 0)

        with self.assertRaisesRegex(
            PhaseLoopError, "PHASE_PROVIDER_SEED_INVALID"
        ):
            PhaseProviderContext(
                phase_name="consultant",
                invocation_id="invocation",
                epoch_ordinal=1,
                provider_seed_contract=None,  # type: ignore[arg-type]
                system_message=None,
                seed_message=None,
                messages=[],
            )

    async def test_mutating_projector_cannot_change_private_seed_identity(
        self,
    ) -> None:
        digest = "sha256:" + "a" * 64
        seed = {
            "task_semantic_hash": digest,
            "request_summary": "safe semantics",
        }

        def mutating_projector(value):
            value.clear()
            return {"schema_version": 1, "request_summary": "safe semantics"}

        async def turn(state):
            context = phase_provider_context(state)
            context.prepare_messages("policy", "suffix")
            response = AIMessage(content="done")
            context.record_assistant_response(response)
            return {
                "messages": [response],
                "consultant_outcome": _consultant_outcome("planning_ready"),
            }

        with tempfile.TemporaryDirectory() as root:
            store = PhaseArchiveStore(root)

            def make_node(projector):
                return build_scoped_phase_node(
                    phase_name="consultant",
                    turn_handler=turn,
                    outcome_field="consultant_outcome",
                    outcome_contract=ConsultantOutcome,
                    continue_types=[],
                    seed_factory=lambda _state: deepcopy(seed),
                    provider_seed_projector=projector,
                    archive_writer=archive_writer_for_store(store),
                )

            mutated = await make_node(mutating_projector)({"task_id": "task-1"})
            baseline = await make_node(project_phase_seed_for_provider)(
                {"task_id": "task-1"}
            )
            mutated_audit = mutated["phase_context_audit"]["consultant"]
            baseline_audit = baseline["phase_context_audit"]["consultant"]
            self.assertEqual(
                mutated_audit["invocation_id"], baseline_audit["invocation_id"]
            )
            record = store.read(
                mutated["phase_archive_refs"]["consultant"]
            ).to_dict()
            self.assertEqual(record["metadata"]["seed_contract"], seed)
            self.assertEqual(seed["task_semantic_hash"], digest)

    def test_planner_skill_seed_is_manifest_only(self) -> None:
        source = {
            "selected_skill_refs": [{"skill_id": "genomics/example"}],
            "available_skill_headings": [{
                "skill_id": "genomics/example",
                "heading_index": ["Purpose", "Inputs"],
            }],
            "relevant_skill_excerpts": [{
                "excerpt_id": "excerpt-1",
                "exact_text": "large document body",
            }],
            "skill_tool_results": [{
                "results": [{"exact_text": "another large document body"}],
            }],
        }

        projected = manifest_only_skill_context(source)

        self.assertEqual(
            projected["selected_skill_refs"],
            [{"skill_id": "genomics/example"}],
        )
        self.assertEqual(projected["relevant_skill_excerpts"], [])
        self.assertEqual(projected["skill_tool_results"], [])
        self.assertIn("large document body", str(source))

    async def test_seed_contract_rejects_transcript_leakage_before_provider_call(
        self,
    ) -> None:
        async def turn(_state):  # pragma: no cover - boundary rejects first
            raise AssertionError("provider turn must not run")

        node = build_scoped_phase_node(
            phase_name="planner",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=[],
            seed_factory=lambda _state: {
                "task_id": "task-1",
                "messages": [{"role": "user", "content": "leak"}],
            },
            provider_seed_projector=project_phase_seed_for_provider,
        )

        with self.assertRaisesRegex(
            StageContractError, "STAGE_CONTEXT_LEAK_FORBIDDEN"
        ):
            await node({"task_id": "task-1", "messages": []})

    async def test_parent_projector_is_an_allowlist_boundary(self) -> None:
        async def turn(state):
            self.assertEqual(state["task_id"], "task-1")
            self.assertNotIn("foreign_phase_payload", state)
            context = phase_provider_context(state)
            context.prepare_messages("policy", "planner input")
            response = AIMessage(content="done")
            context.record_assistant_response(response)
            return {
                "messages": [response],
                "consultant_outcome": _consultant_outcome("planning_ready"),
            }

        node = build_scoped_phase_node(
            phase_name="planner",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=[],
            seed_factory=lambda state: {"task_id": state["task_id"]},
            provider_seed_projector=project_phase_seed_for_provider,
            parent_projector=lambda state: {"task_id": state["task_id"]},
        )

        result = await node({
            "task_id": "task-1",
            "messages": [HumanMessage(content="global history")],
            "foreign_phase_payload": {"large": "must not cross"},
        })

        self.assertNotIn("foreign_phase_payload", result)
        self.assertNotIn("messages", result)

    async def test_local_turns_keep_a_stable_prefix_and_do_not_escape(self) -> None:
        provider_wires: list[list[dict]] = []

        async def turn(state):
            context = phase_provider_context(state)
            self.assertIsNotNone(context)
            ordinal = int(state.get("fake_turn") or 0)
            wire = context.prepare_messages(
                "stable consultant policy",
                "initial semantic seed" if ordinal == 0 else "rebuilt volatile seed",
            )
            provider_wires.append([provider_visible_payload(item) for item in wire])
            response = AIMessage(
                id=f"provider-{ordinal}",
                content="SEARCH" if ordinal == 0 else "HANDOFF",
                response_metadata={"latency_ms": 2 + ordinal},
            )
            context.record_assistant_response(response)
            if ordinal == 0:
                return {
                    "fake_turn": 1,
                    "messages": [
                        AIMessage(content="normalized SEARCH"),
                        SystemMessage(content="skill.search result"),
                    ],
                    "consultant_outcome": _consultant_outcome(
                        "consultation_continues"
                    ),
                }
            return {
                "fake_turn": 2,
                "messages": [AIMessage(content="normalized HANDOFF")],
                "consultation_answer": {
                    "answer_id": "answer-public",
                    "answer": "bounded public result",
                },
                "consultant_outcome": _consultant_outcome("planning_ready"),
            }

        node = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=["consultation_continues"],
            seed_factory=lambda state: {
                "task_id": state["task_id"],
                "task_revision": 0,
            },
            provider_seed_projector=project_phase_seed_for_provider,
        )
        result = await node({
            "task_id": "task-1",
            "messages": [HumanMessage(id="outer-user", content="outer history")],
        })

        self.assertEqual(result["consultant_outcome"]["type"], "planning_ready")
        self.assertNotIn("messages", result)
        self.assertEqual(
            result["consultation_answer"]["answer"], "bounded public result"
        )
        self.assertEqual(len(provider_wires), 2)
        self.assertEqual(provider_wires[0], provider_wires[1][: len(provider_wires[0])])
        self.assertIn(
            '<phase_seed_contract schema_version="1">',
            provider_wires[1][1]["content"],
        )
        self.assertIn("initial semantic seed", provider_wires[1][1]["content"])
        self.assertNotIn("rebuilt volatile seed", provider_wires[1][1]["content"])
        self.assertNotIn("outer history", str(provider_wires))
        audit = result["phase_context_audit"]["consultant"]
        self.assertFalse(audit["transcript_in_parent_state"])
        self.assertEqual(audit["epochs"][0]["turn_count"], 2)
        self.assertEqual(audit["epochs"][0]["provider_call_count"], 2)

    async def test_changed_system_policy_starts_a_fresh_epoch(self) -> None:
        provider_wires: list[list[dict]] = []

        async def turn(state):
            context = phase_provider_context(state)
            ordinal = int(state.get("fake_turn") or 0)
            system = "policy-a" if ordinal == 0 else "policy-b"
            wire = context.prepare_messages(system, f"seed-{ordinal}")
            provider_wires.append([provider_visible_payload(item) for item in wire])
            response = AIMessage(content=f"response-{ordinal}")
            context.record_assistant_response(response)
            return {
                "fake_turn": ordinal + 1,
                "messages": [response],
                "consultant_outcome": _consultant_outcome(
                    "consultation_continues" if ordinal == 0 else "planning_ready"
                ),
            }

        node = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=["consultation_continues"],
            seed_factory=lambda state: {"task_id": state["task_id"]},
            provider_seed_projector=project_phase_seed_for_provider,
            max_epochs=2,
        )
        result = await node({"task_id": "task-1", "messages": []})

        self.assertEqual(result["consultant_outcome"]["type"], "planning_ready")
        audit = result["phase_context_audit"]["consultant"]
        self.assertEqual(audit["epoch_count"], 2)
        self.assertEqual(
            audit["epochs"][0]["boundary_reason"], "system_policy_changed"
        )
        self.assertEqual(provider_wires[-1][0]["content"], "policy-b")
        self.assertIn("seed-1", provider_wires[-1][1]["content"])
        self.assertNotIn('"task_id":"task-1"', provider_wires[-1][1]["content"])

    async def test_local_turn_limit_fails_closed(self) -> None:
        async def turn(state):
            context = phase_provider_context(state)
            context.prepare_messages("policy", "seed")
            response = AIMessage(content="continue")
            context.record_assistant_response(response)
            return {
                "messages": [response],
                "consultant_outcome": _consultant_outcome(
                    "consultation_continues"
                ),
            }

        node = build_scoped_phase_node(
            phase_name="consultant",
            turn_handler=turn,
            outcome_field="consultant_outcome",
            outcome_contract=ConsultantOutcome,
            continue_types=["consultation_continues"],
            seed_factory=lambda state: {"task_id": state["task_id"]},
            provider_seed_projector=project_phase_seed_for_provider,
            max_turns=2,
        )
        result = await node({"task_id": "task-1", "messages": []})

        self.assertEqual(result["consultant_outcome"]["type"], "harness_error")
        self.assertEqual(
            result["consultant_outcome"]["payload"]["error_code"],
            "PHASE_LOCAL_TURN_LIMIT",
        )

    async def test_completed_epoch_is_inspectable_without_leaking_transcript(self) -> None:
        async def turn(state):
            context = phase_provider_context(state)
            context.prepare_messages("policy", "seed with task facts")
            response = AIMessage(content="handoff")
            context.record_assistant_response(response)
            return {
                "messages": [response],
                "consultant_outcome": _consultant_outcome("planning_ready"),
            }

        with tempfile.TemporaryDirectory() as root:
            store = PhaseArchiveStore(root)
            node = build_scoped_phase_node(
                phase_name="consultant",
                turn_handler=turn,
                outcome_field="consultant_outcome",
                outcome_contract=ConsultantOutcome,
                continue_types=["consultation_continues"],
                seed_factory=lambda state: {"task_id": state["task_id"]},
                provider_seed_projector=project_phase_seed_for_provider,
                archive_writer=archive_writer_for_store(store),
            )
            result = await node({"task_id": "task-1", "messages": []})

            archive_ref = result["phase_archive_refs"]["consultant"]
            default_view = store.inspect(archive_ref)
            self.assertNotIn("transcript", default_view)
            transcript_view = store.inspect(
                archive_ref, section="transcript", offset=0, limit=2
            )
            self.assertEqual(transcript_view["page"]["returned"], 2)
            self.assertEqual(
                transcript_view["transcript"][0]["content"], "policy"
            )

    async def test_replayed_logical_invocation_keeps_distinct_immutable_attempts(
        self,
    ) -> None:
        responses = iter(("first answer", "changed replay answer"))

        async def turn(state):
            context = phase_provider_context(state)
            context.prepare_messages("policy", "seed")
            response = AIMessage(content=next(responses))
            context.record_assistant_response(response)
            return {
                "messages": [response],
                "consultant_outcome": _consultant_outcome("planning_ready"),
            }

        with tempfile.TemporaryDirectory() as root:
            store = PhaseArchiveStore(root)
            node = build_scoped_phase_node(
                phase_name="consultant",
                turn_handler=turn,
                outcome_field="consultant_outcome",
                outcome_contract=ConsultantOutcome,
                continue_types=[],
                seed_factory=lambda state: {"task_id": state["task_id"]},
                provider_seed_projector=project_phase_seed_for_provider,
                archive_writer=archive_writer_for_store(store),
            )
            first = await node({"task_id": "task-1", "messages": []})
            replay = await node({"task_id": "task-1", "messages": []})

            first_ref = first["phase_archive_refs"]["consultant"]
            replay_ref = replay["phase_archive_refs"]["consultant"]
            self.assertNotEqual(first_ref, replay_ref)
            first_record = store.read(first_ref)
            replay_record = store.read(replay_ref)
            self.assertEqual(
                first_record.metadata["phase_invocation_id"],
                replay_record.metadata["phase_invocation_id"],
            )
            self.assertNotEqual(
                first_record.metadata["archive_attempt_digest"],
                replay_record.metadata["archive_attempt_digest"],
            )


if __name__ == "__main__":
    unittest.main()
