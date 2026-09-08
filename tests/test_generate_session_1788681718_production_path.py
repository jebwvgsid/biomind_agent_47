"""Offline production-path regression for session_1788681718 Generate admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

import bio_agents
from agent_core.plan_tracker import (
    advance_plan_after_verified_step,
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    canonical_task_spec_for_state,
    commit_recovery_bundle,
    load_plan_state,
    plan_approval_is_current,
    record_provenance_event,
    rollover_recovery_generation_claim_after_candidate_rejection,
    save_plan_state,
    semantic_plan_hash,
    update_recovery_generation_claim,
    validate_plan_for_generate,
)
from agent_core.recovery import (
    RecoveryAction,
    RecoveryCommitBundle,
    RecoveryDecision,
    RecoverySourceScope,
    RecoveryStateStore,
    build_recovery_execution_envelope,
    finalize_recovery_decision,
)
from agent_core.skill_access import (
    SkillAccessService,
    new_skill_access_state,
    record_provider_visible_materials,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec, merge_task_spec
from agent_core.workflow.context_compaction import ContextBudgetPolicy
from agent_core.workflow.execution import TypedExecute
from agent_core.workflow.outcomes import ExecuteOutcome
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder,
    bind_causal_failure_node,
)
from agent_core.workflow.recovery_runtime import RecoveryRuntime
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.scoped_context import provider_visible_payload
from bio_agents import BioAnalystAgent


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "session_1788681718_generate_candidate.json"
)
PLAN_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "session_1788681718_approved_plan.json"
)
SKILL_ID = "genomics/snpeff-annotation"
CAPABILITY_ID = "genomics.snpeff_annotation.check_contig_compatibility"


class _FrozenResponseProvider:
    """LangChain-shaped fake that returns only explicitly queued responses."""

    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def bind(self, **_kwargs: Any) -> "_FrozenResponseProvider":
        return self

    def model_copy(self, update: Any = None) -> "_FrozenResponseProvider":
        del update
        return self

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        **_kwargs: Any,
    ) -> AIMessage:
        self.calls.append([provider_visible_payload(item) for item in messages])
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise AssertionError("unexpected provider call")
        return AIMessage(content=self.responses[index])


class Session1788681718GenerateProductionPathTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="biomind-session-1788681718-generate-"
        )
        self.root = Path(self.temporary.name)
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.plan_fixture = json.loads(
            PLAN_FIXTURE_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(
            self.plan_fixture["source_session"],
            self.fixture["source_session"],
        )
        self.assertEqual(
            self.plan_fixture["source_plan_state_file_sha256"],
            "3b32750de857d48686064d0ee3e092dd901594fdede33f61c7ab4a3bb60fc3c1",
        )
        self.assertEqual(
            self.plan_fixture["source_identity"]["current_step_id"],
            "step.check_contigs",
        )
        self.assertEqual(
            [
                str(step.get("operation") or "")
                for step in self.plan_fixture["candidate"]["steps"]
            ],
            [
                "check_contig_compatibility",
                "prepare_database",
                "build_database",
                "annotate_variants",
                "summarize_ann",
                "finalize_outputs",
            ],
        )
        source_content = str(self.fixture["response"]["content"])
        self.assertEqual(
            hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
            self.fixture["source_response_content_sha256"],
        )

        data_dir = self.root / "input-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        contents = {
            "input.variant_vcf": (
                "##fileformat=VCFv4.2\n"
                "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
                "chr1\t1\t.\tA\tG\t.\tPASS\t.\n"
            ),
            "input.reference_fasta": ">chr1\nA\n",
            "input.annotation_gtf": (
                'chr1\tfixture\tgene\t1\t1\t.\t+\t.\tgene_id "g1";\n'
            ),
        }
        self.path_by_handle: dict[str, Path] = {}
        mapped_content = source_content
        mappings = list(self.fixture.get("path_mappings") or [])
        self.assertEqual(
            [str(item.get("semantic_handle") or "") for item in mappings],
            [
                "input.variant_vcf",
                "input.reference_fasta",
                "input.annotation_gtf",
            ],
        )
        for mapping in mappings:
            handle = str(mapping["semantic_handle"])
            source_path = str(mapping["source_path"])
            target_path = data_dir / str(mapping["fixture_filename"])
            target_path.write_text(contents[handle], encoding="utf-8")
            self.assertEqual(mapped_content.count(source_path), 1)
            mapped_content = mapped_content.replace(source_path, str(target_path))
            self.path_by_handle[handle] = target_path
        self.assertTrue(all(
            str(mapping["source_path"]) not in mapped_content
            for mapping in mappings
        ))

        self.saved_response_content = mapped_content
        self.provider = _FrozenResponseProvider([mapped_content])
        self.patches = [
            patch.object(
                bio_agents,
                "init_chat_model",
                return_value=self.provider,
            ),
            patch.object(
                bio_agents,
                "BIOMIND_MEMORY_DIR",
                str(self.root / "memory"),
            ),
            patch.object(bio_agents, "BIOMIND_WIKI_ENABLED", False),
            patch.object(bio_agents, "ENABLE_ROOT_CAUSE_ANALYZER", False),
            patch.object(
                bio_agents,
                "SCHEMA2_CONTEXT_COMPACTION_ENABLED",
                False,
            ),
        ]
        for item in self.patches:
            item.start()

        self.agent = BioAnalystAgent(
            data_path=str(self.root),
            enable_persistence=False,
            use_llm_supervisor=False,
        )
        skills_root = Path(__file__).resolve().parents[1] / "skills"
        self.manager = SkillManager(str(skills_root))
        self.service = SkillAccessService(self.manager)
        self.agent.skill_manager = self.manager
        self.agent.skill_access_service = self.service

        async def no_observe(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def no_compress(
            messages: Sequence[BaseMessage],
            _task_id: str,
        ) -> tuple[list[BaseMessage], bool]:
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

    def _approved_generate_state(
        self,
        task_id: str,
        *,
        current_step_success_criteria_extra: Sequence[str] = (),
        legacy_renderer_mode: bool = False,
        expected_outputs=(),
    ) -> dict[str, Any]:
        submission = "Check contig compatibility for the three supplied inputs."
        bootstrap, _is_continuation, _raw_ref = (
            self.agent._persist_user_submission(task_id, submission)
        )
        task_spec = merge_task_spec(
            create_task_spec(
                submission,
                task_id=task_id,
                base_dir=str(self.root),
                project_root=str(self.root),
            ),
            {
                "inputs": [
                    {
                        **deepcopy(item),
                        "canonical_path": str(
                            self.path_by_handle[item["semantic_handle"]]
                        ),
                    }
                    for item in self.plan_fixture["task_inputs"]
                ],
                **({"expected_outputs": deepcopy(list(expected_outputs))} if expected_outputs else {}),
            },
            source="user_explicit",
            reason="session_1788681718_offline_replay_fixture",
        )
        candidate = deepcopy(self.plan_fixture["candidate"])
        candidate["steps"][0]["success_criteria"].extend(
            str(item)
            for item in current_step_success_criteria_extra
            if str(item).strip()
        )
        durable = build_plan_state(task_id, json.dumps(candidate), strict=True)
        step_effects = dict(self.plan_fixture.get("step_effects") or {})
        for step in durable["steps"]:
            step_id = str(step.get("step_id") or "")
            self.assertIn(step_id, step_effects)
            step["step_effect"] = deepcopy(step_effects[step_id])
        # The saved Plan fixture is already post-allocation, so reconstruct the
        # allocator-owned ledger row that accompanied its authoritative
        # plan-artifact handle.  This is a synthetic tempdir binding, not a
        # modification of the historical session.
        workspace_handle = next(
            str(item)
            for item in durable["steps"][1].get("outputs", []) or []
            if str(item).startswith("plan-artifact-")
        )
        workspace_binding = {
            "binding_id": "plan-resource-binding-snpeff-fixture",
            "canonical_resource_id": "artifact.snpeff_data_dir",
            "resource_kind": "mutable_workspace",
            "availability_origin": "not_yet_available",
            "initially_available": False,
            "aliases": [
                "plan.snpeff_data_dir",
                workspace_handle,
                "artifact.snpeff_data_dir",
            ],
            "binding_source": "approved_plan_fixture",
            "contract_revision": "snpeff-workspace-transition-v1",
        }
        workspace_path = (
            self.root
            / "task_outputs"
            / task_id
            / "plan_artifacts"
            / workspace_handle
        )
        durable["plan_artifacts"] = {
            workspace_handle: {
                "logical_artifact_id": workspace_handle,
                "artifact_binding_revision": 1,
                "canonical_path": str(workspace_path),
                "object_kind": "directory",
                "resource_kind": "mutable_workspace",
                "canonical_resource_id": "artifact.snpeff_data_dir",
                "resource_binding": deepcopy(workspace_binding),
                "local_ref": "plan.snpeff_data_dir",
            }
        }
        durable["plan_resource_bindings"] = {
            alias: deepcopy(workspace_binding)
            for alias in workspace_binding["aliases"]
        }
        if legacy_renderer_mode:
            durable["steps"][0]["renderer_composite"] = True
        durable["state_generation"] = int(
            bootstrap.get("state_generation") or 0
        )
        durable["current_task"] = {"task_spec": task_spec}
        durable["conversation_evidence"] = deepcopy(
            bootstrap.get("conversation_evidence") or {}
        )
        durable["audit_index"] = deepcopy(bootstrap.get("audit_index") or {})
        durable["selected_skill_ids"] = [SKILL_ID]
        durable = self.manager.bind_plan_execution_capability_contracts(
            durable,
            [SKILL_ID],
        )
        bind_plan_to_task_revision(durable, task_spec["revision"])
        approve_plan_state(
            durable,
            task_spec["revision"],
            approval_source="offline_test_fixture",
        )

        access = new_skill_access_state()
        access, snapshot, _created = self.service.ensure_snapshot(
            access,
            SKILL_ID,
            scope="task",
            status="active",
            selected_at_task_revision=task_spec["revision"],
        )
        access, _documents = self.service.load_skill_documents(
            access,
            [SKILL_ID],
            task_revision=task_spec["revision"],
            request_id="consultant-load-snpeff",
        )
        access = record_provider_visible_materials(
            access,
            [{
                "kind": "skill_document",
                "snapshot_id": snapshot["snapshot_id"],
                "content_hash": snapshot["content_hash"],
            }],
            phase="consultant",
            invocation_id="consultant-session-replay-fixture",
            event_id="consultant-visible-snpeff",
        )
        task_state = dict(durable.get("task_state") or {})
        task_state.update({
            "status": "running",
            "workflow_routing_schema_version": 2,
            "selected_skills": [SKILL_ID],
            "skill_access_state": access,
        })
        if legacy_renderer_mode:
            task_state["legacy_renderer_mode"] = True
        durable["task_state"] = task_state
        durable["workflow_routing_schema_version"] = 2
        save_plan_state(self.root, task_id, durable)

        persisted = load_plan_state(self.root, task_id)
        canonical_task = canonical_task_spec_for_state(persisted)
        return {
            "messages": [HumanMessage(content=submission)],
            "task_id": task_id,
            "session_id": task_id,
            "project_id": self.agent.default_project_id,
            "task_mode": "managed",
            "task_spec": canonical_task,
            "task_revision": canonical_task["revision"],
            "current_plan": json.dumps({"steps": persisted.get("steps")}),
            "current_step_id": persisted["current_step_id"],
            "selected_skills": [SKILL_ID],
            "active_skills": [SKILL_ID],
            "future_skills": [],
            "workflow_routing_schema_version": 2,
            "resolved_bindings": {},
        }

    def _advance_to_prepare_database(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Advance the approved fixture through the verified first step."""

        task_id = str(state["task_id"])
        durable = load_plan_state(self.root, task_id)
        approved_hash = semantic_plan_hash(durable)
        self.assertTrue(advance_plan_after_verified_step(
            durable,
            evidence="offline fixture: contig compatibility already verified",
            expected_step_id="step.check_contigs",
            transition_id=f"{task_id}:check-contigs-verified",
            legacy_route_projection=False,
        ))
        self.assertEqual(semantic_plan_hash(durable), approved_hash)
        save_plan_state(self.root, task_id, durable)

        persisted = load_plan_state(self.root, task_id)
        canonical_task = canonical_task_spec_for_state(persisted)
        self.assertEqual(
            persisted["current_step_id"],
            "step.prepare_database",
        )
        self.assertTrue(plan_approval_is_current(
            persisted,
            canonical_task["revision"],
        ))
        self.assertTrue(validate_plan_for_generate(
            persisted,
            canonical_task["revision"],
        )["valid"])

        refreshed = deepcopy(dict(state))
        refreshed.update({
            "task_spec": canonical_task,
            "task_revision": canonical_task["revision"],
            "current_step_id": persisted["current_step_id"],
            "current_plan": json.dumps(
                {"steps": persisted["steps"]},
                ensure_ascii=False,
            ),
            "plan_id": persisted["plan_id"],
            "plan_version": persisted["plan_version"],
            "plan_revision": persisted["plan_revision"],
        })
        return refreshed

    def _prepare_database_candidate(
        self,
        *,
        task_id: str,
        include_bindings: bool,
    ) -> tuple[str, dict[str, Any], Path]:
        genome_version = "fixture_genome_v1"
        durable = load_plan_state(self.root, task_id)
        workspace_rows = [
            dict(row)
            for row in dict(durable.get("plan_artifacts") or {}).values()
            if isinstance(row, Mapping)
            and str(row.get("canonical_resource_id") or "")
            == "artifact.snpeff_data_dir"
        ]
        self.assertEqual(len(workspace_rows), 1)
        workspace_row = workspace_rows[0]
        db_root = Path(str(workspace_row["canonical_path"]))
        binding_revision = int(workspace_row["artifact_binding_revision"])
        variant = self.path_by_handle["input.variant_vcf"]
        reference = self.path_by_handle["input.reference_fasta"]
        annotation = self.path_by_handle["input.annotation_gtf"]
        script = (
            "#!/bin/bash\n"
            "#SBATCH --job-name=biomind-prepare-db\n"
            "set -euo pipefail\n"
            ': "${GENOME_VERSION:?controller binding required}"\n'
            ': "${SNPEFF_DB:?controller binding required}"\n'
            f"VARIANT_VCF={variant}\n"
            f"REFERENCE_FASTA={reference}\n"
            f"ANNOTATION_GTF={annotation}\n"
            'test -r "$VARIANT_VCF"\n'
            'mkdir -p "$SNPEFF_DB/data/genomes" '
            '"$SNPEFF_DB/data/$GENOME_VERSION"\n'
            'cp -- "$REFERENCE_FASTA" '
            '"$SNPEFF_DB/data/genomes/$GENOME_VERSION.fa"\n'
            'cp -- "$ANNOTATION_GTF" '
            '"$SNPEFF_DB/data/$GENOME_VERSION/genes.gtf"\n'
            'printf "%s.genome : fixture genome\\n" "$GENOME_VERSION" '
            '> "$SNPEFF_DB/snpEff.config"\n'
        )
        bindings = {
            "genome_version": {
                "type": "string",
                "value": genome_version,
            },
            "snpeff_db": {
                "type": "artifact_ref",
                "value": {
                    "canonical_resource_id": "artifact.snpeff_data_dir",
                    "resource_kind": "mutable_workspace",
                    "binding_revision": binding_revision,
                },
            },
        }
        action: dict[str, Any] = {
            "action_type": "execute",
            "operation": "prepare_database",
            "language": "bash",
            "code": script,
            "env": {},
        }
        if include_bindings:
            action["produced_bindings"] = deepcopy(bindings)
            action["binding_env"] = {
                "GENOME_VERSION": {
                    "source": "produced_binding",
                    "name": "genome_version",
                },
                "SNPEFF_DB": {
                    "source": "produced_binding",
                    "name": "snpeff_db",
                },
            }
        return (
            "<action_json>"
            + json.dumps(action, ensure_ascii=False, sort_keys=True)
            + "</action_json>",
            bindings,
            db_root,
        )

    async def _install_recovery_handoff(
        self,
        state: Mapping[str, Any],
        *,
        external: bool = True,
    ) -> tuple[dict[str, Any], RecoveryDecision, Any, str]:
        """Create real typed Recovery and immutable execution authority."""

        task_id = str(state["task_id"])
        durable = load_plan_state(self.root, task_id)
        step_id = str(durable["current_step_id"])
        plan_id = str(durable["plan_id"])
        plan_version = str(durable["plan_version"])
        action_id = "failed-action-external"
        attempt_id = "failed-attempt-external"
        execution_id = "failed-execution-external"
        failure_fingerprint = "failure-external-script-fixture"
        execute_failure = ExecuteOutcome(
            schema_version=1,
            type="execution_failed",
            summary="A prior generated action requires a script repair.",
            scope={
                "task_id": task_id,
                "plan_id": plan_id,
                "plan_version": plan_version,
                "step_id": step_id,
                "action_id": action_id,
                "attempt_id": attempt_id,
            },
            payload={
                "execution_record": {
                    "task_id": task_id,
                    "plan_id": plan_id,
                    "plan_version": plan_version,
                    "step_id": step_id,
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "execution_id": execution_id,
                    "returncode": 17,
                    "failure_fingerprint": failure_fingerprint,
                },
                "action_id": action_id,
                "attempt_id": attempt_id,
                "return_code": 17,
                "failure_fingerprint": failure_fingerprint,
                "progress_snapshot": {},
            },
            evidence_refs=(execution_id,),
        ).to_dict()
        failure_update = CausalFailurePacketBinder(
            data_path=str(self.root)
        ).bind(
            {"task_id": task_id},
            "execute_outcome",
            execute_failure,
        )
        directive = {
            "schema_version": 1,
            "directive_id": "typed-directive-external",
            "task_id": task_id,
            "action": "retry_step",
            "summary": "Repair only the rejected external script candidate.",
            "reason_code": "GENERATED_ACTION_FAILED",
            "target_step_id": step_id,
            "instruction": (
                "Preserve the immutable external envelope and repair the "
                "script payload."
            ),
            "evidence_refs": [execution_id],
        }
        typed_recovery = await RecoveryRuntime(
            data_path=str(self.root)
        ).run({
            "task_id": task_id,
            **failure_update,
            "recovery_directive": directive,
            "recovery_archive_ref": "phase-archive:external-fixture",
        })
        recovery_outcome = dict(typed_recovery["recovery_outcome"])
        self.assertEqual(
            recovery_outcome["type"],
            "candidate_repair_required",
        )

        durable = load_plan_state(self.root, task_id)
        semantic_decision = RecoveryDecision.from_mapping({
            "schema_version": 5,
            "failure_class": "SCRIPT_BUG",
            "failure_owner": "GENERATED_ACTION",
            "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
            "reason": "Repair the generated external script payload.",
            "root_cause": "The prior generated script failed.",
            "evidence": [execution_id],
            "confidence": 1.0,
        })
        if external:
            semantic_decision.preferred_execution_mode = "EXTERNAL_HANDOFF"
        decision = finalize_recovery_decision(
            semantic_decision,
            source_scope=RecoverySourceScope(
                task_id=task_id,
                plan_id=plan_id,
                plan_version=plan_version,
                step_id=step_id,
                failed_action_id=action_id,
                failed_execution_attempt_id=attempt_id,
                source_failure_fingerprint=failure_fingerprint,
            ),
            recovery_context_fingerprint="external-recovery-context",
            source_node="execute",
            expected_state_generation=int(durable["state_generation"]),
        )
        task_root = RecoveryStateStore(self.root, task_id).root.parent
        envelope = build_recovery_execution_envelope(
            decision,
            recovery_context_id="external-recovery-context",
            step_id=step_id,
            task_root=task_root,
            allowed_input_roots=[str(self.root / "input-data")],
        )
        commit_recovery_bundle(
            self.root,
            task_id,
            RecoveryCommitBundle(
                decision,
                envelope,
                {},
                {},
                None,
                int(durable["state_generation"]),
            ),
        )
        committed = load_plan_state(self.root, task_id)
        recovery_state = committed["task_state"]["recovery_state"]
        old_target = str(
            recovery_state["generation_claim"]["target_generation_id"]
        )
        graph_state = {
            **deepcopy(dict(state)),
            "recovery_outcome": recovery_outcome,
            "recovery_decision": decision.to_dict(),
            "recovery_execution_envelope": envelope.to_dict(),
        }
        return graph_state, decision, envelope, old_target

    async def _run_generate_segment(
        self,
        state: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        execute_calls: list[dict[str, Any]] = []

        async def fake_execute(graph_state: Mapping[str, Any]) -> dict[str, Any]:
            generate_outcome = dict(graph_state.get("generate_outcome") or {})
            execute_calls.append(deepcopy(dict(
                (generate_outcome.get("payload") or {}).get("action") or {}
            )))
            return {
                "execute_outcome": ExecuteOutcome(
                    schema_version=1,
                    type="lightweight_completed",
                    summary="Offline executor boundary recorded the action.",
                    scope={
                        "task_id": str(state["task_id"]),
                        "step_id": state["current_step_id"],
                    },
                    payload={
                        "execution_record": {
                            "status": "recorded",
                            "operation": execute_calls[-1].get("operation"),
                            "command_started": False,
                        },
                        "status": "succeeded",
                        "task_mode": "managed",
                    },
                ).to_dict(),
            }

        async def unreachable(_state: Mapping[str, Any]) -> dict[str, Any]:
            raise AssertionError("unexpected post-execute runtime node")

        graph = build_schema2_runtime_graph({
            "generate": self.agent._schema2_runtime_node_handlers["generate"],
            "execute": fake_execute,
            "supervisor": unreachable,
            "commit_step": unreachable,
            "recovery": unreachable,
            "final_audit": unreachable,
        }).compile()
        events: list[dict[str, Any]] = []
        stream = graph.astream(
            deepcopy(state),
            config={"recursion_limit": 8},
            stream_mode="updates",
        )
        try:
            async for event in stream:
                events.append(event)
        finally:
            await stream.aclose()
        return events, execute_calls

    async def _assert_rejected_candidate_repairs_to_saved_action(
        self,
        *,
        state: Mapping[str, Any],
        rejected_response: str,
        expected_exit: str,
        expected_code: str,
    ) -> None:
        self.provider.calls.clear()
        self.provider.responses = [
            rejected_response,
            self.saved_response_content,
        ]

        events, execute_calls = await self._run_generate_segment(state)

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "generate", "execute"],
        )
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(execute_calls), 1)
        first = events[0]["generate"]
        self.assertEqual(first["generate_outcome"]["type"], "candidate_rejected")
        repair = first["generate_outcome"]["payload"]["repair_context"]
        self.assertEqual(repair["producer"], "biomind.generate_candidate_admission.v1")
        self.assertEqual(repair["execution_status"], "not_started")
        self.assertEqual(repair["exact_violations"][0]["code"], expected_code)
        self.assertTrue(str(repair["receipt_ref"]).strip())
        self.assertTrue(str(repair["validation_source"]).strip())
        second_request_text = json.dumps(
            self.provider.calls[1], ensure_ascii=False
        )
        self.assertIn("generate_validator_feedback", second_request_text)
        self.assertIn(expected_code, second_request_text)
        self.assertIn(repair["validation_source"], second_request_text)
        self.assertIn("previous_candidate", second_request_text)
        durable = load_plan_state(self.root, str(state["task_id"]))
        repair_state = dict(
            (durable.get("task_state") or {}).get("candidate_repair_state")
            or {}
        )
        event = dict(
            (repair_state.get("events") or {}).get(
                repair_state.get("last_event_id")
            )
            or {}
        )
        self.assertEqual(event["exit_reason"], expected_exit)
        self.assertEqual(
            events[1]["generate"]["generate_outcome"]["type"],
            "action_ready",
        )
        self.assertEqual(
            execute_calls[0]["operation"],
            "check_contig_compatibility",
        )

    async def test_adm_01_saved_candidate_reaches_fake_executor_once(self) -> None:
        task_id = "session-1788681718-adm-01"
        state = self._approved_generate_state(task_id)
        persisted = load_plan_state(self.root, task_id)
        self.assertEqual(
            [str(step.get("operation") or "") for step in persisted["steps"]],
            [
                "check_contig_compatibility",
                "prepare_database",
                "build_database",
                "annotate_variants",
                "summarize_ann",
                "finalize_outputs",
            ],
        )
        self.assertEqual(persisted["current_step_id"], "step.check_contigs")
        events, execute_calls = await self._run_generate_segment(state)

        visited = [next(iter(event)) for event in events]
        self.assertEqual(visited, ["generate", "execute"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(execute_calls), 1)

        generate_outcome = events[0]["generate"]["generate_outcome"]
        self.assertEqual(generate_outcome["type"], "action_ready")
        action = execute_calls[0]
        self.assertEqual(action["type"], "execute")
        self.assertEqual(action["operation"], "check_contig_compatibility")
        self.assertEqual(action["execution_capability_id"], CAPABILITY_ID)
        self.assertEqual(
            action["expected_step_effect"]["produces_observations"],
            ["contigs_compatible"],
        )
        argv_text = "\n".join(str(item) for item in action.get("argv") or [])
        for path in self.path_by_handle.values():
            self.assertIn(str(path), argv_text)

    async def test_rep_08_fake_execution_failure_enters_real_recovery_graph(
        self,
    ) -> None:
        """Replay one launched failure through the production typed seams."""

        task_id = "session-1788681718-rep-08"
        state = self._approved_generate_state(task_id)
        actual_executor_calls: list[dict[str, Any]] = []
        recovery_provider_calls: list[list[dict[str, Any]]] = []
        execution_id = "fake-execution-rep-08"
        attempt_id = "fake-attempt-rep-08"
        failure_fingerprint = "fake-launched-failure-rep-08"

        async def fake_actual_executor(
            execution_state: Mapping[str, Any],
        ) -> dict[str, Any]:
            action = deepcopy(dict(execution_state.get("pending_action") or {}))
            actual_executor_calls.append(action)
            return {
                "execution_evidence": {
                    "task_id": task_id,
                    "plan_id": str(action.get("plan_id") or ""),
                    "plan_version": str(action.get("plan_version") or ""),
                    "step_id": str(action.get("step_id") or ""),
                    "action_id": str(action.get("action_id") or ""),
                    "attempt_id": attempt_id,
                    "execution_id": execution_id,
                    "returncode": 17,
                    "command_started": True,
                    "status": "failed",
                    "command_role": "analysis_execution",
                    "failure_fingerprint": failure_fingerprint,
                    "evidence_refs": [execution_id],
                    "validation": {
                        "passed": False,
                        "status": "failed",
                    },
                }
            }

        class FakeRecoveryProvider:
            def bind_tools(
                provider_self,
                _tools: Sequence[Mapping[str, Any]],
            ) -> "FakeRecoveryProvider":
                return provider_self

            async def ainvoke(
                provider_self,
                messages: Sequence[BaseMessage],
            ) -> AIMessage:
                recovery_provider_calls.append([
                    provider_visible_payload(item) for item in messages
                ])
                if len(recovery_provider_calls) > 1:
                    raise AssertionError("unexpected Recovery provider call")
                return AIMessage(content=json.dumps({
                    "schema_version": 1,
                    "directive_id": "directive-rep-08-blocked",
                    "task_id": task_id,
                    "action": "blocked",
                    "summary": (
                        "The offline fake executor produced a launched nonzero "
                        "failure."
                    ),
                    "reason_code": "OFFLINE_FAKE_EXECUTION_FAILED",
                    "evidence_refs": [execution_id],
                }))

        typed_execute = TypedExecute(
            executor=fake_actual_executor,
            data_path=str(self.root),
        ).run
        execute_with_causal_binding = bind_causal_failure_node(
            typed_execute,
            binder=CausalFailurePacketBinder(data_path=str(self.root)),
            outcome_field="execute_outcome",
        )
        recovery_node = Schema2RecoveryNode(
            data_path=str(self.root),
            model=FakeRecoveryProvider(),
            phase_store=self.agent.phase_archive_store,
            skill_service=self.agent.skill_access_service,
            context_compaction_enabled=False,
        )

        async def unreachable(_state: Mapping[str, Any]) -> dict[str, Any]:
            raise AssertionError("unexpected post-failure runtime node")

        graph = build_schema2_runtime_graph({
            "generate": self.agent._schema2_runtime_node_handlers["generate"],
            "execute": execute_with_causal_binding,
            "supervisor": unreachable,
            "commit_step": unreachable,
            "recovery": recovery_node.run,
            "final_audit": unreachable,
        }).compile()
        events: list[dict[str, Any]] = []
        stream = graph.astream(
            deepcopy(state),
            config={"recursion_limit": 8},
            stream_mode="updates",
        )
        try:
            async for event in stream:
                events.append(event)
        finally:
            await stream.aclose()

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "execute", "recovery"],
        )
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(actual_executor_calls), 1)
        self.assertEqual(len(recovery_provider_calls), 1)
        self.assertEqual(
            actual_executor_calls[0]["operation"],
            "check_contig_compatibility",
        )

        execute_update = events[1]["execute"]
        execute_outcome = execute_update["execute_outcome"]
        self.assertEqual(execute_outcome["type"], "execution_failed")
        self.assertEqual(execute_outcome["payload"]["return_code"], 17)
        self.assertTrue(
            execute_outcome["payload"]["execution_record"]["command_started"]
        )
        self.assertEqual(
            execute_outcome["payload"]["failure_fingerprint"],
            failure_fingerprint,
        )
        self.assertEqual(
            execute_update["failure_packet"]["failure_type"],
            "execution_failed",
        )
        self.assertEqual(
            execute_update["failure_packet"]["attempt_ref"],
            attempt_id,
        )
        self.assertEqual(
            execute_update["failure_packet"]["action_ref"],
            actual_executor_calls[0]["action_id"],
        )
        packet_ref = execute_update["active_failure_packet_ref"]
        self.assertTrue(packet_ref.startswith("failure-packet:sha256:"))

        recovery_outcome = events[2]["recovery"]["recovery_outcome"]
        self.assertEqual(recovery_outcome["type"], "blocked")
        self.assertEqual(
            recovery_outcome["payload"]["reason_code"],
            "OFFLINE_FAKE_EXECUTION_FAILED",
        )
        recovery_wire = json.dumps(
            recovery_provider_calls,
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn(execution_id, recovery_wire)
        self.assertIn("execution_failed", recovery_wire)

        durable = load_plan_state(self.root, task_id)
        durable_task = dict(durable.get("task_state") or {})
        self.assertIn(packet_ref, durable_task["causal_failure_packets"])
        self.assertIn(
            packet_ref,
            durable_task["consumed_failure_packet_refs"],
        )
        self.assertEqual(durable_task.get("active_failure_packet_ref"), "")
        receipt = durable_task["typed_recovery_receipts"][
            "directive-rep-08-blocked"
        ]
        self.assertEqual(receipt["failure_packet_ref"], packet_ref)

    async def test_wrong_operation_receipt_repairs_to_saved_candidate(self) -> None:
        task_id = "session-1788681718-operation-repair"
        state = self._approved_generate_state(task_id)
        operation_field = '"operation": "check_contig_compatibility"'
        wrong_operation_field = '"operation": "prepare_database"'
        self.assertEqual(self.saved_response_content.count(operation_field), 1)
        wrong_candidate = self.saved_response_content.replace(
            operation_field,
            wrong_operation_field,
        )
        self.provider.responses = [
            wrong_candidate,
            self.saved_response_content,
        ]

        events, execute_calls = await self._run_generate_segment(state)

        visited = [next(iter(event)) for event in events]
        self.assertEqual(visited, ["generate", "generate", "execute"])
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(execute_calls), 1)

        first_outcome = events[0]["generate"]["generate_outcome"]
        second_outcome = events[1]["generate"]["generate_outcome"]
        self.assertEqual(first_outcome["type"], "candidate_rejected")
        self.assertEqual(second_outcome["type"], "action_ready")
        repair = first_outcome["payload"]["repair_context"]
        self.assertEqual(
            repair["producer"],
            "biomind.generate_candidate_admission.v1",
        )
        self.assertTrue(str(repair["receipt_ref"]).strip())
        self.assertEqual(repair["repair_ordinal"], 1)
        self.assertTrue(repair["progress_proven"])
        self.assertEqual(
            repair["previous_candidate"]["operation"],
            "prepare_database",
        )
        self.assertEqual(
            repair["exact_violations"][0]["code"],
            "ACTION_OPERATION_SCOPE_MISMATCH",
        )
        self.assertEqual(
            repair["contract_ref"]["capability_id"],
            CAPABILITY_ID,
        )

        first_request = self.provider.calls[0]
        second_request = self.provider.calls[1]
        self.assertGreater(len(second_request), len(first_request))
        self.assertEqual(second_request[:len(first_request)], first_request)
        repair_request_text = json.dumps(second_request, ensure_ascii=False)
        self.assertIn("generate_validator_feedback", repair_request_text)
        self.assertIn("ACTION_OPERATION_SCOPE_MISMATCH", repair_request_text)
        self.assertIn("previous_candidate", repair_request_text)

        repaired_action = execute_calls[0]
        self.assertEqual(
            repaired_action["operation"],
            "check_contig_compatibility",
        )
        self.assertEqual(
            repaired_action["execution_capability_id"],
            CAPABILITY_ID,
        )

    async def test_rep_05_same_wire_transport_retry_charges_one_repair_and_executes_once(
        self,
    ) -> None:
        """One transport replay remains one candidate/rejection identity."""

        task_id = "session-1788681718-rep-05-transport-replay"
        state = self._approved_generate_state(task_id)
        operation_field = '"operation": "check_contig_compatibility"'
        self.assertEqual(self.saved_response_content.count(operation_field), 1)
        rejected_candidate = self.saved_response_content.replace(
            operation_field,
            '"operation": "prepare_database"',
        )
        self.provider.calls.clear()
        self.provider.responses = [
            "",  # retryable empty final content; the wire must be replayed verbatim
            rejected_candidate,
            self.saved_response_content,
        ]

        events, execute_calls = await self._run_generate_segment(state)

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "generate", "execute"],
        )
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(
            self.provider.calls[1],
            self.provider.calls[0],
            "the bounded transport retry must replay the exact provider wire",
        )
        self.assertEqual(len(execute_calls), 1)
        self.assertEqual(
            execute_calls[0]["operation"],
            "check_contig_compatibility",
        )

        rejection = events[0]["generate"]["generate_outcome"]
        self.assertEqual(rejection["type"], "candidate_rejected")
        receipt = rejection["payload"]["repair_context"]
        self.assertEqual(receipt["repair_ordinal"], 1)
        self.assertEqual(
            receipt["exact_violations"][0]["code"],
            "ACTION_OPERATION_SCOPE_MISMATCH",
        )
        corrective_wire = json.dumps(
            self.provider.calls[2],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn("generate_validator_feedback", corrective_wire)
        self.assertIn("ACTION_OPERATION_SCOPE_MISMATCH", corrective_wire)
        self.assertIn(receipt["validation_source"], corrective_wire)
        self.assertIn("previous_candidate", corrective_wire)

        durable = load_plan_state(self.root, task_id)
        task_state = dict(durable.get("task_state") or {})
        repair_state = dict(task_state.get("candidate_repair_state") or {})
        self.assertEqual(list(repair_state["events"]), [
            receipt["rejection_event_id"]
        ])
        durable_event = repair_state["events"][receipt["rejection_event_id"]]
        self.assertEqual(
            durable_event["candidate_event_id"],
            receipt["candidate_event_id"],
        )
        budgets = dict(task_state.get("budgets") or {})
        self.assertEqual(budgets["preaction_rejections"], 1)
        self.assertEqual(budgets["payload_repairs"], 1)
        identities = budgets["recovery_counter_identities"]
        self.assertEqual(
            identities["preaction_rejections"],
            [receipt["rejection_event_id"]],
        )
        self.assertEqual(
            identities["payload_repairs"],
            [receipt["rejection_event_id"]],
        )
        self.assertEqual(
            len((durable.get("provenance_state") or {}).get("actions") or {}),
            1,
        )

    async def test_ctx_01_three_same_step_provider_rounds_survive_real_compaction(
        self,
    ) -> None:
        """Three real Generate rounds append until a genuine capacity epoch."""

        task_id = "session-1788681718-ctx-01-three-rounds"
        state = self._approved_generate_state(task_id)
        operation_field = '"operation": "check_contig_compatibility"'
        empty_env = '"env": {}'
        self.assertEqual(self.saved_response_content.count(operation_field), 1)
        self.assertEqual(self.saved_response_content.count(empty_env), 1)
        round_one = self.saved_response_content.replace(
            operation_field,
            '"operation": "prepare_database"',
        )
        round_two = self.saved_response_content.replace(
            empty_env,
            '"env": {"BIOMIND_TASK_ID": "ctx-round-two-owned"}',
        )
        generate_responses = [
            round_one,
            round_two,
            self.saved_response_content,
        ]
        generate_wires: list[list[dict[str, Any]]] = []
        compactor_wires: list[list[dict[str, Any]]] = []
        compaction_requests: list[dict[str, Any]] = []
        selected_rule = "capability-owned mutable workspace"

        async def routed_provider(
            messages: Sequence[BaseMessage],
            **_kwargs: Any,
        ) -> AIMessage:
            wire = [provider_visible_payload(item) for item in messages]
            self.provider.calls.append(deepcopy(wire))
            rendered = json.dumps(wire, ensure_ascii=False, sort_keys=True)
            if "<context_compaction_request" in rendered:
                compactor_wires.append(deepcopy(wire))
                body = str(messages[1].content)
                prefix = body.index("\n") + 1
                suffix = body.rindex("\n</context_compaction_request>")
                envelope = json.loads(body[prefix:suffix])
                request = deepcopy(dict(envelope["request"]))
                compaction_requests.append(request)
                units = list(request["source_units"])
                retained = [
                    str(unit["unit_id"])
                    for unit in units
                    if bool(unit.get("selected"))
                    or "generate_validator_feedback" in str(unit.get("body") or "")
                ]
                summarized = [
                    str(unit["unit_id"])
                    for unit in units
                    if str(unit["unit_id"]) not in set(retained)
                ]
                self.assertTrue(any(bool(unit.get("selected")) for unit in units))
                self.assertGreaterEqual(
                    sum(
                        "generate_validator_feedback"
                        in str(unit.get("body") or "")
                        for unit in units
                    ),
                    2,
                )
                self.assertTrue(summarized)
                return AIMessage(content=json.dumps({
                    "schema_version": 1,
                    "compaction_request_id": request["compaction_request_id"],
                    "source_epoch_id": request["source_epoch_id"],
                    "source_manifest_hash": request["source_manifest_hash"],
                    "mode": "normal",
                    "retain_exact_unit_ids": retained,
                    "summary_source_unit_ids": summarized,
                    "summary": (
                        "Two superseded candidate bodies were externalized; "
                        "their exact validator feedback remains resident."
                    ),
                    "material_retrieval_state": [],
                    "unresolved_items": [],
                    "selection_rationale": (
                        "Retain selected Skill material and exact same-step "
                        "validator feedback."
                    ),
                }, ensure_ascii=False, sort_keys=True))

            generate_wires.append(deepcopy(wire))
            ordinal = len(generate_wires)
            if ordinal > len(generate_responses):
                raise AssertionError("unexpected Generate provider call")
            if ordinal == 2:
                # The next re-entry crosses a real capacity boundary.  The first
                # two requests therefore prove ordinary append-only history;
                # the third proves the production compaction transition.
                bio_agents.SCHEMA2_CONTEXT_COMPACTION_ENABLED = True
            return AIMessage(content=generate_responses[ordinal - 1])

        original_policy = self.agent._schema2_phase_compactor.policy
        self.agent._schema2_phase_compactor.policy = ContextBudgetPolicy(
            application_context_limit_tokens=200_000,
            output_reserve_tokens=0,
            estimation_margin_tokens=0,
            safety_headroom_tokens=0,
            trigger_ratio=0.01,
            target_ratio=0.01,
        )
        self.provider.calls.clear()
        try:
            with (
                patch.object(self.provider, "ainvoke", new=routed_provider),
                patch.object(
                    bio_agents,
                    "SCHEMA2_CONTEXT_COMPACTION_ENABLED",
                    False,
                ),
            ):
                events, execute_calls = await self._run_generate_segment(state)
        finally:
            self.agent._schema2_phase_compactor.policy = original_policy

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "generate", "generate", "execute"],
        )
        self.assertEqual(len(generate_wires), 3)
        self.assertEqual(len(compactor_wires), 1)
        self.assertEqual(len(execute_calls), 1)
        self.assertEqual(
            execute_calls[0]["operation"],
            "check_contig_compatibility",
        )

        first_wire, second_wire, final_wire = generate_wires
        self.assertGreater(len(second_wire), len(first_wire))
        self.assertEqual(second_wire[:len(first_wire)], first_wire)
        for wire in generate_wires:
            rendered = json.dumps(wire, ensure_ascii=False, sort_keys=True)
            self.assertIn("step.check_contigs", rendered)
            self.assertIn(selected_rule, rendered)
        second_text = json.dumps(second_wire, ensure_ascii=False, sort_keys=True)
        final_text = json.dumps(final_wire, ensure_ascii=False, sort_keys=True)
        compactor_text = json.dumps(
            compactor_wires[0], ensure_ascii=False, sort_keys=True
        )
        self.assertIn("ACTION_OPERATION_SCOPE_MISMATCH", second_text)
        self.assertIn("ACTION_OPERATION_SCOPE_MISMATCH", compactor_text)
        self.assertIn("MANAGED_ACTION_ENV_KEY_CONFLICT", compactor_text)
        self.assertIn("ACTION_OPERATION_SCOPE_MISMATCH", final_text)
        self.assertIn("MANAGED_ACTION_ENV_KEY_CONFLICT", final_text)
        self.assertEqual(final_text.count("generate_validator_feedback"), 2)
        self.assertIn("context_compaction_summary", final_text)

        rejection_outcomes = [
            event["generate"]["generate_outcome"]
            for event in events[:2]
        ]
        self.assertEqual(
            [outcome["type"] for outcome in rejection_outcomes],
            ["candidate_rejected", "candidate_rejected"],
        )
        repairs = [
            outcome["payload"]["repair_context"]
            for outcome in rejection_outcomes
        ]
        self.assertEqual([item["repair_ordinal"] for item in repairs], [1, 2])
        self.assertEqual(
            [item["exact_violations"][0]["code"] for item in repairs],
            [
                "ACTION_OPERATION_SCOPE_MISMATCH",
                "MANAGED_ACTION_ENV_KEY_CONFLICT",
            ],
        )

        request = compaction_requests[0]
        self.assertGreaterEqual(int(request["history_version"]), 4)
        self.assertGreaterEqual(int(request["selected_revision"]), 1)
        durable = load_plan_state(self.root, task_id)
        task_state = dict(durable.get("task_state") or {})
        episode = dict(task_state["generate_context_episode"])
        self.assertEqual(episode["basis"]["step_id"], "step.check_contigs")
        self.assertEqual(episode["context_epoch"], 1)
        self.assertTrue(str(episode["previous_context_archive_ref"]).strip())
        event_kinds = [
            str(item.get("event_kind") or "")
            for item in episode.get("local_events") or []
        ]
        self.assertEqual(event_kinds.count("validator_feedback"), 2)
        self.assertEqual(event_kinds.count("context_compaction_summary"), 1)
        self.assertEqual(event_kinds[-1], "candidate_response")
        inherited_text = json.dumps(
            episode.get("inherited_materials") or [],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn(selected_rule, inherited_text)
        telemetry = dict(task_state.get("generate_context_telemetry") or {})
        self.assertTrue(telemetry["compaction_triggered"])
        self.assertTrue(telemetry["compaction_committed"])
        self.assertTrue(str(telemetry["source_archive_ref"]).strip())
        residency = dict(
            (task_state.get("skill_material_residency") or {}).get("generate")
            or {}
        )
        self.assertTrue(residency)
        self.assertEqual(
            {str(item.get("residency") or "") for item in residency.values()},
            {"exact"},
        )
        repair_state = dict(task_state.get("candidate_repair_state") or {})
        self.assertEqual(len(repair_state.get("events") or {}), 2)
        budgets = dict(task_state.get("budgets") or {})
        self.assertEqual(budgets["preaction_rejections"], 2)
        self.assertEqual(budgets["payload_repairs"], 2)

    async def test_tampered_or_stale_repair_receipt_never_reaches_fake_provider(
        self,
    ) -> None:
        task_id = "session-1788681718-repair-projection-guard"
        state = self._approved_generate_state(task_id)
        operation_field = '"operation": "check_contig_compatibility"'
        wrong_candidate = self.saved_response_content.replace(
            operation_field,
            '"operation": "prepare_database"',
        )
        self.provider.calls.clear()
        self.provider.responses = [wrong_candidate, self.saved_response_content]

        first = await self.agent._schema2_runtime_node_handlers["generate"](
            deepcopy(state)
        )
        self.assertEqual(first["generate_outcome"]["type"], "candidate_rejected")
        self.assertEqual(len(self.provider.calls), 1)

        tampered = deepcopy(first["generate_outcome"])
        tampered["payload"]["repair_context"]["feedback"] = "tampered"
        tampered_result = await self.agent._schema2_runtime_node_handlers[
            "generate"
        ]({
            **deepcopy(state),
            "generate_outcome": tampered,
        })
        self.assertEqual(
            tampered_result["generate_outcome"]["type"],
            "internal_protocol_error",
        )
        self.assertEqual(
            tampered_result["generate_outcome"]["payload"]["error_code"],
            "GENERATE_REPAIR_RECEIPT_INVALID",
        )
        self.assertFalse(
            tampered_result["generate_outcome"]["payload"][
                "provider_call_attempted"
            ]
        )
        self.assertEqual(len(self.provider.calls), 1)

        receipt = deepcopy(
            first["generate_outcome"]["payload"]["repair_context"]
        )
        durable = load_plan_state(self.root, task_id)
        task_state = dict(durable.get("task_state") or {})
        repair_state = dict(task_state.get("candidate_repair_state") or {})
        events = dict(repair_state.get("events") or {})
        events["newer-projection-event"] = {
            "scope": deepcopy(receipt["scope"]),
            "repair_ordinal": int(receipt["repair_ordinal"]) + 1,
            "status": "internal_error",
        }
        repair_state["events"] = events
        task_state["candidate_repair_state"] = repair_state
        durable["task_state"] = task_state
        save_plan_state(self.root, task_id, durable)

        stale_result = await self.agent._schema2_runtime_node_handlers[
            "generate"
        ]({
            **deepcopy(state),
            "action_format_repair": receipt,
        })
        self.assertEqual(
            stale_result["generate_outcome"]["type"],
            "internal_protocol_error",
        )
        self.assertEqual(
            stale_result["generate_outcome"]["payload"]["receipt_error_code"],
            "CANDIDATE_REPAIR_RECEIPT_STALE",
        )
        self.assertEqual(len(self.provider.calls), 1)

    async def test_binding_na_still_enforces_managed_artifact_gate(self) -> None:
        task_id = "session-1788681718-managed-artifact-repair"
        state = self._approved_generate_state(task_id)
        binding_operations = dict(
            (
                self.manager.get_skill_meta(SKILL_ID).get("generate") or {}
            ).get("binding_contract", {}).get("operations")
            or {}
        )
        self.assertNotIn("check_contig_compatibility", binding_operations)
        empty_env = '"env": {}'
        conflicting_env = '"env": {"BIOMIND_TASK_ID": "candidate-owned"}'
        self.assertEqual(self.saved_response_content.count(empty_env), 1)
        conflicting_candidate = self.saved_response_content.replace(
            empty_env,
            conflicting_env,
        )
        self.provider.responses = [
            conflicting_candidate,
            self.saved_response_content,
        ]

        events, execute_calls = await self._run_generate_segment(state)

        visited = [next(iter(event)) for event in events]
        self.assertEqual(visited, ["generate", "generate", "execute"])
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(execute_calls), 1)

        first_outcome = events[0]["generate"]["generate_outcome"]
        self.assertEqual(first_outcome["type"], "candidate_rejected")
        repair = first_outcome["payload"]["repair_context"]
        self.assertEqual(
            repair["producer"],
            "biomind.generate_candidate_admission.v1",
        )
        self.assertTrue(str(repair["receipt_ref"]).strip())
        self.assertEqual(
            first_outcome["payload"]["budget_receipt"]["ordinal"],
            1,
        )
        self.assertEqual(repair["repair_stage"], "managed_artifact")
        self.assertEqual(
            repair["exact_violations"][0]["code"],
            "MANAGED_ACTION_ENV_KEY_CONFLICT",
        )
        self.assertEqual(
            repair["previous_candidate"]["operation"],
            "check_contig_compatibility",
        )
        self.assertTrue(repair["progress_proven"])

        second_request_text = json.dumps(
            self.provider.calls[1],
            ensure_ascii=False,
        )
        self.assertIn("generate_validator_feedback", second_request_text)
        self.assertIn(
            "MANAGED_ACTION_ENV_KEY_CONFLICT",
            second_request_text,
        )
        self.assertIn("candidate-owned", second_request_text)

        repaired_action = execute_calls[0]
        self.assertEqual(
            repaired_action["operation"],
            "check_contig_compatibility",
        )
        self.assertNotIn("BIOMIND_TASK_ID", repaired_action.get("env") or {})

    async def test_repairable_raw_generate_branches_reenter_real_graph(self) -> None:
        cases = [
            (
                "unrecognized",
                "This is prose, not a structured action.",
                "unrecognized_action",
                "ACTION_SCHEMA_REQUIRED_FIELD",
                "managed",
            ),
            (
                "lightweight",
                "<search>look up a local VCF</search>",
                "lightweight_action_rejected",
                "LIGHTWEIGHT_ACTION_DISALLOWED",
                "readonly",
            ),
            (
                "completion",
                "<solution>Everything is complete.</solution>",
                "completion_candidate_rejected",
                "COMPLETION_CANDIDATE_REJECTED",
                "readonly",
            ),
            (
                "plan-patch",
                (
                    '<action_json>{"action_type":"plan_patch",'
                    '"reason":"change the plan",'
                    '"failure_owner":"GENERATED_ACTION",'
                    '"patch":{}}</action_json>'
                ),
                "plan_patch_owner_rejected",
                "PLAN_PATCH_OWNER_INVALID",
                "managed",
            ),
            (
                "empty-execute",
                "<execute>   </execute>",
                "execute_empty_body_rejected",
                "EXECUTE_BODY_EMPTY",
                "managed",
            ),
            (
                "legacy-activation",
                '<activate_batch batch_id="legacy"/>',
                "legacy_activation_rejected",
                "LEGACY_ACTIVATE_BATCH_DISALLOWED",
                "managed",
            ),
            (
                "knowledge-search",
                "<search>inspect the supplied local VCF</search>",
                "knowledge_search_action_rejected",
                "KNOWLEDGE_SEARCH_CANNOT_SATISFY_EXECUTION_EVIDENCE",
                "managed",
            ),
        ]
        for label, response, exit_reason, code, task_mode in cases:
            with self.subTest(label=label):
                state = self._approved_generate_state(
                    f"session-1788681718-branch-{label}",
                    current_step_success_criteria_extra=(
                        ("The input file exists and is readable",)
                        if label == "knowledge-search"
                        else ()
                    ),
                )
                state["task_mode"] = task_mode
                await self._assert_rejected_candidate_repairs_to_saved_action(
                    state=state,
                    rejected_response=response,
                    expected_exit=exit_reason,
                    expected_code=code,
                )

    async def test_blocked_evidence_rejection_reenters_real_graph(self) -> None:
        state = self._approved_generate_state(
            "session-1788681718-branch-blocked-evidence",
            legacy_renderer_mode=True,
        )
        await self._assert_rejected_candidate_repairs_to_saved_action(
            state=state,
            rejected_response=(
                '<action_json>{"action_type":"blocked",'
                '"reason":"a required input is missing",'
                '"missing_evidence":["unknown.role"]}</action_json>'
            ),
            expected_exit="blocked_evidence_rejected",
            expected_code=(
                "BLOCKED_WITHOUT_DETERMINISTIC_MISSING_EVIDENCE"
            ),
        )

    async def test_external_bundle_rejection_rolls_claim_then_waits_for_submission(
        self,
    ) -> None:
        task_id = "session-1788681718-external-bundle-repair"
        base_state = self._approved_generate_state(task_id)
        state, decision, envelope, old_target = (
            await self._install_recovery_handoff(base_state)
        )
        input_checks = "\n".join(
            f'test -r "{path}"'
            for path in self.path_by_handle.values()
        )
        invalid_script = (
            "<shell>#!/bin/bash\n"
            "set -euo pipefail\n"
            f"{input_checks}\n"
            "printf 'candidate checked\\n'\n"
            "</shell>"
        )
        repaired_script = invalid_script.replace(
            "#!/bin/bash\n",
            "#!/bin/bash\n#SBATCH --job-name=biomind-repair\n",
            1,
        )
        self.provider.calls.clear()
        self.provider.responses = [invalid_script, repaired_script]

        events, execute_calls = await self._run_generate_segment(state)

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "generate", "ask_user"],
        )
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(execute_calls, [])
        first = events[0]["generate"]["generate_outcome"]
        second = events[1]["generate"]["generate_outcome"]
        self.assertEqual(first["type"], "candidate_rejected")
        self.assertEqual(second["type"], "user_input_needed")
        repair = first["payload"]["repair_context"]
        self.assertEqual(
            repair["exact_violations"][0]["code"],
            "EXTERNAL_SCRIPT_INVALID",
        )
        self.assertEqual(repair["previous_candidate"]["payload_revision"], 0)
        self.assertEqual(repair["repair_ordinal"], 1)
        self.assertTrue(str(repair["receipt_ref"]).strip())

        second_request_text = json.dumps(
            self.provider.calls[1], ensure_ascii=False
        )
        feedback_messages = [
            json.loads(str(message.get("content") or ""))
            for message in self.provider.calls[1]
            if "generate_validator_feedback"
            in str(message.get("content") or "")
        ]
        self.assertEqual(len(feedback_messages), 1)
        visible_feedback = feedback_messages[0]["feedback"]
        self.assertIn(
            "exact_violations",
            visible_feedback["action_repair"],
            json.dumps(visible_feedback["action_repair"], ensure_ascii=False),
        )
        self.assertEqual(
            visible_feedback["action_repair"]["exact_violations"][0]["code"],
            "EXTERNAL_SCRIPT_INVALID",
        )
        self.assertEqual(
            visible_feedback["action_repair"]["repair_stage"],
            "external_execution_bundle",
        )
        self.assertEqual(
            visible_feedback["action_repair"]["contract_ref"][
                "recovery_envelope_hash"
            ],
            "verified:recovery_envelope_hash",
        )
        self.assertEqual(
            visible_feedback["action_repair"]["contract_ref"][
                "execution_mode"
            ],
            "EXTERNAL_HANDOFF",
        )
        self.assertIn(repair["validation_source"], second_request_text)
        self.assertNotIn(
            "RECOVERY_PROVIDER_COMPLETION_CACHE_MISSING",
            second_request_text,
        )

        durable = load_plan_state(self.root, task_id)
        task_state = durable["task_state"]
        recovery_state = task_state["recovery_state"]
        active = recovery_state["active_recovery"]
        current_claim = recovery_state["generation_claim"]
        claim_history = recovery_state["generation_claim_history"]
        self.assertEqual(active["recovery_id"], decision.decision_id)
        self.assertEqual(active["envelope_id"], envelope.immutable_hash)
        self.assertEqual(
            recovery_state["active_recovery_envelope"]["immutable_hash"],
            envelope.immutable_hash,
        )
        self.assertNotEqual(active["target_generation_id"], old_target)
        self.assertEqual(current_claim["status"], "action_adopted")
        self.assertEqual(current_claim["payload_revision"], 1)
        self.assertEqual(
            current_claim["parent_target_generation_id"], old_target
        )
        self.assertEqual(claim_history[-1]["target_generation_id"], old_target)
        self.assertEqual(claim_history[-1]["status"], "superseded")
        self.assertEqual(
            claim_history[-1]["candidate_rejection_event_id"],
            repair["rejection_event_id"],
        )

        repair_state = task_state["candidate_repair_state"]
        self.assertEqual(len(repair_state["events"]), 1)
        budgets = task_state["budgets"]
        self.assertEqual(budgets["preaction_rejections"], 1)
        self.assertEqual(budgets["payload_repairs"], 1)
        self.assertEqual(
            len(budgets["recovery_counter_identities"]["preaction_rejections"]),
            1,
        )
        self.assertEqual(
            len(budgets["recovery_counter_identities"]["payload_repairs"]),
            1,
        )

    async def test_binding_applicable_external_recovery_repairs_missing_bindings(
        self,
    ) -> None:
        task_id = "session-1788681718-prepare-binding-external-repair"
        base_state = self._approved_generate_state(task_id)
        prepare_state = self._advance_to_prepare_database(base_state)
        state, decision, envelope, old_target = (
            await self._install_recovery_handoff(prepare_state)
        )
        missing, expected_bindings, db_root = self._prepare_database_candidate(
            task_id=task_id,
            include_bindings=False
        )
        repaired, _, _ = self._prepare_database_candidate(
            task_id=task_id,
            include_bindings=True
        )
        self.provider.calls.clear()
        self.provider.responses = [missing, repaired]

        events, execute_calls = await self._run_generate_segment(state)

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "generate", "ask_user"],
        )
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(execute_calls, [])
        self.assertFalse(db_root.exists())

        first = events[0]["generate"]["generate_outcome"]
        second = events[1]["generate"]["generate_outcome"]
        self.assertEqual(first["type"], "candidate_rejected")
        self.assertEqual(second["type"], "user_input_needed")
        repair = first["payload"]["repair_context"]
        self.assertEqual(
            repair["repair_stage"],
            "operation_binding_admission",
        )
        self.assertEqual(
            repair["validation_source"],
            "agent_core.plan_bindings.validate_operation_binding_admission",
        )
        self.assertEqual(
            repair["contract_ref"]["capability_id"],
            "genomics.snpeff_annotation.prepare_database",
        )
        self.assertEqual(
            [
                (
                    item["code"],
                    item.get("binding_name") or item.get("env_name"),
                )
                for item in repair["exact_violations"]
            ],
            [
                ("BINDING_PRODUCTION_MISSING", "genome_version"),
                ("BINDING_PRODUCTION_MISSING", "snpeff_db"),
                ("BINDING_ENV_REQUIRED", "GENOME_VERSION"),
                ("BINDING_ENV_REQUIRED", "SNPEFF_DB"),
            ],
        )
        self.assertEqual(repair["repair_ordinal"], 1)
        self.assertTrue(repair["progress_proven"])
        self.assertTrue(str(repair["receipt_ref"]).strip())

        first_request_text = json.dumps(
            self.provider.calls[0], ensure_ascii=False
        )
        self.assertIn("semantic_action_v2", first_request_text)
        self.assertIn("prepare_database", first_request_text)
        self.assertIn("genome_version", first_request_text)
        self.assertIn("snpeff_db", first_request_text)
        self.assertIn(str(db_root), first_request_text)
        self.assertIn("artifact.snpeff_data_dir", first_request_text)
        self.assertIn("mutable_workspace", first_request_text)
        self.assertIn(
            "complete executable Slurm script payload",
            first_request_text,
        )
        feedback_messages = [
            json.loads(str(message.get("content") or ""))
            for message in self.provider.calls[1]
            if "generate_validator_feedback"
            in str(message.get("content") or "")
        ]
        self.assertEqual(len(feedback_messages), 1)
        visible_repair = feedback_messages[0]["feedback"]["action_repair"]
        self.assertEqual(
            [item["code"] for item in visible_repair["exact_violations"]],
            [
                "BINDING_PRODUCTION_MISSING",
                "BINDING_PRODUCTION_MISSING",
                "BINDING_ENV_REQUIRED",
                "BINDING_ENV_REQUIRED",
            ],
        )
        previous_candidate_text = json.dumps(
            visible_repair["previous_candidate"],
            ensure_ascii=False,
            sort_keys=True,
        )
        for controller_field in (
            "recovery_execution_envelope",
            "immutable_hash",
            "action_hash",
            "generated_script",
            "runtime_binding_manifest",
            "controller_artifact_environment",
        ):
            self.assertNotIn(controller_field, previous_candidate_text)

        durable = load_plan_state(self.root, task_id)
        task_state = durable["task_state"]
        recovery_state = task_state["recovery_state"]
        current_claim = recovery_state["generation_claim"]
        self.assertEqual(durable["current_step_id"], "step.prepare_database")
        self.assertEqual(task_state.get("pending_action") or {}, {})
        self.assertEqual(
            task_state["external_execution_handoff"]["adopted_action"][
                "produced_bindings"
            ],
            expected_bindings,
        )
        self.assertEqual(current_claim["status"], "action_adopted")
        self.assertEqual(current_claim["payload_revision"], 1)
        self.assertNotEqual(
            recovery_state["active_recovery"]["target_generation_id"],
            old_target,
        )
        previous_claim = recovery_state["generation_claim_history"][-1]
        self.assertEqual(previous_claim["status"], "superseded")
        self.assertEqual(
            previous_claim["candidate_rejection_event_id"],
            repair["rejection_event_id"],
        )

        bundle = RecoveryStateStore(
            self.root, task_id
        ).load_external_execution_bundle(envelope.action_id)
        self.assertEqual(bundle.step_id, "step.prepare_database")
        self.assertEqual(
            bundle.execution_envelope["immutable_hash"],
            envelope.immutable_hash,
        )
        bundled_script = Path(bundle.script_path).read_text(encoding="utf-8")
        self.assertTrue(bundled_script.startswith("#!/bin/bash\n"))
        self.assertIn("#SBATCH --job-name=biomind-prepare-db", bundled_script)
        repair_state = task_state["candidate_repair_state"]
        rejection_event = repair_state["events"][repair_state["last_event_id"]]
        self.assertEqual(
            rejection_event["exit_reason"],
            "binding_operation_contract_mismatch",
        )
        self.assertEqual(
            recovery_state["active_recovery"]["recovery_id"],
            decision.decision_id,
        )

    async def test_policy_rejected_candidate_never_persists_execution_authority(
        self,
    ) -> None:
        task_id = "session-1788681718-rejected-candidate-clean"
        state = self._approved_generate_state(
            task_id,
            current_step_success_criteria_extra=(
                "The input file exists and is readable",
            ),
        )
        self.provider.calls.clear()
        self.provider.responses = [
            "<search>inspect the supplied local VCF</search>"
        ]

        update = await self.agent._schema2_runtime_node_handlers["generate"](
            deepcopy(state)
        )

        self.assertEqual(update["generate_outcome"]["type"], "candidate_rejected")
        durable = load_plan_state(self.root, task_id)
        self.assertEqual(
            (durable.get("task_state") or {}).get("pending_action") or {},
            {},
        )
        self.assertEqual(
            (durable.get("provenance_state") or {}).get("actions") or {},
            {},
        )
        script_root = self.root / "task_outputs" / task_id / "scripts"
        self.assertFalse(script_root.exists())

    async def test_post_bind_external_rejection_persists_no_action_authority(
        self,
    ) -> None:
        task_id = "session-1788681718-external-rejected-candidate-clean"
        base_state = self._approved_generate_state(task_id)
        state, _decision, _envelope, _old_target = (
            await self._install_recovery_handoff(base_state)
        )
        input_checks = "\n".join(
            f'test -r "{path}"'
            for path in self.path_by_handle.values()
        )
        self.provider.calls.clear()
        self.provider.responses = [
            "<shell>#!/bin/bash\n"
            "set -euo pipefail\n"
            f"{input_checks}\n"
            "printf 'candidate checked\\n'\n"
            "</shell>"
        ]

        update = await self.agent._schema2_runtime_node_handlers["generate"](
            deepcopy(state)
        )

        outcome = update["generate_outcome"]
        self.assertEqual(outcome["type"], "candidate_rejected")
        self.assertEqual(
            outcome["payload"]["repair_context"]["repair_stage"],
            "external_execution_bundle",
        )
        durable = load_plan_state(self.root, task_id)
        self.assertEqual(
            (durable.get("task_state") or {}).get("pending_action") or {},
            {},
        )
        self.assertEqual(
            (durable.get("provenance_state") or {}).get("actions") or {},
            {},
        )
        script_root = self.root / "task_outputs" / task_id / "scripts"
        self.assertFalse(script_root.exists())

    async def test_late_recovery_provider_response_after_rollover_is_stale(
        self,
    ) -> None:
        for external in (False, True):
            with self.subTest(external=external):
                task_id = (
                    "session-1788681718-late-provider-external"
                    if external
                    else "session-1788681718-late-provider-ordinary"
                )
                base_state = self._approved_generate_state(task_id)
                state, decision, envelope, old_target = (
                    await self._install_recovery_handoff(
                        base_state,
                        external=external,
                    )
                )
                self.provider.calls.clear()
                self.provider.responses = [self.saved_response_content]
                original_ainvoke = self.provider.ainvoke

                async def respond_after_successor_claim_is_issued(
                    messages: Sequence[BaseMessage],
                    **kwargs: Any,
                ) -> AIMessage:
                    response = await original_ainvoke(messages, **kwargs)
                    durable = load_plan_state(self.root, task_id)
                    recovery_state = durable["task_state"]["recovery_state"]
                    claim = recovery_state["generation_claim"]
                    self.assertEqual(
                        claim["target_generation_id"],
                        old_target,
                    )
                    self.assertEqual(claim["status"], "claimed")
                    update_recovery_generation_claim(
                        durable,
                        target_generation_id=old_target,
                        status="provider_completed",
                    )
                    rollover_recovery_generation_claim_after_candidate_rejection(
                        durable,
                        target_generation_id=old_target,
                        rejection_event_id=(
                            f"{task_id}:concurrent-candidate-rejection"
                        ),
                        candidate_fingerprint=(
                            f"{task_id}:concurrent-candidate-fingerprint"
                        ),
                        repair_ordinal=1,
                        receipt_ref=(
                            f"state://candidate-repair/{task_id}-receipt"
                        ),
                    )
                    save_plan_state(self.root, task_id, durable)
                    return response

                self.provider.ainvoke = (
                    respond_after_successor_claim_is_issued
                )
                try:
                    update = await self.agent._schema2_runtime_node_handlers[
                        "generate"
                    ](deepcopy(state))
                finally:
                    self.provider.ainvoke = original_ainvoke

                outcome = update["generate_outcome"]
                self.assertEqual(outcome["type"], "stale_result")
                self.assertFalse(outcome["payload"]["counts_as_failure"])
                self.assertIn(
                    "recovery_state.active_recovery.target_generation_id",
                    outcome["payload"]["changed_preconditions"],
                )
                self.assertEqual(len(self.provider.calls), 1)

                persisted = load_plan_state(self.root, task_id)
                persisted_task = persisted["task_state"]
                persisted_recovery = persisted_task["recovery_state"]
                successor = persisted_recovery["generation_claim"]
                self.assertNotEqual(
                    successor["target_generation_id"],
                    old_target,
                )
                self.assertEqual(successor["status"], "issued")
                self.assertEqual(
                    persisted_recovery["active_recovery"]["recovery_id"],
                    decision.decision_id,
                )
                self.assertEqual(
                    persisted_recovery["active_recovery"]["envelope_id"],
                    envelope.immutable_hash,
                )
                self.assertEqual(
                    persisted_task.get("pending_action") or {},
                    {},
                )
                self.assertEqual(
                    (persisted.get("provenance_state") or {}).get("actions")
                    or {},
                    {},
                )

    async def test_internal_recovery_adopts_once_then_reaches_fake_executor(
        self,
    ) -> None:
        task_id = "session-1788681718-internal-recovery-adoption"
        base_state = self._approved_generate_state(task_id)
        state, decision, envelope, _old_target = (
            await self._install_recovery_handoff(base_state, external=False)
        )
        self.provider.calls.clear()
        self.provider.responses = [self.saved_response_content]

        events, execute_calls = await self._run_generate_segment(state)

        self.assertEqual(
            [next(iter(event)) for event in events],
            ["generate", "execute"],
        )
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(execute_calls), 1)
        outcome = events[0]["generate"]["generate_outcome"]
        self.assertEqual(outcome["type"], "action_ready")
        self.assertEqual(
            execute_calls[0]["operation"],
            "check_contig_compatibility",
        )
        self.assertTrue(execute_calls[0]["recovery_generated"])
        self.assertEqual(
            execute_calls[0]["recovery_execution_envelope"]["immutable_hash"],
            envelope.immutable_hash,
        )
        durable = load_plan_state(self.root, task_id)
        task_state = durable["task_state"]
        claim = task_state["recovery_state"]["generation_claim"]
        self.assertEqual(claim["status"], "action_adopted")
        self.assertEqual(
            task_state["pending_action"]["action_id"],
            execute_calls[0]["action_id"],
        )
        self.assertEqual(
            task_state["recovery_state"]["active_recovery"]["recovery_id"],
            decision.decision_id,
        )

    async def test_external_handoff_adoption_survives_waiting_sidecar_failure(
        self,
    ) -> None:
        task_id = "session-1788681718-external-sidecar-crash"
        base_state = self._approved_generate_state(task_id)
        state, _decision, envelope, _old_target = (
            await self._install_recovery_handoff(base_state)
        )
        input_checks = "\n".join(
            f'test -r "{path}"'
            for path in self.path_by_handle.values()
        )
        self.provider.calls.clear()
        self.provider.responses = [
            "<shell>#!/bin/bash\n"
            "#SBATCH --job-name=biomind-crash-guard\n"
            "set -euo pipefail\n"
            f"{input_checks}\n"
            "printf 'candidate checked\\n'\n"
            "</shell>"
        ]

        with patch.object(
            bio_agents,
            "save_waiting_state",
            side_effect=OSError("simulated waiting sidecar crash window"),
        ):
            update = await self.agent._schema2_runtime_node_handlers[
                "generate"
            ](deepcopy(state))

        self.assertEqual(
            update["generate_outcome"]["type"],
            "user_input_needed",
        )
        durable = load_plan_state(self.root, task_id)
        task_state = dict(durable.get("task_state") or {})
        self.assertEqual(task_state.get("pending_action") or {}, {})
        self.assertEqual(
            task_state["workflow_status"],
            "AWAITING_USER_SUBMISSION",
        )
        self.assertEqual(
            task_state["status"],
            "waiting_user",
        )
        question = dict(task_state.get("pending_question") or {})
        self.assertEqual(
            question["type"],
            "external_execution_submission",
        )
        authority = dict(task_state.get("external_execution_handoff") or {})
        self.assertEqual(
            authority["status"],
            "AWAITING_USER_SUBMISSION",
        )
        self.assertEqual(authority["execution_mode"], "EXTERNAL_HANDOFF")
        self.assertEqual(authority["result_contract"], "EXECUTION_BUNDLE")
        self.assertEqual(
            authority["execution_bundle"]["execution_envelope"][
                "immutable_hash"
            ],
            envelope.immutable_hash,
        )
        self.assertEqual(
            task_state["recovery_state"]["generation_claim"]["status"],
            "action_adopted",
        )
        self.assertFalse(
            (
                self.root
                / "task_outputs"
                / task_id
                / "waiting_state.json"
            ).exists()
        )
        semantic = self.agent._resolve_schema2_resume_state(task_id, {})
        self.assertEqual(semantic.type, "awaiting_user_input")

    async def test_direct_and_resumed_execute_never_launch_external_bundle(
        self,
    ) -> None:
        direct_task_id = "session-1788681718-external-direct-execute"
        direct_state = self._approved_generate_state(direct_task_id)
        direct_action = {
            "type": "execute",
            "action_id": "forged-external-action",
            "result_contract": "EXECUTION_BUNDLE",
            "execution_mode": "EXTERNAL_HANDOFF",
            "recovery_generated": True,
        }
        direct_state["pending_action"] = deepcopy(direct_action)
        direct_durable = load_plan_state(self.root, direct_task_id)
        direct_task_state = dict(direct_durable.get("task_state") or {})
        direct_task_state["pending_action"] = deepcopy(direct_action)
        direct_durable["task_state"] = direct_task_state
        record_provenance_event(
            direct_durable,
            "action_bound",
            {
                "task_id": direct_task_id,
                "action": deepcopy(direct_action),
                "raw_action": deepcopy(direct_action),
            },
        )
        save_plan_state(self.root, direct_task_id, direct_durable)
        with patch.object(
            bio_agents,
            "run_script_path",
            side_effect=AssertionError("external action reached local launcher"),
        ) as launch:
            direct = await self.agent._schema2_runtime_node_handlers[
                "execute"
            ](deepcopy(direct_state))
        launch.assert_not_called()
        self.assertEqual(
            direct["execute_outcome"]["type"],
            "pre_execution_rejected",
        )
        self.assertEqual(
            direct["execute_outcome"]["payload"]["violation_code"],
            "EXTERNAL_HANDOFF_LOCAL_EXECUTION_FORBIDDEN",
        )
        revoked_direct = load_plan_state(self.root, direct_task_id)
        self.assertEqual(
            revoked_direct["task_state"].get("pending_action") or {},
            {},
        )
        self.assertEqual(
            revoked_direct["provenance_state"]["actions"][
                direct_action["action_id"]
            ]["status"],
            "revoked",
        )

        resume_task_id = "session-1788681718-external-resume-execute"
        base_state = self._approved_generate_state(resume_task_id)
        recovery_state, _decision, _envelope, _old_target = (
            await self._install_recovery_handoff(base_state)
        )
        input_checks = "\n".join(
            f'test -r "{path}"'
            for path in self.path_by_handle.values()
        )
        self.provider.calls.clear()
        self.provider.responses = [
            "<shell>#!/bin/bash\n"
            "#SBATCH --job-name=biomind-resume-guard\n"
            "set -euo pipefail\n"
            f"{input_checks}\n"
            "printf 'candidate checked\\n'\n"
            "</shell>"
        ]
        generated = await self.agent._schema2_runtime_node_handlers[
            "generate"
        ](deepcopy(recovery_state))
        self.assertEqual(
            generated["generate_outcome"]["type"],
            "user_input_needed",
        )
        durable = load_plan_state(self.root, resume_task_id)
        task_state = dict(durable.get("task_state") or {})
        authority = dict(task_state["external_execution_handoff"])
        stale_checkpoint = {
            **deepcopy(base_state),
            "pending_action": deepcopy(authority["adopted_action"]),
        }
        resume_runner = self.agent._build_schema2_resume_runner_state(
            resume_task_id,
            checkpoint_values=stale_checkpoint,
        )
        self.assertEqual(
            resume_runner["resume_state"]["type"],
            "awaiting_user_input",
        )
        with patch.object(
            bio_agents,
            "run_script_path",
            side_effect=AssertionError("resumed external action was launched"),
        ) as launch:
            resumed = await self.agent._schema2_runtime_node_handlers[
                "execute"
            ](deepcopy(resume_runner))
        launch.assert_not_called()
        self.assertEqual(
            resumed["execute_outcome"]["type"],
            "user_input_needed",
        )
        self.assertEqual(
            resumed["execute_outcome"]["payload"]["question_id"],
            task_state["pending_question"]["question_id"],
        )


if __name__ == "__main__":
    unittest.main()
