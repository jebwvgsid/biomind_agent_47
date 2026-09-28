from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from agent_core.plan_contract import StateCASConflict
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.skill_access import (
    SkillAccessService,
    new_skill_access_state,
    record_provider_visible_materials,
    selected_skill_materials_for_carry,
)
from agent_core.skill_manager import SkillManager
from agent_core.workflow.context_compaction import (
    ContextBudgetPolicy,
    TokenEstimate,
)
from agent_core.workflow.generate_handoff import project_corrective_generate_seed
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import (
    build_causal_failure_record,
    project_recovery_seed,
)
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.stage_contracts import GenerateSeed
from tests.test_recovery_projection import _durable, _execute_failure


def _complete_message(**kwargs):
    # Explicitly complete provider fixtures; missing finish metadata is not
    # evidence that a live response completed successfully.
    kwargs.setdefault("response_metadata", {"finish_reason": "tool_calls" if kwargs.get("tool_calls") else "stop"})
    return AIMessage(**kwargs)


class _CodepointEstimator:
    name = "recovery_fixture_codepoints"
    version = "1"
    exact_or_estimated = "exact"

    def estimate(self, text: str) -> TokenEstimate:
        return TokenEstimate(
            tokens=len(text),
            estimator_name=self.name,
            estimator_version=self.version,
            exact_or_estimated=self.exact_or_estimated,
        )


class _Store:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.save_count = 0

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        if int(candidate.get("state_generation") or 0) != int(
            self.value.get("state_generation") or 0
        ):
            raise StateCASConflict("fixture stale generation")
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1


class _ActivationCASStore(_Store):
    def __init__(self, value, *, behavior):
        super().__init__(value)
        self.behavior = str(behavior)
        self.activation_attempted = False

    def save(self, data_path, task_id, candidate):
        is_activation = bool(
            (candidate.get("task_state") or {}).get(
                "recovery_context_compaction"
            )
            and not (candidate.get("task_state") or {}).get(
                "typed_recovery_receipts"
            )
        )
        if is_activation and not self.activation_attempted:
            self.activation_attempted = True
            self.save_count += 1
            if self.behavior == "conflict_before_commit":
                self.value["state_generation"] = int(
                    self.value.get("state_generation") or 0
                ) + 1
                raise StateCASConflict("fixture concurrent writer")
            if self.behavior == "ack_lost_after_commit":
                if int(candidate.get("state_generation") or 0) != int(
                    self.value.get("state_generation") or 0
                ):
                    raise StateCASConflict("fixture stale generation")
                self.value = copy.deepcopy(candidate)
                self.value["state_generation"] = int(
                    candidate["state_generation"]
                ) + 1
                raise StateCASConflict("fixture acknowledgement lost")
        return super().save(data_path, task_id, candidate)


def _directive() -> str:
    return json.dumps({
        "schema_version": 1,
        "directive_id": "directive-compacted-recovery",
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": "The failed generated action requires regeneration.",
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate only the failed action.",
        "evidence_refs": ["execution-1"],
    })


class _ToolThenDirectiveModel:
    def __init__(self, *, repeat_tool: bool = False, diagnostic_chars: int = 7_000):
        self.requests = []
        self.repeat_tool = repeat_tool
        self.diagnostic_chars = int(diagnostic_chars)

    async def ainvoke(self, messages, *, max_tokens):
        self.requests.append(list(messages))
        if len(self.requests) == 1 or self.repeat_tool:
            return _complete_message(
                content=(
                    "DIAGNOSTIC-WORKING-NOTES\n" + "N" * self.diagnostic_chars
                    if len(self.requests) == 1 else ""
                ),
                tool_calls=[{
                    "id": f"failure-call-{len(self.requests)}",
                    "name": "failure_inspect",
                    "args": {},
                    "type": "tool_call",
                }],
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                },
            )
        return _complete_message(
            content=_directive(),
            usage_metadata={
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
            },
        )


class _CrashAfterCompactionModel(_ToolThenDirectiveModel):
    async def ainvoke(self, messages, *, max_tokens):
        if not self.requests:
            return await super().ainvoke(messages, max_tokens=max_tokens)
        self.requests.append(list(messages))
        raise asyncio.CancelledError("fixture crash after epoch CAS")


class _ResumeAwareDirectiveModel:
    def __init__(self):
        self.requests = []

    async def ainvoke(self, messages, *, max_tokens):
        self.requests.append(list(messages))
        if not any(
            "recovery_context_summary" in str(message.content)
            for message in messages
        ):
            return _complete_message(
                content="unexpected old epoch",
                tool_calls=[{
                    "id": "unexpected-replayed-tool",
                    "name": "failure_inspect",
                    "args": {},
                    "type": "tool_call",
                }],
            )
        return _complete_message(content=_directive())


class _SummarizeLocalCompactor:
    def __init__(self):
        self.requests = []

    async def __call__(self, messages, *, max_tokens, **context):
        self.requests.append(copy.deepcopy(list(messages)))
        human = str(messages[1]["content"])
        envelope = json.loads(human.split("\n", 1)[1].rsplit("\n", 1)[0])
        request = envelope["request"]
        units = list(request.get("source_units") or request["source_manifest"]["units"])
        retained = [item["unit_id"] for item in units if item["selected"]]
        summarized = [item["unit_id"] for item in units if not item["selected"]]
        return _complete_message(
            content=json.dumps({
                "schema_version": 1,
                "compaction_request_id": request["compaction_request_id"],
                "source_epoch_id": request["source_epoch_id"],
                "source_manifest_hash": request["source_manifest_hash"],
                "mode": "normal",
                "retain_exact_unit_ids": retained,
                "summary_source_unit_ids": summarized,
                "summary": (
                    "The prior diagnostic inspection is archived by immutable "
                    "reference."
                ),
                "material_retrieval_state": [],
                "unresolved_items": [],
                "selection_rationale": (
                    "Retain selected knowledge exactly and summarize the complete "
                    "assistant/tool protocol group."
                ),
            }),
            usage_metadata={
                "input_tokens": 200,
                "output_tokens": 40,
                "total_tokens": 240,
            },
        )


class _SummarizeSelectedCompactor:
    def __init__(self):
        self.requests = []

    async def __call__(self, messages, *, max_tokens, **context):
        self.requests.append(copy.deepcopy(list(messages)))
        human = str(messages[1]["content"])
        envelope = json.loads(human.split("\n", 1)[1].rsplit("\n", 1)[0])
        request = envelope["request"]
        units = list(request.get("source_units") or request["source_manifest"]["units"])
        recovery = []
        for unit in units:
            if not unit["selected"]:
                continue
            recovery.append({
                "source_unit_id": unit["unit_id"],
                "skill_id": unit["skill_id"],
                "source_ref": unit["source_ref"],
                "content_hash": unit["content_hash"],
                "coverage": unit["coverage"],
                "residency": "summarized",
                "reason": "capacity pressure",
                "summary": "Selected procedure source is available by immutable reference.",
                "missing_details": "Exact command flags and examples were omitted.",
                "retrieval_ref": unit["provenance"]["retrieval_ref"],
                "scope": unit["scope"],
            })
        return _complete_message(content=json.dumps({
            "schema_version": 1,
            "compaction_request_id": request["compaction_request_id"],
            "source_epoch_id": request["source_epoch_id"],
            "source_manifest_hash": request["source_manifest_hash"],
            "mode": "capacity_pressure",
            "retain_exact_unit_ids": [],
            "summary_source_unit_ids": [
                item["unit_id"] for item in units
            ],
            "summary": "Recovery history and selected source were compacted.",
            "material_retrieval_state": recovery,
            "unresolved_items": [],
            "selection_rationale": "Summarize all units under capacity pressure.",
        }))


class _RetainSkillDocumentExternalizeScriptCompactor:
    """Keep one exact selected document but externalize its selected script."""

    def __init__(self):
        self.requests = []
        self.script_unit = {}

    async def __call__(self, messages, *, max_tokens, **context):
        self.requests.append(copy.deepcopy(list(messages)))
        human = str(messages[1]["content"])
        envelope = json.loads(human.split("\n", 1)[1].rsplit("\n", 1)[0])
        request = envelope["request"]
        if request.get("operation") in {"map_source_segment", "merge_segment_summaries"}:
            from tests.test_context_256k_entrypoints import _valid_segment_map, _valid_summary_merge
            result = (_valid_segment_map(request) if request["operation"] == "map_source_segment"
                      else _valid_summary_merge(request))
            return _complete_message(content=json.dumps(result))
        units = list(request.get("source_units") or request["source_manifest"]["units"])
        documents = [
            item for item in units
            if item["selected"]
            and str(item["source_ref"]).startswith("skill-snapshot:")
        ]
        scripts = [
            item for item in units
            if item["selected"]
            and str(item["source_ref"]).startswith("skill-excerpt:")
        ]
        if len(documents) != 1 or len(scripts) != 1:
            raise AssertionError(
                f"expected one selected document and script: {units!r}"
            )
        script = scripts[0]
        self.script_unit = copy.deepcopy(script)
        recovery = [{
            "source_unit_id": script["unit_id"],
            "skill_id": script["skill_id"],
            "source_ref": script["source_ref"],
            "content_hash": script["content_hash"],
            "coverage": script["coverage"],
            "residency": "summarized",
            "reason": "capacity pressure requires exact script externalization",
            "summary": "The immutable recovery script remains available by reference.",
            "missing_details": "Exact sentinels and indentation are not resident.",
            "retrieval_ref": script["provenance"]["retrieval_ref"],
            "scope": script["scope"],
        }]
        retained = [documents[0]["unit_id"]]
        return _complete_message(content=json.dumps({
            "schema_version": 1,
            "compaction_request_id": request["compaction_request_id"],
            "source_epoch_id": request["source_epoch_id"],
            "source_manifest_hash": request["source_manifest_hash"],
            "mode": "capacity_pressure",
            "retain_exact_unit_ids": retained,
            "summary_source_unit_ids": [
                item["unit_id"] for item in units
                if item["unit_id"] not in retained
            ],
            "summary": (
                "The prior failure inspection is archived; the selected "
                "script must be restored through its immutable reference."
            ),
            "material_retrieval_state": recovery,
            "unresolved_items": [],
            "selection_rationale": (
                "Retain the selected Skill document exactly and externalize "
                "the selected script under measured capacity pressure."
            ),
        }))


class _CompactionThenExactResourceModel:
    def __init__(self, *, material, exact_body):
        self.material = copy.deepcopy(material)
        self.exact_body = exact_body
        self.requests = []
        self.retrieval_observation = {}

    async def ainvoke(self, messages, *, max_tokens):
        self.requests.append(list(messages))
        ordinal = len(self.requests)
        if ordinal == 1:
            return _complete_message(
                content="PRE-COMPACTION-DIAGNOSIS\n" + "D" * 27_000,
                tool_calls=[{
                    "id": "integration-b-failure-inspect",
                    "name": "failure_inspect",
                    "args": {},
                    "type": "tool_call",
                }],
            )
        if ordinal == 2:
            return _complete_message(
                content="Restore the externalized selected script exactly.",
                tool_calls=[{
                    "id": "integration-b-exact-script-read",
                    "name": "skill_resource",
                    "args": {
                        "skill_id": self.material["skill_id"],
                        "retrieval_ref": self.material["source_ref"],
                        "content_hash": self.material["content_hash"],
                        "coverage": self.material["coverage"],
                    },
                    "type": "tool_call",
                }],
            )
        if ordinal == 3:
            tool_messages = [
                message for message in messages
                if getattr(message, "type", "") == "tool"
                and getattr(message, "tool_call_id", "")
                == "integration-b-exact-script-read"
            ]
            if len(tool_messages) == 1:
                self.retrieval_observation = json.loads(
                    str(tool_messages[0].content)
                )
            return _complete_message(content=json.dumps({
                "schema_version": 1,
                "directive_id": "directive-integration-b",
                "task_id": "task-recovery",
                "action": "retry_step",
                "summary": "The failed generated action requires regeneration.",
                "reason_code": "GENERATED_ACTION_FAILED",
                "target_step_id": "step-1",
                "instruction": "Regenerate only the failed action.",
                "evidence_refs": [
                    "execution-1", self.material["source_ref"],
                ],
            }))
        raise AssertionError(f"unexpected Recovery provider call {ordinal}: {str(messages[-1].content)[-1800:]}")


def _fixture(*, durable=None):
    durable = copy.deepcopy(durable) if durable is not None else _durable()
    record = build_causal_failure_record(
        "execute_outcome",
        _execute_failure(),
        durable,
        task_id="task-recovery",
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    state = {
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
        "messages": ["global history must remain excluded"],
    }
    return durable, record, state


def _selected_materials(_seed, _durable):
    body = "SELECTED-SKILL-EXACT-SENTINEL\n" + "A" * 64
    return [{
        "material_id": "selected-material-1",
        "skill_id": "genomics/example",
        "source_ref": "skill-snapshot:immutable-example-v1",
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "coverage": {"kind": "full"},
        "residency": "exact",
        "selected": True,
        "body": body,
    }]


def _skill_access_fixture(root):
    skills_root = Path(root) / "skills"
    skill_dir = skills_root / "fixture" / "recovery-carry"
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        "name: Recovery carry\n"
        "description: Offline Recovery compaction fixture\n"
        "---\n"
        "# Procedure\n"
        "CROSS-PHASE-EXACT-BODY-MUST-NOT-REVIVE\n"
        "Use the immutable fixture flags and examples.\n"
        + "Exact selected procedure detail; " * 2500
    )
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    service = SkillAccessService(SkillManager(str(skills_root)))
    access = new_skill_access_state()
    access, snapshot, _created = service.ensure_snapshot(
        access,
        "fixture/recovery-carry",
        scope="task",
        status="active",
        selected_at_task_revision=1,
    )
    access, _documents = service.load_skill_documents(
        access,
        ["fixture/recovery-carry"],
        task_revision=1,
        request_id="load-recovery-carry",
    )
    access = record_provider_visible_materials(
        access,
        [{
            "kind": "skill_document",
            "snapshot_id": snapshot["snapshot_id"],
            "content_hash": snapshot["content_hash"],
        }],
        phase="planner",
        invocation_id="planner-visible-fixture",
        event_id="planner-visible-fixture:event-1",
    )
    exact = selected_skill_materials_for_carry(
        access, current_step_id="step-1"
    )[0]
    return access, exact, body


def _selected_script_access_fixture(root):
    skills_root = Path(root) / "skills"
    skill_dir = skills_root / "fixture" / "recovery-script"
    script_path = skill_dir / "scripts" / "recover.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    skill_body = (
        "---\n"
        "name: Recovery script fixture\n"
        "description: Immutable retrieval integration fixture\n"
        "---\n"
        "# Procedure\n"
        "SKILL-DOCUMENT-RETAINED-SENTINEL\n"
    )
    script_body = (
        "SCRIPT-HEAD-SENTINEL\n"
        "def repair(value):\n"
        "    nested = {\"exact\": [1, 2, 3]}\n"
        "    return value, nested\n"
        "SCRIPT-TAIL-SENTINEL\n"
        + "# immutable procedure detail " * 650
    )
    (skill_dir / "SKILL.md").write_text(skill_body, encoding="utf-8")
    script_path.write_text(script_body, encoding="utf-8")
    service = SkillAccessService(SkillManager(str(skills_root)))
    access = new_skill_access_state()
    access, snapshot, _created = service.ensure_snapshot(
        access,
        "fixture/recovery-script",
        scope="task",
        status="active",
        selected_at_task_revision=1,
    )
    access, _documents = service.load_skill_documents(
        access,
        ["fixture/recovery-script"],
        task_revision=1,
        request_id="integration-b-load-document",
    )
    access, resource_result = service.serve_context_request(
        access,
        {
            "decision": "REQUEST_SKILL_CONTEXT",
            "reason": "Read the exact selected recovery script.",
            "requests": [{
                "skill_id": "fixture/recovery-script",
                "operation": "resource",
                "resource_path": "scripts/recover.py",
            }],
        },
        runtime_envelope={
            "caller_node": "planner",
            "caller_invocation_id": "integration-b-planner",
            "task_revision": 1,
            "plan_revision": 1,
            "authoritative_user_turn_revision": 1,
            "current_step_id": "step-1",
        },
    )
    excerpt = dict(resource_result["results"][0])
    access = record_provider_visible_materials(
        access,
        [
            {
                "kind": "skill_document",
                "snapshot_id": snapshot["snapshot_id"],
                "content_hash": snapshot["content_hash"],
            },
            {
                "kind": "excerpt",
                "excerpt_id": excerpt["excerpt_id"],
                "content_hash": excerpt["content_hash"],
            },
        ],
        phase="planner",
        invocation_id="integration-b-planner",
        event_id="integration-b-visible-materials",
    )
    materials = selected_skill_materials_for_carry(
        access, current_step_id="step-1"
    )
    script = next(
        item for item in materials
        if item.get("source_kind") == "skill_resource"
    )
    document = next(
        item for item in materials
        if item.get("source_kind") == "skill_document"
    )
    return service, access, document, script, script_body, script_path


def _low_policy() -> ContextBudgetPolicy:
    # The first System+Seed+selected-material request fits.  Adding one full
    # assistant/tool round crosses this token trigger and exercises the actual
    # production node boundary.  The larger absolute capacity also keeps the
    # compactor's complete one-shot request below its own hard wire boundary;
    # hierarchical compactor behavior has dedicated contract tests.
    return ContextBudgetPolicy(
        application_context_limit_tokens=200_000,
        known_provider_context_limit_tokens=40_000,
        output_reserve_tokens=100,
        compressor_output_reserve_tokens=100,
        estimation_margin_tokens=100,
        safety_headroom_tokens=100,
        # Current System+Seed (~17.5k) plus tool schema exceeds 24k
        # fixture tokens. Trigger after the completed diagnostic/tool group.
        trigger_ratio=0.75,
        target_ratio=0.65,
    )


def _integration_b_policy() -> ContextBudgetPolicy:
    # Keep the compacted epoch plus one exact resource observation below the
    # trigger while the deliberately long pre-compaction diagnostic crosses
    # it.  The production accounting and compactor path are unchanged.
    return ContextBudgetPolicy(
        application_context_limit_tokens=200_000,
        known_provider_context_limit_tokens=80_000,
        output_reserve_tokens=1_000,
        compressor_output_reserve_tokens=1_000,
        estimation_margin_tokens=1_000,
        safety_headroom_tokens=1_000,
        trigger_ratio=0.80,
        target_ratio=0.50,
        compressor_safe_input_tokens=60_000,
    )




if __name__ == "__main__":
    unittest.main()
