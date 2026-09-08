"""Offline acceptance coverage for the production schema-2 Generate wiring."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

import bio_agents
from agent_core.plan_tracker import (
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    canonical_task_spec_for_state,
    load_plan_state,
    save_plan_state,
)
from agent_core.skill_access import (
    SkillAccessService,
    new_skill_access_state,
    record_provider_visible_materials,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec
from agent_core.workflow.generate_handoff import (
    GenerateReasoningEpisode,
    freeze_generate_provider_request,
    retry_generate_provider_request,
)
from agent_core.workflow.context_compaction import ContextCompactionError
from agent_core.workflow.phase_compaction import PhaseCompactionResult, PhaseContextCompactor
from agent_core.workflow.scoped_context import provider_visible_payload
from bio_agents import BioAnalystAgent, GENERATE_CONTEXT_SYSTEM_V1


SKILL_A = "fixture/generate-a"
SKILL_B = "fixture/generate-b"
CAPABILITY_ID = "fixture.generate_a.observe"
SKILL_A_SENTINEL = "EXACT_GENERATE_SKILL_A_SENTINEL"
SKILL_B_SENTINEL = "EXACT_GENERATE_SKILL_B_SENTINEL"


class _RecordingProvider:
    """Small LangChain-shaped provider with an optional transient first result."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.empty_first = False
        self.on_first_call: Callable[[], None] | None = None
        self.response_text = "<shell>printf 'offline generate fixture\\n'</shell>"

    def bind(self, **_kwargs: Any) -> "_RecordingProvider":
        return self

    def model_copy(self, update: Any = None) -> "_RecordingProvider":
        del update
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage], **_kwargs: Any) -> AIMessage:
        self.calls.append([provider_visible_payload(item) for item in messages])
        if len(self.calls) == 1 and self.on_first_call is not None:
            self.on_first_call()
        if self.empty_first and len(self.calls) == 1:
            return AIMessage(content="")
        return AIMessage(content=self.response_text)


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    marker: str,
    with_capability: bool,
) -> None:
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    generate = ""
    if with_capability:
        generate = (
            "generate:\n"
            "  execution_capabilities:\n"
            f"    - capability_id: {CAPABILITY_ID}\n"
            "      operation: observe\n"
            "      required_runtime_binding_ids: []\n"
            "      optional_runtime_binding_ids: []\n"
            "      required_artifact_binding_ids: []\n"
            "      consumes_artifacts: []\n"
            "      produces_artifacts: []\n"
            "      produces_bindings: []\n"
            "      produces_observations: [fixture_observed]\n"
            "      workspace_mutations: []\n"
            "      explicit_side_effects: []\n"
            "      execution_guidance:\n"
            "        - Emit one harmless offline shell action.\n"
        )
    text = (
        "---\n"
        f"name: {skill_id}\n"
        f"description: Offline production Generate fixture for {skill_id}\n"
        f"{generate}"
        "---\n"
        "# Procedure\n"
        f"{marker}\n"
        "Preserve this exact source body in provider context.\n"
    )
    (directory / "SKILL.md").write_text(text, encoding="utf-8")


class ProductionGenerateContextWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="biomind-generate-wiring-")
        self.root = Path(self.temporary.name)
        self.skills_root = self.root / "skills"
        _write_skill(
            self.skills_root,
            SKILL_A,
            marker=SKILL_A_SENTINEL,
            with_capability=True,
        )
        _write_skill(
            self.skills_root,
            SKILL_B,
            marker=SKILL_B_SENTINEL,
            with_capability=False,
        )
        self.provider = _RecordingProvider()
        self.patches = [
            patch.object(bio_agents, "init_chat_model", return_value=self.provider),
            patch.object(bio_agents, "BIOMIND_MEMORY_DIR", str(self.root / "memory")),
            patch.object(bio_agents, "BIOMIND_WIKI_ENABLED", False),
            patch.object(bio_agents, "ENABLE_ROOT_CAUSE_ANALYZER", False),
            patch.object(bio_agents, "SCHEMA2_CONTEXT_COMPACTION_ENABLED", False),
        ]
        for item in self.patches:
            item.start()
        self.agent = BioAnalystAgent(
            data_path=str(self.root),
            enable_persistence=False,
            use_llm_supervisor=False,
        )
        self.manager = SkillManager(str(self.skills_root))
        self.service = SkillAccessService(self.manager)
        self.agent.skill_manager = self.manager
        self.agent.skill_access_service = self.service

        async def no_observe(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def no_compress(messages: Sequence[BaseMessage], _task_id: str):
            return list(messages), False

        async def no_sleep(_seconds: float) -> None:
            return None

        self.agent._observe_session_memory = no_observe
        self.agent._finalize_task_memory = no_observe
        self.agent.memory_manager.maybe_compress = no_compress
        self.agent._provider_retry_sleep = no_sleep
        self.agent._provider_retry_random = lambda: 0.0
        self.agent.generate_provider_retry_base_seconds = 0.0
        self.agent.generate_provider_retry_cap_seconds = 0.0

    async def asyncTearDown(self) -> None:
        await self.agent.aclose(timeout_seconds=0.2)
        for item in reversed(self.patches):
            item.stop()
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None
        self.temporary.cleanup()

    def _create_task(self, task_id: str) -> dict[str, Any]:
        submission = "Generate the approved offline action."
        bootstrap, _is_continuation, _raw_ref = self.agent._persist_user_submission(
            task_id,
            submission,
        )
        task_spec = create_task_spec(
            "Generate one harmless offline action from the approved fixture skill.",
            task_id=task_id,
            base_dir=str(self.root),
            project_root=str(self.root),
        )
        candidate = {
            "steps": [{
                "step_id": "step-generate",
                "title": "Generate offline fixture action",
                "action": "execute",
                "purpose": "analysis",
                "tool": "printf",
                "execution_capability_id": CAPABILITY_ID,
                "inputs": [],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["a harmless action is generated"],
                "skill_ids": [SKILL_A],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
            }],
            "validation_checklist": [{
                "check_id": "check-generated",
                "description": "the generated action satisfies the approved capability",
            }],
            "metadata": {
                "schema_version": 2,
                "planner_contract": "structured_plan_v2",
            },
        }
        durable = build_plan_state(task_id, json.dumps(candidate), strict=True)
        durable["state_generation"] = int(bootstrap.get("state_generation") or 0)
        durable["current_task"] = {"task_spec": task_spec}
        durable["conversation_evidence"] = deepcopy(
            bootstrap.get("conversation_evidence") or {}
        )
        durable["audit_index"] = deepcopy(bootstrap.get("audit_index") or {})
        durable["selected_skill_ids"] = [SKILL_A, SKILL_B]
        durable = self.manager.bind_plan_execution_capability_contracts(
            durable,
            [SKILL_A, SKILL_B],
        )
        bind_plan_to_task_revision(durable, task_spec["revision"])
        approve_plan_state(durable, task_spec["revision"])

        access = new_skill_access_state()
        for ordinal, skill_id in enumerate((SKILL_A, SKILL_B), start=1):
            access, snapshot, _ = self.service.ensure_snapshot(
                access,
                skill_id,
                scope="task",
                status="active",
                selected_at_task_revision=task_spec["revision"],
            )
            access, _documents = self.service.load_skill_documents(
                access,
                [skill_id],
                task_revision=task_spec["revision"],
                request_id=f"consultant-load-{ordinal}",
            )
            access = record_provider_visible_materials(
                access,
                [{
                    "kind": "skill_document",
                    "snapshot_id": snapshot["snapshot_id"],
                    "content_hash": snapshot["content_hash"],
                }],
                phase="consultant",
                invocation_id="consultant-fixture",
                event_id=f"consultant-visible-{ordinal}",
            )
        task_state = dict(durable.get("task_state") or {})
        task_state.update({
            "status": "running",
            "workflow_routing_schema_version": 2,
            "selected_skills": [SKILL_A, SKILL_B],
            "skill_access_state": access,
        })
        durable["task_state"] = task_state
        durable["workflow_routing_schema_version"] = 2
        save_plan_state(self.root, task_id, durable)
        return self._graph_state(task_id)

    def _graph_state(self, task_id: str) -> dict[str, Any]:
        durable = load_plan_state(self.root, task_id)
        task_spec = canonical_task_spec_for_state(durable)
        return {
            "messages": [HumanMessage(content="Generate the approved offline action.")],
            "task_id": task_id,
            "session_id": task_id,
            "project_id": self.agent.default_project_id,
            "task_mode": "managed",
            "task_spec": task_spec,
            "task_revision": task_spec["revision"],
            "current_plan": json.dumps({"steps": durable.get("steps")}),
            "current_step_id": durable["current_step_id"],
            "selected_skills": [SKILL_A, SKILL_B],
            "active_skills": [SKILL_A],
            "future_skills": [],
            "workflow_routing_schema_version": 2,
            "resolved_bindings": {},
        }

    async def _invoke(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return await self.agent._graph_node_handlers["generate"](deepcopy(dict(state)))

    async def test_same_basis_keeps_prefix_and_appends_candidate_then_feedback(self) -> None:
        state = self._create_task("generate-two-rounds")

        first_result = await self._invoke(state)
        self.assertEqual(
            first_result["generate_result"],
            "GENERATED_ACTION",
            msg=first_result,
        )
        self.assertEqual(first_result["pending_action"]["operation"], "observe")
        self.assertEqual(
            first_result["pending_action"]["execution_capability_id"],
            CAPABILITY_ID,
        )
        self.assertEqual(len(self.provider.calls), 1)
        first_wire = self.provider.calls[0]
        self.assertEqual([item["type"] for item in first_wire[:4]], [
            "system", "human", "human", "human",
        ])
        self.assertEqual(first_wire[0]["content"], GENERATE_CONTEXT_SYSTEM_V1)
        self.assertIn(SKILL_A_SENTINEL, str(first_wire[2]["content"]))
        self.assertIn(SKILL_B_SENTINEL, str(first_wire[3]["content"]))

        after_first = load_plan_state(self.root, "generate-two-rounds")
        first_episode = GenerateReasoningEpisode.from_mapping(
            after_first["task_state"]["generate_context_episode"]
        )
        self.assertEqual(
            [item.event_kind for item in first_episode.local_events],
            ["candidate_response"],
        )

        second_state = {
            **state,
            "last_supervisor_feedback": {
                "assessment": "invalid action representation",
                "required_fix": "emit the approved harmless action again",
            },
        }
        second_result = await self._invoke(second_state)
        self.assertEqual(second_result["generate_result"], "GENERATED_ACTION")
        self.assertEqual(len(self.provider.calls), 2)
        second_wire = self.provider.calls[1]
        self.assertEqual(second_wire[:4], first_wire[:4])
        self.assertEqual(second_wire[4]["type"], "ai")
        self.assertIn("offline generate fixture", str(second_wire[4]["content"]))
        self.assertEqual(second_wire[5]["type"], "human")
        self.assertIn("generate_validator_feedback", str(second_wire[5]["content"]))
        self.assertIn("required_fix", str(second_wire[5]["content"]))

        after_second = load_plan_state(self.root, "generate-two-rounds")
        second_episode = GenerateReasoningEpisode.from_mapping(
            after_second["task_state"]["generate_context_episode"]
        )
        self.assertEqual(second_episode.basis, first_episode.basis)
        self.assertEqual(second_episode.reasoning_episode_id, first_episode.reasoning_episode_id)
        self.assertEqual(
            [item.event_kind for item in second_episode.local_events],
            ["candidate_response", "validator_feedback", "candidate_response"],
        )

    async def test_skill_result_keeps_exact_material_only_in_message_metadata(self) -> None:
        state = self._create_task("generate-skill-result-metadata")
        self.provider.response_text = json.dumps({
            "decision": "REQUEST_SKILL_CONTEXT",
            "reason": "Need the exact fixture procedure.",
            "requests": [{
                "skill_id": SKILL_A,
                "operation": "section",
                "heading": "Procedure",
            }],
        })

        result = await self._invoke(state)

        self.assertEqual(result["generate_result"], "SKILL_CONTINUATION")
        result_message = result["messages"][-1]
        records = result_message.additional_kwargs[
            "schema2_skill_material_records"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["skill_id"], SKILL_A)
        self.assertEqual(records[0]["residency"], "exact")
        self.assertIn(SKILL_A_SENTINEL, records[0]["body"])
        self.assertNotIn(
            "schema2_skill_material_records",
            result["skill_decision_result"],
        )
        self.assertNotIn(
            "schema2_skill_material_records",
            provider_visible_payload(result_message),
        )

    async def test_transport_retry_reuses_the_exact_frozen_provider_request(self) -> None:
        self.provider.empty_first = True
        state = self._create_task("generate-transport-retry")
        generation_before = int(
            load_plan_state(self.root, "generate-transport-retry").get(
                "state_generation"
            ) or 0
        )
        dispatched_generations: list[int] = []
        self.provider.on_first_call = lambda: dispatched_generations.append(
            int(
                load_plan_state(self.root, "generate-transport-retry").get(
                    "state_generation"
                ) or 0
            )
        )

        result = await self._invoke(state)

        self.assertEqual(result["generate_result"], "GENERATED_ACTION")
        self.assertGreater(dispatched_generations[0], generation_before)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.provider.calls[1], self.provider.calls[0])
        durable = load_plan_state(self.root, "generate-transport-retry")
        episode = GenerateReasoningEpisode.from_mapping(
            durable["task_state"]["generate_context_episode"]
        )
        provider_episode = episode.to_dict()
        candidate = provider_episode["local_events"].pop()
        frozen = freeze_generate_provider_request(
            GenerateReasoningEpisode.from_mapping(provider_episode)
        )
        retried = retry_generate_provider_request(frozen)
        self.assertIs(retried, frozen)
        self.assertEqual(retried.provider_payloads(), self.provider.calls[1])
        self.assertEqual(
            candidate["event_id"],
            "generate-candidate:" + frozen.request_digest,
        )
        attempts = self.agent._provider_attempt_history[
            "generate-transport-retry"
        ]
        self.assertEqual(len(attempts), 2)
        for field in ("request_hash", "prompt_hash", "node_context_hash"):
            self.assertEqual(attempts[0][field], attempts[1][field])

    async def test_transport_retry_stops_after_external_authority_change(self) -> None:
        self.provider.empty_first = True
        state = self._create_task("generate-transport-stale")

        def replace_current_attempt() -> None:
            durable = load_plan_state(self.root, "generate-transport-stale")
            task_state = dict(durable.get("task_state") or {})
            task_state["current_attempt"] = "externally-replaced-attempt"
            durable["task_state"] = task_state
            save_plan_state(self.root, "generate-transport-stale", durable)

        self.provider.on_first_call = replace_current_attempt
        result = await self._invoke(state)

        self.assertEqual(result["generate_result"], "BLOCKED")
        self.assertEqual(result["generate_exit_reason"], "provider_attempt_terminal")
        self.assertEqual(len(self.provider.calls), 1)
        durable = load_plan_state(self.root, "generate-transport-stale")
        self.assertEqual(
            durable["failure"]["error_code"],
            "PROVIDER_CANONICAL_CONTEXT_CHANGED",
        )

    async def test_transport_retry_stops_after_selected_snapshot_change(self) -> None:
        self.provider.empty_first = True
        state = self._create_task("generate-transport-selection-stale")

        def supersede_selected_snapshot() -> None:
            durable = load_plan_state(
                self.root, "generate-transport-selection-stale"
            )
            task_state = dict(durable.get("task_state") or {})
            access = deepcopy(task_state["skill_access_state"])
            for item in access["selected_skill_refs"]:
                if item["skill_id"] == SKILL_B:
                    item["status"] = "superseded"
            task_state["skill_access_state"] = access
            durable["task_state"] = task_state
            save_plan_state(
                self.root, "generate-transport-selection-stale", durable
            )

        self.provider.on_first_call = supersede_selected_snapshot
        result = await self._invoke(state)

        self.assertEqual(result["generate_result"], "BLOCKED")
        self.assertEqual(len(self.provider.calls), 1)
        durable = load_plan_state(
            self.root, "generate-transport-selection-stale"
        )
        self.assertEqual(
            durable["failure"]["error_code"],
            "PROVIDER_CANONICAL_CONTEXT_CHANGED",
        )

    async def test_committed_compaction_advances_epoch_and_records_only_exact_resident_material(self) -> None:
        state = self._create_task("generate-capacity-epoch")
        compaction_call: dict[str, Any] = {}

        async def compact_generate(
            _compactor: PhaseContextCompactor,
            **kwargs: Any,
        ) -> PhaseCompactionResult:
            compaction_call.update(kwargs)
            inherited = list(kwargs["inherited_messages"])
            descriptors = list(kwargs["material_descriptors"])
            retained = next(
                message for message in inherited
                if SKILL_A_SENTINEL in str(message.content)
            )
            residency: dict[str, dict[str, Any]] = {}
            for descriptor in descriptors:
                material_id = str(descriptor["material_id"])
                if descriptor["skill_id"] == SKILL_A:
                    residency[material_id] = {"residency": "exact"}
                else:
                    residency[material_id] = {
                        "residency": "summarized",
                        "content_hash": descriptor["content_hash"],
                        "summary": "Skill B was externalized by the offline fixture.",
                        "missing_details": "Exact Skill B source body.",
                        "retrieval_ref": descriptor["source_ref"],
                        "compaction_request_id": "compaction-fixture",
                    }
            summary = HumanMessage(
                id="generate-compaction-summary-fixture",
                content="<context_compaction_summary>Skill B externalized.</context_compaction_summary>",
            )
            messages = (
                kwargs["system_message"],
                kwargs["seed_message"],
                retained,
                summary,
            )
            telemetry = {
                "compaction_triggered": True,
                "compaction_committed": True,
                "source_archive_ref": "phase-archive://generate/source-fixture",
                "candidate_epoch_archive_ref": "phase-archive://generate/epoch-fixture",
                "material_residency": residency,
            }
            return PhaseCompactionResult(
                messages=messages,
                state={"schema_version": 1},
                telemetry=telemetry,
                triggered=True,
            )

        with (
            patch.object(bio_agents, "SCHEMA2_CONTEXT_COMPACTION_ENABLED", True),
            patch.object(PhaseContextCompactor, "prepare", new=compact_generate),
        ):
            result = await self._invoke(state)

        self.assertEqual(result["generate_result"], "GENERATED_ACTION")
        self.assertEqual(compaction_call["phase"], "generate")
        self.assertEqual(len(compaction_call["inherited_messages"]), 2)
        self.assertEqual(len(self.provider.calls), 1)
        final_wire = self.provider.calls[0]
        rendered = "\n".join(str(item["content"]) for item in final_wire)
        self.assertIn(SKILL_A_SENTINEL, rendered)
        self.assertNotIn(SKILL_B_SENTINEL, rendered)
        self.assertIn("Skill B externalized", rendered)

        durable = load_plan_state(self.root, "generate-capacity-epoch")
        episode = GenerateReasoningEpisode.from_mapping(
            durable["task_state"]["generate_context_episode"]
        )
        self.assertEqual(episode.context_epoch, 1)
        self.assertEqual(episode.transition.value, "same_step_validator_append")
        self.assertEqual(
            episode.previous_context_archive_ref,
            "phase-archive://generate/source-fixture",
        )
        self.assertEqual(len(episode.inherited_materials), 1)
        self.assertEqual(
            [item.event_kind for item in episode.local_events],
            ["context_compaction_summary", "candidate_response"],
        )

        access = durable["task_state"]["skill_access_state"]
        records = list(access["provider_visible_materials"].values())
        record_a = next(item for item in records if item["skill_id"] == SKILL_A)
        record_b = next(item for item in records if item["skill_id"] == SKILL_B)
        generate_events_a = [
            item for item in record_a["visibility_events"]
            if item["phase"] == "generate"
        ]
        generate_events_b = [
            item for item in record_b["visibility_events"]
            if item["phase"] == "generate"
        ]
        self.assertEqual(len(generate_events_a), 1)
        self.assertEqual(generate_events_b, [])
        provider_episode = episode.to_dict()
        provider_episode["transition"] = "capacity_epoch"
        provider_episode["local_events"] = provider_episode["local_events"][:-1]
        provider_request = freeze_generate_provider_request(
            GenerateReasoningEpisode.from_mapping(provider_episode)
        )
        self.assertEqual(
            generate_events_a[0]["event_id"],
            (
                f"{episode.context_epoch_id}:provider-request:"
                f"{provider_request.request_digest}"
            ),
        )

    async def test_typed_compaction_failure_does_not_rebuild_the_same_source_epoch(self) -> None:
        state = self._create_task("generate-compaction-bounded-failure")
        compaction_calls = 0

        async def fail_compaction(
            _compactor: PhaseContextCompactor,
            **_kwargs: Any,
        ) -> PhaseCompactionResult:
            nonlocal compaction_calls
            compaction_calls += 1
            raise ContextCompactionError(
                "CONTEXT_COMPACTION_ATTEMPTS_EXHAUSTED",
                "the fixed source epoch already consumed its bounded attempts",
            )

        with (
            patch.object(bio_agents, "SCHEMA2_CONTEXT_COMPACTION_ENABLED", True),
            patch.object(PhaseContextCompactor, "prepare", new=fail_compaction),
        ):
            result = await self._invoke(state)

        self.assertEqual(compaction_calls, 1)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(result["generate_result"], "BLOCKED_INTERNAL_PROTOCOL")
        self.assertEqual(result["generate_exit_reason"], "prompt_assembly_terminal")
        self.assertIn(
            "CONTEXT_COMPACTION_ATTEMPTS_EXHAUSTED",
            str(result["primary_failure"].get("message") or ""),
        )


if __name__ == "__main__":
    unittest.main()
